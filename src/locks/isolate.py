"""One git worktree per task, outside the repository.

A task that writes code runs in its own worktree on its own branch. The user's
checkout is never touched while the attempts run, and a task that passed leaves
exactly one branch to merge.

A task that stopped for a person — blocked on a refused command,
a cell that needs approval — keeps its branch too, marked unverified: the work
its workers did is real, and throwing it away is what made "I created the
routes" read as a lie when the files were nowhere. `locks resume` continues on
that branch. A task that failed or must be replanned leaves nothing behind.

Between attempts the worktree is reset only when the *approach* was wrong (the
next attempt is a different model starting over). When the work was shallow,
the next, more thorough attempt continues from what the last one left, because
that is the work it was asked to finish.

A worktree has only what git carries. The checkout's ignored dependency folders
(`node_modules`, a virtualenv) are linked in and the person's own
`.claude/settings.local.json` is copied in, so a build can run and the worker
is allowed — and refused — what the person set; both are taken out again
before anything is committed.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from locks import legacy

#: Folders a build needs and git does not carry. Linked, not copied: shared with
#: the checkout, which is why the brief tells the worker not to reinstall them.
DEPENDENCY_DIRS = frozenset({"node_modules", ".venv", "venv", "vendor"})
#: How the commit of work kept without a pass is marked: a trailer, so the
#: subject stays the change's. `UNVERIFIED_BEFORE` is how a subject said it once.
UNVERIFIED = "Locks-Verified: no"
UNVERIFIED_BEFORE = "(unverified"
#: The trailer that ties a commit to its task.
TASK_TRAILER = "Locks-Task"
#: How many files of the kept commit locks' own check wrote (`fingerprint`, `rewritten`).
CHECK_OUTPUT_TRAILER = "Locks-Check-Output"
#: How long a commit's subject and the summary under it may be.
SUBJECT_CHARS, BODY_CHARS = 72, 700
#: The person's own permission rules for the project, never committed. Copied,
#: not linked: the worker reads its own copy, and nothing it does reaches theirs.
LOCAL_SETTINGS = ".claude/settings.local.json"


class IsolationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Workspace:
    repo_dir: Path
    path: Path
    branch: str
    base: str
    #: Paths, relative to the checkout, linked (or, a file, copied) into the worktree.
    links: tuple[str, ...] = ()
    #: Dependency folders left out: the lockfile beside them here differs from the checkout's,
    #: so what is installed there is not what this commit needs.
    stale: tuple[str, ...] = ()
    #: A branch of the task's name that held none of its work, renamed to this
    #: before the task started (`prepare(fresh=True)`); None when there was none.
    set_aside: str | None = None
    #: The task's folder git no longer knew (a cancel's removal stopped half way), moved
    #: here before the worktree was added again (`_move_stray`); None when there was none.
    stray: Path | None = None


#: Seconds any git call of locks may run: a git stuck on a lock, a dead network mount or a prompt
#: nobody can answer would otherwise hold a hook, a dispatcher or a run forever.
GIT_TIMEOUT_S = 120


def git_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a git call runs in: `base` (default: this process's) with git told never to
    ask a question on a terminal nobody is at."""
    return {**(os.environ if base is None else base), "GIT_TERMINAL_PROMPT": "0"}


def _run_git(args: Sequence[str], cwd: Path, *, env: Mapping[str, str] | None = None,
             **kwargs: Any) -> subprocess.CompletedProcess:
    """`git <args>` in `cwd`, output captured, bounded by `GIT_TIMEOUT_S`, never prompting.
    A call that outlasts the timeout raises `IsolationError`; the caller decides what a git
    that could not answer means for it. Never checks the exit code."""
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), env=git_env(env), capture_output=True, check=False,
                              timeout=GIT_TIMEOUT_S, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise IsolationError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_S}s") from exc


def _git(cwd: Path, *args: str, check: bool = True, strip: bool = True) -> str:
    proc = _run_git(args, cwd, text=True)
    if check and proc.returncode != 0:
        raise IsolationError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip() if strip else proc.stdout


def branch_for(task_id: int) -> str:
    return f"locks/task-{task_id}"


#: Checkouts whose branches and refs of the old name were renamed in this process.
_ADOPTED: set[str] = set()


