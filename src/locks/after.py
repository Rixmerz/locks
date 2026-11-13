"""Work queued after other work: held until that work passes, then started on what it kept.

`locks run … --after <task>`, `locks queue add … --after <task>` and `locks
workflow run … --after <run>` queue work held behind a task or a workflow run.
The hold is kept in the queued task's options (`after`), and the dispatcher never
starts a task that holds one. A task or run that does not exist, or whose work
was dropped (it failed, it was cancelled, it was found wrong), is refused when
the work is queued, with its reason.

When the work it waits for passes, the engine releases what waits on it, where
it already advances a workflow (`workflows.advance`, at the end of
`orchestrate.run`; for a run, when its last step passes), in one write
transaction: each held task starts on the branch that work kept (a run's, its
last step's), or on the base when that branch is already merged into it, and its
plan says which. Nothing is merged or rebased. Work that stops short of a pass
releases nothing: what waits on it stays held and says why, until a resume of it
passes, or the person dismisses or cancels what waits. A chain is released one
link at a time: each link waits on the one before it, and on nothing else.

Work that leaves the board without passing takes what waits on it along
(`abandon`): a task dismissed, or a run cancelled, will never pass, so what is
held behind it leaves the queue too, cancelled by locks with why, and the session
that queued it hears of it as an ending. A task superseded by a later one with the
same text that passed hands what waits on it to that one (`hand_on`), and one
whose change already landed on the checkout's branch releases it onto the base.

Light: the dispatcher, its views and `stops` read it.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from locks import isolate
from locks.store import Store

#: The key in a queued task's options (and a held workflow run's) that holds it.
KEY = "after"
#: Statuses whose work was dropped: nothing passed to start from, and nothing to wait for.
DROPPED = frozenset({"failed", "cancelled"})


class AfterError(ValueError):
    """The work named with `--after` cannot be waited for: said when the work is queued."""


def hold(task: Mapping[str, Any]) -> dict[str, int] | None:
    """What a task (or a run, by its options) waits for: `{"task": N}` or `{"run": N}`; None."""
    try:
        found = json.loads(task.get("options") or "{}").get(KEY)
    except (ValueError, AttributeError):
        return None
    return found if isinstance(found, dict) and found else None


def held(task: Mapping[str, Any]) -> bool:
    """A queued task that waits for other work to pass: the dispatcher never starts it."""
    return task.get("status") == "queued" and hold(task) is not None


def target(store: Store, *, task_id: int | None = None, run_id: int | None = None) -> dict[str, int]:
    """The hold for work queued after a task or a workflow run, checked. Raises
    `AfterError` with the reason when it does not exist, is no work locks runs, or
    already failed (its work dropped, so nothing would ever release what waits)."""
    if run_id is not None:
        run = store.get_run(run_id)
        if run is None:
            raise AfterError(f"there is no workflow run #{run_id}")
        if run["status"] == "cancelled":
            raise AfterError(f"run #{run_id} was cancelled: nothing it does will pass")
        for task in _latest(store, run_id).values():
            if _work_dropped(store, task):
                # A part is retried by its step's id: a retry runs again the parts that did not pass.
                raise AfterError(f"run #{run_id} stopped at {_at(task, 'task #')}, which "
                                 f"{_ended(store, task)}: `locks workflow retry {run_id} {task['workflow_step']}` "
                                 "first, or queue this without --after")
        return {"run": int(run_id)}
    assert task_id is not None
    task = store.get_task(task_id)
    if task is None:
        raise AfterError(f"there is no task #{task_id}")
    if task["source"] not in ("locks", "queue"):
        raise AfterError(f"#{task_id} is a session's turn, not work locks runs: nothing passes there to wait for")
    if _work_dropped(store, task):
        raise AfterError(f"#{task_id} {_ended(store, task)}, and its work was dropped: resume it first "
                         f"(`locks resume {task_id}`), or queue this without --after")
    return {"task": int(task_id)}


def ref(value: str) -> dict[str, int]:
    """What `--after` names: `N` or `task:N` a task, `run:N` a workflow run. Raises
    `ValueError` with what it takes."""
    kind, _, number = str(value).strip().rpartition(":")
    kind = kind or "task"
    # ASCII digits only: `²` is a digit to isdigit, and int() refuses it
    if kind not in ("task", "run") or not re.fullmatch(r"\d+", number, re.ASCII):
        raise ValueError(f"--after takes a task number (N or task:N) or a workflow run (run:N), not {value!r}")
    return {kind: int(number)}


def target_of(store: Store, wanted: Mapping[str, int]) -> dict[str, int]:
    """`target` for what `ref` read."""
    return target(store, run_id=wanted["run"]) if "run" in wanted else target(store, task_id=wanted["task"])


def caveat(store: Store, wanted: Mapping[str, int], key: str | None) -> str | None:
    """What a person should know about the work they queued after, when it is in another
    repository than `key`: which one it is, and the run of this repository that has the
    same number, when one does. None when it is this repository's."""
    if "run" in wanted:
        other, what = store.get_run(int(wanted["run"])), f"run #{wanted['run']}"
    else:
        other, what = store.get_task(int(wanted["task"])), f"#{wanted['task']}"
    if other is None or other.get("repo") == key:
        return None
    said = f"{what} is {'a run' if 'run' in wanted else 'a task'} in {other.get('repo') or 'no repository'}"
    if "task" in wanted:
        mine = store.get_run(int(wanted["task"]))
        if mine is not None and mine.get("repo") == key:
            said += f"; run #{wanted['task']} of this repository is `--after run:{wanted['task']}`"
    return said + ": this starts on this repository's base, and reads what that work kept where it lives"


