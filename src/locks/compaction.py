"""When a session should compact, and what `/compact` should keep.

Claude Code compacts on its own when the context crosses a threshold: often in
the middle of work, and often after the prompt cache went cold, so the summary
request pays for the whole history again. locks sees better moments. At the end
of a turn the Stop hook asks `decide` whether now is one: the context is big
enough to be worth it, a natural break just happened (work this session sent
ended and was reported, a branch was merged), no run of this session holds the
checkout, the last turn ended while the cache is still warm, and nothing
compacted or was suggested within a cooldown. A decision worth keeping is a
`compaction_decided` event on the turn's task, with its reasons.

What `/compact` is told (`instructions`) is read from what locks already keeps
for a compaction — the scribe's thread, the session's tasks, git — never
written by a model, bounded to fit one line, secrets masked as the digest masks
them. Its marker (`MARKER`) names the decision: PreCompact's
`custom_instructions` show that a compaction was the one locks asked for, and a
text that reached the model as a message says itself that it is no request.

The orchestrating session decides (`orchestrator`, the default `compact`
setting): locks' moment reaches it as advice, with the reasons, and the session
compacts by calling the mod's `compact` tool when it judges the conversation's
detail is no longer needed — on that advice or on its own (`request`). What it
asks the summary to keep is quoted apart, marked as the session's; the rest of
the instructions are locks', read as below. The compaction it asked for runs
when its turn ends, as `auto`'s does.

The mod delivers it (`next_step`, the `compact` setting): `suggest` shows the
person the line, `auto` has the engine compact between turns with the same
instructions (`$.session.compact`, the call `/compact` makes; a `/` text a mod
submits is refused by the engine; a headless session, which has no such call,
gets `/compact` run with them, `$.command.run`, settled from the hooks as a
submit nobody answered). What it answered is kept (`compacted`); a
call that failed only shows the line (`delivered`), since a failure says nothing
of the build. The classic hooks also record each compaction they see (`seen`),
and `settle` decides from them a submit the mod never reported. The answer is
kept per Claude Code version in the locks home: a build found not to run one
gets the line shown from then on.

A compaction the session asked for may carry what it goes on with after it
(`then`, the session's own prompt to itself): with nothing of its work running,
no ending would ever start its next turn. Once the compaction finished (a hook
saw one end after the decision was delivered, or the mod settled it), `next_step`
hands that prompt to the mod once (`continuation`, a `compaction_continued`
event), quoted and marked as the session's own words, and the mod submits it.

Stdlib only, no model call: the Stop hook runs it.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from locks import compact, config, legacy
from locks.store import Store

#: The context is worth compacting from this share of the model's window.
WORTH_SHARE = 0.5
#: From this share a compaction is near anyway (the harness's own, at a moment nobody
#: chose): a natural break is no longer waited for.
PRESSING_SHARE = 0.8
#: From this share the advice is now whatever else holds it back: the harness's own compaction is
#: a few turns away, and a cold cache or a run holding the checkout costs less than losing the
#: moment (issue #124).
URGENT_SHARE = 0.9
#: A decision that says "not now" is recorded at most once in this many seconds per session.
QUIET_S = 10 * 60
#: A model's context window, unless the transcript shows a larger one.
WINDOW = 200_000
#: The window of a model run with a million-token context: named so in its id
#: (`[1m]`), or shown by any context past `WINDOW` in the transcript's tail (the
#: id Claude Code writes there does not name it, and the latest context may be
#: small again after a compaction).
WINDOW_1M = 1_000_000
#: How long the prompt cache keeps the session's prefix: five minutes on an API
#: key, an hour on a subscription within its plan. The session's own responses
#: say which (`cache_creation`); when they do not, the shorter.
CACHE_TTL_S = 300
CACHE_TTL_LONG_S = 3600
#: Room for a compaction to start before the cache goes cold.
WARM_MARGIN_S = 30
#: A break counts when it happened in the turn that just ended or this long before it.
BREAK_SLACK_S = 300
#: No second suggestion, and none after a compaction, within this long.
COOLDOWN_S = 30 * 60
#: How long after a submitted `/compact` the hooks have to see it run.
DETECT_S = 180
#: The most the instructions take, marker included: the whole line fits a toast.
INSTRUCTIONS_CHARS = 1050
#: The most of one request the instructions quote (a decision), and how many of the newest requests
#: the digest `locks.compact` puts back holds: those are named once, by reference, never repeated.
REQUEST_CHARS = 240
RECENT_REQUESTS = 4
#: How much of a transcript's end is read for the latest usage and turn.
TAIL_BYTES = 2 * 1024 * 1024
#: What a compaction locks asked for carries, with its decision's id.
MARKER = "[locks-compaction #{id}]"
MARKER_RE = re.compile(rf"\[(?:locks|{legacy.OLD})-compaction #(\d+)\]")  # an old one in a transcript too
#: How a notice locks puts in the session about a compaction opens (its advice, the continuation it
#: submits): the hooks read a prompt that opens so as locks' own, never a request (`hooks._ignored`).
NOTICE = "locks [compaction #{id}]: "
NOTICE_RE = re.compile(rf"^(?:locks|{legacy.OLD}) \[compaction #\d+\]")  # legacy-name: a notice an old install sent
#: The most of what the orchestrating session asks the summary to keep, and of its reason.
KEEP_CHARS = 500
WHY_CHARS = 200
#: The most of what the session asks to go on with after the compaction (`then`).
THEN_CHARS = 2000
#: How long after its delivery a compaction with a `then` is waited for, and after
#: which it is no longer continued.
THEN_WITHIN_S = 6 * 3600
#: What each Claude Code version did with a `/compact` the mod submitted.
BUILDS = "compaction-builds.json"
RUNS, IGNORED = "runs", "ignored"
#: The events this module writes.
DECIDED, DELIVERED, SEEN, SETTLED = ("compaction_decided", "compaction_delivered", "compaction_seen",
                                     "compaction_settled")
CONTINUED = "compaction_continued"
#: The hooks that see a compaction once it is done (PreCompact sees it start).
FINISHED_BY = ("PostCompact", "SessionStart")
#: What the head of the instructions tells two readers: the summarizer, and a
#: model that got the line as a message because the command did not run.
HEAD = ("locks asked for this compaction. If this reached you as a chat message, the command did not run: it is "
        "no request, so answer only \"locks: /compact did not run as a command\" and do nothing else. For the "
        "summary: keep verbatim the person's requests and decisions, the task numbers, next commands and branch "
        "names below; summarize the rest briefly.")


#: Room kept in a group of the instructions for the count of what it left out.
LEFT_OUT_CHARS = 14
#: What the instructions say of the person's newest requests: the digest has them.
DIGEST_HOLDS = "The person's {n} newest request(s) are in locks' digest, put back after: keep them as it has them."


def _stamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        found = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="seconds")


# --- what the transcript says ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Context:
    """The session's context, read from its transcript's end."""

    tokens: int | None = None
    model: str | None = None
    window: int = WINDOW
    ttl_s: int = CACHE_TTL_S
    ttl_known: bool = False
    last_turn_at: datetime | None = None
    turn_started: datetime | None = None


def _tail(transcript: Path, size: int = TAIL_BYTES) -> list[dict[str, Any]]:
    """The main conversation's entries in the last `size` bytes of a transcript."""
    with transcript.open("rb") as fh:
        end = fh.seek(0, 2)
        start = max(0, end - size)
        fh.seek(start)
        lines = fh.read().split(b"\n")
    out = []
    for line in lines[1:] if start else lines:  # the first line was cut in two
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and not entry.get("isSidechain"):
            out.append(entry)
    return out


