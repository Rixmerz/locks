"""Why a task stopped short of a pass, who made that call, and what continues it.

A card that only says "blocked" asks a person to act without saying on what,
and "cancelled" does not say whether they did it. Every place that ends a task
without a pass — the run's own loop, `locks cancel`, the UI, the dispatcher,
the sweep — writes one account on the task's `finished` event: the cause, who
or what made the call, the reason in full, and the command that continues it
when there is one.

Tasks that ended before these accounts existed get one read back from what
their events and attempts already say (`recovered`), marked as such: an
account locks did not record is never presented as if it had.

Light on purpose: the UI, the hooks and the CLI read it.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from locks import after, allow

#: Who made the call, as the board says it. `you` is the only one a person did
#: by hand; `settings` are theirs too, set before the run. `budget` is no cause a
#: run records any more (cost is measured, never a brake): its words stay for the
#: stops older versions wrote.
WHO = {
    "you": "you",
    "settings": "your permission settings",
    "budget": "a dollar budget an older locks set",
    "environment": "the environment",
    "network": "the network",
    "worker": "the worker's verdict",
    "locks": "locks' rules",
    "outside": "a signal from outside locks",
    "orchestrator": "the orchestrating session",
    "workflow": "the workflow's question",
    "unknown": "not recorded",
}

#: cause → (status it ends in, who made the call).
CAUSES = {
    "permission": ("blocked", "settings"),
    "environment": ("blocked", "environment"),
    "network": ("blocked", "network"),
    # The check's tests kept timing out on a loaded machine, through locks' waits.
    "load": ("blocked", "environment"),
    # The account's usage limit turned its session away: it resumes once the limit resets.
    "limit": ("blocked", "environment"),
    # locks' check failed and the same check at the base could not say what fails there: nothing
    # shows the work caused it, so it is neither charged to the work nor climbed.
    "base_unknown": ("blocked", "locks"),
    # Two cells left the same failing tests: a third would repeat them.
    "same_failures": ("blocked", "locks"),
    "budget": ("blocked", "budget"),
    "missing_dir": ("blocked", "environment"),
    # The preflight read that its check cannot start here: no worker ran, no attempt counted.
    "preflight": ("blocked", "environment"),
    "approval": ("needs_approval", "locks"),
    "spec": ("replan", "worker"),
    "turns": ("replan", "locks"),
    # It outgrew its turns as several pieces: its work is kept, and `locks split` starts the pieces on it.
    "split": ("replan", "locks"),
    "ladder": ("replan", "locks"),
    "exhausted": ("failed", "locks"),
    "attempts": ("failed", "locks"),
    "cancelled": ("cancelled", "you"),
    "signal": ("cancelled", "outside"),
    "died": ("interrupted", "locks"),
    "turn": ("interrupted", "outside"),
    "crashed": ("failed", "locks"),
    "converged": ("failed", "worker"),
    "review": ("blocked", "locks"),
    # The work is done and kept, and the worker names one point that needs a decision.
    "decision": ("blocked", "worker"),
    "dismissed": ("dismissed", "you"),
    "superseded": ("dismissed", "locks"),
    # Its kept branch's work is already on the checkout's HEAD: nothing waits on it.
    "landed": ("dismissed", "locks"),
    # A plan whose parts cannot run side by side (their surfaces may touch): its pass is taken back.
    "parts": ("replan", "locks"),
    # Held behind work (`--after`) that left the board without passing: it never started.
    after.ABANDONED: ("cancelled", "locks"),
    # A workflow's question step: it waits for the person's answer (`workflows.answer`); no worker ran.
    "question": ("blocked", "workflow"),
}

#: Endings a person may dismiss: they wait on someone, and nothing else takes
#: them off the board but a resume.
DISMISSABLE = ("failed", "blocked", "replan", "needs_approval", "interrupted")

#: Stops only a person clears: a refused command needs their rule, a cell their
#: approval, a directory that is gone their hands. Every other stop is the
#: orchestrator's to decide (`locks.orchestrator`): resume it, rewrite it, or
#: dismiss it with why.
#: Where the account's usage limit is kept until it resets (`Store.meta`, an ISO time): no
#: queued work starts before then (`flow.pick`), since every session meets it.
LIMIT_META = "usage_limit_until"

PERSON_ONLY = ("permission", "approval", "missing_dir", "question")

#: The attempts after which a resume nobody approved is refused: two runs' worth
#: (`orchestrate.Options.max_attempts`, kept equal by a test). `cli` refuses it.
UNATTENDED_ATTEMPTS = 2 * 6

#: The turns a task that outgrew its turns is resumed with.
RESUME_TURNS = 120

#: How a person's cancel arrived, as the account says it.
CANCEL_VIA = {
    "ui": "from the UI",
    "cli": "with `locks cancel`",
    "queue": "with `locks queue rm`",
    "keyboard": "with Ctrl-C",
}

#: How a person's dismissal arrived.
DISMISS_VIA = {
    "ui": "from the UI",
    "cli": "with `locks dismiss`",
    "mod": "from the session's locks band",
    "tanka": "from Tanka's page",
    "session": "as the orchestrating session's call",
}


@dataclass(frozen=True)
class Stop:
    cause: str
    reason: str
    #: Overrides the cause's usual author, e.g. a ladder that ran out is locks'
    #: call but a cancel's author is whoever sent it.
    by: str = ""
    #: The tool calls the settings refused, as they were made.
    denied: tuple[str, ...] = ()
    #: The `--allow` rules that let them through, one per program.
    allow: tuple[str, ...] = ()
    #: The cell a person must approve before the run goes on.
    next_cell: str | None = None
    #: The attempt it stopped after, when one ran.
    attempt: int | None = None
    #: What the last worker said, beside locks' reason: "the task is wrong" is
    #: only actionable next to what it found wrong.
    account: str = ""
    #: Read back from an older task's events, not recorded when it stopped.
    recovered: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return CAUSES.get(self.cause, ("failed", "locks"))[0]

    @property
    def who(self) -> str:
        return self.by or CAUSES.get(self.cause, ("failed", "unknown"))[1]

    def line(self) -> str:
        return f"stopped by {WHO.get(self.who, self.who)}: {self.reason}"

    def data(self) -> dict[str, Any]:
        """For the `finished` event; `read` turns it back into a Stop."""
        out: dict[str, Any] = {"cause": self.cause, "by": self.who, "reason": self.reason}
        if self.denied:
            out["denied"] = list(self.denied)
        if self.allow:
            out["allow"] = list(self.allow)
        for key in ("next_cell", "attempt"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        if self.account:
            out["account"] = self.account
        if self.recovered:
            out["recovered"] = True
        return {**self.extra, **out}


def read(data: dict[str, Any] | None) -> Stop | None:
    if not isinstance(data, dict) or not data.get("cause"):
        return None
    known = {"cause", "by", "reason", "denied", "allow", "next_cell", "attempt", "account", "recovered"}
    return Stop(str(data["cause"]), str(data.get("reason") or ""), by=str(data.get("by") or ""),
                denied=tuple(data.get("denied") or ()), allow=tuple(data.get("allow") or ()),
                next_cell=data.get("next_cell"), attempt=data.get("attempt"),
                account=str(data.get("account") or ""), recovered=bool(data.get("recovered")),
                extra={k: v for k, v in data.items() if k not in known})


def next_step(task_id: int, stop: Stop, *, resumable: bool = True) -> str | None:
    """The command that continues the task, or None when none would: a task the
    worker found wrong is rewritten, and one that never ran cannot resume. A
    dismissed task resumes as what it was, so one that was replanned does not."""
    if resumable and (widen := stop.extra.get("widen")) and stop.cause in ("spec", "decision"):
        # Its point was its surfaces: the resume adds the paths it needs and keeps its diagnosis.
        return f"locks resume {task_id}" + "".join(f" --surface {shlex.quote(str(p))}" for p in widen)
    if stop.cause == "question":
        # A question is answered, never resumed: no worker runs it.
        return str(stop.extra.get("answer") or "") or None
    if not resumable or stop.cause in ("spec", "ladder", "missing_dir", "converged", "decision", "landed"):
        return None
    if stop.cause == "superseded" and stop.extra.get("retry"):
        return None  # its step was queued again: the retry is the one that goes on
    if (stop.cause in ("dismissed", "superseded") and stop.extra.get("was") == "replan"
            and stop.extra.get("had_cause") not in ("turns", "split")):
        return None
    base = f"locks resume {task_id}"
    if stop.cause == "turns":
        return f"{base} --max-turns {RESUME_TURNS}"
    if stop.cause == "split":
        return f"locks split {task_id}"
    rules = stop.allow or allow.from_refusals(stop.denied)
    if stop.cause == "permission" and rules:
        return base + "".join(f" --allow {shlex.quote(rule)}" for rule in rules)
    if stop.cause == "approval":
        return f"{base} --allow-approval"
    return base


def for_person(stop: Stop | None, *, tried: int = 0, limit: int | None = None) -> bool:
    """Whether a stop waits on the person rather than on the orchestrator: one of
    `PERSON_ONLY`, or a task that already had the attempts after which a resume
    nobody approved is refused (`limit`, `cli.UNATTENDED_ATTEMPTS`)."""
    if stop is None:
        return True
    return stop.cause in PERSON_ONLY or (limit is not None and tried >= limit)


def waits_on(store: Any, task: dict[str, Any]) -> str:
    """"person" or "orchestrator": who a task that stopped waits on."""
    attempts = store.attempts(task["id"])
    return "person" if for_person(of(store, task), tried=len(attempts) - lost_tries(store, task["id"]),
                                  limit=UNATTENDED_ATTEMPTS) else "orchestrator"


def lost_tries(store: Any, task_id: int) -> int:
    """The tries that said nothing about the work, and count toward no attempt ceiling: the
    network dropped under them, the machine's load timed their check out, or the account's
    usage limit turned their session away."""
    dropped = sum(1 for e in store.events_of(task_id, "moved")
                  if (e["data"] or {}).get("network") or (e["data"] or {}).get("load"))
    return dropped + sum(1 for e in store.events_of(task_id, "attempt_finished")
                         if (e["data"] or {}).get("limited") == "usage")


def what_to_do(stop: Stop) -> str:
    """What a person does before the command, in one sentence."""
    if stop.cause == "dismissed" and stop.who == "orchestrator":
        return "nothing: the orchestrating session took it off the board; resume it if you want it after all"
    if stop.cause == "superseded" and stop.extra.get("retry"):
        return f"nothing: its step was queued again as #{stop.extra.get('by_task')}, which is the one that goes on"
    return {
        "permission": "allow what was refused for this run, or add --keep to keep it for every task here, "
                      "then resume",
        "environment": "fix what failed to run, then resume",
        "preflight": "its check cannot start here, so no worker ran and no attempt was counted: apply the fix "
                     "its reason names, then resume",
        "network": "the network dropped and stayed down through locks' waits; its work is kept: resume it once "
                   "the network is back",
        "limit": "the account's usage limit stopped it; its work is kept: locks resumes it once the limit "
                 "resets" + (f" ({stop.extra['resets_at']})" if stop.extra.get("resets_at") else ""),
        "load": "its check's tests kept timing out while the machine was loaded, after the worker's own run "
                "passed; its work is kept: resume it when the machine is quieter",
        "base_unknown": "its check failed, and the same check at the base could not say what fails there (it "
                        "timed out or could not run), so nothing shows the work caused it; its work is kept: resume "
                        "it, and the base's run is tried again with the time this check was seen to take",
        "same_failures": "two cells left the same failing tests, so a third would repeat them; its work is kept: "
                         "read those tests (they may fail here whatever the work does), then resume or rewrite it",
        "budget": "an older locks stopped it on a dollar budget, which no longer exists: resume it",
        "missing_dir": "the task's directory is gone: restore it, or queue the task again where it now lives",
        "question": "its workflow asks the person (the question is its reason): answer with one of its options "
                    + "(" + ", ".join(str(o) for o in stop.extra.get("options") or ()) + ")",
        "approval": "approve the next cell by resuming with --allow-approval",
        "spec": "the task as written cannot be done: rewrite or split it as a new task",
        "turns": "the work outgrew a worker's turns, not the task: resume it with more turns (what it did "
                 "stays), or split what is left into smaller tasks",
        "split": "it is several pieces and outgrew one worker's turns; its work is kept on its branch. Read the "
                 "pieces `locks split` proposes, then start them on that work with `--queue` (what was queued "
                 "after it waits for the last piece); a resume gives it more turns as one piece instead",
        "ladder": "no stronger model is left: rewrite the task, or read the attempts for what kept failing",
        "exhausted": "every cell failed: read the attempts, then resume or rewrite it",
        "attempts": "the attempt limit was reached: read the attempts, then resume",
        "cancelled": "resume it if you still want it",
        "signal": "resume it if you still want it",
        "died": "its process went away mid-run: resume it",
        "turn": "nothing to resume: this was a turn of a session, not work locks runs; ask again in the session "
                "if its answer was lost",
        "crashed": "locks itself failed: the reason has the error; resume once it is fixed",
        "converged": "the findings are the answer: read them in the worker's account and act on them; "
                     "resuming would only repeat them",
        "decision": "its work is done and kept on its branch, and one point needs a decision (quoted in its "
                    "reason): decide it, then merge the branch and send what the decision asks as a new task",
        "review": "the independent check kept finding problems: resume it: the worker is briefed with the "
                  "findings, and the check reads them first",
        "dismissed": "nothing: you took it off the board; resume it if you want it after all",
        "superseded": "nothing: a later task with the same text passed; resume it if you want it after all",
        "landed": "nothing: its work is already in the checkout's branch",
        "parts": "its parts may touch the same files, so they cannot run side by side: plan again (resume it, "
                 "or `locks workflow retry` its step)",
        after.ABANDONED: "it never started: the work it waited for left the board without passing. Resume it to "
                         "start it on the base, queue it again after other work, or leave it",
    }.get(stop.cause, "read the attempts")


def recovered(status: str, *, result: str = "", failure: str | None = None, move_reason: str = "",
              denied: tuple[str, ...] = (), next_cell: str | None = None, finished_reason: str = "",
              attempt: int | None = None) -> Stop | None:
    """The best account an older task's records give, marked `recovered`."""
    def made(cause: str, reason: str, **kw: Any) -> Stop:
        return Stop(cause, reason or status, recovered=True, attempt=attempt, **kw)

    reason = move_reason or result
    if status == "blocked":
        if failure == "permission" or denied:
            return made("permission", reason, denied=denied)
        if "budget" in result and result.startswith("the $"):
            return made("budget", result)
        if "directory no longer exists" in result:
            return made("missing_dir", result)
        return made("environment", reason)
    if status == "needs_approval":
        return made("approval", reason, next_cell=next_cell)
    if status == "replan":
        if failure in ("spec_bug", "architecture_bug"):
            return made("spec", reason)
        return made("turns" if failure == "turns_exhausted" else "ladder", reason)
    if status == "failed":
        return made("attempts" if "attempts without a pass" in result else "exhausted", reason)
    if status == "cancelled":
        return made("cancelled", result or "cancelled", by="unknown")
    if status == "interrupted":
        return made("died", finished_reason or result or "it stopped with no sign of life")
    return None