def _work_dropped(store: Store, task: Mapping[str, Any]) -> bool:
    """Its work was dropped, as a run drops it: it failed, it was cancelled, or it was found
    wrong. A replan for turns keeps its work, and resumes."""
    if task["status"] in DROPPED:
        return True
    if task["status"] != "replan":
        return False
    from locks import stops  # stops reads holds: imported here, not at the top

    stop = stops.of(store, dict(task))
    return stop is None or stop.cause not in ("turns", "split")


def _ended(store: Store, task: Mapping[str, Any]) -> str:
    """How a task that stopped short of a pass ended, in the words a wait line uses."""
    from locks import stops  # stops reads holds: imported here, not at the top

    stop = stops.of(store, dict(task))
    return f"stopped as {stop.cause if stop else task['status']}"


def _latest(store: Store, run_id: int) -> dict[str, dict]:
    """Each step's newest task in a run (a retried step's last try is the one that counts);
    a step run in parts, each part's, as `step/part`."""
    out: dict[str, dict] = {}
    for task in store.run_tasks(run_id):
        part = _part_id(task)
        out[f"{task['workflow_step']}/{part}" if part else task["workflow_step"]] = task
    return out


def _part_id(task: Mapping[str, Any]) -> str | None:
    """The part of its step a run's task is, None for a step run whole."""
    try:
        return (json.loads(task.get("options") or "{}").get("part") or {}).get("id")
    except (ValueError, AttributeError):
        return None


def _at(task: Mapping[str, Any], label: str) -> str:
    """Where in its run a task is, as a person names it: "step 'build' (part web, #12)",
    `label` before its number."""
    part = _part_id(task)
    return f"step {task['workflow_step']!r} ({f'part {part}, ' if part else ''}{label}{task['id']})"


def waiting(store: Store, task: Mapping[str, Any]) -> str | None:
    """What a held task waits for, in one line: "waits for #17 to pass", or why it
    still waits when that work stopped short ("waits for #17, which stopped as
    review"). None for a task that holds nothing."""
    if not held(task):
        return None
    wanted = hold(task) or {}
    if "run" in wanted:
        run_id = int(wanted["run"])
        run = store.get_run(run_id)
        if run is None:
            return f"waits for run #{run_id}, which no longer exists: `locks dismiss {task['id']}` drops this"
        if run["status"] == "cancelled":
            return f"waits for run #{run_id}, which stopped as cancelled: `locks dismiss {task['id']}` drops this"
        stopped = next((t for t in _latest(store, run_id).values()
                        if t["status"] not in ("queued", "running", "done")), None)
        where = f" in {run['repo']}" if run.get("repo") != task.get("repo") else ""
        if stopped is not None:
            t = stopped
            return (f"waits for run #{run_id}{where}, which stopped at {_at(t, '#')} as "
                    f"{_ended(store, t).removeprefix('stopped as ')}: a resume of it to a pass releases this")
        return f"waits for run #{run_id}{where} to pass"
    task_id = int(wanted.get("task") or 0)
    other = store.get_task(task_id)
    if other is None:
        return f"waits for #{task_id}, which no longer exists: `locks dismiss {task['id']}` drops this"
    if other.get("repo") != task.get("repo"):
        # Numbers are global: work in another repository is named with it, and by its title.
        title = " ".join(str(other.get("title") or "").split())
        title = title if len(title) <= 80 else title[:79] + "…"
        named = f"task #{task_id} in {other.get('repo') or 'no repository'} ({title})"
        if other["status"] in ("queued", "running", "done"):
            return f"waits for {named} to pass"
        return f"waits for {named}, which {_ended(store, other)}: a resume of it to a pass releases this"
    if other["status"] in ("queued", "running", "done"):
        return f"waits for #{task_id} to pass"
    return f"waits for #{task_id}, which {_ended(store, other)}: a resume of it to a pass releases this"


