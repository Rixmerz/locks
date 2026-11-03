from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from locks import __version__, ci, launch, orchestrate, roles
from locks import capabilities as caps
from locks.classify import Classification
from locks.escalate import MAX_TURNS_CEILING, Failure
from locks.launch import WRITE_TOOLS, WorkerResult
from locks.matrix import LADDERS, Cell, turns_for
from locks.orchestrate import Options, choose_start, run

from .conftest import git

IMPLEMENT = LADDERS["implement"]


class Script:
    """A launcher that plays back results and records every spec it was given."""

    def __init__(self, *results: WorkerResult, write: str | None = None):
        self.results = list(results)
        self.specs = []
        self.write = write

    def __call__(self, spec):
        self.specs.append(spec)
        if self.write:
            (spec.target_dir / self.write).write_text(f"attempt {len(self.specs)}\n")
        return self.results.pop(0)


def ok(summary="done", cost=0.1) -> WorkerResult:
    return WorkerResult(True, None, summary, evidence="1 passed", cost_usd=cost)


def bad(failure: Failure, summary="still broken", cost=0.1) -> WorkerResult:
    return WorkerResult(False, failure, summary, cost_usd=cost)


def kind(name: str, complexity: str = "medium"):
    return lambda text: Classification(name, complexity, "rule", "test")


def test_choose_start_rises_with_complexity_and_history_never_falls():
    assert choose_start(IMPLEMENT, "medium", [], [])[0] == Cell("sonnet", "medium")
    assert choose_start(IMPLEMENT, "high", [], [])[0] == Cell("sonnet", "high")
    # A writing kind classified high starts where its ladder starts: only climbs raise it.
    assert choose_start(IMPLEMENT, "high", [], [], writes=True)[0] == Cell("sonnet", "medium")
    assert choose_start(IMPLEMENT, "high", ["opus/high"] * 3, [], writes=True)[0] == Cell("opus", "high")
    # A plan starts at sonnet/high (`bench/speed/plans.py`), and a high one on opus/high.
    assert choose_start(LADDERS["plan"], "medium", [], [], kind="plan")[0] == Cell("sonnet", "high")
    assert choose_start(LADDERS["plan"], "high", [], [], kind="plan")[0] == Cell("opus", "high")
    assert choose_start(LADDERS["review-critical"], "high", [], [], kind="review-critical")[0] \
        == LADDERS["review-critical"][1]
    start, reasons = choose_start(IMPLEMENT, "medium", ["sonnet/xhigh", "sonnet/xhigh", "opus/high"], [])
    assert start == Cell("sonnet", "xhigh") and "this repository" in reasons[-1]
    start, reasons = choose_start(IMPLEMENT, "medium", ["sonnet/xhigh"], ["opus/high"] * 5)
    assert start == Cell("opus", "high") and "all repositories" in reasons[-1]
    assert choose_start(IMPLEMENT, "high", ["sonnet/medium"] * 4, [])[0] == Cell("sonnet", "high")
    # a landing off the ladder counts at the highest rung it is not below
    assert choose_start(IMPLEMENT, "medium", ["opus/max"] * 3, [])[0] == Cell("opus", "high")
    assert choose_start(IMPLEMENT, "medium", ["haiku"] * 3, [])[0] == Cell("sonnet", "medium")
    # a task that passed where it started ("") is no reason to start higher: only climbs raise it
    assert choose_start(IMPLEMENT, "medium", ["", "", "opus/high"], [])[0] == Cell("sonnet", "medium")
    assert choose_start(IMPLEMENT, "medium", ["", "opus/high", "opus/high"], [])[0] == Cell("opus", "high")


