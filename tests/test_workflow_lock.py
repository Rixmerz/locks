"""`workflows.advance` holds SQLite's one write lock while it queues what a passed step released:
every hook, dispatcher and the UI wait on it, so no git command may run in that time."""
from __future__ import annotations

import subprocess

import pytest

from locks import flow, isolate, workflows
from locks.launch import WorkerResult
from locks.orchestrate import run
from locks.store import Store

from .conftest import git
from .test_orchestrate import kind, ok
from .test_workflows import CHAIN, TWO, _runner, _save


@pytest.fixture(autouse=True)
def _no_critique(monkeypatch):
    monkeypatch.setenv("LOCKS_CRITIQUE", "off")


@pytest.fixture
def git_calls(store: Store, monkeypatch):
    """Every git command a test runs, with whether the store's connection was inside a transaction."""
    seen: list[tuple[str, bool]] = []
    real = subprocess.run

    def run(args, *a, **kw):
        if isinstance(args, list) and args and args[0] == "git":
            seen.append((" ".join(args[:3]), store._conn.in_transaction))
        return real(args, *a, **kw)

    monkeypatch.setattr(isolate.subprocess, "run", run)
    return seen


def _chain(git_repo, store: Store, results) -> tuple[int, list[dict]]:
    _save(CHAIN, git_repo)
    started = workflows.start(store, "chain", git_repo, {"goal": "the export"})
    seen: list[dict] = []
    flow.work(store, runner=_runner(results, seen))
    return started["id"], seen


def test_no_git_command_runs_while_advance_holds_the_write_transaction(git_repo, store: Store, kicks, git_calls):
    # The review step starts from the build's kept tip and reads its diff (numstat, generated files);
    # the polish step starts from the same tip.
    run_id, seen = _chain(git_repo, store, [ok("built it"), ok("no problems"), ok("polished")])
    assert len(seen) == 3 and workflows.status(store, run_id)["state"] == "done"
    assert any(call.startswith("git diff --numstat") for call, _ in git_calls)
    assert any(call.startswith("git check-attr") for call, _ in git_calls)
    assert [call for call, inside in git_calls if inside] == []


def test_a_branch_gone_while_a_step_builds_on_it_is_put_back_outside_the_transaction(
        git_repo, store: Store, kicks, git_calls, monkeypatch):
    _save(TWO, git_repo)
    started = workflows.start(store, "two", git_repo, {"goal": "the export"})
    real = workflows.advance

    def advance(st, task, env=None, kick=None):
        # Something deletes the build's branch before `advance` queues the next step.
        if task and task.get("workflow_step") == "build" and task["status"] == "done":
            git(git_repo, "branch", "-D", f"locks/task-{task['id']}")
        return real(st, task, env, kick)

    monkeypatch.setattr(workflows, "advance", advance)
    flow.work(store, runner=_runner([ok("built it"), ok("polished")], []))
    build, polish = store.run_tasks(started["id"])
    assert polish["status"] == "done"
    assert store.last_event(build["id"], "branch_restored")["data"]["branch"] == f"locks/task-{build['id']}"
    assert any(call.startswith("git branch locks/task-") for call, _ in git_calls)
    assert [call for call, inside in git_calls if inside] == []


def test_a_step_the_transaction_finds_unread_is_read_outside_it_and_tried_again(
        git_repo, store: Store, kicks, git_calls, monkeypatch):
    real = workflows._read_ahead
    first: list[bool] = []

    def stale(st, run, reads):
        # Another process advanced the run since: nothing was read for the steps now ready.
        if not reads.memo:
            first.append(True)
            return None
        return real(st, run, reads)

    monkeypatch.setattr(workflows, "_read_ahead", stale)
    run_id, seen = _chain(git_repo, store, [ok("built it"), ok("no problems"), ok("polished")])
    assert first and len(seen) == 3 and workflows.status(store, run_id)["state"] == "done"
    assert [call for call, inside in git_calls if inside] == []


def test_advance_gives_up_after_the_retries_and_queues_nothing(git_repo, store: Store, kicks, git_calls,
                                                                 monkeypatch):
    reads_ahead: list[int] = []
    monkeypatch.setattr(workflows, "_read_ahead", lambda st, run, reads: reads_ahead.append(1))
    monkeypatch.setattr(workflows.Reads, "fill", lambda self, key, thunk: None)
    _save(CHAIN, git_repo)
    started = workflows.start(store, "chain", git_repo, {"goal": "the export"})
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built it")], seen))
    tasks = store.run_tasks(started["id"])
    assert len(seen) == 1 and len(tasks) == 1
    assert len(reads_ahead) == workflows.READ_RETRIES + 1
    error = store.events_of(tasks[0]["id"], "workflow_error")
    assert error and "kept changing" in error[0]["data"]["error"]
    assert [call for call, inside in git_calls if inside] == []


