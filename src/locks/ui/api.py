"""What each screen shows, as plain data. No HTTP here: every screen is testable
against a store, and the server only turns these into JSON."""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from locks import capacity, config, dispatch, flow, habits, phase, progress, project, stops, usage
from locks.matrix import LADDERS
from locks.store import Store

TASK_SOURCES = ("locks", "queue")  # what the board shows: work locks runs or holds
#: States that are waiting on a person, and what each one asks of them.
NEEDS_YOU = {
    "failed": "every cell on its ladder failed: read the attempts",
    "blocked": "a refused command or the environment stopped it: clear it, then `locks resume`",
    "replan": "the task as written cannot be done: rewrite or split it",
    "needs_approval": "the next cell needs your approval",
    "interrupted": "it stopped with no sign of life",
}
#: How far back a passed task's branch still counts as waiting for a review.
REVIEW_DAYS = 3
#: How much of a task's text the board carries; the task view has all of it.
BODY_CHARS = 800


def _brief(task: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "status", "kind", "repo", "cwd", "start_cell", "final_cell", "current_cell",
            "cost_usd", "source", "created_at", "updated_at", "result", "pinned", "session_id",
            "parallel", "parallel_reason", "workflow_run", "workflow_step", "points", "points_why")
    body = task.get("body") or ""
    return {**{k: task.get(k) for k in keys},
            "body": body if len(body) <= BODY_CHARS else body[:BODY_CHARS - 1] + "…"}


def flow_of(store: Store, task_id: int) -> dict[str, Any] | None:
    """A task's way through the matrix: its kind, its ladder and where it started, then every attempt
    with the cell it ran in, how it ended and the move that followed. None for a task locks never planned
    (a prompt answered in a session)."""
    planned = store.last_event(task_id, "planned")
    if planned is None:
        return None
    keys = ("seq", "cell", "passed", "failure", "move", "move_reason", "cost_usd", "turns", "duration_s")
    attempts = store.attempts(task_id)
    return {"kind": planned["data"].get("kind"), "start": planned["data"].get("start"),
            "ladder": planned["data"].get("ladder") or [], "reasons": planned["data"].get("reasons") or [],
            "steps": [{**{k: a.get(k) for k in keys}, "climb": c}
                      for a, c in zip(attempts, climbs_of(store, task_id, attempts), strict=True)]}


#: Moves after which another attempt ran, and the dial each one turned.
_AXIS = {"retry": "retry", "more_effort": "effort", "next_model": "model", "more_turns": "turns"}


def climbs_of(store: Store, task_id: int, attempts: list[dict]) -> list[dict[str, Any] | None]:
    """For each attempt, the move that followed it: from which cell to which, along
    which dial, and the evidence. A run from before the moves kept their evidence
    gets what its attempts say (the destination is the next attempt's cell)."""
    recorded = {e["data"]["seq"]: e["data"] for e in store.events_of(task_id, "moved") if "seq" in e["data"]}
    out: list[dict[str, Any] | None] = []
    for i, a in enumerate(attempts):
        if not a.get("move"):
            out.append(None)
            continue
        found = recorded.get(a["seq"])
        if found is not None:
            out.append({"from": found.get("from_cell"), "to": found.get("to_cell"), "axis": found.get("axis"),
                        "trigger": found.get("trigger"), "because": found.get("because") or [],
                        "skipped": found.get("skipped") or [], "turns_from": found.get("turns_from"),
                        "turns_to": found.get("turns_to"), "spent_usd": found.get("spent_usd")})
            continue
        following = attempts[i + 1] if i + 1 < len(attempts) and a["move"] in _AXIS else None
        out.append({"from": a["cell"], "to": following["cell"] if following else None,
                    "axis": _AXIS.get(a["move"], "stop"), "trigger": a.get("failure"),
                    "because": [a["move_reason"]] if a.get("move_reason") else [], "skipped": [],
                    "recovered": True})
    return out


def worker_of(store: Store, task: dict[str, Any], steps: int) -> dict[str, Any] | None:
    """The one-shot worker a running task has out now: a `claude -p` that locks launched for this
    attempt, with the cell, limits and capabilities it was given. None between attempts."""
    started = store.last_event(task["id"], "attempt_started")
    if started is None or started["data"].get("seq", 0) <= steps:
        return None
    d = started["data"]
    return {"seq": d.get("seq"), "cell": d.get("cell"), "model": d.get("model"), "max_turns": d.get("max_turns"),
            "capabilities": d.get("capabilities") or [],
            "started_at": started["ts"], "alive": flow.alive(task.get("pid"))}


