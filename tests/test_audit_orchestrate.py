"""Four defects of the engine seen in locks' own records: a resume's instruction the independent
check never heard, a late check that blocked on what no probe showed, a point on work that stands
dropped as a wrong task, and widening offers that name no file."""
from __future__ import annotations

import subprocess

import pytest

from locks import orchestrate
from locks.escalate import Failure
from locks.launch import WorkerResult
from locks.orchestrate import Options, run, verifier_brief

from .conftest import git
from .test_checking import BUG, check, passing
from .test_orchestrate import Script, bad, kind


@pytest.fixture
def verifier_on(monkeypatch):
    """The independent check as it runs inline, after locks' own check and before the ending."""
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    monkeypatch.setenv("LOCKS_OVERLAP", "off")
    monkeypatch.setenv("LOCKS_LATE_VERIFIER", "off")


@pytest.fixture
def late(monkeypatch, verifier_on):
    """The independent check as it runs after the ending (`late_verifier`)."""
    monkeypatch.setenv("LOCKS_LATE_VERIFIER", "on")


INSTRUCTION = "ship the flat discount, not the percentage the request asks for"


# --- 1. a resume's instruction reaches the independent check ------------------------------


def test_the_verifier_brief_quotes_a_resume_instruction_as_the_sessions_later_decision(tmp_path):
    text = "apply a percentage discount to the cart"
    plain = verifier_brief(text, "implement", tmp_path, "abc123", ["cart.py"])
    assert "instruction for this resume" not in plain and INSTRUCTION not in plain
    brief = verifier_brief(text, "implement", tmp_path, "abc123", ["cart.py"], instruction=INSTRUCTION)
    assert f"<<<\n{INSTRUCTION}\n>>>" in brief
    assert "orchestrating session's instruction for this resume" in brief
    assert "the instruction is the later decision: following it is no finding" in brief
    # The request stays verbatim and first: the instruction is context beside it, never in its place.
    assert brief.index(text) < brief.index(INSTRUCTION)


def test_a_resume_instruction_is_only_the_one_given_after_the_last_attempt_began(store):
    task = store.create_task("build it", status="blocked", source="locks", repo="r", cwd="/tmp")
    assert orchestrate.resume_instruction(store, task) == ""
    store.add_event(task["id"], orchestrate.RESUME_INSTRUCTION, text="before any attempt")
    assert orchestrate.resume_instruction(store, task) == ""
    store.add_attempt(task["id"], cell="sonnet/medium", max_turns=40, passed=0)
    store.add_event(task["id"], orchestrate.RESUME_INSTRUCTION, text=f"  {INSTRUCTION}  ")
    assert orchestrate.resume_instruction(store, task) == INSTRUCTION


def _resumed_with_instruction(git_repo, store, *results, **options):
    """A task that ended short once, then resumed with an instruction (`locks resume N -`)."""
    first = run("add the discount to the cart", git_repo, store, registry={},
                launcher=Script(bad(Failure.ENVIRONMENT, "no network"), write="app.py"),
                classifier=kind("implement"), options=Options(max_attempts=1))
    store.add_event(first.task_id, orchestrate.RESUME_INSTRUCTION, text=INSTRUCTION)
    script = Script(*results, write="app.py")
    report = run("add the discount to the cart", git_repo, store, Options(**options), registry={},
                 launcher=script, classifier=kind("implement"), task_id=first.task_id)
    return report, script


def test_a_resumed_run_gives_its_instruction_to_the_first_check_and_to_a_correction_s_recheck(
        git_repo, store, verifier_on):
    report, script = _resumed_with_instruction(git_repo, store, passing(), check("fail", [BUG]),
                                               passing("fixed"), check("pass"))
    assert report.status == "done"
    worker, first_check, _, recheck = script.specs
    assert INSTRUCTION in worker.prompt  # the worker has it, as before
    for spec in (first_check, recheck):
        assert f"<<<\n{INSTRUCTION}\n>>>" in spec.prompt
        assert "following it is no finding" in spec.prompt


