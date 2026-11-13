"""Work that is waiting: the queue, its lanes, the dispatcher and the sweep.

A queued task costs nothing until it runs, and it runs in a worker — never in
the orchestrating session's context. Each repository is one lane. The first
task of an idle lane starts; one queued behind running work that will not write
(a reading kind, or text that says it changes nothing) starts beside it with no
question; any other starts beside it only when Haiku judged it independent of
all the writing work running and queued ahead (`dispatch.decide`, kept on the
task with its reason, `parallel` setting) — otherwise it waits its turn. A
task that ends in anything but a pass pauses
the lane, so nothing new starts on a state it may have left broken — unless a
person cancelled it, which is their call on that task, not on the queue. A
person unpauses it.

How many run at once is no number fixed here: it is the machine's level
(`locks.capacity`), which adapts to the API and the machine and is shared between
the lanes by reserve and lending. Every dispatcher settles it, allots it and
claims what it starts under one lock.

Each dispatched task runs as its own `locks run-queued` process: its pid is its
own, so the sweep and `locks cancel` treat it like any other run, and the
dispatcher only watches.

The sweep is what keeps "running" honest. A `locks run` that was killed leaves
its task marked running forever; the sweep checks the process, and a task whose
driver is gone becomes interrupted and pauses its lane. It runs from hooks, the
dispatcher and the UI server — never from a page being open.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from locks import after, capacity, classify, config, dispatch, isolate, launch, lazy, stops, surfaces
from locks.matrix import READ_ONLY_KINDS, Cell
from locks.store import Store, home
from locks.text import clip

if TYPE_CHECKING:
    from locks.orchestrate import Options, Report

#: The run loop: read by the dispatcher's runs, never by a board or a sweep (`locks.lazy`).
orchestrate = lazy.module("locks.orchestrate")

#: How long a session prompt may sit in `running` with no update before it is
#: taken for abandoned. Claude Code fires no Stop for an interrupted turn.
SESSION_TASK_TIMEOUT = timedelta(hours=12)
#: A claimed task (`Store.claim`: running, dispatched, no pid) whose process never started is
#: put back in the queue after this many seconds (`sweep`).
CLAIM_STALE_S = 300
#: How long a session's subagent may sit in `running` before it is taken for one whose
#: ending never reached locks (a background agent's notice lost to a compaction or a
#: closed session). Longer than a turn: a subagent may work for hours.
DELEGATION_TIMEOUT = timedelta(hours=24)
DEFAULT_MAX_TASKS = 10
#: How often a dispatcher with work out checks on it and on new arrivals.
POLL_S = 2.0
#: Its waits, one after another, while the disk is full: it keeps its lock and the work it
#: has out, and goes on when the store takes writes again (the last wait repeats).
DISK_WAITS_S = (5, 15, 30, 60, 120, 300)
#: How long it waits for space in all before it stops; a hook starts a fresh one later
#: (`dispatch.revive`).
DISK_GIVE_UP_S = 3600

Runner = Callable[..., "Report"]


def disk_full(exc: BaseException) -> bool:
    """Whether an error is the disk running out: SQLite's "database or disk is full" or
    "disk I/O error", or an OS write refused for space. Any other error is locks' own."""
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        said = str(exc).lower()
        return "disk is full" in said or "disk i/o error" in said
    return False


def shed(store: Store) -> int:
    """Free the rebuilt folders (build output, installed dependencies) the worktrees of tasks no
    run holds keep (`isolate.shed`): a disk that filled with them killed a worker mid-task.
    Each task's are said in a `shed` event; returns the bytes freed, never raises."""
    from locks import isolate

    def idle(task_id: int) -> bool:
        task = store.get_task(task_id)
        return task is not None and task["status"] not in ("running", "queued")

    try:
        freed, removed = isolate.shed(home(), idle)
    except Exception:
        return 0
    by_task: dict[int, list[str]] = {}
    for folder in removed:
        name = Path(folder).parent.name.removeprefix("task-").split("-")[0]
        by_task.setdefault(int(name), []).append(Path(folder).name)
    for task_id, names in by_task.items():
        with contextlib.suppress(Exception):
            store.add_event(task_id, "shed", folders=names)
    return freed


#: A running task is slow once it has run longer than this and than 9 in 10 of the passes of
#: its kind in its repository, over at least `SLOW_SAMPLES` of them.
SLOW_MIN_S = 20 * 60
SLOW_SAMPLES = 5


def slow(store: Store, task: Mapping[str, Any], now: float | None = None,
         seen: dict[tuple[str, str], list[float]] | None = None) -> str | None:
    """Why a running task is slow, or None: it has run longer than `SLOW_MIN_S` and than the
    90th percentile of the wall time its kind's passes took here (`finished` events' `wall_s`).
    Said, never acted on: nothing is stopped. `seen` keeps each repository and kind's times
    for a caller that asks of several tasks."""
    if task.get("status") != "running" or not task.get("kind"):
        return None
    planned = store.last_event(int(task["id"]), "planned")
    try:
        began = datetime.fromisoformat(str(planned["ts"])).timestamp() if planned else None
    except ValueError:
        began = None
    if began is None or (elapsed := (time.time() if now is None else now) - began) < SLOW_MIN_S:
        return None
    key = (str(task.get("repo") or ""), str(task["kind"]))
    walls = (seen or {}).get(key)
    if walls is None:
        walls = sorted(float(w) for t in store.list_tasks(repo=task.get("repo"), status=["done"], limit=200,
                                                           source=("locks", "queue"))
                       if t["kind"] == task["kind"] and t["id"] != task["id"]
                       and (w := (((store.final_event(t["id"]) or {}).get("data")) or {}).get("wall_s")))
        if seen is not None:
            seen[key] = walls
    if len(walls) < SLOW_SAMPLES:
        return None
    p90 = walls[min(len(walls) - 1, int(0.9 * len(walls)))]
    if elapsed <= p90:
        return None
    return (f"running {round(elapsed / 60)}m: longer than 9 in 10 of the {len(walls)} {task['kind']} tasks that "
            f"passed here (p90 {round(p90 / 60)}m); `locks show {task['id']}` says what it is doing")


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it is someone else's
    return True


