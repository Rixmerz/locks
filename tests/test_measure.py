"""Forensics, the brief footprint, skill use, the notes-fork trial and the A/B trap harness."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from locks import footprint, forensics, notes_fork, skill_use
from locks.store import Store, home

ROOT = Path(__file__).resolve().parents[1]


def _store() -> Store:
    return Store(home() / "locks.db")


def _task(store: Store, kind: str = "implement") -> int:
    task = store.create_task("do it", status="done", source="cli", kind=kind)
    return int(task["id"])


def test_footprint_names_a_release_that_grew_the_brief() -> None:
    store = _store()
    for version, chars in (("0.70.0", 1000), ("0.71.0", 1500)):
        tid = _task(store)
        store.add_event(tid, "briefed", kind="implement", context_chars=chars, system_chars=0, version=version)
    conn = sqlite3.connect(home() / "locks.db")
    got = footprint.report(conn)["implement"]
    assert [r["version"] for r in got["releases"]] == ["0.70.0", "0.71.0"]
    assert got["grew"] == [{"from": "0.70.0", "to": "0.71.0", "ratio": 1.5}]


def test_skill_states_and_pins() -> None:
    store = _store()
    tid = _task(store)
    store.add_event(tid, "briefed", skill_sources={"tdd": "bundled"})
    conn = sqlite3.connect(home() / "locks.db")
    row = skill_use.report(conn, home(), known={"old": "bundled"})
    states = {r["skill"]: r["state"] for r in row}
    assert states == {"tdd": "active", "old": "dormant"}
    later = datetime.now(UTC) + timedelta(days=20)
    assert skill_use.report(conn, home(), now=later)[0]["state"] == "stale"
    skill_use.pin(home(), "old")
    assert {r["skill"]: r["state"] for r in skill_use.report(conn, home(), known={"old": "x"})}["old"] == "pinned"


def test_forensics_reads_a_copy_and_counts_friction() -> None:
    store = _store()
    tid = _task(store)
    store.close()
    conn = sqlite3.connect(home() / "locks.db")
    now = datetime.now(UTC).isoformat()
    conn.execute("INSERT INTO attempts (task_id, seq, cell, max_turns, passed, cost_usd, created_at) "
                 "VALUES (?, 1, 'sonnet/high', 30, 0, 1.0, datetime('now')), (?, 2, 'opus/high', 30, 1, 3.0, "
                 "datetime('now'))", (tid, tid))
    for ok in (0, 0, 1):
        conn.execute("INSERT INTO tool_events (session_id, task_id, attempt, ts, tool, sig, arg_hash, ok) "
                     "VALUES ('s', ?, 1, ?, 'bash', 'bash:npm', 'h', ?)", (tid, now, ok))
    conn.commit()
    conn.close()
    copy = forensics.snapshot(home() / "locks.db")
    assert copy != home() / "locks.db"
    got = forensics.report(forensics.open_copy(copy), {"LOCKS_HOME": str(home())})
    assert got["label"] == "observed"
    assert got["spend"]["usd"] == 4.0 and got["spend"]["unpassed_usd"] == 1.0
    assert got["friction"]["failed"] == 2 and got["friction"]["repeated"] == 2


def test_forensics_cli_runs(tmp_path: Path) -> None:
    _store().close()
    out = subprocess.run([sys.executable, "-m", "locks", "forensics", "--json"], cwd=ROOT, text=True,
                         capture_output=True, check=True).stdout
    assert json.loads(out)["label"] == "observed"
    for cmd in (["footprint"], ["skills"]):
        subprocess.run([sys.executable, "-m", "locks", *cmd], cwd=ROOT, check=True, capture_output=True)


def test_notes_fork_alternates_then_keeps_the_better_arm() -> None:
    store = _store()
    sides = []
    for _ in range(notes_fork.TRIAL * 2):
        side, _why = notes_fork.arm(store, "p")
        sides.append(side)
        notes_fork.tally(store, "p", side, kept=3 if side == "haiku" else 1, usd=0.01)
    assert sides.count("fork") == sides.count("haiku") == notes_fork.TRIAL
    assert notes_fork.arm(store, "p")[0] == "haiku"


def test_ask_fork_reads_the_cli_answer(tmp_path: Path) -> None:
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"structured_output": {"facts": []},
                                                                "total_cost_usd": 0.02}), "")
    out, cost, why = notes_fork.ask_fork("sid", tmp_path, runner=runner)("sys", {}, "text")
    assert out == {"facts": []} and cost == 0.02 and not why
    assert "--fork-session" in calls[0] and "--resume" in calls[0]


def test_ab_traps_build_and_compare(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("traps", ROOT / "bench" / "ab" / "traps.py")
    traps = importlib.util.module_from_spec(spec)
    sys.modules["traps"] = traps
    spec.loader.exec_module(traps)
    for trap in traps.TRAPS:
        repo = traps.build(trap, tmp_path)
        assert (repo / ".git").is_dir()
    rows = [{"trap": "t", "arm": a, "right": a == "head", "attempts": n, "turns": 3, "usd": 1.0, "failed_calls": n}
            for a, n in (("base", 2), ("head", 1))]
    got = traps.compare(rows)
    assert got["t"]["head"]["right"] == 1 and got["t"]["base"]["attempts"] == 2
    assert traps.measure(tmp_path / "none")["status"] is None
