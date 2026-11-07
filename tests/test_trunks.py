from __future__ import annotations

import json
from pathlib import Path

import pytest

from locks import analysis, arms, flow, trunks, workflows
from locks.escalate import Failure
from locks.launch import WorkerResult
from locks.orchestrate import Options, run
from locks.store import Store

from .test_orchestrate import Script, bad, kind, ok
from .test_workflows import PLAN_PARTS, planned


def _steps(*extra):
    return {"name": "x", "steps": [{"id": "plan", "prompt": "p", "kind": "plan"},
                                   {"id": "look", "prompt": "l", "kind": "explore", "needs": []},
                                   {"id": "make", "prompt": "m", "kind": "implement", "needs": []}, *extra]}


@pytest.mark.parametrize(("step", "said"), [
    ({"id": "b", "prompt": "b", "trunk": "ghost"}, "trunk names 'ghost', which is no step before it"),
    ({"id": "b", "prompt": "b", "trunk": "make", "needs": ["make"]}, "a trunk is a reading step"),
    ({"id": "b", "prompt": "b", "trunk": "plan", "needs": ["look"]}, "does not wait on it"),
    ({"id": "b", "prompt": "b", "kind": "review-routine", "trunk": "plan", "needs": ["plan"]},
     "forks only a trunk that decided nothing"),
    ({"id": "b", "prompt": "b", "kind": "review-routine", "ledger": "refute", "trunk": "look", "needs": ["look"]},
     "a refute step forks no trunk"),
    ({"id": "b", "prompt": "b", "trunk": 3}, "trunk is the id of a reading step"),
])
def test_a_trunk_is_a_reading_step_before_the_step_that_waits_on_it(step, said):
    with pytest.raises(workflows.WorkflowError) as err:
        workflows.normalize(_steps(step))
    assert said in str(err.value)


def test_a_build_forks_a_plan_and_a_blind_review_forks_an_exploration():
    defn = workflows.normalize(_steps({"id": "b", "prompt": "b", "kind": "implement", "trunk": "plan",
                                       "needs": ["plan"]},
                                      {"id": "r", "prompt": "r", "kind": "review-routine", "trunk": "look",
                                       "needs": ["look", "b"]}))
    assert trunks.is_trunk(defn, "plan") and trunks.is_trunk(defn, "look") and not trunks.is_trunk(defn, "make")


def test_what_a_step_forks_is_its_trunk_s_last_passing_session(store: Store):
    assert trunks.source(store, None, "plan")["missing"] == "step 'plan' has no task"
    task = store.create_task("plan it", status="failed", source="queue")
    assert "did not pass" in trunks.source(store, task, "plan")["missing"]
    store.update_task(task["id"], status="done")
    store.add_attempt(task["id"], cell="opus/xhigh", max_turns=80, passed=1)
    assert "kept no session" in trunks.source(store, store.get_task(task["id"]), "plan")["missing"]
    seq = store.add_attempt(task["id"], cell="opus/xhigh", max_turns=80, passed=1, session_id="s-2",
                            served_model="claude-opus-5-5")
    store.add_event(task["id"], "attempt_started", seq=seq, base="abc123")
    assert trunks.source(store, store.get_task(task["id"]), "plan") == {
        "step": "plan", "task": task["id"], "session": "s-2", "model": "claude-opus-5-5", "cell": "opus/xhigh",
        "base": "abc123"}


def test_a_trunk_s_context_is_read_from_its_last_model_call(tmp_path: Path):
    folder = tmp_path / "projects" / "-x"
    folder.mkdir(parents=True)
    usage = {"input_tokens": 3, "cache_read_input_tokens": 40_000, "cache_creation_input_tokens": 2_000}
    (folder / "sess-1.jsonl").write_text("\n".join([
        json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 9}}}), "not json",
        json.dumps({"type": "user", "message": {}}),
        json.dumps({"type": "assistant", "message": {"usage": usage}})]) + "\n")
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    assert trunks.context_tokens("sess-1", env) == 42_003
    assert trunks.context_tokens("gone", env) is None


def test_the_fork_s_first_message_marks_the_new_worker_its_role_and_its_directory(tmp_path: Path):
    trunk = {"step": "plan", "task": 4, "session": "s", "base": "a" * 40}
    said = trunks.note(trunk, role="You are the implementer.", capabilities="Tool hints.", workdir=tmp_path,
                       base="b" * 40)
    assert said.startswith("locks: everything above is the session of the workflow step 'plan' (task #4)")
    assert "never a decision made for you or an opinion to defend" in said
    assert f"Your directory is now {tmp_path}" in said and f"git diff {'a' * 12} {'b' * 12}" in said
    assert said.index("Your role from here") < said.index("Tool hints.")
    assert "moved" not in trunks.note(trunk, role="", capabilities="", workdir=tmp_path, base="a" * 40)


