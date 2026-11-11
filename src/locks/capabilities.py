"""Which capabilities a worker is handed, decided by the core, not remembered by the model.

The problem this exists for: in an ordinary Claude Code session, a plugin's
tools sit in a list of dozens and get called when the model happens to think of
them, which is rarely. A capability that is always offered and seldom used is
the same as one that is not installed.

So the core decides. Each capability declares when it belongs:

- ``"always"`` — transversal. A code index belongs in every worker that reads
  or writes code.
- a list of kinds — it belongs to that work: a layout inspector in UI tasks.
- ``after_failure`` — reactive. It joins the *next* attempt once one of these
  kinds has failed: the layout inspector after a UI attempt the checks rejected.
- ``touches`` — by what the work touches, whatever its kind: file patterns (or
  ``"ui"`` for rendered UI, `UI_FILES`) matched against the files a task names,
  the files its work changed so far, and the diff a review reads. A capability
  that only ``measures`` (reads, never changes anything) also goes to reading
  work this way: a review, a refuter, a re-check and the independent verifier
  of a change that touches its files. One that can change things joins only
  work that writes.

A worker gets exactly the selected servers (`--strict-mcp-config`), plus a
line per capability saying when to reach for it. Nothing else is in its list,
so nothing competes with it.

The registry lives in the user's locks home, never in a repository. A server
entry is a command that runs on this machine; a repository that could add one
by committing a file could run anything on the machine of whoever opens it.
"""
from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

#: What rendered UI is, for a capability that `touches` "ui": the files a page is drawn from.
UI_FILES = ("components/**", "islands/**", "routes/**", "pages/**", "app/**/*.tsx", "src/**/*.tsx",
            "*.css", "*.scss", "*.sass", "*.less", "*.tsx", "*.jsx", "*.vue", "*.svelte", "*.astro", "*.html")


#: Paths that match `UI_FILES` and render nothing a person sees: tests and their fixtures, examples,
#: documentation, and what a tool wrote (coverage reports, build output, dependencies). A validator's
#: `.tsx` fixture or a coverage run's `index.html` is no page to measure.
NOT_RENDERED = re.compile(
    r"(^|/)(tests?|__tests__|__fixtures__|__snapshots__|fixtures?|testdata|spec|e2e|examples?|docs?|"
    r"coverage|htmlcov|\.nyc_output|dist|build|out|\.next|\.nuxt|\.svelte-kit|node_modules|vendor)/"
    r"|\.(test|spec|stories)\.\w+$|(^|/)test_[^/]*$|_test\.\w+$")


def renders(path: str) -> bool:
    """Whether a changed path is rendered UI: it matches `UI_FILES` and is not a test, a fixture,
    an example, documentation or a tool's output (`NOT_RENDERED`)."""
    return matches(path, UI_FILES) and not NOT_RENDERED.search(path)


def matches(path: str, patterns: Sequence[str]) -> bool:
    """A path against file patterns, at any depth: `components/**` is a folder of that
    name anywhere, `*.css` a file of that type anywhere."""
    parts = path.split("/")
    for pattern in patterns:
        if pattern.endswith("/**"):
            if pattern[:-3] in ("/".join(parts[i:i + pattern[:-3].count("/") + 1])
                                for i in range(len(parts) - 1)):
                return True
        elif "/" in pattern:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, "*/" + pattern):
                return True
        elif fnmatch.fnmatch(parts[-1], pattern):
            return True
    return False


@dataclass(frozen=True)
class Capability:
    name: str
    server: Mapping[str, Any]
    when: str | tuple[str, ...] = "always"
    after_failure: tuple[str, ...] = ()
    hint: str = ""
    #: A folder the worker reads from when it has this capability: the skill of
    #: the plugin that ships it.
    read_dir: str = ""
    #: The files it is about: it joins work that names or changes one, whatever the kind.
    touches: tuple[str, ...] = ()
    #: It only measures, never changes anything: reading work may have it too.
    measures: bool = False
    #: It joins work whose own request asks to browse (a page, a site, a URL, a running app) or that
    #: was sent with a `--service`, whatever the kind (`locks.orchestrate.asks_to_browse`).
    on_request: bool = False
    #: Its tools that act on the world (send, post, reply): run behind the `outbound`
    #: guard, refused outside a loop the person set to act (`locks.loops`).
    outbound: tuple[str, ...] = ()
    #: The keys the person's registry entry set: what a neighbour of the same name may
    #: still fill in (`_inherit`).
    declared: frozenset[str] = field(default=frozenset(), compare=False)

    def touched(self, paths: Sequence[str]) -> str | None:
        """The first of `paths` this capability is about, or None."""
        return next((p for p in paths if self.touches and matches(p, self.touches)), None)

    def applies(self, kind: str, *, failed_before: bool, paths: Sequence[str] = (), reading: bool = False,
                asked: bool = False) -> bool:
        if self.when == "always":
            return True
        if self.on_request and asked:
            return True
        if isinstance(self.when, tuple) and kind in self.when:
            return True
        if failed_before and kind in self.after_failure:
            # One that is about some files comes after a failure only to work that touches them, or
            # whose files are not known: an API change is no page to measure (issue #123).
            return not self.touches or not paths or self.touched(paths) is not None
        # By what the work touches: reading work only gets one that measures.
        return (self.measures or not reading) and self.touched(paths) is not None


