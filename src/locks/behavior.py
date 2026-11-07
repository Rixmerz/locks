"""How a worker worked: one attempt's profile, read from its own tool calls, and the analysis's
table of those profiles by kind, outcome and cell.

The calls are the PostToolUse log's (`tool_events`, read into the store first): a tool, a
signature and whether it went through, never an argument. An argument's hash is only compared
with the attempt's other calls (a file read again, the same call repeated) and never kept here.
The independent check's calls are not the attempt's (`orchestrate._verifier_env`): `check_calls`
finds them by its session or, without one, by the task's calls that belong to no attempt in the
window it ran in. Observed, never a rule's input and never a trial; no model.
"""
from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from locks import phase
from locks.mcp_guard import LIVESPEC_READ
from locks.store import Store

#: A tool's family, as the profile counts it; an MCP server's tools are its own family.
FAMILIES = {"read": "read", "grep": "search", "glob": "search", "ls": "search", "edit": "edit",
            "write": "edit", "multiedit": "edit", "notebookedit": "edit", "bash": "shell",
            "webfetch": "web", "websearch": "web", "todowrite": "todo"}
#: The fields `profile` gives, in the order an export lists them.
FIELDS = ("calls", "failed", "explore", "implement", "calls_before_edit", "s_before_edit",
          "check_runs", "fix_cycles", "unchecked_end", "rereads", "read_calls", "repeat_max", "gap_max_s",
          "active_s")
#: The fields the analysis's table gives as medians (the rest are shares).
MEDIANS = ("calls", "calls_before_edit", "s_before_edit", "check_runs", "fix_cycles", "rereads",
           "gap_max_s", "active_s")

#: The kinds of a blind review: a pass of one with no finding is read as a clean review.
REVIEW_KINDS = frozenset({"review-routine", "review-critical"})
#: The `WorkerResult.shape` of a review that reported no finding and read nothing.
UNREAD_SHAPE = "unread-review"
#: What a review held back for it is told, in its own session; `{base}` is the commit its diff is against.
UNREAD_PROMPT = ("Your review reported no finding but your calls show you read nothing: read the diff "
                 "(`git diff {base}`) and the files it changed, then answer again under the same result "
                 "contract.")

#: The shell programs whose call reads code, as `signature` names them (`git diff` is `git-diff`).
_SHELL_READS = frozenset({"git-diff", "git-show", "git-log", "git-blame", "git-grep", "cat", "head", "tail",
                          "sed", "nl", "less", "rg", "grep", "git-diff-tree", "git-cat-file", "git-ls-files",
                          "git-show-ref", "diff", "awk", "find", "jq", "wc", "python", "python3", "perl",
                          "ruby", "node"})
#: A chained command (`cd x && git diff`) is signed by its first program, and one no program can be
#: read from is `unknown`: either may have read, and unknown is not none.
_SHELL_HIDDEN = frozenset({
    "bash:cd", "bash:unknown",
    # Programs that run another command (`bash -c 'git diff'`, `timeout 30 git diff`, `gh pr diff`): the
    # signature stops at the first word, so what they ran is not known.
    "bash:bash", "bash:sh", "bash:zsh", "bash:timeout", "bash:xargs", "bash:nohup", "bash:stdbuf",
    "bash:ionice", "bash:gh-pr", "bash:gh-api"})
#: The git verbs that read no code. A `git` call whose verb the signature lost (`git -C dir diff` is
#: `git`, `git -C repo diff` is `git-repo`) or does not know may have been a read; these are not.
_GIT_NOT_READS = frozenset({
    "add", "am", "apply", "branch", "checkout", "cherry-pick", "clean", "clone", "commit", "config", "fetch",
    "init", "merge", "mv", "pull", "push", "rebase", "remote", "reset", "restore", "revert", "rm", "stash",
    "status", "submodule", "switch", "tag", "worktree", "rev-parse", "gc", "prune", "maintenance"})
_LIVESPEC_PREFIX = "mcp:livespec."


