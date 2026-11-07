"""A workflow step whose first attempt forks the session of a reading step before it (its
`trunk`): what it forks, whether it does, the note that marks the role it takes from there,
and the trial that keeps forking only where it pays. stdlib; no model.

A trunk reads and decides nothing for the steps that fork it: it runs with no role in its
system prompt (its role goes in its brief), since Claude Code records a session's system
prompt on its first request and every resume and fork sends that record as it is
(`--system-prompt-snapshot`). A branch's role and its capabilities' hints therefore reach
it as its first message, after the fork point: the trunk's prefix stays the same for every
branch, and the prompt cache serves it to each one on the trunk's model.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from locks import arms as arms_mod
from locks.store import Store

#: The kinds a trunk may be: reading, and nothing it says is the work under judgment.
TRUNK_KINDS = frozenset({"explore", "plan"})
#: The only trunk a step that judges may fork: one that read and decided nothing. A plan
#: holds an approach, and a blind review must not start from one.
BLIND_TRUNKS = frozenset({"explore"})
#: Ledger steps that decide rows read only their rows: they never fork.
NEVER_FORK = frozenset({"refute", "recheck"})
#: A trunk whose context held more than this many tokens at its end is not forked: its
#: branch would start with little room before it compacts.
MAX_TOKENS = 120_000
#: The trial: the arms alternate by task until each has `TRIAL` first tries; then forking
#: stays only where its first tries passed no worse than `MARGIN` below the fresh ones and
#: reached their first change in fewer calls or finished sooner. It ends early, lost, once
#: each arm has `EARLY` and forked first tries pass worse by more than `EARLY_MARGIN`.
TRIAL, MARGIN = 10, 0.1
EARLY, EARLY_MARGIN = 5, 0.2
ARMS = ("fork", "fresh")
#: The key the arm is kept under, on the task's first `briefed` event.
KEY = "trunk_arm"


def problems(steps: Sequence[Mapping[str, Any]], ids: Sequence[str]) -> list[str]:
    """What is wrong with the steps' `trunk`: it names a reading step before the step that
    waits on it, of a kind that reads (`TRUNK_KINDS`), that is no ledger, fanout or
    integrate step; a step that judges forks only one that decided nothing (`BLIND_TRUNKS`),
    and a refute, a recheck or an integrate step forks none."""
    by_id = {s["id"]: s for s in steps if isinstance(s.get("id"), str)}
    out = []
    for step in steps:
        sid, target = step.get("id"), step.get("trunk")
        if target is None or not isinstance(sid, str) or sid not in ids:
            continue
        where = f"step {sid!r}"
        if not isinstance(target, str):
            out.append(f"{where}: trunk is the id of a reading step before it whose session it forks")
            continue
        if target not in by_id or target not in ids[:ids.index(sid)]:
            out.append(f"{where}: trunk names {target!r}, which is no step before it")
            continue
        trunk = by_id[target]
        if trunk.get("kind") not in TRUNK_KINDS:
            out.append(f"{where}: trunk names {target!r}, a {trunk.get('kind') or 'kindless'} step: a trunk is a "
                       f"reading step of kind {' or '.join(sorted(TRUNK_KINDS))}")
        elif trunk.get("ledger") or trunk.get("fanout") or trunk.get("integrate"):
            out.append(f"{where}: trunk names {target!r}, which runs a ledger, parts or a join: a trunk only reads")
        elif str(step.get("kind") or "").startswith("review") and trunk.get("kind") not in BLIND_TRUNKS:
            out.append(f"{where} reviews, so it forks only a trunk that decided nothing (kind "
                       f"{' or '.join(sorted(BLIND_TRUNKS))}), never {target!r}, a {trunk.get('kind')}")
        if step.get("ledger") in NEVER_FORK or step.get("integrate"):
            out.append(f"{where}: a {step.get('ledger') or 'integrate'} step forks no trunk: it reads only what "
                       "it decides")
        if target not in _waits_on(by_id, sid):
            out.append(f"{where} forks {target!r} but does not wait on it: add it to its needs")
    return out


def _waits_on(by_id: Mapping[str, Mapping[str, Any]], sid: str) -> set[str]:
    seen: set[str] = set()
    todo = [d for d in by_id[sid].get("needs") or () if d in by_id]
    while todo:
        dep = todo.pop()
        if dep not in seen:
            seen.add(dep)
            todo += [d for d in by_id[dep].get("needs") or () if d in by_id]
    return seen


def is_trunk(defn: Mapping[str, Any], step_id: str) -> bool:
    """Whether a later step of the definition forks this one."""
    return any(s.get("trunk") == step_id for s in defn["steps"])


def source(store: Store, trunk_task: Mapping[str, Any] | None, step_id: str) -> dict[str, Any]:
    """What a step forks, read when it is queued: the trunk's task and step, the session of
    its last passing attempt, the model it ran on and the commit it read; or `missing`, why
    there is nothing to fork (the branch then starts fresh and says so)."""
    if trunk_task is None:
        return {"step": step_id, "missing": f"step {step_id!r} has no task"}
    out: dict[str, Any] = {"step": step_id, "task": int(trunk_task["id"])}
    if trunk_task.get("status") != "done":
        return {**out, "missing": f"step {step_id!r} (#{trunk_task['id']}) did not pass"}
    passing = [a for a in store.attempts(int(trunk_task["id"])) if a.get("passed")]
    if not passing or not passing[-1].get("session_id"):
        return {**out, "missing": f"step {step_id!r} (#{trunk_task['id']}) kept no session to fork"}
    last = passing[-1]
    started = [e for e in store.events_of(int(trunk_task["id"]), "attempt_started")
               if (e["data"] or {}).get("seq") == last["seq"]]
    return {**out, "session": last["session_id"], "model": last.get("served_model") or "",
            "cell": last.get("cell") or "", "base": ((started[-1]["data"] or {}).get("base") if started else None)}


def context_tokens(session_id: str, env: Mapping[str, str] | None = None) -> int | None:
    """How many tokens the session's context held at its last model call (its input, what
    the cache served and what it stored), from its transcript; None when it cannot be read."""
    from locks import usage

    path = usage.transcript_of(session_id, env)
    if path is None:
        return None
    last: int | None = None
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                used = (entry.get("message") or {}).get("usage") or {}
                if isinstance(used, dict) and used:
                    last = sum(int(used.get(k) or 0) for k in (
                        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    except OSError:
        return None
    return last


def trial(store: Store, repo: str | None) -> dict[str, dict[str, Any]]:
    """Each arm's tasks in a repository (None: in every one): how many, how many passed their
    first try, and the medians of the calls before its first change, its input tokens and its
    duration."""
    rows = store.arm_trial(repo, KEY)
    out: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        mine = [r for r in rows if r["arm"] == arm]
        out[arm] = {"tasks": len(mine), "passed_first": sum(bool(r["passed_first"]) for r in mine),
                    "calls_before_edit_p50": _p50(r["calls_before_edit"] for r in mine),
                    "input_tokens_p50": _p50(r["input_tokens"] for r in mine if r["input_tokens"]),
                    "duration_p50_s": _p50(round(r["duration_s"]) for r in mine if r["duration_s"])}
    return out


def _p50(values: Any) -> Any:
    got = sorted(v for v in values if v is not None)
    return got[(len(got) - 1) // 2] if got else None


def arm(store: Store, repo: str | None, task_id: int) -> tuple[str, str]:
    """Whether this step's first attempt forks its trunk, and why. During the trial the arms
    alternate (the one with fewer tasks first); after it, forking stays where it won."""
    if repo is None:
        return "fork", "no repository to run a trial in"
    found = trial(store, repo)
    f, c = found["fork"], found["fresh"]
    if (got := _verdict(found)) is not None:
        return got
    here = f"the trial here has {f['tasks']} forked, {c['tasks']} fresh, {TRIAL} each"
    if (everywhere := _verdict(trial(store, None), where="everywhere")) is not None:
        return everywhere[0], arms_mod.pooled_why(everywhere[1], here)
    pick = ARMS[arms_mod.side("trunk", repo, task_id, f["tasks"], c["tasks"])]
    return pick, f"the trunk's trial here ({f['tasks']} forked, {c['tasks']} fresh, {TRIAL} each)"


def _verdict(found: Mapping[str, Mapping[str, Any]], *, where: str = "here") -> tuple[str, str] | None:
    """A trial's verdict, or None while its arms are short of `TRIAL` and it is not lost early."""
    f, c = found["fork"], found["fresh"]
    early = min(f["tasks"], c["tasks"]) >= EARLY and \
        f["passed_first"] / f["tasks"] < c["passed_first"] / c["tasks"] - EARLY_MARGIN
    if min(f["tasks"], c["tasks"]) < TRIAL and not early:
        return None
    rate_f, rate_c = f["passed_first"] / f["tasks"], c["passed_first"] / c["tasks"]
    if early:
        return "fresh", (f"forking lost its trial {where} early: forked first tries passed {rate_f:.0%}, fresh "
                         f"{rate_c:.0%}, worse by more than {EARLY_MARGIN:.0%} over {EARLY} or more each")
    sooner = any(f[k] is not None and c[k] is not None and f[k] < c[k]
                 for k in ("calls_before_edit_p50", "duration_p50_s"))
    said = (f"forked first tries passed {rate_f:.0%}, fresh {rate_c:.0%}; calls before the first change p50 "
            f"{f['calls_before_edit_p50']} against {c['calls_before_edit_p50']}, time p50 "
            f"{f['duration_p50_s']}s against {c['duration_p50_s']}s")
    if sooner and rate_f >= rate_c - MARGIN:
        return "fork", f"forking won its trial {where}: {said}"
    return "fresh", f"forking lost its trial {where}: {said}"


