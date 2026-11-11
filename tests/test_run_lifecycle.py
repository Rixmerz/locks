"""A run's lifecycle: it ends whatever fails on the way, and leaves no stray process or number behind."""
from __future__ import annotations

import signal
import subprocess
import time
import types
from pathlib import Path

import pytest

from locks import isolate, launch, orchestrate
from locks.escalate import Failure
from locks.matrix import Cell
from locks.orchestrate import Options, run

from .test_orchestrate import Script, kind, ok


def _finished(store, task_id):
    return store.last_event(task_id, "finished")


@pytest.mark.usefixtures("no_verifier")
def test_an_error_before_the_first_attempt_still_ends_the_task(git_repo, store, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("prepare blew up")

    monkeypatch.setattr(isolate, "prepare", boom)
    with pytest.raises(RuntimeError):
        run("add the export", git_repo, store, registry={}, launcher=Script(ok()), classifier=kind("implement"))
    rows = store._conn.execute("SELECT id, status FROM tasks").fetchall()
    assert rows and rows[0]["status"] != "running"
    assert _finished(store, rows[0]["id"]) is not None


@pytest.mark.usefixtures("no_verifier")
def test_a_failing_worktree_close_out_still_writes_the_final_status(git_repo, store, monkeypatch):
    def full_disk(*a, **kw):
        raise isolate.IsolationError("no space left on device")

    monkeypatch.setattr(isolate, "finish", full_disk)
    report = run("add the export", git_repo, store, registry={}, launcher=Script(ok(), write="app.py"),
                 classifier=kind("implement"))
    assert report.status == "blocked"
    assert report.stop is not None and report.stop.cause == "environment"
    assert "closing its worktree failed" in report.stop.reason and "no space left" in report.stop.reason
    assert store.get_task(report.task_id)["status"] == "blocked"
    assert _finished(store, report.task_id) is not None


@pytest.mark.usefixtures("no_verifier")
def test_an_attempt_is_kept_under_the_number_its_worker_ran_as(git_repo, store):
    task = store.create_task("add the export", status="queued", source="locks")
    # An earlier attempt started and its worker died: nothing was kept for number 1.
    store.add_event(task["id"], "attempt_started", seq=1, cell="sonnet/medium")
    script = Script(ok(), write="app.py")
    report = run("add the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
                 options=Options(), task_id=task["id"])
    assert report.status == "done"
    assert [r["seq"] for r in store.attempts(task["id"])] == [2]


def test_a_check_that_times_out_is_stopped_with_its_children(tmp_path: Path):
    marker = f"sleep {300 + int(time.time()) % 50}.{id(tmp_path) % 997}"
    began = time.monotonic()
    ran = orchestrate._run_check(f"{marker} & wait", tmp_path, timeout=1)
    assert ran == (None, "")
    assert time.monotonic() - began < 10
    time.sleep(0.3)
    alive = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    assert alive.stdout.strip() == ""


@pytest.mark.usefixtures("no_verifier")
def test_the_close_out_ignores_sigterm_and_the_handler_comes_back(git_repo, store, monkeypatch):
    seen = []
    real = isolate.finish

    def watching(*a, **kw):
        seen.append(signal.getsignal(signal.SIGTERM))
        return real(*a, **kw)

    before = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(isolate, "finish", watching)
    run("add the export", git_repo, store, registry={}, launcher=Script(ok(), write="app.py"),
        classifier=kind("implement"))
    assert seen and all(h == signal.SIG_IGN for h in seen)
    assert signal.getsignal(signal.SIGTERM) == before


def test_launch_run_uses_a_stoppable_runner_by_default(tmp_path: Path, monkeypatch):
    made = []

    class Fake(launch.Stoppable):
        def __init__(self):
            super().__init__()
            made.append(self)

        def __call__(self, argv, **kw):
            return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(launch, "Stoppable", Fake)
    spec = launch.LaunchSpec(prompt="do it", cell=Cell("sonnet", "high"), launch_dir=tmp_path)
    launch.run(spec)
    launch.run(spec)
    assert len(made) == 2 and made[0] is not made[1]


def test_a_raced_try_is_kept_under_the_number_its_worker_ran_as(store):
    task = store.create_task("add the export", status="queued", source="locks")
    other = launch.WorkerResult(False, Failure.CODE_BUG, "no", cost_usd=0.1)
    raced = orchestrate.Raced(other, Cell("opus", "high"), None, other, Cell("sonnet", "high"), False)
    report = types.SimpleNamespace(cells=[], served=[], cost_usd=0.0)
    # Attempt 1 died before it was kept; the race's other try ran as 2.
    orchestrate._record_raced(store, task["id"], raced, 10, [], report, first=False, ran_as=2)
    assert [r["seq"] for r in store.attempts(task["id"])] == [2]
