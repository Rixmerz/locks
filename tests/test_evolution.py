from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from locks import __version__, cli, evolution, hooks, standards
from locks.store import Store

from .test_standards import NOW, finished, many, stamp

#: The person's time zone in these tests: three hours behind UTC.
HERE = timezone(timedelta(hours=-3))


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=HERE)


def ended_at(store: Store, moment: datetime, *tries, realized=None, **kw) -> int:
    task_id = finished(store, *tries, at=moment, **kw)
    if realized is not None:
        event = store.final_event(task_id)
        data = {**event["data"], "realized": realized}
        # a fake ending of its own: the record is written as a run writes it
        store._conn.execute("DELETE FROM events WHERE id = ?", (event["id"],))
        new = store.add_event(task_id, "finished", **data)
        store._conn.execute("UPDATE events SET ts = ? WHERE id = ?", (stamp(moment), new))
    return task_id


# --- the evolution history: one snapshot a day, in the person's time zone -------------------------

def test_the_first_ending_of_a_new_local_day_takes_the_snapshot_of_the_days_before(store: Store):
    ended_at(store, local(18, 17), ("sonnet/medium", 1), points=3, wall=1800, cost=1.5, realized=5)
    assert evolution.settle(store, now=local(18, 17, 1), tz=HERE)["snapshot"] == []  # a baseline: nothing before
    ended_at(store, local(18, 23, 30), ("sonnet/medium", 0, "code_bug", "more_effort"), ("sonnet/high", 1),
             points=5, wall=1800, cost=2.5, realized=8)
    # 23:30 here is 02:30 UTC the next day: still the same day for the person, so no new snapshot
    assert evolution.settle(store, now=local(18, 23, 31), tz=HERE)["snapshot"] is None
    ended_at(store, local(19, 1), ("sonnet/medium", 1), points=2, wall=600, cost=0.5)
    rows = evolution.settle(store, now=local(19, 1, 1), tz=HERE)["snapshot"]
    assert [(r["repo"], r["kind"]) for r in rows] == [("r", "implement")]
    row = store.snapshots()[-1]
    assert row["day"] == "2026-10-19" and row["until"] == stamp(local(19, 0).astimezone(UTC))
    assert {k: row[k] for k in ("tasks", "done", "points_done", "points_estimated", "points_realized", "hours",
                                "cost_usd", "velocity", "cost_per_point", "time_per_point_s", "pass_first",
                                "climbed", "version")} == {
        "tasks": 2, "done": 2, "points_done": 8, "points_estimated": 8, "points_realized": 13, "hours": 1.0,
        "cost_usd": 4.0, "velocity": 8.0, "cost_per_point": 0.5, "time_per_point_s": 450.0, "pass_first": 0.5,
        "climbed": 0.5, "version": __version__}
    assert evolution.settle(store, now=local(19, 9), tz=HERE)["snapshot"] is None
    # `locks evolution --snapshot`: one now, from where the last one ended
    taken = evolution.snapshot(store, now=local(19, 9), tz=HERE, until_now=True)
    assert [r["tasks"] for r in taken] == [1] and store.snapshots()[-1]["since"] == row["until"]


def test_a_snapshot_is_append_only(store: Store):
    ended_at(store, local(17, 12), ("sonnet/medium", 1))
    evolution.snapshot(store, now=local(18, 9), tz=HERE)
    with pytest.raises(Exception, match="append-only"):
        store._conn.execute("UPDATE evolution SET tasks = 0")
    with pytest.raises(Exception, match="append-only"):
        store._conn.execute("DELETE FROM evolution")


# --- the report: each metric's trend, and every change with its reason and verdict ------------------

MECHA = "github.com/o/mecha"