def board(store: Store, repos: set[str] | None = None, session: str | None = None) -> dict[str, Any]:
    """Every lane's state; with `repos`, only those repositories' tasks and lanes. With
    `session`, the counts say how many of the waits are that session's work (`mine`): a
    project's board holds every session's, and a status line counts the one it is in."""
    tasks = store.list_tasks(limit=500, source=TASK_SOURCES)
    needs, running, done = [], [], []
    slow_seen: dict[tuple[str, str], list[float]] = {}
    for t in tasks:
        if repos is not None and t["repo"] not in repos:
            continue
        item = _brief(t)
        if t["status"] in NEEDS_YOU or t["status"] in ("running", "done"):
            item["flow"] = flow_of(store, t["id"])
        if t["status"] in NEEDS_YOU:
            # The generic ask is the fallback; the account says what stopped this one, who did, and the next step.
            needs.append({**item, "asks": NEEDS_YOU[t["status"]], "stop": stops.view(store, t),
                          "waits_on": stops.waits_on(store, t)})
        elif t["status"] == "running":
            item.update(usage.so_far(store, t))
            started = store.last_event(t["id"], "attempt_started")
            item["attempt"] = started["data"].get("seq") if started else None
            item["phase"] = phase.of(t["id"], item["attempt"])
            steps = len((item.get("flow") or {}).get("steps", []))
            item["worker"] = worker_of(store, t, steps) if item.get("flow") else None
            item["slow"] = flow.slow(store, t, seen=slow_seen)
            # How far it got: time against its kind's usual, its stage, what it wrote, a quiet worker.
            item["progress"] = progress.of(store, t)
            item["progress_line"] = progress.line(item["progress"])
            item["files_line"] = progress.files_line(item["progress"])
            running.append(item)
        elif t["status"] == "done":
            finished = store.final_event(t["id"])
            branch = finished["data"].get("branch") if finished else None
            item["branch"] = branch
            done.append(item)
            if (branch and _holds_work(finished) and t["updated_at"] >= _days_ago_iso(REVIEW_DAYS)
                    and not _landed(t, branch, finished)):
                needs.append({**item, "asks": f"review branch {branch}, then merge it", "waits_on": "person"})
    # A prompt answered in a session is recorded as a task too (it carries the
    # session's follow-ups and result), but it is not work on the board: every
    # turn would be a card. The sessions answering right now are listed apart.
    answering = [_brief(t) for t in store.list_tasks(status=["running"], limit=100, source=("hook",))
                 if repos is None or t["repo"] in repos]
    lanes = {lane["repo"]: lane for lane in store.lanes()}
    queued: dict[str, dict[str, Any]] = defaultdict(lambda: {"tasks": []})
    for t in reversed(store.list_tasks(status=["queued"], limit=500)):
        if repos is not None and t["repo"] not in repos:
            continue
        lane = queued[t["repo"] or ""]
        lane["repo"] = t["repo"] or ""
        lane["paused"] = bool(lanes.get(t["repo"] or "", {}).get("paused"))
        lane["reason"] = lanes.get(t["repo"] or "", {}).get("reason")
        lane["tasks"].append({**_brief(t), "waits": dispatch.waiting(store, t)})
    for repo, lane in lanes.items():
        if lane["paused"] and repo not in queued and (repos is None or repo in repos):
            queued[repo] = {"repo": repo, "paused": True, "reason": lane["reason"], "tasks": []}
    for lane in queued.values():
        lane["dispatching"] = dispatch.held(store.path.parent, lane["repo"] or "*")
    # The workers this machine runs at once, every repository's together, and each lane's share.
    ceiling = config.max_parallel(os.environ)
    level = capacity.current(store.path.parent, ceiling=ceiling)
    shared = flow.shares(store, level.value)
    for lane in queued.values():
        if (share := shared.get(lane["repo"] or None)) is not None:
            lane["share"] = share.data()
    return {
        "capacity": {"level": level.value, "line": capacity.line(level), "ceiling": ceiling,
                     "lanes": {k or "": v.data() for k, v in shared.items() if repos is None or k in repos}},
        "counts": {"needs_you": sum(1 for n in needs if n["waits_on"] == "person"),
                   "orchestrator": sum(1 for n in needs if n["waits_on"] == "orchestrator"), "running": len(running),
                   **({"mine": sum(1 for n in needs if n.get("session_id") == session)} if session else {}),
                   "workers": sum(1 for r in running if r.get("worker")),
                   "queued": sum(len(v["tasks"]) for v in queued.values()), "done": len(done),
                   "answering": len(answering)},
        "needs_you": needs,
        "running": running,
        "answering": answering,
        "queued": sorted(queued.values(), key=lambda lane: lane["repo"]),
        # By when each ended: a task resumed today belongs to today, whatever its id.
        "done": sorted(done, key=lambda t: t["updated_at"] or "", reverse=True)[:60],
        "last_event": store.last_event_id(),
    }