def note(trunk: Mapping[str, Any], *, role: str, capabilities: str, workdir: Path, base: str | None) -> str:
    """The first words a branch reads after the fork point: where the session came from, that
    it is a new worker from here, its role (the system prompt is the trunk's record, so the
    role is said here), the tools it has now, and where its files are."""
    lines = [
        f"locks: everything above is the session of the workflow step {trunk['step']!r} (task #{trunk['task']}), "
        "which only read. From here this session is a new worker, with its own role and task below. What was "
        "read above is evidence about the code, never a decision made for you or an opinion to defend; what "
        "that step concluded binds you only where your task below quotes it.",
        f"Your directory is now {workdir}: the files read above were in another checkout of this repository. "
        "Read and change files only here, by paths under this directory.",
    ]
    if base and trunk.get("base") and base != trunk["base"]:
        lines.append(f"The code moved since it was read: `git diff {str(trunk['base'])[:12]} {base[:12]}` shows "
                     "what the steps between changed. Read a file again before you change it.")
    lines.append("Your tools are the ones this launch gives you: any tool or server named above that you do not "
                 "have now is gone, and one you have now is yours to use.")
    if role:
        lines.append("Your role from here, in place of anything above:\n" + role)
    if capabilities:
        lines.append(capabilities)
    return "\n\n".join(lines)


def enabled(env: Mapping[str, str] | None = None) -> bool:
    from locks import config

    return config.enabled("trunk", os.environ if env is None else env)
