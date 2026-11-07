"""What the store says is broken: states locks should never reach, read, never written.

Every rule here is a bug locks shipped and found late, in real work, turned into a
query: a lane paused on a task that already passed, a workflow part that lost its mark
when it was resumed, an integrate step that joined fewer branches than its parts kept,
a worker whose death nobody resumed. A rule reads the store as it is (`broken`: work
is stuck now) or as it went over a window (`suspect`: something that should have
happened did not). No model, no write; `locks doctor` prints them, the session start names
what is broken, and the scenario tests assert there is none. Two rules read more than the store:
the livespec index of each repository recent runs were in (opened read-only, with the last
commit's time), since a missing or stale index is a bug nothing in the store shows.

A `suspect` rule names the release that fixes its bug (`FIXED_IN`), and reads it only from
what that release or a later one wrote (the `version` an event keeps): the records from
before it are counted apart as `fixed`, so what it reports is the bug coming back, never
the history the fix is for.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from locks import after, behavior, config
from locks.adapters import ABSENT, UNREADABLE
from locks.matrix import turns_for
from locks.repo import key as repo_key
from locks.store import Store

#: What a finding says of the work: stuck now, a sign over the window, or records from
#: before the release that fixes what they show.
BROKEN, SUSPECT, FIXED = "broken", "suspect", "fixed"
#: How far back the `suspect` rules read by default.
WINDOW_DAYS = 2
#: Queued work no dispatcher took for this long, with nothing running in its lane, is stuck.
QUEUED_STUCK = timedelta(minutes=30)
#: A pickup is seconds when nothing runs in the lane: one this long there was work no dispatcher saw.
PICKUP_UNSEEN = timedelta(minutes=5)
#: An automatic resume that started no attempt after this long was refused or died.
RESUME_SILENT = timedelta(minutes=10)
#: A running task whose process is gone and that said nothing for this long.
RUNNING_SILENT = timedelta(minutes=2)
#: Worktree attempts with no session id at this share or more (over at least as many) mean no
#: attempt can be continued.
SESSIONLESS_SHARE, SESSIONLESS_MIN = 0.5, 3
#: A summary that says a worker's answer had the wrong shape, not that its work was wrong.
_FORMAT = re.compile(r"cannot run in parts|no result block|two result blocks|could not be read", re.I)
#: The release that fixes each suspect rule's bug: records written before it are history.
FIXED_IN = {
    "part-lost-its-mark": "0.29.2",
    "join-short-of-parts": "0.29.2",
    "attempts-with-no-session": "0.29.2",
    "turns-under-the-kind": "0.29.2",
    "format-failure-climbed": "0.30.1",
    "fix-round-off-base": "0.31.0",
    "revision-judged-twice": "0.32.0",
    "trial-never-sampled": "0.32.0",
    "note-proven-unbriefed": "0.32.0",
    "preexisting-that-was-not": "0.34.0",
    "problem-open-after-pass": "0.40.0",
    "flake-fix-checked-once": "0.40.0",
    "turns-under-the-wall": "0.40.0",
    "pickup-nobody-saw": "0.45.0",
    "died-after-a-pass": "0.63.0",
    "resumed-after-it-landed": "0.66.0",
    "swept-before-its-pid": "0.66.0",
    "advised-again-in-a-prompt": "0.82.0",
    "habit-review-unapplied": "0.85.0",
    "tools-logged-off": "0.89.0",
    "problem-opened-by-a-race": "0.90.0",
    "clean-review-read-nothing": "0.91.0",
}
#: An attempt that ran this long per turn it counted kept a count the CLI restarted.
WALL_PER_TURN_S = 300
#: Attempts shorter than this are never read for it: a long check after few turns is honest.
WALL_LONG_S = 20 * 60
#: A candidate revision this old whose trial briefed no task, while its role's tasks here were briefed.
TRIAL_UNSAMPLED = timedelta(days=3)
#: Statuses a task leaves the board with: nothing it paused waits on it any more.
_ENDED = ("done", "dismissed", "superseded", "cancelled")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    what: str
    #: What clears it, as a command or a sentence.
    fix: str
    task_id: int | None = None
    repo: str | None = None

    def data(self) -> dict:
        return asdict(self)


def _ts(value: object) -> datetime | None:
    try:
        found = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _options(task: Mapping) -> dict:
    try:
        found = json.loads(task.get("options") or "{}")
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _release(version: object) -> tuple[int, ...]:
    """A version as numbers; none (a record from before events kept one) is the oldest."""
    found = re.match(r"(\d+)\.(\d+)\.(\d+)", str(version or ""))
    return tuple(int(n) for n in found.groups()) if found else (0,)


def _fixed(rule: str, version: object) -> bool:
    """The record was written before the release that fixes its rule's bug."""
    return _release(version) < _release(FIXED_IN[rule])


def _version_of(store: Store, task_id: int) -> object:
    """The version of the latest record a task holds that keeps one."""
    found = [e for e in (store.last_event(task_id, kind) for kind in ("planned", "attempt_started", "finished"))
             if e is not None]
    return max(found, key=lambda e: e["id"])["data"].get("version") if found else None


def _history(rule: str, ids: list[int], repo: str | None) -> Finding:
    """Records a rule matched that the release fixing it never wrote: counted, never a sign."""
    return Finding(rule, FIXED, f"{len(ids)} record(s) from before {FIXED_IN[rule]}, the release that fixes it "
                                f"(latest #{max(ids)})",
                   "nothing to do: work run since shows whether it came back", max(ids), repo)


# --- broken: work stuck now --------------------------------------------------------------