def overview(store: Store, hours: float = 24, limit: int = 20, repos: set[str] | None = None) -> dict[str, Any]:
    """Every session seen in the last `hours`, with how its locks work stands: for
    one person, or one agent, watching sessions in several projects at once. Each
    task that is open (waits, runs, queued) is listed, and each that ended in the
    window. A read: no ending is claimed, a session still hears of its own. With
    `repos`, only those repositories' sessions and tasks."""
    since = _hours_ago_iso(hours)
    full = board(store, repos=repos)
    open_cards: dict[int, dict[str, Any]] = {}
    for card in full["needs_you"]:
        stop = card.get("stop") or {}
        open_cards[card["id"]] = {"state": "needs_you", "waits_on": card.get("waits_on"),
                                  "asks": stop.get("todo") or card["asks"],
                                  "cause": stop.get("cause"), "next": stop.get("next")}
    for card in full["running"]:
        worker = card.get("worker") or {}
        open_cards[card["id"]] = {"state": "running", "attempt": card.get("attempt"),
                                  "cell": worker.get("cell") or card.get("current_cell"),
                                  "since": worker.get("started_at"), "alive": worker.get("alive")}
    for lane in full["queued"]:
        for card in lane["tasks"]:
            open_cards[card["id"]] = {"state": "queued", "paused": lane["paused"], "reason": lane["reason"]}

    def line(t: dict[str, Any]) -> dict[str, Any]:
        return {"id": t["id"], "title": t["title"], "status": t["status"], "repo": t["repo"],
                "workflow_run": t.get("workflow_run"), "workflow_step": t.get("workflow_step"),
                "cell": t.get("final_cell") or t.get("current_cell"), **usage.so_far(store, t),
                "updated_at": t["updated_at"], **open_cards.get(t["id"], {"state": "ended"})}

    by_session: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for t in store.list_tasks(limit=1000, source=TASK_SOURCES):
        if repos is not None and t["repo"] not in repos:
            continue
        if t["id"] in open_cards or (t["updated_at"] or "") >= since:
            by_session[t.get("session_id")].append(line(t))
    out = []
    for s in sessions(store, repos=repos, limit=limit * 5):
        tasks = by_session.pop(s["id"], [])
        if (s["last_seen_at"] or "") < since and not any(t["state"] != "ended" for t in tasks):
            continue
        counts = Counter(t["state"] for t in tasks)
        out.append({"id": s["id"], "name": s["name"], "cwd": s["cwd"], "repo": s["repo"],
                    "last_seen_at": s["last_seen_at"], "last_prompt": s["last_prompt"],
                    "answering": bool(s.get("running")), "resume": s["resume"], "counts": dict(counts),
                    "tasks": tasks})
        if len(out) >= limit:
            break
    # Work no listed session sent: the queue's, a terminal's, a session not enrolled.
    rest = [t for tasks in by_session.values() for t in tasks]
    # Workflow runs still going, or that ended in the window: where each is, step by step.
    from locks import workflows

    flows = [w for w in workflows.runs(store, repos=repos, limit=limit)
             if w["state"] in ("running", "waits on the orchestrator", "stalled") or (w["updated_at"] or "") >= since]
    return {"since": since, "sessions": out, "other": sorted(rest, key=lambda t: t["id"], reverse=True),
            "workflows": flows}


def _holds_work(finished: dict | None) -> bool:
    """Whether a kept branch holds anything to review. A branch committed before
    tool artifacts were left out may hold only those: a browser's logs and
    snapshots, nothing a person merges. Unrecorded is still asked for."""
    from locks.isolate import is_artifact

    changed = (finished or {}).get("data", {}).get("changed") or []
    return not changed or not all(is_artifact(path) for path in changed)


def _landed(task: dict, branch: str, finished: dict | None) -> bool:
    """A branch merged (or squash-merged, or deleted) waits on nobody. A read of
    git, never a write; unknown, it is still asked for."""
    from locks import isolate

    where = Path(task["cwd"]) if task.get("cwd") else None
    if where is None or not where.is_dir():
        return False
    try:
        changed = (finished or {}).get("data", {}).get("changed") or []
        return isolate.landed(where, branch, changed)
    except OSError:
        return False


def task_detail(store: Store, task_id: int) -> dict[str, Any] | None:
    task = store.get_task(task_id)
    if task is None:
        return None
    attempts = store.attempts(task_id)
    refused = store.denials(task_id)
    tails = store.check_tails(task_id)
    for a, climb in zip(attempts, climbs_of(store, task_id, attempts), strict=True):
        a["check_tail"] = tails.get(a["seq"], "")
        a["changed_paths"] = json.loads(a["changed_paths"] or "[]")
        a["capabilities"] = json.loads(a["capabilities"] or "[]")
        a["denied"] = refused.get(a["seq"], [])
        a["climb"] = climb
    finished = store.final_event(task_id)
    children = store.children(task_id)
    turns = usage.cost_by(store, "task_id", [task_id, *(c["id"] for c in children)])
    return {
        "task": {**_brief(task), **usage.so_far(store, task), "body": task["body"],
                 "options": json.loads(task.get("options") or "{}"),
                 "class_source": task["class_source"], "class_reason": task["class_reason"],
                 # What the session's own turns on it cost, its subagents' included; cost_usd is workers'.
                 "turns_usd": round(sum(turns.values()), 4), "waits": dispatch.waiting(store, task)},
        "plan": (store.last_event(task_id, "planned") or {}).get("data"),
        "attempts": attempts,
        "messages": [{**m, "author": author(m["role"], task)} for m in store.messages(task_id)],
        "children": [{**_brief(c), "turns_usd": turns.get(c["id"], 0.0)} for c in children],
        "stop": stops.view(store, task),
        "branch": finished["data"].get("branch") if finished else None,
        "impact": finished["data"].get("impact", []) if finished else [],
        # What it was shown when it ran; the memory as it is now would include later ones.
        "dead_ends": ((store.last_event(task_id, "dead_ends") or {}).get("data") or {}).get("shown") or [],
        "events": store.events(task_id=task_id, limit=500),
        "checks": [e["data"] for e in store.events_of(task_id, "verified")],
        # Where the risk ended: attempts only ever raise it from the plan's.
        # The size the work turned out to be, beside the plan's estimate on the task.
        "realized": {"points": finished["data"]["realized"], "reasons": finished["data"].get("realized_why") or []}
        if finished and finished["data"].get("realized") else None,
        "risk": (finished["data"].get("risk") if finished else None)
        or ((store.last_event(task_id, "planned") or {}).get("data") or {}).get("risk"),
        **_live(store, task, attempts),
    }