def adopt_legacy(repo_dir: Path) -> list[tuple[str, str]]:
    """Rename what a task kept under the old name (its task, foreign and merge branches and its
    set-aside refs, `legacy.REF_PREFIXES`) to the new one, tips and reflogs kept, once per
    checkout and process: a resume of a task from before the rename finds its work. A name
    already taken under the new prefix is left as it is. Never raises."""
    try:
        common = _git(repo_dir, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    except (OSError, IsolationError):
        return []
    if not common or common in _ADOPTED:
        return []
    _ADOPTED.add(common)
    moved: list[tuple[str, str]] = []
    try:
        listing = _git(repo_dir, "for-each-ref", "--format=%(refname)", *(old for old, _ in legacy.REF_PREFIXES),
                       "refs/heads/locks/", "refs/locks/", check=False)
        for old, new in legacy.old_refs(listing):
            if old.startswith("refs/heads/"):
                # `branch -m` carries the reflog and a worktree checked out on it.
                ok = _run_git(["branch", "-m", old.removeprefix("refs/heads/"), new.removeprefix("refs/heads/")],
                              repo_dir).returncode == 0
            else:
                sha = _git(repo_dir, "rev-parse", "--verify", "--quiet", old, check=False)
                ok = bool(sha) and _run_git(["update-ref", new, sha], repo_dir).returncode == 0
                ok = ok and _run_git(["update-ref", "-d", old, sha], repo_dir).returncode == 0
            if ok:
                moved.append((old, new))
    except (OSError, IsolationError):
        pass  # a git that hangs moves nothing more: what was renamed so far stands, the rest is tried next process
    return moved


def last_commit(repo_dir: Path, branch: str) -> str | None:
    """`<sha> <subject>` of a branch's newest commit, or None when it is gone."""
    adopt_legacy(repo_dir)
    return _git(repo_dir, "log", "-1", "--format=%h %s", f"refs/heads/{branch}", "--", check=False) or None


#: Where a branch named for a task that holds none of its work is moved (a store
#: started over, another locks home on the same checkout): kept, never deleted.
FOREIGN = "locks/foreign/task-{id}-{tip}"


def _set_aside(top: Path, task_id: int) -> str:
    """Rename the branch named for a task that never ran out of the task's way,
    with its tip in the new name: nothing is lost, and the task starts on its base."""
    branch = branch_for(task_id)
    tip = _git(top, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)[:12]
    aside = FOREIGN.format(id=task_id, tip=tip)
    _git(top, "branch", "-m", branch, aside)
    return aside


def kept(repo_dir: Path, task_id: int) -> bool:
    """Whether the task's branch exists: work it kept, verified or not."""
    adopt_legacy(repo_dir)
    return bool(_git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_for(task_id)}", check=False))


def _attached(top: Path, path: Path, branch: str) -> bool:
    """Whether `path` is already a worktree of `top` on `branch`."""
    listing = _git(top, "worktree", "list", "--porcelain", check=False) or ""
    for block in listing.split("\n\n"):
        lines = block.splitlines()
        here = any(_same_path(line, path) for line in lines)
        if here and f"branch refs/heads/{branch}" in lines:
            return True
    return False


def _clear(path: Path) -> None:
    """What `git worktree remove` left of a worktree it was told to remove: files a process
    still wrote into while it went (a dev server's cache), or that it could not delete. Its
    links are gone by then, so this never reaches into the checkout."""
    if path.exists() and not (path / ".git").exists():
        shutil.rmtree(path, ignore_errors=True)


def _registered(top: Path, path: Path) -> bool:
    """Whether `path` is a worktree git knows of, on any branch."""
    listing = _git(top, "worktree", "list", "--porcelain", check=False) or ""
    return any(_same_path(line, path) for line in listing.splitlines())


def _same_path(line: str, path: Path) -> bool:
    """Whether a `worktree <path>` line of `git worktree list --porcelain` names `path`, read
    through links: a home moved from the old name leaves git its old path, a link to the new."""
    if not line.startswith("worktree "):
        return False
    listed = line.removeprefix("worktree ")
    if listed in (str(path), str(path.resolve())):
        return True
    try:
        return Path(listed).resolve() == path.resolve()
    except (OSError, RuntimeError):  # pragma: no cover - a link loop: not this path
        return False


def _move_stray(top: Path, path: Path) -> Path | None:
    """A task's folder that is no worktree of its repository any more (a cancel whose
    removal stopped half way): moved aside under `worktrees/stray/`, never deleted, since
    it may hold files nobody kept, so the worktree can be added again. Where it went."""
    if not path.exists() or path.is_symlink() or _registered(top, path):
        return None
    aside = path.parent / "stray" / f"{path.name}-{time.time_ns()}"
    aside.parent.mkdir(parents=True, exist_ok=True)
    path.rename(aside)
    return aside


def prepare(repo_dir: Path, task_id: int, root: Path, *, base: str | None = None,
            fresh: bool = False) -> Workspace:
    """The task's worktree, on its own branch: from HEAD, or from `base` (a
    commit a workflow step builds on). A `base` that cannot be read is refused,
    never replaced by HEAD: work built on the wrong commit looks like work. A
    task that never ran before (`fresh`) owns no branch: one of its name already
    here is another's (a store started over, another locks home), set aside
    (`FOREIGN`) and never built on."""
    top = Path(_git(repo_dir, "rev-parse", "--show-toplevel"))
    adopt_legacy(top)
    head = _git(top, "rev-parse", "HEAD")
    if base:
        head = _git(top, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False)
        if not head:
            raise IsolationError(f"the commit this task builds on ({base[:12]}) is not in {top}: "
                                 "the branch that held it is gone")
    branch = branch_for(task_id)
    path = root / "worktrees" / f"task-{task_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(top, "worktree", "prune", check=False)  # a run that died left its entry behind
    stray = _move_stray(top, path)
    aside = None
    if fresh and not _attached(top, path, branch) and kept(top, task_id):
        aside = _set_aside(top, task_id)
    if _attached(top, path, branch):
        # A run whose process died left its worktree, uncommitted work and all:
        # the resume continues in it instead of failing to add it again.
        base = _fork_point(top, head, branch, path)
    elif kept(top, task_id):
        # A resumed task continues from the work it kept (its branch mended first, when
        # its newest commit cannot be read: no worktree can be added on it).
        base = _fork_point(top, head, branch, path)
        _git(top, "worktree", "add", str(path), branch)
    else:
        _git(top, "worktree", "add", "-b", branch, str(path), head)
        base = head
    shared = _shared(top, path)
    stale = tuple(rel for rel in shared if _stale(top, path, rel))
    ws = Workspace(top, path, branch, base, tuple(rel for rel in shared if rel not in stale), stale, aside, stray)
    link(ws)
    return ws


def _fork_point(top: Path, head: str, branch: str, path: Path) -> str:
    """Where a kept branch left `head`. A git that died mid-write (a full disk) can leave
    the branch's newest commit unreadable, and every resume would fail on it: the branch
    goes back to the newest commit its reflog holds that git can still read (what the
    lost commit held stays in the worktree's files, as changes not committed). When none
    can be read, the resume stops with the cause in a line, the branch and its worktree
    kept as they are."""
    proc = _run_git(["merge-base", head, branch], top, text=True)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    cause = " ".join((proc.stderr or "no common commit").split())[:200]
    tip = _git(top, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    common = Path(_git(top, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False) or top / ".git")
    try:
        lines = (common / "logs" / "refs" / "heads" / branch).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        sha = line.split(" ", 2)[1] if len(line.split(" ", 2)) > 1 else ""
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha) or sha == tip:
            continue
        found = _run_git(["merge-base", head, sha], top, text=True)
        if found.returncode == 0 and found.stdout.strip():
            _git(top, "update-ref", "-m", f"locks: {tip or 'its tip'} cannot be read ({cause})",
                 f"refs/heads/{branch}", sha)
            return found.stdout.strip()
    raise IsolationError(f"the kept branch {branch} cannot be read after a git failure ({cause}); its work "
                         f"stays on it and in {path}: `git -C {top} fsck` names what is damaged")


#: The files that say what a dependency folder holds, by the folder's name.
LOCKFILES = {
    "node_modules": ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock",
                     "bun.lockb"),
    ".venv": ("uv.lock", "poetry.lock", "Pipfile.lock", "requirements.txt"),
    "venv": ("uv.lock", "poetry.lock", "Pipfile.lock", "requirements.txt"),
    "vendor": ("composer.lock",),
}


def _stale(top: Path, path: Path, rel: str) -> bool:
    """Whether a dependency folder of the checkout would be wrong in the worktree: a
    lockfile beside it differs between the two (the branch moved its dependencies)."""
    folder = Path(rel)
    for name in LOCKFILES.get(folder.name, ()):
        mine, theirs = path / folder.parent / name, top / folder.parent / name
        if mine.is_file() != theirs.is_file():
            return True
        if mine.is_file() and _lock_text(name, mine) != _lock_text(name, theirs):
            return True
    return False


def _lock_text(name: str, file: Path) -> bytes:
    """A lockfile's content with the project's own version taken out: a release bump changes only
    that, and the dependencies installed beside it are still right. Anything else is kept."""
    raw = file.read_bytes()
    try:
        if name == "uv.lock":
            return _without_uv_self_version(raw.decode())
        if name in ("package-lock.json", "npm-shrinkwrap.json"):
            data = json.loads(raw)
            if isinstance(data, dict):
                data.pop("version", None)
                root = (data.get("packages") or {}).get("")
                if isinstance(root, dict):
                    root.pop("version", None)
                return json.dumps(data, sort_keys=True).encode()
    except (ValueError, UnicodeDecodeError, AttributeError):
        pass  # a lockfile this reader cannot parse is compared as written
    return raw


def _without_uv_self_version(text: str) -> bytes:
    """uv.lock without the version line of the [[package]] whose source is the project itself."""
    blocks = re.split(r"(?m)^(?=\[\[package\]\]\s*$)", text)
    own = re.compile(r"^source = \{ (?:editable|virtual) = \"\.\" \}\s*$", re.M)
    out = []
    for block in blocks:
        if block.startswith("[[package]]") and own.search(block):
            block = re.sub(r"(?m)^version = .*\n", "", block, count=1)
        out.append(block)
    return "".join(out).encode()


