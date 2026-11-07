"""A plan's parts read blind before they are built, as the run drives it: the arm drawn on the brief,
the plan that goes back once to its own cell and session, and the cases that launch no reader."""
from __future__ import annotations

import pytest

from locks import critique
from locks.launch import WorkerResult
from locks.matrix import Cell
from locks.orchestrate import Options, run
from locks.store import Store

from .conftest import git
from .test_orchestrate import Script, kind, ok

CONTRACT = "Every part calls `export.write_rows(rows, out)` and returns the row count as an int."
TWO = (
    {"id": "writer", "prompt": "Build `export.write_rows` (S1).", "surfaces": ["src/export/**"]},
    {"id": "cli", "prompt": "Wire `locks export` to it and print the count.", "surfaces": ["src/cli.py"]},
)
ONE = (TWO[0],)
FIXED = (
    {**TWO[0], "prompt": "Build `export.write_rows(rows, out)` writing RFC 4180 CSV to `src/export/csv.py` (S1)."},
    {**TWO[1], "prompt": "Wire `locks export` in `src/cli.py` to `export.write_rows`; print the count as `N rows`."},
)
LEFT_OPEN = {"where": "cli", "what": "the output line format is left to its worker", "evidence_class": "inferential",
             "cause": "unknown", "proof": ""}


def plan(parts=TWO, *, cost: float = 0.1, session: str = "plan-1") -> WorkerResult:
    return WorkerResult(True, None, "planned", "read the layout", cost_usd=cost, session_id=session,
                        parts=tuple(parts), contract=CONTRACT)


def reader(*found: dict, cost: float = 0.02) -> WorkerResult:
    return WorkerResult(True, None, "read", "read the parts", cost_usd=cost, verdict="pass" if not found else "fail",
                        findings=found)


@pytest.fixture
def reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """The setting on, and the trial's arm fixed to the reader: what a run does with it."""
    monkeypatch.setenv("LOCKS_CRITIQUE", "on")
    monkeypatch.setattr(critique, "arm_for", lambda store, repo, task_id: ("with", "with for the test"))


def options(git_repo, **changes) -> Options:
    """A plan step of a workflow reads the commit before it in a worktree of its own, where its session is kept."""
    head = git(git_repo, "rev-parse", "HEAD").strip()
    return Options(parts=True, base=head, **changes)


def critiqued(store: Store, task_id: int) -> list[dict]:
    return [e["data"] for e in store.events_of(task_id, "critiqued")]


@pytest.mark.usefixtures("reading", "no_verifier")
def test_a_plan_with_a_part_that_leaves_a_decision_goes_back_once_and_its_second_answer_stands(git_repo, store: Store):
    script = Script(plan(), reader(LEFT_OPEN), plan(FIXED, session="plan-2"), reader(LEFT_OPEN, cost=0.03))
    report = run("Plan the export command", git_repo, store, options(git_repo), registry={}, launcher=script,
                 classifier=kind("plan"))
    assert report.status == "done" and report.parts == FIXED
    first, read_first, second, read_second = script.specs
    assert second.cell == first.cell and read_first.cell == read_second.cell == critique.CELL
    assert second.resume_session == "plan-1" and second.resume_prompt == critique.PROMPT
    assert second.resume_note.startswith("Why your last answer was held back: critique: cli: the output line format")
    assert not second.fork_session and second.max_turns == first.max_turns
    # The reader is given the plan's parts as they stand now, the second time its rewritten ones.
    assert "Wire `locks export` to it and print the count." in read_first.prompt
    assert "RFC 4180 CSV" in read_second.prompt and "RFC 4180 CSV" not in read_first.prompt
    seen = critiqued(store, report.task_id)
    assert [e["round"] for e in seen] == [1, 2] and [e["held"] for e in seen] == [True, False]
    assert all(e["findings"][0]["where"] == "cli" for e in seen)
    assert [e["seq"] for e in seen] == [1, 2] and all(e["cell"] == "sonnet/low" for e in seen)
    # Never a climb: the same cell answered twice, the first held back as an answer of the wrong form.
    attempts = store.attempts(report.task_id)
    assert [a["cell"] for a in attempts] == [first.cell.label] * 2 and [a["passed"] for a in attempts] == [0, 1]
    assert attempts[0]["failure"] == "inconclusive" and attempts[0]["summary"].startswith("critique: cli:")
    moved = store.last_event(report.task_id, "moved")["data"]
    assert moved["axis"] == "retry" and moved["continued"] is True and moved["resumed"] == "plan-1"
    assert not store.events_of(report.task_id, "spec_owed")


@pytest.mark.usefixtures("reading", "no_verifier")
def test_a_plan_the_reader_finds_clean_passes_on_its_first_answer(git_repo, store: Store):
    script = Script(plan(), reader())
    report = run("Plan the export command", git_repo, store, options(git_repo), registry={}, launcher=script,
                 classifier=kind("plan"))
    assert report.status == "done" and len(script.specs) == 2
    (seen,) = critiqued(store, report.task_id)
    assert seen["round"] == 1 and seen["held"] is False and seen["findings"] == [] and seen["verdict"] == "pass"
    assert len(store.attempts(report.task_id)) == 1


@pytest.mark.usefixtures("reading", "no_verifier")
def test_a_second_answer_the_reader_still_faults_is_not_held_again(git_repo, store: Store):
    # Round 2 stands whatever the reader says: not a failed task, not a decision for the orchestrator.
    script = Script(plan(), reader(LEFT_OPEN), plan(TWO, session="plan-2"), reader(LEFT_OPEN))
    report = run("Plan the export command", git_repo, store, options(git_repo), registry={}, launcher=script,
                 classifier=kind("plan"))
    assert report.status == "done" and report.stop is None and len(script.specs) == 4
    assert [e["held"] for e in critiqued(store, report.task_id)] == [True, False]


