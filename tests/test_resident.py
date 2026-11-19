from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from locks import analysis, arms, resident, trunks
from locks.orchestrate import Options, run
from locks.store import Store

from .test_orchestrate import Script, kind, ok


def _passed(store: Store, *, repo="r", role="implementer", session="s1", cell="sonnet/high", workflow=None):
    task = store.create_task("t", status="done", source="queue", repo=repo)
    if workflow:
        store._conn.execute("UPDATE tasks SET workflow_run = ? WHERE id = ?", (workflow, task["id"]))
    store.add_event(task["id"], "briefed", role=role)
    store.add_attempt(task["id"], cell=cell, max_turns=40, passed=1, session_id=session)
    return task


def test_the_source_is_the_last_passing_worker_of_the_role_on_the_model(store: Store):
    first = _passed(store, session="old")
    _passed(store, role="reviewer", session="other-role")
    _passed(store, session="wf", workflow=1)
    _passed(store, session="opus", cell="opus/high")
    got = resident.source(store, "r", "implementer", 999, "sonnet")
    assert got["task"] == first["id"] and got["session"] == "old" and got["resident"] and got["wrote"]
    assert resident.source(store, "r", "implementer", 999, "haiku") is None
    assert resident.source(store, "other", "implementer", 999, "sonnet") is None
    assert resident.source(store, "r", None, 999, "sonnet") is None
    later = datetime.now(UTC) + timedelta(seconds=resident.WITHIN_S + 60)
    assert resident.source(store, "r", "implementer", 999, "sonnet", now=later) is None


def test_the_note_marks_a_new_worker_on_a_new_task(tmp_path: Path):
    said = resident.note({"task": 4, "base": "a" * 40}, capabilities="Hints.", workdir=tmp_path, base="b" * 40)
    assert said.startswith("locks: everything above is the session of an earlier worker of your role")
    assert "(task #4), which finished and passed" in said and f"Your directory is now {tmp_path}" in said
    assert f"git diff {'a' * 12} {'b' * 12}" in said and said.endswith("Hints.")


def test_the_trial_alternates_then_keeps_forking_only_where_it_won(store: Store):
    ids = {resident.ARMS[arms.side("resident", "r", n, 0, 0)]: n for n in range(1, 30)}
    assert resident.arm(store, "r", ids["fresh"])[0] == "fresh"
    assert resident.arm(store, "r", ids["fork"])[0] == "fork" and resident.arm(store, None, 1)[0] == "fork"
    for _ in range(trunks.TRIAL):
        for arm, took in (("fork", 60), ("fresh", 200)):
            task = store.create_task("t", status="done", source="queue", repo="r")
            store.add_event(task["id"], "briefed", resident_arm=arm)
            store.add_attempt(task["id"], cell="sonnet/high", max_turns=40, passed=1, input_tokens=10,
                              duration_s=took)
    arm, why = resident.arm(store, "r", 3)
    assert arm == "fork" and "won its trial" in why
    store.add_event(task["id"], "planned", kind="implement", start="sonnet/high", plan_s=1.0)
    store.add_event(task["id"], "finished", status="done", wall_s=60.0)
    said = analysis.text(analysis.compute(store, days=0, repo="r"))
    assert "the resident trial" in said and "fork        tasks   10" in said


@pytest.fixture
def residing(monkeypatch):
    monkeypatch.setenv("LOCKS_RESIDENT", "on")
    monkeypatch.setattr(resident, "arm", lambda *a: ("fork", "the resident trial here (test)"))


def _two_tasks(git_repo, store, script):
    first = run("add the export", git_repo, store, Options(), registry={}, launcher=script,
                classifier=kind("implement"))
    second = run("add the import", git_repo, store, Options(), registry={}, launcher=script,
                 classifier=kind("implement"))
    return first, second


def test_the_next_task_of_the_role_forks_the_last_passing_worker(git_repo, store: Store, residing):
    script = Script(dataclasses.replace(ok(), session_id="w1"), ok())
    first, second = _two_tasks(git_repo, store, script)
    if store.events_of(first.task_id, "briefed")[0]["data"].get("role") is None:
        pytest.skip("no role worn here")
    one, two = script.specs
    assert not one.resume_session
    assert two.resume_session == "w1" and two.fork_session and two.resume_note == two.prompt
    assert two.resume_prompt.startswith("locks: everything above is the session of an earlier worker")
    briefed = store.events_of(second.task_id, "briefed")[0]["data"]
    assert briefed["resident_arm"] == "fork" and briefed["resident_task"] == first.task_id
    started = store.events_of(second.task_id, "attempt_started")[0]["data"]
    assert started["forked_from"] == first.task_id and started["forked_session"] == "w1"


def test_a_resident_over_its_room_or_off_is_not_forked(git_repo, store: Store, residing, monkeypatch):
    monkeypatch.setattr(trunks, "context_tokens", lambda *a: trunks.MAX_TOKENS + 1)
    script = Script(dataclasses.replace(ok(), session_id="w1"), ok())
    first, second = _two_tasks(git_repo, store, script)
    assert not script.specs[1].resume_session
    if store.events_of(first.task_id, "briefed")[0]["data"].get("role"):
        assert "over 120000" in store.events_of(second.task_id, "briefed")[0]["data"]["resident_why"]
    monkeypatch.setenv("LOCKS_RESIDENT", "off")
    script = Script(dataclasses.replace(ok(), session_id="w2"), ok())
    _two_tasks(git_repo, store, script)
    assert not script.specs[1].resume_session