#: The store's meta key, with a task id after it, of `<pid>:<start time>` of the process driving it.
PID_START = "pid-start:"


def start_time(pid: int | None) -> str | None:
    """When a process started, as the kernel counts it (Linux: field 22 of `/proc/<pid>/stat`, in
    clock ticks since boot); None where the platform does not say it cheaply or the process is gone.
    Two processes that had one pid at different times never share both the pid and this."""
    try:
        raw = Path(f"/proc/{int(pid or 0)}/stat").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, OverflowError):
        return None
    return _stat_start(raw)


def _stat_start(raw: str) -> str | None:
    """Field 22 of a `/proc/<pid>/stat` line. The command name (field 2) may hold spaces and
    parentheses, so the fields count from its last ")"."""
    fields = raw.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 and fields[19].isdigit() else None


def remember_start(store: Store, task_id: int, pid: int | None,
                   reader: Callable[[int | None], str | None] = start_time) -> None:
    """Keep when the process of a task started beside its pid (a meta key, no schema), so a pid the
    system gives to another process later is told from the worker (`recycled`). Nothing is kept
    where the start time cannot be read."""
    if (began := reader(pid)) is not None:
        store.set_meta(PID_START + str(task_id), f"{pid}:{began}")


def recycled(store: Store, task: Mapping[str, Any],
             reader: Callable[[int | None], str | None] = start_time) -> bool:
    """Whether the process now holding a task's pid started at another time than the one kept for
    that pid: after a reboot or a pid wrap an unrelated process answers to it. False when nothing
    was kept for this pid or the start time cannot be read now: then the pid decides, as it always did."""
    kept = store.meta(PID_START + str(task["id"]))
    pid, _, began = (kept or "").partition(":")
    if not began or pid != str(task.get("pid")):
        return False
    now = reader(task.get("pid"))
    return now is not None and now != began


def _lost_cost(store: Store, task: dict) -> None:
    """What an attempt whose worker died spent, read from its worker's transcript
    (`usage.running_cost`) and kept as an `attempt_lost` event and on the task's cost: a worker
    that never wrote its result block still spent money, and a cost nobody read is never $0.
    Unreadable is said as such (`cost_usd` None). Never fails the sweep."""
    try:
        from locks import usage

        started = store.last_event(task["id"], "attempt_started")
        ended = store.last_event(task["id"], "attempt_finished")
        if started is None or (ended is not None and ended["id"] > started["id"]):
            return
        seq = (started.get("data") or {}).get("seq")
        cost = usage.running_cost(task["id"], seq, since=started["ts"])
        _keep_lost_transcript(task["id"], seq)
        # Its session, read while today's log still holds its calls: the resume goes on in it
        # (`orchestrate._dead_session`).
        from locks import phase

        session = phase.session_of(task["id"], seq if isinstance(seq, int) else None)
        store.add_event(task["id"], "attempt_lost", seq=seq, cell=(started.get("data") or {}).get("cell"),
                        **({"session": session} if session else {}),
                        cost_usd=round(cost, 4) if cost is not None else None)
        if cost:
            store.update_task(task["id"], cost_usd=round(float(task.get("cost_usd") or 0) + cost, 4))
    except Exception:  # noqa: S110 - a death is recorded whatever its cost says
        pass


def _keep_lost_transcript(task_id: int, seq: object, env: Mapping[str, str] | None = None) -> None:
    """With the person's trace on, a dead worker's transcript is kept beside the traces as a
    finished one's is (`launch._keep_worker_transcript`): it died before its attempt ended, which
    is the one place that kept it, so what it read and called was nowhere. Never fails the sweep."""
    try:
        from locks import phase, trace, usage

        env = os.environ if env is None else env
        if not trace.enabled(env):
            return
        session = phase.session_of(task_id, seq if isinstance(seq, int) else None, env)
        source = usage.transcript_of(session, env) if session else None
        if source is not None:
            trace.keep_transcript({"transcript_path": str(source), "session_id": session}, env)
    except Exception:  # noqa: S110 - a trace never fails what it watches
        pass


def sweep(store: Store, *, now: datetime | None = None, is_alive: Callable[[int | None], bool] = alive,
          started: Callable[[int | None], str | None] = start_time) -> list[int]:
    """Mark running tasks with no sign of life as interrupted. Returns their ids. A pid that answers
    but started at another time than the worker did (`recycled`) is no sign of life either; a pid
    seen alive with no start time kept yet has it kept now (`started` reads it)."""
    now = now or datetime.now(UTC)
    swept = []
    for task in store.list_tasks(status=["running"], limit=500):
        if task["dispatched"] and not task["pid"] and task["source"] != "locks":
            # Claimed by a dispatcher that never started its process (it was killed, or `start` raised
            # past the handler in `work`): nobody holds it, and it is no dead worker to resume.
            sat = now - datetime.fromisoformat(task["updated_at"])
            if sat.total_seconds() > CLAIM_STALE_S:
                store.update_task(task["id"], status="queued", dispatched=0)
                store.add_event(task["id"], "claim_released", sat_s=int(sat.total_seconds()))
            continue
        reused = False
        if task["source"] == "locks":
            dead = not is_alive(task["pid"])
            if not dead and task["pid"]:
                if reused := recycled(store, task, started):
                    dead = True
                elif not (store.meta(PID_START + str(task["id"])) or "").startswith(f"{task['pid']}:"):
                    # First sighting of this pid: a run the dispatcher did not start (`_Process` keeps its own).
                    remember_start(store, task["id"], task["pid"], started)
        else:
            updated = datetime.fromisoformat(task["updated_at"])
            dead = now - updated > (DELEGATION_TIMEOUT if task["source"] == "delegation" else SESSION_TASK_TIMEOUT)
        if not dead:
            continue
        gone = (f"no sign of life: its process ({task['pid']}) is gone: the pid belongs to another process now"
                if reused else
                f"no sign of life: its process ({task['pid']}) is gone" if task["source"] == "locks" and task["pid"]
                else "no sign of life")
        # A session's turn is no work to resume: the session closed or was cut off
        # before its answer arrived. It keeps the answer it had so far as its result.
        # A subagent is the session's own work, and nothing locks can resume: the same.
        stop = (stops.Stop("turn", "the session's turn ended without its answer reaching locks")
                if task["source"] == "hook" else
                stops.Stop("turn", "the session's subagent ended without its ending reaching locks")
                if task["source"] == "delegation" else died(store, task["id"], gone))
        if task["source"] == "locks":
            _lost_cost(store, task)
        stops.record(store, task["id"], stop, pid=None, current_cell=None, result=task["source"] == "locks")
        if task["dispatched"]:
            store.pause_lane(task["repo"], f"task #{task['id']} stopped with no sign of life")
        swept.append(task["id"])
    return swept