def _calibrated(store: Store) -> dict:
    """The example: 2 of 8 passed first at sonnet/high and 4 of 5 at sonnet/xhigh."""
    before = local(15, 12)
    many(store, 2, ("sonnet/high", 1), repo=MECHA, kind="feature", points=5, at=before)
    many(store, 4, ("sonnet/high", 0, "approach", "next_model"), ("sonnet/xhigh", 1), repo=MECHA, kind="feature",
         points=5, at=before)
    finished(store, ("sonnet/high", 0, "approach", "next_model"), ("sonnet/xhigh", 0), repo=MECHA, kind="feature",
             points=5, at=before)
    finished(store, ("sonnet/high", 0, "code_bug", "more_effort"), ("opus/high", 1), repo=MECHA, kind="feature",
             points=5, at=before)
    change, = standards.calibrate(store, MECHA, now=local(20, 12))
    return change


def test_a_change_reads_as_a_sentence_with_its_verdict(store: Store):
    change = _calibrated(store)
    assert (change["old_value"], change["new_value"]) == (None, "sonnet/xhigh")
    report = evolution.report(store, days=30, now=local(21, 12), tz=HERE)
    line, = [c["line"] for c in report["changes"]]
    assert line == ("feature in mecha, 5 points: starts at sonnet/xhigh since Oct 20 because 2 of 8 passed first at "
                    "sonnet/high and 4 of 5 at sonnet/xhigh; no task has run under it yet")
    for n in range(2):
        finished(store, ("sonnet/xhigh", int(n == 0)), repo=MECHA, kind="feature", points=5, used=[change["id"]],
                 at=local(22, 12))
    line = evolution.report(store, now=local(22, 13), tz=HERE)["changes"][0]["line"]
    assert line.endswith(f"; pass-first since then 50 % over 2 tasks, judged after {standards.JUDGE_AFTER}")
    for _ in range(3):
        finished(store, ("sonnet/xhigh", 1), repo=MECHA, kind="feature", points=5, used=[change["id"]],
                 at=local(23, 12))
    standards.judge(store, MECHA, now=local(24, 12))
    found = evolution.report(store, now=local(25, 12), tz=HERE)["changes"][0]
    assert found["line"].endswith("; pass-first since then 80 %, kept")
    assert found["verdict"]["verdict"] == "kept" and found["rule"] == change["rule"]


def test_a_revert_reads_as_one(store: Store):
    change = _calibrated(store)
    many(store, standards.JUDGE_AFTER, ("sonnet/xhigh", 0, "code_bug", "more_effort"), ("opus/high", 0),
         repo=MECHA, kind="feature", points=5, used=[change["id"]], at=local(22, 12))
    standards.judge(store, MECHA, now=local(23, 12))
    reverted, back = evolution.report(store, now=local(24, 12), tz=HERE)["changes"]
    assert reverted["line"].endswith("pass-first since then 0 %, reverted on Oct 23")
    assert back["line"].startswith("feature in mecha, 5 points: back to locks' rules since Oct 23: revert #")


def test_the_trend_of_each_metric_over_the_window(store: Store):
    ended_at(store, local(10, 12), ("sonnet/medium", 0, "code_bug", "more_effort"), ("sonnet/high", 1), points=5,
             wall=3600, cost=4.0, realized=8)
    evolution.snapshot(store, now=local(11, 9), tz=HERE)
    for _ in range(2):
        ended_at(store, local(12, 12), ("sonnet/medium", 1), points=5, wall=1800, cost=1.0, realized=5)
    evolution.snapshot(store, now=local(13, 9), tz=HERE)
    report = evolution.report(store, days=30, now=local(14, 9), tz=HERE)
    trend, = report["trends"]
    assert trend["snapshots"] == 2 and trend["tasks"] == 3 and trend["points_done"] == 15
    assert trend["metrics"]["velocity"] == {"from": 5.0, "to": 10.0, "said": "better"}
    assert trend["metrics"]["cost_per_point"] == {"from": 0.8, "to": 0.2, "said": "better"}
    assert trend["metrics"]["pass_first"] == {"from": 0.0, "to": 1.0, "said": "better"}
    assert trend["metrics"]["climbed"] == {"from": 1.0, "to": 0.0, "said": "better"}
    assert trend["line"].startswith("implement in r: velocity 5.0 → 10.0 points/h (better), cost per point "
                                    "$0.80 → $0.20 (better), time per point 12m → 6m (better), pass-first 0 % → "
                                    "100 % (better), climbs 100 % → 0 % (better); 15 points estimated, 18 realized")
    # the window and the repository narrow it
    assert evolution.report(store, days=1, now=local(20, 9), tz=HERE)["trends"] == []
    assert evolution.report(store, repo="other", now=local(14, 9), tz=HERE)["trends"] == []


