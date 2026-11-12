"""livespec, adopted: the code index the core reads before it routes, briefs and reports.

livespec keeps its index in ``<repo>/.mcp-docs/docs.db`` (or a shared file
named by ``[workspace] group_db`` in ``.livespec.toml``): symbols, a call graph,
code chunks under full-text search, and the links between symbols and specs.
That file is readable with ``sqlite3`` in read-only mode, so the core uses it
without a token and without an MCP call:

- **routing** — a task whose code is linked to a critical spec, or whose
  symbol is called from everywhere, starts one rung up;
- **briefing** — the worker's brief carries a dossier (`Livespec.dossier`): the
  symbols the request is about, where they are, what they call and who calls them,
  the specs they implement, the tests that cover them, the specs the request's
  words find and the helpers with names close to what it says to add;
- **reporting** — after a pass, which specs the change touched and which
  callers in other files the change did not touch;
- **freshness** — an index that is missing, or older than the last commit, is built or
  refreshed with livespec's own headless CLI (``livespec index``, incremental) before
  the worker starts, for as long as the person's ``livespec_wait`` allows
  (`Livespec.ensure_fresh`); never for a folder that is no repository, which is
  indexed through each repository inside it.

A worker is not handed livespec's MCP server: locks asks the index itself and briefs
the answers. Only the independent check keeps the server, behind the read-only guard,
with a hint per kind of work and the ``workspace`` every call requires.

Checked against livespec 0.36.0. Every tool named here exists in that release;
`TOOLS` is the pinned list, and a test holds every hint to it.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from locks import repo
from locks.adapters import ABSENT, PRESENT, UNREADABLE, Briefing, Dossier, Status
from locks.matrix import READ_ONLY_KINDS
from locks.store import home
from locks.text import clip, words

MINIMUM_VERSION = (0, 32, 0)
#: Characters kept free for the two lines that say what did not fit and what the caps left out.
_TAIL_ROOM = 160

#: What locks runs through `uvx` when no livespec plugin entry and no `livespec` on PATH is used:
#: the release this adapter was checked against, unless an installed plugin's own manifest names
#: a newer one (`Livespec.pin`). livespec is part of locks' base, so having `uv` is enough: there
#: is no second plugin to install.
PINNED = "0.36.0"

#: The livespec tools locks names to a worker or calls itself. Verified to exist in 0.36.0.
TOOLS = frozenset({
    "quick_orient", "find_symbol", "who_calls", "who_does_this_call", "analyze_impact",
    "git_diff_impact", "get_symbol_source", "get_spec_implementation", "list_specs",
    "audit_coverage", "find_orphan_tests", "find_dead_code", "search",
    "read_unit", "search_similar", "index_project", "sync_openspec", "validate_openspec",
})

_HINTS: Mapping[str, str] = {
    "debug-repro": "find the failing symbol (find_symbol), then who_calls and analyze_impact before changing it",
    "debug-unclear": "map the path first: who_calls, who_does_this_call and analyze_impact on every suspect",
    "implement": "before editing a symbol, who_calls and analyze_impact; get_spec_implementation for the spec "
                 "it serves",
    "feature": "list_specs and get_spec_implementation for what exists; analyze_impact on each symbol you change",
    "refactor": "who_calls on every symbol you move or rename; find_dead_code for what can go",
    "ui": "find_symbol for the component; who_calls to see where it renders",
    "test": "audit_coverage and find_orphan_tests to see what is untested; get_symbol_source to read the target",
    "review-routine": "analyze_impact on each symbol the change touches; who_calls for the callers it may break",
    "review-critical": "analyze_impact on each symbol the change touches; get_spec_implementation for every "
                       "spec it touches",
    "explore": "quick_orient for the overview, then search and find_symbol",
    "plan": "quick_orient, list_specs and analyze_impact on the parts the plan changes",
}

#: A spec a task writes or changes is linked to its code, so the next task finds the code from the spec.
SPEC_ID = "<capability>-<requirement-name>"

#: Spec priority that makes a task critical.
_CRITICAL_PRIORITIES = frozenset({"critical"})
#: Callers above which a symbol is load-bearing enough to start one rung up.
WIDELY_USED = 15
MAP_SYMBOLS = 6
#: The dossier's room in a brief, in characters: what does not fit is said by count.
DOSSIER_CHARS = 6000
#: Symbols a dossier describes (the ones the request names, then those of the files it names, then the
#: best full-text matches), what it lists of each, and the symbols of one named file.
DOSSIER_SYMBOLS, DOSSIER_LINKS, FILE_SYMBOLS = 8, 5, 4
#: Specs a dossier carries, and the characters of a requirement's text it quotes.
DOSSIER_SPECS, SPEC_TEXT_CHARS = 6, 300
#: Words a request must share with a spec's title and text for its words to find it: one is never enough.
SPEC_MIN_TERMS = 2
#: Helpers listed beside each identifier the request says to add.
SIMILAR_HELPERS = 3
INDEX_TIMEOUT_S = 600

_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\b")
#: The verbs that say a request adds something; case-insensitive, then the words up to the name.
_TO_ADD = re.compile(r"\b(?:add|create|write|introduce|implement|extract)\b([^.\n`]{0,60}`[^`\n]+`|[^.\n`]{0,60})",
                     re.IGNORECASE)
#: Words between the verb and the name that are never the name: `add a new helper parse_duration`.
_FILLER = frozenset({
    "a", "an", "the", "new", "small", "simple", "basic", "helper", "helpers", "function", "functions",
    "method", "methods", "class", "classes", "utility", "util", "to", "for", "that", "which", "called",
    "named", "in", "into", "of", "some", "another", "one", "our",
})


def _to_add(text: str) -> list[str]:
    """The identifiers the request says to add: after the verb, the first word that looks like code,
    skipping articles and adjectives (`Add a helper parse_duration`, `implement the new retry_budget
    function`, "create `AuthClient`")."""
    found: list[str] = []
    for m in _TO_ADD.finditer(text):
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", m.group(1)):
            if word.lower() in _FILLER:
                continue
            if _looks_like_code(word):
                found.append(word)
            break
    return list(dict.fromkeys(found))


_NAME_PART = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")

Runner = Callable[..., subprocess.CompletedProcess]


def _run_in_group(argv: Sequence[str], *, timeout: float, env: Mapping[str, str], capture_output: bool = True,
                  text: bool = True, check: bool = False) -> subprocess.CompletedProcess[str]:
    """`subprocess.run` for an index, with the child leading its own process group and the timeout
    ending the whole group. `uvx` starts the `livespec index` that does the work as its child:
    `subprocess.run` kills only `uvx` at the timeout, and that child goes on indexing with nobody
    holding the lock, so `Livespec.indexing` says no and a second index starts beside it."""
    pipe = subprocess.PIPE if capture_output else None
    with subprocess.Popen(list(argv), stdout=pipe, stderr=pipe, text=text, env=dict(env),
                          start_new_session=True) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:  # the timeout, and an interrupt too, as `subprocess.run` treats both
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
    done = subprocess.CompletedProcess(list(argv), proc.returncode, out, err)
    if check:
        done.check_returncode()
    return done

#: What `ensure_fresh` found of one repository's index when its task's worker was about to start.
FRESH, BUILT, LATE, FAILED = "fresh", "built", "late", "failed"
#: How often a refresh another process holds is looked at while a task waits on it.
POLL_S = 0.5
#: Stands for a started refresh whose `Popen` handed nothing back: its lock is looked at instead.
_STARTED = object()


@dataclass(frozen=True)
class _Wait:
    """How long `ensure_fresh` waits, shared by every repository it waits on."""

    began: float
    deadline: float
    wait_s: float
    pause: Callable[[float], None]
    clock: Callable[[], float]


@dataclass(frozen=True)
class Indexed:
    """One repository's index as `ensure_fresh` left it."""

    root: Path
    outcome: str
    detail: str = ""

    def line(self) -> str:
        """What the plan says of it; nothing for an index that was already current."""
        if self.outcome == FRESH:
            return ""
        return f"livespec: {self.root.name}: {self.detail}"


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def _looks_like_code(token: str) -> bool:
    """`parse_duration`, `AuthClient`, `api.routes` — not `the` or `fix`."""
    return "_" in token or "." in token or (token[:1].isalpha() and any(c.isupper() for c in token[1:]))


