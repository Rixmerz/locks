from __future__ import annotations

import csv
import io
import json

from locks import analysis, cli
from locks.escalate import Failure
from locks.launch import WorkerResult
from locks.orchestrate import Options, run
from locks.store import home
from locks.ui import api

from .test_orchestrate import Script, bad, kind, ok
from .test_survey import user, write


def refused():
    return WorkerResult(False, Failure.PERMISSION, "the migration was refused", denied=("Bash(alembic upgrade)",),
                        allow=("Bash(alembic:*)",), cost_usd=0.05)


def seed(git_repo, tmp_path, store):
    """Four runs: a first-try pass, a climb, a refusal, and a queued pass a session sent."""
    run("fix the cart total", git_repo, store, registry={}, launcher=Script(ok(cost=0.2)), classifier=kind("implement"))
    run("fix the invoice rounding", git_repo, store, registry={},
        launcher=Script(bad(Failure.CODE_BUG, "missed it", cost=0.1), ok(cost=0.3)), classifier=kind("implement"))
    run("migrate the totals table", git_repo, store, registry={}, launcher=Script(refused()),
        classifier=kind("implement"))
    write(tmp_path / "claude-config", "-repo", "sess", [user("the cart must keep its coupon field", git_repo)])
    queued = store.enqueue("add the coupon field to the cart", repo="r", cwd=str(git_repo), session_id="sess")
    run(queued["body"], git_repo, store, Options(), registry={}, launcher=Script(ok(cost=0.4)),
        classifier=kind("feature"), task_id=queued["id"])


def test_the_analysis_counts_passes_climbs_phases_and_the_session_s_thread(git_repo, tmp_path, store):
    seed(git_repo, tmp_path, store)
    report = analysis.compute(store, days=30)
    s = report["summary"]
    assert (s["runs"], s["tasks"], s["passed"]) == (4, 4, 3)
    assert s["pass_rate"] == 0.75 and s["first_try_rate"] == 0.5 and s["climbed_rate"] == 0.25
    assert s["cost_usd"] == 1.05 and s["cost_per_pass_usd"] == 0.35 and s["refused_rate"] == 0.25
    assert s["wall_p50_s"] is not None and s["queued_p50_s"] is not None
    kinds = {row["kind"]: row for row in report["by_kind"]}
    assert kinds["implement"]["runs"] == 3 and kinds["feature"]["pass_rate"] == 1.0
    assert {row["source"] for row in report["by_source"]} == {"locks"}
    # Every run here was timed, phase by phase.
    assert report["phases"]["runs"] == 4 and set(report["phases"]["seconds"]) == set(analysis.PHASES)
    assert abs(sum(report["phases"]["share"].values()) - 1) < 0.01 or sum(report["phases"]["seconds"].values()) == 0
    cells = {row["cell"]: row for row in report["cells"]}
    assert cells["sonnet/medium"]["attempts"] == 3 and cells["sonnet/medium"]["failures"] == {"code_bug": 1,
                                                                                              "permission": 1}
    assert report["stops"] == {"permission": 1} and report["moves"] == {"more_effort": 1, "blocked": 1}
    # Only the queued run was sent by a session, and its brief had the thread.
    assert report["context"] == [{"group": "with the session's thread", **analysis._summary(
        [r for r in analysis.runs(*store.runs_since("")) if r["has_session"]])}]
    assert len(report["slowest"]) == 4 and report["costliest"][0]["cost_usd"] == 0.4
    assert report["daily"][0]["runs"] == 4
    assert analysis.compute(store, days=30, repo="nowhere")["summary"]["runs"] == 0


