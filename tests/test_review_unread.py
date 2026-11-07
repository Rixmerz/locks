"""A review that reports no finding and read nothing is not a review (`behavior.UNREAD_SHAPE`): a
worker's is held back once, in its own session, and climbs the second time; the independent check's
is run once more and, read nothing twice, leaves the work's pass standing and says so."""
from __future__ import annotations

import os

import pytest

from locks import behavior, hooks, orchestrate, signature
from locks.escalate import Failure
from locks.launch import WorkerResult
from locks.matrix import Cell
from locks.orchestrate import Options, run
from locks.store import Store

from .test_checking import BUG, check, passing
from .test_orchestrate import Script, kind

LS = ("Bash", {"command": "ls"})
DIFF = ("Bash", {"command": "git diff main"})
READ = ("Read", {"file_path": "app.py"})


class Logging(Script):
    """A launcher whose worker makes tool calls through the hooks' own fast path (`signature.append`),
    as a `claude -p` worker does: `calls[i]` is what its i-th launch calls, then it answers. A launch
    past the list calls nothing, so nothing of it reaches the log."""

    def __init__(self, *results, calls=(), session="", **kw):
        super().__init__(*results, **kw)
        self.calls, self.session = list(calls), session

    def __call__(self, spec):
        env = {**os.environ, **spec.env}
        index = len(self.specs)
        for tool, tool_input in self.calls[index] if index < len(self.calls) else ():
            signature.append({"tool_name": tool, "session_id": self.session or "w", "tool_input": tool_input}, env)
        return super().__call__(spec)


@pytest.fixture
def verifier_on(monkeypatch):
    """The independent check as it runs inline: after locks' own check and before the ending."""
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    monkeypatch.setenv("LOCKS_OVERLAP", "off")
    monkeypatch.setenv("LOCKS_LATE_VERIFIER", "off")


def review() -> WorkerResult:
    """A review's clean pass: it found nothing."""
    return WorkerResult(True, None, "reviewed: nothing to report", "read the change", cost_usd=0.1, session_id="s1")


def reviewing(*results, calls=()):
    return Logging(*results, calls=calls)


def start(script, text="Review the change on this branch", kind_name="review-routine", *, git_repo, store):
    return run(text, git_repo, store, registry={}, launcher=script, classifier=kind(kind_name))


# --- a worker's clean review that read nothing -----------------------------------------------------


@pytest.mark.usefixtures("no_verifier")
def test_a_clean_review_that_read_nothing_goes_back_once_to_the_same_cell_and_then_passes(git_repo, store: Store):
    script = reviewing(review(), review(), calls=[[LS, LS], [DIFF, READ]])
    report = start(script, git_repo=git_repo, store=store)
    assert report.status == "done" and len(script.specs) == 2
    first, again = script.specs
    assert again.cell == first.cell == Cell("sonnet", "high")
    # The same cell is told what was wrong, with the diff it must read; the pass that follows stands.
    assert again.resume_prompt == behavior.UNREAD_PROMPT.format(base=orchestrate._head(git_repo))
    assert "`git diff " in again.resume_prompt and "answer again" in again.resume_prompt
    attempts = store.attempts(report.task_id)
    assert [a["passed"] for a in attempts] == [0, 1] and attempts[0]["failure"] == "inconclusive"
    summary = attempts[0]["summary"]
    assert behavior.UNREAD_SHAPE in summary and "none of your 2 calls read code" in summary
    held = store.events_of(report.task_id, "review_unread")
    assert [(e["data"]["who"], e["data"]["again"], e["data"]["calls"]) for e in held] == [("worker", False, 2)]
    assert held[0]["data"]["seq"] == 1
    finished = store.events_of(report.task_id, "attempt_finished")
    assert finished[0]["data"]["behavior"]["read_calls"] == 0 and finished[1]["data"]["behavior"]["read_calls"] == 2
    assert finished[0]["data"]["passed"] is False and finished[0]["data"]["tools_logged"] is True
    assert "read nothing" in " ".join(report.moves) or "could not be read" in " ".join(report.moves)