#: Statuses whose branch waits on nothing: no resume continues from it.
PRUNE_STATUSES = frozenset({"done", "dismissed", "cancelled"})


def prune(store: Store, dirs: list[Path] | None = None, *, dry_run: bool = False) -> list[dict]:
    """Delete the branches locks kept whose work landed, in each checkout its
    tasks ran in (or in `dirs`): merged, squash-merged, or tool artifacts only;
    see `isolate.prunable`. Each is recorded on its task with its tip, so
    `git branch <name> <tip>` brings it back. Off with `locks config prune off`."""
    from locks import config, isolate, repo

    if not dry_run and not config.enabled("prune", os.environ):
        return []
    if dirs is None:
        seen: dict[str, Path] = {}
        for task in store.list_tasks(status=sorted(PRUNE_STATUSES), source=("locks", "queue"), limit=2000):
            where = Path(task["cwd"]) if task.get("cwd") else None
            top = repo.toplevel(where) if where is not None and where.is_dir() else None
            if top is not None:
                seen.setdefault(str(top), top)
        dirs = list(seen.values())
    pruned = []
    for top in dirs:
        kept = isolate.kept_branches(top)
        trees = {task_id: isolate.tree_of(top, tip) for task_id, tip in kept.items()}
        for task_id, tip in sorted(kept.items()):
            task = store.get_task(task_id)
            if task is None or task["status"] not in PRUNE_STATUSES:
                continue
            run = store.get_run(int(task["workflow_run"])) if task.get("workflow_run") else None
            if run is not None and run["status"] == "active":
                # A later step of its run may still start from this branch, or join it.
                continue
            branch = isolate.branch_for(task_id)
            why = isolate.prunable(top, branch)
            if why is None:
                # Another kept branch holds the very same work: this copy loses nothing. Only a
                # twin whose task passed counts: one that may resume may still fail and drop it.
                twin = next((other for other in sorted(trees) if other != task_id and trees[other]
                             and trees[other] == trees[task_id]
                             and (store.get_task(other) or {}).get("status") == "done"), None)
                why = f"the same work as {isolate.branch_for(twin)}" if twin is not None else None
            if why is None or (not dry_run and not isolate.delete_branch(top, branch)):
                continue
            trees.pop(task_id, None)
            if not dry_run:
                store.add_event(task_id, "branch_pruned", branch=branch, tip=tip, why=why, repo=str(top))
            pruned.append({"task": task_id, "branch": branch, "tip": tip, "why": why, "repo": str(top)})
    return pruned


@dataclass(frozen=True)
class Ran:
    task_id: int
    status: str


@dataclass
class WorkReport:
    ran: list[Ran] = field(default_factory=list)
    stopped_because: str = ""
    # It stopped on `max_tasks` with work still queued in its lane: a detached
    # dispatcher hands that work to a fresh one (`cli.cmd_work`), never leaves it.
    at_limit: bool = False
    #: When it last read its queue: work queued after this, while its lock was still held, went
    #: unseen, and `cli.cmd_work` starts a fresh dispatcher for it once the lock is let go.
    looked_at: float = 0.0

    def text(self) -> str:
        lines = [f"task #{r.task_id}: {r.status}" for r in self.ran]
        lines.append(self.stopped_because)
        return "\n".join(lines)


def queued_options(task: dict, defaults: Options) -> Options:
    """The options a queued or resumed task runs with. Keys an older locks stored
    and nothing reads any more (a dollar `budget_usd`) are left where they are."""
    raw = json.loads(task.get("options") or "{}")
    return orchestrate.Options(
        max_attempts=int(raw.get("max_attempts", defaults.max_attempts)),
        verify=raw.get("verify", defaults.verify),
        kind=raw.get("kind", defaults.kind),
        start=Cell.parse(raw["start"]) if raw.get("start") else defaults.start,
        allow_approval=bool(raw.get("allow_approval", defaults.allow_approval)),
        livespec=raw.get("livespec", defaults.livespec),
        use_model_classifier=defaults.use_model_classifier,
        allow_tools=tuple(raw.get("allow_tools") or defaults.allow_tools),
        isolate=defaults.isolate and not raw.get("no_isolate"),
        max_turns=int(raw["max_turns"]) if raw.get("max_turns") else defaults.max_turns,
        base=raw.get("base") or defaults.base,
        role=raw.get("role") or defaults.role,
        step_role=raw.get("step_role") or defaults.step_role,
        surfaces=tuple(raw.get("surfaces") or defaults.surfaces),
        ledger=raw.get("ledger") or defaults.ledger,
        keep_learned=bool(raw.get("keep_learned", defaults.keep_learned)),
        carry=bool(raw.get("carry", defaults.carry)),
        add_dirs=tuple(raw.get("add_dirs") or defaults.add_dirs),
        services=tuple(raw.get("services") or defaults.services),
        run_verify=raw.get("run_verify") or defaults.run_verify,
        parts=bool(raw.get("parts", defaults.parts)),
        integrate=raw.get("integrate") or defaults.integrate,
        touches=tuple(raw.get("touches") or defaults.touches),
        reviewed_next=bool(raw.get("reviewed_next")) or defaults.reviewed_next,
        whole=bool(raw.get("whole")) or defaults.whole,
        loop=raw.get("loop") or defaults.loop,
        trunk_for=bool(raw.get("trunk_for")) or defaults.trunk_for,
        trunk=raw.get("trunk") or defaults.trunk,
    )