def lanes_paused_on_ended(store: Store, **_) -> Iterable[Finding]:
    """A lane paused on a task that left the board waits on nothing: its queue never starts."""
    for lane in store.lanes():
        found = re.match(r"task #(\d+)\b", str(lane.get("reason") or "")) if lane.get("paused") else None
        task = store.get_task(int(found.group(1))) if found else None
        if task and task["status"] in _ENDED:
            yield Finding("lane-paused-on-ended", BROKEN,
                          f"the lane is paused on #{task['id']}, which is {task['status']}: "
                          f"{lane['queued']} queued task(s) wait on nothing",
                          f"locks lanes --unpause {lane['repo']}", task["id"], lane["repo"])


def running_with_no_process(store: Store, *, now: datetime, alive: Callable[[int | None], bool],
                            **_) -> Iterable[Finding]:
    """A task marked running whose process is gone and that has said nothing since."""
    for task in store.list_tasks(status=["running"], source=("locks", "queue"), limit=500):
        last = _ts(task.get("updated_at"))
        if task.get("pid") and not alive(task["pid"]) and last and now - last > RUNNING_SILENT:
            yield Finding("running-with-no-process", BROKEN,
                          f"#{task['id']} is running, but its process {task['pid']} is gone",
                          "the next turn's hooks sweep it; if they do not, the sweep is broken",
                          task["id"], task["repo"])


def queued_with_no_dispatcher(store: Store, *, now: datetime, **_) -> Iterable[Finding]:
    """Queued work that may run, in an open lane where nothing runs, that nobody started."""
    paused = {lane["repo"] for lane in store.lanes() if lane.get("paused")}
    running = {t["repo"] for t in store.list_tasks(status=["running"], source=("locks", "queue"), limit=500)}
    for task in store.list_tasks(status=["queued"], limit=500):
        since = _ts(task.get("updated_at"))
        if (task["repo"] in paused or task["repo"] in running or after.held(task) or since is None
                or now - since < QUEUED_STUCK):
            continue
        yield Finding("queued-with-no-dispatcher", BROKEN,
                      f"#{task['id']} has waited {int((now - since).total_seconds() // 60)} min in an open lane "
                      "with nothing running", f"locks work --repo {task.get('cwd') or task['repo']}",
                      task["id"], task["repo"])


def wait_judged_on_gone(store: Store, *, now: datetime, **_) -> Iterable[Finding]:
    """A queued task held back ("waits for #N") by a decision about work that is all gone,
    while other work runs in its lane: the dispatcher judges it again as it walks the lane
    (`flow._judged_against_gone`), so one that stays so is a dispatcher that does not."""
    running = {t["repo"] for t in store.list_tasks(status=["running"], source=("locks", "queue"), limit=500)}
    for task in store.list_tasks(status=["queued"], limit=500):
        if task.get("parallel") != 0 or after.held(task) or task["repo"] not in running:
            continue
        decided = store.last_event(task["id"], "dispatch_decided")
        judged = [int(i) for i in ((decided or {}).get("data") or {}).get("ahead") or () if isinstance(i, int)]
        at = _ts((decided or {}).get("ts"))
        if not judged or at is None or now - at < QUEUED_STUCK:
            continue
        if all((store.get_task(i) or {}).get("status") not in ("running", "queued") for i in judged):
            yield Finding("wait-judged-on-gone", BROKEN,
                          f"#{task['id']} waits on a decision about {', '.join(f'#{i}' for i in judged)}, which "
                          "all ended, while other work runs in its lane",
                          f"locks lanes --beside {task['id']} when it does not touch the work running",
                          task["id"], task["repo"])


def held_behind_gone(store: Store, **_) -> Iterable[Finding]:
    """Work queued after work that left the board without passing (dismissed, superseded,
    landed, or a run cancelled or gone): nothing will ever release it."""
    for task in store.list_tasks(status=["queued"], limit=1000):
        wanted = after.hold(task) or {}
        if "run" in wanted:
            run = store.get_run(int(wanted["run"]))
            gone = None if run is not None and run["status"] != "cancelled" else (
                f"run #{wanted['run']}, which " + ("no longer exists" if run is None else "was cancelled"))
        elif "task" in wanted:
            other = store.get_task(int(wanted["task"]))
            gone = None if other is not None and other["status"] != "dismissed" else (
                f"#{wanted['task']}, which " + ("no longer exists" if other is None else "left the board"))
        else:
            continue
        if gone:
            yield Finding("held-behind-gone", BROKEN, f"#{task['id']} is queued after {gone}: nothing releases it",
                          f"locks dismiss {task['id']} --why 'what it waited for is gone', then queue it again "
                          "if it is still wanted", task["id"], task["repo"])


def resumes_that_never_ran(store: Store, *, now: datetime, since: str, **_) -> Iterable[Finding]:
    """An automatic resume that started no attempt: refused, or died before one."""
    for event in store.events_of_kinds(["auto_resumed"], since=since):
        task = store.get_task(event["task_id"])
        at = _ts(event["ts"])
        if task is None or task["status"] in _ENDED or at is None or now - at < RESUME_SILENT:
            continue
        started = store.last_event(task["id"], "attempt_started")
        if started is None or started["id"] < event["id"]:
            yield Finding("resume-never-ran", BROKEN,
                          f"#{task['id']} was resumed on its own {int((now - at).total_seconds() // 60)} min ago and "
                          f"no attempt started ({task['status']}); its one automatic try is spent",
                          f"locks resume {task['id']} --unattended; read its log, "
                          f"~/.local/share/locks/work/task-{task['id']}.log",
                          task["id"], task["repo"])


# --- suspect: what went wrong over the window ---------------------------------------------