def test_a_run_recorded_before_its_phases_were_timed_counts_by_its_timestamps(store):
    task = store.create_task("old work", status="done", source="locks", kind="test")
    for stamp, kind_, data in (
            ("2026-10-01T10:00:00+00:00", "planned", {"kind": "test", "start": "sonnet/medium"}),
            ("2026-10-01T10:04:00+00:00", "attempt_finished", {"cell": "sonnet/medium", "passed": True,
                                                               "cost_usd": 0.2, "turns": 9, "duration_s": 200}),
            ("2026-10-01T10:05:00+00:00", "finished", {"status": "done", "final_cell": "sonnet/medium",
                                                       "cost_usd": 0.21, "stop": None})):
        store._conn.execute("INSERT INTO events (task_id, ts, kind, data) VALUES (?, ?, ?, ?)",
                            (task["id"], stamp, kind_, json.dumps(data)))
    found = analysis.runs(*store.runs_since(""))
    assert found[0]["wall_s"] == 300 and found[0]["worker_s"] == 200 and found[0]["check_s"] is None
    assert found[0]["first_try"] is True and found[0]["cost_usd"] == 0.21
    # Its phases were never measured: the breakdown leaves it out instead of guessing.
    assert analysis.phases(found)["runs"] == 0
    # A run still going has no end yet, and an event before any plan belongs to no run.
    open_task = store.create_task("going", status="running", source="queue")
    store.add_event(open_task["id"], "attempt_started", seq=1)
    store.add_event(open_task["id"], "planned", kind="ui")
    assert [r["task"] for r in analysis.runs(*store.runs_since(""))] == [task["id"]]


def test_a_session_s_turns_and_delegations_are_timed_and_costed(store):
    turn = store.create_task("a prompt", status="done", source="hook", session_id="s")
    store.create_task("cut short", status="interrupted", source="hook", session_id="s")
    store.create_task("a subagent", status="done", source="delegation", session_id="s")
    store.add_usage(message_id="m1", session_id="s", task_id=turn["id"], repo=None, model="claude-sonnet-5-5",
                    input_tokens=100_000, output_tokens=10_000, cache_read=0, cache_write=0, sidechain=0, ts="x")
    out = analysis.turns(store.turns_since(""))
    assert out["turns"]["count"] == 2 and out["turns"]["interrupted"] == 1 and out["turns"]["usd"] > 0
    assert out["turns"]["seconds_p50"] is not None and out["delegations"]["count"] == 1
    assert sum(out["by_day"].values()) == 2


def test_percentiles_and_the_outputs(git_repo, tmp_path, store):
    assert analysis.pct([], 0.5) is None and analysis.pct([None, 3], 0.9) == 3
    assert analysis.pct([1, 2, 3, 4], 0.5) == 2 and analysis.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == 9
    assert analysis._seconds(None) == "—" and analysis._seconds(90) == "90s" and analysis._seconds(600) == "10.0m"
    assert analysis._seconds(9000) == "2.5h" and analysis._between("x", None) is None
    assert analysis._between("2026-10-01 10:00:00", "2026-10-01T10:01:00+00:00") == 60  # no zone: UTC
    empty = analysis.compute(store, days=7)
    assert "No run of locks' yet" in analysis.text(empty)
    seed(git_repo, tmp_path, store)
    report = analysis.compute(store, days=0, keep_runs=None)
    shown = analysis.text(report)
    assert "runs 4 (4 tasks)" in shown and "by kind:" in shown and "where the time went" in shown
    assert "stopped by: permission 1" in shown and "session's thread" in shown
    rows = list(csv.DictReader(io.StringIO(analysis.runs_csv(report["runs"]))))
    assert len(rows) == 4 and rows[0]["kind"] == "implement" and rows[0]["status"] == "done"
    assert json.loads(analysis.as_json(report))["summary"]["runs"] == 4
    assert api.analysis_view(store, days=0)["summary"]["runs"] == 4


