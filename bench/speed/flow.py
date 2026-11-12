"""Whole workflow runs measured as single tasks are: the same build, request and hidden tests as
`versus.py` (imported, not copied), judged on the last branch the run kept.

Arms:

- `single`: `locks run` with `--verify 'make test'`, exactly `versus.locks` (the checkout, as versus judges it);
- `feature`: `locks workflow run feature --whole` on the request, the run's check `make test`;
- `parallel-fork`, `parallel-fresh`: `locks workflow run feature-parallel`, with `LOCKS_TRUNK_ARM` set to `fork`
  or `fresh` for the whole process (`trunks.FORCE`), so every part's first try is in that arm.

Each run has its own locks home and a fresh copy of the build. The run is started, then its queue is driven to
its end in the foreground with `locks work` (no hook, no `autowork`); a run that does not end within `TIMEOUT_S`
is a row with its error. A row is appended to `results-flow.jsonl` (`row_of` builds the run's part from the
store): arm, variant, locks version, wall time, cost (the sum of the run's tasks), hidden tests, the run's
status, each step with its kind, cell, attempts, time and cost, the ledger rows the review opened, whether the
fix ran, and for the parallel arms the parts and each first try's trunk arm.

    python bench/speed/flow.py run single,feature,parallel-fork,parallel-fresh 3 [parallel] [--hard]
    python bench/speed/flow.py report
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

import build  # noqa: E402
import versus  # noqa: E402

from locks import __version__, trunks, workflows  # noqa: E402
from locks.store import Store  # noqa: E402
from locks.text import clip  # noqa: E402

OUT = HERE / "results-flow.jsonl"
ARMS = ("single", "feature", "parallel-fork", "parallel-fresh")
#: The workflow each workflow arm runs.
WORKFLOW = {"feature": "feature", "parallel-fork": "feature-parallel", "parallel-fresh": "feature-parallel"}
#: A run that does not end within this many seconds is a row with its error (`FLOW_TIMEOUT_S` overrides).
TIMEOUT_S = float(os.environ.get("FLOW_TIMEOUT_S", 3 * 3600))
#: Tasks one `locks work` may start before the loop looks at the run again.
WORK_MAX = "50"
#: How long to wait before asking again when another dispatcher holds the lane.
POLL_S = 5


def parse_arms(text: str) -> list[str]:
    """The comma separated arms of the command line, each one of `ARMS`; anything else is an error."""
    got = [a.strip() for a in text.split(",") if a.strip()]
    unknown = [a for a in got if a not in ARMS]
    if not got or unknown:
        raise ValueError(f"unknown arm {', '.join(unknown) or text!r}: arms are {', '.join(ARMS)}")
    return got


def _data(event: dict | None) -> dict:
    return (event or {}).get("data") or {}


def _options(task: dict) -> dict:
    try:
        return json.loads(task.get("options") or "{}")
    except ValueError:
        return {}


def ended(store: Store, run_id: int) -> str | None:
    """How the run ended: `done`, `cancelled`, or `stopped` (no step queued or running, one short of a pass);
    None while any step is queued or running."""
    run = store.get_run(run_id)
    if run is None:
        return "missing"
    if run["status"] in ("done", "cancelled"):
        return run["status"]
    # Each step's newest try, a step built in parts as its parts together, as the engine reads a run.
    latest = workflows._latest(store, run_id)
    if any(t["status"] in ("queued", "running") for t in latest.values()):
        return None
    return "stopped"


def row_of(store: Store, run_id: int) -> dict[str, Any]:
    """The part of a row a finished (or stopped) run's store holds: its status, its steps (kind, cell,
    attempts, wall time, cost), the cost of all its tasks, the ledger rows its review opened, whether its fix
    ran, and its parts with each first try's trunk arm."""
    tasks = store.run_tasks(run_id)
    steps = []
    for t in tasks:
        done = store.final_event(t["id"]) if t["status"] != "queued" else None
        part = _options(t).get("part")
        stop = _data(done).get("stop") or {}
        moves = [_data(e).get("move") for e in store.events_of(t["id"], "moved")]
        steps.append({"step": t["workflow_step"], "task": t["id"], "kind": t.get("kind"),
                      "cell": t.get("final_cell") or t.get("start_cell"), "status": t["status"],
                      "attempts": len(store.attempts(t["id"])),
                      "wall_s": round(float(_data(done).get("wall_s") or 0), 1),
                      "cost": round(float(t.get("cost_usd") or 0), 4),
                      **({"part": part["id"]} if isinstance(part, dict) and part.get("id") else {}),
                      **({"skipped": True} if _options(t).get("skipped") else {}),
                      **({"stop_cause": stop.get("cause"), "stop_reason": clip(str(stop.get("reason") or ""), 300)}
                         if stop else {}),
                      "moves": len(moves), "move_names": moves,
                      # Why each attempt of a plan step failed: the failure kind, in order.
                      **({"failures": [_data(e).get("failure") for e in store.events_of(t["id"], "attempt_finished")]}
                         if t.get("kind") == "plan" else {})})
    rows = sum(len(_data(store.final_event(t["id"])).get("findings") or ())
               for t in tasks if t["workflow_step"] == "review")
    fixes = [s for s in steps if s["step"] == "fix"]
    fix = "absent" if not fixes else "skipped" if fixes[-1].get("skipped") else \
        "ran" if fixes[-1]["status"] != "queued" else "queued"
    builds = [t for t in tasks if t["workflow_step"] == "build" and _options(t).get("part")]
    out: dict[str, Any] = {
        "status": ended(store, run_id) or "active", "cost": round(sum(float(t.get("cost_usd") or 0) for t in tasks), 4),
        "steps": steps, "ledger_rows": rows, "fix": fix}
    if builds:
        firsts = []
        for t in builds:
            first = next(iter(store.events_of(t["id"], "briefed")), None)
            firsts.append(_data(first).get(trunks.KEY))
        out.update(parts=len({_options(t)["part"]["id"] for t in builds}), trunk_arms=firsts)
    return out


