"""Whether a queued task can start beside the work already going in its repository.

Each writing task gets its own git worktree, so two tasks never write into the
same checkout. What parallel work can still break is the *result*: a task that
needs another's change, or two that edit the same behaviour and leave branches
that do not merge. That is a question about meaning, not about files listed in
advance, so Haiku answers it — with a JSON schema and no tools, like the
classifier.

The decision is made once, at dispatch time, against what is running and what
was queued ahead of the task, and it is kept on the task with its reason. It
fails closed: a model that cannot be run, or does not answer, means the task
waits its turn. Waiting costs time; a wrong "parallel" costs a broken merge. A
wait the model gives needs a collision it can name: told to wait "when unsure",
it held back a third of the independent pairs in `bench/speed/dispatch.py`.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from locks import after, config, legacy
from locks.classify import ask_haiku
from locks.isolate import PATHLIKE
from locks.store import Store, home
from locks.text import fold

#: How much of each task's text the model reads: all of it when it fits, else its
#: opening and then the lines that carry a coordination fact, which a long text
#: (a workflow step's, a feature's spec) puts anywhere, often last.
BODY_CHARS = 1200
OPENING_CHARS = 400
#: The most one such line takes, cut around what made it one.
FACT_CHARS = 200
#: The most files a running task changed that the question names.
CHANGED_FILES = 40

#: A coordination fact, read on the folded text (`text.fold`: `migración` is
#: `migracion`), in two ranks: a migration or work beside it (`parallel`,
#: `paralelo`) says how the task takes its place among the others; a version or
#: another task by its number only may, since a spec mentions versions of
#: everything. A line naming a file (`isolate.PATHLIKE`) is one too, ranked last:
#: a long text names many files.
_DECLARES = re.compile(r"\bmigra|\bparal+el")
_MENTIONS = re.compile(r"\bversion|#\d+")
#: Where a task's text is cut into lines: its line breaks, and its sentences (a
#: clause after `;` stays with the sentence it belongs to).
_SEGMENT = re.compile(r"\n+|(?<=[.!?])\s+")

SYSTEM_PROMPT = (
    "You decide whether a new software task can start now, beside other tasks in the same repository, "
    "or must wait until they finish. Each task runs in its own copy of the repository and its changes "
    "are merged afterwards. Waiting costs the whole time the work ahead takes; starting beside work it "
    "collides with costs a merge conflict. Answer parallel=false only for a collision you can name: the "
    "new task needs something another task is building (an endpoint, a table, a field, a function it "
    "will call), or both change the same feature, file, function, schema or behaviour (a rename, a "
    "migration or a refactor across the code collides with whatever it touches), or the new task's "
    "results rest on behaviour another task changes (a rounding, a format, a rule its totals or tests "
    "depend on), as their texts, the surfaces they declared and the files a running task changed so far "
    "(read from git) show. Different features in different parts of the code are parallel=true even when "
    "neither declared its surfaces: a helper both might share, the same router, or the same test setup is "
    "a guess, not a collision. Reading-only tasks (questions, reviews, searches) can always run in "
    "parallel. What a task's text declares is a fact: a migration or version number it gives itself, "
    "while the other tasks take the other numbers, is no dependency on the tasks numbered before it. The "
    "reason is one short sentence: the collision and the task it is with, or why they do not meet."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "parallel": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["parallel", "reason"],
    "additionalProperties": False,
}


#: Tags the model leaked past its answer ("…as #55.</reason> </invoke>"), and a closing
#: tag cut off at the end. A bare `<` in prose ("a < b") is no tag and stays.
_MARKUP = re.compile(r"</?[A-Za-z][^<>]*>|</[^<>]*$")
REASON_CHARS = 200


@dataclass(frozen=True)
class Decision:
    parallel: bool
    reason: str
    cost_usd: float = 0.0


def declared_surfaces(task: Mapping[str, Any]) -> tuple[str, ...]:
    """The surfaces a task was queued with (`--surface`, a workflow step's)."""
    try:
        return tuple(json.loads(task.get("options") or "{}").get("surfaces") or ())
    except ValueError:
        return ()


#: A clause that tells what not to touch: the paths it names are no surface of the task.
_NEGATES = re.compile(r"\b(?:not|never|don'?t|without)\b", re.IGNORECASE)
_LEFT_ALONE = re.compile(r"\bleave\b[^,;]*\balone\b|\buntouched\b", re.IGNORECASE)
#: Where a clause ends. A dot inside a file name ("CHANGELOG.md") is no end.
_CLAUSE = re.compile(r"(?<=[.!?;])\s+|\n+")


def _negated(text: str, at: int) -> bool:
    """Whether the path at `at` sits in a sentence that negates it: a `not`, `never`, `do not`,
    `don't` or `without` before it, or `leave … alone` / `untouched` in the sentence."""
    start = max((m.end() for m in _CLAUSE.finditer(text, 0, at)), default=0)
    end = next((m.start() for m in _CLAUSE.finditer(text, at)), len(text))
    return bool(_NEGATES.search(text[start:at])
                or any(m.start() <= at - start < m.end() or m.group(0).lower() == "untouched"
                       for m in _LEFT_ALONE.finditer(text[start:end])))


def named_surfaces(task: Mapping[str, Any]) -> tuple[str, ...]:
    """The files and folders a task's own text names, as surfaces (a folder as `folder/**`):
    what it will likely change when it declared nothing. Only ever a reason to wait, never to
    run beside other work, and never enforced on the worker. A path in a clause that negates
    it ("do not edit X", "never touch Y") is not one."""
    from locks import points

    text = str(task.get("body") or task.get("title") or "")
    found = []
    for match in points._PATH.finditer(text):
        clean = match.group(0).strip("/")
        if clean and "*" not in clean and not _negated(text, match.start()):
            found.append(clean if "." in clean.rsplit("/", 1)[-1] else f"{clean}/**")
    return tuple(dict.fromkeys(found))


def _clip(line: str, at: int) -> str:
    """`line` in at most `FACT_CHARS`, keeping what sits at `at` and a little before it."""
    if len(line) <= FACT_CHARS:
        return line
    start = max(0, min(at - FACT_CHARS // 4, len(line) - FACT_CHARS + 2))
    end = start + FACT_CHARS - 2
    return ("…" if start else "") + line[start:end] + ("…" if end < len(line) else "")


def _excerpt(task: Mapping[str, Any]) -> tuple[str, list[str]]:
    """What the model reads of a task's text, whitespace collapsed: all of it when it
    fits in `BODY_CHARS`; else its opening, and the lines past it that carry a
    coordination fact (those declaring a migration or parallel work first, then those mentioning a
    version or another task, then those naming a file), in
    the text's order, as many as fit in the same bound."""
    raw = str(task.get("body") or task.get("title") or "")
    whole = " ".join(raw.split())
    if len(whole) <= BODY_CHARS:
        return whole, []
    opening = whole[:OPENING_CHARS - 1] + "…"
    found, offset = [], 0
    for n, line in enumerate(" ".join(part.split()) for part in _SEGMENT.split(raw) if part.strip()):
        offset += len(line) + 1
        if offset - 1 <= OPENING_CHARS - 1:
            continue  # read whole in the opening
        folded = fold(line)
        if hit := _DECLARES.search(folded):
            rank = 0
        elif hit := _MENTIONS.search(folded):
            rank = 1
        elif hit := PATHLIKE.search(line):
            rank = 2
        else:
            continue
        found.append((rank, n, _clip(line, hit.start())))
    room, kept = BODY_CHARS - len(opening), []
    for _, n, line in sorted(found):
        if len(line) + 3 <= room:
            room -= len(line) + 3
            kept.append((n, line))
    return opening, [line for _, line in sorted(kept)]


def _files(paths: Sequence[str]) -> str:
    if not paths:
        return "none yet"
    more = len(paths) - CHANGED_FILES
    return ", ".join(paths[:CHANGED_FILES]) + (f" and {more} more" if more > 0 else "")


def _described(task: Mapping[str, Any]) -> list[str]:
    opening, facts = _excerpt(task)
    lines = [opening, *(f"… {fact}" for fact in facts)]
    if declared := declared_surfaces(task):
        lines.append("Surfaces it declared: " + ", ".join(declared))
    if task.get("changed") is not None:
        lines.append("Files it changed so far (git): " + _files(task["changed"]))
    return lines


def question(task: Mapping[str, Any], ahead: Sequence[Mapping[str, Any]]) -> str:
    """The new task and the work ahead of it, each as `_excerpt` reads it, with the
    surfaces it declared and, for a running task whose worktree git could read
    (`changed`, which the dispatcher sets), the files it changed so far."""
    lines = [f"New task #{task['id']}:", *_described(task), "", "Already running or queued ahead of it:"]
    for t in ahead:
        opening, *rest = _described(t)
        lines += [f"- #{t['id']} ({t['status']}): {opening}", *(f"  {line}" for line in rest)]
    return "\n".join(lines)


def _plain(reason: object) -> str:
    """A reason as the board shows it: text on one line, with no markup the model leaked."""
    return " ".join(_MARKUP.sub(" ", str(reason or "")).split())[:REASON_CHARS].rstrip()


def decide(task: dict, ahead: Sequence[dict], **haiku_kwargs) -> Decision:
    """Never raises. Nothing ahead means nothing to conflict with, and no call."""
    if not ahead:
        return Decision(True, "nothing runs or waits ahead of it")
    out, cost, failure = ask_haiku(SYSTEM_PROMPT, SCHEMA, question(task, ahead), **haiku_kwargs)
    if out is None or not isinstance(out.get("parallel"), bool):
        return Decision(False, f"waits its turn: {failure or 'the model answered outside the schema'}", cost)
    reason = _plain(out.get("reason"))
    return Decision(out["parallel"], reason or ("independent" if out["parallel"] else "may conflict"), cost)


def _limit_holds(store: Store) -> str | None:
    """When the account's usage limit resets, while it holds the queue (`flow.limited_until`)."""
    from locks.stops import LIMIT_META

    at = store.meta(LIMIT_META)
    try:
        return at if at and datetime.fromisoformat(at).timestamp() > time.time() else None
    except ValueError:
        return None


def waiting(store: Store, task: Mapping) -> str | None:
    """What a queued task the dispatcher held back waits for now, in one line
    ("waits for #46: <reason>"); None for one it has not held back, or with
    nothing left ahead of it. The decision is made once, but the work it waits
    for moves on: it is read from the lane as `flow.runnable` holds the task,
    until nothing the dispatcher started runs there and nothing queued is ahead.
    Work queued after other work says that first ("waits for #17 to pass", `after`)."""
    if (held := after.waiting(store, task)) is not None:
        return held
    if task.get("status") == "queued" and (until := _limit_holds(store)):
        return f"waits for the account's usage limit to reset ({until})"
    if task.get("status") != "queued" or task.get("parallel") != 0:
        return None
    ahead = [t["id"] for t in store.dispatched_running(task.get("repo"))]
    ahead += [t["id"] for t in store.queued_in(task.get("repo")) if t["id"] < task["id"] and not after.held(t)]
    if not ahead:
        return None
    decided = (store.last_event(task["id"], "dispatch_decided") or {}).get("data") or {}
    reason = task.get("parallel_reason") or decided.get("reason") or "it may conflict with the work ahead"
    reason = reason.removeprefix("waits its turn: ")  # a model that did not answer: the line says it waits
    # The task the decision named, else one it was judged against, else the first now
    # ahead; whenever that is not the one the decision named, the reason says whom it
    # was about. A decision kept before the event existed names none.
    judged = decided.get("behind")
    behind = next((i for i in [judged, *(decided.get("ahead") or ())] if i in ahead), ahead[0])
    if judged and behind != judged:
        reason += f" (judged against #{judged})"
    return f"waits for #{behind}: {reason}"


# --- the dispatcher process -------------------------------------------------
#
# One `locks work` per repository at a time, held by a lock file. A task queued
# with `++` starts one if none holds the lock; a second one started in the same
# instant finds the lock taken and exits. This module stays light: the prompt
# hook imports it.

def lock_path(root: Path, scope: str) -> Path:
    digest = hashlib.sha256(scope.encode()).hexdigest()[:16]
    return root / "work" / f"{digest}.lock"


@contextlib.contextmanager
def hold(root: Path, scope: str) -> Iterator[bool]:
    """Yields True while this process is the dispatcher for `scope`, False if another one is."""
    path = lock_path(root, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def wake_path(root: Path, scope: str) -> Path:
    return lock_path(root, scope).with_suffix(".wake")


def wake(root: Path, scope: str) -> None:
    """Leave word that work was queued for `scope`, before asking whether a dispatcher runs:
    one that read its queue before this goes round again on its way out (`woken`), so work
    queued while the last dispatcher was leaving never waits for the next hook. Never raises."""
    with contextlib.suppress(OSError):
        path = wake_path(root, scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{time.time():.6f}")


def woken(root: Path, scope: str, since: float) -> bool:
    """Whether work was queued for `scope` at or after `since`: a dispatcher that last read
    its queue at `since` did not see it."""
    try:
        return float(wake_path(root, scope).read_text().strip() or 0) >= since
    except (OSError, ValueError):
        return False


def held(root: Path, scope: str) -> bool:
    """Whether a dispatcher runs for `scope` now."""
    with hold(root, scope) as mine:
        return not mine


def child_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of a process locks starts on its own: it can import locks,
    and it carries no session's identity — the work is not that session's turn."""
    out = dict(os.environ if env is None else env)
    src = str(Path(__file__).resolve().parents[1])
    out["PYTHONPATH"] = src + (os.pathsep + out["PYTHONPATH"] if out.get("PYTHONPATH") else "")
    out.pop("LOCKS_SESSION_ID", None)
    # Nor a worker's: a process started from inside one (its mod asks for endings) is locks'.
    out.pop("LOCKS_WORKER_TASK", None)
    out.pop("LOCKS_WORKER_ATTEMPT", None)
    return dict(legacy.mirror(out))


def kick(cwd: str | Path | None, key: str | None, env: Mapping[str, str]) -> str:
    """Start the repository's dispatcher for work just queued (the `autowork`
    setting), and say what happens next. A `++` and a `locks queue add` both
    come here: a task a session queued by hand otherwise waits for a `locks
    work` nobody runs, or one run in the session's background, which dies
    with the session."""
    if not cwd or not config.enabled("autowork", env):
        return "`locks work` runs the queue."
    root = home(dict(env))
    if start(Path(cwd), root, key or "*") or held(root, key or "*"):
        if config.enabled("parallel", env):
            return "A worker takes it now, beside the running ones if Haiku finds it independent, or after them."
        return "A worker takes it when the tasks ahead of it are done."
    return "Starting a worker failed; `locks work` runs the queue."


def reopened(task: Mapping, env: Mapping[str, str]) -> None:
    """After a person took a lane's stopping task off the board, start the queue
    behind it again: its dispatcher stopped when the lane paused. Never raises."""
    with contextlib.suppress(Exception):
        if task.get("cwd") and Path(str(task["cwd"])).is_dir():
            kick(str(task["cwd"]), task.get("repo"), env)


def start(repo_dir: Path, root: Path, scope: str, *, popen: Callable[..., object] = subprocess.Popen) -> bool:
    """Start a detached `locks work` for a repository, unless one already runs: then the word
    left first (`wake`) sends that one round again before it leaves. Never raises."""
    try:
        wake(root, scope)
        if held(root, scope):
            return False
        log = root / "work" / "dispatcher.log"
        with open(log, "a") as out:
            popen([sys.executable, "-m", "locks", "work", "--repo", str(repo_dir), "--detached"], cwd=str(repo_dir),
                  env=child_env(), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                  start_new_session=True)
        return True
    except OSError:
        return False


def start_lane(store: Store, root: Path, key: str | None, *,
               popen: Callable[..., object] | None = None) -> bool:
    """Start the dispatcher of a lane that has queued work, unless one holds it: the
    first queued task that may run (none held behind other work) says where, by its
    directory. False when nothing is queued with a directory to start in, or a
    dispatcher already runs. A restart leaves queued work with no `locks work` to run
    it; this is how a person's unpause or a resume starts one."""
    queued = [t for t in store.queued_in(key or None) if not after.held(t)]
    where = Path(queued[0]["cwd"]) if queued and queued[0]["cwd"] else None
    if where is None or not where.is_dir():
        return False
    return start(where, root, key or "*", **({"popen": popen} if popen is not None else {}))


#: A lane is revived at most this often: a hook runs at every turn's end.
REVIVE_EVERY_S = 60


def revive(store: Store, root: Path, env: Mapping[str, str], *,
           popen: Callable[..., object] | None = None, now: float | None = None) -> list[str | None]:
    """Start a dispatcher for every lane with queued work that may run and none to run it
    (the `autowork` setting): one that died (a full disk, a killed process) leaves its
    queue still until somebody types `locks work`. The hooks call it; never a GET. A lane
    paused on a person, or whose work is all held behind other work, is left alone, and
    each lane is tried at most every `REVIVE_EVERY_S`. The lanes it started a dispatcher for."""
    if not config.enabled("autowork", env) or held(root, "*"):
        return []
    now = time.time() if now is None else now
    revived: list[str | None] = []
    for lane in store.lanes():
        key = lane["repo"] or None
        if not lane["queued"] or lane["paused"] or held(root, key or "*"):
            continue
        stamp = lock_path(root, key or "*").with_suffix(".revived")
        with contextlib.suppress(OSError):
            if now - stamp.stat().st_mtime < REVIVE_EVERY_S:
                continue
        if start_lane(store, root, key, popen=popen):
            with contextlib.suppress(OSError):
                stamp.touch()
                os.utime(stamp, (now, now))
            revived.append(key)
    return revived


#: How many times a task that stopped for a mechanical cause is resumed on its own, from
#: the work it kept, before it is the orchestrator's to decide.
AUTO_RESUMES = 1
#: Stops the orchestrator would always continue the same way, so no session is woken for
#: them: a worker that died, work that outgrew its turns (resumed with more), a network that
#: stayed down through locks' waits, a machine under load through its waits. Each is locks'
#: own account (`stops`), never a judgment about the work.
MECHANICAL = ("died", "turns", "network", "load", "limit")
#: Only deaths this recent are resumed on their own: an upgrade never wakes old ones.
AUTO_RESUME_WITHIN_S = 6 * 3600
#: A death nobody could have seen (no worker started anywhere since: a machine or container that
#: restarted, a laptop closed) is resumed past `AUTO_RESUME_WITHIN_S`, up to this old.
AUTO_RESUME_ABSENT_S = 3 * 86400
#: Automatic resumes of one stop that never took hold (died before their first attempt, as one
#: started while the machine went down does) are not counted toward `AUTO_RESUMES`, up to this many.
AUTO_RESUME_TRIES = 3
#: A usage limit's resume waits this long past the reset its message named (a minute's word),
#: or `LIMIT_UNKNOWN_S` past the stop when it named none. A limit met again after its reset is
#: another wait, up to `LIMIT_RESUMES` of them, counted apart from the other causes' resume.
LIMIT_GRACE_S = 120
LIMIT_UNKNOWN_S = 3600
LIMIT_RESUMES = 3


def _due(stop: Any, ended_at: float) -> float:
    """When a stop may be resumed on its own: at once, or for a usage limit after its reset."""
    if stop.cause != "limit":
        return ended_at
    grace = float(os.environ.get("LOCKS_LIMIT_GRACE_S") or LIMIT_GRACE_S)  # a scenario's clock is seconds
    try:
        return datetime.fromisoformat(str(stop.extra["resets_at"])).timestamp() + grace
    except (KeyError, ValueError, TypeError):
        return ended_at + LIMIT_UNKNOWN_S


def _wake_at(store: Store, task_id: int, due: float, root: Path, env: Mapping[str, str],
             start: Callable[..., object]) -> None:
    """A detached `locks limit-wait` that sleeps until `due` and then resumes what the limit
    stopped, so nobody has to type after the reset; one per task and reset."""
    if any(abs(float((e["data"] or {}).get("until") or 0) - due) < 1
           for e in store.events_of(task_id, "limit_waiting")):
        return
    try:
        (root / "work").mkdir(parents=True, exist_ok=True)
        with open(root / "work" / "auto-resume.log", "a") as out:
            start([sys.executable, "-m", "locks", "limit-wait", "--until", str(int(due))], env=child_env(env),
                  stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError:
        return
    store.add_event(task_id, "limit_waiting", until=int(due))


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, ValueError):
        return True
    return True


#: How long a resume that was just started is left to take hold: it flips its task to running within
#: seconds, and until then a second `recover` (the dispatcher's, a waiter's, a turn's end) sees the
#: same stopped task and would start a second worker on it.
RESUME_STARTING_S = 120


def _behind_trunk(task: Mapping[str, Any], ended: Mapping[str, Any] | None) -> str | None:
    """How far the work an automatic resume goes on from is behind the branch work lands on
    (`main`), said on its `auto_resumed` event: the resume keeps its own work and is never rebased,
    so a trunk that moved meanwhile is the orchestrator's to know, never a guess of locks'. None when
    it did not move or git cannot say."""
    try:
        from locks import isolate, repo, survey

        top = repo.toplevel(Path(str(task["cwd"])))
        if top is None:
            return None
        tip = str(((ended or {}).get("data") or {}).get("branch") or f"locks/task-{task['id']}")
        trunk = survey.default_branch(top).split("/", 1)[-1]
        found = isolate.behind(top, tip, trunk)
        return isolate.behind_text(found) if found else None
    except Exception:
        return None


def _took_hold(store: Store, task_id: int, resumes: list[dict]) -> int:
    """How many of these automatic resumes reached an attempt (an `attempt_started` after them)."""
    starts = [int(e["id"]) for e in store.events_of(task_id, "attempt_started")]
    ids = [int(e["id"]) for e in resumes]
    return sum(any(a > r and all(not (r < o < a) for o in ids) for a in starts) for r in ids)


def _unseen(store: Store, since: float) -> bool:
    """Whether no worker started anywhere since `since`: nobody was there to see a death then."""
    when = datetime.fromtimestamp(since, UTC).isoformat()
    return not store.events_of_kinds(["attempt_started"], since=when)


def _claim_resume(store: Store, task_id: int, stop: Any, ended_id: int, behind: str | None = None) -> bool:
    """Record the resume of `task_id` before anything starts it, in one write transaction, so two
    `recover` calls at once (separate processes) never both start it; False when one was started
    after its ending (`ended_id`) in the last `RESUME_STARTING_S`. One that took hold and ended
    again is no obstacle; one that never took hold is tried again after that."""
    with store.immediate():
        for event in store.events_of(task_id, "auto_resumed"):
            if int(event["id"]) < ended_id:
                continue
            try:
                started = datetime.fromisoformat(str(event["ts"])).timestamp()
            except ValueError:
                continue
            if 0 <= time.time() - started < RESUME_STARTING_S:
                return False
        store.add_event(task_id, "auto_resumed", after=stop.reason, cause=stop.cause,
                        **({"behind": behind} if behind else {}))
        # Its stop is no ending to hand a session: the resume's own ending is.
        store.add_event(task_id, "reported", via="auto_resume")
    return True


def _start_resume(task: Mapping[str, Any], stop: Any, root: Path, env: Mapping[str, str],
                  start: Callable[..., object], store: Store | None = None) -> bool:
    """`locks resume --unattended` for a stopped task, detached, in the worktree it left, with more
    turns for one that outgrew them; False when it could not start. That leaves the claim: it is
    one of the tries, and the next one waits out `RESUME_STARTING_S`."""
    from locks import stops

    more = ["--max-turns", str(stops.RESUME_TURNS)] if stop.cause == "turns" else []
    try:
        (root / "work").mkdir(parents=True, exist_ok=True)
        # The task's own log, where the dispatcher's runs write too: a death reads its crash from where
        # this run began (`flow.died`).
        with open(root / "work" / f"task-{task['id']}.log", "a") as out:
            if store is not None:
                store.set_meta(f"log_from:{task['id']}", str(out.tell()))
            start([sys.executable, "-m", "locks", "resume", str(task["id"]), "--unattended", *more],
                  cwd=str(task["cwd"]), env=child_env(env), stdin=subprocess.DEVNULL, stdout=out,
                  stderr=subprocess.STDOUT, start_new_session=True)
    except OSError:
        return False
    return True


def recover(store: Store, root: Path, env: Mapping[str, str], *, popen: Callable[..., object] | None = None,
            is_alive: Callable[[int | None], bool] = _alive, now: float | None = None) -> list[int]:
    """Workers that died mid-attempt (a restart, a killed process) cost the time until somebody
    noticed, and then the time until somebody resumed them; the other mechanical stops cost a
    wake of the orchestrating session, which would only resume them. At a turn's end the hooks
    look: a running task whose process is gone is swept as `died` at once, and a task that
    stopped for a `MECHANICAL` cause within `AUTO_RESUME_WITHIN_S` is resumed on its own
    (`locks resume --unattended`, more turns for one that outgrew them, in the worktree it left,
    its work kept), at most `AUTO_RESUMES` times; past that it is the orchestrator's. With
    `autowork` off nothing starts. Never in a GET. The tasks resumed."""
    from locks import stops

    if env.get("LOCKS_WORKER_TASK"):
        return []  # a worker never starts locks work, nor resumes it (its mod asks for endings too)
    running = store.list_tasks(status=["running"], limit=500, source=("locks",))
    if any(t["pid"] and not is_alive(t["pid"]) for t in running):
        from locks import flow  # only when a worker is gone: it pulls in the orchestrator

        flow.sweep(store, is_alive=is_alive)
    if not config.enabled("autowork", env):
        return []
    now = time.time() if now is None else now
    start = popen or subprocess.Popen
    resumed: list[int] = []
    for task in store.list_tasks(status=["interrupted", "replan", "blocked"], limit=100, source=("locks",)):
        stop = stops.of(store, task)
        ended = store.final_event(task["id"])
        if stop is None or stop.cause not in MECHANICAL or not task.get("cwd") or not Path(task["cwd"]).is_dir():
            continue
        if stop.extra.get("repeated"):
            continue  # it died of the same crash as before: a resume repeats it, the orchestrator decides
        limit = stop.cause == "limit"
        tried = [e for e in store.events_of(task["id"], "auto_resumed")
                 if ((e["data"] or {}).get("cause") == "limit") == limit]
        held = _took_hold(store, task["id"], tried)
        if held >= (LIMIT_RESUMES if limit else AUTO_RESUMES) or len(tried) >= AUTO_RESUME_TRIES + held:
            continue
        try:
            due = _due(stop, datetime.fromisoformat(str((ended or {}).get("ts"))).timestamp())
        except ValueError:
            continue
        if now < due:
            # The limit has not reset: a waiter resumes it then, nobody has to type.
            _wake_at(store, int(task["id"]), due, root, env, start)
            continue
        if now - due > AUTO_RESUME_WITHIN_S and not (now - due <= AUTO_RESUME_ABSENT_S and _unseen(store, due)):
            continue
        if not _claim_resume(store, task["id"], stop, int((ended or {}).get("id") or 0), _behind_trunk(task, ended)):
            continue
        if _start_resume(task, stop, root, env, start, store):
            resumed.append(int(task["id"]))
    return resumed