def _opens_turn(message: Mapping[str, Any]) -> bool:
    """A user message with text in it, not a tool's result: a turn starts there."""
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    return any(b.get("type") == "text" for b in blocks) and not any(b.get("type") == "tool_result" for b in blocks)


def _size(entry: Mapping[str, Any]) -> int:
    """An assistant message's context: input, cache read and cache creation tokens."""
    message = entry.get("message")
    used = message.get("usage") if isinstance(message, dict) and entry.get("type") == "assistant" else None
    if not isinstance(used, dict):
        return 0
    return sum(int(used.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                               "cache_creation_input_tokens"))


def window_of(model: str | None, peak: int | None) -> int:
    """The window a session runs in: `peak` is the largest context it showed."""
    return WINDOW_1M if "[1m]" in (model or "").lower() or (peak or 0) > WINDOW else WINDOW


def read_context(transcript: Path) -> Context:
    """The latest assistant message's context (input, cache read and cache
    creation tokens), its model, when it ended, when its turn started, and how
    long the cache keeps it as the session's own cache writes say. The window is
    read from the largest context of the tail, not only the latest."""
    if not transcript.is_file():
        return Context()
    tokens: int | None = None
    model = None
    last = started = None
    ttl, known = CACHE_TTL_S, False
    entries = _tail(transcript)
    peak = max((_size(e) for e in entries), default=0)
    for entry in reversed(entries):
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        used = message.get("usage")
        if entry.get("type") == "assistant" and isinstance(used, dict):
            size = _size(entry)
            if tokens is None and size:  # an error the harness wrote has no usage
                tokens, last = size, _stamp(entry.get("timestamp"))
                model = message.get("model") if isinstance(message.get("model"), str) else None
            split = used.get("cache_creation")
            if not known and isinstance(split, dict):
                if int(split.get("ephemeral_1h_input_tokens") or 0) > 0:
                    ttl, known = CACHE_TTL_LONG_S, True
                elif int(split.get("ephemeral_5m_input_tokens") or 0) > 0:
                    ttl, known = CACHE_TTL_S, True
        elif entry.get("type") == "user" and tokens is not None and started is None and _opens_turn(message):
            started = _stamp(entry.get("timestamp"))
        if tokens is not None and started is not None and known:
            break
    return Context(tokens=tokens, model=model, window=window_of(model, peak), ttl_s=ttl, ttl_known=known,
                   last_turn_at=last, turn_started=started)