Decide = Callable[[dict, list[dict]], dispatch.Decision]


def by_surfaces(task: dict, ahead: list[dict]) -> dispatch.Decision | None:
    """The call surfaces settle without a model. The task waits when the surfaces it
    declared may touch those another task declared, or hold a file a running task
    already changed (`changed`, from git), whatever the others declare; it starts
    beside the work ahead when every task involved declares surfaces and they cannot
    touch. None otherwise: then the dispatcher's judgment decides."""
    mine = dispatch.declared_surfaces(task)
    theirs = [(t, dispatch.declared_surfaces(t)) for t in ahead]
    for t, declared in theirs if mine else ():
        if declared and not surfaces.disjoint(mine, declared):
            return dispatch.Decision(False, f"its surfaces may touch those of #{t['id']}")
        touched = next((p for p in t.get("changed") or () if surfaces.allows(mine, p)), None)
        if touched is not None:
            return dispatch.Decision(False, f"#{t['id']} already changed {touched}, inside its surfaces")
    if not mine or not theirs or any(not s for _, s in theirs):
        return _by_names(task, mine, ahead)
    return dispatch.Decision(True, "its surfaces cannot touch those of the work ahead")


def _by_names(task: dict, mine: tuple[str, ...], ahead: list[dict]) -> dispatch.Decision | None:
    """Where surfaces were not declared, the files the requests name: a task waits when what
    it names may touch what work ahead declares or names, or a file a running task already
    changed. Never a reason to start beside it: in doubt, the dispatcher's judgment decides."""
    named = mine or dispatch.named_surfaces(task)
    if not named:
        return None
    for t in ahead:
        theirs = dispatch.declared_surfaces(t) or dispatch.named_surfaces(t)
        if theirs and dispatch.names_touch(named, theirs):
            return dispatch.Decision(False, f"the files it names may be the ones #{t['id']} changes "
                                            f"({', '.join(named[:3])}): it waits rather than both change them")
        touched = next((p for p in t.get("changed") or () if surfaces.allows(named, p)), None)
        if touched is not None:
            return dispatch.Decision(False, f"#{t['id']} already changed {touched}, which it names")
    return None


def chain(store: Store, task: dict, where: Path | None) -> str | None:
    """A task the dispatcher held behind other work for the files it names or declares, started
    now that the work passed: when that work changed one of those files and its kept branch is
    not merged yet, the task starts on that branch, as work queued `--after` it would, instead of
    on a base without the change it waited for (two branches changing one file, a conflict for
    the person to merge). Its `base` is set and a `chained` event says why; None, and the base as
    before, in every other case: the work did not pass, changed none of those files, kept no
    branch here, its late check is still to come, or the task has a start already (a workflow
    step, a release, a resume). Git is read here, so the caller holds no lock."""
    try:
        options = json.loads(task.get("options") or "{}")
        if (options.get("base") or options.get(after.KEY) or task.get("workflow_run")
                or store.attempts(task["id"]) or store.last_event(task["id"], "chained")
                or store.last_event(task["id"], "released")):
            return None
        waits = [e["data"] for e in store.events_of(task["id"], "dispatch_decided")
                 if not e["data"].get("parallel") and e["data"].get("by") == "surfaces" and e["data"].get("behind")]
        if not waits:
            return None
        ahead = store.get_task(int(waits[-1]["behind"]))
        if ahead is None or ahead["status"] != "done":
            return None
        deferred, read = store.last_event(ahead["id"], "verifier_deferred"), store.last_event(ahead["id"], "verified")
        if deferred is not None and (read is None or read["id"] < deferred["id"]):
            return None  # its independent check is still to come: it may yet be reopened
        commit, branch, paths = after.kept_start(store, ahead["id"], where)
        mine = dispatch.declared_surfaces(task) or dispatch.named_surfaces(task)
        shared = [p for p in paths if surfaces.allows(mine, p) or dispatch.names_touch(mine, [p])]
        if not commit or not shared:
            return None
        why = (f"waited for #{ahead['id']}, which passed and changed {', '.join(shared[:5])}"
               + (f" and {len(shared) - 5} more" if len(shared) > 5 else "")
               + f", which this task names: this starts on {branch} ({commit[:12]}), the branch it kept, so "
               "it builds on that change; nothing is merged or rebased")
        options["base"] = commit
        task["options"] = json.dumps(options)
        store.update_task(task["id"], options=task["options"])
        store.add_event(task["id"], "chained", after={"task": ahead["id"]}, base=commit, branch=branch,
                        files=shared[:20], why=why)
        return why
    except Exception:  # where it starts never stops a task from starting
        return None


def with_changes(store: Store, task: dict) -> dict:
    """A running task with the files git shows it changed so far in its worktree, as
    `changed`; as it is when git cannot say (no worktree, no attempt started yet)."""
    if not task.get("cwd"):
        return task
    started = store.last_event(task["id"], "attempt_started")
    files = isolate.changed_so_far(home(), Path(task["cwd"]), task["id"], started["data"] if started else {})
    return task if files is None else {**task, "changed": list(files)}


def _behind(decision: dispatch.Decision, ahead: list[dict]) -> int | None:
    """The task a held-back one waits for: the one its reason names, else the first ahead of it."""
    if decision.parallel or not ahead:
        return None
    ids = [t["id"] for t in ahead]
    return next((int(n) for n in re.findall(r"#(\d+)", decision.reason) if int(n) in ids), ids[0])


#: Why a task that changes nothing starts beside writing work with no question asked.
READS_ONLY = "reads only: runs beside writing work"
#: Why a task that writes starts beside work that does not.
NOTHING_WRITES_AHEAD = "nothing that writes runs or waits ahead of it"


