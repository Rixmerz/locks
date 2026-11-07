"""What a running task shows of itself while it runs: how long it has run against how long its
kind usually takes here, where it is (its worker, locks' check, the independent check), what its
worker has written so far, and whether its worker has gone quiet.

A person who sent work to a worker sees it move, instead of trusting that it will end. All of it
is read: the store's events, the tool-call log's tail and its worktree's git; nothing is written,
no model is asked, and nothing is stopped. A view (the status line, the board, `locks tasks`) may
call it on every refresh.
"""
from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from locks import criteria, isolate, phase
from locks.config import home

#: A worker with no tool call for this long is said to be quiet: a long command, or stuck.
IDLE_S = 5 * 60
#: The passes of a kind (and of its size, when there are as many) an estimate needs.
ESTIMATE_SAMPLES = 3
#: The most new files whose lines are counted, and the largest one read.
NEW_FILES = 200
NEW_FILE_BYTES = 1_000_000
#: What each stage of a running attempt is called, as its `stage` event names it.
STAGES = {"worker": "worker", "check": "locks' check", "verifier": "independent check"}


def _ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def estimate(store: Any, task: Mapping[str, Any]) -> dict[str, Any] | None:
    """How long tasks like this one took to pass here: the median and the 90th percentile of the
    wall time of its kind's passes in its repository, of its size when enough of them were, else
    of its kind at any size. None with fewer than `ESTIMATE_SAMPLES` passes: no guess."""
    if not task.get("kind"):
        return None
    passed = [(t.get("points"), float(w)) for t in store.list_tasks(
                  repo=task.get("repo"), status=["done"], limit=200, source=("locks", "queue"))
              if t["kind"] == task["kind"] and t["id"] != task.get("id")
              and (w := (((store.final_event(t["id"]) or {}).get("data")) or {}).get("wall_s"))]
    same = sorted(w for p, w in passed if task.get("points") and p == task.get("points"))
    walls, by = (same, "its kind and size") if len(same) >= ESTIMATE_SAMPLES else \
        (sorted(w for _, w in passed), "its kind")
    if len(walls) < ESTIMATE_SAMPLES:
        return None
    return {"p50_s": round(_quantile(walls, 0.5)), "p90_s": round(_quantile(walls, 0.9)), "samples": len(walls),
            "by": by}