def parts_that_lost_their_mark(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """A step run in parts with a task that is no part any more: its integrate drops the others."""
    runs = {e["task_id"]: None for e in store.events_of_kinds(["finished"], since=since)}
    seen: set[int] = set()
    for task_id in runs:
        task = store.get_task(task_id)
        run = task.get("workflow_run") if task else None
        if not run or run in seen:
            continue
        seen.add(run)
        steps: dict[str, list[dict]] = {}
        for t in store.run_tasks(run):
            steps.setdefault(t["workflow_step"], []).append(t)
        for step, tasks in steps.items():
            marked = [t for t in tasks if _options(t).get("part")]
            for t in tasks:
                if marked and not _options(t).get("part"):
                    if _fixed("part-lost-its-mark", _version_of(store, t["id"])):
                        yield _history("part-lost-its-mark", [t["id"]], t["repo"])
                        continue
                    yield Finding("part-lost-its-mark", SUSPECT,
                                  f"run {run}: #{t['id']} runs step {step!r} with no part beside "
                                  f"{len(marked)} part(s): the step reads as it alone",
                                  "a resume or a rewrite dropped `part` from its options", t["id"], t["repo"])


def joins_short_of_their_parts(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """An integrate step that skipped its join while more than one part kept a branch."""
    for event in store.events_of_kinds(["finished"], since=since):
        data = event["data"]
        # A ledger step skipped for want of findings joins nothing: only an integrate step's skip counts.
        task = store.get_task(event["task_id"]) if data.get("skipped") and "ledger" not in data else None
        if not task or not task.get("workflow_run"):
            continue
        kept = []
        for t in store.run_tasks(task["workflow_run"]):
            if t["id"] < task["id"] and _options(t).get("part") is not None:
                ended = store.final_event(t["id"]) or {}
                if (ended.get("data") or {}).get("branch"):
                    kept.append(t["id"])
        if len(kept) > 1 and _fixed("join-short-of-parts", data.get("version")):
            yield _history("join-short-of-parts", [task["id"]], task["repo"])
        elif len(kept) > 1:
            yield Finding("join-short-of-parts", SUSPECT,
                          f"run {task['workflow_run']}: #{task['id']} passed without joining, while parts "
                          f"{', '.join(f'#{k}' for k in kept)} each kept a branch: the work after it misses some",
                          "the parts' branches were never merged; check what the run's last branch holds",
                          task["id"], task["repo"])


def attempts_with_no_session(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """Worktree attempts keep their session to be continued; when most keep none, none can be."""
    by_repo: dict[str | None, list[bool]] = {}
    before: dict[str | None, list[int]] = {}
    for event in store.events_of_kinds(["attempt_started"], since=since):
        if not event["data"].get("base"):
            continue  # not in a worktree: no session is kept there
        attempt = store.attempt(event["task_id"], int(event["data"].get("seq") or 0))
        task = store.get_task(event["task_id"])
        if attempt is None or task is None or (attempt.get("passed") is None and attempt.get("failure") is None):
            continue
        if _fixed("attempts-with-no-session", event["data"].get("version")):
            if not attempt.get("session_id"):
                before.setdefault(task["repo"], []).append(task["id"])
            continue
        by_repo.setdefault(task["repo"], []).append(bool(attempt.get("session_id")))
    for repo, ids in before.items():
        yield _history("attempts-with-no-session", ids, repo)
    for repo, kept in by_repo.items():
        missing = kept.count(False)
        if len(kept) >= SESSIONLESS_MIN and missing / len(kept) >= SESSIONLESS_SHARE:
            yield Finding("attempts-with-no-session", SUSPECT,
                          f"{missing} of {len(kept)} worktree attempts kept no session id: more turns or more "
                          "effort cannot continue a worker, every one starts fresh",
                          "a worker that inherits the host's session id reports it as its own "
                          "(launch.HOST_SESSION_VARS)", None, repo)


def turns_under_the_kind(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """First attempts that started under their kind's turns, with no `--max-turns` asking for
    it: one finding per repository and kind."""
    under: dict[tuple[str | None, str], list[int]] = {}
    before: dict[str | None, list[int]] = {}
    for event in store.events_of_kinds(["attempt_started"], since=since):
        turns = event["data"].get("max_turns")
        task = store.get_task(event["task_id"]) if event["data"].get("seq") == 1 else None
        if not task or not task.get("kind") or not isinstance(turns, int) or _options(task).get("max_turns"):
            continue
        if turns < turns_for(task["kind"]) and _fixed("turns-under-the-kind", event["data"].get("version")):
            before.setdefault(task["repo"], []).append(task["id"])
        elif turns < turns_for(task["kind"]):
            under.setdefault((task["repo"], task["kind"]), []).append(task["id"])
    for repo, old in before.items():
        yield _history("turns-under-the-kind", old, repo)
    for (repo, kind), ids in under.items():
        yield Finding("turns-under-the-kind", SUSPECT,
                      f"{len(ids)} {kind} task(s) started under the kind's {turns_for(kind)} turns "
                      f"(latest #{max(ids)})",
                      "a standard or a history rule set it lower than the kind allows", max(ids), repo)


def format_failures_that_climbed(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """A worker's answer of the wrong shape is no shallow work: it climbed effort anyway."""
    before: list[int] = []
    for event in store.events_of_kinds(["moved"], since=since):
        data = event["data"]
        if data.get("axis") != "effort" or data.get("trigger") != "inconclusive":
            continue
        attempt = store.attempt(event["task_id"], int(data.get("seq") or 0))
        if attempt and _FORMAT.search(attempt.get("summary") or "") and _fixed("format-failure-climbed",
                                                                               data.get("version")):
            before.append(event["task_id"])
        elif attempt and _FORMAT.search(attempt.get("summary") or ""):
            yield Finding("format-failure-climbed", SUSPECT,
                          f"#{event['task_id']} climbed {data.get('from_cell')} → {data.get('to_cell')} because its "
                          f"answer had the wrong shape: {str(attempt['summary'])[:120]}",
                          "the shape the brief asks for was missed; a stronger cell pays for a format, not for depth",
                          event["task_id"], None)
    if before:
        yield _history("format-failure-climbed", before, None)


def fix_rounds_off_their_base(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """A fix's later round that started where the round before it started, not on what that
    round kept: the run's last branch misses the earlier round's fixes."""
    before: list[int] = []
    seen: set[int] = set()
    for event in store.events_of_kinds(["planned"], since=since):
        task = store.get_task(event["task_id"])
        spec = _options(task).get("ledger") or {} if task else {}
        if not task or task["id"] in seen or spec.get("role") != "fix" or int(spec.get("round") or 1) < 2:
            continue
        seen.add(task["id"])
        earlier = [t for t in store.run_tasks(task["workflow_run"]) if t["id"] < task["id"]
                   and t.get("workflow_step") == task.get("workflow_step") and t["status"] == "done"]
        if not earlier:
            continue
        prior = earlier[-1]
        kept = (store.final_event(prior["id"]) or {}).get("data") or {}
        if not kept.get("branch"):
            continue
        prior_base = (_options(prior).get("ledger") or {}).get("base")
        off = spec.get("base") != kept["tip"] if kept.get("tip") else spec.get("base") == prior_base
        if off and _fixed("fix-round-off-base", _version_of(store, task["id"])):
            before.append(task["id"])
        elif off:
            yield Finding("fix-round-off-base", SUSPECT,
                          f"run {task['workflow_run']}: fix round {spec.get('round')} #{task['id']} did not start "
                          f"from what round {int(spec.get('round')) - 1} #{prior['id']} kept on {kept['branch']}",
                          f"merge {kept['branch']} into the run's last branch before using it", task["id"],
                          task["repo"])
    if before:
        yield _history("fix-round-off-base", before, None)


def candidates_on_trial_at_once(store: Store, **_) -> Iterable[Finding]:
    """Two revisions of one role on trial in one repository: each would take the other's arm."""
    from locks import revisions

    for repo, role in sorted({(r["repo"], r["role"]) for r in store.role_revisions()}):
        open_ = [p for p in revisions.state(store, repo, role)["proposals"] if p["trial"] is None]
        if len(open_) > 1:
            yield Finding("candidates-on-trial-at-once", BROKEN,
                          f"{role} has {len(open_)} revisions on trial at once here: "
                          + ", ".join(f"#{p['id']}" for p in open_),
                          "the newest is the one briefed; the others wait on a verdict they never get",
                          None, repo)


def revisions_judged_twice(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """A role's revision given two verdicts of one kind: adopted and dropped, or kept and reverted."""
    before: list[int] = []
    rows = store.role_revisions(since=since or "")
    for revision in sorted({r["revision"] for r in rows if r["revision"] is not None}):
        mine = [r for r in rows if r["revision"] == revision]
        for pair in (("adopted", "dropped"), ("kept", "reverted")):
            twice = [r for r in mine if r["record"] in pair]
            if len(twice) < 2:
                continue
            if _fixed("revision-judged-twice", twice[-1]["version"]):
                before.append(revision)
                continue
            yield Finding("revision-judged-twice", SUSPECT,
                          f"{twice[0]['role']}'s revision #{revision} was judged {len(twice)} times: "
                          + ", ".join(r["record"] for r in twice),
                          "the first verdict stands; a second settle judged it again", None, twice[0]["repo"])
    if before:
        yield _history("revision-judged-twice", before, None)


def trials_never_sampled(store: Store, *, now: datetime, **_) -> Iterable[Finding]:
    """A revision on trial for days whose arms hold no task while its role's tasks here were
    briefed since: the trial is never given, and nothing is ever judged."""
    from locks import revisions

    before: list[int] = []
    for repo, role in sorted({(r["repo"], r["role"]) for r in store.role_revisions()}):
        candidate = revisions.state(store, repo, role)["candidate"]
        at = _ts(candidate["at"]) if candidate else None
        if candidate is None or at is None or now - at < TRIAL_UNSAMPLED:
            continue
        briefs = store.briefs(repo)
        sampled = any(b["data"].get("revision_trial") == candidate["id"] for b in briefs)
        since = [b for b in briefs if b["data"].get("role") == role and str(b["ts"]) > candidate["at"]]
        if sampled or not since:
            continue
        if _fixed("trial-never-sampled", candidate["version"]):
            before.append(candidate["id"])
            continue
        yield Finding("trial-never-sampled", SUSPECT,
                      f"{role}'s revision #{candidate['id']} has been on trial since {candidate['at'][:10]}, and none "
                      f"of the {len(since)} {role} task(s) briefed here since was put in an arm",
                      "check that `role_trials` is on where the workers run (`locks config role_trials`)", None, repo)
    if before:
        yield _history("trial-never-sampled", before, None)


def notes_proven_unbriefed(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """A worker-learned note made current as proven, though fewer tasks than proving takes
    kept a brief that carried it: it was promoted on a guess, not on evidence. Proving
    exists since the release that names this rule, so every such note is a sign."""
    from locks import notes

    briefs: dict[int, set[int]] | None = None
    for project in store.note_projects():
        for note in store.notes(project, states=["current"], limit=100_000):
            if note["author"] != "worker" or not str(note["state_reason"] or "").startswith(notes.PROVEN) or (
                    since and str(note["updated_at"]) < since):
                continue
            briefs = notes.carried(store) if briefs is None else briefs
            read = [t for t, ids in briefs.items() if note["id"] in ids and t != note["task_id"]]
            if len(read) < notes.PROVE_AFTER:
                yield Finding("note-proven-unbriefed", SUSPECT,
                              f"note #{note['id']} was made current as proven, but {len(read)} task(s) kept a brief "
                              f"that carried it (proving takes {notes.PROVE_AFTER})",
                              f"check it with `locks notes` and drop it if it does not hold (#{note['id']})",
                              note["task_id"], project)


#: Kinds whose change must make a failure go away (`orchestrate._REPRODUCES`): failing at
#: the base is their pass only when they fixed at least one of the failures.
_MUST_FIX = frozenset({"debug-repro", "debug-unclear"})


def preexisting_that_was_not(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """A check whose failures were called pre-existing though they were not all there at the
    base: the work added a failing line, the work's run named none, it named more than the
    base's, or a fix that must clear a failure fixed none. A red check let through."""
    before: list[int] = []
    for event in store.events_of_kinds(["check_preexisting"], since=since):
        data = event["data"]
        task = store.get_task(event["task_id"])
        n, base_n = int(data.get("n") or 0), int(data.get("base_n") or 0)
        wrong = int(data.get("added") or 0) > 0 or n == 0 or n > base_n or (
            task is not None and task.get("kind") in _MUST_FIX and not int(data.get("fixed") or 0))
        if not wrong:
            continue
        if _fixed("preexisting-that-was-not", data.get("version")):
            before.append(event["task_id"])
            continue
        yield Finding("preexisting-that-was-not", SUSPECT,
                      f"#{event['task_id']}'s red `{data.get('command')}` was let through as failing at the base "
                      f"({n} failure(s) here, {base_n} at the base, {int(data.get('added') or 0)} new)",
                      f"read the task's attempts with `locks show {event['task_id']}`: the comparison with the base "
                      "let a failure of the work pass", event["task_id"], task["repo"] if task else None)
    if before:
        yield _history("preexisting-that-was-not", before, None)


def problems_open_after_pass(store: Store, **_) -> Iterable[Finding]:
    """A problem still open whose own failures came from a task that has since passed: the
    pass should have solved it (`orchestrate._remember`), and the memory keeps showing it to
    workers as a dead end with no fix."""
    before: list[int] = []
    for problem in store.recent_problems(limit=200):
        if problem["state"] == "solved":
            continue
        tasks = {f["task_id"] for f in problem["fixes"] if f["outcome"] == "failed" and f["task_id"]}
        worked_after = {f["task_id"] for f in problem["fixes"] if f["outcome"] == "worked"}
        for task_id in sorted(tasks - worked_after):
            task = store.get_task(int(task_id))
            if task is None or task["status"] != "done":
                continue
            ended = store.final_event(int(task_id))
            if ended is None or ended["ts"] < max(f["created_at"] for f in problem["fixes"]):
                continue
            if _fixed("problem-open-after-pass", _version_of(store, int(task_id))):
                before.append(int(task_id))
                continue
            yield Finding("problem-open-after-pass", SUSPECT,
                          f"problem #{problem['id']} ({problem['title'][:60]}) is still {problem['state']} though "
                          f"#{task_id}, whose attempts opened it, passed",
                          f"`locks memory record --problem {json.dumps(problem['title'])} --fix … --outcome worked`",
                          int(task_id), problem["repo"])
            break
    if before:
        yield _history("problem-open-after-pass", before, None)


def flake_fixes_checked_once(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """Work that fixes a flaky test, passed with a check, whose check ran once: one green run
    says nothing of a flake (`orchestrate.flake_fix_runs`)."""
    from locks.orchestrate import flake_fix_runs

    before: list[int] = []
    for event in store.events_of_kinds(["finished"], since=since):
        data = event["data"]
        task = store.get_task(event["task_id"])
        check = (data.get("verdict") or {}).get("check") if data.get("status") == "done" else None
        if task is None or not isinstance(check, Mapping) or int(check.get("runs") or 1) > 1 or \
                flake_fix_runs(str(task.get("kind") or ""), str(task.get("body") or task.get("title") or "")) <= 1:
            continue
        if _fixed("flake-fix-checked-once", data.get("version")):
            before.append(task["id"])
            continue
        yield Finding("flake-fix-checked-once", SUSPECT,
                      f"#{task['id']} fixes a flaky test and passed on one run of `{check.get('command')}`",
                      f"run its check several times on its branch before merging it (`locks show {task['id']}`)",
                      task["id"], task["repo"])
    if before:
        yield _history("flake-fix-checked-once", before, None)


def turns_under_the_wall(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """Long attempts that kept far fewer turns than their time allows: the CLI's count was
    restarted (a session that compacted) and the transcript was not read
    (`launch.counted_turns`)."""
    before: list[int] = []
    for event in store.events_of_kinds(["attempt_finished"], since=since):
        data = event["data"]
        wall, turns = float(data.get("wall_s") or 0), int(data.get("turns") or 0)
        if wall < WALL_LONG_S or turns * WALL_PER_TURN_S >= wall:
            continue
        if _fixed("turns-under-the-wall", data.get("version")):
            before.append(event["task_id"])
            continue
        task = store.get_task(event["task_id"])
        yield Finding("turns-under-the-wall", SUSPECT,
                      f"#{event['task_id']} attempt {data.get('seq')} ran {round(wall / 60)}m and kept {turns} turn(s)",
                      "its worker transcript holds the real count: `locks show` reads the attempt",
                      event["task_id"], task["repo"] if task else None)
    if before:
        yield _history("turns-under-the-wall", before, None)


def pickups_nobody_saw(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """Work that waited `PICKUP_UNSEEN` or more to be picked up while nothing ran in its lane and
    no dispatch decision or usage limit held it: no worker was busy, so the wait was no worker's.
    It was queued while the lane's last dispatcher was leaving, and the kick that found that one's
    lock held was lost (`dispatch.wake`)."""
    events = store.events_of_kinds(["attempt_started", "attempt_finished", "finished"], since=since)
    began: dict[tuple[int, object], datetime] = {}
    busy: list[tuple[int, datetime, datetime]] = []  # (task, from, to) of every attempt that ran
    for event in events:
        at, key = _ts(event["ts"]), (event["task_id"], event["data"].get("seq"))
        if at is None:
            continue
        if event["kind"] == "attempt_started":
            began[key] = at
        elif event["kind"] == "attempt_finished" and key in began:
            busy.append((event["task_id"], began.pop(key), at))
    busy += [(task_id, start, datetime.max.replace(tzinfo=UTC)) for (task_id, _), start in began.items()]
    before: list[int] = []
    for event in events:
        data, task = event["data"], None
        waited = data.get("pickup_s") if event["kind"] == "finished" else None
        if not isinstance(waited, int | float) or isinstance(waited, bool):
            continue
        held = sum(float(data.get(k) or 0) for k in ("pickup_conflict_s", "pickup_limit_s"))
        task = store.get_task(event["task_id"])
        dispatched = _ts((store.last_event(event["task_id"], "dispatched") or {}).get("ts"))
        if task is None or dispatched is None or waited - held < PICKUP_UNSEEN.total_seconds():
            continue
        start = dispatched - timedelta(seconds=float(waited))
        repo = task["repo"]
        if any(other != task["id"] and a < dispatched and b > start
               and (store.get_task(other) or {}).get("repo") == repo for other, a, b in busy):
            continue
        if _fixed("pickup-nobody-saw", data.get("version")):
            before.append(task["id"])
            continue
        yield Finding("pickup-nobody-saw", SUSPECT,
                      f"#{task['id']} waited {int(waited // 60)} min to be picked up while nothing ran in its lane",
                      "a dispatcher left as it was queued; `locks doctor` names it again if it recurs", task["id"],
                      repo)
    if before:
        yield _history("pickup-nobody-saw", before, None)


#: How many spec ids a finding about unlinked specs names; the rest are counted.
SPECS_SHOWN = 5


def _checkouts(store: Store, since: str | None, livespec: Any) -> list[Path]:
    """The repositories recent runs were in, each once: the checkout a run's folder is in, or each
    repository inside a folder of them (`Livespec.roots`, never the folder)."""
    folders: dict[str, None] = {}
    for event in store.events_of_kinds(["planned"], since=since):
        task = store.get_task(event["task_id"])
        if task and task.get("cwd"):
            folders[str(task["cwd"])] = None
    # Each folder once: a store of months holds thousands of runs in a handful of folders, and each
    # folder's repositories are read with git (`locks doctor` took 33 s on 3000 runs, one process each).
    found: dict[Path, None] = {}
    for folder in folders:
        where = Path(folder)
        if where.is_dir():
            found.update(dict.fromkeys(livespec.roots(where)))
    return list(found)


def indexes_not_ready(store: Store, *, since: str | None, livespec: Any = None, **_) -> Iterable[Finding]:
    """A repository recent runs were in whose livespec index is missing, unreadable or older than its last
    commit: its workers open files to find their way, at 300-400k tokens a UI task, instead of reading
    the specs and symbols. A task indexes it before its worker starts (`livespec_wait`), so one found
    here is one that never got that far: livespec cannot run, or its index fails."""
    if livespec is None:
        return
    for root in _checkouts(store, since, livespec):
        status = livespec.inspect(root)
        key = repo_key(root)
        fix = f"locks index-livespec {root}" + ("" if livespec.server() is not None
                                                 else " (needs livespec, or `uv` to run the pinned release)")
        if status.state == ABSENT:
            yield Finding("index-missing", SUSPECT, f"{root.name}: no livespec index ({status.detail})", fix,
                          None, key)
        elif status.state == UNREADABLE:
            yield Finding("index-unreadable", SUSPECT, f"{root.name}: its livespec index cannot be read "
                          f"({status.detail})", fix, None, key)
        elif status.stale:
            yield Finding("index-stale", SUSPECT, f"{root.name}: its livespec index is older than the last "
                          f"commit ({status.detail})", fix, None, key)


def specs_with_no_code(store: Store, *, since: str | None, livespec: Any = None, **_) -> Iterable[Finding]:
    """A repository recent runs were in whose livespec index holds specs that no code implements: a worker
    that asks livespec for a spec's code finds none, and falls back to opening files. `@spec:<id>` comments
    on the functions that implement each, and an index again, link them."""
    if livespec is None:
        return
    for root in _checkouts(store, since, livespec):
        loose = livespec.unlinked_specs(livespec.inspect(root))
        if loose:
            shown = ", ".join(spec_id for spec_id, _ in loose[:SPECS_SHOWN])
            more = f" and {len(loose) - SPECS_SHOWN} more" if len(loose) > SPECS_SHOWN else ""
            what = f"{root.name}: {len(loose)} spec(s) in the livespec index have no code linked ({shown}{more})"
            fix = (f"put `@spec:<id>` on the functions that implement each, then `locks index-livespec {root}`; "
                   "livespec's `audit_coverage` lists them")
            yield Finding("specs-with-no-code", SUSPECT, what, fix, None, repo_key(root))


def died_after_a_pass(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """A run that died after its attempt had passed: the work was done and the ending killed it
    (locks committing nothing, when the worker had committed its own work and only its tools'
    artifacts were left, `isolate.finish`)."""
    before: list[int] = []
    for event in store.events_of_kinds(["finished"], since=since):
        data = event["data"]
        if (data.get("stop") or {}).get("cause") != "died":
            continue
        tried = [e for e in store.events_of(event["task_id"], "attempt_finished") if e["id"] < event["id"]]
        if not tried or not tried[-1]["data"].get("passed"):
            continue
        task = store.get_task(event["task_id"]) or {}
        if _fixed("died-after-a-pass", data.get("version")):
            before.append(event["task_id"])
            continue
        yield Finding("died-after-a-pass", SUSPECT,
                      f"#{event['task_id']} died after its attempt {tried[-1]['data'].get('seq')} passed: its ending "
                      f"failed, not its work ({(data.get('stop') or {}).get('reason')})",
                      f"read hook-errors.log for the trace, then `locks resume {event['task_id']} --unattended`",
                      event["task_id"], task.get("repo"))
    if before:
        yield _history("died-after-a-pass", sorted(set(before)), None)


def resumed_after_it_landed(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """A task whose kept work had landed that was resumed on its own anyway: SessionStart resumed a
    death (`dispatch.recover`) before it read what landed (`stops.landed`), so a worker ran on work
    already in the checkout's branch while the task was dismissed as landed."""
    before: list[int] = []
    for event in store.events_of_kinds(["finished"], since=since):
        data = event["data"]
        if (data.get("stop") or {}).get("cause") != "landed":
            continue
        earlier = [e["id"] for e in store.events_of(event["task_id"], "finished") if e["id"] < event["id"]]
        after_death = max(earlier, default=0)
        resumed = [e for e in store.events_of(event["task_id"], "auto_resumed")
                   if after_death < e["id"] < event["id"]]
        if not resumed:
            continue
        task = store.get_task(event["task_id"]) or {}
        if _fixed("resumed-after-it-landed", data.get("version")):
            before.append(event["task_id"])
            continue
        yield Finding("resumed-after-it-landed", SUSPECT,
                      f"#{event['task_id']} was resumed on its own after its work had landed: a worker ran on work "
                      "already merged", "nothing to undo; say it if it happens again on this release",
                      event["task_id"], task.get("repo"))
    if before:
        yield _history("resumed-after-it-landed", sorted(set(before)), None)


def swept_before_its_pid(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """A locks run stopped as dead while it had no pid: a run was marked running and given its pid in
    two writes, and a sweep between them read it as dead (`no sign of life`, no process named). The run
    went on unseen; a resume that started so died with nobody noticing."""
    before: list[int] = []
    for event in store.events_of_kinds(["finished"], since=since):
        stop = event["data"].get("stop") or {}
        if stop.get("cause") != "died" or not str(stop.get("reason") or "").startswith("no sign of life"):
            continue
        if "its process (" in str(stop.get("reason")):
            continue
        task = store.get_task(event["task_id"]) or {}
        if task.get("source") != "locks":
            continue
        if _fixed("swept-before-its-pid", event["data"].get("version")):
            before.append(event["task_id"])
            continue
        yield Finding("swept-before-its-pid", SUSPECT,
                      f"#{event['task_id']} was stopped as dead with no process on record: a run between its "
                      "start and its pid", f"`locks resume {event['task_id']} --unattended` if it should go on",
                      event["task_id"], task.get("repo"))
    if before:
        yield _history("swept-before-its-pid", sorted(set(before)), None)


def advised_again_in_a_prompt(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """locks' own compaction advice delivered more than once in one prompt of the person's, or
    over a compaction the session asked for that had not finished: each one a turn at a full
    context (`compaction.pending`, `_advised_this_prompt`)."""
    advised: dict[int, list[dict]] = {}
    for event in store.events_of_kinds(["compaction_delivered"], since=since):
        if event["data"].get("how") == "advise":
            advised.setdefault(event["task_id"], []).append(event)
    before: list[int] = []
    for task_id, events in advised.items():
        if len(events) < 2:
            continue
        if _fixed("advised-again-in-a-prompt", events[-1]["data"].get("locks")):
            before.append(task_id)
            continue
        task = store.get_task(task_id)
        yield Finding("advised-again-in-a-prompt", SUSPECT,
                      f"compaction advice was delivered {len(events)} times in the prompt of #{task_id}",
                      "nothing to do now: say it to locks' maintainers with `locks trace`'s compaction events",
                      task_id, task["repo"] if task else None)
    if before:
        yield _history("advised-again-in-a-prompt", sorted(before), None)


def habit_reviews_unapplied(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """A habits review that passed and whose proposals locks never applied: no `habit_installed` or
    `habit_refused` event on it (`habits.apply_reviews` looked for reviews by the `queue` source a
    started task no longer has; from 0.85.0 it finds them by their options and says every verdict
    it could not read)."""
    before: list[int] = []
    for event in store.events_of_kinds(["finished"], since=since):
        if event["data"].get("status") != "done":
            continue
        task = store.get_task(event["task_id"])
        try:
            reviewed = json.loads((task or {}).get("options") or "{}").get("habit_review")
        except (TypeError, ValueError):
            reviewed = None
        if not task or not reviewed:
            continue
        kinds = {e["kind"] for e in store.events(task_id=task["id"], limit=500)}
        if kinds & {"habit_installed", "habit_refused"}:
            continue
        if _fixed("habit-review-unapplied", event["data"].get("version")):
            before.append(task["id"])
            continue
        yield Finding("habit-review-unapplied", SUSPECT,
                      f"habits review #{task['id']} passed and locks never applied it: no habit_installed or "
                      "habit_refused event",
                      "the next settle applies it (a hook, the dispatcher, or `locks endings`); with `habit_auto` "
                      f"off, `locks show {task['id']}` has the proposals", task["id"], task["repo"])
    if before:
        yield _history("habit-review-unapplied", sorted(before), None)


def tools_logged_off(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """An attempt whose record says none of its worker's tool calls reached the log
    (`attempt_finished.tools_logged` false) while the store holds them: the record was read
    before the hooks' log was loaded into the store, which happened only when the worker was
    handed a capability (from 0.89.0 the log is loaded once before anything of the attempt is
    read). `locks stats` counted such attempts as not recorded, and the evidence gate read
    their calls as none."""
    before: list[int] = []
    for event in store.events_of_kinds(["attempt_finished"], since=since):
        data = event["data"]
        if data.get("tools_logged") is not False:
            continue
        held = sum(store.tool_counts(event["task_id"], int(data.get("seq") or 0), "").values())
        if not held:
            continue
        if _fixed("tools-logged-off", data.get("version")):
            before.append(event["task_id"])
            continue
        task = store.get_task(event["task_id"])
        yield Finding("tools-logged-off", SUSPECT,
                      f"#{event['task_id']} attempt {data.get('seq')} says no tool call was logged, and the store "
                      f"holds {held} of its worker's",
                      "the record was read before the hooks' log was loaded; `locks stats` counts the attempt as "
                      "not recorded", event["task_id"], task["repo"] if task else None)
    if before:
        yield _history("tools-logged-off", sorted(before), None)


def problems_opened_by_a_race(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """A problem a task opened with a pass and no failure of its own: its only attempts beside the
    pass were a race's other try stopped because the pass came first (`attempt_finished.raced` and
    `stopped`), which found nothing. Before 0.90.0 a pass with any earlier attempt was read as a pass
    after a failure."""
    before: list[int] = []
    for event in store.events_of_kinds(["finished"], since=since):
        task_id = int(event["task_id"])
        if event["data"].get("status") != "done":
            continue
        ends = [e["data"] for e in store.events(task_id=task_id, limit=500) if e["kind"] == "attempt_finished"]
        stopped = [e for e in ends if e.get("raced") and e.get("stopped")]
        if not stopped or any(not e.get("passed") and e not in stopped for e in ends):
            continue
        opened = [p for p in store.recent_problems(limit=200)
                  if p.get("fixes") and all(f.get("outcome") == "worked" for f in p["fixes"])
                  and p["fixes"][0].get("task_id") == task_id]
        if not opened:
            continue
        if _fixed("problem-opened-by-a-race", event["data"].get("version")):
            before.append(task_id)
            continue
        task = store.get_task(task_id)
        yield Finding("problem-opened-by-a-race", SUSPECT,
                      f"#{task_id} passed beside a race's other try and opened problem #{opened[0]['id']} with no "
                      "failure of its own",
                      "the problem is solved and harmless; a race's other try is no failure of the work",
                      task_id, task["repo"] if task else None)
    if before:
        yield _history("problem-opened-by-a-race", sorted(before), None)


def clean_reviews_read_nothing(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """A blind review that passed with no finding while its own calls show it read nothing
    (`attempt_finished.behavior.read_calls` 0, its calls logged): a worker that never read the diff
    cannot have found nothing, and `workflows` read its empty `findings` as a clean review. From
    0.91.0 such an answer is held back once (a `review_unread` event at the attempt's `seq`); one
    without that event passed unread. An attempt recorded before `read_calls` existed, or whose
    calls never reached the log, is unknown and no finding."""
    held = {(int(e["task_id"]), int(e["data"].get("seq") or 0)) for e in store.events_of_kinds(["review_unread"])}
    before: list[int] = []
    for event in store.events_of_kinds(["attempt_finished"], since=since):
        data, task_id = event["data"], int(event["task_id"])
        if not data.get("passed") or data.get("tools_logged") is not True:
            continue
        if (data.get("behavior") or {}).get("read_calls") != 0 or (task_id, int(data.get("seq") or 0)) in held:
            continue
        task = store.get_task(task_id)
        if not task or task["kind"] not in behavior.REVIEW_KINDS:
            continue
        ended = store.last_event(task_id, "finished")
        if not ended or ended["data"].get("status") != "done" or ended["data"].get("findings"):
            continue
        if _fixed("clean-review-read-nothing", data.get("version")):
            before.append(task_id)
            continue
        yield Finding("clean-review-read-nothing", SUSPECT,
                      f"#{task_id} attempt {data.get('seq')} passed a review with no finding and its calls show it "
                      "read nothing, and nothing held it back",
                      f"`locks show {task_id}`: the review is no evidence the change is clean; run it again",
                      task_id, task["repo"])
    if before:
        yield _history("clean-review-read-nothing", sorted(before), None)


RULES: tuple[Callable[..., Iterable[Finding]], ...] = (
    lanes_paused_on_ended, running_with_no_process, queued_with_no_dispatcher, held_behind_gone, wait_judged_on_gone,
    resumes_that_never_ran,
    parts_that_lost_their_mark, joins_short_of_their_parts, attempts_with_no_session, turns_under_the_kind,
    format_failures_that_climbed, fix_rounds_off_their_base, candidates_on_trial_at_once, revisions_judged_twice,
    trials_never_sampled, notes_proven_unbriefed, preexisting_that_was_not, problems_open_after_pass,
    flake_fixes_checked_once, turns_under_the_wall, pickups_nobody_saw, died_after_a_pass, resumed_after_it_landed,
    swept_before_its_pid, indexes_not_ready, specs_with_no_code, advised_again_in_a_prompt,
    habit_reviews_unapplied, tools_logged_off, problems_opened_by_a_race, clean_reviews_read_nothing,
)


def _livespec() -> Any:
    """The adapter the index rules read through, or None where the `livespec` setting is off."""
    from locks.adapters.livespec import Livespec

    return Livespec() if config.enabled("livespec", os.environ) else None


def check(store: Store, *, repo: str | None = None, days: int = WINDOW_DAYS, now: datetime | None = None,
          alive: Callable[[int | None], bool] | None = None, livespec: Any = None) -> list[Finding]:
    """Every finding of every rule, broken first; `repo` keeps one repository's (and the
    ones no repository owns)."""
    from locks import flow

    now = now or datetime.now(UTC)
    since = (now - timedelta(days=days)).isoformat() if days else None
    livespec = livespec if livespec is not None else _livespec()
    found: list[Finding] = []
    for rule in RULES:
        found.extend(rule(store, now=now, since=since, alive=alive or flow.alive, livespec=livespec))
    if repo is not None:
        found = [f for f in found if f.repo in (repo, None)]
    return sorted(found, key=lambda f: ((BROKEN, SUSPECT, FIXED).index(f.severity), f.rule, f.task_id or 0))


def text(found: list[Finding]) -> str:
    lines = []
    if not any(f.severity != FIXED for f in found):
        lines.append("locks doctor: nothing broken, nothing suspect.")
    for severity in (BROKEN, SUSPECT):
        mine = [f for f in found if f.severity == severity]
        if mine:
            lines.append(f"{severity} ({len(mine)}):")
            lines += [f"  [{f.rule}] {f.what}\n      → {f.fix}" for f in mine]
    if history := [f for f in found if f.severity == FIXED]:
        lines.append("fixed since, history only:")
        lines += [f"  [{f.rule}] {f.what}" for f in history]
    return "\n".join(lines)