def _shared(top: Path, path: Path) -> tuple[str, ...]:
    ignored = _git(top, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z",
                   check=False, strip=False)
    found = [e.rstrip("/") for e in ignored.split("\0") if e.endswith("/") and Path(e.rstrip("/")).name
             in DEPENDENCY_DIRS]
    if (top / LOCAL_SETTINGS).is_file() and not (path / LOCAL_SETTINGS).exists():
        found.append(LOCAL_SETTINGS)
    return tuple(sorted(found))


def link(ws: Workspace) -> None:
    for rel in ws.links:
        place, target = ws.path / rel, ws.repo_dir / rel
        if place.exists() or place.is_symlink() or not target.exists():
            continue
        place.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir():
            place.symlink_to(target, target_is_directory=True)
        else:
            shutil.copyfile(target, place)


def unlink(ws: Workspace) -> None:
    for rel in ws.links:
        place = ws.path / rel
        if place.is_symlink() or place.is_file():
            place.unlink()
        parent = place.parent
        while parent != ws.path and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def _linked(ws: Workspace, path: str) -> bool:
    return any(path == rel or path.startswith(rel + "/") for rel in ws.links)


#: Where the work an attempt left is set aside before a reset: a ref per attempt, so
#: the next model starts clean and can still read what the last one did.
ASIDE = "refs/locks/aside"


def set_aside(ws: Workspace, attempt: int) -> str | None:
    """Keep the worktree's work as it is on a ref of its own, before a reset throws
    it away. None when there is nothing to keep."""
    if not has_changes(ws):
        return None
    _git(ws.path, "add", "-A")
    tree = _git(ws.path, "write-tree")
    commit = _git(ws.path, *_identity(ws.path), "commit-tree", tree, "-p", _git(ws.path, "rev-parse", "HEAD"),
                  "-m", f"locks: what attempt {attempt} left, set aside before a reset")
    ref = f"{ASIDE}/{ws.branch.rsplit('/', 1)[-1]}-{attempt}"
    _git(ws.path, "update-ref", ref, commit)
    return ref


def drop_aside(ws: Workspace) -> None:
    """Forget the work set aside for this task: nothing will resume from it."""
    prefix = f"{ASIDE}/{ws.branch.rsplit('/', 1)[-1]}-"
    for ref in _git(ws.repo_dir, "for-each-ref", "--format=%(refname)", ASIDE, check=False).split():
        if ref.startswith(prefix):
            _git(ws.repo_dir, "update-ref", "-d", ref, check=False)


def dirty(top: Path) -> list[str]:
    """What a checkout holds that is not committed: changed, added, deleted or untracked
    (ignored files aside)."""
    status = _git(top, "status", "--porcelain", "-uall", "-z", check=False, strip=False)
    return sorted({e[3:] for e in status.split("\0") if len(e) > 3 and e[2] == " " and not is_artifact(e[3:])})


def named_in(text: str, paths: Sequence[str]) -> list[str]:
    """The paths a task's text names: by path, or by a file name of four characters or more."""
    low = text.lower()
    out = []
    for p in paths:
        name = Path(p).name.lower()
        if p.lower() in low or (len(name) >= 4 and re.search(rf"(?<![\w./-]){re.escape(name)}(?![\w-])", low)):
            out.append(p)
    return out


def carry(ws: Workspace, paths: Sequence[str]) -> list[str]:
    """Bring the person's uncommitted state of `paths` into the worktree, a deletion as
    a deletion: work the task builds on that no commit holds yet."""
    carried = []
    for rel in paths:
        src, dst = ws.repo_dir / rel, ws.path / rel
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif not src.exists() and dst.is_file():
            dst.unlink()
        else:
            continue
        carried.append(rel)
    return carried


#: A path-looking word of a task's text: `scripts/slides/`, `config.local.json`.
PATHLIKE = re.compile(r"(?<![\w/.-])((?:\.?[\w@-]+/)+[\w.@-]*|[\w-]+\.[A-Za-z0-9]{1,8})(?![\w/-])")
#: The most a copied ignored path may weigh: past it, the task says so instead.
BRING_BYTES = 50 * 1024 * 1024


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += (Path(root) / f).stat().st_size
            if total > BRING_BYTES:
                return total
    return total


def ignored_named(ws: Workspace, text: str) -> list[str]:
    """Paths the task's text names that the person's checkout has, git ignores, and the
    worktree therefore lacks."""
    out = []
    for word in sorted(set(PATHLIKE.findall(text))):
        rel = word.strip("/").removeprefix("./")
        if not rel or ".." in rel.split("/") or not (ws.repo_dir / rel).exists() or (ws.path / rel).exists():
            continue
        try:
            ignored = _run_git(["check-ignore", "-q", rel], ws.repo_dir).returncode == 0
        except IsolationError:
            continue  # a git that cannot say names nothing as ignored
        if ignored:
            out.append(rel)
    return out