def test_a_second_fix_round_is_queued_with_git_read_before_the_transaction(git_repo, store: Store, kicks,
                                                                           git_calls):
    """`_next_round`: a recheck that leaves a row open queues its fix again from the tip the round before kept."""
    found = ({"where": "src/chart.py:3", "what": "the animation has no safety timeout",
              "evidence_class": "deterministic", "cause": "introduced", "proof": "grep -n setTimeout src/chart.py"},)
    fixed = WorkerResult(True, None, "documented it as known debt", "wrote it down",
                         verdicts=({"id": "F1", "verdict": "fixed", "proof": "the note is in CLAUDE.md"},))
    results = [ok("built"), WorkerResult(True, None, "one problem", "read it", findings=found),
               ok("wrote it down"), fixed, ok("wrote it down again"), fixed]

    def launch(spec):
        if "Ledger (fix" in spec.prompt:
            notes = spec.target_dir / "CLAUDE.md"
            notes.write_text((notes.read_text() if notes.exists() else "") + "round\n")
        elif "Edit" not in spec.disallowed_tools:
            (spec.target_dir / "src").mkdir(exist_ok=True)
            (spec.target_dir / "src" / "chart.py").write_text("x = 1\n")
        return results.pop(0)

    def runner(text, where, st, options, **kw):
        return run(text, where, st, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    started = workflows.start(store, "feature", git_repo, {"goal": "the chart"})
    flow.work(store, runner=runner)
    steps = [t["workflow_step"] for t in store.run_tasks(started["id"])]
    assert steps.count("fix") == 2
    rounds = [e for t in store.run_tasks(started["id"]) for e in store.events_of(t["id"], "workflow_advanced")
              if e["data"].get("round")]
    assert len(rounds) == 1
    assert [call for call, inside in git_calls if inside] == []


SKIPPED = {"name": "skipped", "inputs": ["goal"], "steps": [
    {"id": "build", "prompt": "Build {goal}", "kind": "implement"},
    {"id": "fix", "prompt": "Fix what the review found in {goal}", "kind": "implement",
     "when": {"build": "findings"}},
    {"id": "again", "prompt": "Fix {goal} again", "kind": "implement", "needs": ["fix", "build"],
     "when": {"build": "findings"}},
    {"id": "check", "prompt": "Review, without changing any file, {goal}", "kind": "review-routine",
     "needs": ["again"]},
]}


def test_steps_made_ready_by_skipped_ones_may_need_more_unread_calls_than_the_retries(
        git_repo, store: Store, kicks, git_calls):
    """Two steps skipped in one pass make the review ready inside the transaction: its calls (the tip it starts
    from, the diff, the generated files, the links) are more than `READ_RETRIES`, and each is progress."""
    _save(SKIPPED, git_repo)
    started = workflows.start(store, "skipped", git_repo, {"goal": "the export"})
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built it"), ok("no problems")], seen))
    assert len(seen) == 2 and workflows.status(store, started["id"])["state"] == "done"
    assert not store.events_of(store.run_tasks(started["id"])[0]["id"], "workflow_error")
    assert [call for call, inside in git_calls if inside] == []


def test_a_step_that_will_be_skipped_restores_no_branch(git_repo, store: Store, kicks, git_calls, monkeypatch):
    skipped = {"name": "two", "inputs": ["goal"], "steps": [
        {"id": "build", "prompt": "Build {goal}", "kind": "implement"},
        {"id": "polish", "prompt": "Polish {goal}", "kind": "implement", "when": {"build": "findings"}}]}
    _save(skipped, git_repo)
    started = workflows.start(store, "two", git_repo, {"goal": "the export"})
    real = workflows.advance

    def advance(st, task, env=None, kick=None):
        if task and task.get("workflow_step") == "build" and task["status"] == "done":
            git(git_repo, "branch", "-D", f"locks/task-{task['id']}")
        return real(st, task, env, kick)

    monkeypatch.setattr(workflows, "advance", advance)
    flow.work(store, runner=_runner([ok("built it")], []))
    build = store.run_tasks(started["id"])[0]
    assert workflows.status(store, started["id"])["state"] == "done"
    assert not store.events_of(build["id"], "branch_restored")
    assert not any(call.startswith("git branch locks/task-") for call, _ in git_calls)