def _live(store: Store, task: dict[str, Any], attempts: list[dict] | None = None) -> dict[str, Any]:
    """What a running task's worker is doing now: the attempt in flight, its phase and its last
    tool calls (`phase.calls`, signatures only), read from the tool-call log's tail. Nothing for a
    task that is not running: its calls are over."""
    if task["status"] != "running":
        return {"attempt_now": None, "phase": None, "recent_calls": []}
    started = store.last_event(task["id"], "attempt_started")
    seq = started["data"].get("seq") if started else None
    if not seq:
        ran = attempts if attempts is not None else store.attempts(task["id"])
        seq = ran[-1]["seq"] + 1 if ran else 1
    found = progress.of(store, task)
    # With the person's trace on, what each call ran and printed, from their own trace.
    from .. import trace as tracing

    traced = tracing.worker_calls(os.environ, phase.session_of(task["id"], seq), task["id"])
    return {"attempt_now": seq, "phase": phase.of(task["id"], seq), "recent_calls": phase.calls(task["id"], seq),
            "traced": traced, "trace_on": tracing.enabled(os.environ),
            "progress": found, "progress_line": progress.line(found), "files_line": progress.files_line(found)}


def task_live(store: Store, task_id: int) -> dict[str, Any] | None:
    """The light read a task page polls while its task runs (tool calls write no events): its
    state and what its worker is doing now. Read only."""
    task = store.get_task(task_id)
    if task is None:
        return None
    return {"id": task_id, "status": task["status"], "updated_at": task["updated_at"], **_live(store, task)}


#: The most of a diff the UI is sent.
DIFF_BYTES = 200_000


def task_diff(store: Store, task_id: int) -> dict[str, Any] | None:
    """What a task's kept branch changed against where it left the checkout, read
    from git. Only read: no checkout, no fetch, nothing written."""
    import subprocess

    task = store.get_task(task_id)
    if task is None:
        return None
    if task["status"] == "running":
        # Nothing kept yet: what its worker wrote so far, read from its worktree.
        started = store.last_event(task_id, "attempt_started")
        begun = (started or {}).get("data") or {}
        found = progress.peek(task_id, begun.get("base"), places=begun.get("places"))
        if found is None:
            return {"branch": None, "diff": "", "why": "it works in no worktree of its own, or git cannot say",
                    "running": True}
        return {"branch": None, "diff": found["diff"], "cut": found["cut"], "running": True,
                "why": "" if found["diff"] else "nothing written yet"}
    finished = store.final_event(task_id)
    branch = (finished["data"].get("branch") if finished else None) or f"locks/task-{task_id}"
    where = task.get("cwd")
    if not where or not Path(where).is_dir():
        return {"branch": branch, "diff": "", "why": "the task's checkout is not here"}
    try:
        proc = subprocess.run(["git", "diff", "--stat", "--patch", "--no-color", f"HEAD...{branch}"], cwd=where,
                              capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"branch": branch, "diff": "", "why": f"git did not answer: {exc}"}
    if proc.returncode != 0:
        return {"branch": branch, "diff": "", "why": "no kept branch to show: " + proc.stderr.decode(
            "utf-8", "replace").strip()[:200]}
    raw = proc.stdout[:DIFF_BYTES].decode("utf-8", "replace")
    return {"branch": branch, "diff": raw, "cut": len(proc.stdout) > DIFF_BYTES, "why": "" if raw else "no change"}


#: Who wrote a message, in words.
AUTHORS = {"person": "you", "orchestrator": "main session (orchestrator)", "assistant": "main session's answer",
           "subagent": "subagent's report",
           "worker": "locks report"}


def author(role: str, task: dict[str, Any]) -> str:
    """Who wrote a message. A session's own prompt is the person's. Work recorded
    before authors were kept says `user` whoever wrote it; sent from a session,
    it was almost always the main session, and the label says it is inferred."""
    if role in AUTHORS:
        return AUTHORS[role]
    if role == "user":
        if task.get("source") == "hook" or not task.get("session_id"):
            return "you"
        return "main session (orchestrator, inferred: recorded before locks kept who wrote it)"
    return role