def test_the_stats_command(git_repo, tmp_path, store, capsys):
    seed(git_repo, tmp_path, store)
    store._conn.commit()
    assert cli.main(["stats"]) == 0
    assert "locks stats — every repository, last 30 days" in capsys.readouterr().out
    assert cli.main(["stats", "--json", "--days", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["passed"] == 3
    assert cli.main(["stats", "--runs"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 4
    assert cli.main(["stats", "--runs", "--csv", "--repo", str(git_repo)]) == 0
    assert capsys.readouterr().out.startswith("task,repo,kind")
    assert home().is_dir()


def test_a_check_s_cost_and_the_cache_are_kept_apart(store):
    task = store.create_task("checked work", status="done", source="locks", kind="implement")
    for kind_, data in (
            ("planned", {"kind": "implement", "start": "sonnet/medium", "plan_s": 1.0}),
            ("attempt_finished", {"cell": "sonnet/medium", "passed": True, "cost_usd": 0.5, "worker_cost_usd": 0.3,
                                  "verifier_cost_usd": 0.2, "input_tokens": 10_000, "cache_read_tokens": 8_000,
                                  "wall_s": 100}),
            ("finished", {"status": "done", "cost_usd": 0.5, "wall_s": 120})):
        store.add_event(task["id"], kind_, **data)
    old = store.create_task("older work", status="done", source="locks", kind="implement")
    for kind_, data in (("planned", {"kind": "implement"}),
                        ("verified", {"passed": True, "cost_usd": 0.1}),
                        ("attempt_finished", {"cell": "sonnet/medium", "passed": True, "cost_usd": 0.4,
                                              "input_tokens": 50_000}),
                        ("finished", {"status": "done", "cost_usd": 0.4})):
        store.add_event(old["id"], kind_, **data)
    report = analysis.compute(store, days=0)
    cell = report["cells"][0]
    # The cell's own cost leaves its check out (an older attempt's check is read from its `verified`
    # event); the cache share counts only attempts that recorded it.
    assert cell["cost_usd"] == 0.6 and cell["verifier_cost_usd"] == 0.3 and cell["cache_read_share"] == 0.8
    assert report["summary"]["verifier_cost_usd"] == 0.3 and report["summary"]["cache_read_share"] == 0.8
    assert "independent checks $0.30" in analysis.text(report) and "cache read 80%" in analysis.text(report)


def test_attempts_that_went_on_in_a_session_are_compared_with_fresh_ones(git_repo, store):
    shallow = WorkerResult(False, Failure.CODE_BUG, "missed it", cost_usd=0.4, turns=40, session_id="sess-1")
    run("fix the invoice rounding", git_repo, store, registry={}, classifier=kind("implement"),
        launcher=Script(shallow, WorkerResult(True, None, "fixed", "1 passed", cost_usd=0.1, turns=8)))
    run("fix the cart total", git_repo, store, registry={}, classifier=kind("implement"),
        launcher=Script(bad(Failure.CODE_BUG, "missed it", cost=0.4), ok(cost=0.5)))
    old = store.create_task("before sessions were kept", status="done", source="locks", kind="implement")
    for kind_, data in (("planned", {"kind": "implement"}),
                        ("attempt_finished", {"seq": 1, "cell": "sonnet/medium", "passed": False, "cost_usd": 0.3}),
                        ("moved", {"move": "more_turns"}),
                        ("attempt_started", {"seq": 2, "resumed": "sess-9"}),
                        ("attempt_finished", {"seq": 2, "cell": "sonnet/medium", "passed": True, "cost_usd": 0.2}),
                        ("finished", {"status": "done", "cost_usd": 0.5})):
        store.add_event(old["id"], kind_, **data)
    report = analysis.compute(store, days=0)
    rows = {(r["move"], r["continued"]): r for r in report["continuations"]}
    assert set(rows) == {("more_effort", True), ("more_effort", False), ("more_turns", True)}
    went_on, fresh = rows[("more_effort", True)], rows[("more_effort", False)]
    assert (went_on["attempts"], went_on["passed"], went_on["turns_p50"], went_on["cost_usd"]) == (1, 1, 8, 0.1)
    assert (fresh["attempts"], fresh["cost_usd"]) == (1, 0.5)
    # first attempts follow no move, and are no part of it
    assert sum(r["attempts"] for r in report["continuations"]) == 3
    said = analysis.text(report)
    assert "after more effort or more turns" in said and "more_effort  continued attempts    1" in said


def test_the_gaps_between_steps_are_measured_on_each_run_s_ending(git_repo, store, kicks):
    from locks import flow, workflows

    from .test_workflows import CHAIN, _runner, _save

    _save(CHAIN, git_repo)
    started = workflows.start(store, "chain", git_repo, {"goal": "the export"})
    flow.work(store, runner=_runner([ok(), ok(), ok()], []))
    build, check, polish = (store.final_event(t["id"])["data"] for t in store.run_tasks(started["id"]))
    # The dispatcher started each one: from queued to its start, and from there to the plan.
    for ended in (build, check, polish):
        assert ended["pickup_s"] >= 0 and ended["spawn_s"] >= 0 and isinstance(ended["at"], float)
    # The first step was queued by the run's start, no step before it passed: no advance is guessed.
    assert "advance_s" not in build and check["advance_s"] >= 0 and polish["advance_s"] >= 0
    found = {r["step"]: r for r in analysis.runs(*store.runs_since(""))}
    assert found["check"]["advance_s"] == check["advance_s"] and found["build"]["advance_s"] is None
    # A run nobody dispatched (a person's `locks run`) measured none of them.
    direct = run("fix the cart total", git_repo, store, registry={}, launcher=Script(ok()),
                 classifier=kind("implement"))
    ended = store.final_event(direct.task_id)["data"]
    assert not {"spawn_s", "pickup_s", "advance_s"} & set(ended)


def test_the_breakdown_shows_the_gaps_instead_of_other_and_leaves_out_what_was_not_measured(store):
    def ended(task_id, wall, **phases):
        store.add_event(task_id, "planned", kind="implement", start="sonnet/medium", plan_s=1.0, queued_s=30.0)
        store.add_event(task_id, "finished", status="done", wall_s=wall, worker_s=50.0, check_s=10.0,
                        verifier_s=0.0, wait_s=0.0, **phases)

    a = store.create_task("a step", status="done", source="queue", kind="implement")
    ended(a["id"], 70.0, advance_s=4.0, pickup_s=25.0, spawn_s=5.0)
    b = store.create_task("a person's run", status="done", source="locks", kind="implement")
    ended(b["id"], 70.0)
    found = analysis.runs(*store.runs_since(""))
    ph = analysis.phases(found)
    seconds = ph["seconds"]
    assert (seconds["advance_s"], seconds["pickup_s"], seconds["spawn_s"]) == (4.0, 25.0, 5.0)
    # The queue time a run split into pickup and spawn is not counted twice; the other run's stays.
    assert seconds["queued_s"] == 30.0
    # Inside each run, what no phase measured: 70 - 1 - 50 - 10, twice.
    assert seconds["other_s"] == 18.0
    said = analysis.text(analysis.compute(store, days=0))
    assert "advance 4s" in said and "pickup 25s" in said and "spawn 5s" in said
    assert {"advance_s", "pickup_s", "spawn_s"} <= set(analysis.RUN_COLUMNS)


def test_an_attempt_whose_worker_died_is_lost_time_and_notes_are_their_own_phase(store):
    """The time a dead worker cost was read as "other": 17% of it, here."""
    from datetime import UTC, datetime, timedelta

    task = store.create_task("x", status="interrupted", source="locks", kind="implement")
    start = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    for event_kind, at, data in (("planned", start, {"kind": "implement", "plan_s": 1.0}),
                                 ("attempt_started", start, {"seq": 1}),
                                 ("finished", start + timedelta(minutes=30), {"status": "interrupted"})):
        event = store.add_event(task["id"], event_kind, **data)
        store._conn.execute("UPDATE events SET ts = ? WHERE id = ?", (at.isoformat(timespec="seconds"), event))
    done = store.create_task("y", status="done", source="locks", kind="implement")
    store.add_event(done["id"], "planned", kind="implement", plan_s=1.0)
    store.add_event(done["id"], "attempt_started", seq=1)
    store.add_event(done["id"], "attempt_finished", seq=1, passed=True, wall_s=50.0)
    store.add_event(done["id"], "finished", status="done", wall_s=70.0, worker_s=50.0, check_s=10.0, notes_s=6.0)
    found = {r["task"]: r for r in analysis.runs(*store.runs_since(""))}
    assert found[task["id"]]["lost_s"] == 1800.0 and found[done["id"]]["lost_s"] is None
    seconds = analysis.phases(list(found.values()))["seconds"]
    assert seconds["lost_s"] == 1800.0 and seconds["notes_s"] == 6.0
    assert seconds["other_s"] == 3.0  # 70 - 1 - 50 - 10 - 6; the run that died leaves nothing to "other"
    assert {"lost_s", "notes_s"} <= set(analysis.RUN_COLUMNS)


def test_a_dead_worker_noticed_days_later_is_lost_only_up_to_its_time_limit(store):
    from datetime import UTC, datetime, timedelta

    from locks import launch

    task = store.create_task("x", status="interrupted", source="locks", kind="implement")
    start = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    for event_kind, at, data in (("planned", start, {"kind": "implement", "plan_s": 1.0}),
                                 ("attempt_started", start, {"seq": 1, "max_turns": 30}),
                                 ("finished", start + timedelta(days=3), {"status": "interrupted"})):
        event = store.add_event(task["id"], event_kind, **data)
        store._conn.execute("UPDATE events SET ts = ? WHERE id = ?", (at.isoformat(timespec="seconds"), event))
    run = analysis.runs(*store.runs_since(""))[0]
    limit = launch.timeout_for(30)
    # Three days passed before a sweep noticed it; the worker could not have run past its limit.
    assert run["lost_s"] == limit and run["unnoticed_s"] == 3 * 86400 - limit
    assert "unnoticed_s" in analysis.RUN_COLUMNS


def test_the_runs_that_cost_most_for_their_kind_say_why(capsys):
    def r(task, cost, **kw):
        base = {"task": task, "title": f"t{task}", "kind": "implement", "cost_usd": cost, "moves": [], "cells": [],
                "turns": 20, "verifier_cost_usd": 0.0, "check_s": None, "wall_s": None, "refused": 0, "stop": None,
                "attempts": 1}
        return {**base, **kw}

    found = [r(n, 1.0) for n in range(1, 6)] + [
        r(9, 30.0, moves=["more_turns", "next_model"], cells=["sonnet/high", "sonnet/high", "opus/high"], turns=320,
          verifier_cost_usd=2.0, check_s=600.0, wall_s=1800.0, stop="turns", attempts=3)]
    rows = analysis.expensive(found)
    assert rows[0]["task"] == 9
    why = rows[0]["why"]
    assert "ran out of turns 1× and was given more (320 turns in all)" in why
    assert "climbed models: sonnet → opus" in why and "stopped as turns" in why
    assert any(w.startswith("its check took") for w in why)
    assert analysis.expensive_text(rows).startswith("The runs that cost the most for their kind")
    assert analysis.expensive_text([]).startswith("No run")


def test_stats_expensive_prints_the_section(capsys):
    from locks import cli

    assert cli.main(["stats", "--expensive"]) == 0
    assert capsys.readouterr().out.startswith("No run cost more")
    assert cli.main(["stats", "--expensive", "--json"]) == 0
    assert capsys.readouterr().out.strip() == "[]"


def test_a_pickup_is_split_by_what_it_waited_on(store):
    from datetime import datetime

    from locks import analysis, orchestrate

    task = store.create_task("t", status="running", source="queue", repo="r")
    store.add_event(task["id"], "dispatch_decided", parallel=False, reason="may touch #1")
    decided = datetime.fromisoformat(store.last_event(task["id"], "dispatch_decided")["ts"]).timestamp()
    queued, at = decided - 100, decided + 300
    # judged to wait from the decision to its dispatch; the hundred seconds before, the ceiling
    out = orchestrate.pickup_causes(store, task["id"], queued, at, {"level": 1, "ceiling": 1})
    assert out == {"pickup_conflict_s": 300.0, "pickup_ceiling_s": 100.0}
    assert orchestrate.pickup_causes(store, task["id"], queued, at, {"level": 2})["pickup_level_s"] == 100.0
    runs = [{"pickup_s": 400.0, "pickup_why": out}, {"pickup_s": 50.0, "pickup_why": {}}]
    told = analysis.pickup(runs)
    assert told["seconds"]["pickup_conflict_s"] == 300.0 and told["unknown_s"] == 50.0
    assert told["share"]["pickup_ceiling_s"] == 0.25


def test_livespec_use_is_counted_from_the_attempts_it_was_handed(store):
    from locks import analysis

    task = store.create_task("build it", status="done", source="locks")
    for seq, passed, calls in ((1, False, 0), (2, True, 3)):
        store.add_event(task["id"], "attempt_started", seq=seq, capabilities=["livespec"])
        store._conn.execute("INSERT INTO attempts (task_id, seq, cell, max_turns, passed, created_at) "
                            "VALUES (?, ?, 'sonnet/high', 40, ?, '2026')", (task["id"], seq, int(passed)))
        for _ in range(calls):
            store._conn.execute("INSERT INTO tool_events (task_id, attempt, ts, tool, sig, arg_hash, ok) "
                                "VALUES (?, ?, '2026', 'mcp__livespec__who_calls', 'x', '', 1)", (task["id"], seq))
    other = store.create_task("other", status="done", source="locks")
    store.add_event(other["id"], "attempt_started", seq=1, capabilities=[])
    use = analysis.capability_use(store.capability_use("livespec"))
    assert use == {"given": 2, "used": 1, "calls": 3, "passed_used": 1.0, "passed_idle": 0.0}


# --- livespec: used or not, which calls, and what a run cost in tokens ----------------


class Calling:
    """A launcher whose worker calls tools through the hooks' own fast path (`signature.append`), as a
    `claude -p` worker does, then answers with `tokens` spent."""

    def __init__(self, tools, tokens, store):
        self.tools, self.tokens, self.store, self.specs = tools, tokens, store, []

    def __call__(self, spec):
        from locks import signature

        self.specs.append(spec)
        task = self.store._conn.execute("SELECT max(id) FROM tasks").fetchone()[0]
        env = {"LOCKS_HOME": str(home()), "LOCKS_WORKER_TASK": str(task),
               "LOCKS_WORKER_ATTEMPT": str(len(self.specs))}
        for tool in self.tools:
            signature.append({"tool_name": tool, "tool_input": {"q": "x"}, "session_id": "w"}, env)
        return WorkerResult(True, None, "done", evidence="1 passed", cost_usd=0.1, input_tokens=self.tokens,
                            output_tokens=1_000)


def _indexed_run(git_repo, tmp_path, store, text, tools, tokens, *, kind_name="implement", livespec=True):
    from .test_orchestrate import _livespec

    adapters = [_livespec(tmp_path)] if livespec else []
    return run(text, git_repo, store, registry={}, launcher=Calling(tools, tokens, store),
               classifier=kind(kind_name), adapters=adapters)


def test_an_attempt_keeps_the_livespec_calls_its_worker_made_and_a_run_without_livespec_keeps_none(
        git_repo, tmp_path, store, monkeypatch):
    from .livespec_fixture import build

    build(git_repo)
    monkeypatch.setenv("LOCKS_LIVESPEC_TRIAL", "off")  # every worker handed it: the record of calls is the point
    live = "mcp__livespec__"
    used = _indexed_run(git_repo, tmp_path, store, "fix the cart total",
                        [live + "quick_orient", live + "read_unit", live + "quick_orient", "Read"], 100_000)
    assert store.events_of(used.task_id, "attempt_finished")[0]["data"]["livespec_calls"] == {
        "quick_orient": 2, "read_unit": 1}
    idle = _indexed_run(git_repo, tmp_path, store, "fix the invoice total", ["Read", "Grep"], 100_000)
    assert store.events_of(idle.task_id, "attempt_finished")[0]["data"]["livespec_calls"] == {}
    bare = _indexed_run(git_repo, tmp_path, store, "fix the coupon total", [live + "quick_orient"], 100_000,
                        livespec=False)
    assert "livespec_calls" not in store.events_of(bare.task_id, "attempt_finished")[0]["data"]


def test_tool_counts_are_one_attempt_s_calls_by_name(store):
    task = store.create_task("t", status="done", source="locks")
    for attempt, tool in ((1, "mcp__livespec__who_calls"), (1, "mcp__livespec__who_calls"),
                          (1, "mcp__livespec__read_unit"), (1, "Read"), (2, "mcp__livespec__find_symbol")):
        store.add_tool_event(task_id=task["id"], attempt=attempt, tool=tool, sig="x", arg_hash="h", ok=1)
    assert store.tool_counts(task["id"], 1, "mcp__livespec__") == {"read_unit": 1, "who_calls": 2}
    assert store.tool_counts(task["id"], 2, "mcp__livespec__") == {"find_symbol": 1}
    assert store.tool_counts(task["id"], 3, "mcp__livespec__") == {}


def test_stats_compare_the_tokens_of_runs_that_called_livespec_with_those_that_did_not(git_repo, tmp_path, store,
                                                                                        monkeypatch):
    from .livespec_fixture import build

    build(git_repo)
    monkeypatch.setenv("LOCKS_LIVESPEC_TRIAL", "off")  # every worker handed it: the comparison is of calls
    live = "mcp__livespec__"
    _indexed_run(git_repo, tmp_path, store, "fix the cart total", [live + "quick_orient"], 99_000)
    _indexed_run(git_repo, tmp_path, store, "fix the invoice total", [live + "read_unit"], 119_000)
    _indexed_run(git_repo, tmp_path, store, "fix the coupon total", ["Read"], 299_000)
    report = analysis.compute(store, days=30, keep_runs=None)
    rows = {r["kind"]: r for r in report["livespec_tokens"]}
    row = rows["implement"]
    assert row["with"] == {"runs": 2, "avg": 110_000, "p50": 100_000.0}
    assert row["without"] == {"runs": 1, "avg": 300_000, "p50": 300_000.0}
    assert row["change"] == round((110_000 - 300_000) / 300_000, 3)
    said = analysis.text(report)
    assert "tokens per run by kind, livespec called against not called (observed, not a trial):" in said
    assert "implement" in said and "with 2 run(s) avg 110k p50 100k" in said and "without 1 run(s) avg 300k" in said
    assert "(-63%)" in said
    by_run = {r["task"]: r for r in report["runs"]}
    first = min(by_run)
    assert by_run[first]["livespec_handed"] is True and by_run[first]["livespec_calls"] == 1
    assert by_run[first]["tokens"] == 100_000 and by_run[first]["livespec_tools"] == {"quick_orient": 1}
    csv_rows = list(csv.DictReader(io.StringIO(analysis.runs_csv(report["runs"]))))
    assert {"livespec_handed", "livespec_calls", "tokens"} <= set(csv_rows[0])
    assert csv_rows[0]["tokens"] == "100000" and csv_rows[0]["livespec_calls"] == "1"


def test_the_token_comparison_holds_one_arm_and_leaves_out_runs_that_spent_none():
    rows = analysis.livespec_tokens([
        {"kind": "ui", "tokens": 50_000, "livespec_calls": 4},
        {"kind": "ui", "tokens": 0, "livespec_calls": 0},
        {"kind": "docs", "tokens": 10_000, "livespec_calls": 0},
        {"kind": "plan", "tokens": 0, "livespec_calls": 0},
        {"kind": None, "tokens": 5, "livespec_calls": 0}])
    assert [r["kind"] for r in rows] == ["docs", "ui"]
    ui = rows[1]
    assert ui["with"]["runs"] == 1 and ui["without"] == {"runs": 0, "avg": None, "p50": None}
    assert ui["change"] is None
    assert "ui" in analysis._tokens_line(ui) and "without 0 run(s) avg - p50 -" in analysis._tokens_line(ui)
    assert analysis._tokens(950) == "950" and analysis._tokens(None) == "-"


def test_each_climb_of_a_ladder_is_counted_by_rung_with_whether_the_next_attempt_passed(store):
    from .test_standards import finished

    for passed in (True, True, False):
        t = finished(store, ("sonnet/high", False, "code_bug", "more_effort"), ("sonnet/xhigh", passed),
                     kind="implement")
        store.add_event(t, "moved", seq=1, move="more_effort", from_cell="sonnet/high", to_cell="sonnet/xhigh")
    t = finished(store, ("sonnet/xhigh", False, "approach", "next_model"), ("opus/high", True), kind="implement")
    store.add_event(t, "moved", seq=1, move="next_model", from_cell="sonnet/xhigh", to_cell="opus/high")
    # A climb whose next attempt never started, and a move that is no climb, count nothing.
    t = finished(store, ("sonnet/high", False, "code_bug"), kind="implement", status="cancelled")
    store.add_event(t, "moved", seq=1, move="more_effort", from_cell="sonnet/high", to_cell="sonnet/xhigh")
    t = finished(store, ("sonnet/high", False, "turns_exhausted", "more_turns"), ("sonnet/high", True))
    store.add_event(t, "moved", seq=1, move="more_turns", from_cell="sonnet/high", to_cell="sonnet/high")
    report = analysis.compute(store, days=0, repo="r")
    rows = [(r["from"], r["to"], r["move"], r["climbs"], r["passed_next"]) for r in report["climbs"]]
    assert rows == [("sonnet/high", "sonnet/xhigh", "more_effort", 3, 2),
                    ("sonnet/xhigh", "opus/high", "next_model", 1, 1)]
    text = analysis.text(report)
    assert "climbs by rung (observed" in text
    assert "implement          sonnet/high → sonnet/xhigh  more effort  climbs   3  passed next 2 (67%)" in text


def test_heavy_reads_said_at_a_prompt_are_counted_with_whether_locks_work_followed(store):
    turn = store.create_task("x", status="done", source="prompt", repo="r", session_id="s")
    store.add_event(turn["id"], "routed", mode="main", read_chars=50_000, read_calls=4, read_said=True)
    store.add_event(turn["id"], "routed", mode="main", read_chars=100, read_calls=1, read_said=False)
    from .test_standards import finished

    store.create_task("explore it", status="done", source="locks", repo="r", session_id="s")
    finished(store, ("haiku/medium", True), kind="explore")  # a run, so the text says more than "no run yet"
    report = analysis.compute(store, days=0, repo="r")
    assert report["heavy_reads"] == {"advised": 1, "sent": 1, "sent_rate": 1.0}
    assert "heavy reads said 1 time(s)" in analysis.text(report)


def _ended_run(store, kind_="review-routine", source="locks"):
    task = store.create_task("review the change", status="done", source=source, repo="r", kind=kind_)
    store.add_event(task["id"], "planned", kind=kind_, start="sonnet/high")
    store.add_event(task["id"], "finished", status="done", final_cell="sonnet/high", cost_usd=0.1, wall_s=5)
    return task["id"]


def test_the_reviews_held_back_as_unread_are_counted_by_who_held_them_and_whether_it_was_again(store):
    from locks import behavior

    zero = {"worker": 0, "worker_again": 0, "verifier": 0, "verifier_again": 0}
    first, second = _ended_run(store), _ended_run(store, "review-critical")
    assert analysis.compute(store, days=0)["reviews_unread"] == zero
    for task, seq, who, again in ((first, 1, "worker", False), (first, 2, "worker", True),
                                  (second, 1, "worker", False), (second, 1, "verifier", False),
                                  (second, 2, "verifier", True), (second, 3, "verifier", False)):
        store.add_event(task, "review_unread", seq=seq, **behavior.unread_record(who, again=again, calls=0))
    # A task the window does not hold (a session's turn is no run of locks') is not counted.
    store.add_event(_ended_run(store, source="hook"), "review_unread", seq=1,
                    **behavior.unread_record("worker", again=False, calls=0))
    report = analysis.compute(store, days=30)
    assert report["reviews_unread"] == {"worker": 2, "worker_again": 1, "verifier": 2, "verifier_again": 1}
    said = analysis.text(report)
    assert ("reviews held back for reporting no finding and reading nothing (their calls show no read): "
            "workers held 2 (1 unread again), independent checks run again 2 (1 unread twice)") in said
    # An old event is outside the window, a run with none says nothing of it.
    store._conn.execute("UPDATE events SET ts = '2020-01-01T00:00:00+00:00' WHERE kind = 'review_unread'")
    assert analysis.compute(store, days=30)["reviews_unread"] == zero
    assert "held back" not in analysis.text(analysis.compute(store, days=30))