def _trial_task(store: Store, arm: str, *, passed: bool, calls: int, took: float, repo="r"):
    task = store.create_task("t", status="done", source="queue", repo=repo)
    store.add_event(task["id"], "briefed", trunk_arm=arm)
    store.add_attempt(task["id"], cell="sonnet/xhigh", max_turns=80, passed=int(passed), input_tokens=1000,
                      duration_s=took)
    for i in range(calls):
        store._conn.execute("INSERT INTO tool_events (session_id, task_id, attempt, ts, tool, sig, arg_hash, ok) "
                            "VALUES ('w', ?, 1, '2026-10-25T00:00:00Z', 'read', 'read', ?, 1)", (task["id"], str(i)))
    store._conn.execute("INSERT INTO tool_events (session_id, task_id, attempt, ts, tool, sig, arg_hash, ok) "
                        "VALUES ('w', ?, 1, '2026-10-25T00:00:00Z', 'edit', 'edit', 'e', 1)", (task["id"],))
    return task


def test_the_trunk_s_trial_alternates_then_keeps_forking_only_where_it_won(store: Store):
    ids = {trunks.ARMS[arms.side("trunk", "r", n, 0, 0)]: n for n in range(1, 30)}
    assert trunks.arm(store, "r", ids["fresh"]) == ("fresh", "the trunk's trial here (0 forked, 0 fresh, 10 each)")
    assert trunks.arm(store, "r", ids["fork"])[0] == "fork" and trunks.arm(store, None, 7)[0] == "fork"
    _trial_task(store, "fork", passed=True, calls=2, took=60)
    _trial_task(store, "fork", passed=True, calls=2, took=60)
    assert trunks.arm(store, "r", ids["fork"])[0] == "fresh"  # two apart: the arm with fewer tasks goes next
    for _ in range(trunks.TRIAL):
        _trial_task(store, "fork", passed=True, calls=2, took=60)
        _trial_task(store, "fresh", passed=True, calls=15, took=200)
    assert trunks.trial(store, "r")["fresh"] == {"tasks": 10, "passed_first": 10, "calls_before_edit_p50": 15,
                                                "input_tokens_p50": 1000, "duration_p50_s": 200}
    arm, why = trunks.arm(store, "r", 8)
    assert arm == "fork" and "won" in why
    # Where forked first tries failed more, forking lost; early, once each arm has enough.
    for _ in range(trunks.EARLY):
        _trial_task(store, "fork", passed=False, calls=1, took=30, repo="o")
        _trial_task(store, "fresh", passed=True, calls=15, took=200, repo="o")
    arm, why = trunks.arm(store, "o", 8)
    assert arm == "fresh" and "early" in why
    # No faster and no better: lost at the end of its trial.
    for _ in range(trunks.TRIAL):
        _trial_task(store, "fork", passed=True, calls=20, took=300, repo="s")
        _trial_task(store, "fresh", passed=True, calls=15, took=200, repo="s")
    assert trunks.arm(store, "s", 8)[0] == "fresh"


def test_stats_show_the_trunk_s_trial(store: Store):
    task = _trial_task(store, "fork", passed=True, calls=3, took=90)
    _trial_task(store, "fresh", passed=False, calls=9, took=240)
    store.add_event(task["id"], "planned", kind="implement", start="sonnet/medium", plan_s=1.0)
    store.add_event(task["id"], "finished", status="done", wall_s=60.0)
    report = analysis.compute(store, days=0)
    said = analysis.text(report)
    assert "workflow steps that fork the step that read for them" in said
    assert "fork        tasks    1  first 100%  calls before the first change p50 3  input p50 1000  time p50 90s" \
        in said


@pytest.fixture
def forking(monkeypatch):
    monkeypatch.setenv("LOCKS_TRUNK", "on")
    monkeypatch.setattr(trunks, "arm", lambda *a: ("fork", "the trunk's trial here (test)"))


TRUNK = {"step": "plan", "task": 99, "session": "trunk-sess", "model": "claude-opus-5-5", "base": None}