def reads_only(task: dict) -> bool:
    """Whether a task will not write: its kind is a reading one, or its own words
    say it changes nothing (the rule `orchestrate.run` applies once it has a kind).
    A task with no kind yet gets the one the run would give it without the model:
    a text no rule reads, or one that asks for a change, is a build and writes."""
    try:
        chosen = json.loads(task.get("options") or "{}").get("kind") or task.get("kind")
    except ValueError:
        chosen = task.get("kind")
    text = task.get("body") or task.get("title") or ""
    kind = chosen or classify.classify(text, use_model=False).kind
    return kind in READ_ONLY_KINDS or orchestrate.declares_read_only(kind, text)


@dataclass
class Undecided:
    """A queued task that needs the model's call, read under the lock and decided outside it:
    the writing work it is judged against (git's `changed` read), and why it is judged now."""
    task: dict
    writing: list[dict]
    again: list[int] = field(default_factory=list)
    moved: list[int] = field(default_factory=list)


def _judged(store: Store, task: dict, ahead: list[dict], decide: Decide,
            read_changes: Callable[[], None], defer: list[Undecided] | None = None) -> dict | None:
    """The queued task with its dispatch decision made. A task that will not write
    is never put to the model, and a writing one is judged against the writing
    work ahead of it alone: a reader changes no file and holds no checkout, and the
    goal its text carries is not what it does. With no writing work ahead it starts,
    whatever a "no" given against work that has ended once held it behind.
    `read_changes` puts what git shows the running tasks changed into `ahead`,
    called only when a task is put to a decision. With `defer`, a task that needs the
    model is added to it instead (`Undecided`) and None is returned: the caller asks
    outside the lock the model call would hold."""
    if reads_only(task):
        if not task["parallel"]:  # a "no" given before this rule held a reader behind a build
            store.set_parallel(task["id"], True, READS_ONLY)
        return {**task, "parallel": 1}
    writing = [t for t in ahead if not reads_only(t)]
    if not writing:
        if not task["parallel"]:
            store.set_parallel(task["id"], True, NOTHING_WRITES_AHEAD)
        return {**task, "parallel": 1}
    stale = _judged_against_gone(store, task, writing)
    moved: list[int] = []
    if task["parallel"] is not None and not stale:
        if not _may_have_moved(store, task):
            return task
        read_changes()
        writing = [t for t in ahead if not reads_only(t)]
        if not (moved := _moved_since_judged(store, task, writing)):
            return task
    read_changes()
    writing = [t for t in ahead if not reads_only(t)]
    settled = by_surfaces(task, writing)
    if settled is None and defer is not None:
        defer.append(Undecided(task, writing, stale, moved))
        return None
    decision = settled or decide(task, writing)
    _record(store, task, writing, decision, by="surfaces" if settled else "haiku", again=stale, moved=moved)
    return {**task, "parallel": int(decision.parallel)}


def _record(store: Store, task: dict, writing: list[dict], decision: dispatch.Decision, *, by: str,
            again: list[int], moved: list[int]) -> None:
    """A dispatch decision kept on its task, with the work it was made against."""
    store.set_parallel(task["id"], decision.parallel, decision.reason)
    store.add_event(task["id"], "dispatch_decided", parallel=decision.parallel,
                    reason=decision.reason, behind=_behind(decision, writing),
                    ahead=[t["id"] for t in writing], by=by,
                    seen={str(t["id"]): sorted(t["changed"]) for t in writing if t.get("changed") is not None},
                    **({"again": again} if again else {"moved": moved} if moved else {}))
    if decision.cost_usd:
        store.update_task(task["id"], cost_usd=(task["cost_usd"] or 0) + decision.cost_usd)


#: How many times a pass under the lock hands tasks to the model outside it and looks again before
#: it leaves the rest for the dispatcher's next pass (work that keeps moving under the judgments).
DECIDE_ROUNDS = 4


def decide_outside(store: Store, undecided: list[Undecided], decide: Decide) -> None:
    """Ask `decide` for each task read under the lock and keep its answer on the task. No lock of the
    machine's is held here: the call takes seconds and every other dispatcher waits on that lock.
    A task that is no longer queued (another dispatcher took it, a person cancelled it) keeps none.
    The pass that follows reads the answers under the lock again, with the rules that make a kept
    decision stale (`_judged_against_gone`, `_moved_since_judged`)."""
    for u in undecided:
        decision = decide(u.task, u.writing)
        current = store.get_task(u.task["id"])
        if current is None or current["status"] != "queued":
            continue
        _record(store, u.task, u.writing, decision, by="haiku", again=u.again, moved=u.moved)


def _judged_against_gone(store: Store, task: dict, writing: list[dict]) -> list[int]:
    """The work a "wait" was judged against, when none of it is ahead any more: it ended, so
    the decision says nothing of the work ahead now and is made again. A wait kept before
    the decision named its work ahead stands. [] when it is not stale."""
    if task["parallel"] != 0:
        return []
    decided = (store.last_event(task["id"], "dispatch_decided") or {}).get("data") or {}
    judged = [int(i) for i in decided.get("ahead") or () if isinstance(i, int)]
    now = {int(t["id"]) for t in writing}
    return judged if judged and not now.intersection(judged) else []


#: How long a "wait" the dispatcher's judgment gave stands before the work it waits for is
#: read again: what that work changed since may show the two never meet.
REJUDGE_S = 120.0


def _may_have_moved(store: Store, task: dict) -> bool:
    """Whether a kept "wait" is worth reading git for: the dispatcher's judgment gave it (one
    surfaces or names gave is a fact, and stands), at least `REJUDGE_S` ago."""
    if task["parallel"] != 0:
        return False
    decided = store.last_event(task["id"], "dispatch_decided")
    if not decided or (decided.get("data") or {}).get("by") != "haiku":
        return False
    try:
        at = datetime.fromisoformat(decided["ts"])
    except (TypeError, ValueError):
        return False
    return (datetime.now(UTC) - (at if at.tzinfo else at.replace(tzinfo=UTC))).total_seconds() >= REJUDGE_S