def record(store: Any, task_id: int, stop: Stop, *, result: bool = True, **fields: Any) -> None:
    """End a task that stopped outside a run's own loop (a cancel before it
    started, the dispatcher, the sweep): its status, its result and the account."""
    store.update_task(task_id, status=stop.status, **({"result": stop.reason} if result else {}), **fields)
    store.add_event(task_id, "finished", status=stop.status, reason=stop.reason, stop=stop.data())


#: The command that dismisses a task, as every notice that offers it prints it.
DISMISS_COMMAND = "locks dismiss {id} --why '<why>'"


def dismiss_command(task_id: Any) -> str:
    """`locks dismiss N --why '<why>'`: the syntax a notice tells a session to type."""
    return DISMISS_COMMAND.format(id=task_id)


def dismiss(store: Any, task: dict[str, Any], *, via: str, why: str = "") -> Stop | None:
    """Take a task that waits on a person off the board, on their word. Its result
    and attempts stay; the account says what it had stopped on, and why it was
    dismissed when that was given. None when the task waits on no one. Work queued
    after other work (`after`) waits on it, and is dismissed off the queue; work
    queued after this task will never be released by it, and leaves the queue with
    it (`after.abandon`), its ids kept on the account (`took_along`)."""
    if task["status"] not in DISMISSABLE and not after.held(task):
        return None
    before = of(store, task)
    by = "orchestrator" if via == "session" else "you"
    said = ("the orchestrating session dismissed it" if by == "orchestrator"
            else f"you dismissed it {DISMISS_VIA.get(via, via)}")
    why = " ".join(str(why or "").split())
    if why:
        said = f"{said}: {why}"
    along = [] if after.held(task) else after.abandon(
        store, {"task": int(task["id"])}, f"it waited for #{task['id']} to pass, which left the board: {said}")
    stop = Stop("dismissed", said, by=by,
                attempt=before.attempt if before else None,
                extra={"via": via, "was": task["status"], **({"why": why} if why else {}),
                       **({"took_along": along} if along else {}),
                       **({"had": before.reason, "had_cause": before.cause} if before else {})})
    record(store, task["id"], stop, result=False)
    # The person did it: no session is told of it as an ending.
    store.add_event(task["id"], "reported", via="dismiss")
    store.release_lane(task.get("repo"), task["id"])
    return stop


