"""bench/speed/flow.py: the arms it parses and the row it builds from a finished run's store. No model runs."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from locks.store import Store

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("flow_bench", ROOT / "bench" / "speed" / "flow.py")
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)


def step(store: Store, run: int, name: str, *, status="done", kind="implement", cost=0.5, wall=10.0, tries=1,
         part: str | None = None, findings=(), branch: str | None = None, skipped=False, trunk_arm=None) -> int:
    options = {**({"part": {"id": part}} if part else {}), **({"skipped": "no rows"} if skipped else {})}
    task = store.create_task(name, status=status, source="queue", repo="r", kind=kind, cost_usd=cost,
                             start_cell="sonnet/high", final_cell="opus/high", workflow_run=run,
                             workflow_step=name, options=json.dumps(options))
    for _ in range(tries):
        store.add_attempt(task["id"], cell="sonnet/high", max_turns=30, passed=int(status == "done"))
    if trunk_arm:
        store.add_event(task["id"], "briefed", trunk_arm=trunk_arm)
    if status != "queued":
        store.add_event(task["id"], "finished", status=status, wall_s=wall, findings=list(findings),
                        **({"branch": branch} if branch else {}))
    return task["id"]


def make_run(store: Store, status="active") -> int:
    return store.create_run(name="feature", scope="bundled", source="locks", definition="{}", status=status)["id"]


def test_arms_are_parsed_and_unknown_ones_refused():
    assert flow.parse_arms("single, feature,parallel-fork,parallel-fresh") == list(flow.ARMS)
    for bad in ("", "single,parallel", "fork"):
        with pytest.raises(ValueError, match="arms are"):
            flow.parse_arms(bad)


def test_a_plain_workflow_row_has_its_steps_rows_and_fix(store: Store):
    run = make_run(store, "done")
    step(store, run, "build", kind="feature", cost=1.0, wall=30.0)
    step(store, run, "review", kind="review-routine", cost=0.25, findings=[{"what": "a"}, {"what": "b"}])
    step(store, run, "fix", cost=0.5, branch="locks/task-9", tries=2)
    got = flow.row_of(store, run)
    assert got["status"] == "done" and got["cost"] == 1.75
    assert got["ledger_rows"] == 2 and got["fix"] == "ran"
    assert [(s["step"], s["kind"], s["cell"], s["attempts"], s["wall_s"], s["cost"]) for s in got["steps"]] == [
        ("build", "feature", "opus/high", 1, 30.0, 1.0), ("review", "review-routine", "opus/high", 1, 10.0, 0.25),
        ("fix", "implement", "opus/high", 2, 10.0, 0.5)]
    assert "parts" not in got
    assert flow.branch_of(store, run) == "locks/task-9"


def test_a_skipped_fix_is_said_skipped(store: Store):
    run = make_run(store, "done")
    step(store, run, "review", kind="review-routine")
    step(store, run, "fix", cost=0, skipped=True)
    assert flow.row_of(store, run)["fix"] == "skipped" and flow.row_of(store, run)["ledger_rows"] == 0


def test_a_parallel_row_counts_parts_and_each_first_trys_trunk_arm(store: Store):
    run = make_run(store, "done")
    step(store, run, "plan", kind="plan")
    for part, arm in (("a", "fork"), ("b", "fork")):
        step(store, run, "build", kind="feature", part=part, trunk_arm=arm)
    got = flow.row_of(store, run)
    assert got["parts"] == 2 and got["trunk_arms"] == ["fork", "fork"]


def test_a_run_is_ended_only_when_nothing_is_queued_or_running(store: Store):
    run = make_run(store)
    step(store, run, "build", status="queued")
    assert flow.ended(store, run) is None and flow.row_of(store, run)["status"] == "active"
    step(store, run, "review", status="running")
    assert flow.ended(store, run) is None
    other = make_run(store)
    step(store, other, "build", status="failed")
    assert flow.ended(store, other) == "stopped"
    assert flow.ended(store, make_run(store, "cancelled")) == "cancelled"


def test_a_run_that_does_not_end_in_time_is_an_error_not_a_hang(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(flow, "TIMEOUT_S", 0)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no process runs past the deadline"))
    assert "timed out" in flow.drive({}, tmp_path, tmp_path, 1, flow.time.monotonic())


def test_a_row_carries_arm_variant_version_and_the_hidden_score(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(flow.build, "repository", lambda variant: tmp_path / "x" / "repo")
    monkeypatch.setattr(flow.build, "hidden", lambda where, variant: 6)
    monkeypatch.setattr(flow, "workflow", lambda where, request, arm: {"cost": 1.5, "status": "done", "wall": 12.34})
    row = flow.one("feature", "hard")
    assert row["arm"] == "feature" and row["variant"] == "hard" and row["version"] == flow.__version__
    assert (row["hidden"], row["of"], row["wall_s"], row["cost"], row["status"]) == (6, 10, 12.3, 1.5, "done")