def _moved_since_judged(store: Store, task: dict, writing: list[dict]) -> list[int]:
    """The running work a "wait" was judged against that changed files it had not when it was
    judged (git, `changed`): the judgment read less than there is now, often nothing at all
    when that work had just started, and is made again. [] when nothing moved."""
    decided = (store.last_event(task["id"], "dispatch_decided") or {}).get("data") or {}
    seen = decided.get("seen") or {}
    judged = {int(i) for i in decided.get("ahead") or () if isinstance(i, int)}
    return [t["id"] for t in writing if t["id"] in judged and t.get("changed") is not None
            and set(t["changed"]) - set(seen.get(str(t["id"])) or ())]


def _walk(store: Store, repo: str | None, *, parallel: bool, decide: Decide,
          defer: list[Undecided] | None = None) -> Iterator[dict]:
    """A lane's tasks that may start now, in queue order, each judged only when it is
    reached: the first when nothing runs; after that, tasks that will not write, and
    tasks judged independent of the writing work running and queued ahead. Work held
    behind other work (`after`) is never picked, and nothing waits behind it. With `defer`, the walk
    ends at the first task that needs the model (`_judged`): it is judged outside the lock, and the
    tasks behind it are reached on the next pass."""
    running = store.dispatched_running(repo)
    ahead, mine, read = list(running), 0, False

    def read_changes() -> None:
        nonlocal read
        if not read:
            # Git is read only when a task is to be judged, and once a pass.
            ahead[:len(running)] = [with_changes(store, t) for t in running]
            read = True

    for task in store.queued_in(repo):
        if after.held(task):
            continue  # it waits for other work to pass, not for a place in the lane
        if running or mine:
            if not parallel:
                return
            judged = _judged(store, task, ahead, decide, read_changes, defer)
            if judged is None:
                return
            task = judged
            if not task["parallel"]:
                ahead.append(task)
                continue
        ahead.append(task)
        mine += 1
        yield task


def _demand(queued: list[dict], running: int, *, parallel: bool) -> int:
    """What another dispatcher's lane could start now, read without judging anything: its
    first task when nothing runs there, and beside running work every task that starts
    without a decision (it will not write) or that a decision already let start. One not
    judged yet is no demand: a worker kept for it would sit idle if it is judged to wait."""
    idle = running == 0
    if not parallel:
        return 1 if idle and queued else 0
    return sum(1 for n, t in enumerate(queued) if (idle and n == 0) or t.get("parallel") or reads_only(t))


def _lanes(store: Store) -> tuple[list[capacity.Lane], dict[str | None, list[dict]]]:
    """Every lane as the allotment reads it, and the queued work of each that may run
    (none held behind other work). A lane whose work all runs has no queue row, yet holds
    its workers: it is read too."""
    counts = store.dispatched_counts()
    lanes, queued = [], {}
    for lane in store.lanes():
        key = lane["repo"] or None
        queued[key] = [t for t in store.queued_in(key) if not after.held(t)] if lane["queued"] else []
        lanes.append(capacity.Lane(key, counts.get(key, 0), bool(queued[key]),
                                   queued[key][0]["id"] if queued[key] else 0, bool(lane["paused"])))
    listed = {x.repo for x in lanes}
    lanes += [capacity.Lane(key, n, False, 0) for key, n in counts.items() if key not in listed]
    return lanes, queued


def shares(store: Store, level: int) -> dict[str | None, capacity.Share]:
    """Each lane's share of the level as it stands: what it reserves and runs, and what it
    lends or borrows. Nothing is judged or started."""
    return capacity.allot(level, _lanes(store)[0], lambda key: False)


def limited_until(store: Store, *, now: datetime | None = None) -> str | None:
    """When the account's usage limit resets, while it has not (`stops.LIMIT_META`, kept by the
    run it stopped); None once it has, or when no limit was met."""
    at = store.meta(stops.LIMIT_META)
    try:
        return at if at and datetime.fromisoformat(at) > (now or datetime.now(UTC)) else None
    except ValueError:
        return None


def pick(store: Store, repo: str | None, *, parallel: bool, decide: Decide,
         level: int | None = None,
         defer: list[Undecided] | None = None) -> tuple[list[dict], dict[str | None, capacity.Share]]:
    """What may start now within the machine's level, and each lane's share of it
    (`capacity.allot`): reserves first, in the order each lane's oldest work was queued,
    then what is lent. The lanes of `repo` (every lane, with None) are walked and judged
    (`_walk`); another dispatcher's are read as they stand, never judged here. With `defer`, no
    task is put to `decide` here: the ones that need it are added to `defer` and wait for the
    caller, who asks outside the lock this runs under (`decide_outside`)."""
    level = level if level is not None else capacity.current(home()).value
    if limited_until(store):
        # The account's usage limit turns every session away until it resets: nothing starts,
        # and `locks limit-wait` starts the dispatchers again after it.
        return [], capacity.allot(level, _lanes(store)[0], lambda key: False)
    lanes, queued = _lanes(store)
    walks: dict[str | None, Iterator[dict]] = {}
    demand: dict[str | None, int] = {}
    for lane in lanes:
        if lane.waiting and not lane.paused:
            if repo is None or lane.repo == repo:
                walks[lane.repo] = _walk(store, lane.repo, parallel=parallel, decide=decide, defer=defer)
            else:
                demand[lane.repo] = _demand(queued[lane.repo], lane.running, parallel=parallel)
    picked: list[dict] = []

    def take(key: str | None) -> bool:
        if key in walks:
            task = next(walks[key], None)
            if task is None:
                del walks[key]
                return False
            picked.append(task)
            return True
        if demand.get(key, 0) > 0:
            demand[key] -= 1
            return True
        return False

    shares = capacity.allot(level, lanes, take)
    return picked, shares


def runnable(store: Store, repo: str | None, *, parallel: bool, decide: Decide,
             level: int | None = None) -> list[dict]:
    """What may start now (`pick`), within the machine's level: the one kept, unless given."""
    return pick(store, repo, parallel=parallel, decide=decide, level=level)[0]


class Handle(Protocol):
    task_id: int

    def poll(self) -> str | None:
        """The task's status once it ended, None while it runs."""