def worth(context: Context) -> bool:
    return bool(context.tokens) and context.tokens / context.window >= WORTH_SHARE


# --- the decision -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signals:
    """What the decision weighs, gathered by `gather` (or a test)."""

    context: Context
    breaks: tuple[str, ...] = ()
    holding: int | None = None
    last_compaction: datetime | None = None
    worker: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether now is the moment, and why: `because` is the chain of evidence,
    each reason that held, then each that did not (`not now: …`)."""

    now: bool
    because: tuple[str, ...]
    worth: bool = False
    tokens: int | None = None
    window: int = WINDOW
    ttl_s: int = CACHE_TTL_S
    age_s: float | None = None
    warm_until: datetime | None = None
    warm_usd: float | None = None
    cold_usd: float | None = None

    def data(self) -> dict[str, Any]:
        return {"now": self.now, "because": list(self.because), "tokens": self.tokens, "window": self.window,
                "ttl_s": self.ttl_s, "age_s": self.age_s, "warm_until": _iso(self.warm_until) if self.warm_until
                else None, "warm_usd": self.warm_usd, "cold_usd": self.cold_usd}


def cost(model: str | None, tokens: int) -> tuple[float | None, float | None]:
    """What the summary request's input costs with the prefix read from the cache,
    and reprocessed cold at the input rate; None for a model with no known rate."""
    from locks import usage

    warm, cold = usage.dollars(model or "", 0, 0, cache_read=tokens), usage.dollars(model or "", tokens, 0)
    return (round(warm, 4), round(cold, 4)) if cold else (None, None)


def decide(signals: Signals, at: datetime) -> Decision:
    """Whether a session should compact now. Pure: everything it weighs is in `signals`."""
    c = signals.context
    if signals.worker:
        return Decision(False, ("a locks worker never compacts on locks' word",))
    if not c.tokens:
        return Decision(False, ("the transcript says nothing of the context's size",))
    share = c.tokens / c.window
    size = f"context {c.tokens:,} tokens of {c.window:,} ({share:.0%})"
    if share < WORTH_SHARE:
        return Decision(False, (f"{size}: under the {WORTH_SHARE:.0%} worth compacting",), tokens=c.tokens,
                        window=c.window)
    held = [f"{size}: at least {WORTH_SHARE:.0%}, worth compacting"]
    not_held = []
    if signals.breaks:
        held.append("a natural break: " + "; ".join(signals.breaks[:3]))
    elif share >= PRESSING_SHARE:
        held.append(f"no natural break, but from {PRESSING_SHARE:.0%} the harness's own compaction is near: "
                    "better now, at a moment locks can see")
    else:
        not_held.append("no natural break: no work this session sent ended and was reported, no branch was merged")
    if signals.holding:
        not_held.append(f"#{signals.holding}, a run this session started, holds the checkout")
    else:
        held.append("no run of this session holds the checkout")
    plan = "the session's cache writes say so" if c.ttl_known else "assumed: nothing says the plan"
    cache = f"the cache ({c.ttl_s // 60}m, {plan})"
    age = round((at - c.last_turn_at).total_seconds(), 1) if c.last_turn_at else None
    warm_until = c.last_turn_at + timedelta(seconds=c.ttl_s) if c.last_turn_at else None
    if age is None:
        not_held.append("when the last turn ended is not known, nor whether the cache is warm")
    elif age > c.ttl_s - WARM_MARGIN_S:
        not_held.append(f"the last turn ended {age:.0f}s ago: {cache} is cold or about to be")
    else:
        held.append(f"the last turn ended {age:.0f}s ago: {cache} is warm")
    last = signals.last_compaction
    if last is not None and (at - last).total_seconds() < COOLDOWN_S:
        minutes = int((at - last).total_seconds() // 60)
        not_held.append(f"compacted or suggested {minutes}m ago, within the {COOLDOWN_S // 60}m cooldown")
    else:
        held.append(f"nothing compacted or suggested in the last {COOLDOWN_S // 60}m")
    warm, cold = cost(c.model, c.tokens)
    if not_held and share >= URGENT_SHARE:
        held.append(f"from {URGENT_SHARE:.0%} it is now whatever else holds it back")
        not_held = [f"(overridden) {r}" for r in not_held]
        return Decision(True, (*held, *not_held), worth=True, tokens=c.tokens, window=c.window,
                        ttl_s=c.ttl_s, age_s=age, warm_until=warm_until, warm_usd=warm, cold_usd=cold)
    return Decision(not not_held, (*held, *(f"not now: {r}" for r in not_held)), worth=True, tokens=c.tokens,
                    window=c.window, ttl_s=c.ttl_s, age_s=age, warm_until=warm_until, warm_usd=warm, cold_usd=cold)


# --- what the decision weighs, read from the store, git and the lock ---------------


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def merges(cwd: str | None, since: datetime) -> list[str]:
    """Merges into this checkout's HEAD since `since`, from its reflog."""
    if not cwd or not Path(cwd).is_dir():
        return []
    out = _git(cwd, "reflog", "show", "--date=unix", "--format=%gd%x09%gs", "-n", "40", "HEAD")
    found = []
    for line in out.stdout.splitlines() if out and out.returncode == 0 else []:
        selector, _, subject = line.partition("\t")
        when = re.search(r"@\{(\d+)\}", selector)
        if when and subject.startswith("merge ") and int(when.group(1)) >= since.timestamp():
            found.append(compact._clip(subject, 120))
    return found


