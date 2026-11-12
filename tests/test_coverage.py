"""`locks coverage`: which features the store's records show ever ran."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from locks import coverage, forensics, matrix
from locks.store import Store, home

ROOT = Path(__file__).resolve().parents[1]


def _row(info: dict, name: str) -> dict:
    return next(r for r in info["features"] if r["name"] == name)


def _report(store: Store, **kw) -> dict:
    store.close()
    conn = forensics.open_copy(forensics.snapshot(home() / "locks.db"))
    try:
        return coverage.report(conn, **kw)
    finally:
        conn.close()


def _task(store: Store, repo: str = "r", **fields) -> int:
    return store.create_task("x", status="done", source="queue", repo=repo, **fields)["id"]


def test_an_event_makes_its_feature_used_with_count_tasks_and_last_date() -> None:
    store = Store(home() / "locks.db")
    a, b = _task(store), _task(store)
    store.add_event(a, "check_flaky", tests=["t"])
    store.add_event(a, "check_flaky", tests=["t"])
    store.add_event(b, "check_flaky", tests=["u"])
    info = _report(store)
    flaky = _row(info, "check-flaky")
    assert flaky["status"] == "used" and flaky["runs"] == 3 and flaky["tasks"] == 2
    assert re.match(r"\d{4}-\d{2}-\d{2}T", flaky["last"])
    never = _row(info, "check-repeated")
    assert never["status"] == "never" and never["runs"] == 0 and never["last"] is None
    assert info["summary"]["used"] == 1 and info["summary"]["total"] == len(coverage.FEATURES)
    assert info["summary"]["areas"]["checks"]["used"] == 1


def test_a_briefed_arm_counts_its_trial_and_a_planned_field_its_feature() -> None:
    store = Store(home() / "locks.db")
    t = _task(store)
    store.add_event(t, "briefed", primer_arm="with", trunk_arm=None)
    store.add_event(t, "planned", predicted={"to": "x"}, probation=None)
    info = _report(store)
    assert _row(info, "trial-primer")["runs"] == 1
    assert _row(info, "trial-trunk")["status"] == "never"
    assert _row(info, "predicted-start")["status"] == "used"
    assert _row(info, "probation-start")["status"] == "never"


def test_a_race_is_read_from_the_attempts_moves() -> None:
    store = Store(home() / "locks.db")
    t = _task(store)
    store._conn.execute("INSERT INTO attempts (task_id, seq, cell, max_turns, passed, move, created_at) "
                        "VALUES (?, 1, 'c', 5, 0, 'raced_out', datetime('now'))", (t,))
    store._conn.commit()
    assert _row(_report(store), "race")["tasks"] == 1


def test_a_workflow_run_counts_its_template_and_kinds_list_at_zero() -> None:
    store = Store(home() / "locks.db")
    run = store.create_run(name="bugfix", scope="bundled", source="x", definition="{}", repo="r")
    _task(store, kind="refactor", workflow_run=run["id"])
    info = _report(store)
    assert _row(info, "workflow:bugfix")["runs"] == 1 and _row(info, "workflow:bugfix")["tasks"] == 1
    assert _row(info, "workflow:panel")["runs"] == 0
    assert _row(info, "kind:refactor")["runs"] == 1
    kinds = [r for r in info["features"] if r["name"].startswith("kind:")]
    assert len(kinds) == len(matrix.KINDS) and any(r["runs"] == 0 for r in kinds)
    assert _row(info, "kind:docs")["status"] == "never"


def test_tables_and_task_options_are_read() -> None:
    store = Store(home() / "locks.db")
    _task(store, options=json.dumps({"part": {"id": "a"}}))
    store._conn.execute("INSERT INTO calibrations (at, repo, kind, band, field, rule, version) "
                        "VALUES (datetime('now'), 'r', 'bugfix', 1, 'start', 'rule', '1')")
    store._conn.commit()
    info = _report(store)
    assert _row(info, "parts")["status"] == "used" and _row(info, "loop-round")["status"] == "never"
    assert _row(info, "calibration")["runs"] == 1 and _row(info, "evolution-snapshot")["runs"] == 0


def test_repo_and_days_narrow_the_records() -> None:
    store = Store(home() / "locks.db")
    store.add_event(_task(store, "mine"), "check_flaky")
    old = _task(store, "other")
    store.add_event(old, "check_flaky")
    store._conn.execute("UPDATE events SET ts = '2000-01-01T00:00:00+00:00' WHERE task_id = ?", (old,))
    store._conn.commit()
    assert _row(_report(Store(home() / "locks.db"), repo="other"), "check-flaky")["runs"] == 1
    assert _row(_report(Store(home() / "locks.db"), repo="mine"), "check-flaky")["runs"] == 1
    assert _row(_report(Store(home() / "locks.db"), days=7), "check-flaky")["runs"] == 1
    assert _row(_report(Store(home() / "locks.db")), "check-flaky")["runs"] == 2


def test_the_table_is_big_enough_and_names_are_unique() -> None:
    names = [f.name for f in coverage.FEATURES]
    assert len(names) == len(set(names)) and len(names) >= 30
    assert {f.area for f in coverage.FEATURES} <= set(coverage.AREAS)


def test_every_event_kind_the_table_names_is_written_in_the_source() -> None:
    source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src" / "locks").rglob("*.py")
                       if p.name != "coverage.py")
    missing = [k for k in coverage.event_kinds() if f'"{k}"' not in source and f"'{k}'" not in source]
    assert not missing


def test_cli_prints_groups_never_and_json_round_trips() -> None:
    store = Store(home() / "locks.db")
    store.add_event(_task(store), "check_flaky")
    store.close()
    base = [sys.executable, "-m", "locks", "coverage"]

    def run(*args: str) -> str:
        return subprocess.run([*base, *args], cwd=ROOT, text=True, capture_output=True, check=True).stdout

    text = run()
    assert "1 of" in text and "checks (1/" in text and "check-flaky" in text and "never:" in text
    only = run("--never")
    assert "check-flaky" not in only and "never:" in only
    info = json.loads(run("--json"))
    assert info["summary"]["used"] == 1 and _row(info, "check-flaky")["status"] == "used"
    assert json.loads(json.dumps(info)) == info