def bring(ws: Workspace, rels: Sequence[str]) -> tuple[list[str], list[str]]:
    """Copy ignored paths from the person's checkout into the worktree: (copied, too big).
    Copies, never links: what the worker does to them stays out of the person's files,
    and being ignored they are never committed."""
    copied, big = [], []
    for rel in rels:
        src, dst = ws.repo_dir / rel, ws.path / rel
        if _size(src) > BRING_BYTES:
            big.append(rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
        copied.append(rel)
    return copied, big


def race_lane(ws: Workspace, root: Path) -> Workspace:
    """A second worktree beside a task's, on its own branch from the same base, with the
    same dependencies linked: where the rung a race runs works (`orchestrate.race_cell`)."""
    branch = f"{ws.branch}-race"
    path = root / "worktrees" / f"{ws.path.name}-race"
    _git(ws.repo_dir, "worktree", "prune", check=False)
    if path.exists():
        _git(ws.repo_dir, "worktree", "remove", "--force", str(path), check=False)
    _git(ws.repo_dir, "branch", "-D", branch, check=False)
    _git(ws.repo_dir, "worktree", "add", "-b", branch, str(path), ws.base)
    lane = Workspace(ws.repo_dir, path, branch, ws.base, ws.links)
    link(lane)
    return lane


@contextlib.contextmanager
def at_base(repo_dir: Path, base: str, root: Path, at: Path | None = None) -> Iterator[Path]:
    """A temporary detached worktree of `repo_dir` at `base` (made at `at` when given), with the
    checkout's ignored dependency folders linked in as a task's are (one whose lockfile differs at
    `base` is left out, `Workspace.stale`): where locks runs a check on the commit a task started
    from. Removed when the block ends, whatever happened in it."""
    top = Path(_git(repo_dir, "rev-parse", "--show-toplevel"))
    commit = _git(top, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False)
    if not commit:
        raise IsolationError(f"the base commit {base[:12]} is not in {top}")
    path = at or root / "worktrees" / f"base-{commit[:12]}-{os.getpid()}-{time.monotonic_ns()}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(top, "worktree", "add", "--detach", str(path), commit)
    ws = Workspace(top, path, "", commit)
    try:
        deps = tuple(rel for rel in _shared(top, path) if Path(rel).name in DEPENDENCY_DIRS)
        ws = Workspace(top, path, "", commit, tuple(rel for rel in deps if not _stale(top, path, rel)),
                       tuple(rel for rel in deps if _stale(top, path, rel)))
        link(ws)
        yield path
    finally:
        # The links go first: removing the worktree must never reach through them into the checkout.
        unlink(ws)
        _git(top, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
        _git(top, "worktree", "prune", check=False)


@contextlib.contextmanager
def at_bases(folder: Path, places: Sequence[tuple[str, str]], root: Path) -> Iterator[Path]:
    """`at_base` for a folder that is no checkout but holds some: a temporary folder with, at each
    `name`, a detached worktree of the repository `folder/name` at its commit (`places`), laid out as
    `folder` is so a check that names them (`cd web && npm test`) reads the same paths. What is in
    `folder` outside those repositories is not copied. Everything is removed when the block ends."""
    scratch = root / "worktrees" / f"base-{os.getpid()}-{time.monotonic_ns()}"
    try:
        with contextlib.ExitStack() as stack:
            for name, commit in places:
                stack.enter_context(at_base(folder / name, commit, root, at=scratch / name))
            yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def places_label(places: Sequence[tuple[str, str]]) -> str:
    """The commits of a folder's repositories as one line (`api@0123456789ab, web@…`): what stands
    for the base of a run that started in a folder that is no checkout."""
    return ", ".join(f"{name}@{commit[:12]}" for name, commit in places)


def base_label(base: str) -> str:
    """A base as the reports name it: a commit cut to twelve characters, or the line `places_label` made."""
    return base if "@" in base else base[:12]


def adopt(src: Workspace, dst: Workspace) -> tuple[str, ...]:
    """Make the task's worktree hold what a race's lane did: `dst` back to its base, and
    `src`'s change against that base applied on it, uncommitted, as a worker leaves its
    work. What it changed, from git."""
    if src.links:
        _git(src.path, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *src.links, check=False)
    _git(src.path, "add", "-A")
    if src.links:
        _git(src.path, "reset", "-q", "--", *src.links, check=False)
    patch = _git(src.path, "diff", "--cached", "--binary", src.base, strip=False)
    reset(dst)
    if patch.strip():
        proc = _run_git(["apply", "--binary", "--whitespace=nowarn"], dst.path, input=patch, text=True)
        if proc.returncode != 0:
            raise IsolationError(f"the race's work could not be carried over: {proc.stderr.strip()[:300]}")
    return changed(dst)


def drop(ws: Workspace) -> None:
    """Remove a worktree and its branch, keeping nothing: a race's lane once it is decided."""
    unlink(ws)
    _git(ws.repo_dir, "worktree", "remove", "--force", str(ws.path), check=False)
    _clear(ws.path)
    _git(ws.repo_dir, "branch", "-D", ws.branch, check=False)


def reset(ws: Workspace) -> None:
    """Back to the base commit, in the task's worktree only."""
    _git(ws.path, "reset", "--hard", ws.base)
    _git(ws.path, "clean", "-fdq")
    link(ws)


#: Folders a worker's tools write for their own use (a browser server's page
#: snapshots and console logs, an interpreter's bytecode, a dev-server plugin's state, the
#: database a code index (livespec) builds in the directory it is asked to index).
#: They are no work: never a changed file, never committed to a kept branch.
ARTIFACT_DIRS = frozenset({".playwright-mcp", "__pycache__", ".fresh-dev", ".mcp-docs"})


def is_artifact(path: str) -> bool:
    return any(part in ARTIFACT_DIRS for part in path.split("/")[:-1])


def changed(ws: Workspace) -> tuple[str, ...]:
    """What the task changed against its base, from git: committed or not."""
    # A status that never takes the index lock: a watcher's read must not fail the
    # worker's own `git add`, nor locks' commit at the end of the run.
    status = _git(ws.path, "--no-optional-locks", "status", "--porcelain", "-uall", "-z", strip=False)
    paths = {e[3:] for e in status.split("\0") if len(e) > 3}
    paths |= set(_git(ws.path, "diff", "--name-only", ws.base, "HEAD").splitlines())
    return tuple(sorted(p for p in paths if p and not _linked(ws, p) and not is_artifact(p)))


def snapshot(path: Path) -> str | None:
    """A commit of a worktree as it stands, tracked and untracked files alike (what `.gitignore`
    keeps out stays out), made through a temporary index so the worktree's own index, HEAD and
    branch are left as they were, and dangling: nothing points at it. What an independent check
    read, so the next round reads only what changed since (`changed_since`). None when git cannot
    say."""
    import tempfile

    try:
        with tempfile.TemporaryDirectory(prefix="locks-snapshot-") as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index"), "GIT_AUTHOR_NAME": "locks",
                   "GIT_AUTHOR_EMAIL": "locks@localhost", "GIT_COMMITTER_NAME": "locks",
                   "GIT_COMMITTER_EMAIL": "locks@localhost"}

            def git(*args: str) -> str:
                proc = _run_git(args, path, env=env, text=True)
                if proc.returncode != 0:
                    raise IsolationError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
                return proc.stdout.strip()

            parent = git("rev-parse", "--verify", "HEAD")
            git("read-tree", parent)
            git("add", "-A", "--", ".")
            return git("commit-tree", git("write-tree"), "-p", parent, "-m", "locks: what an independent check read")
    except (IsolationError, OSError):
        return None


def changed_since(path: Path, commit: str) -> list[str] | None:
    """The files of a worktree that differ from a `snapshot` of it, added and deleted ones among
    them, its tools' artifacts left out. None when git cannot say."""
    now = snapshot(path)
    if now is None:
        return None
    try:
        names = _git(path, "diff", "--name-only", commit, now).splitlines()
    except (IsolationError, OSError):
        return None
    return sorted(p for p in names if p and not is_artifact(p))


def put_back(path: Path, commit: str, paths: Sequence[str]) -> list[str]:
    """These paths of a worktree as a `snapshot` of it held them: a file it held is written back,
    one it did not is removed. What could not be put back is returned."""
    left: list[str] = []
    for name in paths:
        target = path / name
        try:
            held = _run_git(["cat-file", "-e", f"{commit}:{name}"], path).returncode == 0
            if held:
                shown = _run_git(["show", f"{commit}:{name}"], path)
                if shown.returncode != 0:
                    raise subprocess.CalledProcessError(shown.returncode, shown.args)
                blob = shown.stdout
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)
            elif target.is_file() or target.is_symlink():
                target.unlink()
        except (OSError, subprocess.CalledProcessError, IsolationError):
            left.append(name)
    return left