def spend(store: Store, days: int = 7) -> dict[str, Any]:
    report = usage.spend(store, days=days)
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in store.spend_by_day(days=days):
        if row["kind"] == "session":
            fam = usage.family(row["model"])
            by_day[row["day"]][fam] += usage.dollars(row["model"], row["input_tokens"], row["output_tokens"],
                                                     row["cache_read"], row["cache_write"])
        else:
            by_day[row["day"]]["workers"] += row["usd"] or 0
    report["by_day"] = [{"day": d, **{k: round(v, 4) for k, v in parts.items()}} for d, parts in sorted(by_day.items())]
    return report


def analysis_view(store: Store, days: int = 30, repo_key: str | None = None) -> dict[str, Any]:
    """Where locks' time and money go (`locks.analysis`): one project, or every one."""
    from locks import analysis

    return analysis.compute(store, days=days, repo=repo_key or None, keep_runs=12)


def evolution_view(store: Store, days: int = 30, repo_key: str | None = None) -> dict[str, Any]:
    """How locks evolves and why (`locks.evolution`): read only, as every GET is."""
    from locks import evolution

    return evolution.report(store, days=days, repo=repo_key or None)


#: What Routing says passed where no worker ran.
NO_WORKER = "no worker"


def routing(store: Store, days: int = 0) -> list[dict[str, Any]]:
    """`days` keeps the last few days only (0: everything): a rule fixed last
    week should not keep showing as a rung that fails."""
    since = _days_ago_iso(days) if days else ""
    stats: dict[str, dict[str, Any]] = {}
    for row in store.routing_stats(since):
        kind = stats.setdefault(row["kind"], {"kind": row["kind"], "tasks": 0, "passed": 0, "climbed": 0,
                                              "pinned": 0, "cost_usd": 0.0, "starts": Counter(), "passes": Counter()})
        kind["tasks"] += 1
        kind["cost_usd"] += row["cost_usd"] or 0
        kind["pinned"] += row["pinned"]
        kind["starts"][row["start_cell"]] += 1
        if row["status"] == "done":
            kind["passed"] += 1
            # A workflow step that passed with no worker (a clean join, a ledger the verifier decided)
            # has no cell: said so, never shown as a cell named "null".
            kind["passes"][row["final_cell"] or NO_WORKER] += 1
        if row["attempts"] > 1:
            kind["climbed"] += 1
    moves: dict[str, Counter] = defaultdict(Counter)
    landed: dict[str, Counter] = defaultdict(Counter)
    for m in store.moves(since):
        to = m["next_cell"] if m["move"] in _AXIS else None
        if m["move"] in _AXIS and to is None:
            # A move that goes on but has no next attempt: still running, or
            # cancelled or dismissed first. Not a stop the router made.
            to = f"— {m['task_status']}, no next attempt"
        key = (m["cell"], m["move"], to, m["failure"] or "inconclusive")
        moves[m["kind"]][key] += 1
        if to and m["next_passed"]:
            landed[m["kind"]][key] += 1
    out = []
    for name, ladder in LADDERS.items():
        k = stats.get(name, {"kind": name, "tasks": 0, "passed": 0, "climbed": 0, "pinned": 0, "cost_usd": 0.0,
                             "starts": Counter(), "passes": Counter()})
        out.append({
            **{key: k[key] for key in ("kind", "tasks", "passed", "climbed", "pinned")},
            "cost_usd": round(k["cost_usd"], 4),
            "ladder": [c.label for c in ladder],
            "starts": dict(k["starts"]),
            "passes": dict(k["passes"]),
            # How this ladder is really climbed: each move with the failure behind it, how often, and how
            # often the next attempt passed. The evidence for moving a start or a rung.
            "climbs": [{"from": f, "move": mv, "to": to, "after": why, "times": n,
                        "then_passed": landed[name][(f, mv, to, why)] if to and not to.startswith("—") else None}
                       for (f, mv, to, why), n in moves[name].most_common()],
        })
    return out


def memory(store: Store, query: str = "", limit: int = 40, repos: set[str] | None = None) -> list[dict[str, Any]]:
    """Problems and every fix tried on them; with `repos`, only those repositories'."""
    if query.strip():
        found = store.search(query, limit=200 if repos is not None else limit)
        return [p for p in found if repos is None or p["repo"] in repos][:limit]
    return store.recent_problems(repos=repos, limit=limit)