def breaks(store: Store, session_id: str, cwd: str | None, since: datetime) -> list[str]:
    """The natural breaks since `since`: work this session sent that ended and was
    reported to it (a workflow's run when it ended, not each step the engine went
    past), and branches merged into the checkout."""
    found: dict[str, None] = {}
    for event in store.session_events(session_id, ("reported",), since=_iso(since)):
        task = store.get_task(event["task_id"])
        if event["data"].get("via") != "workflow" and task and task["source"] in ("locks", "queue"):
            found[f"#{task['id']} ended ({task['status']}) and was reported"] = None
    found.update((f"merged: {m}", None) for m in merges(cwd, since))
    return list(found)


def holding(store: Store, root: Path, session_id: str, cwd: str | None) -> int | None:
    """A run this session started that holds the checkout the session works in."""
    from locks import isolate, repo

    if not cwd or not Path(cwd).is_dir():
        return None
    folder = Path(cwd)
    for checkout in isolate.held_by(folder, repo.toplevel(folder)):
        task_id = isolate.holder(root, checkout)
        task = store.get_task(task_id) if task_id else None
        if task and task["session_id"] == session_id:
            return task_id
    return None


def last_compaction(store: Store, session_id: str, at: datetime) -> datetime | None:
    """When the session last compacted, or locks last decided it should."""
    since = _iso(at - timedelta(seconds=COOLDOWN_S))
    stamps = [_stamp(e["ts"]) for e in store.session_events(session_id, (DECIDED, SEEN), since=since)
              if e["kind"] == SEEN or e["data"].get("now")]
    return max((s for s in stamps if s), default=None)


def gather(store: Store, root: Path, *, session_id: str, transcript: Path, cwd: str | None, at: datetime,
           worker: bool = False) -> Signals:
    """Everything `decide` weighs. The context first: when it is not worth
    compacting, nothing else is read."""
    context = read_context(transcript)
    if worker or not worth(context):
        return Signals(context=context, worker=worker)
    since = (context.turn_started or at) - timedelta(seconds=BREAK_SLACK_S)
    return Signals(context=context, breaks=tuple(breaks(store, session_id, cwd, since)),
                   holding=holding(store, root, session_id, cwd),
                   last_compaction=last_compaction(store, session_id, at))


# --- what /compact is told ---------------------------------------------------------


def _quoted(text: str) -> str:
    return '"' + compact._clip(text, REQUEST_CHARS) + '"'


def instructions(*, requests: Sequence[Mapping[str, Any]], open_work: Sequence[str], branches: Sequence[str],
                 budget: int = INSTRUCTIONS_CHARS - len(MARKER) - 12) -> str:
    """The instructions after the marker: what to keep verbatim, newest and most
    needed first, within `budget`, on one line. `requests` are the scribe's
    (`text`, `correction`): a correction is a decision the person made."""
    recent = list(requests[-RECENT_REQUESTS:])
    decisions = [r for r in requests[:-RECENT_REQUESTS] if r.get("correction")]
    groups = [
        ("Open work", [compact._clip(w, 300) for w in open_work], False),
        ("Branches waiting to merge", [compact._clip(b, 160) for b in branches], False),
        ("Decisions the person made (oldest first)", [_quoted(r["text"]) for r in decisions], True),
    ]
    # The digest holds the newest requests verbatim: named once, here, never quoted a second time.
    parts = [DIGEST_HOLDS.format(n=len(recent))] if recent else []
    room = budget - len(HEAD) - sum(len(p) + 1 for p in parts)
    for title, items, newest_first in groups:
        picked: list[str] = []
        used = len(title) + 3
        for item in reversed(items) if newest_first else items:
            if used + len(item) + 1 + LEFT_OUT_CHARS > room:
                break
            picked.append(item)
            used += len(item) + 1
        if picked:
            left = f" (+{len(items) - len(picked)} more)" if len(picked) < len(items) else ""
            parts.append(f"{title}: {' '.join(reversed(picked) if newest_first else picked)}{left}.")
            room -= used + len(left)
    return " ".join([HEAD, *parts])


