"""0.80.0: an attempt whose worker died keeps its own number and what it spent, and a worker that
left `notes_used` out is told apart from one that said none helped."""
from __future__ import annotations

import os

from locks import analysis, flow, launch, usage
from locks.store import Store


def _dead(store: Store) -> dict:
    task = store.create_task("t", status="running", source="locks", repo="r", kind="feature", pid=999_999)
    store.add_event(task["id"], "planned", plan_s=0.1)
    store.add_event(task["id"], "attempt_started", seq=1, cell="sonnet/high")
    return store.get_task(task["id"])


def test_a_dead_attempt_is_never_numbered_again(store: Store):
    task = _dead(store)
    assert store.next_attempt(task["id"]) == 2  # no `attempts` row, yet number 1 is taken
    fresh = store.create_task("u", status="running", source="locks", repo="r", kind="feature")
    assert store.next_attempt(fresh["id"]) == 1


def test_the_sweep_keeps_what_a_dead_attempt_spent(store: Store, monkeypatch):
    monkeypatch.setattr(usage, "running_cost", lambda *a, **k: 1.25)
    task = _dead(store)
    assert flow.sweep(store, is_alive=lambda _pid: False) == [task["id"]]
    lost = store.last_event(task["id"], "attempt_lost")
    assert lost["data"]["seq"] == 1 and lost["data"]["cost_usd"] == 1.25
    assert store.get_task(task["id"])["cost_usd"] == 1.25
    run = analysis.runs([store.get_task(task["id"])], store.events_of_kinds(["planned", "attempt_lost", "finished"]))
    assert run[0]["lost_usd"] == 1.25 and run[0]["cost_usd"] == 1.25


def test_a_dead_attempt_whose_cost_cannot_be_read_is_never_zero(store: Store, monkeypatch):
    monkeypatch.setattr(usage, "running_cost", lambda *a, **k: None)
    task = _dead(store)
    flow.sweep(store, is_alive=lambda _pid: False)
    assert store.last_event(task["id"], "attempt_lost")["data"]["cost_usd"] is None
    assert not store.get_task(task["id"])["cost_usd"]


def test_notes_used_left_out_is_unsaid_and_an_empty_list_is_none_relied_on(tmp_path):
    from .test_launch import block, envelope, proc, spec

    said = launch.parse(proc(envelope(block(verdict="pass", summary="s", evidence="e", notes_used=[]))),
                        spec(tmp_path))
    unsaid = launch.parse(proc(envelope(block(verdict="pass", summary="s", evidence="e"))), spec(tmp_path))
    assert said.notes_said and said.notes_used == ()
    assert not unsaid.notes_said


def test_stats_count_tasks_whose_workers_did_not_say():
    events = [{"task_id": 1, "kind": "briefed", "data": {"notes": [3, 4]}},
              {"task_id": 1, "kind": "attempt_finished", "data": {"passed": True, "notes_unsaid": True}},
              {"task_id": 2, "kind": "briefed", "data": {"notes": [3]}},
              {"task_id": 2, "kind": "attempt_finished", "data": {"passed": True, "notes_used": []}}]
    relied = analysis.notes_relied_on(events, {1, 2})
    assert relied["tasks_unsaid"] == 1 and relied["relied_on"] == 0


def test_a_dead_workers_transcript_is_kept_beside_the_trace(tmp_path, monkeypatch):
    from locks import flow, phase, trace, usage

    source = tmp_path / "worker.jsonl"
    source.write_text('{"type": "assistant"}\n')
    monkeypatch.setenv("LOCKS_TRACE", "on")
    monkeypatch.setattr(phase, "session_of", lambda task_id, attempt, env=None: "w-1")
    monkeypatch.setattr(usage, "transcript_of", lambda session, env=None: source)
    flow._keep_lost_transcript(7, 1)
    kept = trace.path(os.environ, "w-1").with_suffix(".transcript.jsonl")
    assert kept.read_text() == source.read_text()
    monkeypatch.setenv("LOCKS_TRACE", "off")
    kept.unlink()
    flow._keep_lost_transcript(7, 1)
    assert not kept.exists()


def test_the_sweep_keeps_the_dead_worker_s_session_for_the_resume_to_go_on_in(store: Store, monkeypatch, tmp_path):
    from locks import orchestrate, phase
    from locks.matrix import Cell

    monkeypatch.setattr(usage, "running_cost", lambda *a, **k: 0.4)
    monkeypatch.setattr(phase, "session_of", lambda *a, **k: "sess-swept")
    config = tmp_path / "claude-config"
    (config / "projects" / "wt").mkdir(parents=True)
    (config / "projects" / "wt" / "sess-swept.jsonl").write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    task = store.create_task("t", status="running", source="locks", repo="r", kind="feature", pid=999_999)
    store.add_event(task["id"], "attempt_started", seq=1, cell="sonnet/high", model="sonnet")
    flow.sweep(store, is_alive=lambda _pid: False)
    assert store.last_event(task["id"], "attempt_lost")["data"]["session"] == "sess-swept"
    monkeypatch.setattr(phase, "session_of", lambda *a, **k: None)  # the log moved on: the event still says it
    assert orchestrate._dead_session(store, task["id"], Cell("sonnet", "high")) == ("sess-swept", "")