@pytest.mark.usefixtures("no_verifier")
def test_two_clean_reviews_that_read_nothing_climb_as_any_inconclusive_does(git_repo, store: Store):
    script = reviewing(review(), review(), review(), calls=[[LS], [LS], [DIFF]])
    report = start(script, git_repo=git_repo, store=store)
    assert report.status == "done" and len(script.specs) == 3
    first, second, third = (s.cell for s in script.specs)
    assert first == second == Cell("sonnet", "high") and third == Cell("opus", "high")
    held = store.events_of(report.task_id, "review_unread")
    assert [(e["data"]["seq"], e["data"]["again"]) for e in held] == [(1, False), (2, True)]
    # Held both times for the same reason: the second keeps the shape, which is why it climbs.
    assert [a["failure"] for a in store.attempts(report.task_id)] == ["inconclusive", "inconclusive", None]


@pytest.mark.usefixtures("no_verifier")
def test_with_no_call_logged_nothing_is_held_because_unknown_is_not_none(git_repo, store: Store):
    script = reviewing(review())
    report = start(script, git_repo=git_repo, store=store)
    assert report.status == "done" and len(script.specs) == 1
    assert not store.events_of(report.task_id, "review_unread")
    assert store.events_of(report.task_id, "attempt_finished")[0]["data"]["tools_logged"] is False


@pytest.mark.usefixtures("no_verifier")
def test_a_review_that_reported_a_finding_stands_whatever_it_read(git_repo, store: Store):
    found = WorkerResult(True, None, "one problem", "read the change", cost_usd=0.1, findings=(BUG,))
    script = reviewing(found, calls=[[LS]])
    report = start(script, git_repo=git_repo, store=store)
    assert report.status == "done" and len(script.specs) == 1
    assert not store.events_of(report.task_id, "review_unread")


@pytest.mark.usefixtures("no_verifier")
@pytest.mark.parametrize("kind_name", ["explore", "plan", "chat"])
def test_a_reading_step_that_is_no_review_is_never_held_back(git_repo, store: Store, kind_name):
    # No finding is a reading kind's normal answer: only a blind review is held to having read.
    script = reviewing(review(), calls=[[LS]])
    report = start(script, "Find out how the cart is totalled", kind_name, git_repo=git_repo, store=store)
    assert report.status == "done" and len(script.specs) == 1
    assert not store.events_of(report.task_id, "review_unread")