def fingerprint(path: Path) -> dict[str, str] | None:
    """Every file of a worktree that differs from its HEAD (tracked or not), with the blob
    git would make of it ("" when deleted): taken before and after locks runs a check, to
    tell what the check itself wrote. None when git cannot say."""
    try:
        status = _git(path, "--no-optional-locks", "status", "--porcelain", "-uall", "-z", strip=False)
    except (IsolationError, OSError):
        return None
    entries = status.split("\0")
    paths: list[str] = []
    skip = False
    for entry in entries:
        if skip:  # the old name of a rename
            skip = False
            continue
        if len(entry) > 3:
            paths.append(entry[3:])
            skip = entry[0] in "RC"
    present = [p for p in paths if (path / p).is_file() and not (path / p).is_symlink()]
    try:
        proc = _run_git(["hash-object", "--stdin-paths"], path, input="\n".join(present),
                        text=True) if present else None
    except IsolationError:
        return None
    if proc is not None and proc.returncode != 0:
        return None
    blobs = dict(zip(present, proc.stdout.split(), strict=False)) if proc is not None else {}
    return {p: blobs.get(p, "") for p in paths if not is_artifact(p)}


def rewritten(before: Mapping[str, str] | None, after: Mapping[str, str] | None) -> tuple[str, ...]:
    """The files a check wrote: their content after it differs from before it."""
    if before is None or after is None:
        return ()
    return tuple(sorted(p for p in {*before, *after} if before.get(p) != after.get(p)))


#: git's empty tree: what a checkout with no commit yet is compared against.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def stand(directory: Path, checkouts: Sequence[Path]) -> list[dict[str, Any]]:
    """Where each checkout a run with no worktree may change stands as an attempt starts: its
    commit and the files already differing from it, with the blob of each. Kept on the attempt's
    start so what the worker wrote can be read later in each of them, against what the person
    already had there. `name` is the checkout's path from `directory` ("" for the directory
    itself), the prefix its files are listed under. A checkout git cannot read is left out."""
    out = []
    for top in checkouts:
        try:
            head = _git(top, "rev-parse", "--verify", "--quiet", "HEAD", check=False) or EMPTY_TREE
        except OSError:
            continue
        dirty = fingerprint(top)
        if dirty is None:
            continue
        # A checkout above the folder lists its files from its own root: no name in front.
        name = "" if top.resolve() == directory.resolve() or not top.resolve().is_relative_to(directory.resolve()) \
            else top.resolve().relative_to(directory.resolve()).as_posix()
        out.append({"path": str(top), "name": name, "head": head, "dirty": dirty})
    return out


def named(place: Mapping[str, Any], path: str) -> str:
    """A file of a checkout as the folder that holds it lists it: the checkout's name in front."""
    return f"{place['name']}/{path}" if place.get("name") else path


def moved_in_place(place: Mapping[str, Any]) -> tuple[str, ...] | None:
    """What changed in a checkout since `stand` read it, paths from the checkout: a file whose
    content differs from what it held then (one already dirty and left alone is not it), and
    what was committed after that commit. None when git cannot say."""
    top = Path(place["path"])
    try:
        now = fingerprint(top)
        moved = _git(top, "rev-parse", "HEAD", check=False) != place["head"]
        # Only commits made on the branch itself: what a merge brought in is another's work.
        committed = _git(top, "log", "--first-parent", "--no-merges", "--name-only", "--format=",
                         f"{place['head']}..HEAD", check=False).splitlines() if moved else []
    except OSError:
        return None
    if now is None:
        return None
    before = place.get("dirty") or {}
    # HEAD moved (the session merged there): a file that differs from the stand only because
    # HEAD holds it now is clean at the end and is not the run's; a dirty one whose content
    # differs from what the stand recorded is, and so is a commit made on the branch itself.
    dirty = {p for p, blob in now.items() if before.get(p) != blob} if moved else set(rewritten(before, now))
    return tuple(sorted(dirty | {p for p in committed if p and not is_artifact(p)}))


def changed_so_far(root: Path, repo_dir: Path, task_id: int, started: Mapping[str, Any]) -> tuple[str, ...] | None:
    """What a running task changed so far in its worktree, against the base its
    attempt started from (`started` is its `attempt_started` event's data, which
    names the base and the linked dependencies); with no worktree, in each checkout
    the attempt recorded (`places`), against how it stood then. None when git cannot
    say: no worktree and no places, no base, a git that failed."""
    path = root / "worktrees" / f"task-{task_id}"
    if not path.is_dir() and started.get("places"):
        found = [(place, moved_in_place(place)) for place in started["places"]]
        if any(moved is None for _, moved in found):
            return None
        return tuple(sorted(named(place, p) for place, moved in found for p in moved or ()))
    if not path.is_dir() or not started.get("base"):
        return None
    ws = Workspace(repo_dir, path, branch_for(task_id), str(started["base"]), links=tuple(started.get("links") or ()))
    try:
        return changed(ws)
    except (IsolationError, OSError):
        return None


def undo(ws: Workspace, paths: Sequence[str]) -> None:
    """Put these paths back as they were at the base, in the task's worktree only:
    a file the base had is restored, one it did not have is removed."""
    for path in paths:
        known = _run_git(["cat-file", "-e", f"{ws.base}:{path}"], ws.path).returncode == 0
        if known:
            _git(ws.path, "checkout", ws.base, "--", path)
        else:
            target = ws.path / path
            if target.is_file() or target.is_symlink():
                target.unlink()


def has_changes(ws: Workspace) -> bool:
    return bool(changed(ws))


def finish(ws: Workspace, *, keep: bool, message: str) -> str | None:
    """Commit and keep the branch when `keep`, else drop both. Returns the branch kept."""
    kept_branch = None
    unlink(ws)
    if ws.links:
        # A worker that committed a link committed a pointer into this machine.
        _git(ws.path, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *ws.links, check=False)
    if keep and has_changes(ws):
        staged = False
        if _git(ws.path, "status", "--porcelain"):
            _git(ws.path, "add", "-A")
            _git(ws.path, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--",
                 *(f":(glob)**/{d}/**" for d in sorted(ARTIFACT_DIRS)), check=False)
            # A worker that committed its work itself leaves only its tools' artifacts behind:
            # once they are dropped nothing is staged, and a commit of nothing failed the run.
            staged = bool(_git(ws.path, "diff", "--cached", "--name-only"))
        if staged:
            _commit(ws, message)
        elif _unverified(_git(ws.path, "log", "-1", "--format=%B")) and not _unverified(message):
            # Work kept unverified has passed since, unchanged: the branch says so.
            _commit(ws, message, "--allow-empty")
        kept_branch = ws.branch
    _git(ws.repo_dir, "worktree", "remove", "--force", str(ws.path), check=False)
    _clear(ws.path)
    if kept_branch is None:
        _git(ws.repo_dir, "branch", "-D", ws.branch, check=False)
        drop_aside(ws)
    return kept_branch