def test_one_snapshot_is_no_trend_yet(store: Store):
    ended_at(store, local(10, 12), ("sonnet/medium", 1), points=3, wall=1800)
    evolution.snapshot(store, now=local(11, 9), tz=HERE)
    trend, = evolution.report(store, now=local(12, 9), tz=HERE)["trends"]
    assert trend["metrics"]["velocity"] == {"from": 6.0, "to": 6.0, "said": "one snapshot"}
    assert "velocity 6.0 points/h" in trend["line"]


def test_the_command_prints_the_report_and_its_data(store: Store, capsys, monkeypatch):
    _calibrated(store)
    monkeypatch.setattr(evolution, "_now", lambda: local(21, 12))
    assert cli.main(["evolution"]) == 0
    out = capsys.readouterr().out
    assert "feature in mecha, 5 points: starts at sonnet/xhigh since Oct 20" in out
    assert "No snapshot in the last 30 days yet" in out
    assert cli.main(["evolution", "--json", "--days", "7"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["days"] == 7 and data["changes"][0]["new_value"] == "sonnet/xhigh" and data["standards"]
    assert cli.main(["evolution", "--snapshot"]) == 0
    assert "snapshot: 1 row" in capsys.readouterr().out
    assert len(store.snapshots()) == 1


# --- where it runs: the hooks and the dispatcher, never a GET -----------------------------------

def test_the_stop_hook_settles_and_a_failure_costs_the_turn_nothing(store: Store, monkeypatch, _isolated_home):
    many(store, standards.CALIBRATE_EVERY, ("sonnet/medium", 0, "code_bug", "next_model"), ("opus/high", 1))
    # The day the fake tasks ended, whatever day the suite runs on: nothing before it to snapshot.
    monkeypatch.setattr(evolution, "_now", lambda: NOW - timedelta(hours=23))
    hooks.stop({"session_id": "s"}, store)
    assert [c["field"] for c in store.calibrations(repo="r")] == ["start_cell"]
    assert store.snapshots() == [] and store.meta(evolution.SNAPSHOT_KEY)

    def broken(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(evolution, "settle", broken)
    assert hooks.stop({"session_id": "s"}, store) is None
    assert "Stop (evolution)" in (_isolated_home / hooks.ERROR_LOG).read_text()


def test_the_dispatcher_settles_after_the_work_it_ran(git_repo, store: Store):
    from locks import flow

    from .test_flow import _runner
    from .test_orchestrate import ok

    key = str(git_repo.resolve())
    for n in range(standards.CALIBRATE_EVERY):
        store.enqueue(f"task {n}", repo=key, cwd=str(git_repo))
    flow.work(store, runner=_runner([ok()] * standards.CALIBRATE_EVERY))
    assert store.meta(standards.SEEN_KEY) and store.meta(f"standards:since:{key}") == "0"


def test_the_evolution_screen_reads_and_never_writes(store: Store):
    from locks.ui import api

    _calibrated(store)
    ended_at(store, local(16, 12), ("sonnet/medium", 1), realized=1)
    counts = lambda: [store._conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: E731
                      for t in ("calibrations", "calibration_verdicts", "evolution", "meta")]
    seen = counts()
    view = api.evolution_view(store, days=30, repo_key=MECHA)
    assert view["changes"][0]["repo"] == MECHA and counts() == seen


def test_what_a_snapshot_leaves_out_and_what_a_trend_cannot_say(store: Store):
    ended_at(store, local(10, 12), ("sonnet/medium", 1), points=None, wall=600)  # no points: nothing per point
    ended_at(store, local(10, 13), ("sonnet/medium", 1), repo=None)  # no repository's
    evolution.snapshot(store, now=local(11, 9), tz=HERE)
    ended_at(store, local(12, 12), ("sonnet/medium", 1), points=None, wall=600)
    evolution.snapshot(store, now=local(13, 9), tz=HERE)
    assert {r["repo"] for r in store.snapshots()} == {"r"}
    trend, = evolution.report(store, now=local(14, 9), tz=HERE)["trends"]
    assert trend["metrics"]["velocity"]["said"] == "not measured"
    assert trend["metrics"]["pass_first"] == {"from": 1.0, "to": 1.0, "said": "same"}
    assert "velocity — → — points/h (not measured)" in trend["line"]
    # nothing ended since: a snapshot now writes no row, and a second at the same moment covers nothing
    assert evolution.snapshot(store, now=local(14, 9), tz=HERE, until_now=True) == []
    assert evolution.snapshot(store, now=local(14, 9), tz=HERE, until_now=True) == []
    assert json.loads(store.meta(evolution.SNAPSHOT_KEY))["until"] == stamp(local(14, 9))
    text = evolution.text(evolution.report(store, days=0, now=local(14, 9), tz=HERE))
    assert "all time" in text and "No standard changed in this period." in text and "Standards now" not in text
    assert "No snapshot at all yet" in evolution.text(evolution.report(store, days=0, repo="none", now=local(14, 9)))


def test_the_report_says_what_the_record_decided_beyond_the_standards(store: Store, tmp_path):
    """Where each trial stands, a role's lessons, the notes their trial moved and what a habit caught were
    each in a different command; the evolution report had only the standards."""
    from locks import habits

    one = store.create_task("x", status="done", source="queue", repo="r")["id"]
    store.add_event(one, "briefed", recall_arm="fused", recall_why="the recall order's trial here (1 fused)")
    two = store.create_task("y", status="done", source="queue", repo="r")["id"]
    store.add_event(two, "briefed", recall_arm="fused", recall_why="the recall order won its trial here: 88%",
                    primer_arm="primer", primer_why="the primer's trial here (4 primed, 4 without, 12 each)")
    resumed = store.create_task("z", status="done", source="queue", repo="r")["id"]
    store.add_event(resumed, "briefed", primer_arm="none", primer_why="the arm this task's first brief drew")
    note = store.add_note("r", "code", "a title", "a fact", author="worker", filed_by="worker", state="candidate")
    store.update_note(note, state="dropped", state_reason="disproven: 67% against 100%")
    store.add_note("r", "code", "another", "a fact of its own", author="worker", filed_by="worker", state="candidate")
    (tmp_path / "repo").mkdir()
    hid, _ = habits.install(store, habits.Candidate(("edit:.py", "bash:ruff"), 3, 3, 1.0, 1, 9.0),
                            command="ruff check", repo_dir=tmp_path / "repo")
    store.update_habit(hid, runs=7, caught=2)
    found = evolution.report(store, days=0)["learned"]
    assert [(t["trial"], t["arm"], t["decided"], t["task"]) for t in found["trials"]] == [
        ("primer", "primer", False, two), ("recall", "fused", True, two)]
    assert found["notes"] == [{"repo": "r", "waiting": 1, "unrecalled": 1, "adopted": 0, "dropped": [
        {"id": note, "title": "a title", "reason": "disproven: 67% against 100%"}]}]
    text = evolution.text(evolution.report(store, days=0))
    assert "recall in r: decided, last arm fused" in text and "primer in r: still trying" in text
    assert "1 still candidates (1 of them no brief recalled yet)" in text and "7 runs, 2 found something" in text
    assert evolution.learned(store, repo="elsewhere") == {"trials": [], "roles": [], "notes": [],
                                                          "habits": found["habits"]}
