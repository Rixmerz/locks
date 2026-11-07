"""Branches of the small pure modules the other suites leave unread: arms, capabilities, cells,
evidence, ledger, matrix, methods, naming and allow."""
from __future__ import annotations

import os

import pytest

from locks import allow, arms, capabilities, cells, dispatch, evidence, ledger, matrix, methods, naming, project
from locks.store import Store


def test_tally_ignores_a_row_of_an_arm_it_does_not_count():
    found = arms.tally([{"arm": "with", "tries": 2, "passed": 1}, {"arm": "elsewhere", "tries": 9, "passed": 9}],
                       ("with", "without"))
    assert found["with"] == {"tasks": 1, "tries": 2, "passed": 1, "rate": 0.5, "pass_p50_s": None}
    assert found["without"] == {"tasks": 0, "tries": 0, "passed": 0, "rate": None, "pass_p50_s": None}
    assert "elsewhere" not in found


def test_a_pattern_with_a_folder_matches_at_any_depth():
    assert capabilities.matches("app/routes/index.tsx", ["routes/*.tsx"])
    assert capabilities.matches("routes/index.tsx", ["routes/*.tsx"])
    assert not capabilities.matches("app/lib/index.tsx", ["routes/*.tsx"])


def test_memory_text_falls_back_to_the_body_when_the_run_is_gone_or_has_no_goal(store: Store):
    assert cells.memory_text(store, {"workflow_run": 99, "body": "the body"}) == "the body"
    run = store.create_run(name="w", scope="project", source="x", definition="{}", inputs='{"goal": "  "}')
    assert cells.memory_text(store, {"workflow_run": run["id"], "title": "the title"}) == "the title"
    run = store.create_run(name="w", scope="project", source="x", definition="{}",
                           inputs='{"goal": "fix the cart", "extra": "and totals"}')
    assert cells.memory_text(store, {"workflow_run": run["id"], "body": "b"}) == "fix the cart\nand totals"


def test_the_primer_loses_its_trial_when_it_did_not_reach_the_first_change_sooner(store: Store, monkeypatch):
    arm = {"tasks": cells.PRIMER_TRIAL, "passed_first": cells.PRIMER_TRIAL, "calls_before_edit_p50": 5,
           "input_tokens_p50": 10}
    monkeypatch.setattr(cells, "trial", lambda store, repo: {"primer": dict(arm), "control": dict(arm)})
    side, why = cells.primer_arm(store, "r", 1)
    assert side == "control"
    assert why.startswith("the primer lost its trial here: primed first tries passed 100%")
    assert "p50 5 against 5" in why


def test_the_evidence_trial_ignores_an_arm_it_does_not_know(store: Store, monkeypatch):
    monkeypatch.setattr(store, "arm_trial", lambda repo, key: [
        {"task_id": 1, "arm": "gate", "passed_first": True}, {"task_id": 2, "arm": "other", "passed_first": True}])
    found = evidence.trial(store, "r")
    assert found["gate"]["tasks"] == 1 and found["free"]["tasks"] == 0
    assert "other" not in found


def test_a_step_that_reports_one_finding_twice_is_named_once():
    f = {"where": "a.py:3", "what": "the total drops the tax", "evidence_class": "inferential",
         "cause": "introduced"}
    g = {**f, "what": "tax missing from the total", "evidence_class": "deterministic"}
    rows = ledger.collect([("review", [f, g])])
    assert len(rows) == 1
    assert rows[0]["seen_by"] == ["review"]
    assert rows[0]["evidence_class"] == "deterministic"


def test_a_model_without_the_dial_refuses_an_effort(monkeypatch):
    monkeypatch.setitem(matrix.MODELS, "plain", matrix.Model("plain", 1.0, 1.0, has_effort=False))
    assert matrix.Cell("plain").label == "plain"
    with pytest.raises(ValueError, match="plain has no effort dial"):
        matrix.Cell("plain", "high")