def _kept(store: Store, wanted: Mapping[str, int]) -> tuple[str | None, list[str], str, str | None]:
    """What the work a hold names kept when it passed: its branch, the files it changed,
    how it is named, and the directory it ran in. A run's is its last step's, the one that
    holds every step's work."""
    if "run" in wanted:
        run_id = int(wanted["run"])
        run = store.get_run(run_id) or {}
        order = [s["id"] for s in json.loads(run.get("definition") or "{}").get("steps") or ()]
        latest = _latest(store, run_id)
        paths: list[str] = []
        branch = None
        for step in reversed(order):
            finished = ((store.final_event(latest[step]["id"]) or {}).get("data") or {}) if step in latest else {}
            paths += list(finished.get("changed") or ())
            branch = branch or finished.get("branch")
        return branch, sorted(set(paths)), f"run #{run_id}'s last step", run.get("cwd")
    task_id = int(wanted["task"])
    finished = (store.final_event(task_id) or {}).get("data") or {}
    return (finished.get("branch"), list(finished.get("changed") or ()), f"#{task_id}",
            (store.get_task(task_id) or {}).get("cwd"))


def _elsewhere(source: str | None, where: Path | None) -> Path | None:
    """The checkout the awaited work ran in, when it is another repository than the
    held work's: None when they are the same one, or git cannot say."""
    from locks import repo

    if not source or where is None or not where.is_dir() or not Path(source).is_dir():
        return None
    theirs, mine = repo.toplevel(Path(source)), repo.toplevel(where)
    if theirs is None or mine is None or theirs == mine:
        return None
    # A worktree of the same repository is the same repository: one git store, with or without a
    # remote (with none, each checkout's key is its own path).
    if repo.key(theirs) == repo.key(mine) or _store_of(theirs) == _store_of(mine):
        return None
    return theirs


def _store_of(top: Path) -> str:
    """The git store a checkout keeps its objects in, shared by every worktree of it."""
    from locks import repo

    return repo._git(top, "rev-parse", "--path-format=absolute", "--git-common-dir") or str(top)


def reads(kept: tuple[str | None, list[str], str, str | None], where: Path | None) -> str | None:
    """The other repository held work is given to read (`--add-dir`), when the work it
    waited for kept a branch there; None when it is this repository's."""
    if not kept[0]:
        return None
    other = _elsewhere(kept[3], where)
    return str(other) if other is not None else None


def _start(kept: tuple[str | None, list[str], str, str | None], where: Path | None) -> tuple[str | None, str]:
    """The commit held work starts on in its checkout (None: the base, its HEAD) and the
    line its plan says. Read only: nothing is merged or rebased."""
    branch, paths, who, source = kept
    if not branch:
        return None, f"queued after {who}, which passed and kept no branch: this starts on the base"
    if where is None or not where.is_dir():
        return None, f"queued after {who}, which passed on {branch}: this starts where it was queued"
    if (other := _elsewhere(source, where)) is not None:
        # Another repository: nothing of it crosses into this one; its branch is read where it lives.
        changed = (f"; it changed {', '.join(paths[:20])}" + (" and more" if len(paths) > 20 else "")) if paths else ""
        return None, (f"queued after {who}, which passed in another repository, {other}, on {branch}: this starts "
                      "on this repository's base, and nothing of it is merged here. Read what it built there, "
                      f"without changing that checkout: `git -C {other} log --oneline HEAD..{branch}`, "
                      f"`git -C {other} diff HEAD...{branch}` and `git -C {other} show {branch}:<path>`{changed}")
    try:
        tip = isolate.head(where, f"refs/heads/{branch}")
    except isolate.IsolationError:
        return None, (f"queued after {who}, which passed on {branch}: that branch is not in this checkout "
                      "(deleted, or in another repository), so this starts on the base")
    if isolate.landed(where, branch, paths):
        return None, (f"queued after {who}, which passed on {branch}: that branch is already merged into the base, "
                      "so this starts on the base")
    return tip, (f"queued after {who}, which passed: this starts on {branch} ({tip[:12]}), the branch it kept; "
                 "nothing is merged or rebased")