def test_a_step_s_first_try_forks_its_trunk_with_its_role_after_the_fork(git_repo, store: Store, forking):
    script = Script(bad(Failure.CODE_BUG), ok())
    report = run("add the export", git_repo, store, Options(trunk=dict(TRUNK)), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "done"
    first, second = script.specs
    assert first.resume_session == "trunk-sess" and first.fork_session
    assert first.resume_note == first.prompt and "add the export" in first.prompt
    assert first.resume_prompt.startswith("locks: everything above is the session of the workflow step 'plan'")
    assert f"Your directory is now {first.target_dir}" in first.resume_prompt
    if first.append_system_prompt:
        # The fork keeps the trunk's recorded system prompt: the role is said in its first message.
        assert "Your role from here" in first.resume_prompt
    # A later try goes on from its own attempts, never from the trunk.
    assert second.resume_session != "trunk-sess"
    briefed = store.events_of(report.task_id, "briefed")[0]["data"]
    assert briefed["trunk_arm"] == "fork" and briefed["trunk_task"] == 99
    started = store.events_of(report.task_id, "attempt_started")
    assert started[0]["data"]["forked_trunk"] == {"task": 99, "step": "plan", "session": "trunk-sess"}
    assert "forked_trunk" not in started[1]["data"]


def test_a_step_with_nothing_to_fork_starts_fresh_and_is_in_no_arm(git_repo, store: Store, forking):
    script = Script(ok())
    report = run("add the export", git_repo, store, Options(trunk={"step": "plan", "missing": "it kept no session"}),
                 registry={}, launcher=script, classifier=kind("implement"))
    assert not script.specs[0].resume_session
    briefed = store.events_of(report.task_id, "briefed")[0]["data"]
    assert "trunk_arm" not in briefed and briefed["trunk_why"].endswith("it kept no session")


def test_a_trunk_over_its_room_is_not_forked(git_repo, store: Store, forking, monkeypatch):
    monkeypatch.setattr(trunks, "context_tokens", lambda *a: trunks.MAX_TOKENS + 1)
    script = Script(ok())
    report = run("add the export", git_repo, store, Options(trunk=dict(TRUNK)), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert not script.specs[0].resume_session
    assert "over 120000" in store.events_of(report.task_id, "briefed")[0]["data"]["trunk_why"]


def test_trunks_off_forks_nothing(git_repo, store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_TRUNK", "off")
    script = Script(ok())
    run("add the export", git_repo, store, Options(trunk=dict(TRUNK)), registry={}, launcher=script,
        classifier=kind("implement"))
    assert not script.specs[0].resume_session


def test_a_trunk_keeps_its_role_out_of_its_system_prompt(git_repo, store: Store):
    script = Script(WorkerResult(True, None, "planned", evidence="read it"))
    run("Plan, without changing any file, the export", git_repo, store, Options(trunk_for=True, base="HEAD"),
        registry={}, launcher=script, classifier=kind("plan"))
    spec = script.specs[0]
    assert spec.append_system_prompt == ""
    plain = Script(WorkerResult(True, None, "planned", evidence="read it"))
    run("Plan, without changing any file, the export", git_repo, store, Options(), registry={}, launcher=plain,
        classifier=kind("plan"))
    if plain.specs[0].append_system_prompt:
        assert spec.prompt.startswith("Your role for this step:\n")


def test_feature_parallel_s_parts_fork_the_plan_s_session(git_repo, store: Store, kicks, forking):
    import dataclasses

    defn = workflows.load("feature-parallel", git_repo, "bundled")
    assert {s["id"]: s for s in defn["steps"]}["build"]["trunk"] == "plan"
    workflows.start(store, "feature-parallel", git_repo, {"goal": "the orders view"})
    results = [dataclasses.replace(planned(), session_id="plan-sess"), ok("api built"), ok("web built"),
               ok("no problems")]
    specs: list = []

    def launch(spec):
        specs.append(spec)
        mine = next((p["id"] for p in PLAN_PARTS if f"Your part: {p['id']}" in spec.prompt), None)
        if mine and "Edit" not in spec.disallowed_tools:
            (spec.target_dir / mine).mkdir(exist_ok=True)
            (spec.target_dir / mine / f"{mine}.py").write_text(f"{mine}\n")
        return results.pop(0)

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    flow.work(store, runner=runner)
    plan, *parts = specs[:3]
    # The plan kept its session (it ran in a worktree) and no role in its system prompt.
    assert plan.persist_session and plan.append_system_prompt == ""
    assert len(parts) == 2
    for spec in parts:
        assert spec.resume_session == "plan-sess" and spec.fork_session
        assert spec.resume_note == spec.prompt and "Your part:" in spec.prompt