def supersede(store: Any, task: dict[str, Any]) -> list[int]:
    """Copies of a task that passed, with the same text in the same repository,
    that stopped short of a pass: the work they waited on is done, so they no
    longer wait on anyone. The ids taken off the board."""
    body = " ".join(str(task.get("body") or "").split())
    if not body or not task.get("repo"):
        return []
    gone = []
    for other in store.list_tasks(repo=task["repo"], status=DISMISSABLE, source=("locks",), limit=500):
        if other["id"] == task["id"] or " ".join(str(other.get("body") or "").split()) != body:
            continue
        before = of(store, other)
        record(store, other["id"], Stop("superseded", f"task #{task['id']} passed the same work", by="locks",
                                        extra={"by_task": task["id"], "was": other["status"],
                                               **({"had_cause": before.cause} if before else {})}), result=False)
        # The pass's own report names it; no session is told of it twice.
        store.add_event(other["id"], "reported", via="superseded")
        store.release_lane(other.get("repo"), other["id"])
        # What waited on the copy waits on the work that passed, and starts on what it kept.
        after.hand_on(store, other["id"], task)
        gone.append(other["id"])
    return gone


def landed(store: Any, top: Path) -> list[int]:
    """Tasks of this checkout that stopped short of a pass, whose kept branch's work is
    already on its HEAD (merged into it, or squash-merged: `isolate.prunable`'s reasons,
    read only): nothing waits on them any more, so they leave the board with an account
    that says where their change is. Swept by the hooks, never by a GET. The ids taken off."""
    from locks import isolate, repo

    kept = isolate.kept_branches(top)
    if not kept:
        return []
    here = str(top.resolve())
    gone = []
    for task_id in sorted(kept):
        task = store.get_task(task_id)
        if task is None or task["status"] not in DISMISSABLE or task.get("source") not in ("locks", "queue"):
            continue
        where = Path(task["cwd"]) if task.get("cwd") else None
        top_of = repo.toplevel(where) if where is not None and where.is_dir() else None
        if top_of is None or str(top_of.resolve()) != here:
            continue
        branch = isolate.branch_for(task_id)
        why = isolate.prunable(top, branch)
        # "merged" says another branch holds its commits: only HEAD's history is the checkout's.
        if not (why == "squash-merged" or (why == "merged" and isolate.landed(top, branch))):
            continue
        into = isolate.current_branch(top) or "HEAD"
        before = of(store, task)
        record(store, task_id, Stop("landed", f"its change is already in {into} ({why})", by="locks",
                                    extra={"branch": branch, "into": into, "how": why, "was": task["status"],
                                           **({"had_cause": before.cause} if before else {})}), result=False)
        store.add_event(task_id, "reported", via="landed")
        store.release_lane(task.get("repo"), task_id)
        # What waited on it starts on the base, which holds its change.
        after.landed(store, task_id, into)
        gone.append(task_id)
    return gone