def notes_view(store: Store, repo_key: str, *, topic: str = "", query: str = "", state: str = "") -> dict[str, Any]:
    """One project's notes: the topics with their counts, and the notes asked for,
    each with its anchors and links. Read only: a GET never reviews."""
    from locks import notes

    counts = store.note_counts(repo_key)
    topics = [{"name": name, "description": about,
               "live": sum(counts.get(name, {}).get(s, 0) for s in notes.LIVE),
               "review": counts.get(name, {}).get("review", 0)}
              for name, about in notes.topics(store, repo_key).items()]
    states = [state] if state in notes.STATES else list(notes.LIVE)
    in_topics = [topic] if topic else None
    if query.strip():
        found = notes.recall(store, repo_key, query, in_topics=in_topics, limit=40, hops=1, states=states)
    else:
        rows = store.notes(repo_key, topics=in_topics, states=states, limit=200)
        found = notes.expand(store, [r["id"] for r in rows])
    titles = {n["id"]: n["title"] for n in found}
    for n in found:
        for x in n["links"]:
            if x["to"] not in titles:
                other = store.get_note(x["to"])
                titles[x["to"]] = other["title"] if other else ""
            x["title"] = titles[x["to"]]
    proposals = [{"id": n["id"], "title": n["title"], "topic": n["proposed_topic"]}
                 for n in store.notes(repo_key, states=notes.LIVE) if n["proposed_topic"]]
    return {"topics": topics, "notes": found, "proposals": proposals}


def projects(store: Store, enrolled_only: bool = True) -> list[dict[str, Any]]:
    """Every repository locks has worked in, at the top of the checkout it was last used from.
    Only enrolled ones (a `.locks/` folder) unless asked: the plugin being on is not using locks."""
    from locks import repo as repo_mod
    from locks import workspaces

    out = []
    for p in store.projects():
        where = Path(p["cwd"]) if p["cwd"] else None
        # A workspace is shown as its folder, whichever of its repositories was used last.
        top = (workspaces.of(where) or repo_mod.toplevel(where)) if where is not None and where.is_dir() else None
        enrolled = project.find(where) is not None if where is not None else False
        if enrolled_only and not enrolled:
            continue
        out.append({**p, "dir": str(top or where or ""), "exists": bool(where and where.is_dir()),
                    "enrolled": enrolled})
    return out