def test_a_run_with_no_instruction_adds_nothing_to_its_check(git_repo, store, verifier_on):
    script = Script(passing(), check("pass"), write="app.py")
    run("add the discount to the cart", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert "instruction for this resume" not in script.specs[1].prompt


def test_a_resumed_run_gives_its_instruction_to_the_late_check(git_repo, store, late):
    report, script = _resumed_with_instruction(git_repo, store, passing(), check("pass"), verify="true")
    assert report.status == "done" and report.late_check
    assert len(script.specs) == 2 and f"<<<\n{INSTRUCTION}\n>>>" in script.specs[1].prompt


# --- 2. the late check blocks by the same rule: only what is shown ----------------------


READ_A = {"where": "cart.py:4", "what": "the discount may round twice", "evidence_class": "inferential",
          "cause": "introduced", "proof": ""}
READ_B = {"where": "cart.py:9", "what": "an empty cart may divide by zero", "evidence_class": "inferential",
          "cause": "worsened", "proof": ""}


def test_a_late_check_that_passes_with_findings_only_read_from_the_code_leaves_the_pass(git_repo, store, late):
    script = Script(passing(), check("pass", [READ_A, READ_B]), write="app.py")
    report = run("fix the cart total", git_repo, store, Options(verify="true"), registry={}, launcher=script,
                 classifier=kind("implement"))
    verified = store.last_event(report.task_id, "verified")["data"]
    assert verified["late"] and verified["passed"] and verified["blocking"] == 0 and verified["carried"] == 2
    assert [f["what"] for f in verified["findings"]] == [READ_A["what"], READ_B["what"]]
    # The pass stands: the task is still done, never reopened as a review.
    assert store.get_task(report.task_id)["status"] == "done"
    kinds = [e["kind"] for e in store.events(task_id=report.task_id)]
    assert kinds.count("finished") == 1


def test_a_late_check_that_passes_but_shows_a_finding_still_reopens_the_task(git_repo, store, late):
    script = Script(passing(), check("pass", [READ_A, BUG]), write="app.py")
    report = run("fix the cart total", git_repo, store, Options(verify="true"), registry={}, launcher=script,
                 classifier=kind("implement"))
    verified = store.last_event(report.task_id, "verified")["data"]
    assert verified["blocking"] == 1 and verified["carried"] == 1 and not verified["passed"]
    assert [f["what"] for f in verified["blockers"]] == [BUG["what"]]
    assert store.last_event(report.task_id, "finished")["data"]["stop"]["cause"] == "review"


def test_a_late_check_that_fails_on_a_finding_only_read_from_the_code_still_blocks(git_repo, store, late):
    # The first check blocks it too, in its first round: only a verdict of pass carries what no probe showed.
    script = Script(passing(), check("fail", [READ_A]), write="app.py")
    report = run("fix the cart total", git_repo, store, Options(verify="true"), registry={}, launcher=script,
                 classifier=kind("implement"))
    verified = store.last_event(report.task_id, "verified")["data"]
    assert verified["blocking"] == 1 and verified["carried"] == 0
    assert store.last_event(report.task_id, "finished")["data"]["stop"]["cause"] == "review"


# --- 3. a point on work that stands, because the surfaces kept the cause out -------------


def _repo_with_tests(git_repo):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_learning.py").write_text("def test_old():\n    assert True\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "tests")
    return git_repo


def _spec_bug(summary: str) -> WorkerResult:
    return WorkerResult(False, Failure.SPEC_BUG, summary, verdict="fail", cost_usd=0.1)


def test_a_point_on_work_that_changed_files_and_names_what_its_surfaces_kept_out_is_a_decision(git_repo, store):
    from locks import stops

    repo = _repo_with_tests(git_repo)
    wrong = _spec_bug("tests/test_learning.py locks in the old behaviour, outside my surfaces (app.py): "
                      "I may not change it")
    report = run("change the greeting in app.py", repo, store, Options(surfaces=("app.py",)), registry={},
                 launcher=Script(wrong, write="app.py"), classifier=kind("implement"))
    assert report.status == "blocked" and report.stop.cause == "decision"
    assert report.stop.extra["widen"] == ["tests/test_learning.py"]
    # The work is kept on its branch and the finished event says so.
    assert report.branch and report.changed == ["app.py"]
    assert store.last_event(report.task_id, "finished")["data"]["branch"] == report.branch
    assert subprocess.run(["git", "rev-parse", "--verify", report.branch], cwd=repo,
                          capture_output=True).returncode == 0
    assert stops.next_step(report.task_id, report.stop) == (
        f"locks resume {report.task_id} --surface tests/test_learning.py")