@pytest.mark.usefixtures("no_verifier")
def test_a_build_is_never_held_back_for_reading_nothing(git_repo, store: Store):
    script = Logging(passing(), calls=[[LS]], write="app.py")
    report = run("Rename the helper in notes", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "done" and not store.events_of(report.task_id, "review_unread")


@pytest.mark.usefixtures("no_verifier")
def test_a_refute_of_ledger_rows_that_read_nothing_is_held_back_as_a_review_is(git_repo, store: Store):
    # A step that decides ledger rows reviews whatever its kind: rows it calls refuted with no look at
    # the code decided nothing. One that confirms a row is no clean answer and is never held.
    refuted = WorkerResult(True, None, "refuted", "no read", cost_usd=0.1,
                           verdicts=({"id": "F1", "verdict": "refuted", "proof": "none"},))
    confirmed = WorkerResult(True, None, "confirmed", "no read", cost_usd=0.1,
                             verdicts=({"id": "F1", "verdict": "confirmed", "proof": "app.py:1"},))
    held = reviewing(refuted, refuted, calls=[[LS], [DIFF]])
    options = Options(ledger={"role": "refute", "ids": ["F1"], "agreed": []})
    report = run("Refute the rows", git_repo, store, options, registry={}, launcher=held, classifier=kind("explore"))
    assert len(held.specs) == 2 and [e["data"]["who"] for e in store.events_of(report.task_id, "review_unread")] == [
        "worker"]
    stands = reviewing(confirmed, calls=[[LS]])
    run("Refute the rows again", git_repo, store, options, registry={}, launcher=stands, classifier=kind("explore"))
    assert len(stands.specs) == 1


@pytest.mark.usefixtures("no_verifier")
def test_a_review_that_says_a_criterion_is_not_met_is_no_clean_review(git_repo, store: Store):
    # Held to the criteria it answered: one it says the change misses is its finding, carried forward.
    missed = WorkerResult(True, None, "one missing", "read nothing", cost_usd=0.1,
                          criteria=({"id": "S1", "met": False, "evidence": "not built"},))
    script = reviewing(missed, calls=[[LS]])
    report = start(script, "Review it:\nS1: the export is built", git_repo=git_repo, store=store)
    assert len(script.specs) == 1 and not store.events_of(report.task_id, "review_unread")


def test_what_makes_an_answer_clean_is_no_finding_and_no_row_confirmed_not_fixed_or_regressed():
    base = review()
    assert orchestrate._clean_answer(base)
    assert not orchestrate._clean_answer(WorkerResult(True, None, "x", criteria=(
        {"id": "S1", "met": False, "evidence": ""},)))
    assert not orchestrate._clean_answer(WorkerResult(True, None, "x", findings=(BUG,)))
    for word in ("confirmed", "not_fixed", "regression"):
        assert not orchestrate._clean_answer(WorkerResult(True, None, "x", verdicts=({"id": "F1", "verdict": word,
                                                                                     "proof": "p"},)))
    for word in ("refuted", "unsure", "fixed"):
        assert orchestrate._clean_answer(WorkerResult(True, None, "x", verdicts=({"id": "F1", "verdict": word,
                                                                                 "proof": "p"},)))


def test_the_reshape_prompt_of_an_unread_review_names_its_base_and_the_continuation_says_why():
    from locks.escalate import Attempt

    held = Attempt(Cell("sonnet", "high"), 30, False, Failure.INCONCLUSIVE, "held",
                   shape=f"{behavior.UNREAD_SHAPE}: your review reported no finding and none of your 2 calls read code")
    assert orchestrate._reshape_prompt([held], "abc1234") == behavior.UNREAD_PROMPT.format(base="abc1234")
    assert "`git diff HEAD`" in orchestrate._reshape_prompt([held], None)
    assert orchestrate.continuation_note(held).startswith("Why your last answer was held back: unread-review")


# --- the independent check that read nothing ----------------------------------------------------------


def unread_check(summary="looks fine") -> WorkerResult:
    return check("pass", summary=summary)


def test_a_check_that_answers_clean_and_read_nothing_twice_leaves_the_pass_and_says_so(
        git_repo, store: Store, verifier_on):
    script = Logging(passing(), unread_check(), unread_check(), calls=[[], [LS], [LS]], write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and len(script.specs) == 3
    _, first, second = script.specs
    assert first.cell == second.cell and first.prompt == second.prompt  # the same check, asked again
    held = store.events_of(report.task_id, "review_unread")
    assert [(e["data"]["who"], e["data"]["again"], e["data"]["calls"]) for e in held] == [
        ("verifier", False, 1), ("verifier", True, 1)]
    verified = store.events_of(report.task_id, "verified")
    assert len(verified) == 1 and verified[0]["data"]["unread"] is True
    assert verified[0]["data"]["passed"] is False and verified[0]["data"]["blocking"] == 0
    # The window its calls were found in is no snapshot delta: a first round's check records none.
    assert "delta" not in verified[0]["data"] and verified[0]["data"]["worker"] == "sonnet/medium"
    # Both reads are paid for, and the pass stands on locks' own check.
    assert report.cost_usd == pytest.approx(0.1 + 0.05 + 0.05)
    card = store.last_event(report.task_id, "finished")["data"]["verdict"]["independent"]
    assert card["ran"] is True and card["unread"] is True and card["passed"] is False and card["blocking"] == 0
    lines = hooks.verdict_lines(store.last_event(report.task_id, "finished")["data"]["verdict"])
    assert "  independent check: read nothing twice; the pass stands on locks' own check" in lines
    assert "open the diff only where it says something was not checked" in lines[-1]


def test_a_check_that_read_nothing_the_first_time_and_read_the_second_is_the_second_s_verdict(
        git_repo, store: Store, verifier_on):
    script = Logging(passing(), unread_check(), check("fail", [BUG]), passing("fixed"), check("pass"),
                     calls=[[], [LS], [DIFF], [], [DIFF]], write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done"
    held = store.events_of(report.task_id, "review_unread")
    assert [(e["data"]["who"], e["data"]["again"]) for e in held] == [("verifier", False)]
    verified = store.events_of(report.task_id, "verified")
    assert [e["data"].get("unread") for e in verified] == [None, None]
    assert [e["data"]["blocking"] for e in verified] == [1, 0]
    # The first read's cost is in the attempt with the second's.
    assert store.attempts(report.task_id)[0]["cost_usd"] == pytest.approx(0.1 + 0.05 + 0.05)


def test_a_check_that_found_a_problem_is_never_run_again_for_having_read_nothing(
        git_repo, store: Store, verifier_on):
    script = Logging(passing(), check("fail", [BUG]), passing("fixed"), check("pass"),
                     calls=[[], [LS], [], [DIFF]], write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and len(script.specs) == 4
    assert not store.events_of(report.task_id, "review_unread")


def test_a_check_whose_calls_were_never_logged_is_taken_as_it_answered(git_repo, store: Store, verifier_on):
    script = Logging(passing(), unread_check(), calls=[[]], write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and len(script.specs) == 2
    assert not store.events_of(report.task_id, "review_unread")
    assert store.events_of(report.task_id, "verified")[0]["data"]["passed"] is True


def test_a_check_that_read_the_change_is_taken_as_it_answered(git_repo, store: Store, verifier_on):
    script = Logging(passing(), unread_check(), calls=[[], [DIFF]], write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and len(script.specs) == 2
    assert not store.events_of(report.task_id, "review_unread")


def test_a_check_found_by_its_session_is_read_by_the_calls_that_session_made(
        git_repo, store: Store, verifier_on):
    # The verdict names its session: the calls are that session's, whatever the clock says.
    clean = WorkerResult(True, None, "checked", "probe: ok", cost_usd=0.05, verdict="pass", session_id="v1")
    script = Logging(passing(), clean, clean, calls=[[], [LS], [LS]], write="app.py", session="v1")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done"
    assert [e["data"]["again"] for e in store.events_of(report.task_id, "review_unread")] == [False, True]


def test_a_check_that_ran_beside_locks_own_is_found_by_its_session_alone(git_repo, store: Store, verifier_on,
                                                                           monkeypatch):
    monkeypatch.setenv("LOCKS_OVERLAP", "on")
    seen = []

    def launcher(spec):
        seen.append(spec)
        if "Check this change" not in spec.prompt:
            (spec.target_dir / "app.py").write_text("attempt 1\n")
            return passing()
        signature.append({"tool_name": "Bash", "session_id": "v1", "tool_input": {"command": "ls"}},
                         {**os.environ, **spec.env})
        return WorkerResult(True, None, "checked", "probe: ok", cost_usd=0.05, verdict="pass", session_id="v1")

    report = run("fix the cart total", git_repo, store, Options(verify="python3 -c 'import time; time.sleep(0.3)'"),
                 registry={}, launcher=launcher, classifier=kind("implement"))
    assert report.status == "done" and len(seen) == 3
    verified = store.events_of(report.task_id, "verified")
    assert len(verified) == 1 and verified[0]["data"]["beside_check"] and verified[0]["data"]["unread"] is True
    assert [e["data"]["again"] for e in store.events_of(report.task_id, "review_unread")] == [False, True]


def test_the_late_check_that_reads_nothing_twice_leaves_the_pass_and_does_not_reopen_it(
        git_repo, store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    monkeypatch.setenv("LOCKS_OVERLAP", "off")
    monkeypatch.setenv("LOCKS_LATE_VERIFIER", "on")
    script = Logging(passing(), unread_check(), unread_check(), calls=[[], [LS], [LS]], write="app.py")
    report = run("fix the cart total", git_repo, store, Options(verify="true"), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "done" and len(script.specs) == 3
    verified = store.events_of(report.task_id, "verified")
    assert len(verified) == 1 and verified[0]["data"]["late"] is True and verified[0]["data"]["unread"] is True
    assert verified[0]["data"]["passed"] is False and verified[0]["data"]["blocking"] == 0
    assert store.get_task(report.task_id)["status"] == "done"
    assert [e["data"]["who"] for e in store.events_of(report.task_id, "review_unread")] == ["verifier", "verifier"]