def retried(store: Any, task: dict[str, Any], again: int) -> None:
    """A workflow step's try that stopped, once `locks workflow retry` queued the step
    again as `again`: it is superseded by that retry, offers nothing to resume, and no
    session is told of it as an ending (the retry's own ending is the one that counts)."""
    if task["status"] in ("queued", "running", "done"):
        return
    before = of(store, task)
    record(store, task["id"], Stop("superseded", f"`locks workflow retry` queued its step again as #{again}",
                                   by="you", extra={"by_task": again, "retry": True, "was": task["status"],
                                                    **({"had_cause": before.cause} if before else {})}),
           result=False)
    store.add_event(task["id"], "reported", via="retried")


def of(store: Any, task: dict[str, Any]) -> Stop | None:
    """The account of a task that stopped: the one recorded with its ending, else
    the one its records give. None for a task that is queued, running or done."""
    status = task["status"]
    if status in ("queued", "running", "done"):
        return None
    finished = store.final_event(task["id"])
    if task.get("source") == "hook" and status in ("interrupted", "failed"):
        # A session's turn that ended without its answer: no work of locks'
        # stopped, and there is nothing to resume.
        reason = str(((finished or {}).get("data") or {}).get("reason") or task.get("result") or "")
        return Stop("turn", reason or "the turn ended without its answer reaching locks")
    data = (finished or {}).get("data") or {}
    found = read(data.get("stop"))
    if found is not None and found.status == status:
        return found
    attempts = store.attempts(task["id"])
    last = attempts[-1] if attempts else {}
    moved = store.last_event(task["id"], "moved")
    moved_data = (moved or {}).get("data") or {}
    seq = last.get("seq")
    return recovered(
        status, result=str(task.get("result") or ""), failure=last.get("failure"),
        move_reason=str(last.get("move_reason") or ""),
        denied=tuple(store.denials(task["id"]).get(seq, ())) if seq is not None else (),
        next_cell=moved_data.get("next_cell"), finished_reason=str(data.get("reason") or ""), attempt=seq)