def test_a_spec_bug_with_nothing_to_widen_by_is_still_a_wrong_task(git_repo, store):
    repo = _repo_with_tests(git_repo)
    # It changed a file, but names no surface that kept it from anything.
    wrong = _spec_bug("the request contradicts tests/test_learning.py")
    report = run("change the greeting in app.py", repo, store, Options(surfaces=("app.py",)), registry={},
                 launcher=Script(wrong, write="app.py"), classifier=kind("implement"))
    assert report.status == "replan" and report.stop.cause == "spec" and "widen" not in report.stop.extra
    # It names a path outside its surfaces but changed nothing: no work stands.
    nothing = _spec_bug("tests/test_learning.py is outside my surfaces: I may not change it")
    other = run("change the greeting in app.py", repo, store, Options(surfaces=("app.py",)), registry={},
                launcher=Script(nothing), classifier=kind("implement"))
    assert other.status == "replan" and other.stop.cause == "spec"


# --- 4. widening offers only paths that exist --------------------------------------------


def _widened(store, repo, summary: str, surfaces=("app.py",)) -> list[str]:
    task = store.create_task("a task", status="running", source="locks", repo="r", cwd=str(repo))
    wrong = _spec_bug(summary)
    return orchestrate._widen(store, task["id"], surfaces, wrong, repo)


def test_widen_keeps_only_paths_that_exist_and_resolves_a_bare_name_to_its_one_tracked_file(git_repo, store):
    repo = _repo_with_tests(git_repo)
    (repo / "src" / "locks" / "ui").mkdir(parents=True)
    (repo / "src" / "locks" / "ui" / "server.py").write_text("x = 1\n")
    (repo / "a").mkdir()
    (repo / "b").mkdir()
    (repo / "a" / "dup.py").write_text("x = 1\n")
    (repo / "b" / "dup.py").write_text("x = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "more")
    said = ("outside my surfaces: test_learning.py, tests/test_learning.py, src/locks/ui/server.py, server.py, "
            "dup.py, ghost.py, tests/ghost.py")
    # The bare names collapse into the full path they are; the one that matches two files, the one that
    # matches none and the path that is not there are dropped.
    assert _widened(store, repo, said) == ["src/locks/ui/server.py", "tests/test_learning.py"]


def test_widen_offers_a_folder_that_exists_and_never_a_path_leaving_the_work_directory(git_repo, store):
    repo = _repo_with_tests(git_repo)
    assert _widened(store, repo, "my surfaces keep me out of tests/ and ../outside.py") == ["tests"]


def test_widen_does_not_offer_what_the_surfaces_already_allow(git_repo, store):
    repo = _repo_with_tests(git_repo)
    (repo / "tests" / "test_new.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "new")
    # A bare name that resolves into the surfaces is no widening.
    assert _widened(store, repo, "outside my surfaces: test_new.py", surfaces=("tests/**",)) == []


def test_widen_keeps_a_path_git_reported_as_changed_even_when_the_stray_write_made_a_new_file(git_repo, store):
    task = store.create_task("a task", status="running", source="locks", repo="r", cwd=str(git_repo))
    store.add_event(task["id"], "outside_surfaces", seq=1, paths=["docs/new.md"], surfaces=["app.py"], undone=True)
    assert orchestrate._widen(store, task["id"], ("app.py",), _spec_bug("nothing named"), git_repo) == ["docs/new.md"]


@pytest.mark.parametrize("summary", ["", "the rounding rule lives in tests/test_learning.py"])
def test_widen_without_the_worker_naming_its_surfaces_offers_nothing(git_repo, store, summary):
    assert _widened(store, _repo_with_tests(git_repo), summary) == []