class _Done:
    """A run that already finished: the in-process runner the tests use."""

    def __init__(self, task_id: int, status: str) -> None:
        self.task_id, self.status = task_id, status

    def poll(self) -> str | None:
        return self.status


class _Process:
    """`locks run-queued <id>` as its own process."""

    def __init__(self, task: dict, where: Path, store: Store, defaults: Options) -> None:
        self.task_id, self.store = task["id"], store
        argv = [sys.executable, "-m", "locks", "run-queued", str(task["id"])]
        if not defaults.use_model_classifier:
            argv.append("--no-model")
        log = task_log(task["id"])
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as out:
            # Where this run's output starts in the log it appends to: a death reads its crash from there.
            self.log, self.log_from = log, out.tell()
            store.set_meta(LOG_FROM + str(task["id"]), str(self.log_from))
            self.proc = subprocess.Popen(argv, cwd=str(where), env=dispatch.child_env(),
                                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        # The run keeps this pid as its own (`orchestrate.run`): its start time is kept beside it.
        with contextlib.suppress(Exception):
            remember_start(store, task["id"], self.proc.pid)

    def poll(self) -> str | None:
        if self.proc.poll() is None:
            return None
        task = self.store.get_task(self.task_id)
        if task is None:
            return "failed"
        if task["status"] in ("running", "queued"):
            # It exited without saying how it ended: it died. Killed outright, the kernel's
            # out-of-memory killer is the likely hand, and the machine's level halves on it.
            killed = self.proc.returncode in launch.KILLED
            stop = died(self.store, self.task_id,
                        f"its process exited with {self.proc.returncode} without saying how the task ended"
                        + (" (killed: the kernel does so when memory runs out)" if killed else ""),
                        log=getattr(self, "log", None), start=getattr(self, "log_from", 0),
                        extra={"starved": "memory"} if killed else {})
            # What its dead attempt spent and the session it ran in, as the sweep keeps them: the
            # dispatcher sees most deaths first, and its resume goes on in that session.
            _lost_cost(self.store, task)
            stops.record(self.store, self.task_id, stop, pid=None, current_cell=None)
            self.store.pause_lane(task["repo"], f"task #{self.task_id}: its process exited unexpectedly")
            return "interrupted"
        return task["status"]


#: A Python exception's last line, as a traceback ends: `module.Error: what`.
_CRASH = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Fault))(?::\s*(.*))?$")


def crash_line(log: Path, start: int = 0) -> str:
    """The last exception line a run wrote to its log from `start` (its own output), clipped; "" for none."""
    try:
        with open(log, "rb") as fh:
            fh.seek(start)
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    found = [line.strip() for line in text.splitlines() if _CRASH.match(line.strip())]
    return clip(found[-1], 300, one_line=True) if found else ""


#: The store's meta key, with a task id after it, of where its latest run's output starts in its log.
LOG_FROM = "log_from:"


def task_log(task_id: int) -> Path:
    """The log every run of a task appends its output to (the dispatcher's and an automatic resume's)."""
    return home() / "work" / f"task-{task_id}.log"


def died(store: Store, task_id: int, reason: str, *, log: Path | None = None, start: int | None = None,
         extra: Mapping[str, Any] | None = None) -> stops.Stop:
    """The stop of a run that died: its reason with the crash its run wrote to its log, when it wrote
    one (from `start`, else where the store says its latest run began), and whether that crash is the
    one its last death named (`repeated`: locks' failure, which a resume repeats)."""
    log = log or task_log(task_id)
    if start is None:
        start = int(store.meta(LOG_FROM + str(task_id)) or -1)
    crash = crash_line(log, start) if start >= 0 else ""
    again = bool(crash) and crash == _last_crash(store, task_id)
    out = dict(extra or {})
    if crash:
        reason += f": {crash}"
        out.update(crash=crash, log=str(log), **({"repeated": True} if again else {}))
    if again:
        reason += " — the same crash as its last death: locks' failure, not the work's; resuming repeats it"
    return stops.Stop("died", reason, extra=out)


def _last_crash(store: Store, task_id: int) -> str:
    """The crash an earlier death of this task named, "" for none."""
    for event in reversed(store.events_of(task_id, "finished")):
        stop = event["data"].get("stop") or {}
        if stop.get("cause") == "died":
            return str(stop.get("crash") or "")
    return ""


Start = Callable[[dict, Path], Handle]


def _settle(store: Store) -> None:
    """What ended settles the standards and the evolution history (`locks.evolution`).
    Its failure is an event on task 0 and never stops the queue."""
    from locks import evolution

    try:
        evolution.settle(store)
    except Exception as exc:  # the dispatcher goes on; the error is kept, never silent
        store.add_event(0, "evolution_error", error=f"{type(exc).__name__}: {exc}"[:300])


#: The statuses a mechanical stop leaves (`dispatch.MECHANICAL`): only these may resume on their own.
_MAY_RESUME = ("interrupted", "replan", "blocked")


def _recover(store: Store) -> None:
    """A run of its own that ended badly may be a mechanical stop (a worker killed, out of
    turns): resumed here at once (`dispatch.recover`), not at some session's next turn,
    which may never come while work runs unattended. Its failure is an event on task 0."""
    from locks import dispatch

    try:
        dispatch.recover(store, home(), os.environ)
    except Exception as exc:  # the dispatcher goes on; the error is kept, never silent
        store.add_event(0, "recover_error", error=f"{type(exc).__name__}: {exc}"[:300])