def test_the_model_that_served_is_recorded_and_a_pin_reaches_the_worker(git_repo, store, monkeypatch):
    from locks import models

    served = WorkerResult(True, None, "done", "1 passed", served_model="claude-sonnet-5")
    report = run("x", git_repo, store, registry={}, launcher=Script(served), classifier=kind("implement"))
    assert report.served == ["claude-sonnet-5"] and "attempt 1 sonnet/medium [claude-sonnet-5]" in report.text()
    assert store.attempts(report.task_id)[0]["served_model"] == "claude-sonnet-5"
    assert store.last_event(report.task_id, "attempt_finished")["data"]["served_model"] == "claude-sonnet-5"
    # the person's sessions run a newer sonnet: the plan says so, with the pin
    store.add_usage(message_id="m1", session_id="s", model="claude-sonnet-5-5", output_tokens=1, ts="2099-01-01")
    dry = run("y", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"))
    assert any("workers last ran claude-sonnet-5 for `sonnet` while your sessions use claude-sonnet-5-5" in r
               and "locks config model sonnet claude-sonnet-5-5" in r for r in dry.plan.reasons)
    models.pin("sonnet", "claude-sonnet-5-5")
    script = Script(ok())
    run("z", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert script.specs[0].model_id == "claude-sonnet-5-5"
    dry = run("w", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"))
    assert not any("workers last ran" in r for r in dry.plan.reasons)


@pytest.mark.usefixtures("no_verifier")
def test_a_first_try_pass_leaves_a_branch(git_repo, store):
    script = Script(ok(), write="feature.py")
    report = run("add the feature", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and report.final_cell == "sonnet/medium"
    assert report.branch == f"locks/task-{report.task_id}"
    assert "feature.py" in git(git_repo, "show", "--name-only", report.branch)
    assert not (git_repo / "feature.py").exists()
    spec = script.specs[0]
    assert spec.launch_dir != git_repo and spec.workdir is None
    assert spec.disallowed_tools == () and "--max-budget-usd" not in launch.build_argv(spec)
    task = store.get_task(report.task_id)
    assert task["status"] == "done" and task["final_cell"] == "sonnet/medium"
    assert store.messages(report.task_id)[-1]["role"] == "worker"
    assert "passed at sonnet/medium" in report.text()


@pytest.mark.usefixtures("no_verifier")
def test_shallow_failures_climb_effort_and_keep_the_work(git_repo, store):
    seen = []

    def launcher(spec):
        target = spec.target_dir / "f.py"
        seen.append(target.read_text() if target.exists() else None)
        target.write_text(f"attempt {len(seen)}\n")
        return bad(Failure.CODE_BUG, "missed the empty case") if len(seen) == 1 else ok("fixed")

    script = Script(bad(Failure.CODE_BUG, "missed the empty case"), ok("fixed"))
    report = run("fix the parser", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    assert report.cells == ["sonnet/medium", "sonnet/high"]
    assert report.moves[0].startswith("more_effort to sonnet/high: ")
    moved = store.last_event(report.task_id, "moved")["data"]
    assert moved["seq"] == 1 and moved["from_cell"] == "sonnet/medium" and moved["to_cell"] == "sonnet/high"
    assert moved["axis"] == "effort" and moved["trigger"] == "code_bug" and moved["turns_to"] == turns_for("implement")
    assert moved["because"][-1].startswith("next: sonnet/high") and moved["spent_usd"] == pytest.approx(0.1)
    assert "budget_left" not in moved
    assert "    · next: sonnet/high (the floor for work that needs judgment)" in report.text()
    assert seen == [None, "attempt 1\n"]  # the more thorough attempt continues the work
    run("fix the parser", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    second = script.specs[1]
    assert "missed the empty case" in second.prompt and "already tried" in second.prompt
    # the failure and the fix that worked are now memory
    dead = store.dead_ends("fix the parser")
    assert dead and dead[0]["worked_instead"].startswith("sonnet/high")


def test_a_wrong_approach_resets_the_worktree_and_changes_model(git_repo, store):
    seen = []

    def launcher(spec):
        seen.append(sorted(p.name for p in spec.target_dir.iterdir() if p.name != ".git"))
        (spec.target_dir / "wrong.py").write_text("x\n")
        return bad(Failure.APPROACH, "rewrote the wrong module") if len(seen) == 1 else ok()

    report = run("refactor auth flow", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    assert report.cells == ["sonnet/medium", "opus/high"]
    assert seen[1] == ["app.py"]  # the second model started from the base commit


def test_read_only_work_gets_no_edit_tools_and_no_worktree(git_repo, store):
    script = Script(ok("found it"))
    report = run("where is the router defined", git_repo, store, registry={}, launcher=script,
                 classifier=kind("explore"))
    spec = script.specs[0]
    assert spec.disallowed_tools == WRITE_TOOLS
    assert spec.launch_dir == git_repo.resolve()
    assert "read-only" in spec.prompt
    assert report.branch is None and report.final_cell == "haiku/medium"


def test_a_capability_joins_the_work_that_touches_its_files(git_repo, store):
    registry = {"measure": caps.Capability("measure", {"command": "m"}, when=(), touches=caps.UI_FILES,
                                           measures=True, hint="measure it"),
                "drive": caps.Capability("drive", {"command": "d"}, when=(), touches=caps.UI_FILES, hint="drive it")}
    # Named in the request: from the first attempt.
    script = Script(ok())
    run("restyle components/Chart.tsx", git_repo, store, registry=registry, launcher=script,
        classifier=kind("feature"))
    assert set(script.specs[0].mcp_servers) == {"measure", "drive"}
    # A review of a diff that touches it (a workflow step's `touches`): only what measures.
    script = Script(ok("reads fine"))
    run("review the change", git_repo, store, Options(touches=("assets/app.css",)), registry=registry,
        launcher=script, classifier=kind("review-routine"))
    assert set(script.specs[0].mcp_servers) == {"measure"}
    # What the work changed so far counts on the next attempt.
    script = Script(bad(Failure.CODE_BUG), ok(), write="theme.css")
    run("fix the colours", git_repo, store, registry=registry, launcher=script, classifier=kind("implement"))
    first, second = script.specs
    assert first.mcp_servers == {} and set(second.mcp_servers) == {"measure", "drive"}


def test_capabilities_are_handed_over_and_reactive_ones_join_after_a_failure(git_repo, store):
    registry = {
        "index": caps.Capability("index", {"command": "idx", "args": ["{workdir}"]}, hint="use it"),
        "inspector": caps.Capability("inspector", {"command": "li"}, when=(), after_failure=("implement",)),
    }
    script = Script(bad(Failure.CODE_BUG), ok())
    run("polish it", git_repo, store, registry=registry, launcher=script, classifier=kind("implement"))
    first, second = script.specs
    assert set(first.mcp_servers) == {"index"} and set(second.mcp_servers) == {"index", "inspector"}
    assert first.mcp_servers["index"]["args"] == [str(first.target_dir)]
    assert "index: use it" in first.append_system_prompt


def test_a_claimed_pass_the_check_rejects_is_a_failure(git_repo, store):
    prompts = []

    def launcher(spec):
        prompts.append(spec.prompt)
        if len(prompts) == 2:
            (spec.target_dir / "proof.txt").write_text("ok\n")
        return ok("all good")

    report = run("make it pass", git_repo, store, Options(verify="test -f proof.txt"), registry={},
                 launcher=launcher, classifier=kind("implement"))
    assert report.status == "done"
    assert report.cells == ["sonnet/medium", "sonnet/high"]
    attempt = store.attempts(report.task_id)[0]
    assert attempt["failure"] == "code_bug" and "exited 1" in attempt["summary"]
    assert "checked by running: test -f proof.txt" in prompts[0]


def test_files_the_check_itself_writes_are_said_and_marked_on_the_commit(git_repo, store):
    script = Script(ok(), write="src.py")
    report = run("build it", git_repo, store, Options(verify="mkdir -p out && date +%N > out/bundle.js"),
                 registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and report.check_output == ["out/bundle.js"]
    assert "written by the check itself (1, committed as its output" in report.text()
    assert store.last_event(report.task_id, "check_rewrote")["data"]["files"] == ["out/bundle.js"]
    assert "Locks-Check-Output: 1 file" in git(git_repo, "log", "-1", "--format=%B", report.branch)
    # a check that writes nothing says nothing
    quiet = run("build it again", git_repo, store, Options(verify="true"), registry={},
                launcher=Script(ok(), write="other.py"), classifier=kind("implement"))
    assert quiet.check_output == [] and "written by the check" not in quiet.text()


def test_a_check_that_passes_keeps_the_pass(git_repo, store):
    script = Script(ok())
    report = run("x", git_repo, store, Options(verify="true"), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "done"


@pytest.mark.parametrize(
    ("results", "status"),
    [
        ([bad(Failure.SPEC_BUG, "the API has no such field")], "replan"),
        ([bad(Failure.ENVIRONMENT), bad(Failure.ENVIRONMENT)], "blocked"),
        ([bad(Failure.CODE_BUG, s) for s in ("missed a", "broke b", "left c", "forgot d")], "failed"),
        ([bad(Failure.CODE_BUG, "still broken")] * 3, "replan"),  # every model says the same
    ],
)
def test_runs_end_with_a_status_that_says_why(git_repo, store, results, status):
    report = run("do the thing", git_repo, store, registry={}, launcher=Script(*results),
                 classifier=kind("implement"))
    assert report.status == status
    assert store.get_task(report.task_id)["status"] == status
    assert report.branch is None
    assert results[-1].summary in report.summary or report.summary.endswith("attempts without a pass")


def test_fable_waits_for_approval(tmp_path, store):
    script = Script(bad(Failure.INCONCLUSIVE, "could not read the queue"),
                    bad(Failure.INCONCLUSIVE, "the queue is still unclear"),
                    bad(Failure.INCONCLUSIVE, "the scheduler is unclear"),
                    bad(Failure.INCONCLUSIVE, "the workers are unclear"))
    report = run("design the system", tmp_path, store, registry={}, launcher=script, classifier=kind("plan"))
    assert report.status == "needs_approval"
    assert report.cells == ["sonnet/high", "opus/high", "opus/xhigh", "opus/max"]


def test_cost_is_measured_never_a_brake_and_attempts_are_the_ceiling(git_repo, store):
    # What the old $5 default would have stopped goes on: cost is shown, never a limit.
    script = Script(bad(Failure.CODE_BUG, "missed a", cost=3.0), bad(Failure.CODE_BUG, "missed b", cost=3.0),
                    ok(cost=0.5))
    report = run("x", git_repo, store, Options(), registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and report.cost_usd == pytest.approx(6.5) and "cost $6.50" in report.text()
    assert all("--max-budget-usd" not in launch.build_argv(s) for s in script.specs)
    assert "budget_usd" not in store.last_event(report.task_id, "attempt_started")["data"]
    assert "budget_usd" not in Options().stored()
    script = Script(bad(Failure.CODE_BUG, "a"), bad(Failure.CODE_BUG, "b"))
    report = run("y", git_repo, store, Options(max_attempts=2), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "failed" and "2 attempts" in report.summary


def test_turns_asked_for_never_pass_the_ceiling(git_repo, store):
    script = Script(ok())
    report = run("x", git_repo, store, Options(max_turns=5000), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert script.specs[0].max_turns == MAX_TURNS_CEILING
    assert any("5000 turns were asked for" in r for r in report.plan.reasons)


@pytest.mark.usefixtures("no_verifier")
def test_history_moves_the_next_start(git_repo, store):
    for _ in range(3):
        script = Script(bad(Failure.CODE_BUG, "a"), bad(Failure.CODE_BUG, "b"), ok(), write="f.py")
        run("tweak the layout", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    script = Script(ok(), write="f.py")
    report = run("tweak the layout again", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.cells == ["sonnet/xhigh"]
    assert any("had to climb to sonnet/xhigh" in r for r in report.plan.reasons)


def test_dead_ends_from_another_repository_reach_the_brief(git_repo, store):
    p = store.open_problem("websocket reconnect loop", repo="github.com/x/other")
    store.add_fix(p, "raise the backoff", "failed", repo="github.com/x/other", why="the loop is in the client")
    script = Script(ok())
    run("the websocket reconnect loop is back", git_repo, store, registry={}, launcher=script,
        classifier=kind("implement"))
    assert "raise the backoff" in script.specs[0].prompt and "github.com/x/other" in script.specs[0].prompt
    task_id = store.list_tasks()[0]["id"]
    assert store.last_event(task_id, "dead_ends")["data"]["shown"][0]["tried"] == "raise the backoff"


def test_options_pin_and_launch_dir_and_dry_run(git_repo, store, tmp_path):
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    script = Script(ok())
    report = run("x", git_repo, store, Options(start=Cell("opus", "low"), launch_dir=launch_dir, isolate=False),
                 registry={}, launcher=script, classifier=kind("implement"))
    spec = script.specs[0]
    assert spec.cell == Cell("opus", "low") and spec.launch_dir == launch_dir.resolve()
    assert spec.workdir == git_repo.resolve()
    assert report.plan.reasons == ["pinned by the caller"]
    dry = run("y", git_repo, store, Options(dry_run=True, kind="docs"), registry={}, launcher=script)
    assert dry.task_id is None and dry.status == "dry run" and dry.plan.start == Cell("haiku")
    assert "plan: dry run" in dry.text()


def test_rules_only_classification(git_repo, store):
    plan = orchestrate.plan("haz commit y push", Path(git_repo), store, Options(use_model_classifier=False), {})
    assert plan.kind == "docs" and plan.start == Cell("haiku")


# --- livespec, adopted ---------------------------------------------------------

def _livespec(tmp_path, *, runner=None, popen=None):
    import subprocess as sp

    from locks.adapters.livespec import Livespec

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "uvx").write_text("#!/bin/sh\n")
    (bindir / "uvx").chmod(0o755)
    env = {"PATH": str(bindir), "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "cc"),
           "LOCKS_HOME": str(tmp_path / "locks")}
    return Livespec(env=env, runner=runner or (lambda argv, **kw: sp.CompletedProcess(argv, 0, "", "")),
                    **({"popen": popen} if popen else {}))


@pytest.mark.usefixtures("no_verifier")
def test_livespec_runs_through_the_whole_flow(git_repo, store, tmp_path):
    from .livespec_fixture import build

    build(git_repo)  # dated in the future, so never older than the last commit
    script = Script(ok("fixed"))

    def launcher(spec):
        (spec.target_dir / "src").mkdir(exist_ok=True)
        (spec.target_dir / "src" / "billing.py").write_text("fixed\n")
        script(spec)
        return WorkerResult(True, None, "fixed", "1 passed", ("src/billing.py",), 0.1)

    report = run("charge_card charges twice on retry", git_repo, store, registry={}, launcher=launcher,
                 classifier=kind("implement"), adapters=[_livespec(tmp_path)])
    spec = script.specs[0]
    # routing: a critical spec moves the start one rung up
    assert report.cells == ["sonnet/high"]
    assert any("critical spec" in r for r in report.plan.reasons)
    # briefing: the code map opens the worker's prompt
    assert "Code map from livespec" in spec.prompt and "SPEC-7" in spec.prompt
    # the worker gets livespec's server and a hint naming the workspace
    assert spec.mcp_servers["livespec"]["command"] == "uvx"
    # and its tools are allowed with it: a worker is never refused the index it was handed
    assert "mcp__livespec" in spec.allowed_tools
    assert f'workspace="{git_repo.resolve()}"' in spec.append_system_prompt
    assert "livespec" in report.plan.capabilities
    # after the pass: what the change touched
    assert any("SPEC-7" in line for line in report.impact)
    assert "livespec: present" in report.text()


def test_an_unindexed_repo_is_indexed_in_the_background_and_the_task_starts_at_once(git_repo, store, tmp_path):
    from locks.adapters import livespec as ls

    from .livespec_fixture import build

    started = []

    def popen(argv, **kw):
        started.append(argv)
        return None

    dry = run("parse_amount rounds wrong", git_repo, store, Options(dry_run=True), registry={},
              classifier=kind("implement"), adapters=[_livespec(tmp_path, popen=popen)])
    assert "livespec: would index in the background" in dry.plan.reasons
    assert started == [] and not (git_repo / ".mcp-docs").exists()
    script = Script(ok())
    lines = []
    report = run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[_livespec(tmp_path, popen=popen)], progress=lines.append)
    # the index is started, never waited on: the task runs without it
    assert started and started[0][-2:] == ["index-livespec", str(git_repo.resolve())]
    assert "livespec: indexing in the background; this task runs without it" in report.plan.reasons
    assert "Code map from livespec" not in script.specs[0].prompt
    assert report.status == "done" and lines[0].startswith(f"locks: task #{report.task_id} started")
    # a task planned while that index runs starts no second one
    adapter = _livespec(tmp_path, popen=popen)
    with ls._lock(adapter.lock_path(git_repo), wait_s=0) as (got, _):
        assert got
        busy = run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=Script(ok()),
                   classifier=kind("implement"), adapters=[adapter])
    assert "livespec: already indexing in the background; this task runs without it" in busy.plan.reasons
    assert len(started) == 1
    # the next task finds the index the background run built
    build(git_repo)
    script = Script(ok())
    report = run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[_livespec(tmp_path, popen=popen)])
    assert len(started) == 1 and "22 caller(s)" in script.specs[0].prompt


class _Index:
    """A `locks index-livespec` process: `wait` builds the index the fixture describes, or runs out of time."""

    def __init__(self, repo_dir, *, late=False):
        self.repo_dir, self.late = repo_dir, late

    def wait(self, timeout=None):
        from .livespec_fixture import build

        if self.late:
            raise subprocess.TimeoutExpired("index-livespec", timeout)
        build(self.repo_dir)
        return 0


def test_a_task_waits_for_the_index_before_its_worker_starts(git_repo, store, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKS_LIVESPEC_WAIT", "30")
    started = []

    def popen(argv, **kw):
        started.append(argv)
        return _Index(git_repo)

    dry = run("parse_amount rounds wrong", git_repo, store, Options(dry_run=True), registry={},
              classifier=kind("implement"), adapters=[_livespec(tmp_path, popen=popen)])
    assert "livespec: would index before the worker starts (waits up to 30 s)" in dry.plan.reasons
    assert started == [] and not (git_repo / ".mcp-docs").exists()
    script, lines = Script(ok()), []
    report = run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[_livespec(tmp_path, popen=popen)], progress=lines.append)
    assert len(started) == 1 and started[0][-2:] == ["index-livespec", str(git_repo.resolve())]
    assert any(r.startswith("livespec: repo: indexed before the worker started") for r in report.plan.reasons)
    # The worker's brief opens with the map of the index that was built for it.
    assert "Code map from livespec" in script.specs[0].prompt and "22 caller(s)" in script.specs[0].prompt
    assert report.status == "done"
    waiting = next(i for i, line in enumerate(lines) if "livespec is indexing repo before the worker starts" in line)
    assert waiting < next(i for i, line in enumerate(lines) if "started: implement" in line)


def test_past_the_wait_the_task_starts_without_the_index_and_says_so(git_repo, store, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKS_LIVESPEC_WAIT", "30")
    started = []

    def popen(argv, **kw):
        started.append(argv)
        return _Index(git_repo, late=True)

    script = Script(ok())
    report = run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[_livespec(tmp_path, popen=popen)])
    assert report.status == "done" and len(started) == 1
    said = [r for r in report.plan.reasons if r.startswith("livespec: repo: still indexing after the 30 s")]
    assert len(said) == 1 and "this task runs without it" in said[0]
    # Said once: not again as a background refresh.
    assert not any("in the background" in r for r in report.plan.reasons)
    assert "Code map from livespec" not in script.specs[0].prompt


def test_past_the_wait_a_stale_index_still_reaches_the_worker_and_the_plan_says_it_is_used_as_it_is(
        git_repo, store, tmp_path, monkeypatch):
    from locks.adapters import livespec as ls

    from .livespec_fixture import build

    build(git_repo)
    monkeypatch.setattr(ls, "_older_than_head", lambda indexed_at, root: True)
    monkeypatch.setenv("LOCKS_LIVESPEC_WAIT", "30")
    script = Script(ok())
    report = run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"),
                 adapters=[_livespec(tmp_path, popen=lambda argv, **kw: _Index(git_repo, late=True))])
    said = [r for r in report.plan.reasons if r.startswith("livespec: repo: still indexing after the 30 s")]
    assert len(said) == 1
    assert "this task uses the index as it is" in said[0]
    assert "22 caller(s)" in script.specs[0].prompt


def test_a_task_in_a_folder_of_repositories_indexes_each_one_and_never_the_folder(store, tmp_path, monkeypatch):
    from .test_livespec_adapter import make_repo

    monkeypatch.setenv("LOCKS_LIVESPEC_WAIT", "30")
    folder = tmp_path / "folder"
    one, two = make_repo(folder, "one"), make_repo(folder, "two")
    started = []

    def popen(argv, **kw):
        started.append(Path(argv[-1]))
        return _Index(Path(argv[-1]))

    the_plan = orchestrate.plan("parse_amount rounds wrong", folder, store, Options(), {},
                                classifier=kind("implement"), adapters=[_livespec(tmp_path, popen=popen)])
    assert sorted(started) == [one, two]
    assert folder.resolve() not in started and not (folder / ".mcp-docs").exists()
    assert sum(r.startswith("livespec: ") and "indexed before the worker started" in r
               for r in the_plan.reasons) == 2


def test_a_worker_is_told_to_orient_by_specs_before_the_map_and_before_opening_files(git_repo, store, tmp_path):
    from .livespec_fixture import build

    build(git_repo)
    script = Script(ok())
    run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
        adapters=[_livespec(tmp_path)])
    prompt = script.specs[0].prompt
    assert prompt.index("Orient by livespec before you open files") < prompt.index("Code map from livespec")
    assert prompt.index("`list_specs`") < prompt.index("`get_spec_implementation`") < prompt.index("`read_unit`")
    assert "`search_similar`" in prompt and "`git_diff_impact`" in prompt
    # Told also when the request names no symbol the index knows: there is no map, and the way still holds.
    bare = Script(ok())
    run("make the build faster", git_repo, store, registry={}, launcher=bare, classifier=kind("explore"),
        adapters=[_livespec(tmp_path)])
    assert "Orient by livespec before you open files" in bare.specs[0].prompt
    assert "Code map from livespec" not in bare.specs[0].prompt and "`search_similar`" not in bare.specs[0].prompt


def test_a_worker_with_no_index_is_not_told_to_call_livespec(git_repo, store, tmp_path):
    script = Script(ok())
    run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
        adapters=[_livespec(tmp_path)])
    assert "Orient by livespec" not in script.specs[0].prompt


def test_a_worker_that_writes_in_a_repository_with_specs_is_told_to_link_them_to_their_code(git_repo, store,
                                                                                            tmp_path):
    from .livespec_fixture import build
    from .test_specs import _behind

    _behind(git_repo, code_commits=1)
    build(git_repo)
    writing, reading, unindexed = Script(ok()), Script(ok()), Script(ok())
    run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=writing, classifier=kind("implement"),
        adapters=[_livespec(tmp_path)])
    assert "`@spec:<capability>-<requirement-name>`" in writing.specs[0].prompt
    assert "`index_project`" in writing.specs[0].prompt
    run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=reading, classifier=kind("explore"),
        adapters=[_livespec(tmp_path)])
    assert "@spec:" not in reading.specs[0].prompt
    # With no index there is nothing to re-index: the spec-links rule is livespec's, so it is not said.
    (git_repo / ".mcp-docs" / "docs.db").unlink()
    run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=unindexed, classifier=kind("implement"),
        adapters=[_livespec(tmp_path)])
    assert "@spec:" not in unindexed.specs[0].prompt


def test_a_stale_index_is_used_as_it_is_while_one_refresh_runs(git_repo, store, tmp_path, monkeypatch):
    from locks.adapters import livespec as ls

    from .livespec_fixture import build

    build(git_repo)
    monkeypatch.setattr(ls, "_older_than_head", lambda indexed_at, root: True)
    started = []
    adapter = _livespec(tmp_path, popen=lambda argv, **kw: started.append(argv))
    script = Script(ok())
    report = run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[adapter])
    assert "livespec: refreshing in the background; this task uses the index as it is" in report.plan.reasons
    assert "22 caller(s)" in script.specs[0].prompt and len(started) == 1
    # a refresh that holds the lock is not started twice
    with ls._lock(adapter.lock_path(git_repo), wait_s=0) as (got, _):
        assert got
        again = run("parse_amount rounds wrong", git_repo, store, Options(dry_run=False), registry={},
                    launcher=Script(ok()), classifier=kind("implement"), adapters=[adapter])
    assert "livespec: already refreshing in the background; this task uses the index as it is" in again.plan.reasons
    assert len(started) == 1


def test_a_stale_index_livespec_cannot_refresh_says_so_and_never_claims_a_running_one(
        git_repo, store, tmp_path, monkeypatch):
    from locks.adapters import livespec as ls

    from .livespec_fixture import build

    build(git_repo)
    monkeypatch.setattr(ls, "_older_than_head", lambda indexed_at, root: True)
    adapter = _livespec(tmp_path, popen=lambda argv, **kw: pytest.fail("nothing to start"))
    monkeypatch.setattr(adapter, "server", lambda: None)
    report = run("parse_amount rounds wrong", git_repo, store, Options(dry_run=True), registry={},
                 classifier=kind("implement"), adapters=[adapter])
    assert "livespec: cannot refresh here; this task uses the index as it is" in report.plan.reasons
    assert not any("already" in r or "would refresh" in r for r in report.plan.reasons)


def test_review_of_critical_code_is_reviewed_as_critical(git_repo, store, tmp_path):
    from .livespec_fixture import build

    build(git_repo)
    report = run("review the charge_card change", git_repo, store, Options(dry_run=True), registry={},
                 classifier=kind("review-routine"), adapters=[_livespec(tmp_path)])
    assert report.plan.kind == "review-critical"
    assert "Code map from livespec" in report.text()


def test_the_switch_turns_livespec_off_everywhere(git_repo, store, monkeypatch):
    from locks import orchestrate as o

    monkeypatch.setenv("LOCKS_ENGRAM", "off")  # the other neighbour adopted, its own switch
    monkeypatch.setenv("LOCKS_LIVESPEC", "on")
    assert [a.name for a in o.adopted(Options(), None)] == ["livespec"]
    assert o.adopted(Options(livespec=False), None) == []
    monkeypatch.setenv("LOCKS_LIVESPEC", "off")
    assert o.adopted(Options(), None) == []
    assert [a.name for a in o.adopted(Options(livespec=True), None)] == ["livespec"]


# --- what a watcher sees, and stopping a run -------------------------------------

def test_a_run_leaves_an_event_trail_a_watcher_can_follow(git_repo, store):
    script = Script(bad(Failure.CODE_BUG, "missed it"), ok("fixed"))
    report = run("fix the thing", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    events = store.events(task_id=report.task_id)
    assert [e["kind"] for e in events] == ["planned", "briefed", "attempt_started", "attempt_finished", "moved",
                                           "attempt_started", "attempt_finished", "finished"]
    assert events[2]["data"]["cell"] == "sonnet/medium" and events[4]["data"]["next_cell"] == "sonnet/high"
    assert events[-1]["data"]["status"] == "done"
    assert store.events(after=events[3]["id"], task_id=report.task_id)[0]["kind"] == "moved"
    # Where its time went: each phase is kept on the events, for the analysis.
    assert events[0]["data"]["plan_s"] >= 0 and events[0]["data"]["queued_s"] is None
    assert events[1]["data"] == {"session": None, "dead_ends": 0, "areas": [],
                                 "context_chars": events[1]["data"]["context_chars"],
                                 # The brief's footprint (`locks.footprint`), and the release that wrote it.
                                 "kind": "implement", "system_chars": events[1]["data"]["system_chars"],
                                 "skills": events[1]["data"]["skills"], "version": events[1]["data"]["version"],
                                 # Which role it wore, and which version of its text (`roles.version`).
                                 "role": "implementer", "role_version": events[1]["data"]["role_version"],
                                 # Where each of its skills was read from: bundled here, no plugin installed.
                                 "skill_sources": events[1]["data"]["skill_sources"]}
    assert set(events[1]["data"]["skill_sources"].values()) == {"bundled"}
    assert events[2]["data"]["since_start_s"] >= 0 and events[2]["data"]["brief_chars"] > 0
    assert {"wall_s", "duration_s", "check_s", "verifier_s", "input_tokens"} <= set(events[3]["data"])
    assert {"wall_s", "hold_s", "services_s", "worker_s", "check_s", "verifier_s"} <= set(events[-1]["data"])
    assert events[-1]["data"]["attempts"] == 2
    task = store.get_task(report.task_id)
    assert task["pid"] is None and task["current_cell"] is None


def test_a_cancel_requested_between_attempts_stops_the_run(git_repo, store):
    def launcher(spec):
        store.request_cancel(store.list_tasks()[0]["id"])
        return bad(Failure.CODE_BUG, "first")

    report = run("x", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    assert report.status == "cancelled" and report.cells == ["sonnet/medium"]
    assert report.branch is None
    assert store.get_task(report.task_id)["status"] == "cancelled"


def test_a_cancel_in_the_middle_of_an_attempt_keeps_what_it_did(git_repo, store):
    from locks.orchestrate import Cancelled

    def launcher(spec):
        (spec.target_dir / "half.py").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=spec.target_dir, check=True)
        subprocess.run(["git", "-c", "user.name=w", "-c", "user.email=w@x", "commit", "-qm", "half"],
                       cwd=spec.target_dir, check=True)
        raise Cancelled

    report = run("x", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    # A cancel stops the work; it never throws away what the worker committed.
    assert report.status == "cancelled" and report.branch == f"locks/task-{report.task_id}"
    assert "half.py" in git(git_repo, "show", "--name-only", "--format=", report.branch)
    assert "keeps what it did before the cancel" in report.text()
    finished = store.last_event(report.task_id, "finished")["data"]
    assert finished["tip"] == git(git_repo, "rev-parse", report.branch).strip()


@pytest.mark.usefixtures("no_verifier")
def test_the_fix_that_worked_is_anchored_to_its_commit_and_costs_inform_the_next_plan(git_repo, store):
    script = Script(bad(Failure.CODE_BUG, "missed it"), ok("fixed", cost=0.4), write="f.py")
    report = run("fix the parser", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    fix = store.problem(store.dead_ends("fix the parser")[0]["problem_id"])["fixes"][-1]
    assert fix["outcome"] == "worked" and fix["commit_sha"] == git(git_repo, "rev-parse", report.branch)
    nxt = run("another", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"))
    assert any("cost $0.50 on average" in r for r in nxt.plan.reasons)


def refused(summary="created the routes; node was refused", verdict="inconclusive", cost=0.1) -> WorkerResult:
    return WorkerResult(False, Failure.PERMISSION, summary, cost_usd=cost, verdict=verdict,
                        denied=("Bash(node server.js)",), changed_paths=("routes.js",))


def test_work_a_refusal_stopped_is_kept_unverified_and_the_report_says_what_git_saw(git_repo, store):
    """The audited run: a worker wrote the routes, was refused `node`, said so; the
    run ended blocked and the routes were nowhere. Now they are on the branch."""
    script = Script(refused(), write="routes.js")
    report = run("create the training routes", git_repo, store, Options(allow_tools=("Bash(npm run build)",)),
                 registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "blocked" and report.cells == ["sonnet/medium"]  # no retry into the same refusal
    assert "Bash(node server.js)" in report.summary and "--allow" in report.summary
    assert "The last worker's own account: created the routes" in report.summary
    # Beside the person's rule, only reading the skill folders of the worker's role.
    assert script.specs[0].allowed_tools == ("Bash(npm run build)", f"Read(/{roles.bundled_skills_dir()}/**)")
    assert report.branch == f"locks/task-{report.task_id}" and report.changed == ["routes.js"]
    assert "routes.js" in git(git_repo, "show", "--name-only", report.branch)
    message = git(git_repo, "log", "-1", "--format=%B", report.branch)
    # The subject is the change's; the task and its state are trailers a history can read past.
    assert message.splitlines()[0] == "create the training routes"
    assert f"Locks-Task: #{report.task_id}" in message and "Locks-Verified: no (blocked)" in message
    assert "created the routes; node was refused" in message
    text = report.text()
    assert "changed (from git): routes.js" in text
    assert "keeps this task's work, unverified" in text and f"locks resume {report.task_id}" in text
    assert "review it, then merge it" not in text
    assert json.loads(store.get_task(report.task_id)["options"])["allow_tools"] == ["Bash(npm run build)"]
    finished = store.last_event(report.task_id, "finished")["data"]
    assert finished["changed"] == ["routes.js"] and finished["branch"] == report.branch
    assert store.last_event(report.task_id, "attempt_finished")["data"]["denied"] == ["Bash(node server.js)"]


def test_the_repositorys_check_can_settle_what_a_refused_worker_could_not(git_repo, store):
    script = Script(refused(), write="routes.js")
    report = run("create the routes", git_repo, store, Options(verify="test -f routes.js"), registry={},
                 launcher=script, classifier=kind("implement"))
    assert report.status == "done" and report.final_cell == "sonnet/medium"
    assert "`test -f routes.js` passed in its place" in report.summary
    assert store.attempts(report.task_id)[0]["passed"] == 1
    # red, it stays blocked, with the check's output for the person
    script = Script(refused(), write="routes.js")
    report = run("create the routes", git_repo, store, Options(verify="echo nope; exit 3"), registry={},
                 launcher=script, classifier=kind("implement"))
    assert report.status == "blocked"
    assert "exited 3" in store.attempts(report.task_id)[0]["evidence"]
    # a worker that called it a failure is not passed by a green check, nor a task that wrote nothing
    for result in (refused(verdict="fail"), WorkerResult(False, Failure.PERMISSION, "x", verdict="inconclusive",
                                                         denied=("Bash(ls)",))):
        report = run("create the routes", git_repo, store, Options(verify="true"), registry={},
                     launcher=Script(result, write="routes.js" if result.verdict == "fail" else None),
                     classifier=kind("implement"))
        assert report.status == "blocked"
    # outside a worktree, what the attempt changed is what the task wrote
    report = run("create the routes", git_repo, store, Options(verify="true", isolate=False), registry={},
                 launcher=Script(refused()), classifier=kind("implement"))
    assert report.status == "done" and report.changed == ["routes.js"]
    # No branch to review: the files are the work, and locks' check passed on them.
    assert "these changes are in your checkout (the run had no worktree), checked by locks' own run of its " \
           "check\n" in report.text()
    report = run("create the routes", git_repo, store, Options(isolate=False), registry={},
                 launcher=Script(refused()), classifier=kind("implement"))
    assert "these changes are in your checkout (the run had no worktree), unverified" in report.text()


def test_a_run_in_a_folder_of_several_repositories_is_read_in_each_while_it_runs(tmp_path, store):
    """A request that edits two repositories is sent from the folder that holds them: no worktree can be
    made there, so what the worker writes is read in each repository against how it stood at the start,
    while it runs and at the end, and the ending says which repositories it changed in place."""
    from locks import progress
    from locks.store import home

    from .conftest import git
    from .test_checking import log

    folder = tmp_path / "capacitacion"
    api, web = folder / "api", folder / "web"
    for repo_dir in (api, web):
        repo_dir.mkdir(parents=True)
        git(repo_dir, "init", "-q", "-b", "main")
        (repo_dir / "README.md").write_text("hi\n")
        git(repo_dir, "add", "-A")
        git(repo_dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    (web / "mine.txt").write_text("the person's own, not committed\n")
    midway: dict = {}

    def launcher(spec):
        task = store.list_tasks(status=["running"])[0]
        (api / "routes.js").write_text("a\nb\n")
        (web / "app.ts").write_text("x\n")
        log([(task["id"], 1, "read:app")])
        midway["progress"] = progress.of(store, task)
        midway["peek"] = progress.peek(task["id"], None, places=store.last_event(
            task["id"], "attempt_started")["data"]["places"])
        return WorkerResult(True, None, "done", evidence="1 passed", changed_paths=("api/routes.js", "web/app.ts"))

    report = run("edit both", folder, store, Options(), registry={}, launcher=launcher,
                 classifier=kind("implement"))
    assert report.status == "done"
    assert not report.isolated
    assert not (home() / "worktrees" / f"task-{report.task_id}").exists()
    # the attempt kept where each repository stood; the person's own file is no change of the worker's
    started = store.last_event(report.task_id, "attempt_started")["data"]
    assert [(p["name"], bool(p["head"])) for p in started["places"]] == [("api", True), ("web", True)]
    assert midway["progress"]["changes"] == {"files": 2, "added": 3, "removed": 0,
                                             "paths": ["api/routes.js", "web/app.ts"]}
    assert midway["progress"]["phase"] == "implementing"  # the log's last call was a read
    assert "+a" in midway["peek"]["diff"]
    assert "b/api/routes.js" in midway["peek"]["diff"]
    assert "b/web/app.ts" in midway["peek"]["diff"]
    assert "mine.txt" not in midway["peek"]["diff"]
    # and the ending says plainly which repositories it changed, in place
    assert report.in_place == ["api", "web"]
    assert "in your checkouts, repository by repository: api (1 file), web (1 file); the run had no worktree" \
        in report.text()
    assert store.final_event(report.task_id)["data"]["in_place"] == ["api", "web"]


@pytest.mark.usefixtures("no_verifier")
def test_a_later_attempt_in_a_folder_of_repositories_still_reads_the_work_of_the_earlier_ones(tmp_path, store):
    """The repositories' starting point is the run's, taken once: a second attempt begins on what the
    first wrote in the person's checkouts, which is the task's work and not what they already had."""
    from locks import progress

    from .conftest import git

    folder = tmp_path / "capacitacion"
    api = folder / "api"
    api.mkdir(parents=True)
    git(api, "init", "-q", "-b", "main")
    (api / "README.md").write_text("hi\n")
    git(api, "add", "-A")
    git(api, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    seen: list = []

    def launcher(spec):
        task = store.list_tasks(status=["running"])[0]
        started = store.last_event(task["id"], "attempt_started")["data"]
        seen.append((started["places"], progress.changes(task["id"], None, places=started["places"])))
        name = f"f{len(seen)}.js"
        (api / name).write_text("a\n")
        if len(seen) == 1:
            return WorkerResult(False, Failure.CODE_BUG, "still broken", cost_usd=0.1, changed_paths=(f"api/{name}",))
        return WorkerResult(True, None, "done", evidence="1 passed", changed_paths=("api/f1.js", f"api/{name}"))

    report = run("edit the api", folder, store, Options(), registry={}, launcher=launcher,
                 classifier=kind("implement"))
    assert report.status == "done" and len(seen) == 2
    assert seen[0][0] == seen[1][0]  # the same starting point, recorded again for the attempt that reads it
    assert seen[0][1]["files"] == 0
    assert seen[1][1]["paths"] == ["api/f1.js"]  # what the first attempt wrote is not "already there"


def test_work_that_failed_is_dropped_and_the_report_says_so(git_repo, store):
    script = Script(bad(Failure.SPEC_BUG, "no such API"), write="half.py")
    report = run("do it", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "replan" and report.branch is None
    assert "changed (from git): half.py" in report.text() and "nothing of it was kept" in report.text()
    # a reading task prints no changed line: it has nothing to change
    report = run("where is it", git_repo, store, registry={}, launcher=Script(ok()), classifier=kind("explore"))
    assert "changed (from git)" not in report.text()


@pytest.mark.usefixtures("no_verifier")
def test_a_resumed_task_continues_on_its_branch_with_its_history_in_the_brief(git_repo, store):
    first = run("create the routes", git_repo, store, registry={}, launcher=Script(refused(), write="routes.js"),
                classifier=kind("implement"))
    (git_repo / ".gitignore").write_text("node_modules/\n")
    (git_repo / "node_modules").mkdir()
    seen = []

    spec_dirs = []

    def launcher(spec):
        seen.append((spec.prompt, (spec.target_dir / "routes.js").read_text()))
        spec_dirs.extend(spec.extra_dirs)
        return ok("checked the routes")

    history = [orchestrate.Attempt(Cell("sonnet", "medium"), 30, False, Failure.PERMISSION, "node was refused",
                                   ("Bash(node server.js)",))]
    report = run("create the routes", git_repo, store, Options(kind="implement", start=Cell("sonnet", "medium")),
                 registry={}, launcher=launcher, task_id=first.task_id, history=history)
    prompt, routes = seen[0]
    assert routes == "attempt 1\n"  # what the first run wrote is where the second starts
    assert "is resumed" in prompt and "attempt 1 [sonnet/medium, permission] node was refused" in prompt
    assert "refused: Bash(node server.js)" in prompt
    assert "Linked from the person's checkout and shared with it: node_modules" in prompt
    assert spec_dirs == [(git_repo / "node_modules").resolve()]
    assert report.status == "done" and report.branch == first.branch
    assert store.get_task(first.task_id)["cost_usd"] == pytest.approx(0.2)
    assert [a["seq"] for a in store.attempts(first.task_id)] == [1, 2]


@pytest.mark.usefixtures("no_verifier")
def test_a_new_task_never_builds_on_a_branch_of_its_number_another_store_left(git_repo, store):
    task = store.create_task("create the routes", status="queued", source="locks")
    branch = f"locks/task-{task['id']}"
    git(git_repo, "checkout", "-q", "-b", branch)
    (git_repo / "foreign.py").write_text("x\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "another store's task")
    git(git_repo, "checkout", "-q", "-")
    seen = []

    def launcher(spec):
        seen.append(((spec.target_dir / "foreign.py").exists(), spec.prompt))
        (spec.target_dir / "routes.js").write_text("r\n")
        return ok("wrote the routes")

    report = run("create the routes", git_repo, store, Options(kind="implement"), registry={}, launcher=launcher,
                 task_id=task["id"])
    [(found, prompt)] = seen
    assert not found and "is resumed" not in prompt
    assert report.status == "done"
    [aside] = store.events_of(task["id"], "branch_set_aside")
    assert aside["data"]["to"].startswith(f"locks/foreign/task-{task['id']}-")
    assert git(git_repo, "log", "-1", "--format=%s", aside["data"]["to"]).strip() == "another store's task"


def test_a_resumed_worker_that_finds_the_work_done_is_settled_by_the_check(git_repo, store):
    first = run("create the routes", git_repo, store, registry={}, launcher=Script(refused(), write="routes.js"),
                classifier=kind("implement"))
    again = WorkerResult(False, Failure.PERMISSION, "routes.js was already there", verdict="inconclusive",
                         denied=("Bash(node server.js)",))
    report = run("create the routes", git_repo, store, Options(kind="implement", verify="test -f routes.js"),
                 registry={}, launcher=Script(again), task_id=first.task_id)
    assert report.status == "done" and report.changed == ["routes.js"]
    assert "Locks-Verified: no" in git(git_repo, "log", "-1", "--format=%B", f"{report.branch}~1")
    assert "Locks-Verified: no" not in git(git_repo, "log", "-1", "--format=%B", report.branch)


def test_a_worker_that_ran_out_of_turns_is_checked_by_locks_and_a_red_check_reaches_the_next_brief(git_repo, store):
    """A large change used every turn before it ran the tests; the tests were never run
    and the run was sent back to be split. The repository's check now runs for it."""
    out_of_turns = WorkerResult(False, Failure.TURNS_EXHAUSTED, "used all 30 turns without a verdict", cost_usd=0.1)
    report = run("remove the old module", git_repo, store, Options(verify="test -f gone.txt"), registry={},
                 launcher=Script(out_of_turns, write="gone.txt"), classifier=kind("refactor"))
    assert report.status == "done" and report.cells == ["sonnet/medium"]
    assert "ran out of turns before checking its work; `test -f gone.txt` passed in its place" in report.summary

    specs = []

    def launcher(spec):
        specs.append(spec)
        (spec.target_dir / ("half.txt" if len(specs) == 1 else "done.txt")).write_text("x\n")
        return out_of_turns if len(specs) == 1 else ok()

    check = "test -f done.txt || { echo 2 tests failed; exit 1; }"
    report = run("remove the old module", git_repo, store, Options(verify=check), registry={}, launcher=launcher,
                 classifier=kind("refactor"))
    first = store.attempts(report.task_id)[0]
    assert first["failure"] == "turns_exhausted" and "2 tests failed" in first["evidence"]
    assert f"locks ran `{check}`, which exited 1" in first["summary"]
    assert specs[1].max_turns == 2 * turns_for("refactor") and report.status == "done"
    assert "What locks' own check said when it failed the last attempt" in specs[1].prompt
    assert "2 tests failed" in specs[1].prompt


def test_a_run_in_the_checkout_resumes_in_the_checkout(git_repo, store):
    assert Options(isolate=False).stored()["no_isolate"] is True
    assert "no_isolate" not in Options().stored()
    from locks import flow

    task = {"options": '{"no_isolate": true}'}
    assert flow.queued_options(task, Options()).isolate is False
    assert flow.queued_options({"options": "{}"}, Options()).isolate is True


def test_the_plan_names_the_rules_workers_here_were_refused_before(git_repo, store, monkeypatch):
    for i in range(2):
        refusal = WorkerResult(False, Failure.PERMISSION, "refused", verdict="inconclusive",
                               denied=("Bash(npx tsc --noEmit; npm run build)",),
                               allow=("Bash(npx tsc:*)", "Bash(npm run build:*)"), permission_mode="auto")
        run(f"task {i}", git_repo, store, registry={}, launcher=Script(refusal), classifier=kind("implement"))
    for _ in range(2):
        old = store.create_task("older", status="blocked", source="locks", repo=str(git_repo.resolve()))
        store.add_event(old["id"], "attempt_finished", seq=1, denied=["Bash(make lint)"])  # before rules were kept
    once = store.create_task("one task's", status="blocked", source="locks", repo=str(git_repo.resolve()))
    store.add_event(once["id"], "attempt_finished", seq=1, denied=["Bash(git checkout main)"],
                    permission_mode="auto")
    dry = run("next", git_repo, store, Options(dry_run=True, allow_tools=("Bash(npm run build:*)",)), registry={},
              classifier=kind("implement"))
    note = next(r for r in dry.plan.reasons if r.startswith("workers here were refused before"))
    assert "Bash(npx tsc:*) ×2" in note and "npm run build" not in note
    # what acceptEdits refused, before auto mode, is not what auto mode will refuse; one task's refusal is its own
    assert "make lint" not in note and "git checkout" not in note
    monkeypatch.setenv("LOCKS_MODE", "acceptEdits")
    dry = run("next", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"))
    note = next(r for r in dry.plan.reasons if r.startswith("workers here were refused before"))
    assert note.startswith("workers here were refused before: Bash(make lint:*) ×2.")
    # work since ages them out
    monkeypatch.setattr(orchestrate, "REFUSALS_WINDOW", 1)
    later = store.create_task("later", status="done", source="locks", repo=str(git_repo.resolve()))
    store.add_event(later["id"], "attempt_finished", seq=1)
    dry = run("next", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"))
    assert not any(r.startswith("workers here were refused before") for r in dry.plan.reasons)
    blocked = store.list_tasks(status=["blocked"])[-1]
    assert "--allow 'Bash(npx tsc:*)' --allow 'Bash(npm run build:*)'" in store.get_task(blocked["id"])["result"]


def test_the_task_text_says_who_wrote_it(git_repo, store):
    """Sent by the main session through its own Bash, the text is the orchestrator's,
    not something the person typed."""
    sent = run("x", git_repo, store, registry={}, launcher=Script(ok()), classifier=kind("implement"), session_id="s1")
    typed = run("y", git_repo, store, registry={}, launcher=Script(ok()), classifier=kind("implement"))
    assert store.messages(sent.task_id)[0]["role"] == "orchestrator"
    assert store.messages(typed.task_id)[0]["role"] == "person"


def test_the_plan_says_when_a_task_needs_a_browser_or_running_servers(git_repo, store, tmp_path):
    text = "Start both dev servers (app on 4200) and open the browser at localhost:4200 to check the admin panel"
    dry = run(text, git_repo, store, Options(dry_run=True), registry={}, classifier=kind("test"))
    notes = [r for r in dry.plan.reasons if r.startswith("this task ")]
    assert len(notes) == 2 and "browser MCP server" in notes[0] and "--allow 'mcp__browser'" in notes[0]
    assert "starts them itself" in notes[1] and "'Start both dev servers'" in notes[1] and "--service" not in notes[1]
    (tmp_path / "caps.json").write_text(json.dumps({"browser": caps.EXAMPLE["browser"]}))
    registry = caps.load(tmp_path / "caps.json")
    dry = run(text, git_repo, store, Options(dry_run=True), registry=registry, classifier=kind("explore"))
    assert [r for r in dry.plan.reasons if r.startswith("this task ")] == [notes[1]]
    plain = run("fix the parser", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"))
    assert not any(r.startswith("this task needs") for r in plain.plan.reasons)


def test_work_that_outgrew_its_turns_is_kept_for_the_resume(git_repo, store):
    out = WorkerResult(False, Failure.TURNS_EXHAUSTED, "ran out", cost_usd=0.1)
    report = run("reduce it", git_repo, store, registry={}, launcher=Script(out, out, write="half.py"),
                 classifier=kind("refactor"))
    assert report.status == "replan" and report.stop.cause == "turns"
    assert report.branch == f"locks/task-{report.task_id}"
    assert "half.py" in git(git_repo, "show", "--name-only", report.branch)
    assert f"locks resume {report.task_id} --max-turns 120" in report.text()


def test_a_worker_gets_its_mode_the_repository_s_rules_and_the_tools_it_was_handed(git_repo, store, tmp_path):
    from locks import grants, repo

    grants.grant(repo.key(git_repo), ["Bash(npm:*)"])
    (tmp_path / "caps.json").write_text(json.dumps({"browser": caps.EXAMPLE["browser"]}))
    script = Script(ok())
    registry = caps.load(tmp_path / "caps.json")
    run("check the page", git_repo, store, Options(allow_tools=("Bash(make:*)",)), registry=registry,
        launcher=script, classifier=kind("test"))
    spec = script.specs[0]
    assert spec.permission_mode == "auto"
    assert spec.allowed_tools[:3] == ("Bash(make:*)", "Bash(npm:*)", "mcp__browser")
    script = Script(ok())
    run("where is it", git_repo, store, registry={}, launcher=script, classifier=kind("explore"))
    assert script.specs[0].permission_mode == "bypassPermissions"  # a haiku cell
    dry = run("where is it", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("explore"))
    assert "workers on haiku run in bypassPermissions mode" in dry.plan.reasons


def test_a_refused_attempt_tells_how_to_keep_its_rules(git_repo, store):
    report = run("x", git_repo, store, registry={}, launcher=Script(refused()), classifier=kind("implement"))
    assert "or, for every task in this repository: locks allow 'Bash(node:*)'" in report.text()


def test_a_reading_task_is_told_its_findings_are_the_answer_and_stops_when_cells_agree(git_repo, store):
    said = "the API still allows the old role in two files"
    script = Script(bad(Failure.CODE_BUG, said), bad(Failure.CODE_BUG, said + "."))
    report = run("audit the role rename", git_repo, store, registry={}, launcher=script,
                 classifier=kind("review-critical"))
    assert "also when what you found is broken" in script.specs[0].prompt
    assert report.status == "failed" and report.cells == ["opus/high", "opus/xhigh"]
    assert report.stop.cause == "converged" and report.stop.account == said + "."
    assert "resuming would only repeat them" in report.text() and "locks resume" not in report.text()


@pytest.mark.usefixtures("no_verifier")
def test_a_passing_worker_s_learned_facts_are_filed_and_later_workers_read_them(git_repo, store, monkeypatch):
    from locks import notes

    monkeypatch.setenv("LOCKS_NOTES", "on")
    learned = WorkerResult(True, None, "done", "1 passed", learned=("Prices are stored in cents in every table",))
    asked = []

    ended = []

    def ask(system, schema, question, **kw):
        asked.append(question)
        # Filed after the ending is out: no session or next step waits on the model call.
        ended.append(store.last_event(1, "finished") is not None)
        return {"notes": [{"fact": 0, "keep": True, "topic": "business", "title": "Prices in cents",
                           "duplicate_of": None, "links": [], "paths": ["prices.py"], "new_topic": None}]}, 0.0, ""

    report = run("store prices in cents", git_repo, store, registry={}, launcher=Script(learned, write="prices.py"),
                 classifier=kind("implement"), notes_ask=ask)
    assert report.noted == [{"id": 1, "new": True, "topic": "business", "title": "Prices in cents"}]
    assert "notes: kept #1 [business] Prices in cents" in report.text()
    assert "- prices.py" in asked[0] and ended == [True]
    assert "notes_s" in store.last_event(report.task_id, "notes_filed")["data"]
    anchor = store.note_anchors([1])[0]
    assert anchor["path"] == "prices.py" and anchor["commit_sha"] == git(git_repo, "rev-parse", "locks/task-1")
    assert store.last_event(report.task_id, "noted")["data"]["notes"][0]["id"] == 1
    # the next worker in this project reads it
    script = Script(ok())
    later = run("change how prices are stored in cents", git_repo, store, registry={}, launcher=script,
                classifier=kind("implement"), notes_ask=ask)
    assert "What this project's notes say" in script.specs[0].prompt and "#1 [business] Prices in cents" in (
        script.specs[0].prompt)
    assert any(r.startswith("notes: 1 from this project's memory") for r in later.plan.reasons)
    # off: neither read nor filed
    monkeypatch.setenv("LOCKS_NOTES", "off")
    script = Script(learned)
    run("prices in cents again", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
        notes_ask=ask)
    assert "notes say" not in script.specs[0].prompt and len(asked) == 1
    assert notes.recall(store, notes.repo.key(git_repo), "prices")[0]["id"] == 1


def test_notes_that_cannot_be_filed_never_fail_the_pass(git_repo, store, monkeypatch):
    monkeypatch.setenv("LOCKS_NOTES", "on")

    def broken(*a, **kw):
        raise RuntimeError("database is locked")

    learned = WorkerResult(True, None, "done", "1 passed", learned=("The cache is warmed by a cron job",))
    report = run("x", git_repo, store, registry={}, launcher=Script(learned), classifier=kind("implement"),
                 notes_ask=broken)
    assert report.status == "done" and report.noted == []
    assert any("could not be filed (RuntimeError: database is locked)" in line for line in report.impact)
    # a failed attempt's facts are not filed: they were not confirmed by a pass
    report = run("y", git_repo, store, registry={}, launcher=Script(
        WorkerResult(False, Failure.SPEC_BUG, "no", learned=("A guess about the cache layer",))),
        classifier=kind("implement"), notes_ask=broken)
    assert report.status == "replan" and not store.notes(notes_key(git_repo))


def notes_key(where):
    from locks import repo

    return repo.key(where)


# --- what a run says while it runs ------------------------------------------------

def test_a_run_says_its_task_number_warnings_and_attempts_as_they_happen(git_repo, store):
    lines = []
    script = Script(bad(Failure.CODE_BUG, "missed the rounding " + "x" * 400), ok("fixed"))
    report = run("check the checkout page in the browser and fix the total", git_repo, store, registry={},
                 launcher=script, classifier=kind("implement"), progress=lines.append)
    n = report.task_id
    assert lines[0] == f"locks: task #{n} started: implement, at {report.plan.start.label}"
    # the warning comes before any money is spent, not only in the report
    assert lines[1].startswith(f"locks: #{n} warning: this task needs a browser")
    assert lines.index(f"locks: #{n} attempt 1 at {report.cells[0]}") == 2
    ended = lines[3]
    assert ended.startswith(f"locks: #{n} attempt 1 ended code_bug: missed the rounding")
    assert len(ended) < 220  # a summary is cut, never dumped
    assert lines[-1] == f"locks: #{n} attempt 2 passed"
    # every line is locks', and a reason that is no warning is not repeated
    assert all(line.startswith("locks: ") for line in lines)
    assert not any("ladder" in line for line in lines)


@pytest.mark.parametrize(("files", "verify", "driven"), [
    ({}, "npx playwright test", "its check runs playwright (`npx playwright test`)"),
    ({"deno.json": {"tasks": {"e2e": "npx playwright test"}}}, "deno task e2e", "its check runs playwright"),
    ({"package.json": {"devDependencies": {"playwright-core": "1.56.1"}}}, None,
     "the project's own tests drive a headless browser (playwright-core in package.json)"),
    ({"package.json": {"scripts": {"e2e": "playwright test"}}}, "npm test",
     "the project's own tests drive a headless browser (its e2e script runs playwright)"),
])
def test_a_task_whose_check_drives_the_browser_is_not_warned_it_needs_one(git_repo, store, files, verify, driven):
    for name, content in files.items():
        (git_repo / name).write_text(json.dumps(content))
    the_plan = orchestrate.plan("check the checkout page in the browser and fix the total", git_repo, store,
                                Options(verify=verify, use_model_classifier=False), {}, adapters=())
    assert not any("needs a browser" in r for r in the_plan.reasons)
    said = [r for r in the_plan.reasons if r.startswith("the browser this task needs")]
    assert len(said) == 1 and driven in said[0] and orchestrate.warnings(the_plan.reasons) == []


def test_a_task_with_neither_a_browser_check_nor_browser_tests_is_still_warned(git_repo, store):
    (git_repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest", "e2e": "vitest e2e"}}))
    the_plan = orchestrate.plan("check the checkout page in the browser and fix the total", git_repo, store,
                                Options(verify="npm test", use_model_classifier=False), {}, adapters=())
    said = orchestrate.warnings(the_plan.reasons)
    assert len(said) == 1 and said[0].startswith("this task needs a browser and workers have none")


def test_a_check_that_covers_less_than_ci_is_said_in_the_plan_and_on_the_report(git_repo, store, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (git_repo / ".github" / "workflows").mkdir(parents=True)
    (git_repo / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  db:\n    services:\n      postgres:\n        image: postgres:16\n    steps:\n"
        "      - run: npm ci\n      - run: npm test\n        env:\n          DATABASE_URL: postgres://localhost/t\n")
    (git_repo / "db.js").write_text("const url = process.env.DATABASE_URL\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "ci")
    lines = []
    report = run("fix the total", git_repo, store, Options(verify="true"), registry={}, launcher=Script(ok()),
                 classifier=kind("implement"), progress=lines.append)
    note = "CI also runs: db (`npm test`, a postgres service, DATABASE_URL), which this check does not" + ci.QUIET_HINT
    assert note in report.plan.reasons and note in report.text()
    assert f"locks: #{report.task_id} warning: {note}" in lines
    assert store.last_event(report.task_id, "planned")["data"]["warnings"] == [note]
    # a check that runs it with what it sets covers it; a task without a check is not compared
    covered = orchestrate.plan("fix the total", git_repo, store, Options(
        verify="DATABASE_URL=postgres://localhost/t npm test", use_model_classifier=False), {}, adapters=())
    unchecked = orchestrate.plan("fix the total", git_repo, store, Options(use_model_classifier=False), {},
                                 adapters=())
    assert not any("CI" in r for r in [*covered.reasons, *unchecked.reasons])
    # a workflow that cannot be read is said once, and is no warning
    (git_repo / ".github" / "workflows" / "odd.yml").write_text("jobs:\n  a: b\n c: d\n")
    odd = orchestrate.plan("fix the total", git_repo, store, Options(verify="npm test", use_model_classifier=False),
                           {}, adapters=())
    assert [r for r in odd.reasons if r.startswith("CI:")] == [
        "CI: .github/workflows/odd.yml could not be read; it is left out of what this check is compared with"]


def test_a_dry_run_and_a_run_without_a_listener_say_nothing(git_repo, store, capsys):
    run("check it in the browser", git_repo, store, Options(dry_run=True), registry={},
        classifier=kind("implement"), progress=lambda line: pytest.fail(line))
    report = run("fix the thing", git_repo, store, registry={}, launcher=Script(ok()), classifier=kind("implement"))
    assert report.status == "done"
    assert capsys.readouterr() == ("", "")


# --- a check that cannot run, and work the environment stopped -----------------------

@pytest.mark.parametrize(("check", "why"), [
    ("echo 'npm error Missing script: \"test\"' >&2; exit 1", 'npm error Missing script: "test"'),
    ("echo 'npm ERR! enoent Could not read package.json: Error: ENOENT' >&2; exit 254",
     "npm ERR! enoent Could not read package.json"),
    ("echo 'Node.js version v22.16.0 detected.'; "
     "echo 'The build tool requires a minimum Node.js version of v22.22.3.'; exit 3",
     "requires a minimum Node.js version"),
    # a program the preflight cannot see (inside `sh -c`), so the check runs and exits 127
    ("sh -c 'no-such-tool-anywhere --run'", "exit 127"),
])
def test_a_check_that_cannot_run_is_the_environment_and_stops_the_work_it_let_run(git_repo, store, check, why):
    script = Script(WorkerResult(True, None, "upgraded the lockfile", "0 vulnerabilities", ("package-lock.json",),
                                 0.1))
    report = run("upgrade the vulnerable dependencies", git_repo, store, Options(verify=check), registry={},
                 launcher=script, classifier=kind("implement"))
    # one attempt: no climb to a stronger model, no retry of the same wall
    assert report.status == "blocked" and len(script.specs) == 1
    attempt = store.attempts(report.task_id)[0]
    assert attempt["failure"] == "environment" and "could not run here" in attempt["summary"]
    assert why in attempt["summary"]
    assert report.stop.cause == "environment" and "stopped work that had started" in report.stop.reason
    # nothing the check said is a dead end against the problem
    assert store.dead_ends("upgrade the vulnerable dependencies") == []


@pytest.mark.parametrize("output", [
    "FAIL src/app.spec.ts\n  expected 'npm error Missing script: test' to be shown",  # quoted, not npm's own line
    "npm warn EBADENGINE Unsupported engine {node: '>=22.22.3'}\n1 failing",  # a warning, then a real failure
    "AssertionError: 2 != 3",
])
def test_a_check_that_ran_and_failed_is_still_a_bug_in_the_work(git_repo, store, output):
    # It fails only on the work (the base has no f.py): a failure the base shares would be no bug of it.
    check = f"if [ -f f.py ]; then printf '%s\\n' {shlex.quote(output)}; exit 1; fi"
    script = Script(ok("fixed"), ok("fixed for real"), write="f.py")
    report = run("fix the total", git_repo, store, Options(verify=check, max_attempts=2), registry={}, launcher=script,
                 classifier=kind("implement"))
    first = store.attempts(report.task_id)[0]
    assert first["failure"] == "code_bug" and "exited 1" in first["summary"]
    assert report.cells[:2] == ["sonnet/medium", "sonnet/high"]  # it climbs, as a red check should


def test_a_worker_that_says_the_environment_stopped_it_after_changing_files_is_not_retried(git_repo, store):
    lines = []
    stopped = WorkerResult(False, Failure.ENVIRONMENT, "the CLI needs a newer Node", "Node.js version v22.16.0",
                           ("package-lock.json",), 0.17)
    script = Script(stopped, ok())
    report = run("upgrade the vulnerable dependencies", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), progress=lines.append)
    assert report.status == "blocked" and len(script.specs) == 1
    assert lines[-1].startswith(f"locks: #{report.task_id} attempt 1 ended environment")
    # a launch that broke, nothing changed: the one retry stands
    never = WorkerResult(False, Failure.ENVIRONMENT, "transport error", "", (), 0.0, transient=True)
    script = Script(never, ok())
    report = run("upgrade the vulnerable dependencies", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "done" and report.cells == ["sonnet/medium", "sonnet/medium"]
    # the worker named the wall, nothing changed: blocked at once, no retry
    named = WorkerResult(False, Failure.ENVIRONMENT, "Node v22.16 is below the CLI's minimum", "", (), 0.1)
    script = Script(named, ok())
    report = run("upgrade the vulnerable dependencies", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "blocked"
    assert len(script.specs) == 1


def test_a_feature_starts_with_more_turns_and_a_pin_still_wins(git_repo, store):
    feature = Script(ok())
    report = run("add the export", git_repo, store, registry={}, launcher=feature, classifier=kind("feature"))
    assert feature.specs[0].max_turns == turns_for("feature") == 120
    assert any("feature work starts with 120 turns" in r for r in report.plan.reasons)
    implement = Script(ok())
    run("fix the export", git_repo, store, registry={}, launcher=implement, classifier=kind("implement"))
    assert implement.specs[0].max_turns == turns_for("implement") == 80
    pinned = Script(ok())
    report = run("add the import", git_repo, store, Options(max_turns=45), registry={}, launcher=pinned,
                 classifier=kind("feature"))
    assert pinned.specs[0].max_turns == 45
    assert not any("starts with 120 turns" in r for r in report.plan.reasons)


@pytest.mark.usefixtures("no_verifier")
def test_more_turns_continue_the_worker_s_own_session_in_its_worktree(git_repo, store):
    out = WorkerResult(False, Failure.TURNS_EXHAUSTED, "used all 30 turns", cost_usd=0.1, session_id="sess-1")
    lines = []
    script = Script(out, ok(), write="half.py")
    report = run("fix the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
                 progress=lines.append)
    assert report.status == "done" and report.moves[0].startswith("more_turns")
    first, second = script.specs
    assert first.persist_session and first.resume_session == ""
    assert second.resume_session == "sess-1" and second.max_turns == 2 * turns_for("implement")
    assert second.cell == first.cell
    assert second.launch_dir == first.launch_dir and not second.fork_session
    assert second.resume_prompt == launch.CONTINUE_PROMPT
    moved = store.last_event(report.task_id, "moved")["data"]
    assert moved["continued"] is True and moved["resumed"] == "sess-1" and "forked_from" not in moved
    assert store.attempts(report.task_id)[0]["session_id"] == "sess-1"
    started = [e["data"] for e in store.events(task_id=report.task_id) if e["kind"] == "attempt_started"]
    assert "resumed" not in started[0] and started[1]["resumed"] == "sess-1"
    assert f"locks: #{report.task_id} attempt 2 continues attempt 1's session (sess-1)" in lines


@pytest.mark.usefixtures("no_verifier")
def test_a_continued_session_is_told_the_check_that_failed_after_it_ran_out(git_repo, store):
    out = WorkerResult(False, Failure.TURNS_EXHAUSTED, "used all 30 turns", cost_usd=0.1, session_id="sess-1")
    script = Script(out, ok(), write="half.py")
    check = "grep -q 'attempt 2' half.py || (echo 'AssertionError: total is 3'; false)"
    run("fix the export", git_repo, store, Options(verify=check), registry={}, launcher=script,
        classifier=kind("implement"))
    first, second = script.specs
    assert second.resume_session == "sess-1"
    # the red check locks ran after the worker stopped is in the brief a fresh start gets, and in the continuation
    assert "AssertionError: total is 3" in second.resume_note and "AssertionError: total is 3" in second.prompt
    assert first.resume_note == ""


@pytest.mark.usefixtures("no_verifier")
def test_an_answer_of_the_wrong_form_is_asked_again_in_the_same_session_and_climbs_nothing(git_repo, store):
    out = WorkerResult(False, Failure.INCONCLUSIVE, "the worker did not answer with exactly one result block",
                       cost_usd=0.1, session_id="sess-1", shape="its answer did not hold exactly one result block")
    script = Script(out, ok(), write="half.py")
    report = run("fix the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and report.moves[0].startswith("retry")
    first, second = script.specs
    assert second.cell == first.cell and second.max_turns == first.max_turns
    assert second.resume_session == "sess-1" and not second.fork_session
    assert second.resume_prompt == launch.RESHAPE_PROMPT
    assert "did not hold exactly one result block" in second.resume_note
    moved = store.last_event(report.task_id, "moved")["data"]
    assert moved["axis"] == "retry" and moved["continued"] is True and moved["version"] == __version__


@pytest.mark.usefixtures("no_verifier")
def test_more_turns_without_a_session_start_fresh_and_say_why(git_repo, store):
    out = WorkerResult(False, Failure.TURNS_EXHAUSTED, "ran past its time", cost_usd=0.1)
    lines = []
    script = Script(out, ok(), write="half.py")
    report = run("fix the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
                 progress=lines.append)
    assert script.specs[1].resume_session == "" and report.status == "done"
    started = [e["data"] for e in store.events(task_id=report.task_id) if e["kind"] == "attempt_started"]
    assert started[1]["fresh"] == "attempt 1 kept no session to continue"
    assert f"locks: #{report.task_id} attempt 2 starts fresh: attempt 1 kept no session to continue" in lines


@pytest.mark.usefixtures("no_verifier")
def test_more_effort_on_the_same_model_continues_its_session_as_a_fork(git_repo, store):
    shallow = WorkerResult(False, Failure.CODE_BUG, "2 tests fail", evidence="FAIL test_total: 3 != 4",
                           cost_usd=0.1, session_id="sess-1")
    lines = []
    script = Script(shallow, WorkerResult(True, None, "fixed", "1 passed", cost_usd=0.1, session_id="sess-2"),
                    write="half.py")
    report = run("fix the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
                 progress=lines.append)
    assert report.status == "done" and report.moves[0].startswith("more_effort to sonnet/high")
    first, second = script.specs
    assert not first.fork_session and first.resume_session == ""
    assert second.resume_session == "sess-1" and second.fork_session and second.cell == Cell("sonnet", "high")
    assert second.launch_dir == first.launch_dir and second.max_turns == first.max_turns
    assert second.resume_prompt == launch.DEEPER_PROMPT and "FAIL test_total: 3 != 4" in second.resume_note
    argv = launch.build_argv(second)
    assert argv[argv.index("--resume") + 1] == "sess-1" and "--fork-session" in argv
    assert argv[argv.index("--effort") + 1] == "high"
    # the move says it continued, and from which session; the attempt says what it did
    moved = store.last_event(report.task_id, "moved")["data"]
    assert moved["continued"] is True and moved["forked_from"] == "sess-1" and "fresh" not in moved
    finished = [e["data"] for e in store.events_of(report.task_id, "attempt_finished")]
    assert [f["continued"] for f in finished] == [False, True]
    assert [a["session_id"] for a in store.attempts(report.task_id)] == ["sess-1", "sess-2"]
    assert f"locks: #{report.task_id} attempt 2 continues attempt 1's session (sess-1) at sonnet/high" in lines


@pytest.mark.usefixtures("no_verifier")
def test_a_fork_the_cli_refuses_starts_fresh_and_says_so(git_repo, store):
    shallow = WorkerResult(False, Failure.CODE_BUG, "2 tests fail", cost_usd=0.1, session_id="sess-1")
    refused = WorkerResult(True, None, "fixed", "1 passed", cost_usd=0.1, session_id="sess-2",
                           fresh="the CLI could not resume session sess-1: No conversation found")
    lines = []
    script = Script(shallow, refused, write="half.py")
    report = run("fix the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
                 progress=lines.append)
    assert report.status == "done" and script.specs[1].resume_session == "sess-1"
    finished = [e["data"] for e in store.events_of(report.task_id, "attempt_finished")]
    assert [f["continued"] for f in finished] == [False, False]
    assert finished[1]["fresh"] == "the CLI could not resume session sess-1: No conversation found"
    assert (f"locks: #{report.task_id} attempt 2 started fresh: the CLI could not resume session sess-1: "
            "No conversation found") in lines


@pytest.mark.usefixtures("no_verifier")
def test_a_move_to_another_model_or_a_retry_never_inherits_the_session(git_repo, store):
    wrong = WorkerResult(False, Failure.APPROACH, "rewrote the wrong module", cost_usd=0.1, session_id="sess-1")
    broke = WorkerResult(False, Failure.ENVIRONMENT, "no JSON", transient=True, session_id="sess-2")
    script = Script(wrong, broke, ok(), write="wrong.py")
    report = run("fix the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert [m.split(" ")[0] for m in report.moves] == ["next_model", "retry"]
    assert [s.resume_session for s in script.specs] == ["", "", ""] and not any(s.fork_session for s in script.specs)
    # a new model starts from the base, with the work set aside named in its brief
    assert "reset to the base commit" in script.specs[1].prompt
    moved = [e["data"] for e in store.events_of(report.task_id, "moved")]
    assert [m["continued"] for m in moved] == [False, False] and not any("fresh" in m for m in moved)


def test_more_effort_without_a_session_starts_fresh_and_the_move_says_why(git_repo, store):
    lines = []
    script = Script(bad(Failure.CODE_BUG, "2 tests fail"), ok())
    report = run("fix the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
                 progress=lines.append)
    assert script.specs[1].resume_session == "" and report.status == "done"
    moved = store.last_event(report.task_id, "moved")["data"]
    assert moved["continued"] is False and moved["fresh"] == "attempt 1 kept no session to continue"
    assert f"locks: #{report.task_id} attempt 2 starts fresh: attempt 1 kept no session to continue" in lines


def test_a_resume_a_person_starts_starts_fresh_whatever_its_last_attempt_kept(git_repo, store):
    from locks.escalate import Attempt

    shallow = WorkerResult(False, Failure.CODE_BUG, "2 tests fail", cost_usd=0.1, session_id="sess-1")
    first = run("fix the export", git_repo, store, Options(max_attempts=1), registry={}, launcher=Script(shallow),
                classifier=kind("implement"))
    assert first.status == "failed" and store.attempts(first.task_id)[0]["session_id"] == "sess-1"
    script = Script(ok())
    run("fix the export", git_repo, store, Options(start=Cell("sonnet", "high")), registry={}, launcher=script,
        classifier=kind("implement"), task_id=first.task_id,
        history=[Attempt(Cell("sonnet", "medium"), 30, False, Failure.CODE_BUG, "2 tests fail")])
    assert script.specs[0].resume_session == "" and not script.specs[0].fork_session


def test_a_session_is_continued_only_where_the_cell_and_the_model_still_match_it(monkeypatch):
    from locks import models
    from locks.escalate import Decision, Move
    from locks.orchestrate import WorkerSession, _continues

    high, medium = Cell("sonnet", "high"), Cell("sonnet", "medium")
    kept = WorkerSession("sess-1", "sonnet", medium, 1)
    effort, turns = Decision(Move.MORE_EFFORT, high, 30), Decision(Move.MORE_TURNS, medium, 60)
    assert _continues(effort, kept) == ("sess-1", "") and _continues(turns, kept) == ("sess-1", "")
    assert _continues(Decision(Move.MORE_TURNS, high, 60), kept) == (
        "", "attempt 1 ran at sonnet/medium, this one at sonnet/high")
    for move in (Move.NEXT_MODEL, Move.RETRY, Move.REPLAN):
        assert _continues(Decision(move, Cell("opus", "high"), 30), kept) == ("", "")
    reshape = Decision(Move.RETRY, medium, 30, reshape=True)
    assert _continues(reshape, kept) == ("sess-1", "")
    assert _continues(Decision(Move.RETRY, high, 30, reshape=True), kept)[0] == ""
    models.pin("sonnet", "claude-sonnet-5-5")  # pinned since: another model runs it
    assert _continues(effort, kept) == ("", "attempt 1 ran on sonnet, this one runs on claude-sonnet-5-5")


def test_a_run_in_the_person_s_checkout_keeps_no_session(tmp_path, store):
    folder = tmp_path / "plain"
    folder.mkdir()
    script = Script(ok())
    run("fix the export", folder, store, registry={}, launcher=script, classifier=kind("implement"))
    assert not script.specs[0].persist_session


def _ran(store, key, kind_name, *attempts):
    task = store.create_task(f"{kind_name} work", status="done", source="locks", repo=key, kind=kind_name)
    for turns, failure in attempts:
        store.add_attempt(task["id"], cell="sonnet/high", max_turns=turns, passed=int(failure is None),
                          failure=failure)


def test_history_of_tasks_that_outgrew_their_turns_raises_where_turns_start(git_repo, store):
    from locks import repo as repos

    key = repos.key(git_repo)
    _ran(store, key, "implement", (80, None))
    _ran(store, key, "implement", (80, "turns_exhausted"), (160, None))
    _ran(store, key, "implement", (80, "turns_exhausted"), (200, None))
    _ran(store, key, "implement", (80, "turns_exhausted"), (160, None))
    _ran(store, key, "refactor", (80, "turns_exhausted"), (160, None))  # another kind: not this one's
    script = Script(ok())
    report = run("fix the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert script.specs[0].max_turns == 160
    assert "starts with 160 turns: 3 of the last 4 implement tasks here needed more than 80" in report.plan.reasons
    # an explicit --max-turns wins, and history never lowers the kind's own
    pinned = Script(ok())
    run("fix the import", git_repo, store, Options(max_turns=30), registry={}, launcher=pinned,
        classifier=kind("implement"))
    assert pinned.specs[0].max_turns == 30
    feature = Script(ok())
    report = run("add the export", git_repo, store, registry={}, launcher=feature, classifier=kind("feature"))
    assert feature.specs[0].max_turns == 120 and not any("needed more than" in r for r in report.plan.reasons)


def test_one_task_that_needed_more_turns_is_no_history(git_repo, store):
    from locks import repo as repos

    key = repos.key(git_repo)
    _ran(store, key, "implement", (80, "turns_exhausted"), (160, None))
    _ran(store, key, "implement", (80, None))
    script = Script(ok())
    report = run("fix the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert script.specs[0].max_turns == 80 and not any("needed more than" in r for r in report.plan.reasons)


def test_a_dead_end_that_shares_only_a_word_or_two_stays_out_of_the_brief(git_repo, store):
    from locks import repo as repos

    key = repos.key(git_repo)
    near = store.open_problem("npm audit fix breaks the lockfile", repo=key)
    store.add_fix(near, "ran npm audit fix --force", "failed", repo=key, why="it jumped a major version")
    far = store.open_problem("remove the old signup flow from the shop", repo=key)
    store.add_fix(far, "deleted the signup folder", "failed", repo=key, why="routes still pointed at it")
    script = Script(ok())
    run("fix the npm audit vulnerabilities in the shop lockfile", git_repo, store, registry={},
        launcher=script, classifier=kind("implement"))
    prompt = script.specs[0].prompt
    assert "ran npm audit fix --force" in prompt
    assert "deleted the signup folder" not in prompt


def test_a_resumed_task_numbers_its_attempts_on_from_its_last(git_repo, store):
    stopped = WorkerResult(False, Failure.ENVIRONMENT, "the CLI needs a newer Node", "", ("NOTES.md",), 0.1)
    first = run("add the notes file", git_repo, store, registry={}, launcher=Script(stopped),
                classifier=kind("implement"))
    lines, seen = [], []

    def launcher(spec):
        seen.append(spec.env["LOCKS_WORKER_ATTEMPT"])
        return ok()

    again = run("add the notes file", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"),
                task_id=first.task_id, progress=lines.append)
    n = first.task_id
    assert f"locks: #{n} attempt 2 at sonnet/medium" in lines and lines[-1] == f"locks: #{n} attempt 2 passed"
    assert seen == ["2"] and again.status == "done"
    assert [e["data"]["seq"] for e in store.events_of(n, "attempt_started")] == [1, 2]


def test_one_run_at_a_time_works_in_a_checkout_without_a_worktree(git_repo, store, monkeypatch):
    import threading

    from locks import isolate
    from locks.store import home

    monkeypatch.setattr(isolate, "WAIT_POLL_S", 0.05)
    held, release = threading.Event(), threading.Event()

    def holder():
        with isolate.in_place(home(), git_repo, 41, waiting=lambda who: None, check=lambda: None):
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(5)
    lines = []

    def said(line):
        # The holder lets go only once the run says it waits: no race on timing.
        lines.append(line)
        if "waits" in line:
            release.set()

    report = run("create the routes", git_repo, store, Options(isolate=False), registry={},
                 launcher=Script(ok()), classifier=kind("implement"), progress=said)
    thread.join()
    assert report.status == "done"
    assert any("waits: task #41 is working in this checkout" in line for line in lines)
    assert store.last_event(report.task_id, "waiting") is not None
    assert isolate.checkout_lock(home(), git_repo).read_text() == ""


def test_a_cancel_while_waiting_for_the_checkout_stops_the_run(git_repo, store, monkeypatch):
    from locks import isolate
    from locks.store import home

    monkeypatch.setattr(isolate, "WAIT_POLL_S", 0.05)
    def launcher(spec):
        raise AssertionError("a run that never got the checkout launches nothing")

    with isolate.in_place(home(), git_repo, 7, waiting=lambda who: None, check=lambda: None):
        def asked(line):
            if "waits" in line:
                store.request_cancel(store.list_tasks()[0]["id"])

        report = run("create the routes", git_repo, store, Options(isolate=False), registry={},
                     launcher=launcher, classifier=kind("implement"), progress=asked)
    assert report.status == "cancelled"


@pytest.mark.usefixtures("no_verifier")
def test_a_run_in_a_worktree_never_waits_for_the_checkout(git_repo, store):
    from locks import isolate
    from locks.store import home

    with isolate.in_place(home(), git_repo, 7, waiting=lambda who: None, check=lambda: None):
        report = run("create the routes", git_repo, store, registry={}, launcher=Script(ok(), write="a.py"),
                     classifier=kind("implement"))
    assert report.status == "done"


def test_a_refusal_the_worker_got_past_does_not_stop_a_climb(git_repo, store):
    got_past = WorkerResult(False, Failure.TURNS_EXHAUSTED, "out of turns", cost_usd=0.1,
                            changed_paths=("routes.ts",), overcome=("Write(routes.ts)",))
    report = run("x", git_repo, store, registry={}, launcher=Script(got_past, ok()), classifier=kind("implement"))
    assert report.status == "done"
    assert report.moves[0].startswith("more_turns")
    finished = next(e for e in store.events(task_id=report.task_id) if e["kind"] == "attempt_finished")
    assert finished["data"]["refused_but_written"] == ["Write(routes.ts)"]


def test_servers_named_by_a_port_that_answers_need_no_warning():
    import socket

    from locks import orchestrate

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert orchestrate._unmet(f"the dev server is up at localhost:{port}", {}) == []
    warned = orchestrate._unmet(f"the dev server is up at localhost:{port}", {})
    assert len(warned) == 1
    assert "running servers" in warned[0]
    assert orchestrate._unmet("start the dev server", {})  # no port named: still said
    # A check that needs no server (lint, tests, a build) is not told to start one for a word in the
    # task's text; one that talks to a server still is, with the words that set it off.
    checks = "npm run lint && npm test && npm run build"
    assert orchestrate._unmet("the app runs with npm run dev", {}, verify=checks) == []
    said = orchestrate._unmet("the app runs with npm run dev", {}, verify="npx playwright test")
    assert len(said) == 1 and "mentions 'npm run dev'" in said[0]
    # A task that tells the worker to start its servers needs them, whatever its check says, and its
    # worker starts them: the plan says so and suggests no --service.
    said = orchestrate._unmet("Start both dev servers, then fix the 500 /api/cart returns", {}, verify="npm test")
    assert len(said) == 1 and "the worker starts them itself" in said[0] and "--service" not in said[0]
    said = orchestrate._unmet("Levanta el backend y el front con npm run dev y corre los flujos de la API", {},
                              verify="npm test")
    assert len(said) == 1 and "'Levanta'" in said[0] and "--service" not in said[0]
    said = orchestrate._unmet("Fix the cart page; start the servers yourself and check it", {})
    assert len(said) == 1 and "the worker starts them itself" in said[0]
    assert orchestrate._unmet("Fix the cart page; start the servers yourself and check it", {}, served=True) == []
    assert not orchestrate._START_SERVERS.search("Run the backend tests and fix the ones that fail")
    # A host and port named as context, with a check that talks to no server, is no need. The port is
    # one nothing listens on, so the test never depends on what the host runs.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    context = f"The API at localhost:{free} returns a 500 for /cart; fix the handler and its test"
    assert orchestrate._unmet(context, {}, verify="npm run lint && npm test && npm run build") == []
    assert orchestrate._unmet(context, {}, verify=f"deno test -A --allow-net=localhost:{free}") == []
    assert orchestrate._unmet(context, {}, verify=f"curl -f localhost:{free}/cart")
    assert orchestrate._unmet("the app runs with npm run dev", {}, verify="pytest tests/e2e_api")


@pytest.mark.parametrize("text", ["TASK: Final browser validation (READ-ONLY)", "Valida el flujo, solo lectura",
                                  "do not edit any files, just report", "no modifiques ningún archivo",
                                  "THIS IS A READ-ONLY VALIDATION.",
                                  "READ-ONLY: do not edit, create or delete any file",
                                  "Do not modify any repo file.", "Investigate (read-only) a timezone bug",
                                  "una tarea de solo lectura"])
def test_a_task_that_says_it_changes_nothing_is_read_only(text):
    from locks import orchestrate

    assert orchestrate._READ_ONLY.search(text)


@pytest.mark.parametrize("text", [
    "fix the reader so it reads only once",
    "guardar crea una nueva version; historial de versiones solo lectura.",
    "Spanish, matching the README's existing style. Do not change code. Commit it.",
    "use the --space-* tokens; Do NOT edit any file under src/app/features/admin",
    "three independent read-only investigations found the bug",
])
def test_a_task_that_writes_is_not_read_only(text):
    """Each was a task that had to write, told by its brief that it could not."""
    from locks import orchestrate

    assert orchestrate._READ_ONLY.search(text) is None


@pytest.mark.parametrize("text", [
    'Fix the refute step. Its brief said "This task is read-only: find out" and it changed no file.',
    "Fix the bug. Evidence: task #4 (a read-only review) changed no file.",
    "Fix it (the earlier task was read-only: it did not edit any files) and add a test.",
    "Fix it: the step said \u201cdo not edit any file\u201d and changed none.",
    "Fix it: the step said `read-only:` in its brief.",
])
def test_quoted_or_parenthesized_evidence_about_another_task_is_not_a_read_only_task(text):
    """A fix task quoting what the observed step did ("read-only", "changed no file") was read as
    changing nothing and briefed so; only the task's own words count."""
    from locks import orchestrate

    # Each case is read-only to the bare pattern: it is the stripping that makes it a task that writes.
    assert orchestrate._READ_ONLY.search(text)
    assert orchestrate.declares_read_only("bugfix", text) is False


@pytest.mark.parametrize("text", [
    "Investiga el bug (solo lectura, no edites ning\u00fan archivo)",
    "Find why the payment test fails (read-only: report the cause)",
    "Encuentra por qu\u00e9 falla el test de pagos (solo lectura, solo reporta)",
    "Investigate (read-only) a timezone bug",
])
def test_a_parenthesis_that_opens_with_the_task_s_own_declaration_is_read(text):
    """The aside that opens with the declaration is the task's own, whatever words follow it."""
    from locks import orchestrate

    assert orchestrate.declares_read_only("bugfix", text) is True


def test_a_fix_that_quotes_a_step_s_read_only_evidence_is_briefed_as_one_that_writes(git_repo, store):
    script = Script(ok())
    run('Fix the refute step, which ended "read-only" (read-only review) and changed no file',
        git_repo, store, registry={}, launcher=script, classifier=kind("test"))
    assert "This task is read-only" not in script.specs[0].prompt


def test_a_task_s_own_words_still_make_it_read_only():
    from locks import orchestrate

    assert orchestrate.declares_read_only("test", "Validate the flow (READ-ONLY)") is True
    assert orchestrate.declares_read_only("feature", "Validate the flow (READ-ONLY)") is False


def test_a_feature_that_names_something_read_only_still_writes(git_repo, store):
    """A feature asking for a read-only version history was briefed as read-only,
    three times, and stopped each time as a task found wrong."""
    script = Script(ok())
    run("Add the version history view (solo lectura)", git_repo, store, registry={},
        launcher=script, classifier=kind("feature"))
    assert "This task is read-only" not in script.specs[0].prompt


def test_a_validation_that_says_it_is_read_only_is_read_as_one(git_repo, store):
    """A browser validation filed as `test` found a real bug in the app, twice, and
    climbed to opus as if the worker had failed."""
    said = "check 7 fails: the quiz bank is missing"
    script = Script(bad(Failure.CODE_BUG, said), bad(Failure.CODE_BUG, said + "."))
    report = run("Validate the student path in the browser (READ-ONLY)", git_repo, store, registry={},
                 launcher=script, classifier=kind("test"))
    assert "what you found is broken" in script.specs[0].prompt
    assert report.stop.cause == "converged"
    assert len(report.cells) == 2


@pytest.mark.usefixtures("no_verifier")
def test_the_kept_commit_carries_the_line_the_worker_proposed(git_repo, store):
    script = Script(WorkerResult(True, None, "routes.js serves /health", "1 passed", commit="Serve a health route"),
                    write="routes.js")
    report = run("Repo api: we need some way to know it is alive", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"))
    message = git(git_repo, "log", "-1", "--format=%B", report.branch)
    assert message.splitlines()[0] == "Serve a health route"
    assert "routes.js serves /health" in message and "Locks-Verified" not in message


# --- what a worktree holds ----------------------------------------------------------------


def test_work_a_reset_throws_away_is_kept_on_a_ref_and_the_brief_says_so(git_repo, store):
    script = Script(bad(Failure.APPROACH, "rewrote the wrong module"), bad(Failure.APPROACH, "still wrong"),
                    write="wrong.py")
    report = run("refactor auth flow", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    second = script.specs[1].prompt
    assert "reset to the base commit" in second and "refs/locks/aside/task-1-1" in second
    assert "Take what is right from them, not their approach" in second
    # The run ended for good: nothing resumes from what was set aside, so it is gone.
    assert report.branch is None and git(git_repo, "for-each-ref", "refs/locks/aside") == ""


@pytest.mark.usefixtures("no_verifier")
def test_a_task_naming_uncommitted_files_is_warned_and_carry_brings_them(git_repo, store):
    (git_repo / "app.py").write_text("print('work in progress')\n")
    said = []
    script = Script(ok())
    run("finish the change in app.py", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
        progress=said.append)
    assert any("uncommitted changes in your checkout" in s for s in said)
    assert "app.py" in script.specs[0].prompt and "say so (`environment`)" in script.specs[0].prompt
    seen = []

    def launcher(spec):
        seen.append((spec.target_dir / "app.py").read_text())
        return ok()

    report = run("finish the change in app.py", git_repo, store, Options(carry=True), registry={},
                 launcher=launcher, classifier=kind("implement"))
    assert seen == ["print('work in progress')\n"]
    assert store.events_of(report.task_id, "carried")[0]["data"]["paths"] == ["app.py"]
    assert "work in progress" in git(git_repo, "show", f"{report.branch}:app.py")


def test_ignored_paths_the_task_names_are_copied_into_the_worktree(git_repo, store):
    (git_repo / ".gitignore").write_text("scripts/slides/\nbig.bin\n")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-q", "-m", "ignore")
    (git_repo / "scripts" / "slides").mkdir(parents=True)
    (git_repo / "scripts" / "slides" / "deck.js").write_text("slides\n")
    (git_repo / "big.bin").write_bytes(b"x" * 10)
    seen = []

    def launcher(spec):
        seen.append(((spec.target_dir / "scripts" / "slides" / "deck.js").is_file(), "too big to copy" in spec.prompt))
        return ok()

    import locks.isolate as isolate_mod

    original = isolate_mod.BRING_BYTES
    isolate_mod.BRING_BYTES = 5
    try:
        report = run("update the deck in scripts/slides/ and read big.bin", git_repo, store, registry={},
                     launcher=launcher, classifier=kind("implement"))
    finally:
        isolate_mod.BRING_BYTES = original
    assert seen == [(False, True)]  # 7 bytes is over a 5-byte cap: said, not copied
    assert report.status == "done"
    script = Script(ok())
    run("update the deck in scripts/slides/", git_repo, store, registry={}, launcher=script,
        classifier=kind("implement"))
    assert "Copied from the person's checkout" in script.specs[0].prompt and "scripts/slides" in script.specs[0].prompt
    assert "scripts/slides" not in git(git_repo, "ls-tree", "-r", "--name-only", "HEAD")


def test_a_sibling_directory_reaches_the_worker(git_repo, store, tmp_path):
    sibling = tmp_path / "api"
    sibling.mkdir()
    script = Script(ok())
    run("align the client with the api", git_repo, store, Options(add_dirs=(str(sibling),)), registry={},
        launcher=script, classifier=kind("implement"))
    assert sibling.resolve() in script.specs[0].extra_dirs
    assert Options(add_dirs=("x",)).stored()["add_dirs"] == ["x"]


def test_the_brief_says_which_node_is_on_path_and_which_commands_pass(git_repo, store, monkeypatch):
    from locks import orchestrate, runtime

    monkeypatch.setattr(runtime, "node_bin", lambda workdir, env=None: runtime.Node("22.23.2", git_repo, ".nvmrc"))
    script = Script(ok())
    run("fix the cart total", git_repo, store, Options(allow_tools=("Bash(npm test:*)", "mcp__x")), registry={},
        launcher=script, classifier=kind("implement"))
    prompt = script.specs[0].prompt
    assert "Node 22.23.2 (what .nvmrc asks for) is already first on your PATH" in prompt
    assert "Never `source ~/.nvm/nvm.sh`" in prompt and "in these forms only, one per call: Bash(npm test:*)" in prompt
    assert "mcp__x" not in prompt.split("in these forms only")[1].splitlines()[0]
    monkeypatch.setattr(runtime, "node_bin", lambda workdir, env=None: None)
    assert orchestrate.how_to_run(git_repo, ()) == []


def test_a_second_attempt_starts_whatever_the_first_cost(git_repo, store):
    script = Script(bad(Failure.CODE_BUG, "missed a case", cost=4.85), ok())
    report = run("fix the cart total", git_repo, store, Options(), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert len(script.specs) == 2 and report.status == "done" and report.stop is None


def test_an_index_that_failed_at_this_commit_is_not_restarted_by_each_task(git_repo, store, tmp_path, monkeypatch):
    adapter = _livespec(tmp_path, popen=lambda argv, **kw: pytest.fail("no refresh while it still fails"))
    monkeypatch.setattr(adapter, "last_failure", lambda repo_dir: "Workspace directory not found: /old")
    report = run("parse_amount rounds wrong", git_repo, store, Options(dry_run=True), registry={},
                 classifier=kind("implement"), adapters=[adapter])
    assert any("its last index here failed (Workspace directory not found: /old); not tried again" in r
               for r in report.plan.reasons)


def test_an_attempt_keeps_the_start_and_the_end_of_long_evidence():
    from locks.orchestrate import kept_evidence

    assert kept_evidence("short") == "short"
    long = "start " + "x" * 5000 + " the last line"
    kept = kept_evidence(long)
    assert len(kept) <= 4000 and kept.startswith("start ") and kept.endswith(" the last line") and "\n…\n" in kept


def _dropped(summary="the session errored: API Error: Connection error."):
    return WorkerResult(False, Failure.ENVIRONMENT, summary, transient=True, network="Connection error", cost_usd=0.1)


@pytest.mark.usefixtures("no_verifier")
def test_a_network_drop_waits_and_goes_on_from_the_work_it_kept(git_repo, store, monkeypatch):
    from locks import orchestrate

    slept = []
    monkeypatch.setattr(orchestrate, "_sleep", slept.append)
    script = Script(_dropped(), ok("finished it"), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and len(script.specs) == 2
    # The same cell again, after the first wait, and the second try sees what the first one wrote.
    assert script.specs[1].cell == script.specs[0].cell and sum(slept) == 30
    # The wait is a phase of its own, never "the rest of the run".
    assert "wait_s" in store.last_event(report.task_id, "finished")["data"]
    moved = store.last_event(report.task_id, "moved")["data"]
    assert moved["move"] == "retry" and moved["wait_s"] == 30 and moved["network"] == "Connection error"


def test_a_network_that_stays_down_stops_as_the_network_with_the_work_kept(git_repo, store, monkeypatch):
    from locks import orchestrate, stops
    from locks.escalate import NETWORK_WAITS

    monkeypatch.setattr(orchestrate, "_sleep", lambda s: None)
    drops = len(NETWORK_WAITS) + 1
    script = Script(*[_dropped() for _ in range(drops)], write="app.py")
    # More drops than the attempt ceiling allows attempts: a try after a drop is no attempt at the work.
    report = run("fix the cart total", git_repo, store, Options(max_attempts=2), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert len(script.specs) == drops and report.status == "blocked"
    assert report.stop.cause == "network" and stops.WHO[report.stop.who] == "the network" and report.branch
    assert "resume it once the network is back" in stops.what_to_do(report.stop)


def test_a_check_that_lost_the_network_is_no_bug_in_the_work(tmp_path):
    from locks import orchestrate

    lost = orchestrate._verified(ok(), "echo 'npm ERR! code ENOTFOUND registry.npmjs.org'; exit 1", tmp_path)
    assert lost.failure is Failure.ENVIRONMENT and lost.network.startswith("npm ERR! code ENOTFOUND")
    assert "lost the network" in lost.summary
    red = orchestrate._verified(ok(), "echo '1 failed'; exit 1", tmp_path)
    assert red.failure is Failure.CODE_BUG and red.network == ""
    # A test that logs a name that does not resolve, beside a real failure, is the work's failure.
    logged = orchestrate._verified(ok(), "echo 'console.error getaddrinfo ENOTFOUND api.invalid'; "
                                         "echo 'FAIL src/cart.test.ts Expected 10.8 Received 10'; exit 1", tmp_path)
    assert logged.failure is Failure.CODE_BUG and logged.network == ""


JEST_TIMEOUTS = """FAIL src/api.test.ts
  ● recruitment api › lists candidates

    thrown: "Exceeded timeout of 5000 ms for a test.
    Add a timeout value to this test to increase the timeout, if this is a long-running test."

  ● recruitment api › saves a candidate

    thrown: "Exceeded timeout of 5000 ms for a test."

  ● Console

    console.log Exceeded timeout of 5000 ms for a test. (a line a test logs)
"""


def test_a_check_whose_tests_timed_out_after_a_claimed_pass_is_the_load_not_the_work(tmp_path):
    from locks import launch, orchestrate

    (tmp_path / "out.txt").write_text(JEST_TIMEOUTS)
    slow = orchestrate._verified(ok(), "cat out.txt; exit 1", tmp_path)
    assert slow.failure is Failure.ENVIRONMENT and slow.starved == "load"
    assert slow.load.startswith("Exceeded timeout of 5000 ms for a test") and "loaded machine" in slow.summary
    # Mostly real failures, one timeout: the work's.
    mixed = JEST_TIMEOUTS.split("  ● Console")[0] + "  ● cart › total\n\n    Expected 10.8 Received 10\n" * 3
    assert launch.check_timed_out(mixed) == ""
    # Other runners' own lines; never a word a test logs on a line of its own.
    assert launch.check_timed_out("Error: Test timed out in 5000ms.\n × cart > total")
    assert launch.check_timed_out("  1) cart\n     Error: Timeout of 2000ms exceeded. For async tests")
    assert launch.check_timed_out("E   Failed: Timeout >10.0s\nFAILED tests/test_cart.py::test_total - Failed")
    assert launch.check_timed_out("FAILED tests/test_cart.py::test_total - AssertionError: timeout") == ""
    assert launch.check_timed_out("") == ""


def test_tests_that_keep_timing_out_under_load_stop_with_the_work_kept_and_no_attempt_spent(
        git_repo, store, monkeypatch):
    from locks import capacity, orchestrate, stops
    from locks.escalate import LOAD_WAITS
    from locks.store import home

    slept = []
    monkeypatch.setattr(orchestrate, "_sleep", slept.append)
    capacity.settle(store, home(), machine=capacity.Machine(4, None, None), ceiling=None, wanting=False)
    out = git_repo.parent / "out.txt"  # outside the checkout: the check runs in the task's worktree
    out.write_text(JEST_TIMEOUTS)
    tries = len(LOAD_WAITS) + 1
    script = Script(*[ok("467/467 passed") for _ in range(tries)], write="app.py")
    # More tries than the attempt ceiling allows attempts: a try timed out by the load is no attempt.
    report = run("fix the cart total", git_repo, store, Options(max_attempts=2, verify=f"cat {out}; exit 1"),
                 registry={}, launcher=script, classifier=kind("implement"))
    assert len(script.specs) == tries and {s.cell for s in script.specs} == {script.specs[0].cell}
    assert report.status == "blocked" and report.stop.cause == "load" and report.branch
    assert stops.WHO[report.stop.who] == "the environment" and "machine is quieter" in stops.what_to_do(report.stop)
    assert sum(slept) == sum(LOAD_WAITS)
    moved = store.events_of(report.task_id, "moved")[0]["data"]
    assert moved["load"].startswith("Exceeded timeout") and moved["wait_s"] == LOAD_WAITS[0]
    assert not store.dead_ends(limit=5)  # a machine's load is no dead end
    # The machine's level halves on it.
    level = capacity.settle(store, home(), machine=capacity.Machine(4, None, None), ceiling=None, wanting=False)
    assert level.value == 2 and level.reason == "halved, a check's tests timed out under load"


def test_a_whole_check_that_timed_out_after_a_claimed_pass_is_the_load_and_never_climbs(
        git_repo, store, monkeypatch):
    from locks import orchestrate
    from locks.escalate import LOAD_WAITS

    monkeypatch.setattr(orchestrate, "_sleep", lambda s: None)
    monkeypatch.setattr(orchestrate, "_run_check", lambda command, workdir, timeout=None: (None, ""))
    alone = orchestrate._verified(ok(), "npm test", git_repo)
    assert alone.failure is Failure.ENVIRONMENT and alone.starved == "load" and alone.load
    assert "timed out after the claimed pass" in alone.summary
    tries = len(LOAD_WAITS) + 1
    script = Script(*[ok("467/467 passed") for _ in range(tries)], write="app.py")
    report = run("fix the cart total", git_repo, store, Options(max_attempts=2, verify="npm test"),
                 registry={}, launcher=script, classifier=kind("implement"))
    assert len(script.specs) == tries and {s.cell for s in script.specs} == {script.specs[0].cell}
    assert report.status == "blocked" and report.stop.cause == "load" and report.branch
    assert not store.dead_ends(limit=5)


def test_a_task_that_changed_many_files_counts_them_all_on_its_ending(git_repo, store):
    def launcher(spec):
        for i in range(106):
            (spec.target_dir / f"f{i}.py").write_text("x\n")
        return ok("built it")

    report = run("add the portal", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    finished = store.last_event(report.task_id, "finished")["data"]
    assert len(report.changed) == 106 and finished["changed_n"] == 106
    assert finished["changed"] == report.changed
    assert git(git_repo, "diff", "--name-only", "main", finished["branch"]).count("\n") + 1 == 106


def _two_branches(repo, *, conflict: bool) -> str:
    """Two part branches from the base: both change app.py (a conflict), or each its own file."""
    from .conftest import git

    base = git(repo, "rev-parse", "HEAD")
    for n, text in ((901, "api"), (902, "web")):
        git(repo, "branch", f"locks/task-{n}", base)
        git(repo, "checkout", "-q", f"locks/task-{n}")
        (repo / ("app.py" if conflict else f"{text}.py")).write_text(f"print('{text}')\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"{text} part")
        git(repo, "checkout", "-q", "main")
    return base


JOIN = {"branches": [{"part": "api", "task": 901, "branch": "locks/task-901"},
                     {"part": "web", "task": 902, "branch": "locks/task-902"}],
        "contract": "- main() prints one line"}


@pytest.mark.usefixtures("no_verifier")
def test_a_conflict_makes_the_integrate_step_a_worker_told_the_files_the_branch_and_the_contract(git_repo, store):
    from .conftest import git

    base = _two_branches(git_repo, conflict=True)
    told = []

    def resolve(spec):
        told.append(spec.prompt)
        work = spec.target_dir
        subprocess.run(["git", "merge", "--no-edit", "locks/task-902"], cwd=work, capture_output=True)
        (work / "app.py").write_text("print('api and web')\n")
        git(work, "add", "app.py")
        git(work, "commit", "-q", "--no-edit")
        return ok("resolved app.py")

    report = run("Join the parts", git_repo, store, Options(kind="implement", base=base, integrate=JOIN),
                 registry={}, launcher=resolve, classifier=kind("implement"), adapters=[])
    assert report.status == "done" and len(told) == 1
    brief = told[0]
    assert "api merged cleanly" in brief and "Merging `locks/task-902` (the web part) conflicts in: app.py" in brief
    assert "`git merge --no-edit locks/task-902`" in brief and "> - main() prints one line" in brief
    joined = store.last_event(report.task_id, "integrated")["data"]
    assert joined["merged"] == ["locks/task-901"] and joined["conflict"] == "locks/task-902"
    assert joined["files"] == ["app.py"]
    assert git(git_repo, "show", f"{report.branch}:app.py") == "print('api and web')"


@pytest.mark.usefixtures("no_verifier")
def test_a_new_model_on_a_conflicting_join_starts_from_the_parts_that_joined_cleanly(git_repo, store):
    from .conftest import git

    base = _two_branches(git_repo, conflict=True)
    seen = []

    def launcher(spec):
        work = spec.target_dir
        seen.append(((work / "app.py").read_text(), spec.cell.model))
        if len(seen) == 1:
            (work / "app.py").write_text("print('a wrong guess')\n")
            return bad(Failure.APPROACH, "rewrote the wrong module")
        subprocess.run(["git", "merge", "--no-edit", "locks/task-902"], cwd=work, capture_output=True)
        (work / "app.py").write_text("print('api and web')\n")
        git(work, "add", "app.py")
        git(work, "commit", "-q", "--no-edit")
        return ok("resolved app.py")

    report = run("Join the parts", git_repo, store, Options(kind="implement", base=base, integrate=JOIN),
                 registry={}, launcher=launcher, classifier=kind("implement"), adapters=[])
    assert report.status == "done" and len(seen) == 2 and seen[0][1] != seen[1][1]
    # The worktree went back to the base, and the api part, which joined cleanly, was joined again.
    assert seen[1][0] == "print('api')\n"
    assert "Join the api part" in git(git_repo, "log", "--format=%s", report.branch)


@pytest.mark.usefixtures("no_verifier")
def test_a_clean_join_whose_check_fails_goes_to_a_worker_with_the_output(git_repo, store):
    base = _two_branches(git_repo, conflict=False)
    told = []

    def fix(spec):
        told.append(spec.prompt)
        (spec.target_dir / "glue.py").write_text("import api, web\n")
        return ok("glued")

    report = run("Join the parts", git_repo, store,
                 Options(kind="implement", base=base, integrate=JOIN, verify="test -f glue.py && echo joined"),
                 registry={}, launcher=fix, classifier=kind("implement"), adapters=[])
    assert report.status == "done" and len(told) == 1
    assert "merged cleanly (api, web), but the run's check `test -f glue.py && echo joined` fails" in told[0]
    # A clean join whose check passes needs no worker at all.
    clean = run("Join the parts again", git_repo, store,
                Options(kind="implement", base=base, integrate=JOIN, verify="test -f api.py && test -f web.py"),
                registry={}, launcher=lambda spec: pytest.fail("no worker for a clean join"),
                classifier=kind("implement"), adapters=[])
    assert clean.status == "done" and "joined 2 parts cleanly (api, web)" in clean.summary
    assert "`test -f api.py && test -f web.py` passed on the joined work" in clean.summary
    assert sorted(clean.changed) == ["api.py", "web.py"] and clean.branch


def test_an_integrate_step_with_a_branch_that_is_gone_stops_for_the_environment(git_repo, store):
    from .conftest import git

    base = git(git_repo, "rev-parse", "HEAD")
    report = run("Join the parts", git_repo, store, Options(kind="implement", base=base, integrate=JOIN),
                 registry={}, launcher=lambda spec: pytest.fail("no worker"), classifier=kind("implement"),
                 adapters=[])
    assert report.status == "blocked" and report.stop.cause == "environment"
    assert "could not join the parts" in report.stop.reason


# --- points: a task's size, estimated when planned and measured when it ends -------------------

@pytest.mark.usefixtures("no_verifier")
def test_a_task_is_scored_when_planned_and_measured_when_it_ends(git_repo, store):
    text = "Add the export:\n- a CSV writer\n- a header row\n- an --out flag\n- a test for it"
    report = run(text, git_repo, store, registry={}, launcher=Script(bad(Failure.CODE_BUG), ok(), write="export.py"),
                 classifier=kind("implement"), adapters=[])
    task = store.get_task(report.task_id)
    assert (task["points"], task["points_why"]) == (3, "4 criteria")
    planned = store.last_event(report.task_id, "planned")["data"]
    assert planned["points"] == 3 and planned["points_why"] == ["4 criteria"]
    assert report.plan.points.line() == "3: 4 criteria"
    assert "points 3: 4 criteria" in report.text()
    finished = store.final_event(report.task_id)["data"]
    # one file, one line, two attempts: git and the attempts say it, never the worker
    assert finished["points"] == 3 and finished["realized"] == 2 and finished["realized_why"] == ["2 attempts"]
    dry = run(text, git_repo, store, Options(dry_run=True), registry={}, classifier=kind("feature"), adapters=[])
    assert "points 5: 4 criteria, kind feature" in dry.text()


def test_the_symbols_livespec_reaches_weigh_on_the_points(git_repo, store, tmp_path):
    from .livespec_fixture import build

    build(git_repo)
    dry = run("parse_amount rounds wrong", git_repo, store, Options(dry_run=True), registry={},
              classifier=kind("implement"), adapters=[_livespec(tmp_path)])
    assert dry.plan.points.reasons == ("reaches 23 symbols",) and dry.plan.points.points == 3
    unread = run("parse_amount rounds wrong", git_repo, store, Options(dry_run=True), registry={},
                 classifier=kind("implement"), adapters=[])
    assert unread.plan.points.points == 1


def test_a_resume_that_ends_short_keeps_what_the_run_before_it_kept(git_repo, store):
    first = run("migrate the totals", git_repo, store, registry={}, adapters=[],
                launcher=Script(WorkerResult(False, Failure.PERMISSION, "refused", denied=("Bash(x)",)),
                                write="m.py"),
                classifier=kind("implement"))
    assert first.status == "blocked" and first.branch
    again = run("migrate the totals", git_repo, store, registry={}, adapters=[], task_id=first.task_id,
                launcher=Script(bad(Failure.SPEC_BUG, "no such API")), classifier=kind("implement"))
    # The resume found the task wrong; what the run before it kept is still on its branch.
    assert again.status == "replan" and again.branch == first.branch
    assert "m.py" in git(git_repo, "show", "--name-only", "--format=", first.branch)


def test_work_done_that_names_one_decision_is_kept_and_never_called_failed(git_repo, store):
    """A worker that did the work (files changed, its check green) and reports `architecture_bug`
    because one point needs a design decision: the work is kept on its branch, the stop waits on
    the orchestrator's decision, and nothing says it failed."""
    from locks import cli, hooks, stops

    point = "the export works; whether totals round per line or per invoice is a product decision"
    decided = WorkerResult(False, Failure.ARCHITECTURE_BUG, point, evidence="pytest: 12 passed", cost_usd=0.1)
    report = run("add the invoice export", git_repo, store, Options(verify="true"), registry={},
                 launcher=Script(decided, write="export.py"), classifier=kind("implement"), session_id="s")
    assert report.status == "blocked" and report.cells == ["sonnet/medium"]  # no climb, no replan
    assert report.stop.cause == "decision" and report.stop.who == "worker"
    assert stops.for_person(report.stop) is False  # the orchestrator's to decide
    assert report.branch == f"locks/task-{report.task_id}" and report.commit
    assert "export.py" in git(git_repo, "show", "--name-only", report.branch)
    text = report.text()
    assert f"(commit {report.commit[:12]})" in text and "checked by locks' own run of its check" in text
    assert "failed" not in text.lower() and "decide the open point, then merge it" in text
    assert point in report.stop.reason and "the work is done and kept on its branch" in report.stop.reason
    assert cli._exit(report) == cli.EXIT_DECISION != 1
    notice = hooks.endings(store, "s")[0]
    assert f"#{report.task_id} [work done, one decision open]" in notice and "[blocked]" not in notice
    assert f"the work is done and kept on branch {report.branch} (commit {report.commit[:12]})" in notice
    assert f"one point needs a decision; the worker says: {point}" in notice and "failed" not in notice
    assert "(its check passed, run by locks)" in notice
    # With no check given, the worker's own is the evidence, and said as such.
    report = run("add the invoice export", git_repo, store, Options(), registry={},
                 launcher=Script(decided, write="export.py"), classifier=kind("implement"))
    assert report.stop.cause == "decision"
    assert "checked by the worker's own check, not re-run by locks" in report.text()
    # A red check, or nothing changed, leaves the rule for a wrong task as it was.
    report = run("add the invoice export", git_repo, store, Options(verify="false"), registry={},
                 launcher=Script(decided, write="export.py"), classifier=kind("implement"))
    assert report.status == "replan" and report.stop.cause == "spec" and report.branch is None
    report = run("add the invoice export", git_repo, store, Options(verify="true"), registry={},
                 launcher=Script(decided), classifier=kind("implement"))
    assert report.status == "replan" and report.stop.cause == "spec"


@pytest.mark.usefixtures("no_verifier")
def test_a_passing_worker_that_names_a_security_problem_raises_an_alert_with_the_secret_masked(git_repo, store):
    from locks import hooks

    learned = ("The CI reads the registry credential from .npmrc, which is committed: it needs rotation",
               "Run the suite with `npm test`, it takes about a minute")
    found = ({"where": "config/prod.env:3", "what": "api key sk-abcdefghijklmnop exposed in the repo",
              "evidence_class": "deterministic", "cause": "pre-existing", "proof": "grep"},)
    passed = WorkerResult(True, None, "added the route", evidence="1 passed", learned=learned, findings=found)
    report = run("add the health route", git_repo, store, Options(), registry={},
                 launcher=Script(passed, write="health.py"), classifier=kind("implement"), session_id="s")
    assert report.status == "done" and len(report.alerts) == 2
    assert "sk-abcdefghijklmnop" not in report.text() and "[secret]" in report.text()
    assert "⚠ security: The CI reads the registry credential from .npmrc" in report.text()
    notice = hooks.endings(store, "s")[0]
    assert "  ⚠ security: The CI reads the registry credential from .npmrc, which is committed" in notice
    assert "  ⚠ security: config/prod.env:3: api key [secret] exposed in the repo" in notice
    assert "sk-abcdefghijklmnop" not in notice
    assert not any("npm test" in line for line in notice.splitlines() if "⚠" in line)


def test_an_index_failure_is_said_in_one_plan_not_in_every_one(git_repo, store, tmp_path):
    adapter = _livespec(tmp_path, popen=lambda argv, **kw: pytest.fail("no refresh while it still fails"))
    adapter._remember_failure(git_repo, "Workspace directory not found: /old")
    said = []
    for _ in range(3):
        report = run("where is parse_amount", git_repo, store, Options(), registry={}, launcher=Script(ok()),
                     classifier=kind("explore"), adapters=[adapter])
        said.append(any("its last index here failed" in r for r in report.plan.reasons))
    assert said == [True, False, False]
    # A dry run shows it every time, and marks nothing.
    report = run("where is parse_amount", git_repo, store, Options(dry_run=True), registry={},
                 classifier=kind("explore"), adapters=[adapter])
    assert any("its last index here failed" in r for r in report.plan.reasons)


def test_a_task_of_several_pieces_that_outgrows_its_turns_stops_for_a_split_with_its_work_kept(
        git_repo, store, monkeypatch):
    monkeypatch.setattr(orchestrate, "SPLIT_POINTS", 1)
    out = WorkerResult(False, Failure.TURNS_EXHAUSTED, "ran out", changed_paths=("a.py",), cost_usd=0.1)
    report = run("add the control, the PDF notes and the overlaps", git_repo, store, registry={},
                 launcher=Script(out, write="a.py"), classifier=kind("implement"))
    assert report.status == "replan" and report.stop.cause == "split" and report.branch
    assert report.stop.who == "locks" and "split what is left" in report.stop.reason
    # --whole is the session's word that it is one piece: more turns, as before
    again = run("add the control, the PDF notes and the overlaps", git_repo, store, Options(whole=True), registry={},
                launcher=Script(out, ok()), classifier=kind("implement"))
    assert again.status == "done"


def test_a_writing_brief_names_the_tests_near_the_change_to_run_as_it_goes(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest --coverage"}}')
    said = orchestrate.brief("add x", "implement", tmp_path, [], [], "npm test", True)
    assert "run only the tests near your change (npx jest --findRelatedTests <the files you changed>)" in said
    assert "findRelatedTests" not in orchestrate.brief("read x", "explore", tmp_path, [], [], "npm test", False)


def test_the_account_s_usage_limit_stops_the_task_until_it_resets_with_the_work_kept(git_repo, store):
    from locks import flow, stops

    limited = WorkerResult(False, Failure.ENVIRONMENT, "the session errored: Session limit reached ∙ resets 5pm",
                           transient=True, limited="usage", retry_after_s=3600.0, cost_usd=0.1)
    script = Script(limited, write="app.py")
    report = run("fix the cart total", git_repo, store, Options(max_attempts=4), registry={}, launcher=script,
                 classifier=kind("implement"))
    # One try: every cell meets the same limit until it resets.
    assert len(script.specs) == 1 and report.status == "blocked" and report.branch
    assert report.stop.cause == "limit" and report.stop.extra["resets_at"]
    assert "locks resumes it once the limit resets" in stops.what_to_do(report.stop)
    assert stops.lost_tries(store, report.task_id) == 1
    # Nothing queued starts before the reset: every session would meet it.
    queued = store.enqueue("rename a flag", repo=None, cwd=str(git_repo))
    assert flow.limited_until(store) == report.stop.extra["resets_at"]
    assert flow.runnable(store, None, parallel=True, decide=lambda t, a: None) == []
    from locks import dispatch

    assert dispatch.waiting(store, store.get_task(queued["id"])).startswith(
        "waits for the account's usage limit to reset")
    from datetime import datetime, timedelta

    later = datetime.fromisoformat(report.stop.extra["resets_at"]) + timedelta(minutes=1)
    assert flow.limited_until(store, now=later) is None


def test_a_pass_whose_worker_never_ran_its_own_check_says_so_on_its_verdict(git_repo, store):
    from locks import hooks

    out_of_turns = WorkerResult(False, Failure.TURNS_EXHAUSTED, "used all 120 turns without a verdict")
    report = run("remove the old module", git_repo, store, Options(verify="test -f gone.txt"), registry={},
                 launcher=Script(out_of_turns, write="gone.txt"), classifier=kind("refactor"))
    assert report.status == "done" and report.unchecked == "it ran out of turns"
    verdict = store.final_event(report.task_id)["data"]["verdict"]
    assert verdict["unchecked"].startswith("the worker never ran its own check (it ran out of turns)")
    lines = hooks.verdict_lines(verdict)
    assert any(line.startswith("  not self-checked: the worker never ran") for line in lines)
    assert any("open the diff only where it says something was not checked" in line for line in lines)
    plain = run("add gone.txt", git_repo, store, Options(verify="test -f gone.txt"), registry={},
                launcher=Script(ok(), write="gone.txt"), classifier=kind("implement"))
    assert "unchecked" not in store.final_event(plain.task_id)["data"]["verdict"]


def test_a_task_its_surfaces_kept_from_the_cause_offers_a_resume_with_them_widened(git_repo, store, monkeypatch,
                                                                                    capsys):
    from locks import cli, stops

    wrong = WorkerResult(False, Failure.SPEC_BUG, "the root cause is in src/sync/clock.ts, outside my surfaces "
                         "(src/radio/**): I may not change it", verdict="fail",
                         evidence="src/radio/mac.ts:40 reads clock.now() from src/sync/clock.ts")
    report = run("fix the dropped frames in src/radio", git_repo, store, Options(surfaces=("src/radio/**",)),
                 registry={}, launcher=Script(wrong), classifier=kind("bugfix"))
    assert report.status == "replan" and report.stop.cause == "spec"
    assert report.stop.extra["widen"] == ["src/sync/clock.ts"]
    line = stops.next_step(report.task_id, report.stop)
    assert line == f"locks resume {report.task_id} --surface src/sync/clock.ts"
    # without a word about its surfaces, a path the account names is no reason to widen them
    plain = WorkerResult(False, Failure.SPEC_BUG, "the request contradicts src/sync/clock.ts", verdict="fail")
    other = run("fix the frames", git_repo, store, Options(surfaces=("src/radio/**",)), registry={},
                launcher=Script(plain), classifier=kind("bugfix"))
    assert "widen" not in other.stop.extra and stops.next_step(other.task_id, other.stop) is None

    calls = []
    monkeypatch.setattr(orchestrate, "run", lambda text, repo_dir, st, options, **kw: calls.append((options, kw))
                        or orchestrate.Report(kw.get("task_id"), "done", None))
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "ok")
    monkeypatch.setattr(cli, "Store", type("S", (), {"open": staticmethod(lambda: store)}))
    monkeypatch.setattr(store, "close", lambda: None)
    assert cli.main(["resume", str(report.task_id), "--no-model"]) == 1  # a replan without more surfaces
    assert cli.main(["resume", str(report.task_id), "--surface", "src/sync/clock.ts", "--no-model"]) == 0
    options, kw = calls[-1]
    assert options.surfaces == ("src/radio/**", "src/sync/clock.ts")
    assert "its surfaces now take src/sync/clock.ts too" in options.resume_why
    assert kw["history"][0].summary.startswith("the root cause is in src/sync/clock.ts")


def test_lines_a_repository_hears_on_every_task_are_said_once_a_day(git_repo, store):
    lines = ["engram: absent", "CI also runs: e2e (playwright)", "risk medium: payments"]
    assert orchestrate.said_today(store, "r", lines, today="2026-10-24") == frozenset()
    assert orchestrate.said_today(store, "r", lines, today="2026-10-24") == {"engram: absent",
                                                                             "CI also runs: e2e (playwright)"}
    assert orchestrate.said_today(store, "other", lines, today="2026-10-24") == frozenset()
    assert orchestrate.said_today(store, "r", lines, today="2026-10-25") == frozenset()
    assert orchestrate.repeats("role ui: capability browser is not in your registry or an adopted neighbour here")
    # Every step of a workflow that reaches a sibling repository planned it again.
    assert orchestrate.repeats("this task spans 2 repositories (api, web): tracked apart, each keeps its own notes")

    from locks.classify import Classification

    plan = orchestrate.Plan("implement", Classification("implement"), (), orchestrate.Cell.parse("sonnet/medium"),
                            ["CI also runs: e2e", "first change"], (), quiet=frozenset({"CI also runs: e2e"}))
    text = orchestrate.Report(7, "done", plan).text()
    assert "CI also runs" not in text and "first change" in text
    assert "(1 note(s) this repository already heard today left out: `locks show 7` keeps them)" in text


@pytest.mark.usefixtures("no_verifier")
def test_a_problem_its_task_left_open_is_solved_when_a_resume_passes_its_first_attempt(git_repo, store):
    first = run("fix the ble race in the scanner", git_repo, store, Options(max_attempts=1), registry={},
                launcher=Script(bad(Failure.CODE_BUG, "the race is in the scanner callback")),
                classifier=kind("bugfix"))
    [problem] = store.recent_problems(limit=5)
    assert problem["state"] == "open"
    again = run("fix the ble race in the scanner", git_repo, store, Options(kind="bugfix"), registry={},
                launcher=Script(ok("serialized the callback"), write="scan.py"), task_id=first.task_id)
    assert again.status == "done"
    assert store.problem(problem["id"])["state"] == "solved"


@pytest.mark.usefixtures("no_verifier")
def test_a_later_task_whose_text_matched_an_open_problem_solves_it(git_repo, store):
    key = __import__("locks.repo", fromlist=["key"]).key(git_repo)
    problem = store.open_problem("esp-now frames dropped on the macos bridge", repo=key,
                                 symptom="esp-now frames dropped on the macos bridge under load")
    store.add_fix(problem, "sonnet/high: retried the send", "failed", repo=key, why="code_bug", task_id=999)
    report = run("fix the esp-now frames dropped on the macos bridge", git_repo, store, Options(kind="bugfix"),
                 registry={}, launcher=Script(ok("acked every frame"), write="bridge.py"))
    assert report.status == "done"
    found = store.problem(problem)
    assert found["state"] == "solved" and found["fixes"][-1]["task_id"] == report.task_id
    # a different subject is never closed by it
    other = store.open_problem("landing page claims no binaries", repo=key)
    store.add_fix(other, "x", "failed", repo=key, task_id=998)
    run("fix the esp-now bridge retries", git_repo, store, Options(kind="bugfix"), registry={},
        launcher=Script(ok(), write="b2.py"))
    assert store.problem(other)["state"] == "open"


def test_work_failing_on_a_solved_problem_under_another_title_marks_it_recurring(store):
    problem = store.open_problem("esp-now flaky test on macos", repo="r", symptom="esp-now flaky test on macos")
    store.add_fix(problem, "awaited the ack", "worked", repo="r", task_id=1)
    assert store.regressed("the esp-now flaky test on macos fails again on CI", repo="r", task_id=2) == [problem]
    found = store.problem(problem)
    assert found["state"] == "recurring" and found["fixes"][0]["invalidated_on"]
    assert "task #2 met it again" in found["fixes"][0]["why"]
    assert store.regressed("the esp-now flaky test on macos", repo="other", task_id=3) == []


def test_a_task_about_a_readme_in_a_repository_with_several_is_warned(git_repo, store):
    (git_repo / "README.md").write_text("# upstream\n")
    (git_repo / "bench").mkdir()
    (git_repo / "bench" / "README.md").write_text("# the fork\n")
    dry = run("update the README with the new install steps", git_repo, store, Options(dry_run=True), registry={},
              classifier=kind("docs"))
    [warned] = [w for w in orchestrate.warnings(dry.plan.reasons) if "README" in w]
    assert "this repository has 2: README.md, bench/README.md" in warned and "the one at the top" in warned
    (git_repo / "bench" / "README.md").unlink()
    again = run("update the README", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("docs"))
    assert not [w for w in orchestrate.warnings(again.plan.reasons) if "README" in w]


@pytest.mark.parametrize("kind", ["explore", "review-routine", "review-critical", "chat"])
@pytest.mark.parametrize("text", ["Implement the export of the monthly report as CSV",
                                  "Add a retry to the upload client and its tests",
                                  "Implementa la exportacion del reporte mensual"])
def test_a_reading_kind_given_to_a_request_that_orders_a_change_is_widened(kind, text):
    """A session's `--kind review` on "Implement X" sent a reading worker work it could only refuse."""
    from locks.orchestrate import _caller_kind

    found = _caller_kind(Options(kind=kind), text)
    assert found.kind == "implement"
    assert f"--kind {kind} widened" in found.reason


@pytest.mark.parametrize(("kind", "text", "options"), [
    ("review-routine", "Review the export module for bugs", {}),
    ("explore", "Implement or not? read the export module first?", {}),
    ("explore", "Implement nothing (read-only): map how the export module works", {}),
    ("review-routine", "#review-routine Implement the export as CSV", {}),
    ("review-routine", "Implement the export as CSV", {"step_role": "reviewer"}),
    ("review-routine", "Implement the export as CSV", {"touches": ("src/export.py",)}),
    ("implement", "Implement the export as CSV", {}),
    ("plan", "Implement the export as CSV", {}),
])
def test_a_given_kind_stays_when_the_request_or_its_source_chose_it(kind, text, options):
    from locks.orchestrate import _caller_kind

    assert _caller_kind(Options(kind=kind, **options), text).kind == kind


def test_the_kind_and_the_neighbours_indexes_are_found_side_by_side(git_repo, store, monkeypatch):
    """Haiku's answer and an index built or refreshed each wait on a process, and neither needs the
    other: a plan waits for the longer one, not for both (a real plan took 13.7 s, 4 + 9)."""
    import threading
    import time

    from locks.adapters import ABSENT, Status

    class Neighbour:
        name = "slow"

        def inspect(self, _repo):
            return Status("slow", ABSENT)

    seen: dict[str, int] = {}

    def indexing(adapter, status, repo_dir, options, say):
        seen["index"] = threading.get_ident()
        time.sleep(0.6)
        return status, ["slow: indexed"]

    def classifying(text):
        seen["classify"] = threading.get_ident()
        time.sleep(0.6)
        return Classification("implement", "medium", "rule", "test")

    monkeypatch.setattr(orchestrate, "_bring_current", indexing)
    monkeypatch.setattr(orchestrate, "adopted", lambda options, adapters: list(adapters or ()))
    began = time.monotonic()
    found = orchestrate.plan("Rename the helper in notes", git_repo, store, Options(), {}, classifier=classifying,
                             adapters=[Neighbour()])
    assert time.monotonic() - began < 1.1 and seen["index"] != seen["classify"]
    assert found.kind == "implement"