def kept_by_session(keep: str) -> str:
    """What the orchestrating session asks the summary to keep, quoted apart: its own
    words, never locks' digest."""
    return f'The orchestrating session asks the summary to also keep: "{compact._clip(keep, KEEP_CHARS)}".'


def line(decision_id: int, text: str) -> str:
    """The `/compact` line a person pastes or the mod submits."""
    return f"/compact {MARKER.format(id=decision_id)} {text}"


def _requests(root: Path, session_id: str, transcript: Path) -> list[dict[str, Any]]:
    """The person's requests as the scribe keeps them, or read from the transcript
    when it keeps none (the scribe is off)."""
    from locks import scribe
    from locks.notes import _PRIVATE

    state = scribe.load(root, session_id)
    if state and state.get("requests"):
        return list(state["requests"])
    if not transcript.is_file():
        return []
    return [{"text": _PRIVATE.sub("[private]", r), "correction": False} for r in compact.read(transcript)["requests"]]


def _unmerged(cwd: str | None, branch: str) -> bool:
    if not cwd or not Path(cwd).is_dir():
        return False
    exists = _git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if not exists or exists.returncode != 0:
        return False
    merged = _git(cwd, "merge-base", "--is-ancestor", branch, "HEAD")
    return bool(merged) and merged.returncode == 1


def what_is_open(store: Store, session_id: str, cwd: str | None) -> tuple[list[str], list[str]]:
    """This session's open work, each with its next command, and the branches of
    its passed work that wait to be merged."""
    from locks import stops
    from locks.store import OPEN_FOR_A_RELAY

    work = []
    for task in store.list_tasks(session_id=session_id, status=OPEN_FOR_A_RELAY, source=("locks", "queue"),
                                 limit=10):
        shown = stops.view(store, task) if task["status"] not in ("queued", "running") else None
        work.append(f"#{task['id']} [{task['status']}] {task['title']}"
                    + (f", next: {shown['next']}" if shown and shown.get("next") else ""))
    branches = []
    for task in store.list_tasks(session_id=session_id, status=("done",), source=("locks", "queue"), limit=20):
        branch = ((store.final_event(task["id"]) or {}).get("data") or {}).get("branch")
        if branch and _unmerged(cwd, str(branch)):
            branches.append(f"{branch} (#{task['id']} {task['title']})")
    return work, branches


# --- the Stop hook: decide and record -------------------------------------------------


def consider(store: Store, root: Path, *, session_id: str, transcript: Path, cwd: str | None, task_id: int,
             env: Mapping[str, str], at: datetime | None = None) -> dict[str, Any] | None:
    """At a turn's end: decide, and keep a decision whose context was worth
    compacting as a `compaction_decided` event on the turn's task (with the
    instructions when it says now). None when the setting is off, the session is
    a locks worker, or the context is too small to be worth a record."""
    mode = config.choice("compact", env)
    if mode == "off" or env.get("LOCKS_WORKER_TASK"):
        return None
    at = at or datetime.now(UTC)
    decision = decide(gather(store, root, session_id=session_id, transcript=transcript, cwd=cwd, at=at), at)
    if not decision.worth:
        return None
    if not decision.now and _said_not_now(store, session_id, at):
        return None
    data = {**decision.data(), "mode": mode, "cwd": cwd}
    if decision.now:
        work, branches = what_is_open(store, session_id, cwd)
        data["instructions"] = instructions(requests=_requests(root, session_id, transcript), open_work=work,
                                            branches=branches)
    data["id"] = store.add_event(task_id, DECIDED, **data)
    return data


def _said_not_now(store: Store, session_id: str, at: datetime) -> bool:
    """Whether a "not now" was recorded for the session within `QUIET_S`: the next one adds
    nothing but noise (issue #124)."""
    events = store.session_events(session_id, (DECIDED,), since=_iso(at - timedelta(seconds=QUIET_S)))
    return any(not e["data"].get("now") for e in events)