def work(
    store: Store,
    *,
    repo: str | None = None,
    max_tasks: int = DEFAULT_MAX_TASKS,
    defaults: Options | None = None,
    runner: Runner | None = None,
    parallel: bool | None = None,
    decide: Decide | None = None,
    start: Start | None = None,
    pause: Callable[[], None] | None = None,
    machine: Callable[[], capacity.Machine] | None = None,
    backoff: Callable[[float], None] | None = None,
    **run_kwargs,
) -> WorkReport:
    """Run queued work until nothing is runnable and nothing is out, or
    `max_tasks` started — the bound on how long an unattended run goes on. Each
    pass settles the machine's level (`capacity.settle`, on `machine()`) first;
    a lane whose work waits only for a worker another lane holds keeps waiting.

    `runner` runs a task in this process, one after another (tests); without it,
    each task is its own process and several may run at once.

    A full disk does not end it (`disk_full`): it waits (`DISK_WAITS_S`, by `backoff`)
    with its work out and its lock held, tries the pass again, and once the store takes
    writes the level halves for it (`capacity.disk_filled`); after `DISK_GIVE_UP_S` it stops.
    """
    defaults = defaults or orchestrate.Options()
    parallel = config.enabled("parallel", os.environ) if parallel is None else parallel
    decide = decide or (lambda task, ahead: dispatch.decide(task, ahead, cwd=home()))
    pause = pause or (lambda: time.sleep(POLL_S))
    if start is None:
        if runner is not None:
            def start(task: dict, where: Path) -> Handle:
                ran = runner(task["body"], where, store, queued_options(task, defaults), task_id=task["id"],
                             **run_kwargs)
                return _Done(task["id"], ran.status)
        else:
            def start(task: dict, where: Path) -> Handle:
                return _Process(task, where, store, defaults)
    machine = machine or (lambda: capacity.machine(home()))
    backoff = backoff or time.sleep
    ceiling = config.max_parallel(os.environ)
    report, out, started, wanting, waits = WorkReport(), [], 0, False, False
    full_for: float | None = None  # seconds waited for space, while the disk is full
    fails = 0

    def through() -> None:
        """A whole pass went through: once a full disk is behind it, the level halves for it."""
        nonlocal full_for, fails
        if full_for is not None:
            with capacity.held(home()):
                capacity.disk_filled(store, home(), waited_s=full_for, ceiling=ceiling)
            full_for, fails = None, 0

    try:
        sweep(store)
    except Exception as exc:
        if not disk_full(exc):
            raise
    while True:
        try:
            ended, stopped = False, False
            for handle in list(out):
                status = handle.poll()
                if status is not None:
                    out.remove(handle)
                    report.ran.append(Ran(handle.task_id, status))
                    ended, stopped = True, stopped or status in _MAY_RESUME
            if ended:
                _settle(store)
            if stopped:
                _recover(store)
            launched: list[tuple[dict, Path]] = []
            leftover = False
            for round_ in range(DECIDE_ROUNDS + 1) if started < max_tasks else ():
                # The machine's lock is held to settle, allot and claim, never for a model call: what
                # needs one is read here, decided outside the lock, and the next round reads it again.
                undecided: list[Undecided] = []
                claimed: list[tuple[dict, Path]] = []
                with capacity.held(home()):
                    if round_ == 0:
                        report.looked_at = time.time()
                        level = capacity.settle(store, home(), machine=machine(), ceiling=ceiling, wanting=wanting)
                    else:
                        level = capacity.current(home(), ceiling=ceiling)
                    picked, shares = pick(store, repo, parallel=parallel, decide=decide, level=level.value,
                                          defer=undecided)
                    wanting = capacity.full(level.value, shares)
                    # Its own work waits for a worker another lane holds: it waits too, rather than stop.
                    waits = wanting and any(s.waiting for k, s in shares.items() if repo is None or k == repo)
                    for task in picked[:max_tasks - started]:
                        if not store.claim(task["id"]):
                            continue  # another dispatcher took it
                        where = Path(task["cwd"]) if task["cwd"] and Path(task["cwd"]).is_dir() else None
                        if where is None:
                            stops.record(store, task["id"], stops.Stop(
                                "missing_dir", f"its directory no longer exists: {task['cwd'] or '(none recorded)'}"))
                            store.pause_lane(task["repo"], f"task #{task['id']}: its directory no longer exists")
                            continue
                        started += 1
                        # When the dispatcher started it, to the millisecond: the run measures its
                        # pickup (from queued) and its spawn (to its plan) from here.
                        store.add_event(task["id"], "dispatched", at=round(time.time(), 3), level=level.value,
                                        **({"ceiling": ceiling} if isinstance(ceiling, int) else {}))
                        claimed.append((task, where))
                launched += claimed
                for task, where in claimed:
                    try:
                        chain(store, task, where)
                        out.append(start(task, where))
                    except Exception:
                        # Whatever raised, a task claimed this round with no process yet would stay
                        # `running` until the sweep's timeout: it goes back to the queue, with the rest.
                        for back, _ in claimed:
                            with contextlib.suppress(Exception):
                                current = store.get_task(back["id"]) or {}
                                if current.get("status") == "running" and not current.get("pid"):
                                    store.update_task(back["id"], status="queued", dispatched=0)
                        raise
                if not undecided or started >= max_tasks:
                    break
                if round_ == DECIDE_ROUNDS:
                    leftover = True  # work kept moving under the judgments: the next pass goes on
                    break
                decide_outside(store, undecided, decide)
            waits = waits or leftover
            through()
            if launched:
                continue  # look again at once: a finished run may have opened a lane
            if not out and (not waits or started >= max_tasks):
                report.at_limit = started >= max_tasks and bool(
                    store.queued_in(repo) if repo is not None else any(lane["queued"] for lane in store.lanes()))
                report.stopped_because = (
                    f"started {max_tasks} tasks, the unattended limit; run again to continue" if started >= max_tasks
                    else "nothing runnable is queued (empty, every lane paused, waiting on work in flight, or held "
                         "until other work passes)")
                return report
            pause()
        except Exception as exc:
            if not disk_full(exc):
                raise
            waited = full_for or 0.0
            if waited >= DISK_GIVE_UP_S:
                report.stopped_because = (f"the disk stayed full for {int(waited)}s ({exc}); the next session's hook "
                                          "starts a dispatcher again")
                return report
            if fails == 0:
                shed(store)  # what idle worktrees rebuild anyway goes first
            delay = DISK_WAITS_S[min(fails, len(DISK_WAITS_S) - 1)]
            fails += 1
            backoff(delay)
            full_for = waited + delay