def behind(store: Store, wanted: Mapping[str, int]) -> list[dict]:
    """The queued tasks held behind a task (`{"task": N}`) or a run (`{"run": N}`), oldest first."""
    want = {k: int(v) for k, v in wanted.items()}
    return [t for t in reversed(store.list_tasks(status=["queued"], limit=1000)) if hold(t) == want]


#: The cause a held task leaves the queue with when what it waited for left the board unpassed.
ABANDONED = "after"


def abandon(store: Store, wanted: Mapping[str, int], why: str) -> list[int]:
    """Work held behind work that left the board without passing (a task dismissed, a run
    cancelled): nothing will release it now, so it leaves the queue, cancelled by locks with
    `why`, its hold dropped. It is never marked reported: the session that queued it hears
    of it as an ending, and a resume starts it on the base. The ids taken off."""
    from locks import stops  # stops reads holds: imported here, not at the top

    want = {k: int(v) for k, v in wanted.items()}
    gone = []
    for t in behind(store, want):
        options = json.loads(t.get("options") or "{}")
        options.pop(KEY, None)
        store.update_task(t["id"], options=json.dumps(options))
        stops.record(store, t["id"], stops.Stop(ABANDONED, why, by="locks", extra={"after": want}),
                     result=False)
        run = store.get_run(int(t["workflow_run"])) if t.get("workflow_run") else None
        if run is not None and run["status"] == "active" and hold(run) == want:
            # A whole run held behind it never starts either.
            store.update_run(run["id"], status="cancelled", reason=why)
        gone.append(t["id"])
    return gone


def hand_on(store: Store, task_id: int, to: Mapping[str, Any]) -> list[int]:
    """Work held behind a task superseded by `to`, a later one with the same text that
    passed: it waits on `to` instead, and the end of `to`'s run releases it onto what `to`
    kept (`workflows.advance`), as it releases what was queued after `to` itself. The ids moved."""
    return reassign(store, task_id, int(to["id"]), f"#{task_id} was superseded by #{to['id']}, which passed the "
                                                   "same work")


def reassign(store: Store, task_id: int, to_id: int, why: str) -> list[int]:
    """Work held behind task `task_id` waits on task `to_id` instead, with an `after_moved`
    event that says why. The ids moved."""
    moved = []
    for t in behind(store, {"task": task_id}):
        options = json.loads(t.get("options") or "{}")
        options[KEY] = {"task": int(to_id)}
        store.update_task(t["id"], options=json.dumps(options))
        store.add_event(t["id"], "after_moved", was={"task": task_id}, now={"task": int(to_id)}, why=why)
        moved.append(t["id"])
    return moved


def landed(store: Store, task_id: int, into: str) -> list[dict]:
    """Work held behind a task whose change is already on the checkout's branch (`into`):
    released onto the base, which holds that change. The tasks released."""
    freed = []
    for t in behind(store, {"task": task_id}):
        with store.immediate():
            now = store.get_task(t["id"])
            if now is None or not held(now):
                continue
            _free(store, now, {"task": task_id}, None,
                  f"#{task_id}'s change is already in {into}: it starts on the base, which holds it")
        freed.append(now)
    return freed