def _reads(sig: str) -> bool:
    if sig.startswith("read:") or sig in ("grep", "glob") or sig in _SHELL_HIDDEN:
        return True
    if sig.startswith("bash:"):
        program = sig[len("bash:"):]
        if program == "git" or (program.startswith("git-") and program[4:] not in _GIT_NOT_READS):
            return True
        return program in _SHELL_READS
    return sig.startswith(_LIVESPEC_PREFIX) and sig[len(_LIVESPEC_PREFIX):] in LIVESPEC_READ


def read_calls(calls: Sequence[Mapping[str, Any]]) -> int:
    """How many of an attempt's calls read code, by signature: a file read, `grep`, `glob`, a shell
    read (`git diff`, `cat`, `sed`…), a livespec tool that reads the index, and the shell calls whose
    program a signature cannot name. A call that failed is counted: a worker that tried to read is not
    one that never did, and a count that is too high holds back fewer reviews, never more."""
    return sum(_reads(str(c.get("sig") or "")) for c in calls)


def unread_record(who: str, *, again: bool, calls: int) -> dict[str, Any]:
    """The data of a `review_unread` event: `who` ("worker" or "verifier") reported no finding and
    read nothing, `again` when it was the second such answer, with how many calls it made."""
    return {"who": who, "again": again, "calls": calls}