def view(store: Any, task: dict[str, Any]) -> dict[str, Any] | None:
    """The account as the UI and `--json` show it: who in words, what to do, and
    the command, when there is one."""
    stop = of(store, task)
    if stop is None:
        return None
    # A queued task cancelled before it started runs from the base when resumed (`cli.cmd_resume`).
    resumable = bool(task.get("cwd")) and (task.get("source") == "locks" or (
        task.get("source") == "queue" and stop.status == "cancelled"))
    todo = what_to_do(stop) if resumable or stop.status not in ("cancelled", "dismissed") else (
        "it never ran: queue it again if you still want it")
    rules = list(stop.allow or allow.from_refusals(stop.denied)) if stop.cause == "permission" else []
    keep = ("locks allow " + " ".join(shlex.quote(r) for r in rules)) if rules else None
    return {**stop.data(), "allow": rules, "keep": keep, "status": stop.status, "who": WHO.get(stop.who, stop.who),
            "todo": todo,
            "next": next_step(task["id"], stop, resumable=resumable)}


def resume_args(task_id: int, shown: dict[str, Any] | None, *, keep: bool = False) -> list[str] | None:
    """The `locks resume` a person's press runs for a stopped task, from locks'
    own account, never from what a page or a model sends: the rules a refusal
    suggests, or an approval. None when no press continues it."""
    if not shown or not shown.get("next") or shown.get("cause") == "question":
        return None  # a question is answered with its line, never resumed by a press
    base = ["resume", str(task_id), "--detach"]
    cause = shown.get("cause")
    if cause == "permission":
        rules = list(shown.get("allow") or [])
        if not rules:
            return None
        return [*base, *(a for rule in rules for a in ("--allow", rule)), *(["--keep"] if keep else [])]
    if keep:
        return None
    if cause == "approval":
        return [*base, "--allow-approval"]
    return base