@pytest.mark.usefixtures("reading", "no_verifier")
def test_a_plan_of_one_part_launches_no_critique(git_repo, store: Store):
    script = Script(plan(ONE))
    report = run("Plan the export command", git_repo, store, options(git_repo), registry={}, launcher=script,
                 classifier=kind("plan"))
    assert report.status == "done" and len(script.specs) == 1
    assert critiqued(store, report.task_id) == []
    assert "critique_cost_usd" not in store.last_event(report.task_id, "attempt_finished")["data"]


@pytest.mark.usefixtures("reading", "no_verifier")
def test_a_pinned_task_launches_no_critique(git_repo, store: Store):
    script = Script(plan())
    report = run("Plan the export command", git_repo, store, options(git_repo, start=Cell("sonnet", "high")),
                 registry={}, launcher=script, classifier=kind("plan"))
    assert report.status == "done" and len(script.specs) == 1
    assert critiqued(store, report.task_id) == []
    briefed = store.events_of(report.task_id, "briefed")[0]["data"]
    assert "critique_arm" not in briefed and "critique_why" not in briefed


@pytest.mark.usefixtures("no_verifier")
def test_with_the_setting_off_nothing_runs_and_the_brief_names_no_arm(git_repo, store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_CRITIQUE", "off")
    drawn = []
    monkeypatch.setattr(critique, "arm_for", lambda *a: drawn.append(a) or ("with", "x"))
    script = Script(plan())
    report = run("Plan the export command", git_repo, store, options(git_repo), registry={}, launcher=script,
                 classifier=kind("plan"))
    assert report.status == "done" and len(script.specs) == 1 and drawn == []
    assert critiqued(store, report.task_id) == []
    assert "critique_arm" not in store.events_of(report.task_id, "briefed")[0]["data"]


@pytest.mark.usefixtures("no_verifier")
def test_the_arm_without_the_reader_is_kept_on_the_brief_and_runs_nothing(git_repo, store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_CRITIQUE", "on")
    monkeypatch.setattr(critique, "arm_for", lambda store, repo, task_id: ("without", "the other arm for the test"))
    script = Script(plan())
    report = run("Plan the export command", git_repo, store, options(git_repo), registry={}, launcher=script,
                 classifier=kind("plan"))
    assert report.status == "done" and len(script.specs) == 1 and critiqued(store, report.task_id) == []
    briefed = store.events_of(report.task_id, "briefed")[0]["data"]
    assert briefed["critique_arm"] == "without" and briefed["critique_why"] == "the other arm for the test"


@pytest.mark.usefixtures("reading", "no_verifier")
def test_a_step_that_is_not_a_plan_of_parts_draws_no_arm(git_repo, store: Store):
    report = run("Fix the export", git_repo, store, registry={}, launcher=Script(ok(), write="half.py"),
                 classifier=kind("implement"))
    assert report.status == "done"
    assert "critique_arm" not in store.events_of(report.task_id, "briefed")[0]["data"]


@pytest.mark.usefixtures("reading", "no_verifier")
def test_the_critique_s_cost_is_the_task_s_and_kept_apart_on_each_attempt(git_repo, store: Store):
    script = Script(plan(cost=0.10), reader(LEFT_OPEN, cost=0.02), plan(FIXED, cost=0.20, session="plan-2"),
                    reader(cost=0.03))
    report = run("Plan the export command", git_repo, store, options(git_repo), registry={}, launcher=script,
                 classifier=kind("plan"))
    assert report.status == "done"
    first, second = [e["data"] for e in store.events_of(report.task_id, "attempt_finished")]
    assert first["critique_cost_usd"] == 0.02 and second["critique_cost_usd"] == 0.03
    assert first["cost_usd"] == pytest.approx(0.12) and second["cost_usd"] == pytest.approx(0.23)
    assert first["worker_cost_usd"] == 0.1 and second["worker_cost_usd"] == 0.2
    assert first["verifier_cost_usd"] == 0 and second["verifier_cost_usd"] == 0
    assert [a["cost_usd"] for a in store.attempts(report.task_id)] == [pytest.approx(0.12), pytest.approx(0.23)]
    assert report.cost_usd == pytest.approx(0.35)
    assert [e["cost_usd"] for e in critiqued(store, report.task_id)] == [0.02, 0.03]
    assert store.last_event(report.task_id, "finished")["data"]["cost_usd"] == pytest.approx(0.35)


def test_a_resume_keeps_the_arm_its_first_brief_drew(store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_CRITIQUE", "on")
    task = store.create_task("Plan the export command", status="running", source="locks")
    store.add_event(task["id"], "briefed", critique_arm="without")
    assert critique.arm_for(store, "r", task["id"]) == ("without", "kept from its first brief")


def test_a_held_plan_s_reason_reaches_a_fresh_brief_through_its_summary(git_repo, store: Store, monkeypatch):
    # No worktree, so no session is kept: the next attempt starts fresh and reads why from the attempt.
    monkeypatch.setenv("LOCKS_CRITIQUE", "on")
    monkeypatch.setenv("LOCKS_VERIFIER", "off")
    monkeypatch.setattr(critique, "arm_for", lambda store, repo, task_id: ("with", "with for the test"))
    script = Script(plan(), reader(LEFT_OPEN), plan(FIXED, session="plan-2"), reader())
    report = run("Plan the export command", git_repo, store, Options(parts=True), registry={}, launcher=script,
                 classifier=kind("plan"))
    assert report.status == "done"
    first, _, second, _ = script.specs
    assert second.resume_session == "" and second.cell == first.cell
    assert "critique: cli: the output line format is left to its worker" in second.prompt