def request(store: Store, root: Path, *, session_id: str, cwd: str | None, why: str = "", keep: str = "",
            then: str = "", transcript: Path | None = None, at: datetime | None = None) -> dict[str, Any]:
    """The orchestrating session decided to compact (the mod's `compact` tool): a
    decision to compact now, on its word, with locks' instructions and what it asks
    to keep quoted apart. It runs when the session's turn ends (`next_step`), with no
    check of the cache, the size or the cooldown: the session judged it. What it
    asks to go on with after it (`then`) is kept on the decision, for
    `continuation`. Refused without a turn to keep it on."""
    from locks import scribe

    at = at or datetime.now(UTC)
    turn = store.last_prompt_task(session_id)
    if turn is None:
        return {"error": "locks has seen no turn of this session yet"}
    transcript = transcript or scribe.find_transcript(session_id) or Path("/nonexistent")
    context = read_context(transcript)
    work, branches = what_is_open(store, session_id, cwd)
    keep_part = kept_by_session(keep) if keep.strip() else ""
    text = instructions(requests=_requests(root, session_id, transcript), open_work=work, branches=branches,
                        budget=INSTRUCTIONS_CHARS - len(MARKER) - 12 - len(keep_part))
    reason = compact._clip(why, WHY_CHARS) if why.strip() else "no reason given"
    data = {"now": True, "requested": True, "why": reason, "keep": bool(keep_part),
            "because": [f"the orchestrating session decided to compact: {reason}"], "tokens": context.tokens,
            "window": context.window, "mode": "orchestrator", "cwd": cwd,
            "instructions": " ".join(p for p in (text, keep_part) if p)}
    if then.strip():
        data["then"] = compact._clip(then.strip(), THEN_CHARS)
    data["id"] = store.add_event(turn["id"], DECIDED, **data)
    return data


def continuation(store: Store, session_id: str, at: datetime) -> dict[str, Any] | None:
    """The prompt a compaction the session asked for carries (`then`), once that
    compaction finished: a hook saw a compaction end after the decision was
    delivered, the engine answered the mod (a hook's veto too: the work still goes
    on), or it was found never to run. A marker PreCompact saw is no end: the
    compaction is still running then.
    Claimed with a `compaction_continued` event, so it is handed over once; the
    caller holds the write lock. `{"wait": True}` while one delivered within
    `DETECT_S` has not finished yet; None when there is nothing to go on with."""
    events = store.session_events(session_id, (DECIDED, DELIVERED, SEEN, SETTLED, CONTINUED),
                                  since=_iso(at - timedelta(seconds=THEN_WITHIN_S)))
    continued = {e["data"].get("decision") for e in events if e["kind"] == CONTINUED}
    waiting = False
    for decision in [e for e in events if e["kind"] == DECIDED and e["data"].get("then")]:
        if decision["id"] in continued:
            continue
        sent = [e for e in events if e["kind"] == DELIVERED and e["data"].get("decision") == decision["id"]]
        if not sent:
            continue
        done = any(e["kind"] == SETTLED and e["data"].get("decision") == decision["id"]
                   and (e["data"].get("answered") or not e["data"].get("ran")) for e in events) or any(
            e["kind"] == SEEN and e["id"] > sent[0]["id"] and e["data"].get("source") in FINISHED_BY
            for e in events)
        if not done:
            stamp = _stamp(sent[0]["ts"])
            waiting = waiting or (stamp is not None and (at - stamp).total_seconds() < DETECT_S)
            continue
        store.add_event(decision["task_id"], CONTINUED, decision=decision["id"])
        return {"decision": decision["id"], "text": (
            f"{NOTICE.format(id=decision['id'])}this session compacted. Before it, the session left itself "
            f"what to go on with, in its own words: \"{decision['data']['then']}\". Go on with it now.")}
    return {"wait": True} if waiting else None


def advice(found: Mapping[str, Any]) -> str:
    """What the orchestrating session is told when locks sees a moment: the reasons,
    and that the call is its own."""
    because = [r for r in found["data"].get("because") or () if not str(r).startswith("not now")]
    return (f"{NOTICE.format(id=found['id'])}this looks like a good moment to compact this session ("
            + "; ".join(because) + "). It is your call. If the work at hand no longer needs this conversation's "
            "detail, call locks' `compact` tool (say in `keep` anything that must survive beyond the person's "
            "requests, the open work and branches, which locks keeps itself); it runs when this turn ends. If "
            "something in flight still needs the detail, answer in one line why not, and do nothing else: locks "
            "looks again at the next break.")


# --- the hooks that see a compaction ----------------------------------------------------