def keep_left(repo_dir: Path, task_id: int, root: Path, *, message: str) -> str | None:
    """A task's worktree left behind (a run that died, then a dismissal): what it holds is
    committed on the task's branch, unverified as `message` says, and the worktree removed,
    as a stop that waits on a person keeps its work (`finish`). The branch kept, or None when
    no worktree of the task is here or it holds nothing."""
    try:
        top = Path(_git(repo_dir, "rev-parse", "--show-toplevel"))
    except (IsolationError, OSError):
        return None
    branch, path = branch_for(task_id), root / "worktrees" / f"task-{task_id}"
    if not path.is_dir() or not _attached(top, path, branch):
        return None
    head = _git(top, "rev-parse", "HEAD")
    base = _git(top, "merge-base", head, branch, check=False) or head
    links = tuple(rel for rel in (*(e.rstrip("/") for e in _git(
        top, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z",
        check=False, strip=False).split("\0") if e.endswith("/")), LOCAL_SETTINGS)
        if rel and ((rel == LOCAL_SETTINGS and (path / rel).is_file()) or (path / rel).is_symlink()))
    ws = Workspace(top, path, branch, base, links)
    if not has_changes(ws):
        return None  # nothing of its own to keep: the worktree is left as it is
    return finish(ws, keep=True, message=message)


def _unverified(message: str) -> bool:
    return (UNVERIFIED in message or legacy.OLD_UNVERIFIED in message
            or UNVERIFIED_BEFORE in message.split("\n", 1)[0])


#: Words a cut subject must not end on: the thought they open is the part that was cut.
_DANGLING = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "its", "his", "her", "their", "our", "your", "my",
    "each", "every", "all", "any", "no", "not", "and", "or", "but", "so", "if", "when", "while", "because",
    "which", "where", "than", "then", "as", "at", "by", "for", "from", "in", "into", "of", "on", "to", "with",
    "via", "per", "after", "before", "over", "under", "about", "between", "through", "during", "until", "upon",
    "is", "are", "was", "were", "be", "been", "will", "would", "should", "can", "could"})


def _subject_of(text: str) -> str:
    """A commit subject from one line: itself when it fits, else cut at the last
    whole word within `SUBJECT_CHARS`, before a word that opens a clause the cut
    left out and before a parenthesis it left open. Never an ellipsis: git
    history is read as sentences."""
    if len(text) <= SUBJECT_CHARS:
        return text
    cut = text[:SUBJECT_CHARS]
    if text[SUBJECT_CHARS] != " ":
        cut = cut.rpartition(" ")[0] or cut  # no space to cut at: the limit is the cut
    words = cut.split()
    while True:
        while words and words[-1].lower().strip(",;:") in _DANGLING:
            words.pop()
        kept = " ".join(words)
        if kept.count("(") <= kept.count(")"):
            break
        words = kept.rpartition("(")[0].split()
    # Nothing but dangling words or an open clause: the whole-word cut is the subject.
    return kept.rstrip(" ,;:-–—(") or cut


def message(task_id: int, *, title: str, subject: str = "", summary: str = "", status: str = "done",
            check_output: int = 0) -> str:
    """The commit message of a task's work, as a project's history reads it: the
    change in one line (the worker's, else the task's title), what it does, and
    locks' trailers: the task, and whether it was verified."""
    import textwrap

    from locks.notes import _SECRET

    whole = _SECRET.sub("[secret]", " ".join((subject or "").split()) or " ".join((title or "").split()))
    whole = whole or f"Work of locks task #{task_id}"
    line = _subject_of(whole)
    parts = [line]
    # A subject that had to be cut is given whole first in the body: the history keeps what was asked.
    if line != whole:
        parts.append(textwrap.fill(whole, 72))
    body = _SECRET.sub("[secret]", " ".join((summary or "").split()))
    if body:
        body = body if len(body) <= BODY_CHARS else body[:BODY_CHARS - 1].rsplit(" ", 1)[0] + "…"
        parts.append(textwrap.fill(body, 72))
    trailers = [f"{TASK_TRAILER}: #{task_id}"]
    if check_output:
        trailers.append(f"{CHECK_OUTPUT_TRAILER}: {check_output} file{'s' if check_output != 1 else ''}")
    if status != "done":
        trailers.append(f"{UNVERIFIED} ({status})")
    parts.append("\n".join(trailers))
    return "\n\n".join(parts)


def _identity(path: Path) -> list[str]:
    return [] if _git(path, "config", "user.email", check=False) else [
        "-c", "user.name=locks", "-c", "user.email=locks@localhost"]


def _commit(ws: Workspace, message: str, *extra: str) -> None:
    # locks' own snapshot of a worker's work, for a person to review: a
    # pre-commit hook that needs approval or a missing install must not lose it.
    _git(ws.path, *_identity(ws.path), "commit", "-q", "--no-verify", *extra, "-m", message)


def join(ws: Workspace, branches: Sequence[str], *, message: Callable[[str], str]) -> dict[str, Any]:
    """Merge `branches` into the task's worktree one by one, in order, each a merge commit
    (`message(branch)`), until one conflicts: that merge is undone, and its conflicting
    files named. {merged, conflict, files, left}: what is left starts with the one that
    conflicted. Raises when a merge fails for another reason (a branch that is gone)."""
    merged: list[str] = []
    for n, branch in enumerate(branches):
        proc = _run_git([*_identity(ws.path), "merge", "--no-ff", "--no-verify", "--no-edit", "-m",
                         message(branch), f"refs/heads/{branch}"], ws.path, text=True)
        if proc.returncode == 0:
            merged.append(branch)
            continue
        files = _git(ws.path, "diff", "--name-only", "--diff-filter=U", check=False).splitlines()
        _git(ws.path, "merge", "--abort", check=False)
        if not files:
            said = " ".join((proc.stderr or proc.stdout).split())[:300]
            raise IsolationError(f"git merge {branch} failed: {said}")
        return {"merged": merged, "conflict": branch, "files": sorted(files), "left": list(branches[n:])}
    return {"merged": merged, "conflict": None, "files": [], "left": []}


def numstat(repo_dir: Path, start: str, end: str) -> list[tuple[int, str]] | None:
    """Each file changed from `start` to `end` with its changed lines (added and removed; a
    binary file counts none), tool artifacts aside. None when git cannot say."""
    try:
        proc = _run_git(["diff", "--numstat", "--no-renames", start, end, "--"], repo_dir, text=True)
    except IsolationError:
        return None
    if proc.returncode != 0:
        return None
    rows = []
    for line in proc.stdout.splitlines():
        added, deleted, path = [*line.split("\t", 2), "", ""][:3]
        if path and not is_artifact(path):
            rows.append(((int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0), path))
    return rows


def generated(repo_dir: Path, paths: Sequence[str]) -> set[str]:
    """The paths the repository marks as generated in its `.gitattributes`: the standard
    `linguist-generated`, or `-diff`. Empty when git cannot say."""
    if not paths:
        return set()
    try:
        proc = _run_git(["check-attr", "-z", "--stdin", "linguist-generated", "diff"], repo_dir,
                        input="\0".join(paths), text=True)
    except IsolationError:
        return set()
    if proc.returncode != 0:
        return set()
    fields = proc.stdout.split("\0")
    out = set()
    for path, attr, value in zip(fields[0::3], fields[1::3], fields[2::3], strict=False):
        if (attr == "linguist-generated" and value in ("set", "true")) or (attr == "diff" and value == "unset"):
            out.add(path)
    return out


def head(repo_dir: Path, ref: str) -> str:
    return _git(repo_dir, "rev-parse", ref)