def branch_of(store: Store, run_id: int) -> str | None:
    """The last branch a step of the run kept."""
    for t in reversed(store.run_tasks(run_id)):
        if t["status"] == "done" and (branch := _data(store.final_event(t["id"])).get("branch")):
            return str(branch)
    return None


def drive(env: dict[str, str], where: Path, home: Path, run_id: int, deadline: float) -> tuple[str | None, str | None]:
    """Run the queue in the foreground until the run ends. Returns (error, status): an error text when the
    deadline passes first, or a status (`lane_paused`) when nothing is running and the lane is paused, which
    only a person clears. Sleeps between polls so a lane that starts nothing is never spun on."""
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return f"timed out after {TIMEOUT_S:.0f}s", None
        try:
            subprocess.run([str(ROOT / "bin" / "locks"), "work", "--repo", str(where), "--max", WORK_MAX],
                           cwd=where, env=env, capture_output=True, text=True, timeout=left)
        except subprocess.TimeoutExpired:
            return f"timed out after {TIMEOUT_S:.0f}s", None
        store = Store(home / "locks.db")
        try:
            state = ended(store, run_id)
            running = any(t["status"] == "running" for t in store.run_tasks(run_id))
            paused = any(lane["paused"] for lane in store.lanes())
        finally:
            store.close()
        if state is not None:
            return None, None
        if paused and not running:  # a part stopped and paused its lane: nothing startable is left
            return None, "lane_paused"
        time.sleep(min(POLL_S, max(deadline - time.monotonic(), 0)))


KEEP = "LOCKS_BENCH_KEEP"  # a folder: each workflow run's store is copied there before its home goes