def _git(path: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _artifact(path: str) -> bool:
    return any(part in isolate.ARTIFACT_DIRS for part in Path(path).parts)


def _new_lines(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return fh.read(NEW_FILE_BYTES).count(b"\n")
    except OSError:
        return 0


def _changes_in(places: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The same count for a task that runs with no worktree: each checkout it may change, read
    against how it stood when the attempt started (`isolate.stand`). The lines of a file that was
    already modified there before the attempt include what the person had written. These are the
    person's own checkouts, read every few seconds while their worker (or they) run `git add` and
    `git commit` in them: `git diff` rewrites the index to refresh its stat data (and
    `--no-optional-locks` does not stop it), so the plumbing `diff-index` reads them, which never does."""
    files, added, removed = set(), 0, 0
    for place in places:
        top = Path(place["path"])
        moved = isolate.moved_in_place(place)
        numstat = _git(top, "diff-index", "--numstat", place["head"], "--")
        if moved is None or numstat is None:
            return None
        seen = set()
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[2] in moved:
                seen.add(parts[2])
                added += int(parts[0]) if parts[0].isdigit() else 0
                removed += int(parts[1]) if parts[1].isdigit() else 0
        for name in [m for m in moved if m not in seen][:NEW_FILES]:
            added += _new_lines(top / name)
        files.update(isolate.named(place, m) for m in moved if not _artifact(m))
    return {"files": len(files), "added": added, "removed": removed, "paths": sorted(files)}


def _linked(path: str, links: Sequence[str]) -> bool:
    return any(path == rel or path.startswith(rel + "/") for rel in links)


def changes(task_id: int, base: str | None, root: Path | None = None,
            places: Sequence[Mapping[str, Any]] | None = None,
            links: Sequence[str] = ()) -> dict[str, Any] | None:
    """What a task's worker has written in its worktree so far, against the commit it started on:
    the files and the lines added and removed, new files counted whole. A task with no worktree
    (a run in the checkout, or in a folder of several) is read in each checkout its attempt
    recorded in `places`, its files named from the folder. The checkout's dependencies linked into
    the worktree (`links`, as the attempt recorded them: a `.venv` link is untracked, since
    `.venv/` in .gitignore matches a directory and not a link) are never its writing. None when
    it has neither, or git cannot say."""
    path = worktree(task_id, root)
    if not path.is_dir():
        return _changes_in(places) if places else None
    numstat = _git(path, "diff", "--numstat", base or "HEAD", "--")
    untracked = _git(path, "ls-files", "--others", "--exclude-standard")
    if numstat is None or untracked is None:
        return None
    files, added, removed = set(), 0, 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and not _artifact(parts[2]) and not _linked(parts[2], links):
            files.add(parts[2])
            added += int(parts[0]) if parts[0].isdigit() else 0
            removed += int(parts[1]) if parts[1].isdigit() else 0
    new = [p for p in untracked.splitlines() if p and not _artifact(p) and not _linked(p, links)]
    for name in new[:NEW_FILES]:
        added += _new_lines(path / name)
    files.update(new)
    return {"files": len(files), "added": added, "removed": removed, "paths": sorted(files)}


def worktree(task_id: int, root: Path | None = None) -> Path:
    return (root or home()) / "worktrees" / f"task-{task_id}"


#: The most of a running worker's diff one look carries; the rest is said cut.
PEEK_BYTES = 200_000


def _peek_in(places: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """`peek` of a task with no worktree: what changed in each checkout since its attempt started
    (`isolate.stand`), one after the other, each file under its checkout's name."""
    text, shown = [], []
    for place in places:
        top = Path(place["path"])
        moved = isolate.moved_in_place(place)
        if moved is None:
            return None
        if not moved:
            continue
        prefix = f"{place['name']}/" if place.get("name") else ""
        flags = [f"--src-prefix=a/{prefix}", f"--dst-prefix=b/{prefix}"]
        tracked = _git(top, "diff-index", "--stat", "--patch", "--no-color", *flags, place["head"], "--", *moved)
        untracked = _git(top, "ls-files", "--others", "--exclude-standard")
        if tracked is None or untracked is None:
            return None
        text.append(f"# {place['name'] or top}\n{tracked}")
        new = set(untracked.splitlines())
        shown.append({"path": str(top), "head": place["head"]})
        for name in [m for m in moved if m in new and not _artifact(m)][:NEW_FILES]:
            try:
                done = subprocess.run(["git", "diff", "--no-index", "--no-color", *flags, "--", "/dev/null", name],
                                      cwd=top, capture_output=True, text=True, timeout=10, errors="replace")
            except (OSError, subprocess.SubprocessError):
                continue
            text.append(done.stdout)
    raw = "".join(text)
    return {"diff": raw[:PEEK_BYTES], "cut": len(raw) > PEEK_BYTES, "places": shown}


def peek(task_id: int, base: str | None, root: Path | None = None,
         places: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any] | None:
    """A running worker's diff so far: its worktree against the commit it started on, new files
    whole, a tool's artifacts left out; a task with no worktree, each checkout in `places`
    against how it stood when its attempt started. Read only (no index, no stash): None when it
    has neither, or git cannot say."""
    path = worktree(task_id, root)
    if not path.is_dir():
        return _peek_in(places) if places else None
    tracked = _git(path, "diff", "--stat", "--patch", "--no-color", base or "HEAD", "--")
    untracked = _git(path, "ls-files", "--others", "--exclude-standard")
    if tracked is None or untracked is None:
        return None
    text = [tracked]
    for name in [p for p in untracked.splitlines() if p and not _artifact(p)][:NEW_FILES]:
        try:
            done = subprocess.run(["git", "diff", "--no-index", "--no-color", "--", "/dev/null", name], cwd=path,
                                  capture_output=True, text=True, timeout=10, errors="replace")
        except (OSError, subprocess.SubprocessError):
            continue
        text.append(done.stdout)
    raw = "".join(text)
    return {"diff": raw[:PEEK_BYTES], "cut": len(raw) > PEEK_BYTES, "worktree": str(path)}


#: The most files a view of the texts carries; the rest is said by count.
TEXT_FILES = 20


def _text(raw: str | None) -> str | None:
    return None if raw is None or "\0" in raw or len(raw) > NEW_FILE_BYTES else raw


def texts(where: Path, base: str | None, head: str | None = None) -> dict[str, Any] | None:
    """Each file a change touched, before and after, for a view that draws its own diff (an editor
    over ACP): against `base`, the commit `head` when given, else the working tree as it is now
    (new files included). A tool's artifacts, binaries and files over `NEW_FILE_BYTES` are left
    out; past `TEXT_FILES` the rest is counted. Read only; None when git cannot say."""
    if not where.is_dir():
        return None
    base = base or "HEAD"
    listed = _git(where, "diff", "--name-only", base, *([head] if head else []), "--")
    if listed is None:
        return None
    names = listed.splitlines()
    if head is None:
        names += (_git(where, "ls-files", "--others", "--exclude-standard") or "").splitlines()
    names = sorted({n for n in names if n and not _artifact(n)})
    files = []
    for name in names[:TEXT_FILES]:
        old = _git(where, "show", f"{base}:{name}")
        if head:
            new = _git(where, "show", f"{head}:{name}")
        else:
            try:
                new = (where / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                new = None
        if (old is not None and _text(old) is None) or (new is not None and _text(new) is None):
            continue
        files.append({"path": name, "old": old, "new": new})
    return {"files": files, "more": max(0, len(names) - TEXT_FILES)}


def asked(task: Mapping[str, Any]) -> list[dict[str, str]]:
    """The request's own criteria, `S1..Sn`, verbatim: the subtasks a running task shows. Its
    worker keeps no list of its own: one that kept a task list spent ~5x the model calls and
    ~19% more time on a four-item task (3 runs each), so the request's items stand for it."""
    return [{"id": f"S{i}", "text": item} for i, item in enumerate(criteria.items(str(task.get("body") or "")), 1)]


def of(store: Any, task: Mapping[str, Any], *, now: float | None = None, root: Path | None = None,
       env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """A running task's progress: elapsed time, stage, its worker's phase and how long since its
    last tool call, the estimate, and what it changed so far. What could not be read is left out."""
    now = time.time() if now is None else now
    task_id = int(task["id"])
    out: dict[str, Any] = {}
    planned = store.last_event(task_id, "planned")
    if planned and (began := _ts(planned["ts"])) is not None:
        out["elapsed_s"] = round(now - began)
    started = store.last_event(task_id, "attempt_started")
    seq = (started["data"] or {}).get("seq") if started else None
    begun = (started or {}).get("data") or {}
    stage = store.last_event(task_id, "stage")
    out["stage"] = STAGES.get(str((stage["data"] or {}).get("stage")), "worker") \
        if stage and started and stage["id"] > started["id"] else "worker"
    if (doing := phase.of(task_id, seq, env)) is not None:
        out["phase"] = doing
    if out["stage"] == "worker" and started:
        calls = phase._calls(task_id, seq, env)
        last = _ts(calls[-1].get("ts")) if calls else _ts(started["ts"])
        if last is not None:
            out["quiet_s"] = round(now - last)
        if calls and (said := words(str(calls[-1].get("sig") or ""))):
            out["doing"] = said
    if (expected := estimate(store, task)) is not None:
        out["estimate"] = expected
    if items := asked(task):
        out["criteria"] = items
    if (written := changes(task_id, begun.get("base"), root, begun.get("places"),
                           tuple(begun.get("links") or ()))) is not None:
        out["changes"] = written
        # A worker that writes through its shell (`cat >`, `sed -i`) makes no write call.
        if written["files"] and out.get("phase") == "exploring":
            out["phase"] = "implementing"
    return out


def _minutes(seconds: float) -> str:
    return f"{max(1, round(seconds / 60))}m" if seconds >= 60 else f"{round(seconds)}s"


def words(sig: str) -> str:
    """A tool call's signature in words, as `running npm test` or `editing a .py file`. A signature
    never holds an argument, so it cannot name the file or the whole command: this says what it
    keeps. "" for a call it cannot read."""
    kind, _, rest = sig.partition(":")
    if kind == "bash":
        return f"running {rest.replace('-', ' ')}" if rest and rest != "unknown" else "running a command"
    if kind in ("read", "edit", "write"):
        what = f"a .{rest} file" if rest and rest != "noext" else "a file"
        return {"read": "reading", "edit": "editing", "write": "writing"}[kind] + f" {what}"
    if kind == "mcp":
        return f"calling {rest}" if rest else "calling a tool"
    if kind in ("grep", "glob"):
        return "searching the code"
    return f"using {kind}" if kind else ""


def warnings(p: Mapping[str, Any]) -> list[str]:
    """What a running task says is wrong with how long it takes: past 9 in 10 of its kind here, or no
    tool call for `IDLE_S`. The same words `line` ends with."""
    out = []
    if (expected := p.get("estimate")) and (elapsed := p.get("elapsed_s")) is not None \
            and elapsed > expected["p90_s"]:
        out.append(f"past 9 in 10 of its kind here ({_minutes(expected['p90_s'])})")
    if (quiet := p.get("quiet_s")) is not None and quiet >= IDLE_S:
        out.append(f"no tool call for {_minutes(quiet)} (a long command, or stuck)")
    return out


def line(p: Mapping[str, Any]) -> str:
    """One running task's progress in one line, as the status line, `locks tasks` and the mod's
    `tasks` tool say it: `implementing · 4m of ≈7m · 3 files +120 −8`."""
    parts = []
    doing = p.get("phase") if p.get("stage", "worker") == "worker" else p.get("stage")
    if doing:
        parts.append(str(doing))
    if (elapsed := p.get("elapsed_s")) is not None:
        expected = p.get("estimate")
        if not expected:
            parts.append(_minutes(elapsed))
        elif elapsed <= expected["p50_s"]:
            parts.append(f"{_minutes(elapsed)} of ≈{_minutes(expected['p50_s'])}")
        elif elapsed <= expected["p90_s"]:
            # The usual is a median: half the tasks like it go past it, so past it is said with
            # the range, never as a task late against a promise.
            parts.append(f"{_minutes(elapsed)}, past the usual ≈{_minutes(expected['p50_s'])} "
                         f"(9 in 10 end within {_minutes(expected['p90_s'])})")
        else:
            parts.append(f"{_minutes(elapsed)} of ≈{_minutes(expected['p50_s'])}")  # `warnings` says the rest
    if written := p.get("changes"):
        if written["files"]:
            parts.append(f"{written['files']} file{'s' if written['files'] != 1 else ''} "
                         f"+{written['added']} −{written['removed']}")
        else:
            parts.append("nothing written yet")
    return " · ".join([*parts, *warnings(p)])


#: The most file names one line says; the rest is said by count.
FILES_SHOWN = 5


def files_line(p: Mapping[str, Any]) -> str:
    """The files a running worker has written so far, by name: `wrote calc.py, test_calc.py`."""
    paths = list(((p.get("changes") or {}).get("paths")) or [])
    if not paths:
        return ""
    more = len(paths) - FILES_SHOWN
    return "wrote " + ", ".join(paths[:FILES_SHOWN]) + (f" and {more} more" if more > 0 else "")


def expected_line(store: Any, task: Mapping[str, Any]) -> str:
    """What sending a task says of how long it should take: its kind's usual time here. A task
    not classified yet is read by the rules alone (no model); one they cannot read gets none."""
    if not task.get("kind"):
        from locks.classify import by_rules

        try:
            asked = json.loads(task.get("options") or "{}").get("kind")
        except (ValueError, AttributeError):
            asked = None
        ruled = None if asked else by_rules(str(task.get("body") or task.get("title") or ""))
        task = {**task, "kind": asked or (ruled.kind if ruled else None)}
    found = estimate(store, task)
    if found is None:
        return ""
    return (f"tasks like it took ≈{_minutes(found['p50_s'])} here (9 in 10 within {_minutes(found['p90_s'])}; "
            f"{found['samples']} passes of {found['by']})")