def release(store: Store, *, task: Mapping[str, Any] | None = None,
            run: Mapping[str, Any] | None = None) -> list[dict]:
    """After `task` or workflow `run` ended: when it passed, every queued task held behind
    it is released onto what it kept, in one write transaction (a task released twice at
    once is released once). The tasks released, for the caller to start their lanes."""
    ended, passed = (({"task": int(task["id"])}, task.get("status") == "done") if task is not None
                     else ({"run": int(run["id"])}, (run or {}).get("status") == "done"))
    if not passed:
        return []
    waiting_on = [t for t in store.list_tasks(status=["queued"], limit=1000) if hold(t) == ended]
    if not waiting_on:
        return []
    kept = _kept(store, ended)
    # Git is read before the transaction: the write lock is held for the writes alone.
    starts = {t["id"]: _start(kept, Path(t["cwd"]) if t.get("cwd") else None) for t in waiting_on}
    read = {t["id"]: reads(kept, Path(t["cwd"]) if t.get("cwd") else None) for t in waiting_on}
    heads = {t["id"]: None if starts[t["id"]][0] else _now(t) for t in waiting_on}
    released = []
    with store.immediate():
        for t in waiting_on:
            now = store.get_task(t["id"])
            if now is None or not held(now) or hold(now) != ended:
                continue
            _free(store, now, ended, *starts[t["id"]], head=heads[t["id"]], read=read[t["id"]])
            released.append(now)
    return released


def settle(store: Store, task: Mapping[str, Any]) -> bool:
    """Work just queued after work that has passed already: released at once, onto what
    it kept. False while that work has not passed."""
    wanted = hold(task) or {}
    other = (store.get_run(int(wanted["run"])) if "run" in wanted
             else store.get_task(int(wanted["task"])) if "task" in wanted else None)
    if other is None or other["status"] != "done":
        return False
    kept = _kept(store, wanted)
    where = Path(task["cwd"]) if task.get("cwd") else None
    commit, why = _start(kept, where)
    head = None if commit else _now(task)
    with store.immediate():
        _free(store, task, wanted, commit, why, head=head, read=reads(kept, where))
    return True


def _now(task: Mapping[str, Any]) -> str | None:
    """The HEAD a held workflow step's checkout is at now, for a run released with nothing
    kept to start on; None for a task in no run, or where git cannot say."""
    where = Path(task["cwd"]) if task.get("cwd") else None
    if not task.get("workflow_run") or where is None or not where.is_dir():
        return None
    try:
        return isolate.head(where, "HEAD")
    except isolate.IsolationError:
        return None


def _free(store: Store, task: Mapping[str, Any], waited: Mapping[str, int], commit: str | None, why: str, *,
          head: str | None = None, read: str | None = None) -> None:
    """One held task, released: its hold dropped, its start set, and a `released` event the
    plan reads. A step of a held workflow run frees its run too, so the steps after it
    build on the same start; with no commit to start on, what the run changed is read from
    `head`, the checkout as it is now, not as it was when the run was queued."""
    options = json.loads(task.get("options") or "{}")
    options.pop(KEY, None)
    if commit:
        options["base"] = commit
    if read and read not in (options.get("add_dirs") or ()):
        # The other repository the awaited work kept its branch in: given to read, as `--add-dir` gives it.
        options["add_dirs"] = [*(options.get("add_dirs") or ()), read]
    store.update_task(task["id"], options=json.dumps(options))
    store.add_event(task["id"], "released", after=dict(waited), base=commit, why=why)
    run = store.get_run(int(task["workflow_run"])) if task.get("workflow_run") else None
    if run is not None and hold(run) is not None:
        kept = json.loads(run["options"] or "{}")
        kept.pop(KEY, None)
        if read and read not in (kept.get("add_dirs") or ()):
            kept["add_dirs"] = [*(kept.get("add_dirs") or ()), read]
        if commit:
            # What the run changed is read from here on, and a step with nothing kept before it starts here.
            kept.update(base=commit, origin=commit)
        elif head:
            kept["origin"] = head
        store.update_run(run["id"], options=json.dumps(kept))


def kept_start(store: Store, task_id: int, where: Path | None) -> tuple[str | None, str | None, list[str]]:
    """The commit work may start on to build on what task `task_id` kept when it passed (None:
    no branch, merged already, elsewhere), the branch, and the files it changed. Read only."""
    kept = _kept(store, {"task": int(task_id)})
    return _start(kept, where)[0], kept[0], kept[1]


def released_line(store: Store, task_id: int) -> str | None:
    """What a held task's release said of where it starts, for its first plan; or what the
    dispatcher said when it started it on the work it had waited for (`flow.chain`)."""
    found = store.last_event(task_id, "released") or store.last_event(task_id, "chained")
    return str(found["data"].get("why") or "") or None if found else None