#: What `uv` says when a release is not on the package registry.
_NOT_PUBLISHED = re.compile(r"not found in the package registry|no version of livespec|"
                            r"no solution found when resolving", re.IGNORECASE)


#: The release an error names: uv says `livespec==X` whatever form the command took.
_NAMED_RELEASE = re.compile(r"\blivespec(?:\[[^\]]*\])?(?:==|@)([0-9][\w.+-]*)", re.IGNORECASE)


def unpublished_in(text: str) -> str | None:
    """The livespec release uv's resolver says is not on the registry ("No solution found
    when resolving tool dependencies … livespec==X", "there is no version of
    livespec==X"), or None when the text says no such thing."""
    if not _NOT_PUBLISHED.search(text or ""):
        return None
    found = _NAMED_RELEASE.search(text)
    return found.group(1).rstrip(".,;:") if found else None


def _uvx_release(server: Mapping[str, Any]) -> str | None:
    """The livespec release a `uvx livespec@X` (or `livespec==X`) entry runs, or None."""
    if Path(str(server.get("command") or "")).name not in ("uvx", "uv"):
        return None
    for arg in server.get("args") or []:
        match = re.fullmatch(r"livespec(?:\[[^\]]*\])?(?:@|==)([0-9][\w.+-]*)", str(arg))
        if match:
            return match.group(1)
    return None


def _version_tuple(text: str | None) -> tuple[int, ...] | None:
    if not text:
        return None
    found = re.findall(r"\d+", text)
    return tuple(int(n) for n in found[:3]) if found else None


class LivespecError(RuntimeError):
    """livespec could not be asked: it did not start, did not answer in time, or answered with an error."""