def current_branch(repo_dir: Path) -> str | None:
    """The branch checked out in `repo_dir`; None when HEAD is detached or git cannot say."""
    return _git(repo_dir, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) or None


def moved_since(repo_dir: Path, base: str) -> set[str] | None:
    """The files the checkout's branch changed since `base` (a task's base commit), as HEAD
    has them now: work that landed after the task started. None when git cannot say."""
    if not base or not _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False):
        return None
    out = _git(repo_dir, "diff", "--name-only", base, "HEAD", "--", check=False)
    return set(out.splitlines())


def behind(repo_dir: Path, tip: str, base: str) -> dict[str, Any] | None:
    """How far `tip` is behind the branch `base` as it is now: the commits `base` gained
    since they parted, and the files both changed since. Read only: nothing is merged or
    rebased. None when git cannot say (the branch is gone, no commit in common)."""
    now = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}^{{commit}}", check=False)
    fork = _git(repo_dir, "merge-base", tip, now, check=False) if now else ""
    if not fork:
        return None
    ours_from, theirs_from = _synced(repo_dir, fork, tip, now)
    count = _git(repo_dir, "rev-list", "--count", f"{theirs_from}..{now}", check=False)
    if not count.isdigit():
        return None
    ours = set(_git(repo_dir, "diff", "--name-only", ours_from, tip, "--", check=False).splitlines())
    theirs = set(_git(repo_dir, "diff", "--name-only", theirs_from, now, "--", check=False).splitlines())
    return {"base": base, "commits": int(count), "both": sorted(ours & theirs)}


def _synced(repo_dir: Path, fork: str, tip: str, now: str) -> tuple[str, str]:
    """Where the branch and the base last held the same content: the newest commit of the
    base since the fork whose tree is the tree of a commit on the branch (a squash or a
    re-commit of work the branch already has), and that commit of the branch. The fork
    itself when there is none: commits are not content, so a base that took in the
    branch's own work under another commit is not ahead of it."""
    on_tip = {}
    for line in _git(repo_dir, "log", "--format=%H %T", f"{fork}..{tip}", check=False).splitlines():
        sha, _, tree = line.partition(" ")
        on_tip.setdefault(tree, sha)
    for line in _git(repo_dir, "log", "--format=%H %T", f"{fork}..{now}", check=False).splitlines():
        sha, _, tree = line.partition(" ")
        if tree in on_tip:
            return on_tip[tree], sha
    return fork, fork


#: Commits behind the branch work lands on from which a task's starting point is said to be old.
TRUNK_BEHIND = 10


def behind_trunk(repo_dir: Path) -> str | None:
    """A task that starts from a checkout whose branch is not the one work lands on, and is
    `TRUNK_BEHIND` or more commits behind it: said, never changed, since which base to build
    on is the person's (a branch of their own may be behind on purpose). None when it is not."""
    from locks import survey

    try:
        top = Path(_git(repo_dir, "rev-parse", "--show-toplevel"))
    except (IsolationError, OSError):
        return None
    trunk = survey.default_branch(top)
    here = current_branch(top)
    if trunk == "HEAD" or here is None or here == trunk or trunk.split("/", 1)[-1] == here:
        return None
    count = _git(top, "rev-list", "--count", f"HEAD..{trunk}", check=False)
    if not count or not count.isdigit() or int(count) < TRUNK_BEHIND:
        return None
    return (f"base: this starts from {here}, {count} commits behind {trunk}, where work lands; its change may "
            f"conflict there. To build on {trunk} instead, switch the checkout (or rebase {here}) before it runs")


def behind_text(found: Mapping[str, Any]) -> str | None:
    """`behind main by 2 commits; both changed: a, b; ...`, or None when the base did not move."""
    if not found.get("commits"):
        return None
    n, both = int(found["commits"]), list(found.get("both") or ())
    text = f"behind {found['base']} by {n} commit{'' if n == 1 else 's'}"
    if both:
        text += ("; both changed: " + ", ".join(both[:8]) + (f" and {len(both) - 8} more" if len(both) > 8 else "")
                 + "; a merge will conflict-check them")
    return text


def landed(repo_dir: Path, branch: str, paths: Sequence[str] = ()) -> bool:
    """Whether a kept branch needs no review any more: it is gone, it is in HEAD's
    history, or (a squash merge) HEAD holds what it changed, file for file."""
    if not _git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
        return True
    try:
        if _run_git(["merge-base", "--is-ancestor", f"refs/heads/{branch}", "HEAD"], repo_dir).returncode == 0:
            return True
        if not paths:
            return False
        return _run_git(["diff", "--quiet", "HEAD", f"refs/heads/{branch}", "--", *paths],
                        repo_dir).returncode == 0
    except IsolationError:
        return False  # a git that cannot say leaves the branch to review



def already_in_head(repo_dir: Path, base: str, tip: str) -> bool:
    """Whether the checkout's HEAD holds, byte for byte, every file the branch at `tip` changed
    against `base`: its work is merged, whatever the history says. A file deleted on both sides
    is equal. False when git cannot say or the branch changed nothing."""
    if not base or not tip:
        return False
    names = _git(repo_dir, "diff", "--name-only", "-z", base, tip, "--", check=False, strip=False)
    paths = [n for n in names.split("\0") if n]
    if not paths:
        return False
    for path in paths:
        mine = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"HEAD:{path}", check=False)
        theirs = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{tip}:{path}", check=False)
        if mine != theirs:
            return False
    return True


def path_landed(repo_dir: Path, path: str, tip: str) -> bool:
    """Whether the blob `path` has at `tip` appears in the history of `path` on HEAD: the work
    landed (a squash merge, say) and HEAD edited the file since. False when git cannot say."""
    blob = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{tip}:{path}", check=False)
    if not blob:
        return False
    # The blob a commit set the file to is the new side of its raw diff line (`:mode mode old new status`).
    raw = _git(repo_dir, "log", "--raw", "--no-abbrev", "--format=", "HEAD", "--", path, check=False)
    return any(line.split()[3] == blob for line in raw.splitlines() if line.startswith(":") and len(line.split()) > 3)


def live_paths(repo_dir: Path, tip: str, paths: Sequence[str]) -> list[str]:
    """The paths whose content at `tip` differs from HEAD's and whose tip blob HEAD's history of
    the path does not hold (`path_landed`): what merging the branch would still change here."""
    live = []
    for path in paths:
        mine = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"HEAD:{path}", check=False)
        theirs = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{tip}:{path}", check=False)
        if mine != theirs and not path_landed(repo_dir, path, tip):
            live.append(path)
    return live