def workflow(where: Path, request: str, arm: str) -> dict[str, Any]:
    """A `locks workflow run` of the arm's workflow in the copy, driven to its end; the branch it kept is checked
    out so the hidden tests judge it. `error` instead of a hang when the run does not end in time."""
    home = Path(tempfile.mkdtemp(prefix="locks-home-"))
    env = {**os.environ, "LOCKS_HOME": str(home), "LOCKS_AUTOWORK": "off", "LOCKS_NAMES": "off"}
    for name in ("LOCKS_SESSION_ID", "CLAUDE_CODE_SESSION_ID", trunks.FORCE):
        env.pop(name, None)
    if arm.startswith("parallel-"):
        env[trunks.FORCE] = arm.removeprefix("parallel-")
    began = time.monotonic()
    deadline = began + TIMEOUT_S
    started = subprocess.run([str(ROOT / "bin" / "locks"), "workflow", "run", WORKFLOW[arm], request, "--verify",
                              "make test", "--json", *(["--whole"] if arm == "feature" else [])],
                             cwd=where, env=env, capture_output=True, text=True, timeout=600)
    try:
        run_id = int(json.loads(started.stdout.splitlines()[-1])["id"])
    except (ValueError, KeyError, IndexError):
        shutil.rmtree(home, ignore_errors=True)
        return {"error": (started.stderr or started.stdout)[-300:]}
    error, status = drive(env, where, home, run_id, deadline)
    wall = time.monotonic() - began
    if error:
        subprocess.run([str(ROOT / "bin" / "locks"), "workflow", "cancel", str(run_id)], cwd=where, env=env,
                       capture_output=True, text=True, timeout=120)
    store = Store(home / "locks.db")
    try:
        got = row_of(store, run_id)
        if status:
            got["status"] = status
        branch = branch_of(store, run_id)
    finally:
        store.close()
    if os.environ.get(KEEP):  # the run's store, kept to read why a step stopped
        kept = Path(os.environ[KEEP])
        kept.mkdir(parents=True, exist_ok=True)
        # A backup, never a copy of the file: what the store still holds in its write-ahead log comes too.
        source, target = sqlite3.connect(home / "locks.db"), sqlite3.connect(kept / f"{arm}-{int(time.time())}.db")
        with target:
            source.backup(target)
        source.close()
        target.close()
    shutil.rmtree(home, ignore_errors=True)
    if branch and subprocess.run(["git", "rev-parse", "--verify", "-q", branch], cwd=where,
                                 capture_output=True).returncode == 0:
        subprocess.run(["git", "checkout", "-q", branch], cwd=where, check=True)
    return {**got, "wall": wall, **({"error": error} if error else {})}


def one(arm: str, variant: str) -> dict[str, Any]:
    where = build.repository(variant)
    request = versus.ask(variant)
    began = time.monotonic()
    try:
        got = versus.locks(where, request) if arm == "single" else workflow(where, request, arm)
    except subprocess.TimeoutExpired:
        got = {"error": "timed out"}
    wall = got.pop("wall", None)
    if wall is None:
        wall = time.monotonic() - began - float(got.get("late_s") or 0)
    row = {"arm": arm, "variant": variant, "version": __version__, "wall_s": round(wall, 1),
           "hidden": build.hidden(where, variant), "of": 10 if variant == "hard" else 7, **got}
    shutil.rmtree(where.parent, ignore_errors=True)
    return row


def report(rows: list[dict]) -> None:
    print(f"{'build':<7}{'arm':<16}{'n':>3}{'right':>8}{'hidden':>9}{'wall p50':>10}{'$ p50':>8}{'$ mean':>8}"
          f"{'steps':>7}{'rows':>6}{'errors':>8}")
    for key in sorted({(r["variant"], r["arm"]) for r in rows}):
        rs = [r for r in rows if (r["variant"], r["arm"]) == key]
        costs = [r.get("cost", 0) for r in rs]
        print(f"{key[0]:<7}{key[1]:<16}{len(rs):>3}{sum(r['hidden'] == r['of'] for r in rs):>5}/{len(rs):<2}"
              f"{sum(r['hidden'] for r in rs):>5}/{sum(r['of'] for r in rs):<3}"
              f"{median(r['wall_s'] for r in rs):>10.0f}{median(costs):>8.2f}{sum(costs) / len(costs):>8.2f}"
              f"{sum(len(r.get('steps') or ()) for r in rs) / len(rs):>7.1f}"
              f"{sum(r.get('ledger_rows') or 0 for r in rs) / len(rs):>6.1f}{sum(bool(r.get('error')) for r in rs):>8}")


def main(argv: list[str]) -> None:
    variant = "hard" if "--hard" in argv else "plain"
    argv = [a for a in argv if a != "--hard"]
    if argv and argv[0] == "report":
        report([json.loads(x) for x in OUT.read_text().splitlines() if x.strip()])
        return
    if len(argv) < 2 or argv[0] != "run":
        sys.exit(__doc__)
    arms, reps = parse_arms(argv[1]), int(argv[2]) if len(argv) > 2 else 3
    jobs = [a for _ in range(reps) for a in arms]

    def run(arm: str) -> None:
        row = one(arm, variant)
        with OUT.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    with ThreadPoolExecutor(int(argv[3]) if len(argv) > 3 else 1) as pool:
        list(pool.map(run, jobs))


if __name__ == "__main__":
    main(sys.argv[1:])