class Livespec:
    name = "livespec"

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        runner: Runner = _run_in_group,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.env = dict(os.environ) if env is None else dict(env)
        self.runner = runner
        self.popen = popen

    # --- where livespec is ------------------------------------------------

    def _config_root(self) -> Path:
        return Path(self.env.get("CLAUDE_CONFIG_DIR") or Path(self.env.get("HOME", "~")).expanduser() / ".claude")

    def _plugin_config(self) -> tuple[dict[str, Any], str] | None:
        """The server entry of an installed livespec plugin, and its version."""
        root = self._config_root()
        found = []
        for cfg in root.glob("plugins/cache/*/livespec/*/.mcp.json"):
            found.append((_version_tuple(cfg.parent.name) or (0,), cfg))
        for _, cfg in sorted(found, reverse=True):
            try:
                server = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["livespec"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if isinstance(server, dict) and server.get("command"):
                return server, cfg.parent.name
        return None

    def _plugin_release(self) -> str | None:
        """The release the newest installed livespec plugin declares in its own manifest
        (`.claude-plugin/plugin.json`), unless uv found it missing from the registry."""
        found = [(_version_tuple(m.parent.parent.name) or (0,), m)
                 for m in self._config_root().glob("plugins/cache/*/livespec/*/.claude-plugin/plugin.json")]
        for _, manifest in sorted(found, reverse=True):
            try:
                version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if isinstance(version, str) and _version_tuple(version) and version not in self.unpublished():
                return version
        return None

    def pin(self) -> str:
        """The release `uvx` runs when no plugin entry and no `livespec` on PATH is used: the
        installed plugin's own, else the one this adapter was built against."""
        return self._plugin_release() or PINNED

    def _unpublished_path(self) -> Path:
        return home(self.env) / "work" / "livespec-unpublished.json"

    def unpublished(self) -> set[str]:
        """The releases `uvx` found missing from the package registry: a plugin may
        name one it was tagged with before it was published."""
        try:
            return {str(v) for v in json.loads(self._unpublished_path().read_text(encoding="utf-8"))}
        except (OSError, ValueError, TypeError):
            return set()

    def _remember_unpublished(self, version: str) -> None:
        path = self._unpublished_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(self.unpublished() | {version})), encoding="utf-8")

    def server(self) -> Mapping[str, Any] | None:
        """How to run livespec: an installed plugin's own entry (unless it names a
        release `uvx` found unpublished), then a `livespec` on PATH, then the pinned
        release through `uvx`."""
        plugin = self._plugin_config()
        if plugin and _uvx_release(plugin[0]) not in self.unpublished():
            return plugin[0]
        path = self.env.get("PATH")
        if shutil.which("livespec", path=path):
            return {"command": "livespec", "args": []}
        if shutil.which("uvx", path=path):
            return {"command": "uvx", "args": [f"livespec@{self.pin()}"]}
        if plugin and _uvx_release(plugin[0]) is not None and Path(str(plugin[0]["command"])).is_absolute():
            # The plugin's own `uvx` (a full path off the PATH) runs the pinned release instead.
            command = str(plugin[0]["command"])
            run = ["tool", "run"] if Path(command).name == "uv" else []
            return {"command": command, "args": [*run, f"livespec@{self.pin()}"]}
        return plugin[0] if plugin else None

    def read_only_server(self) -> Mapping[str, Any] | None:
        """The same server behind `locks.mcp_guard`'s `livespec` policy: calls that read the index
        pass, everything that could change it is refused. For a worker that only checks work."""
        server = self.server()
        if server is None:
            return None
        from locks.plugins import guarded

        return guarded(server, "livespec")

    def _version(self) -> str | None:
        plugin = self._plugin_config()
        if plugin and _uvx_release(plugin[0]) not in self.unpublished():
            return plugin[1]
        server = self.server()
        for arg in (server or {}).get("args", []):
            if isinstance(arg, str) and arg.startswith("livespec@"):
                return arg.split("@", 1)[1]
        return None

    # --- the index --------------------------------------------------------

    @staticmethod
    def db_path(root: Path) -> Path:
        config = root / ".livespec.toml"
        if config.is_file():
            try:
                raw = tomllib.loads(config.read_text(encoding="utf-8")).get("workspace", {}).get("group_db")
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, str) and raw.strip():
                path = Path(raw).expanduser()
                return path if path.is_absolute() else (root / path).resolve()
        return root / ".mcp-docs" / "docs.db"

    def roots(self, repo_dir: Path) -> list[Path]:
        """The repositories an index is about: the checkout `repo_dir` is in, else each
        repository right inside it. A folder of repositories is never one: livespec's
        own guidance is not to index the parent of many."""
        top = repo.toplevel(repo_dir)
        if top is not None:
            return [top.resolve()]
        try:
            return sorted(c.resolve() for c in repo_dir.iterdir() if (c / ".git").exists())
        except OSError:
            return []

    def indexable(self, repo_dir: Path) -> bool:
        """Whether there is a repository to index here (`roots` finds one)."""
        return bool(self.roots(repo_dir))

    def inspect(self, repo_dir: Path) -> Status:
        top = repo.toplevel(repo_dir)
        root = (top or repo_dir).resolve()
        version = self._version()
        db = self.db_path(root)
        if not db.is_file():
            if top is None:
                held = self.roots(repo_dir)
                shown = ", ".join(r.name for r in held[:6]) + (" …" if len(held) > 6 else "")
                detail = (f"{root} is a folder of {len(held)} repositories ({shown}): each is indexed on its own, "
                          "never the folder" if held else f"{root} is no git repository: nothing to index")
                return Status(self.name, ABSENT, detail, version)
            runnable = self.server() is not None
            detail = ("this repository is not indexed yet" if runnable
                      else "cannot run: no livespec, and no `uvx` to run the pinned release")
            return Status(self.name, ABSENT, detail, version)
        try:
            with _connect(db) as conn:
                row = conn.execute("SELECT id FROM project WHERE root = ? ORDER BY id LIMIT 1",
                                   (str(root),)).fetchone()
                if row is None:
                    return Status(self.name, ABSENT, f"the index at {db} has no project for {root}", version)
                project = int(row[0])
                run = conn.execute(
                    "SELECT finished_at FROM index_run WHERE project_id = ? AND finished_at IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1", (project,)).fetchone()
        except sqlite3.Error as exc:
            return Status(self.name, UNREADABLE, f"{db}: {exc}", version)
        indexed_at = run[0] if run else None
        stale = _older_than_head(indexed_at, root)
        detail = f"indexed {indexed_at} UTC" if indexed_at else "never finished an index run"
        floor = _version_tuple(version)
        if floor is not None and floor < MINIMUM_VERSION:
            checked = ".".join(map(str, MINIMUM_VERSION))
            detail += f"; v{version} predates the {checked} this adapter was checked against"
        return Status(self.name, PRESENT, detail, version, stale,
                      {"db": str(db), "project": project, "root": str(root)})

    def _index_argv(self, repo_dir: Path) -> list[str] | None:
        server = self.server()
        top = repo.toplevel(repo_dir)
        if server is None or top is None:
            return None
        return [str(server["command"]), *[str(a) for a in server.get("args", [])], "index", str(top.resolve())]

    def lock_path(self, repo_dir: Path) -> Path:
        """One lock per index file: two `livespec index` runs on the same file
        collide ("database is locked"), and a shared group index is one file for
        several repositories."""
        root = (repo.toplevel(repo_dir) or repo_dir).resolve()
        digest = hashlib.sha256(str(self.db_path(root)).encode()).hexdigest()[:16]
        return home(self.env) / "work" / f"livespec-index-{digest}.lock"

    def _failure_path(self, repo_dir: Path) -> Path:
        return self.lock_path(repo_dir).with_name(self.lock_path(repo_dir).stem.replace("index", "failed") + ".json")

    def last_failure(self, repo_dir: Path) -> str | None:
        """Why the last index run failed, while the checkout is still at the commit it
        failed at: an index that cannot be built (a workspace moved since, a broken
        config) is not tried again before every task, only once the checkout moves."""
        root = (repo.toplevel(repo_dir) or repo_dir).resolve()
        try:
            data = json.loads(self._failure_path(repo_dir).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        missing = unpublished_in(str(data.get("error") or ""))
        if missing and missing != PINNED:
            # An index that failed only because the release it ran is not published (kept by
            # a locks that did not read uv's resolver error so): the release is remembered as
            # unpublished, the pinned one runs from now on, and the failure is no failure here.
            self._remember_unpublished(missing)
            self._remember_failure(repo_dir, None)
            return None
        head = _head(root)
        return str(data.get("error") or "it failed") if head and data.get("head") == head else None

    def failure_said(self, repo_dir: Path) -> bool:
        """Whether a plan already said the failure `last_failure` gives: said once, it is
        not said again in every plan while the checkout stays where it failed. The first
        call marks it said."""
        path = self._failure_path(repo_dir)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if data.get("said"):
            return True
        with contextlib.suppress(OSError):
            path.write_text(json.dumps({**data, "said": True}), encoding="utf-8")
        return False

    def _remember_failure(self, repo_dir: Path, error: str | None) -> None:
        path = self._failure_path(repo_dir)
        if error is None:
            path.unlink(missing_ok=True)
            return
        root = (repo.toplevel(repo_dir) or repo_dir).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"head": _head(root), "error": error[:300]}), encoding="utf-8")

    def indexing(self, repo_dir: Path) -> bool:
        """Whether a refresh locks started holds this index now."""
        with _lock(self.lock_path(repo_dir), wait_s=0) as (got, _):
            return not got

    def refresh(self, repo_dir: Path, *, wait_s: float = INDEX_TIMEOUT_S) -> str:
        """`livespec index` through livespec's own CLI: a first index, or an
        incremental one. Costs time, never tokens.

        Never two at once on one index. The session-start refresh, and parallel
        tasks each planning in the same repository, would otherwise start one
        apiece and all but one fail. A refresh that finds another running waits
        for it, and then runs only if the index is still behind.
        """
        argv = self._index_argv(repo_dir)
        if argv is None:
            if repo.toplevel(repo_dir) is None:
                return (f"{repo_dir} is no git repository: livespec never indexes a folder (nor the parent of "
                        "several repositories); index each repository")
            return "livespec cannot run here; the index stays as it is"
        with _lock(self.lock_path(repo_dir), wait_s=wait_s) as (got, waited):
            if not got:
                return "another livespec index is still running; the index stays as it is for this task"
            if waited:
                status = self.inspect(repo_dir)
                if status.present and not status.stale:
                    return "livespec index refreshed by the run this one waited for"
            try:
                proc = self.runner(argv, capture_output=True, text=True, timeout=INDEX_TIMEOUT_S, check=False,
                                   env={**self.env, "LOCKS_HOOKS_OFF": "1"})
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"livespec index did not run: {exc}"
            fell_back = ""
            said = f"{proc.stderr or ''}\n{proc.stdout or ''}"
            missing = _uvx_release({"command": argv[0], "args": argv[1:]})
            if missing is None and Path(argv[0]).name in ("uvx", "uv"):
                # A release named some other way (`uv tool run`, `--from`): uv's error names it.
                missing = unpublished_in(said)
            if proc.returncode != 0 and missing and missing != PINNED and _NOT_PUBLISHED.search(said):
                # A plugin tagged with a release not on the registry yet: every index (and
                # every worker's server) would fail until it is. The pinned one runs instead.
                self._remember_unpublished(missing)
                again = self._index_argv(repo_dir)
                if again is not None and again != argv:
                    fell_back = (f"livespec {missing} is not published, so the pinned {self.pin()} ran instead "
                                 "(and will, until the plugin names a published release)")
                    try:
                        proc = self.runner(again, capture_output=True, text=True, timeout=INDEX_TIMEOUT_S,
                                           check=False, env={**self.env, "LOCKS_HOOKS_OFF": "1"})
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        return f"{fell_back}; livespec index did not run: {exc}"
        if proc.returncode != 0:
            error = (proc.stderr or proc.stdout or "").strip()[:200]
            self._remember_failure(repo_dir, error)
            return (f"{fell_back}; " if fell_back else "") + f"livespec index failed: {error}"
        self._remember_failure(repo_dir, None)
        return f"livespec index refreshed ({fell_back})" if fell_back else "livespec index refreshed"

    def start_refresh(self, repo_dir: Path, log: Path) -> Any | None:
        """Start an index refresh and hand back its process at once, or None when none
        started (livespec cannot run here, another refresh holds this index, or the process
        could not be made). It runs as `locks index-livespec`, so it holds the same lock as
        every other refresh; none starts while one runs."""
        if self._index_argv(repo_dir) is None or self.indexing(repo_dir):
            return None
        from locks import dispatch

        top = repo.toplevel(repo_dir)
        if top is None:
            return None
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as out:
                return self.popen([sys.executable, "-m", "locks", "index-livespec", str(top.resolve())],
                                  stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                  env={**dispatch.child_env(self.env), "LOCKS_HOOKS_OFF": "1"},
                                  start_new_session=True) or _STARTED
        except OSError:
            return None

    def refresh_in_background(self, repo_dir: Path, log: Path) -> bool:
        """`start_refresh` for a hook, which must not wait minutes on a large repository."""
        return self.start_refresh(repo_dir, log) is not None

    def ensure_fresh(self, repo_dir: Path, wait_s: float, *, say: Callable[[str], None] | None = None,
                     pause: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic) -> list[Indexed]:
        """Index, or refresh, every repository `roots` finds that has no index or a stale
        one, and wait for them up to `wait_s` seconds in all: the step that comes before the
        worker, so it orients by specs and symbols instead of opening files. Incremental:
        livespec re-reads only what changed. Past `wait_s` a repository is `late`: its
        refresh is left running, never killed, and the task starts without it (on the stale
        index a repository already has, said so). A repository whose last index failed at this
        commit is not tried again until the checkout moves."""
        began = clock()
        found: list[Indexed] = []
        running: list[tuple[Path, Any, str]] = []
        for root in self.roots(repo_dir):
            status = self.inspect(root)
            # A stale index is still read by the task; only a missing or unreadable one is done without.
            meanwhile = "this task uses the index as it is" if status.present else "this task runs without it"
            if status.present and not status.stale:
                found.append(Indexed(root, FRESH, status.detail))
            elif failed := self.last_failure(root):
                found.append(Indexed(root, FAILED, f"its last index here failed ({failed}); not tried again "
                                                   f"until the checkout moves; {meanwhile}"))
            elif self.server() is None:
                found.append(Indexed(root, FAILED, "cannot index here: no livespec, and no `uvx` to run the "
                                                   f"pinned release; {meanwhile}"))
            else:
                started = self.start_refresh(root, self._index_log())
                if started is not None and say is not None:
                    say(f"locks: livespec is indexing {root.name} before the worker starts "
                        f"(waits up to {int(wait_s)} s)")
                running.append((root, started, meanwhile))
        wait = _Wait(began, began + wait_s, wait_s, pause, clock)
        for root, started, meanwhile in running:
            found.append(self._settled(root, started, meanwhile, wait))
        return found

    def _index_log(self) -> Path:
        return home(self.env) / f"{self.name}-index.log"

    def _settled(self, root: Path, started: Any, meanwhile: str, wait: _Wait) -> Indexed:
        """One repository `ensure_fresh` waited on: indexed, failed, or still indexing at the deadline.
        `started` is the refresh it started for it, None when another held the index's lock then.
        A lock another refresh held may have been for another repository of a shared index
        (`group_db`), which leaves this one as it was: once it is let go, this one's own starts."""
        while True:
            if not self._waited(root, started, wait):
                return Indexed(root, LATE, f"still indexing after the {int(wait.wait_s)} s locks waits; "
                                           f"{meanwhile}; the index keeps building and the next task gets it")
            status = self.inspect(root)
            if status.present and not status.stale:
                return Indexed(root, BUILT, f"indexed before the worker started "
                                            f"(took {int(wait.clock() - wait.began)} s)")
            if started is not None or self.last_failure(root) or wait.clock() >= wait.deadline:
                break
            started = self.start_refresh(root, self._index_log())
            if started is None and not self.indexing(root):
                break
        failed = self.last_failure(root) or "it left no usable index"
        return Indexed(root, FAILED, f"indexing failed ({failed}); {meanwhile}")

    def _waited(self, root: Path, started: Any, wait: _Wait) -> bool:
        """Whether the refresh `started` (or, with none, the one holding the index's lock) ended
        before the deadline. One that did not is left running."""
        if started is not None and hasattr(started, "wait"):
            try:
                started.wait(timeout=max(wait.deadline - wait.clock(), 0))
            except subprocess.TimeoutExpired:
                return False
            return True
        # Another process holds this index, or a start whose `Popen` handed nothing back: look at its lock.
        while self.indexing(root):
            if wait.clock() >= wait.deadline:
                return False
            wait.pause(POLL_S)
        return True

    # --- the specs' structure ---------------------------------------------

    def call_tools(self, workspace: Path, calls: Sequence[tuple[str, Mapping[str, Any]]], *,
                   env: Mapping[str, str] | None = None, timeout_s: float = 60.0) -> list[dict[str, Any]]:
        """Tools livespec has no command line for (`validate_openspec`), asked of its own server as
        any MCP client does: one session over stdio, run in `workspace` with each call's `workspace`
        it, cut at `timeout_s`. Raises LivespecError; the answers are the tools' own JSON objects."""
        server = self.server()
        if server is None:
            raise LivespecError("no livespec to ask")
        argv = [str(server["command"]), *[str(a) for a in server.get("args", [])]]
        env = {**(self.env if env is None else env),
               **{str(k): str(v) for k, v in (server.get("env") or {}).items()}, "LOCKS_HOOKS_OFF": "1"}
        try:
            proc = self.popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, env=env, cwd=str(workspace))
        except OSError as exc:
            raise LivespecError(f"livespec did not start: {exc}") from exc
        cut = threading.Timer(timeout_s, proc.kill)
        cut.start()
        try:
            _ask(proc, 1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                         "clientInfo": {"name": "locks", "version": "0"}})
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()
            return [_tool_data(name, _ask(proc, n, "tools/call", {"name": name, "arguments": {
                "workspace": str(workspace), **arguments}})) for n, (name, arguments) in enumerate(calls, start=2)]
        except (OSError, ValueError) as exc:
            raise LivespecError(f"livespec did not answer: {exc}") from exc
        finally:
            cut.cancel()
            proc.kill()
            proc.wait()

    def list_tools(self, workspace: Path, *, timeout_s: float = 60.0) -> set[str]:
        """The tools the livespec locks runs here serves (`tools/list`), asked as any MCP client does.
        Raises LivespecError."""
        server = self.server()
        if server is None:
            raise LivespecError("no livespec to ask")
        argv = [str(server["command"]), *[str(a) for a in server.get("args", [])]]
        env = {**self.env, **{str(k): str(v) for k, v in (server.get("env") or {}).items()}, "LOCKS_HOOKS_OFF": "1"}
        try:
            proc = self.popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, env=env, cwd=str(workspace))
        except OSError as exc:
            raise LivespecError(f"livespec did not start: {exc}") from exc
        cut = threading.Timer(timeout_s, proc.kill)
        cut.start()
        try:
            _ask(proc, 1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                         "clientInfo": {"name": "locks", "version": "0"}})
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()
            listed = _ask(proc, 2, "tools/list", {})
        except (OSError, ValueError) as exc:
            raise LivespecError(f"livespec did not answer: {exc}") from exc
        finally:
            cut.cancel()
            proc.kill()
            proc.wait()
        tools = listed.get("tools") if isinstance(listed, dict) else None
        if not isinstance(tools, list):
            raise LivespecError("tools/list: no tools in its answer")
        return {str(t["name"]) for t in tools if isinstance(t, dict) and t.get("name")}

    def _tools_path(self) -> Path:
        return home(self.env) / "work" / "livespec-tools.json"

    def missing_tools(self, version: str | None, workspace: Path) -> frozenset[str] | None:
        """The tools locks names (`TOOLS`) that a livespec release newer than the one this adapter was
        checked against (`PINNED`) does not serve: read from its own `tools/list` once per release and
        kept in the locks home. Empty for the checked release or an older one (`MINIMUM_VERSION` is
        their floor); None when a newer one's tools could not be read, which briefs as for `PINNED`."""
        found, checked = _version_tuple(version), _version_tuple(PINNED)
        if found is None or checked is None or found <= checked:
            return frozenset()
        try:
            kept = json.loads(self._tools_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            kept = {}
        if not isinstance(kept, dict):
            kept = {}
        if isinstance(kept.get(version), list):
            names = {str(n) for n in kept[version]}
        else:
            try:
                names = self.list_tools(workspace)
            except LivespecError:
                return None
            kept[str(version)] = sorted(names)
            with contextlib.suppress(OSError):
                self._tools_path().parent.mkdir(parents=True, exist_ok=True)
                self._tools_path().write_text(json.dumps(kept, indent=1), encoding="utf-8")
        return frozenset(TOOLS - names)

    def _missing_for(self, status: Status) -> frozenset[str] | None:
        root = status.data.get("root") if isinstance(status.data, Mapping) else None
        return self.missing_tools(status.version, Path(root)) if root else frozenset()

    def validate_openspec(self, repo_dir: Path, *, strict: bool = False, timeout_s: float = 120.0) -> dict[str, Any]:
        """The structural check of the repository's OpenSpec specs, by livespec's own
        `validate_openspec`, run on a temporary copy of `openspec/` and nothing else: that tool
        checks an index, and syncing the specs into the person's (or building one) would write in
        their checkout. The copy gets its own index (livespec's CLI), its own `LIVESPEC_HOME` (so
        livespec's registry of projects never lists it) and no `.livespec.toml` (whose `group_db`
        would send the sync to a shared index), and is removed afterwards. Raises LivespecError."""
        root = (repo.toplevel(repo_dir) or repo_dir).resolve()
        specs = root / "openspec"
        if not specs.is_dir():
            raise LivespecError("there is no openspec/ here")
        server = self.server()
        if server is None:
            raise LivespecError("no livespec to ask")
        with tempfile.TemporaryDirectory(prefix="locks-openspec-") as tmp:
            where = Path(tmp).resolve() / "repo"
            try:
                shutil.copytree(specs, where / "openspec", symlinks=True)
            except OSError as exc:
                raise LivespecError(f"openspec/ could not be copied: {exc}") from exc
            env = {**self.env, "LIVESPEC_HOME": str(Path(tmp).resolve() / "livespec-home"), "LOCKS_HOOKS_OFF": "1",
                   **{str(k): str(v) for k, v in (server.get("env") or {}).items()}}
            argv = [str(server["command"]), *[str(a) for a in server.get("args", [])], "index", str(where)]
            try:
                proc = self.runner(argv, capture_output=True, text=True, timeout=timeout_s, check=False, env=env)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise LivespecError(f"livespec index did not run: {exc}") from exc
            if proc.returncode != 0:
                said = (proc.stderr or proc.stdout or "").strip()
                raise LivespecError(clip(f"livespec index failed: {said}", 300))
            _, result = self.call_tools(where, [("sync_openspec", {}), ("validate_openspec", {"strict": strict})],
                                        env=env, timeout_s=timeout_s)
        return result

    # --- before the first attempt ------------------------------------------

    def brief(self, text: str, repo_dir: Path, status: Status) -> Briefing:
        if not status.present:
            return Briefing()
        try:
            with _connect(Path(status.data["db"])) as conn:
                symbols, named = _matching_symbols(conn, int(status.data["project"]), text)
                entries = [_describe(conn, sid) for sid in symbols]
        except sqlite3.Error as exc:
            return Briefing(reasons=(f"livespec index unreadable while briefing: {exc}",))
        reached = len({*symbols, *(c for e in entries for c in e["caller_ids"])})
        release = self._release_note(status)
        if not entries:
            return Briefing(reasons=release, reached=reached)
        raise_rungs, critical, reasons = 0, False, []
        for sid, e in zip(symbols, entries, strict=True):
            if sid not in named:
                # A full-text match is the map's, not the work's: a word the request shares with a
                # widely used symbol it never names raises no rung (a 27-caller symbol matched on
                # "state" started a test task a rung up).
                continue
            if any(s["priority"] in _CRITICAL_PRIORITIES for s in e["specs"]):
                critical = True
                reasons.append(f"livespec: {e['qname']} implements a critical spec")
            elif e["callers"] >= WIDELY_USED:
                reasons.append(f"livespec: {e['qname']} has {e['callers']} callers")
        if reasons:
            raise_rungs = 1
        # What this release lacks is said after the rule above: it raises no rung. The map itself is
        # the dossier's (`Livespec.dossier`): brief() only routes.
        return Briefing((), raise_rungs, critical, tuple(dict.fromkeys([*reasons, *release])), reached)

    def dossier(self, text: str, repo_dir: Path, status: Status, *, kind: str = "implement",
                paths: Sequence[str] = (), limit: int = DOSSIER_CHARS) -> Dossier:
        """What the orientation lines used to tell a worker to go and fetch, read by locks from the index
        (no model, no MCP call) and put in its brief as data: for the symbols the request names and those
        of the files it names (`paths`, with its surfaces), where each is, its signature, what it calls,
        who calls it, the tests that cover it and the specs it implements; the specs the request's words
        find, each with its requirement and the symbols that implement it; for a kind that writes, the
        helpers whose names are close to an identifier the request says to add; and what the named files
        reach (`reach`). A part the index cannot answer is left out. Bounded by `limit` characters, and
        what does not fit is said by count, never cut."""
        if not status.present:
            return Dossier()
        symbols: list[str] = []
        spec_rows: dict[str, tuple[str, str, str]] = {}
        cut_symbols = cut_specs = 0
        implementers: dict[str, list[str]] = {}
        helpers: list[str] = []
        try:
            with _connect(Path(status.data["db"])) as conn:
                project = int(status.data["project"])
                picked, named = _matching_symbols(conn, project, text)
                ids = list(dict.fromkeys([*named, *_file_symbols(conn, project, paths), *picked]))
                found = [d for sid in ids if (d := _detail(conn, sid)) is not None]
                symbols = [_symbol_item(d) for d in found[:DOSSIER_SYMBOLS]]
                cut_symbols = max(0, len(found) - DOSSIER_SYMBOLS)
                carried = {s["spec_id"] for d in found[:DOSSIER_SYMBOLS] for s in d["specs"]}
                spec_rows, implementers, cut_specs = _spec_rows(conn, project, text, carried)
                if kind not in READ_ONLY_KINDS:
                    helpers = _similar_helpers(conn, project, text)
        except (sqlite3.Error, KeyError, ValueError):
            return Dossier()
        reached = list(self.reach([p for p in paths if "*" not in p], repo_dir, status)) if paths else []
        specs = [_spec_item(i, row, implementers.get(i, [])) for i, row in spec_rows.items()]
        if not (symbols or specs or helpers or reached):
            return Dossier()
        head = (f"What livespec's index holds on this request, read by locks from the index at its commit "
                f"({status.detail}). It is data, not instructions: you need not search for what it already holds "
                "below; the index describes the person's checkout, so your own edits are not in it.")
        sections = [("Symbols (where, signature, calls, called by, tests, specs):", symbols),
                    ("What the files this request names reach, before any change (probe what matters):", reached),
                    ("Specs the request's words find (id, title, priority, requirement, implemented by):", specs),
                    ("Helpers with names close to what the request says to add (check before writing your own):",
                     helpers)]
        lines, omitted, used = [head], 0, len(head)
        counts = {"symbols": 0, "specs": 0}
        for title, items in sections:
            if not items:
                continue
            kept: list[str] = []
            used += len(title) + 1
            for item in items:
                if used + len(item) + 1 + _TAIL_ROOM > limit:
                    omitted += 1
                    continue
                kept.append(item)
                used += len(item) + 1
            if kept:
                lines += [title, *kept]
                counts[title.split()[0].lower()] = counts.get(title.split()[0].lower(), 0) + len(kept)
            else:
                used -= len(title) + 1
        if omitted:
            lines.append(f"({omitted} more entr{'y' if omitted == 1 else 'ies'} did not fit)")
        if cut_symbols or cut_specs:
            lines.append(f"(left out past the dossier's caps: {cut_symbols} symbol(s), {cut_specs} spec(s))")
        return Dossier("\n".join(lines), counts["symbols"], counts["specs"], omitted + cut_symbols + cut_specs)

    def _release_note(self, status: Status) -> tuple[str, ...]:
        """The plan's line when the livespec here is newer than the one this adapter was checked against
        and does not serve every tool locks names, or its tools could not be read; nothing otherwise."""
        missing = self._missing_for(status)
        if missing is None:
            return (f"livespec: v{status.version} is newer than the {PINNED} locks was checked against and its "
                    f"tools could not be read; briefed as for {PINNED}",)
        if missing:
            return (f"livespec: v{status.version} does not serve {', '.join(sorted(missing))}; they are left out "
                    "of the brief",)
        return ()

    def symbols_named(self, text: str, status: Status) -> list[tuple[str, str]]:
        """The symbols a text names outright, as (qualified name, path): what a
        note about them is anchored to. Read-only, like everything here."""
        if not status.present:
            return []
        named = sorted({t for t in _IDENT.findall(text) if len(t) >= 4 and _looks_like_code(t)})
        found: list[tuple[str, str]] = []
        try:
            with _connect(Path(status.data["db"])) as conn:
                for token in named:
                    rows = conn.execute(
                        "SELECT s.qualified_name, f.path FROM symbol s JOIN file f ON f.id = s.file_id "
                        "WHERE f.project_id = ? AND (s.qualified_name = ? OR s.qualified_name LIKE ? OR s.name = ?) "
                        "LIMIT 3", (int(status.data["project"]), token, f"%.{token}", token.rsplit(".", 1)[-1]),
                    ).fetchall()
                    found += [(r["qualified_name"], r["path"]) for r in rows if not is_test_path(r["path"])]
        except sqlite3.Error:
            return []
        return list(dict.fromkeys(found))[:6]

    # --- after a pass ------------------------------------------------------

    def reach(self, paths: Sequence[str], repo_dir: Path, status: Status) -> tuple[str, ...]:
        """What the index says the files a task names reach, before any change: the specs they
        implement, the files outside them that call into them, the tests that exercise them. Asked
        by locks for the worker (data in its brief), so it starts knowing, whether or not it calls
        livespec itself. Nothing when the index holds none of the files."""
        found = self._reach(set(paths), status)
        if isinstance(found, str):
            return ()
        specs, callers = found
        lines = []
        if specs:
            lines.append("livespec: these files implement specs " + ", ".join(
                f"{s[0]} {s[1]} [{s[2]}]" for s in specs[:8]))
        outside = [p for (p,) in callers if p not in set(paths)]
        code = [p for p in outside if not is_test_path(p)]
        tests = [p for p in outside if is_test_path(p)]
        if code:
            lines.append(f"livespec: {len(code)} file(s) outside them call into them: "
                         + ", ".join(code[:6]) + (" …" if len(code) > 6 else ""))
        if tests:
            lines.append("livespec: tests that exercise them: " + ", ".join(tests[:6]))
        return tuple(lines)

    def _reach(self, files: set[str], status: Status) -> tuple[list, list] | str:
        """The specs the symbols of `files` implement and the files that call those symbols, or why
        the index could not say; ([], []) when it holds none of them."""
        if not status.present or not files:
            return [], []
        try:
            with _connect(Path(status.data["db"])) as conn:
                project = int(status.data["project"])
                marks = ", ".join("?" for _ in files)
                ids = [r[0] for r in conn.execute(
                    f"SELECT s.id FROM symbol s JOIN file f ON f.id = s.file_id "  # noqa: S608 - placeholders only
                    f"WHERE f.project_id = ? AND f.path IN ({marks})", [project, *files])]
                if not ids:
                    return [], []
                id_marks = ", ".join("?" for _ in ids)
                specs = conn.execute(
                    f"SELECT DISTINCT sp.spec_id, sp.title, sp.priority FROM spec_symbol ss "  # noqa: S608
                    f"JOIN spec sp ON sp.id = ss.spec_id WHERE ss.symbol_id IN ({id_marks}) "
                    f"ORDER BY sp.spec_id", ids).fetchall()
                callers = conn.execute(
                    f"SELECT DISTINCT f.path FROM symbol_edge e JOIN symbol s ON s.id = e.src_symbol_id "  # noqa: S608
                    f"JOIN file f ON f.id = s.file_id WHERE e.edge_type = 'calls' "
                    f"AND e.dst_symbol_id IN ({id_marks}) ORDER BY f.path", ids).fetchall()
        except (sqlite3.Error, KeyError, ValueError) as exc:
            return str(exc)
        return specs, callers

    def assess(self, changed: Sequence[str], repo_dir: Path, status: Status) -> tuple[str, ...]:
        if not status.present or not changed:
            return ()
        changed_set = set(changed)
        found = self._reach(changed_set, status)
        if isinstance(found, str):
            return (f"livespec: could not assess the change ({found})",)
        specs, callers = found
        lines = []
        if specs:
            lines.append("livespec: the change touches specs " + ", ".join(
                f"{s[0]} {s[1]} [{s[2]}]" for s in specs[:8]))
        outside = [p for (p,) in callers if p not in changed_set]
        code = [p for p in outside if not is_test_path(p)]
        tests = [p for p in outside if is_test_path(p)]
        if code:
            lines.append(f"livespec: {len(code)} file(s) outside the change call what it changed: "
                         + ", ".join(code[:6]) + (" …" if len(code) > 6 else ""))
        if tests:
            lines.append("livespec: tests that exercise the changed code: " + ", ".join(tests[:6]))
        return tuple(lines)

    def unlinked_specs(self, status: Status) -> list[tuple[str, str]]:
        """The specs the index holds that no symbol implements, as (spec id, title): what `@spec:` comments
        would link. Read-only; empty when the index cannot be read."""
        if not status.present:
            return []
        try:
            with _connect(Path(status.data["db"])) as conn:
                rows = conn.execute(
                    "SELECT sp.spec_id, sp.title FROM spec sp WHERE sp.project_id = ? AND sp.status != 'deprecated' "
                    "AND NOT EXISTS (SELECT 1 FROM spec_symbol ss WHERE ss.spec_id = sp.id "
                    "AND ss.relation = 'implements') ORDER BY sp.spec_id", (int(status.data["project"]),)).fetchall()
        except sqlite3.Error:
            return []
        return [(r["spec_id"], r["title"]) for r in rows]

    def spec_links_brief(self, repo_dir: Path) -> list[str]:
        """What a worker that writes or changes an OpenSpec spec is told so the spec reaches its code:
        a `@spec:<id>` comment on each function that implements it, and the index of the directory it
        works in brought up to date (`sync_openspec` reads the spec and links the comments, `index_project`
        indexes the functions), so `get_spec_implementation` lists them. A shared index (`group_db`) is
        not touched from a worktree: that would add a project for a directory that is removed."""
        root = (repo.toplevel(repo_dir) or repo_dir).resolve()
        shared = self.db_path(root) != root / ".mcp-docs" / "docs.db"
        lines = [f"A spec you write or change is linked to the code that implements it: put `@spec:{SPEC_ID}` in a "
                 "comment on each function that implements it (the capability folder under `openspec/specs/`, a "
                 "hyphen and the requirement's name in lower case with hyphens and accents folded: validación "
                 "becomes validacion; a requirement with a `<!-- livespec:id=… -->` marker under its heading has "
                 "that id; `list_specs` shows the ids of the specs already indexed). "
                 "locks' check reads those comments: a spec you wrote with none is a finding that blocks, and "
                 "one that already existed and has none is a warning."]
        if shared:
            lines.append("This repository's index is shared with others (`group_db`): do not index the directory "
                         "you work in; the links are made when it is indexed after your work lands.")
        else:
            lines.append("Then bring the index of the directory you work in up to date: `sync_openspec` with that "
                         "workspace reads the specs you wrote and links the comments, and `index_project` (it is "
                         "incremental) indexes the functions they sit on. Check with `get_spec_implementation` that "
                         "each spec under `openspec/specs/` lists its code; a requirement you only proposed under "
                         "`openspec/changes/` links once it is applied, so it needs the comment and no check.")
        return lines

    # --- for the worker ------------------------------------------------------

    def hint(self, kind: str, repo_dir: Path, workdir: Path) -> str:
        root = (repo.toplevel(repo_dir) or repo_dir).resolve()
        from locks import manifests

        own = manifests.read("livespec", self.env)
        what = (own.hint_for(kind) if own else "") or _HINTS.get(kind, _HINTS["implement"])
        status = self.inspect(root)
        if lacking := sorted((self._missing_for(status) or frozenset()) & set(_named_tools(what))):
            what += f" (not served by livespec v{status.version}: {', '.join(lacking)}; do not call them)"
        where = f'pass workspace="{root}" on every call'
        if workdir.resolve() != root:
            where += (f"; the index describes the base commit there, not your edits in {workdir}: read what you "
                      "changed from your files")
        return f"{what}; {where}."


def _named_tools(text: str) -> list[str]:
    """The livespec tools a line of guidance names."""
    return [w for w in re.findall(r"[a-z][a-z_]+", text) if w in TOOLS]


# --- queries ------------------------------------------------------------------


@contextlib.contextmanager
def _lock(path: Path, *, wait_s: float, pause: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic) -> Iterator[tuple[bool, bool]]:
    """Yields (held, waited): an exclusive lock on `path`, waiting up to `wait_s`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline, waited = clock() + wait_s, False
    with open(path, "a") as fh:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if clock() >= deadline:
                    yield False, waited
                    return
                waited = True
                pause(1.0)
        try:
            yield True, waited
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _ask(proc: Any, ident: int, method: str, params: Mapping[str, Any]) -> Any:
    """One JSON-RPC request to an MCP server on stdio (a message a line) and its answer."""
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            raise LivespecError(f"livespec closed before it answered {method}")
        try:
            message = json.loads(line)
        except ValueError:
            continue  # a log line on stdout, not a message
        if isinstance(message, dict) and message.get("id") == ident:
            if isinstance(message.get("error"), dict):
                raise LivespecError(f"{method}: {message['error'].get('message') or message['error']}")
            return message.get("result")


def _tool_data(name: str, result: Any) -> dict[str, Any]:
    """The JSON object a tool answered with (its `structuredContent`; older releases only wrap the
    same object in a text item), or the error it said."""
    if not isinstance(result, dict):
        raise LivespecError(f"{name}: no answer")
    content = result.get("content")
    text = " ".join(str(c.get("text", "")) for c in content or [] if isinstance(c, dict))
    data = result.get("structuredContent")
    if not isinstance(data, dict):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if result.get("isError") or not isinstance(data, dict) or data.get("error"):
        fields = data if isinstance(data, dict) else {}
        said = fields.get("error") or text or "an answer that is no object"
        hint = f" ({fields['hint']})" if fields.get("hint") else ""
        raise LivespecError(clip(f"{name}: {said}{hint}", 300))
    return data


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _head(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True,
                             timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def _older_than_head(indexed_at: str | None, root: Path) -> bool:
    if not indexed_at:
        return True
    try:
        stamp = datetime.fromisoformat(indexed_at.replace(" ", "T")).replace(tzinfo=UTC)
        out = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=str(root), capture_output=True,
                             text=True, timeout=10, check=False).stdout.strip()
        return bool(out) and datetime.fromtimestamp(int(out), UTC) > stamp
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return False


def _matching_symbols(conn: sqlite3.Connection, project: int, text: str) -> tuple[list[int], list[int]]:
    """Symbols the request names outright, then the best full-text matches —
    production code first; tests reach the map through the symbols they call. (every symbol
    picked, the ones the request named): only a named one says what the work touches."""
    picked: list[int] = []
    named = {t for t in _IDENT.findall(text) if len(t) >= 4 and _looks_like_code(t)}
    for token in sorted(named):
        rows = conn.execute(
            "SELECT s.id, f.path FROM symbol s JOIN file f ON f.id = s.file_id WHERE f.project_id = ? "
            "AND (s.name = ? OR s.qualified_name = ? OR s.qualified_name LIKE ?) LIMIT 5",
            (project, token.rsplit(".", 1)[-1], token, f"%.{token}"),
        ).fetchall()
        picked += [r["id"] for r in rows if not is_test_path(r["path"]) and r["id"] not in picked]
    named_ids = list(picked)
    terms = [w.replace('"', "") for w in words(text)][:12]
    if terms and len(picked) < MAP_SYMBOLS:
        rows = conn.execute(
            "SELECT c.source_id AS id, c.file_path AS path FROM chunk_fts JOIN chunk c ON c.id = chunk_fts.rowid "
            "WHERE chunk_fts MATCH ? AND c.project_id = ? AND c.source_type = 'symbol' "
            "ORDER BY bm25(chunk_fts) LIMIT 60",
            (" OR ".join(f'"{t}"' for t in terms), project),
        ).fetchall()
        for r in rows:
            if r["id"] is not None and not is_test_path(r["path"] or "") and r["id"] not in picked:
                picked.append(int(r["id"]))
    return picked[:MAP_SYMBOLS], named_ids


# The indexer resolves a call on an object of unknown type (`rule.apply_line(...)`, `super().apply_line(...)`)
# by bare name to every method of that name, one `calls` edge each at weight FANOUT_WEIGHT or less. The call
# site is one caller of each of them, but the same-named methods that fan out to one another are not callers of
# each other: those edges are no one's call, and counting them made 21 methods "have 21 callers" for one call.
FANOUT_WEIGHT = 0.6


def _callers(conn: sqlite3.Connection, symbol_id: int) -> list[sqlite3.Row]:
    """The distinct symbols that call this one, once each: the one reading of "callers" that `brief`, `_describe`,
    `reached` and the dossier share. An edge from a same-named symbol that the index could only resolve by name
    (weight at most FANOUT_WEIGHT) is a sibling's fan-out, not a call. Tests are in it, marked by their path."""
    return conn.execute(
        "SELECT s.id AS id, s.qualified_name AS name, f.path AS path, s.start_line AS line FROM symbol_edge e "
        "JOIN symbol s ON s.id = e.src_symbol_id JOIN file f ON f.id = s.file_id "
        "JOIN symbol d ON d.id = e.dst_symbol_id "
        "WHERE e.dst_symbol_id = ? AND e.edge_type = 'calls' "
        "AND NOT (s.name = d.name AND s.id != d.id AND e.weight <= ?) "
        "GROUP BY s.id ORDER BY f.path, s.start_line, s.id", (symbol_id, FANOUT_WEIGHT)).fetchall()


def _describe(conn: sqlite3.Connection, symbol_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT s.qualified_name, s.kind, s.start_line, f.path FROM symbol s JOIN file f ON f.id = s.file_id "
        "WHERE s.id = ?", (symbol_id,)).fetchone()
    callers = _callers(conn, symbol_id)
    specs = conn.execute(
        "SELECT sp.spec_id, sp.title, sp.priority FROM spec_symbol ss JOIN spec sp ON sp.id = ss.spec_id "
        "WHERE ss.symbol_id = ? AND ss.relation = 'implements' ORDER BY sp.spec_id", (symbol_id,)).fetchall()
    tests = sorted({r["path"] for r in callers if is_test_path(r["path"])})
    return {
        "qname": row["qualified_name"], "kind": row["kind"], "line": row["start_line"], "path": row["path"],
        "callers": len(callers),
        "caller_ids": sorted(r["id"] for r in callers),
        "specs": [dict(s) for s in specs],
        "tests": tests,
    }


def _detail(conn: sqlite3.Connection, symbol_id: int) -> dict[str, Any] | None:
    """One symbol as the dossier tells it: where, its signature (else the first line of its chunk), what
    it calls and who calls it as `path:line`, the tests among the callers, the specs it implements."""
    row = conn.execute(
        "SELECT s.qualified_name, s.kind, s.signature, s.start_line, s.end_line, f.path FROM symbol s "
        "JOIN file f ON f.id = s.file_id WHERE s.id = ?", (symbol_id,)).fetchone()
    if row is None:
        return None
    first = row["signature"]
    if not first:
        chunk = conn.execute("SELECT text FROM chunk WHERE source_type = 'symbol' AND source_id = ? LIMIT 1",
                             (symbol_id,)).fetchone()
        first = next((ln.strip() for ln in (chunk["text"] if chunk else "").splitlines() if ln.strip()), "")
    near = "SELECT DISTINCT o.qualified_name AS name, f.path AS path, o.start_line AS line FROM symbol_edge e " \
           "JOIN symbol o ON o.id = e.{other}_symbol_id JOIN file f ON f.id = o.file_id " \
           "WHERE e.{mine}_symbol_id = ? AND e.edge_type = 'calls' ORDER BY f.path, o.start_line"
    calls = conn.execute(near.format(other="dst", mine="src"), (symbol_id,)).fetchall()
    callers = _callers(conn, symbol_id)
    specs = conn.execute(
        "SELECT sp.spec_id, sp.title, sp.priority FROM spec_symbol ss JOIN spec sp ON sp.id = ss.spec_id "
        "WHERE ss.symbol_id = ? AND ss.relation = 'implements' ORDER BY sp.spec_id", (symbol_id,)).fetchall()
    return {"qname": row["qualified_name"], "kind": row["kind"], "first": first, "path": row["path"],
            "start": row["start_line"], "end": row["end_line"], "calls": [dict(r) for r in calls],
            "callers": [dict(r) for r in callers if not is_test_path(r["path"])],
            "tests": sorted({r["path"] for r in callers if is_test_path(r["path"])}),
            "specs": [dict(r) for r in specs]}


def _places(rows: Sequence[Mapping[str, Any]]) -> str:
    """`name (path:line)` for up to `DOSSIER_LINKS` rows, the rest said by count."""
    shown = [f"{r['name'].rsplit('.', 1)[-1]} ({r['path']}:{r['line']})" for r in rows[:DOSSIER_LINKS]]
    more = len(rows) - DOSSIER_LINKS
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _symbol_item(d: Mapping[str, Any]) -> str:
    head = f"- `{d['qname']}` ({d['kind']}) {d['path']}:{d['start']}-{d['end']}"
    if d["first"]:
        head += f" — `{clip(d['first'], 160, one_line=True)}`"
    parts = []
    if d["calls"]:
        parts.append("calls " + _places(d["calls"]))
    if d["callers"]:
        parts.append(f"{len(d['callers'])} caller(s): " + _places(d["callers"]))
    if d["tests"]:
        parts.append("tests: " + ", ".join(d["tests"][:DOSSIER_LINKS]))
    if d["specs"]:
        parts.append("specs: " + ", ".join(f"{s['spec_id']} {s['title']} [{s['priority']}]" for s in d["specs"][:3]))
    return head + "".join(f"\n    {p}" for p in parts)


def _file_symbols(conn: sqlite3.Connection, project: int, paths: Sequence[str]) -> list[int]:
    """The most called symbols of each file the request names (or its surfaces), `FILE_SYMBOLS` a file."""
    found: list[int] = []
    for path in dict.fromkeys(p for p in paths if "*" not in p and not is_test_path(p)):
        rows = conn.execute(
            "SELECT s.id FROM symbol s JOIN file f ON f.id = s.file_id LEFT JOIN symbol_edge e "
            "ON e.dst_symbol_id = s.id AND e.edge_type = 'calls' WHERE f.project_id = ? AND f.path = ? "
            "GROUP BY s.id ORDER BY count(e.id) DESC, s.start_line LIMIT ?", (project, path, FILE_SYMBOLS)).fetchall()
        found += [r["id"] for r in rows]
    return found


def _spec_rows(conn: sqlite3.Connection, project: int, text: str,
               carried: set[str]) -> tuple[dict[str, tuple[str, str, str]], dict[str, list[str]], int]:
    """The specs a dossier carries, by spec id: those its symbols implement, then those the request's
    words find (at least `SPEC_MIN_TERMS` words in common with the title and text, a word most specs
    use counting for none; a spec id the request writes out counts too), and the symbols that implement
    each, and how many matching specs the cap `DOSSIER_SPECS` left out. The index may hold no specs: then
    nothing."""
    rows = conn.execute("SELECT id, spec_id, title, description, priority FROM spec WHERE project_id = ? "
                        "AND status != 'deprecated' ORDER BY spec_id LIMIT 2000", (project,)).fetchall()
    if not rows:
        return {}, {}, 0
    asked = set(words(text))
    held = [set(words(f"{r['title']} {r['description'] or ''}")) for r in rows]
    common = {w for w in asked if len(rows) >= 5 and sum(w in h for h in held) > 0.2 * len(rows)}
    scored = []
    for r, have in zip(rows, held, strict=True):
        shared = len((asked & have) - common)
        if r["spec_id"] in carried:
            scored.append((-1, r))
        elif shared >= SPEC_MIN_TERMS or re.search(rf"(?<![\w-]){re.escape(r['spec_id'])}(?![\w-])", text):
            scored.append((-max(shared, SPEC_MIN_TERMS), r))
    scored.sort(key=lambda pair: (pair[0], pair[1]["spec_id"]))
    chosen = [r for _, r in scored[:DOSSIER_SPECS]]
    out: dict[str, tuple[str, str, str]] = {}
    who: dict[str, list[str]] = {}
    for r in chosen:
        out[r["spec_id"]] = (r["title"], r["priority"], r["description"] or "")
        who[r["spec_id"]] = [x["qualified_name"] for x in conn.execute(
            "SELECT s.qualified_name FROM spec_symbol ss JOIN symbol s ON s.id = ss.symbol_id "
            "WHERE ss.spec_id = ? AND ss.relation = 'implements' ORDER BY s.qualified_name LIMIT ?",
            (r["id"], DOSSIER_LINKS + 1))]
    return out, who, max(0, len(scored) - DOSSIER_SPECS)


def _spec_item(spec_id: str, row: tuple[str, str, str], by: Sequence[str]) -> str:
    title, priority, text = row
    line = f"- {spec_id} {title} [{priority}]"
    if text.strip():
        line += f": {clip(text, SPEC_TEXT_CHARS, one_line=True)}"
    if by:
        line += "; implemented by " + ", ".join(f"`{q}`" for q in by[:DOSSIER_LINKS]) \
                + (" and more" if len(by) > DOSSIER_LINKS else "")
    else:
        line += "; no symbol implements it yet"
    return line


def _similar_helpers(conn: sqlite3.Connection, project: int, text: str) -> list[str]:
    """For each identifier the request says to add that the index does not hold, the production symbols
    whose names share at least two of its parts (one, when it has one): no embeddings, only names."""
    out: list[str] = []
    for token in _to_add(text):
        if len(token) < 4 or not _looks_like_code(token) or "." in token:
            continue
        parts = {p.lower() for p in _NAME_PART.findall(token) if len(p) >= 3}
        if not parts:
            continue
        held = conn.execute("SELECT 1 FROM symbol s JOIN file f ON f.id = s.file_id WHERE f.project_id = ? "
                            "AND s.name = ? LIMIT 1", (project, token)).fetchone()
        if held is not None:
            continue
        scored: dict[str, tuple[int, str]] = {}
        for part in sorted(parts)[:4]:
            for r in conn.execute(
                    "SELECT s.name, s.qualified_name, s.start_line, f.path FROM symbol s JOIN file f "
                    "ON f.id = s.file_id WHERE f.project_id = ? AND lower(s.name) LIKE ? LIMIT 40",
                    (project, f"%{part}%")):
                if is_test_path(r["path"]):
                    continue
                shared = {p.lower() for p in _NAME_PART.findall(r["name"])} & parts
                scored[r["qualified_name"]] = (len(shared), f"{r['path']}:{r['start_line']}")
        need = min(2, len(parts))
        close = sorted((q for q, (n, _) in scored.items() if n >= need), key=lambda q: (-scored[q][0], q))
        if close:
            out.append(f"- for `{token}`: " + ", ".join(f"`{q}` ({scored[q][1]})" for q in close[:SIMILAR_HELPERS]))
    return out