def family(tool: str) -> str:
    tool = (tool or "").lower()
    if tool.startswith("mcp__"):
        return "mcp:" + tool[5:].split("__", 1)[0]
    return FAMILIES.get(tool, "other")


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def profile(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One attempt's calls, oldest first, as its profile; {} for an attempt that made none.

    `calls` and `failed`: every call and the ones that did not go through. `explore` and
    `implement`: the calls of those phases (`phase.kind_of`). `calls_before_edit` and `s_before_edit`:
    how far it read before its first write (absent when it wrote nothing). `check_runs`: the checks
    it ran itself (the calls of the checking phase); `fix_cycles`: checks run again after a write
    that followed a check (a fix loop);
    `unchecked_end`: it wrote after its last check, or wrote and never checked. `rereads`: reads of
    a file it had read already; `read_calls`: the calls that read code at all (`read_calls`);
    `repeat_max`: the longest run of the same call in a row. `gap_max_s`:
    the longest wait between two calls; `active_s`: from its first call to its last. `tools`: the
    calls by family."""
    if not calls:
        return {}
    kinds = [phase.kind_of(str(c.get("sig") or "")) for c in calls]
    stamps = [_ts(c.get("ts")) for c in calls]
    out: dict[str, Any] = {
        "calls": len(calls), "failed": sum(1 for c in calls if not int(c.get("ok", 1) or 0)),
        "explore": kinds.count("exploring"), "implement": kinds.count("implementing"),
        "check_runs": kinds.count("checking"),
    }
    first_edit = next((i for i, k in enumerate(kinds) if k == "implementing"), None)
    if first_edit is not None:
        out["calls_before_edit"] = first_edit
        if stamps[0] is not None and stamps[first_edit] is not None:
            out["s_before_edit"] = round((stamps[first_edit] - stamps[0]).total_seconds(), 1)
    cycles, wrote_since_check, checked = 0, False, False
    for k in kinds:
        if k == "checking":
            cycles += int(checked and wrote_since_check)
            checked, wrote_since_check = True, False
        elif k == "implementing":
            wrote_since_check = True
    out["fix_cycles"] = cycles
    out["unchecked_end"] = bool(wrote_since_check)
    seen: set[str] = set()
    rereads = 0
    for c in calls:
        if family(str(c.get("tool") or "")) == "read" and (key := str(c.get("arg_hash") or "")):
            rereads += int(key in seen)
            seen.add(key)
    out["rereads"] = rereads
    out["read_calls"] = read_calls(calls)
    longest = streak = 1
    for before, now in itertools.pairwise(calls):
        same = (before.get("sig"), before.get("arg_hash")) == (now.get("sig"), now.get("arg_hash"))
        streak = streak + 1 if same else 1
        longest = max(longest, streak)
    out["repeat_max"] = longest
    times = [t for t in stamps if t is not None]
    if len(times) >= 2:
        out["gap_max_s"] = round(max((b - a).total_seconds() for a, b in itertools.pairwise(times)), 1)
        out["active_s"] = round((times[-1] - times[0]).total_seconds(), 1)
    out["tools"] = dict(Counter(family(str(c.get("tool") or "")) for c in calls).most_common())
    return out


def of_attempt(store: Store, task_id: int, nth: int) -> dict[str, Any]:
    """The profile of an attempt's worker, the hooks' log read into the store first. Never fails a
    run: what cannot be read is {}."""
    from locks import habits

    try:
        habits.load_events(store)
        return profile(store.attempt_calls(task_id, nth))
    except Exception:  # a measure never costs the run it measures
        return {}


def check_calls(store: Store, task_id: int, session_id: str | None, since: str | None,
                until: str | None) -> list[dict[str, Any]] | None:
    """The independent check's tool calls, the hooks' log read into the store first. Its calls are
    logged under no attempt (`orchestrate._verifier_env`) but keep their task and their session: found
    by the session when one is given and logged calls, else the task's calls of no attempt between
    `since` and `until` (`Store.unattributed_calls`). None when none is found or the log cannot be
    read: unknown, never none, since a check whose calls were not logged read nothing no less than one
    that read."""
    from locks import habits

    try:
        habits.load_events(store)
        if session_id and (found := store.session_calls(session_id)):
            return found
        if since and until:
            return store.unattributed_calls(task_id, since, until) or None
    except Exception:  # a measure never costs the run it measures
        return None
    return None


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return float(ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2)


def _share(part: float, whole: float) -> float | None:
    return round(part / whole, 3) if whole else None


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Profiles added up: their count, the median of each count, and the shares of the attempts
    that failed a call, ended with an unchecked write, never checked and repeated a call three
    times or more; the calls by family as shares of every call."""
    out: dict[str, Any] = {"attempts": len(rows)}
    for field in MEDIANS:
        out[f"{field}_p50"] = _median([float(r[field]) for r in rows if r.get(field) is not None])
    out["failed_share"] = _share(sum(int(r.get("failed") or 0) for r in rows),
                                 sum(int(r.get("calls") or 0) for r in rows))
    out["unchecked_end_share"] = _share(sum(bool(r.get("unchecked_end")) for r in rows), len(rows))
    out["never_checked_share"] = _share(sum(not r.get("check_runs") for r in rows), len(rows))
    out["looping_share"] = _share(sum(int(r.get("repeat_max") or 0) >= 3 for r in rows), len(rows))
    tools: Counter[str] = Counter()
    for r in rows:
        tools.update({str(k): int(v) for k, v in (r.get("tools") or {}).items()})
    total = sum(tools.values())
    out["tools"] = {k: _share(v, total) for k, v in tools.most_common()}
    return out


def table(events: Sequence[Mapping[str, Any]], kinds: Mapping[int, str | None]) -> dict[str, Any]:
    """Every attempt with a profile (`attempt_finished.behavior`) of the tasks in `kinds`, added up
    by kind and outcome (`passed` or `failed`) and by cell. Observed: what passing workers did
    differently, never a cause."""
    by_kind: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    by_cell: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for e in events:
        task = int(e["task_id"])
        data = e.get("data") or {}
        if e["kind"] != "attempt_finished" or task not in kinds or not data.get("behavior"):
            continue
        outcome = "passed" if data.get("passed") else "failed"
        by_kind[(str(kinds[task] or "—"), outcome)].append(data["behavior"])
        by_cell[str(data.get("cell") or "—")].append(data["behavior"])
    return {
        "by_kind": [{"kind": kind, "outcome": outcome, **summarize(rows)}
                    for (kind, outcome), rows in sorted(by_kind.items())],
        "by_cell": sorted(({"cell": cell, **summarize(rows)} for cell, rows in by_cell.items()),
                          key=lambda row: -row["attempts"]),
    }