def kept_branches(repo_dir: Path) -> dict[int, str]:
    """Every `locks/task-<id>` branch in a checkout, by task id, with its tip."""
    adopt_legacy(repo_dir)
    out = _git(repo_dir, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/locks/", check=False)
    found = {}
    for line in out.splitlines():
        name, _, tip = line.partition(" ")
        number = name.removeprefix("locks/task-")
        if number.isdigit() and tip:
            found[int(number)] = tip
    return found


def restore(repo_dir: Path, branch: str, tip: str) -> bool:
    """Point `branch` at `tip` again when the branch is gone and the commit is still in
    the repository: a kept branch something deleted while work still built on it."""
    adopt_legacy(repo_dir)
    if _git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
        return False
    if not _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{tip}^{{commit}}", check=False):
        return False
    try:
        return _run_git(["branch", branch, tip], repo_dir).returncode == 0
    except IsolationError:
        return False


def tree_of(repo_dir: Path, ref: str) -> str:
    """The tree a commit holds: two branches with one tree hold the same work."""
    return _git(repo_dir, "rev-parse", f"{ref}^{{tree}}", check=False)


def prunable(repo_dir: Path, branch: str) -> str | None:
    """Why a kept branch can be deleted with nothing lost, or None. Its commits are
    on another branch (a merge); or HEAD holds every file it changed, as it
    changed them (a squash or a cherry-pick); or all it changed is tool artifacts.
    A branch checked out anywhere, or only on a remote's `locks/` copy (or its old name's), stays."""
    adopt_legacy(repo_dir)
    tip = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if not tip:
        return None
    if f"branch refs/heads/{branch}\n" in _git(repo_dir, "worktree", "list", "--porcelain", strip=False, check=False):
        return None
    holders = _git(repo_dir, "for-each-ref", "--contains", tip, "--format=%(refname)", "refs/heads", "refs/remotes",
                   check=False).splitlines()
    copies = ("/locks/task-", f"/{legacy.OLD}/task-")
    if any(not any(c in ref for c in copies) and not ref.endswith("/HEAD") for ref in holders):
        return "merged"
    base = _git(repo_dir, "merge-base", "HEAD", tip, check=False)
    if not base:
        return None
    paths = _git(repo_dir, "diff", "--name-only", base, tip, check=False).splitlines()
    if paths and all(is_artifact(path) for path in paths):
        return "artifacts only"
    try:
        same = _run_git(["diff", "--quiet", "HEAD", tip, "--", *paths], repo_dir)
    except IsolationError:
        return None
    return "squash-merged" if paths and same.returncode == 0 else None


def delete_branch(repo_dir: Path, branch: str) -> bool:
    try:
        return _run_git(["branch", "-D", branch], repo_dir).returncode == 0
    except IsolationError:
        return False


# --- one run at a time in a checkout ----------------------------------------
#
# A run without a worktree works in the person's checkout itself. Two of them at
# once share its files and its node_modules: one reinstalls while the other's
# check runs, and the check fails for a reason that is neither task's work.

#: How often a run that waits for the checkout looks again, and for a cancel.
WAIT_POLL_S = 2.0


def held_by(directory: Path, toplevel: Path | None) -> list[Path]:
    """What a run in `directory` holds: its checkout; or, in a folder that is no
    checkout, the folder and every checkout right inside it, since its work can
    reach them all. Sorted, so runs that hold several take them in one order and
    never wait on each other in a ring."""
    if toplevel is not None:
        return [toplevel.resolve()]
    inside = []
    with contextlib.suppress(OSError):
        inside = [child.resolve() for child in directory.iterdir() if (child / ".git").exists()]
    return sorted({directory.resolve(), *inside})


def checkout_lock(root: Path, checkout: Path) -> Path:
    digest = hashlib.sha256(str(checkout.resolve()).encode()).hexdigest()[:16]
    return root / "work" / f"checkout-{digest}.lock"


def holder(root: Path, checkout: Path) -> int | None:
    """The task whose run holds `checkout` now, or None when no run does. Looked
    at with a shared lock taken and let go at once: a run that starts in that
    instant waits one poll."""
    path = checkout_lock(root, checkout)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - not on the platforms locks runs on
        return None
    if not path.is_file():
        return None
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            held = fh.read().strip()
            return int(held) if held.isdigit() else None
        fcntl.flock(fh, fcntl.LOCK_UN)
    return None


@contextlib.contextmanager
def in_place(root: Path, checkout: Path, task_id: int, *, waiting: Callable[[str], None],
             check: Callable[[], None]) -> Iterator[None]:
    """Holds `checkout` for task `task_id` while the block runs. Another run there
    holds it: `waiting` is told who, once, and the run waits, calling `check`
    (which raises to stop waiting, a cancel) between looks. Without `fcntl`, a
    no-op: the platform has no lock to take."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - not on the platforms locks runs on
        yield
        return
    path = checkout_lock(root, checkout)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        told = False
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if not told:
                    fh.seek(0)
                    holder = fh.read().strip()
                    waiting(f"task #{holder}" if holder.isdigit() else "another run")
                    told = True
                check()
                time.sleep(WAIT_POLL_S)
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(task_id))
            fh.flush()
            yield
        finally:
            fh.seek(0)
            fh.truncate()
            fcntl.flock(fh, fcntl.LOCK_UN)


#: The files that make a folder a Docker Compose project.
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")


def has_compose(path: Path) -> bool:
    return any((path / name).is_file() for name in COMPOSE_FILES)


def stop_stacks(path: Path, *, runner: Any = subprocess.run) -> list[str]:
    """Stop the Compose project named after a worktree's folder (what `sail up` or `docker compose
    up` starts there), when one runs: a worker's stack must not outlive its task (issue #119).
    Best effort: no docker, no compose file or a timeout stops nothing. The project names stopped."""
    if not has_compose(path) or shutil.which("docker") is None:
        return []
    project = re.sub(r"[^a-z0-9_-]", "", path.name.lower())
    try:
        running = runner(["docker", "compose", "-p", project, "ps", "-q"], cwd=str(path), capture_output=True,
                         text=True, timeout=30, check=False)
        if running.returncode != 0 or not running.stdout.strip():
            return []
        runner(["docker", "compose", "-p", project, "down", "--remove-orphans"], cwd=str(path),
               capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [project]


#: Build output and installed dependencies a worktree may hold of its own (never linked from the
#: checkout): what a compiler or a package manager makes again from the files git keeps.
REBUILT_DIRS = frozenset({"target", "build", "dist", ".next", "node_modules", ".gradle", ".venv", ".turbo"})


def _size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def shed(root: Path, idle: Callable[[int], bool]) -> tuple[int, list[str]]:
    """Free the disk the worktrees of tasks no run holds keep in rebuilt folders
    (`REBUILT_DIRS`): a real folder at a worktree's top, ignored by git, never a link to the
    checkout's. Nothing git keeps is touched, so a resume loses no work and builds them again.
    `idle(task_id)` says a task no run holds and none is about to (not running, not queued).
    Returns the bytes freed and the folders removed; never raises."""
    freed, removed = 0, []
    for path in sorted((root / "worktrees").glob("task-*")):
        try:
            task_id = int(path.name.removeprefix("task-").split("-")[0])
        except ValueError:
            continue
        if not path.is_dir() or not idle(task_id):
            continue
        for name in sorted(REBUILT_DIRS):
            folder = path / name
            if folder.is_symlink() or not folder.is_dir():
                continue
            try:
                ignored = _run_git(["check-ignore", "-q", name], path)
            except IsolationError:
                continue  # a git that cannot say frees nothing here
            if ignored.returncode != 0:
                continue
            size = _size(folder)
            shutil.rmtree(folder, ignore_errors=True)
            if not folder.exists():
                freed += size
                removed.append(str(folder))
    return freed, removed