def seen(store: Store, session_id: str, source: str, *, trigger: str | None = None,
         instructions: str | None = None) -> int | None:
    """A compaction the classic hooks saw (PreCompact, PostCompact, a SessionStart
    after one), with the decision its marker names, on the session's latest turn.
    None when the session has no turn to keep it on."""
    turn = store.last_prompt_task(session_id)
    if turn is None:
        return None
    marker = MARKER_RE.search(instructions or "")
    return store.add_event(turn["id"], SEEN, source=source, trigger=trigger,
                           marker=int(marker.group(1)) if marker else None)


# --- the mod: deliver, and learn what this Claude Code does with a submitted line --------


def builds(root: Path) -> dict[str, dict[str, Any]]:
    try:
        found = json.loads((root / BUILDS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _keep_build(root: Path, version: str, verdict: str, decision_id: int) -> None:
    known = builds(root)
    known[version] = {"verdict": verdict, "decision": decision_id, "at": _iso(datetime.now(UTC))}
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".{BUILDS}.tmp"
    tmp.write_text(json.dumps(known, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(root / BUILDS)


def _ran(after: Sequence[Mapping[str, Any]], decision_id: int) -> str | None:
    """What shows the compaction a decision asked for ran, among the compactions
    seen after it was submitted; None when nothing does."""
    if any(e["data"].get("marker") == decision_id for e in after):
        return "PreCompact carried its marker"
    manual = [e["data"]["source"] for e in after if e["data"].get("trigger") == "manual"]
    if manual:
        return f"a manual compaction followed ({manual[0]})"
    automatic = any(e["data"].get("trigger") == "auto" for e in after)
    if not automatic and any(e["data"].get("source") == "SessionStart" for e in after):
        return "the session started again from a compaction"
    return None


def settle(store: Store, root: Path, session_id: str, *, at: datetime) -> str | None:
    """Decide each `/compact` the mod submitted for this session that is still
    open: it ran when a compaction it asked for was seen, it did not when none was
    within `DETECT_S`. Either answer is kept for the Claude Code version that got
    it. What to tell the person when a version is first found not to run one."""
    events = store.session_events(session_id, (DELIVERED, SEEN, SETTLED), since=_iso(at - timedelta(days=1)))
    delivered: dict[int, dict[str, Any]] = {}
    done = {e["data"].get("decision") for e in events if e["kind"] == SETTLED}
    for event in events:
        if event["kind"] == DELIVERED:
            delivered[int(event["data"]["decision"])] = event
    said = None
    for decision_id, event in delivered.items():
        if decision_id in done or event["data"].get("how") != "submit":
            continue
        evidence = _ran([e for e in events if e["kind"] == SEEN and e["id"] > event["id"]], decision_id)
        sent = _stamp(event["ts"])
        if evidence is None and sent is not None and (at - sent).total_seconds() < DETECT_S:
            continue
        version = str(event["data"].get("version") or "unknown")
        store.add_event(event["task_id"], SETTLED, decision=decision_id, ran=evidence is not None, version=version,
                        evidence=evidence or f"no compaction seen within {DETECT_S}s of the submit")
        said = _learn(root, version, RUNS if evidence else IGNORED, decision_id) or said
    return said


def _learn(root: Path, version: str, verdict: str, decision_id: int) -> str | None:
    """Keep what a version did with a submitted line; what to tell the person the
    first time a version is found not to run one."""
    if (builds(root).get(version) or {}).get("verdict") == verdict:
        return None
    _keep_build(root, version, verdict, decision_id)
    if verdict != IGNORED:
        return None
    return (f"locks: this Claude Code ({version}) did not run the /compact locks submitted, so `compact auto` falls "
            "back to suggest on it: locks shows you the line to paste from now on, and tries again after Claude "
            "Code updates.")


def pending(store: Store, session_id: str, at: datetime) -> dict[str, Any] | None:
    """The latest decision to compact now that was not delivered, while its
    cache is warm and nothing compacted since. A decision the session asked for
    (`requested`) goes first, whatever locks decided after it: a later decision of
    locks' own never buries it. While one it asked for was delivered and nothing says it
    finished (no compaction seen, no answer settled), locks advises nothing over it."""
    events = store.session_events(session_id, (DECIDED, DELIVERED, SEEN, SETTLED),
                                  since=_iso(at - timedelta(seconds=CACHE_TTL_LONG_S)))
    latest = asked = None
    in_flight: set[int] = set()
    for event in events:
        data = event["data"]
        if event["kind"] == DECIDED and data.get("now"):
            latest = event
            if data.get("requested"):
                asked = event
        elif event["kind"] == SEEN:
            latest = asked = None
            in_flight.clear()
        elif event["kind"] == SETTLED:
            in_flight.discard(data.get("decision"))
        elif event["kind"] == DELIVERED:
            if asked and data.get("decision") == asked["id"]:
                in_flight.add(asked["id"])
                asked = None
            if latest and data.get("decision") == latest["id"]:
                latest = None
    if asked is not None:
        return asked  # the session judged the moment: no cache check overrules it
    if in_flight:
        return None  # what the session asked for is running or about to: advise nothing over it
    warm_until = _stamp((latest or {}).get("data", {}).get("warm_until"))
    if latest is None or warm_until is None or at > warm_until - timedelta(seconds=WARM_MARGIN_S):
        return None
    return latest


def _advised_this_prompt(store: Store, session_id: str) -> bool:
    """Whether locks already advised this session since the person's last prompt: its own
    advice reaches the session at most once per prompt, never once per notice it gets."""
    prompt = store.last_prompt_task(session_id)
    if prompt is None:
        return False
    # A decision is kept on the prompt's own task, so its delivery is too: seconds are too coarse.
    since = str(prompt.get("created_at") or "")
    return any(e["data"].get("how") == "advise" and e["task_id"] == prompt["id"]
               for e in store.session_events(session_id, (DELIVERED,), since=since))


def next_step(store: Store, root: Path, session_id: str, *, version: str, mode: str,
              at: datetime | None = None) -> dict[str, Any]:
    """What the mod does now that the session is idle: say what `settle` found,
    and take the pending decision once — `suggest` (show the line), `submit`
    (with `auto`, or a decision the orchestrating session asked for, and only on a
    build not found to ignore it; with the `instructions` the engine compacts
    with), or `advise` (with `orchestrator`, locks' own moment: the session is
    told, and decides), or `continue` (a compaction the session asked for
    finished, and the prompt it left itself is due: `continuation`). A run of
    this session that took the
    checkout since leaves the decision waiting, and says so (`wait`): the mod asks
    again while idle, and otherwise only after the next turn ends."""
    at = at or datetime.now(UTC)
    out: dict[str, Any] = {"action": None}
    said = settle(store, root, session_id, at=at)
    if said:
        out["said"] = said
    if mode == "off":
        return out
    with store.immediate():
        follow = continuation(store, session_id, at)
        if follow is not None and follow.get("text"):
            out.update(action="continue", decision=follow["decision"], text=follow["text"])
            return out
        found = pending(store, session_id, at)
        if found is None:
            if follow is not None:
                out["wait"] = True  # a compaction with a prompt to follow it is still running
            return out
        if holding(store, root, session_id, found["data"].get("cwd")):
            out["wait"] = True
            return out
        ignored = (builds(root).get(version) or {}).get("verdict") == IGNORED
        requested = bool(found["data"].get("requested"))
        if mode == "orchestrator" and not requested:
            if _advised_this_prompt(store, session_id):
                return out  # advised once since the person last spoke: never again per notice
            how = "advise"
        else:
            how = "submit" if (mode == "auto" or requested) and not ignored else "suggest"
        from locks import __version__

        store.add_event(found["task_id"], DELIVERED, decision=found["id"], how=how, version=version, mode=mode,
                        locks=__version__)
    if how == "advise":
        out.update(action=how, decision=found["id"], advice=advice(found))
        return out
    text = str(found["data"].get("instructions") or "")
    out.update(action=how, decision=found["id"], line=line(found["id"], text))
    if how == "submit":
        out["instructions"] = f"{MARKER.format(id=found['id'])} {text}"
    return out


def delivered(store: Store, session_id: str, decision_id: int, *, how: str, why: str) -> bool:
    """The mod changed how it delivered a decision (a turn started before it could
    submit, or the call failed, so it showed the line): kept, so `settle` never reads a line nobody
    submitted as one the build ignored."""
    found = [e for e in store.session_events(session_id, (DECIDED,)) if e["id"] == decision_id]
    if not found:
        return False
    store.add_event(found[0]["task_id"], DELIVERED, decision=decision_id, how=how, why=why)
    return True


def compacted(store: Store, root: Path, session_id: str, decision_id: int, *, version: str,
              skip: str | None = None) -> dict[str, Any]:
    """What the engine answered the mod's `$.session.compact` for a decision: the
    compaction stood (this version runs locks' compactions), or a hook vetoed it
    (`skip`), which says nothing of the version. Settled either way, so the
    timeout never reads it again."""
    found = [e for e in store.session_events(session_id, (DECIDED,)) if e["id"] == decision_id]
    if not found:
        return {"kept": False}
    evidence = "the engine compacted it" if skip is None else "a hook vetoed it: " + compact._clip(skip, 300)
    store.add_event(found[0]["task_id"], SETTLED, decision=decision_id, ran=skip is None, version=version,
                    evidence=evidence, answered=True)
    if skip is None:
        _learn(root, version, RUNS, decision_id)
    return {"kept": True}