def sessions(store: Store, repos: set[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Claude Code sessions in enrolled projects — those with a `.locks/` folder in the session's
    directory or the checkout above it — with their name and the command that resumes each."""
    import shlex

    out, names = [], {}
    for s in store.sessions(repos=repos, limit=limit * 5):
        folder = project.find(s["cwd"])
        if folder is None:
            continue
        if folder not in names:
            names[folder] = project.names(folder)
        entry = names[folder].get(s["id"])
        name = str((entry or {}).get("name") or "").strip() or None
        out.append({**s, "resume": f"cd {shlex.quote(s['cwd'] or '.')} && claude --resume {s['id']}",
                    "name": name, "named_by": ("you" if project.person_named(entry) else "haiku") if name else None})
        if len(out) >= limit:
            break
    turns = usage.cost_by(store, "session_id", [s["id"] for s in out])
    return [{**s, "turns_usd": turns.get(s["id"], 0.0)} for s in out]


SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def claude_tasks(session_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The session's own task list, as Claude Code keeps it: one JSON file per task
    under `<config>/tasks/<session>/` (older versions: one todo file under `todos/`).

    Read only, and read defensively: the layout is Claude Code's, not a contract
    locks owns. `state` says which of absent, read and unreadable it was — "no
    plan" and "could not read the plan" are different answers.
    """
    env = os.environ if env is None else env
    if not SESSION_ID.match(session_id or ""):
        return {"state": "absent", "tasks": []}
    config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    folder = config_dir / "tasks" / session_id
    try:
        if folder.is_dir():
            tasks = []
            for f in folder.glob("*.json"):
                item = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(item, dict) and item.get("subject"):
                    tasks.append({k: item.get(k) for k in
                                  ("id", "subject", "description", "status", "activeForm", "blockedBy")})
            tasks.sort(key=lambda t: (len(str(t["id"])), str(t["id"])))
            return {"state": "read", "tasks": tasks}
        todos = sorted((config_dir / "todos").glob(f"{session_id}-agent-*.json"))
        if todos:
            items = json.loads(todos[-1].read_text(encoding="utf-8"))
            tasks = [{"id": str(i + 1), "subject": t.get("content"), "description": None, "status": t.get("status"),
                      "activeForm": t.get("activeForm"), "blockedBy": []}
                     for i, t in enumerate(items if isinstance(items, list) else []) if isinstance(t, dict)]
            return {"state": "read", "tasks": tasks}
    except (OSError, ValueError):
        return {"state": "unreadable", "tasks": []}
    return {"state": "absent", "tasks": []}


def _plan_counts(session_id: str) -> dict[str, int] | None:
    plan = claude_tasks(session_id)
    if not plan["tasks"]:
        return None
    return {"done": sum(1 for t in plan["tasks"] if t["status"] == "completed"), "total": len(plan["tasks"])}


def session_list(store: Store, repos: set[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """`sessions`, each with how far its own task list got."""
    return [{**s, "plan": _plan_counts(s["id"])} for s in sessions(store, repos, limit)]


def session_detail(store: Store, session_id: str, turns: int = 60) -> dict[str, Any] | None:
    """One session: its own task list, the work it handed to locks, and its recent turns."""
    found = [s for s in sessions(store, limit=100000) if s["id"] == session_id]
    if not found:
        return None
    mine = store.list_tasks(session_id=session_id, limit=500)
    return {
        "session": found[0],
        "plan": claude_tasks(session_id),
        "tasks": [_brief(t) for t in mine if t["source"] in TASK_SOURCES],
        "turns": [{**_brief(t), "result": (t["result"] or "")[:400]} for t in mine if t["source"] == "hook"][:turns],
    }


def habit_view(store: Store, repo_key: str | None = None, days: int = 30) -> dict[str, Any]:
    """One project's habits: mined from its own events, the ones other projects
    share that it has not, and the ones installed in it. Without a project,
    every repository's events mined together."""
    from locks import repo as repo_mod

    habits.load_events(store)
    events = store.tool_events(days=days)
    if repo_key:
        cwd = next((p["cwd"] for p in store.projects() if p["repo"] == repo_key), None)
        here, elsewhere = habits.for_repo(events, repo_key, habits.checkout_of(cwd))
    else:
        here, elsewhere = habits.candidates(events), []
    installed = [{**h, "steps": json.loads(h["steps"])} for h in store.habits()]
    if repo_key:
        installed = [h for h in installed if repo_mod.key(Path(h["settings_path"]).parent.parent) == repo_key]
    return {"repo": repo_key, "candidates": [c.data() for c in here[:40]],
            "elsewhere": [c.data() for c in elsewhere[:20]], "installed": installed}


def teams_view(repo_key: str | None = None) -> dict[str, Any]:
    """Every team and every role, in full: who makes up each team, what each role
    is told and carries, which projects use which team, and which one this
    project uses. Read from the person's locks home and locks' own files only."""
    from locks import roles, teams

    catalog = roles.load()
    found, refused = teams.listing(catalog)
    here = teams.of(repo_key, catalog)[0].name if repo_key else None
    every = []
    for role in catalog.roles.values():
        briefing = roles.brief(role)
        every.append({**role.summary(), "prompt": role.prompt, "carried": list(briefing.skills),
                      "missing": list(briefing.missing),
                      "teams": [t.name for t in found if role.name in t.role_names]})
    skills = {s.name: s.description for s in roles.bundled_skills()}
    return {"here": here, "teams": [teams.describe(t, catalog) for t in found], "roles": every,
            "refused": {**catalog.refused, **{f"team {k}": v for k, v in refused.items()}},
            "skills": skills, "assignments": teams.assignments()}


def _hours_ago_iso(hours: float) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _days_ago_iso(days: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


# --- workflows, the project map and its milestones ---------------------------------------------


def _project_dir(store: Store, repo_key: str) -> str | None:
    """The checkout a project was last used from: where its project-scope workflows live."""
    return next((p["dir"] for p in projects(store, enrolled_only=False) if p["repo"] == repo_key and p["exists"]),
                None)


def workflows_view(store: Store, repo_key: str, *, limit: int = 30) -> dict[str, Any]:
    """One project's workflow runs, each a graph of its steps with their state and the
    progress made, and the definitions it can run: version, the runs holding each and a
    change waiting for them. Read only."""
    from locks import workflows

    runs = []
    for run in workflows.runs(store, repos={repo_key}, limit=limit):
        done = sum(1 for s in run["steps"] if s["state"] == "done")
        runs.append({**run, "progress": {"done": done, "total": len(run["steps"])}})
    where = _project_dir(store, repo_key)
    definitions = []
    for entry in workflows.listing(where):
        if entry["shadowed"]:
            continue
        item = {k: entry.get(k) for k in ("name", "scope", "description", "error")}
        if not entry.get("error"):
            defn = workflows.load(entry["name"], where, entry["scope"])
            item.update(version=defn["version"], inputs=defn["inputs"],
                        steps=[{k: st.get(k) for k in ("id", "title", "kind", "needs", "memory")}
                               for st in defn["steps"]])
            if entry["scope"] != "bundled":
                item.update(held_by=workflows.in_use(entry["path"], store),
                            pending=workflows.pending_path(Path(entry["path"])).is_file())
        definitions.append(item)
    return {"runs": runs, "definitions": definitions, "dir": where}


def session_tab(session_id: str) -> dict[str, str]:
    """The one browser tab a session gets (`locks ui --tabs`): the page's fragment that opens the
    session, whose page lists every piece of its work and opens each inside it (`#session=<id>`)."""
    return {"key": f"session:{session_id}", "title": f"session {session_id[:8]}", "fragment": f"session={session_id}"}


#: How many notes and problems the map draws before it says how many it left out.
MAP_NOTES, MAP_PROBLEMS = 150, 40


def map_view(store: Store, repo_key: str) -> dict[str, Any]:
    """One project as a mind map: the project, its topics, each live note, the code each
    note is about, and the problems worked on here with every fix tried. A tree of `nodes`
    (each with its `parent`) and the typed links between notes as cross `edges`. Built
    from what locks keeps, no model; read only."""
    from locks import notes

    nodes: list[dict[str, Any]] = [{"id": "root", "parent": None, "kind": "project", "label": repo_key}]
    rows = store.notes(repo_key, states=notes.LIVE, limit=MAP_NOTES + 1)
    found = notes.expand(store, [r["id"] for r in rows[:MAP_NOTES]])
    topics = sorted({n["topic"] for n in found})
    for topic in topics:
        nodes.append({"id": f"topic:{topic}", "parent": "root", "kind": "topic", "label": topic})
    paths: set[tuple[str, str]] = set()
    for n in found:
        nodes.append({"id": f"note:{n['id']}", "parent": f"topic:{n['topic']}", "kind": "note", "ref": n["id"],
                      "label": n["title"], "state": n["state"], "text": n["text"], "author": n["author"]})
        for a in n["anchors"]:
            key = (f"note:{n['id']}", a["path"])
            if key not in paths:
                paths.add(key)
                nodes.append({"id": f"anchor:{n['id']}:{a['path']}", "parent": f"note:{n['id']}", "kind": "anchor",
                              "label": f"{a['symbol']} ({a['path']})" if a.get("symbol") else a["path"]})
    shown = {n["id"] for n in found}
    edges = [{"from": f"note:{x['from_id']}", "to": f"note:{x['to_id']}", "kind": x["kind"]}
             for x in store.note_links(shown) if x["from_id"] in shown and x["to_id"] in shown]
    problems = store.recent_problems(repos=[repo_key], limit=MAP_PROBLEMS + 1)
    if problems:
        nodes.append({"id": "problems", "parent": "root", "kind": "topic", "label": "problems"})
    for p in problems[:MAP_PROBLEMS]:
        nodes.append({"id": f"problem:{p['id']}", "parent": "problems", "kind": "problem", "ref": p["id"],
                      "label": p["title"], "state": p["state"], "text": p.get("symptom") or ""})
        for f in p["fixes"]:
            outcome = "disproved" if f.get("invalidated_on") else f["outcome"]
            nodes.append({"id": f"fix:{f['id']}", "parent": f"problem:{p['id']}", "kind": "fix", "ref": f["id"],
                          "label": f["description"], "state": outcome, "text": f.get("why") or "",
                          "task": f.get("task_id")})
    return {"nodes": nodes, "edges": edges,
            "left_out": {"notes": max(0, len(rows) - MAP_NOTES), "problems": max(0, len(problems) - MAP_PROBLEMS)}}


#: Task kinds whose pass is a step in where the project stands, not routine work.
MILESTONE_KINDS = ("feature", "refactor", "plan")


def milestones(store: Store, repo_key: str, *, limit: int = 60) -> list[dict[str, Any]]:
    """Where the project stands, newest first, from what already happened, no model:
    workflow runs that finished, the decisions its notes keep, and the features and
    refactors that passed."""
    from locks import notes, workflows

    out: list[dict[str, Any]] = []
    for run in workflows.runs(store, repos={repo_key}, limit=limit):
        if run["state"] == "done":
            goal = "; ".join(f"{k}: {v}" for k, v in run["inputs"].items())
            out.append({"at": run["updated_at"], "kind": "workflow", "title": f"{run['name']} — {goal}" if goal
                        else run["name"], "ref": run["id"], "detail": f"{len(run['steps'])} steps, "
                        f"${run['cost_usd']:.2f}" + (f", branch {run['branch']}" if run["branch"] else "")})
    for n in store.notes(repo_key, topics=["decisions"], states=notes.LIVE, limit=limit):
        out.append({"at": n["created_at"], "kind": "decision", "title": n["title"], "ref": n["id"],
                    "detail": n["text"][:300]})
    for t in store.list_tasks(repo=repo_key, status=["done"], limit=limit * 4, source=TASK_SOURCES):
        if t["kind"] in MILESTONE_KINDS and not t.get("workflow_run"):
            out.append({"at": t["updated_at"], "kind": t["kind"], "title": t["title"], "ref": t["id"],
                        "detail": f"passed at {t['final_cell'] or '-'}, ${t['cost_usd'] or 0:.2f}"})
    out.sort(key=lambda m: m["at"] or "", reverse=True)
    return out[:limit]


def attach_note(store: Store, repo_key: str, workflow: str, step: str, note_id: int,
                scope: str | None = None) -> dict[str, Any]:
    """A person's press: carry a note of this project into a workflow step's brief. Saved
    like any change, so a workflow in use gets it as a pending revision."""
    from locks import workflows

    note = store.get_note(note_id)
    if note is None or note["project"] != repo_key:
        raise workflows.WorkflowError(f"no note #{note_id} in this project")
    ref = f"note:{note_id}"

    def change(defn: dict[str, Any]) -> None:
        current = defn["steps"][workflows._index(defn, step)].get("memory") or []
        if ref not in current:
            workflows.set_step(defn, step, {"memory": [*current, ref]})

    saved = workflows.edit(workflow, _project_dir(store, repo_key), change, scope)
    return {"scope": saved.scope, "path": str(saved.path), "held_by": saved.held_by}
