"""A task whose surfaces leave out the tests that pin the behaviour it changes: its check fails only
in those tests, a climb cannot touch them, so it stops at once as a decision with the files to widen
by; and the plan warns before the first attempt when the code index knows such tests."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from locks import dispatch, orchestrate, stops, surfaces
from locks.escalate import Attempt, Failure, Move, decide
from locks.matrix import LADDERS, Cell
from locks.orchestrate import Options, run

from .conftest import git
from .test_orchestrate import Script, _livespec, kind, ok

#: Fails only while the worker's file is there, so the base passes and every failure is the work's.
OUTSIDE = "if [ -f f.py ]; then echo 'FAILED tests/test_old.py::test_pins_the_old - assert 1 == 2'; exit 1; fi"


def _repo_with_test(git_repo):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_old.py").write_text("def test_pins_the_old_behaviour():\n    assert True\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "tests")
    return git_repo


# --- which failing lines name a file outside the surfaces -----------------------------------


@pytest.mark.parametrize(("failing", "expected"), [
    ({"FAILED tests/test_old.py::test_a"}, ["tests/test_old.py"]),
    ({"FAILED tests/test_old.py::test_a", "ERROR tests/test_b.py", "FAIL ./tests/c.test.ts > suite"},
     ["tests/c.test.ts", "tests/test_b.py", "tests/test_old.py"]),
    # one inside the surfaces: a climb may fix it
    ({"FAILED tests/test_old.py::test_a", "FAILED src/app.py::test_b"}, []),
    # one that names no file: the failure cannot be read to files
    ({"FAILED tests/test_old.py::test_a", "✕ adds two"}, []),
    (set(), []),
])
def test_failing_tests_outside_the_surfaces_are_named_only_when_all_of_them_are(failing, expected):
    assert sorted(surfaces.failing_outside(("src/**",), failing)) == expected


def test_no_surfaces_name_no_failing_file():
    assert surfaces.failing_outside((), {"FAILED tests/test_old.py::test_a"}) == []


# --- the move ---------------------------------------------------------------------------------


def _attempt(**kw) -> Attempt:
    return Attempt(Cell("sonnet", "medium"), 40, False, Failure.CODE_BUG, "claimed a pass, but the check failed",
                   changed=("src/app.py",), **kw)


def test_a_check_failing_only_outside_the_surfaces_is_a_decision_not_a_climb():
    ladder = LADDERS["implement"]
    away = decide(ladder, [_attempt(outside=("tests/test_old.py",))])
    assert away.move is Move.DECIDE and not away.continues
    assert "outside its surfaces" in away.reason and "tests/test_old.py" in away.reason
    assert decide(ladder, [_attempt()]).move is Move.MORE_EFFORT


def test_a_refused_attempt_still_blocks_before_the_surfaces_are_read():
    away = decide(LADDERS["implement"], [_attempt(outside=("tests/test_old.py",), denied=("Bash(npm test)",))])
    assert away.move is Move.BLOCKED


# --- the run ----------------------------------------------------------------------------------


def test_a_check_failing_only_in_tests_outside_its_surfaces_stops_at_once_with_the_files_to_widen_by(
        git_repo, store):
    repo = _repo_with_test(git_repo)
    script = Script(ok(), ok(), ok(), write="f.py")
    report = run("change the greeting in f.py", repo, store, Options(surfaces=("f.py",), verify=OUTSIDE),
                 registry={}, launcher=script, classifier=kind("implement"))
    assert len(script.specs) == 1  # no climb, no further attempt
    assert report.status == "blocked" and report.stop.cause == "decision"
    assert report.stop.extra["widen"] == ["tests/test_old.py"]
    assert "outside its surfaces" in report.stop.reason and "tests/test_old.py" in report.stop.reason
    assert report.branch and report.changed == ["f.py"]  # the work is kept
    assert stops.next_step(report.task_id, report.stop) == f"locks resume {report.task_id} --surface tests/test_old.py"
    assert store.last_event(report.task_id, "moved")["data"]["move"] == "decide"


def test_a_stop_on_tests_outside_the_surfaces_is_never_resumed_on_its_own():
    assert "decision" not in dispatch.MECHANICAL


def test_a_failing_test_inside_the_surfaces_climbs_as_before(git_repo, store):
    repo = _repo_with_test(git_repo)
    script = Script(ok(), ok(), ok(), write="f.py")
    report = run("change the greeting in f.py", repo, store,
                 Options(surfaces=("f.py", "tests/test_old.py"), verify=OUTSIDE, max_attempts=2),
                 registry={}, launcher=script, classifier=kind("implement"))
    assert len(script.specs) == 2
    assert not (report.stop and report.stop.cause == "decision")


def test_a_failure_that_cannot_be_read_to_files_climbs_as_before(git_repo, store):
    check = "if [ -f f.py ]; then echo '✕ adds two'; exit 1; fi"
    script = Script(ok(), ok(), ok(), write="f.py")
    report = run("change the greeting in f.py", _repo_with_test(git_repo), store,
                 Options(surfaces=("f.py",), verify=check, max_attempts=2), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert len(script.specs) == 2
    assert not (report.stop and report.stop.cause == "decision")


def test_a_task_with_no_surfaces_climbs_as_before(git_repo, store):
    script = Script(ok(), ok(), ok(), write="f.py")
    report = run("change the greeting in f.py", _repo_with_test(git_repo), store,
                 Options(verify=OUTSIDE, max_attempts=2), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert len(script.specs) == 2
    assert not (report.stop and report.stop.cause == "decision")


def test_the_base_comparison_still_wins_over_the_surface_stop(git_repo, store):
    # The same test fails at the base: it was there before the work, and the pass stands.
    check = "echo 'FAILED tests/test_old.py::test_pins_the_old - assert 1 == 2'; exit 1"
    script = Script(ok(), write="f.py")
    report = run("change the greeting in f.py", _repo_with_test(git_repo), store,
                 Options(surfaces=("f.py",), verify=check), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "done"


# --- the plan's warning -----------------------------------------------------------------------


def _plan(git_repo, store, tmp_path, *surface, adapters=None):
    return run("parse_amount rounds wrong", git_repo, store, Options(dry_run=True, surfaces=tuple(surface)),
               registry={}, classifier=kind("implement"),
               adapters=[_livespec(tmp_path)] if adapters is None else adapters).plan


def test_the_plan_names_the_tests_the_index_says_exercise_the_surfaces_and_the_surfaces_leave_out(
        git_repo, store, tmp_path):
    from .livespec_fixture import build

    build(git_repo)
    the_plan = _plan(git_repo, store, tmp_path, "src/parse.py")
    line = next(r for r in the_plan.reasons if r.startswith("this task's surfaces leave out"))
    assert "tests/test_parse.py" in line and "needs them in its surfaces" in line
    assert line in orchestrate.warnings(the_plan.reasons)
    assert sum(r.startswith("this task's surfaces leave out") for r in the_plan.reasons) == 1


def test_the_plan_is_silent_when_the_test_is_in_the_surfaces_there_is_no_index_or_no_surface(
        git_repo, store, tmp_path):
    from .livespec_fixture import build

    left_out = "this task's surfaces leave out"
    assert not any(r.startswith(left_out) for r in _plan(git_repo, store, tmp_path, "src/parse.py",
                                                         "tests/test_parse.py").reasons)
    assert not any(r.startswith(left_out) for r in _plan(git_repo, store, tmp_path, "src/parse.py").reasons)  # no index
    build(git_repo)
    assert not any(r.startswith(left_out) for r in _plan(git_repo, store, tmp_path).reasons)  # no surfaces
    assert not any(r.startswith(left_out) for r in _plan(git_repo, store, tmp_path, "src/billing.py").reasons)


def test_the_plan_names_up_to_six_tests_and_the_rest_by_count():
    rows = [(f"tests/test_{n}.py",) for n in range(8)] + [("src/api.py",)]
    adapter = SimpleNamespace(_reach=lambda files, status: ([], rows))
    (line,) = orchestrate._tests_left_out(("src/parse.py",), None, [(adapter, None)])
    assert "8 test(s)" in line and "tests/test_5.py" in line and "tests/test_6.py" not in line
    assert "and 2 more" in line and "src/api.py" not in line