@dataclass(frozen=True)
class Selection:
    servers: Mapping[str, Any] = field(default_factory=dict)
    hints: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    read_dirs: tuple[str, ...] = ()

    def system_prompt(self) -> str:
        if not self.hints:
            return ""
        return "Capabilities this task was given, and when to use them:\n" + "\n".join(
            f"- {h}" for h in self.hints
        )


class RegistryError(ValueError):
    pass


def load(path: Path) -> dict[str, Capability]:
    """The registry at `path`; an absent file is an empty registry."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RegistryError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RegistryError(f"{path} must hold an object of capabilities")
    registry: dict[str, Capability] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("server"), dict):
            raise RegistryError(f"capability {name!r} needs a `server` object")
        when = entry.get("when", "always")
        if isinstance(when, list):
            when = tuple(str(k) for k in when)
        elif when != "always":
            raise RegistryError(f"capability {name!r}: `when` is \"always\" or a list of kinds")
        touches = entry.get("touches", [])
        if touches == "ui":
            touches = list(UI_FILES)
        elif not isinstance(touches, list):
            raise RegistryError(f"capability {name!r}: `touches` is \"ui\" or a list of file patterns")
        registry[name] = Capability(
            name=name,
            server=entry["server"],
            when=when,
            after_failure=tuple(str(k) for k in entry.get("after_failure", [])),
            hint=str(entry.get("hint", "")),
            touches=tuple(str(t) for t in touches),
            measures=bool(entry.get("measures", False)),
            on_request=bool(entry.get("on_request", False)),
            outbound=_outbound(name, entry),
            declared=frozenset(entry),
        )
        if registry[name].outbound:
            from locks import plugins

            guarded = plugins.guarded(entry["server"], "outbound")
            guarded["env"] = {**guarded["env"], "LOCKS_OUTBOUND_ACTS": ",".join(registry[name].outbound)}
            registry[name] = replace(registry[name], server=guarded)
    return registry


def _outbound(name: str, entry: Mapping[str, Any]) -> tuple[str, ...]:
    raw = entry.get("outbound")
    if raw is None:
        return ()
    acts = raw.get("acts") if isinstance(raw, dict) else None
    if not isinstance(acts, list) or not acts or not all(isinstance(a, str) and a.strip() for a in acts):
        raise RegistryError(f"capability {name!r}: `outbound` is {{\"acts\": [the tools that act]}}")
    if "command" not in entry["server"]:
        raise RegistryError(f"capability {name!r}: `outbound` guards a server run by a `command`")
    return tuple(a.strip() for a in acts)


def for_loop(selection: Selection, loop: str | None, locks_home: Path) -> Selection:
    """The servers behind the `outbound` guard told the loop the task is a round of, if
    any, and where its rules are kept: the guard reads them at each call."""
    servers = {}
    for name, server in selection.servers.items():
        args = list(server.get("args") or ())
        if args[:3] == ["-m", "locks.mcp_guard", "outbound"]:
            server = {**server, "env": {**dict(server.get("env") or {}), "LOCKS_HOME": str(locks_home),
                                        **({"LOCKS_LOOP": loop} if loop else {})}}
        servers[name] = server
    return replace(selection, servers=servers)


def load_all(path: Path, env: Mapping[str, str] | None = None) -> dict[str, Capability]:
    """The registry at `path` over the neighbours the person installed as Claude
    Code plugins (`locks.plugins`): a registry entry of the same name wins, and
    the `plugins` setting off leaves the installed ones out."""
    import os

    from locks import config, plugins

    env = os.environ if env is None else env
    found = plugins.capabilities(env) if config.enabled("plugins", env) else {}
    return {**found, **_inherit(load(path))}


def _neighbours() -> dict[str, Any]:
    from locks import plugins

    return {n.server: n for n in plugins.KNOWN}


def _inherit(registry: Mapping[str, Capability]) -> dict[str, Capability]:
    """An entry named after a neighbour locks knows that says nothing of what it touches
    or whether it measures takes the neighbour's answer: written before those keys
    existed, it would otherwise keep the neighbour from the work its files call for.
    Whatever the entry declares still wins."""
    known = _neighbours()
    out = dict(registry)
    for name, c in registry.items():
        n = known.get(name)
        if n is not None and not ({"touches", "measures"} & c.declared):
            out[name] = replace(c, touches=tuple(n.touches), measures=n.measures,
                                on_request=c.on_request or n.on_request)
    return out


#: Runners that fetch a package each time they start one.
_FETCHERS = frozenset({"npx", "bunx", "pnpx"})


def unpinned(server: Mapping[str, Any]) -> str | None:
    """The package a server's command fetches with no fixed version (`npx pkg`,
    `npx pkg@latest`), or None: what runs can change between two workers."""
    if Path(str(server.get("command") or "")).name not in _FETCHERS:
        return None
    args = [str(a) for a in server.get("args") or [] if isinstance(a, (str, int, float))]
    package = next((a for a in args if not a.startswith("-")), None)
    if package is None:
        return None
    version = (package[1:] if package.startswith("@") else package).partition("@")[2]
    return package if version in ("", "latest", "next") else None


def notices(path: Path) -> list[str]:
    """What the person's registry does that they may not mean, one line each: an entry
    that shadows a neighbour locks knows (and what it took from it), a server fetched
    with no fixed version. Said, never refused; a registry that cannot be read says
    nothing here (its error is raised where it is loaded)."""
    try:
        registry = load(path)
    except RegistryError:
        return []
    known, lines = _neighbours(), []
    for name, c in registry.items():
        n = known.get(name)
        if n is not None:
            took = [] if {"touches", "measures"} & c.declared else [
                word for word, has in (("what it touches", bool(n.touches)), ("that it only measures", n.measures))
                if has]
            lines.append(f"locks: your capabilities.json entry {name!r} shadows the {n.plugin} neighbour locks "
                         "knows and wins on every key it sets"
                         + (f"; it sets neither `touches` nor `measures`, so it takes {' and '.join(took)} "
                            "from the neighbour" if took else "")
                         + (f"; its `when` ({', '.join(c.when)}) still gives it to every task of those kinds, "
                            "whatever files they touch" if isinstance(c.when, tuple) else "")
                         + ". Remove the entry to use the neighbour as locks ships it.")
        package = unpinned(c.server)
        if package:
            lines.append(f"locks: capability {name!r} runs `{package}` with no fixed version, so each worker may "
                         "fetch another release; pin it (`name@1.2.3`) to keep what runs the same.")
    return lines


def select(
    registry: Mapping[str, Capability],
    kind: str,
    *,
    failed_before: bool = False,
    workdir: Path | None = None,
    every: bool = False,
    paths: Sequence[str] = (),
    reading: bool = False,
    asked: bool = False,
) -> Selection:
    """The capabilities a task of `kind` gets; `every` takes all of `registry`,
    for the ones a worker's role names whatever their `when`. `paths` are the files
    the work names or changed (a review's: the diff it reads); `reading` work gets
    by them only what measures. `asked`: the request asks to browse, or was sent with a service."""
    chosen = [c for c in registry.values()
              if every or c.applies(kind, failed_before=failed_before, paths=paths, reading=reading, asked=asked)]
    values = {"workdir": str(workdir) if workdir else ""}
    servers = {c.name: _fill(c.server, values) for c in chosen}
    hints = tuple(f"{c.name}: {_fill(c.hint, values)}" + _why_touched(c, kind, paths, asked) for c in chosen if c.hint)
    return Selection(servers, hints, tuple(c.name for c in chosen), tuple(c.read_dir for c in chosen if c.read_dir))


def _why_touched(c: Capability, kind: str, paths: Sequence[str], asked: bool = False) -> str:
    """Said when a capability came by what the work touches or asks, not by its kind."""
    if c.when == "always" or (isinstance(c.when, tuple) and kind in c.when):
        return ""
    if c.on_request and asked:
        return " (given because the request asks to browse)"
    hit = c.touched(paths)
    return f" (given because this work touches {hit})" if hit else ""


def measuring(registry: Mapping[str, Capability]) -> dict[str, Capability]:
    """The capabilities that only measure: the read-only independent check may have them."""
    return {name: c for name, c in registry.items() if c.measures}


def _fill(value: Any, values: Mapping[str, str]) -> Any:
    """`{workdir}` in a server's args, env or hint becomes the task's directory:
    a code index that takes the workspace as an argument gets the right one."""
    if isinstance(value, str):
        for key, replacement in values.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if isinstance(value, Mapping):
        return {k: _fill(v, values) for k, v in value.items()}
    if isinstance(value, Sequence):
        return [_fill(v, values) for v in value]
    return value


EXAMPLE = {
    "livespec": {
        "server": {"command": "livespec", "args": []},
        "when": "always",
        "hint": "before editing a symbol, ask who calls it (who_calls) and what breaks "
        "(analyze_impact); pass workspace=\"{workdir}\" on every call",
    },
    "layout-inspector": {
        "server": {"command": "layout-inspector", "args": []},
        "when": ["ui"],
        "after_failure": ["implement", "feature"],
        "touches": "ui",
        "measures": True,
        "hint": "measure the rendered page (detect_issues, compare_viewports) instead of "
        "judging it by eye",
    },
    "browser": {
        # Pinned: `@latest` is fetched again by each worker, and two may run different releases.
        "server": {"command": "npx", "args": ["@playwright/mcp@0.0.83"]},
        "when": [],
        "on_request": True,
        "hint": "open the running app with browser_navigate and read it with browser_snapshot; report what "
        "the page shows, never what it should show",
    },
}