def test_a_premortem_is_judged_by_the_first_tries_of_the_steps_after_its_plan(store: Store):
    premortem = methods.METHODS["plan"]
    run = store.create_run(name="w", scope="project", source="x", definition="{}")
    plan = store.create_task("plan it", status="done", source="locks", repo="r", kind="plan",
                             workflow_run=run["id"])
    store.add_attempt(plan["id"], cell="sonnet/high", max_turns=10, passed=1, input_tokens=5)
    store.add_event(plan["id"], "briefed", method_arm="premortem:with")
    built = store.create_task("build", status="done", source="locks", repo="r", kind="implement",
                              workflow_run=run["id"])
    store.add_attempt(built["id"], cell="sonnet/high", max_turns=10, passed=0, input_tokens=5)
    store.add_attempt(built["id"], cell="opus/high", max_turns=10, passed=1, input_tokens=5)
    store.create_task("still queued", status="queued", source="locks", repo="r", kind="review",
                      workflow_run=run["id"])
    unrun = store.create_task("no attempt", status="failed", source="locks", repo="r", kind="review",
                              workflow_run=run["id"])
    assert unrun["id"] > plan["id"]
    # A plan outside a run is judged by nothing after it.
    lone = store.create_task("lone plan", status="done", source="locks", repo="r", kind="plan")
    store.add_attempt(lone["id"], cell="sonnet/high", max_turns=10, passed=1, input_tokens=5)
    store.add_event(lone["id"], "briefed", method_arm="premortem:without")
    # A diagnosis's arm is no premortem's.
    other = store.create_task("bug", status="done", source="locks", repo="r", kind="debug-unclear")
    store.add_attempt(other["id"], cell="opus/high", max_turns=10, passed=1, input_tokens=5)
    store.add_event(other["id"], "briefed", method_arm="diagnosis:with")
    found = methods.trial(store, "r", premortem)
    assert found["with"] == {"tasks": 1, "tries": 1, "passed": 0, "rate": 0.0, "pass_p50_s": None}
    assert found["without"] == {"tasks": 1, "tries": 0, "passed": 0, "rate": None, "pass_p50_s": None}


def test_no_naming_starts_while_one_runs_for_the_session(store: Store, git_repo, tmp_path, monkeypatch):
    started = []
    store.touch_session("s", str(git_repo), "r")
    store.create_task("fix the cart", status="done", source="hook", session_id="s", repo="r")
    project.enroll(git_repo)
    root = tmp_path / "home"
    (root / "work").mkdir(parents=True)
    monkeypatch.setattr(dispatch, "held", lambda root, scope: scope == "name:s")
    on = {**os.environ, "LOCKS_NAMES": "on"}
    assert naming.maybe_start(store, "s", root, on, popen=lambda argv, **kw: started.append(argv)) is False
    assert started == []


def test_a_separator_with_no_command_before_it_makes_no_empty_command():
    assert allow.commands("; ls -la && && git status") == ["ls", "git status"]


def test_the_evidence_and_trunk_trials_short_here_take_every_repository_s_verdict(store: Store, monkeypatch):
    from locks import trunks

    full = {"tasks": evidence.TRIAL, "read": evidence.TRIAL, "blocked": 0, "passed_first": evidence.TRIAL,
            "blocked_rate": 0.0}
    empty = {"tasks": 0, "read": 0, "blocked": 0, "passed_first": 0, "blocked_rate": None}
    monkeypatch.setattr(evidence, "trial", lambda store, repo: (
        {"gate": dict(empty), "free": dict(empty)} if repo else
        {"gate": dict(full), "free": {**full, "blocked": 2, "blocked_rate": 0.25}}))
    side, why = evidence.arm(store, "r", 1)
    assert side == "gate" and "won its trial everywhere" in why and "pooled from every repository" in why
    fork = {"tasks": trunks.TRIAL, "passed_first": trunks.TRIAL, "calls_before_edit_p50": 3, "input_tokens_p50": 1,
            "duration_p50_s": 10}
    none = {"tasks": 0, "passed_first": 0, "calls_before_edit_p50": None, "input_tokens_p50": None,
            "duration_p50_s": None}
    monkeypatch.setattr(trunks, "trial", lambda store, repo: (
        {"fork": dict(none), "fresh": dict(none)} if repo else
        {"fork": dict(fork), "fresh": {**fork, "calls_before_edit_p50": 9, "duration_p50_s": 30}}))
    side, why = trunks.arm(store, "r", 1)
    assert side == "fork" and "won its trial everywhere" in why and "the trial here has 0 forked" in why
