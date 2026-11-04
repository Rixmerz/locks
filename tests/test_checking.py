"""What was asked, findings as records, risk, and the independent check."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

from locks import criteria, findings, flow, isolate, launch, orchestrate, risk
from locks.escalate import Attempt, Failure, Move, decide
from locks.launch import LaunchSpec, WorkerResult
from locks.matrix import LADDERS, Cell
from locks.orchestrate import Options, run, verifier_brief, verify_cell
from locks.store import home

from .test_orchestrate import Script, bad, kind, ok
from .test_ui import ui  # noqa: F401  (the fixture)

# --- criteria -------------------------------------------------------------------------


def test_listed_items_become_criteria_in_the_person_s_words():
    text = "Fix the cart:\n- totals round once\n* coupons expire at midnight UTC\n2) keep the API as it is\n"
    assert criteria.items(text) == ["totals round once", "coupons expire at midnight UTC", "keep the API as it is"]
    assert criteria.ids(text) == ("S1", "S2", "S3")
    assert "S2: coupons expire at midnight UTC" in criteria.brief(text)
    assert criteria.items("one sentence. Another sentence.") == [] and criteria.brief("- only one") == ""
    assert criteria.read({"criteria": [{"id": "s1", "met": True}, {"id": "S2"}, "x"]}) == (
        {"id": "S1", "met": True, "evidence": ""},)
    assert criteria.read({"criteria": "no"}) == ()
    assert criteria.unmet(("S1", "S2", "S3"), [{"id": "S1", "met": True}, {"id": "S2", "met": False}]) == (
        ["S3"], ["S2"])


def proc(block: dict) -> subprocess.CompletedProcess:
    text = "done\n```locks-result\n" + json.dumps(block) + "\n```"
    return subprocess.CompletedProcess([], 0, json.dumps({"result": text, "total_cost_usd": 0.1}), "")


def test_a_pass_must_say_each_criterion_is_met(tmp_path):
    spec = LaunchSpec("p", Cell("sonnet", "medium"), tmp_path, criteria=("S1", "S2"))
    ok = {"verdict": "pass", "summary": "s", "evidence": "1 passed"}
    both = launch.parse(proc({**ok, "criteria": [{"id": "S1", "met": True}, {"id": "S2", "met": True}]}), spec)
    assert both.passed and len(both.criteria) == 2
    left_out = launch.parse(proc({**ok, "criteria": [{"id": "S1", "met": True}]}), spec)
    assert not left_out.passed and left_out.failure is Failure.INCONCLUSIVE and "S2" in left_out.summary
    not_met = launch.parse(proc({**ok, "criteria": [{"id": "S1", "met": True}, {"id": "S2", "met": False}]}), spec)
    assert not not_met.passed and not_met.failure is Failure.CODE_BUG and "S2 is not met" in not_met.summary
    plain = launch.parse(proc(ok), LaunchSpec("p", Cell("sonnet", "medium"), tmp_path))
    assert plain.passed


def test_a_judging_step_that_reports_a_criterion_unmet_by_the_work_it_judges_passes(tmp_path):
    """A refute step decided its row and was failed for 'S1, S4 is not met': the criteria it
    reported unmet were the judged change's, a finding carried forward, not its own failure."""
    ok = {"verdict": "pass", "summary": "F1 confirmed", "evidence": "pytest -k cart: 1 failed",
          "criteria": [{"id": "S1", "met": False}, {"id": "S2", "met": True}]}
    judging = LaunchSpec("p", Cell("sonnet", "medium"), tmp_path, criteria=("S1", "S2"), judging=True)
    judged = launch.parse(proc(ok), judging)
    assert judged.passed and judged.failure is None
    assert [c["id"] for c in judged.criteria if c["met"] is False] == ["S1"]
    # What it was not asked is still not a pass: a criterion left out stays inconclusive.
    left_out = launch.parse(proc({**ok, "criteria": [{"id": "S1", "met": False}]}), judging)
    assert not left_out.passed and left_out.failure is Failure.INCONCLUSIVE
    # A step that builds or fixes keeps the rule.
    building = LaunchSpec("p", Cell("sonnet", "medium"), tmp_path, criteria=("S1", "S2"))
    assert launch.parse(proc(ok), building).failure is Failure.CODE_BUG
    # And it is told so: a refute answered `fail` for "S1 is not met because of F4" when its
    # contract said only that a pass reporting an unmet criterion is not a pass.
    assert launch.JUDGING_INSTRUCTIONS in launch.instructions(judging)
    assert launch.JUDGING_INSTRUCTIONS not in launch.instructions(building)
    assert launch.instructions(building) == launch.RESULT_INSTRUCTIONS


@pytest.mark.parametrize("step_kind, judges", [("review-routine", True), ("review-critical", True),
                                               ("explore", True), ("bugfix", False), ("implement", False)])
def test_only_a_step_that_gives_a_verdict_is_launched_as_judging(git_repo, store, step_kind, judges):
    script = Script(WorkerResult(True, None, "ok", "1 passed", criteria=({"id": "S1", "met": True},)))
    run("Judge it:\n- one", git_repo, store, registry={}, launcher=script, classifier=kind(step_kind))
    assert script.specs[0].judging is judges


def test_the_brief_carries_the_criteria(git_repo, store):
    script = Script(WorkerResult(True, None, "ok", "1 passed", criteria=({"id": "S1", "met": True},
                                                                          {"id": "S2", "met": True})))
    run("Do it:\n- one\n- two", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert "S1: one" in script.specs[0].prompt and script.specs[0].criteria == ("S1", "S2")


# --- findings -------------------------------------------------------------------------


def test_findings_are_records_and_only_the_change_s_own_shown_ones_block():
    read = findings.read({"findings": [
        {"where": "src/a.py:12", "what": "Off by one in the loop", "evidence_class": "deterministic",
         "cause": "introduced", "proof": "pytest -k loop"},
        {"where": "src/b.py", "what": "old bug", "evidence_class": "inferential", "cause": "pre-existing"},
        {"where": "src/c.py", "what": "maybe slow", "evidence_class": "insufficient", "cause": "introduced"},
        {"what": "no place, odd values", "evidence_class": "weird", "cause": "??"},
        {"where": "x"}, "junk"]})
    assert len(read) == 4 and read[3]["evidence_class"] == "inferential" and read[3]["cause"] == "unknown"
    assert [f["where"] for f in findings.blocking(read)] == ["src/a.py:12", ""]
    moved = {"where": "src/a.py:40", "what": "reworded entirely", "evidence_class": "deterministic",
             "cause": "introduced"}
    assert findings.same(read[0], moved) and not findings.same(read[1], moved)
    assert findings.places(read) == {"src/a.py", "src/b.py", "src/c.py"}
    assert "- [introduced, deterministic] src/a.py:12: Off by one in the loop — pytest -k loop" in findings.text(read)
    assert findings.read({"findings": "no"}) == ()
    # Shown by a command means its proof is given: a claim of it without one is read from the code.
    claimed = findings.read({"findings": [{"where": "a.py", "what": "x", "evidence_class": "deterministic",
                                           "cause": "introduced", "proof": "  "}]})
    assert claimed[0]["evidence_class"] == "inferential"


def test_two_reviews_that_place_the_same_findings_converge():
    a = Attempt(Cell("sonnet", "medium"), 30, False, Failure.CODE_BUG, "words one",
                findings=({"where": "a.py:1", "what": "x"},))
    b = Attempt(Cell("sonnet", "high"), 30, False, Failure.CODE_BUG, "entirely other words",
                findings=({"where": "a.py:9", "what": "y"},))
    assert decide(LADDERS["review-routine"], [a, b], reading=True).move is Move.CONVERGED
    c = Attempt(Cell("sonnet", "high"), 30, False, Failure.CODE_BUG, "words one",
                findings=({"where": "b.py", "what": "y"},))
    assert decide(LADDERS["review-routine"], [a, c], reading=True).move is not Move.CONVERGED


# --- risk -----------------------------------------------------------------------------


def test_risk_starts_from_the_request_and_only_rises_with_the_change():
    assert risk.of_request("explain the parser", "explore").tier == "passive"
    assert risk.of_request("rename a helper", "refactor").tier == "medium"
    r = risk.of_request("fix the login token refresh", "implement")
    assert r.tier == "high" and "security" in r.reasons[0]
    assert risk.of_request("audit it", "review-critical").tier == "high"
    r = risk.of_change(risk.of_request("rename", "refactor"), ["src/a.py", "tests/test_a.py"])
    assert r.tier == "medium"
    r = risk.of_change(risk.of_request("rename", "refactor"), ["src/a.py"])
    assert r.tier == "high" and "no test beside it" in r.reasons[-1]
    assert risk.of_change(risk.Risk("medium"), ["db/migrations/0002.sql"]).tier == "high"
    assert risk.of_change(risk.Risk("medium"), None).tier == "high"
    docs = risk.of_change(risk.Risk("medium"), ["README.md"])
    assert docs.tier == "medium" and "documentation only" in docs.reasons[-1]
    high = risk.Risk("high")
    high.raise_to("passive", "x")
    assert high.tier == "high"
    assert risk.needs_verifier(risk.Risk("high"), cheap=False)
    assert risk.needs_verifier(risk.Risk("medium"), cheap=True) and not risk.needs_verifier(risk.Risk("medium"),
                                                                                             cheap=False)


def _areas(text: str) -> set[str]:
    return {r.split(" (")[0].removeprefix("the request is about ") for r in risk.of_request(text, "implement").reasons}


@pytest.mark.parametrize("text", [
    "Run `git checkout -b fix/totals` first, then fix the rounding",
    "Work in the checkout, not in a worktree, and fix the parser",
    "This checkout has uncommitted work: commit it before you start",
    "Read the notes from /home/user/checkout/app before you change the parser",
    "Keep the checkout path in the brief and fix the parser",
    "A run in the worktree checkout of the parser fails: fix it",
])
def test_checkout_in_git_s_sense_is_no_money(text):
    assert "money" not in _areas(text)


@pytest.mark.parametrize("text", [
    "Fix rounding of the checkout total",
    "Add a checkout to the store",
    "The checkout fails when the cart is empty",
    "Implement checkout with saved cards",
    "Move the checkout page to the new layout",
])
def test_checkout_in_a_shop_s_sense_is_money(text):
    assert "money" in _areas(text)


@pytest.mark.parametrize(("text", "area"), [
    ("Migrate the SVG charts away from ECharts, one component at a time", "data"),
    ("Colors only through the design token for ink, never a hex in a component", "security"),
    ("A read-only checkout of the library is at ../lib: copy from it", "money"),
    ("Never truncate the month labels on the axis with an ellipsis", "data"),
    ("CI sets MECHA_TEST_TRUNCATE=1 for the Postgres job; fix the flaky chart test", "data"),
    ("Keep src/db/migrations/README.md as it is and fix the header", "data"),
    ("Regenerate package-lock.json after bumping the parser dependency", "concurrency"),
    ("Update the yarn.lock and the lock file of the web app", "concurrency"),
    ("Keep the $schema key in the biome config and fix its rules", "data"),
    ("The eslint config must stay valid against its JSON schema", "data"),
    ("Fix the lint warnings in the dashboard, no deployment changes", "delivery"),
    ("Move the eslint config the deploy script used into the repository root", "delivery"),
])
def test_a_word_in_another_sense_raises_no_area(text, area):
    assert area not in _areas(text)


@pytest.mark.parametrize(("text", "area"), [
    ("Migrate the users table to the new schema", "data"),
    ("Migrate the chart data stored in the reports table", "data"),
    ("Rotate the session token on every login", "security"),
    ("The API token leaks into the design system's logs", "security"),
    ("Truncate the audit log table after export", "data"),
    ("Fix the lock around the queue so two workers never take the same task", "concurrency"),
    ("Add a schema migration for the invoices table", "data"),
    ("Validate the config schema before it writes the users table", "data"),
    ("Change the deploy step of the CI pipeline to use the new image", "delivery"),
    ("Fix the lint warnings, but do not touch the deployment pipeline", "delivery"),
])
def test_a_word_in_its_own_sense_still_raises_its_area(text, area):
    assert area in _areas(text)


def test_a_linter_config_change_is_not_high_for_its_lockfile_schema_and_deployment_words():
    request = ("Configure eslint in the API repos: add the flat config, fix the lockfile, keep the json schema "
               "of the config valid, no deployment changes")
    r = risk.of_request(request, "implement")
    assert r.tier == "medium" and r.reasons == []
    # the tier is only raised: a change that touches deploy files is delivery all the same
    r = risk.of_change(r, ["eslint.config.js", ".github/workflows/ci.yml", "tests/lint_test.ts"])
    assert r.tier == "high" and any(x.startswith("it changed delivery (.github/workflows/ci.yml)") for x in r.reasons)
    assert "concurrency" in _areas("Fix the lockfile and run the installs in parallel")


def test_parallel_about_tasks_is_no_concurrency_and_about_code_is():
    assert "concurrency" in _areas("Take S1 and S2; other tasks running in parallel take the other numbers")
    assert "concurrency" in _areas("Two agents work in parallel on this repository: touch only src/ui")
    assert "concurrency" in _areas("Upload the images in parallel instead of one by one")
    assert "concurrency" in _areas("Run the tasks in parallel with asyncio.gather")
    # "parallel" is never read away: a missed concurrency request skips the verifier
    assert "concurrency" in _areas("Run the Celery tasks in parallel")
    assert "concurrency" in _areas("Make the dispatcher run queued tasks in parallel instead of one by one")
    assert "concurrency" in _areas("Process uploads in parallel")
    assert "concurrency" in _areas("Let multiple workers run jobs in parallel")
    assert "concurrency" in _areas("Make several workers run in parallel instead of one")
    # a count of tasks is coordination only when the line gives this one its share
    assert "concurrency" in _areas("Let three tasks run in parallel")
    assert "concurrency" in _areas("Several tasks running in parallel corrupt the queue; fix it")
    assert "concurrency" in _areas("Make many steps run in parallel instead of in sequence")
    assert "concurrency" in _areas("Support multiple tasks running in parallel on the same checkout")
    assert "concurrency" in _areas("Let up to 4 agents run in parallel per lane")
    for request in ("Let two agents work in parallel on the same repo, but only one may write",
                    "Three tasks running in parallel take the same row; fix it",
                    "Make several steps run in parallel so you can finish faster",
                    "Make 2 sessions run in parallel and keep only one writer",
                    "Make other tasks run in parallel with this one"):
        assert risk.of_request(request, "implement").tier == "high", request
    assert "concurrency" in _areas("Three tasks run in parallel here: take S2 and leave the rest")


def test_payment_as_an_event_name_is_no_money_unless_the_change_touches_payment_code():
    named = "A ticket blocks until its event: `client_approval`, `payment`, `access`, `dependency` or `window`"
    assert "money" not in _areas(named)
    assert "money" not in _areas("Block events are client_approval, payment, access: add the window one")
    assert "money" not in _areas("Unblock the ticket when the `payment` event arrives")
    # unquoted, it may well be a processor's: still money
    assert "money" in _areas("Handle the payment event from the processor")
    assert "money" in _areas("Retry the payment when the card is declined")
    assert "money" in _areas("Store each payment with its currency")
    r = risk.of_change(risk.of_request(named, "implement"), ["lib/server/payments.ts", "tests/payments_test.ts"])
    assert r.tier == "high" and any(x.startswith("it changed money (lib/server/payments.ts)") for x in r.reasons)
    r = risk.of_change(risk.of_request(named, "implement"), ["lib/schema.ts", "tests/schema_test.ts"])
    assert not any("money" in x for x in r.reasons)


# --- the independent check --------------------------------------------------------------


def passing(summary="done"):
    return WorkerResult(True, None, summary, "1 passed", cost_usd=0.1)


def check(verdict="pass", found=(), summary="checked"):
    return WorkerResult(verdict == "pass", None if verdict == "pass" else Failure.CODE_BUG, summary, "probe: ok",
                        cost_usd=0.05, verdict=verdict, findings=tuple(found))


BUG = {"where": "app.py:3", "what": "drops the last item", "evidence_class": "deterministic", "cause": "introduced",
       "proof": "python probe.py"}
OTHER = {"where": "lib.py:9", "what": "a new crash", "evidence_class": "deterministic", "cause": "introduced",
         "proof": "python -c 'import lib'"}
OLD = {"where": "old.py", "what": "was always so", "evidence_class": "deterministic", "cause": "pre-existing",
       "proof": "git stash; pytest old_test.py"}


@pytest.fixture
def verifier_on(monkeypatch):
    """The independent check as it runs inline: after locks' own check and before the ending. A
    test of its form beside the check (`overlap`) or after the ending (`late_verifier`) turns that on."""
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    monkeypatch.setenv("LOCKS_OVERLAP", "off")
    monkeypatch.setenv("LOCKS_LATE_VERIFIER", "off")


def test_a_risky_pass_is_checked_and_its_findings_earn_one_correction(git_repo, store, verifier_on):
    script = Script(passing(), check("fail", [BUG, OLD]), passing("fixed"), check("pass", [OLD]), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and report.cells == ["sonnet/medium", "sonnet/high"]
    _, verifier, again, recheck = script.specs
    assert verifier.cell == Cell("sonnet", "high") and set(launch.WRITE_TOOLS) <= set(verifier.disallowed_tools)
    assert "fix the cart total" in verifier.prompt and "done" not in verifier.prompt.split("verbatim:")[1][:40]
    assert verifier.append_system_prompt.startswith("You work as the `verifier` role")
    assert "drops the last item" in again.prompt  # the findings are the next attempt's evidence
    assert "drops the last item" in recheck.prompt and "whether it is fixed" in recheck.prompt
    events = store.events_of(report.task_id, "verified")
    assert [e["data"]["blocking"] for e in events] == [1, 0] and events[0]["data"]["risk"]["tier"] == "high"
    attempts = store.attempts(report.task_id)
    assert attempts[0]["cost_usd"] == pytest.approx(0.15)
    assert "risk high" in report.text()
    # The check is told why it runs, in the router's words, and its own cost is kept apart.
    assert "Why this check runs: " in verifier.prompt and verifier.max_turns <= orchestrate.VERIFIER_TURNS
    first = store.events_of(report.task_id, "attempt_finished")[0]["data"]
    assert first["worker_cost_usd"] == pytest.approx(0.1) and first["verifier_cost_usd"] == pytest.approx(0.05)


def test_a_correction_continues_the_worker_s_session_with_the_blockers_and_the_check_never_does(
        git_repo, store, verifier_on):
    worked = WorkerResult(True, None, "done", "1 passed", cost_usd=0.1, session_id="sess-1")
    script = Script(worked, check("fail", [BUG]), passing("fixed"), check("pass"), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and report.moves[0].startswith("more_effort to sonnet/high")
    _, verifier, again, recheck = script.specs
    assert again.resume_session == "sess-1" and again.fork_session and again.resume_prompt == launch.DEEPER_PROMPT
    assert "app.py:3: drops the last item" in again.resume_note and "independent check" in again.resume_note
    # the check reads the request and the diff in a session of its own, never the worker's
    assert verifier.resume_session == recheck.resume_session == "" and not recheck.fork_session
    assert not verifier.persist_session and not recheck.persist_session
    assert "fix the cart total" in recheck.prompt


def test_progress_earns_the_next_round_and_what_is_left_at_the_cap_goes_to_a_person(git_repo, store, verifier_on):
    """The palette that never passed: each round fixed what the one before found, and the
    check showed something new. Fixing every earlier problem is progress, and earns a round
    up to the cap; what is still shown there stops the task for a person."""
    third = {**OTHER, "where": "theme.css:4", "what": "a third contrast failure"}
    script = Script(passing(), check("fail", [BUG]), passing("fixed"), check("fail", [OTHER]), passing("fixed too"),
                    check("fail", [third]), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert len(report.cells) == 3 and "a new crash" in script.specs[4].prompt
    assert report.status == "blocked" and report.stop.cause == "review"
    assert "still shows problems the earlier checks had not" in report.stop.reason
    assert report.branch and "a third contrast failure" in report.stop.account


def test_after_progress_a_finding_only_read_from_the_code_goes_with_the_pass(git_repo, store, verifier_on):
    read = {**OTHER, "evidence_class": "inferential", "what": "the hover colour may be too light"}
    script = Script(passing(), check("fail", [BUG]), passing("fixed"), check("fail", [read]), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and len(script.specs) == 4
    events = store.events_of(report.task_id, "verified")
    assert [(e["data"]["blocking"], e["data"]["carried"]) for e in events] == [(1, 0), (0, 1)]
    # The blockers themselves are kept, so a resume after a stop answers exactly them.
    assert [f["what"] for f in events[0]["data"]["blockers"]] == [BUG["what"]] and events[1]["data"]["blockers"] == []
    assert "the hover colour may be too light" in store.attempts(report.task_id)[-1]["evidence"]
    finished = store.last_event(report.task_id, "finished")["data"]
    assert any(f["what"] == read["what"] for f in finished["findings"])


def test_a_problem_that_survives_beside_new_ones_stops_at_the_cap(git_repo, store, verifier_on):
    script = Script(passing(), check("fail", [BUG]), passing(), check("fail", [BUG, OTHER]), passing(),
                    check("fail", [BUG, OTHER]), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "blocked" and "still there, beside new ones" in report.stop.reason


def test_the_same_problem_twice_earns_a_second_correction_then_a_person(git_repo, store, verifier_on):
    script = Script(passing(), check("fail", [BUG]), passing(), check("fail", [BUG]), passing(),
                    check("fail", [{**BUG, "where": "app.py:7"}]), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "blocked" and "survived 2 corrections" in report.stop.reason
    assert len(report.cells) == 3


def test_the_ladder_running_out_with_only_the_checks_blockers_open_keeps_the_work(git_repo, store, verifier_on):
    """Task #50 of another repository: a feature started at opus/high, two rungs left. Both
    attempts passed the repository's check; the independent check found a different blocker
    each round (progress). The corrections took the ladder's rungs, and the top of it said
    `exhausted`: failed, branch deleted, 25 files and the money gone. The work passed its own
    check and only the verifier's findings stand: it stops for a person, kept."""
    script = Script(passing("added the export endpoint and its csv writer"), check("fail", [BUG]),
                    passing("fixed the dropped last row in the totals"), check("fail", [OTHER]), write="app.py")
    report = run("add the cart export", git_repo, store, Options(start=Cell("opus", "high"), verify="true"),
                 registry={}, launcher=script, classifier=kind("feature"))
    assert report.cells == ["opus/high", "opus/xhigh"] and len(script.specs) == 4
    assert report.status == "blocked" and report.stop.cause == "review"
    assert "ladder" in report.stop.reason and "opus/xhigh" in report.stop.reason
    assert OTHER["what"] in report.stop.account and BUG["what"] not in report.stop.account
    assert report.branch and isolate.kept(git_repo, report.task_id)
    assert isolate.UNVERIFIED in subprocess.run(["git", "log", "-1", "--format=%B", report.branch], cwd=git_repo,
                                                capture_output=True, text=True, check=True).stdout
    # Why, in the move's own words: no model above, and the check's blockers are all that is open.
    decision = report.decisions[-1]
    assert decision.move is Move.REVIEW and any("passed its own check" in line for line in decision.because)
    finished = store.last_event(report.task_id, "finished")["data"]
    assert finished["status"] == "blocked" and finished["stop"]["cause"] == "review"
    # It resumes like any review stop: its first check is given the blockers the last round kept.
    assert [f["what"] for f in store.last_event(report.task_id, "verified")["data"]["blockers"]] == [OTHER["what"]]


def test_two_cells_agreeing_with_no_stronger_model_left_also_keeps_the_work(git_repo, store, verifier_on):
    """The same sequence with summaries that read alike: the repeated-answer rule found no model above
    and sent it to `replan`, which drops the work as well."""
    script = Script(passing(), check("fail", [BUG]), passing(), check("fail", [OTHER]), write="app.py")
    report = run("add the cart export", git_repo, store, Options(start=Cell("opus", "high"), verify="true"),
                 registry={}, launcher=script, classifier=kind("feature"))
    assert report.status == "blocked" and report.stop.cause == "review" and OTHER["what"] in report.stop.account
    assert report.branch and isolate.kept(git_repo, report.task_id)


def test_decide_reviews_only_work_whose_own_check_passed():
    top = Cell("opus", "xhigh")
    ladder = LADDERS["feature"]
    open_ = Attempt(top, 60, False, Failure.CODE_BUG, "the independent check found 1 problem(s)",
                    blockers=(BUG,))
    review = decide(ladder, [open_])
    assert review.move is Move.REVIEW and not review.continues and "top of this ladder" in review.reason
    assert "blocker" in review.reason and any("passed its own check" in line for line in review.because)
    # Its own check never passed: the ladder running out is a failure, as before.
    assert decide(ladder, [Attempt(top, 60, False, Failure.CODE_BUG, "red")]).move is Move.EXHAUSTED
    # Below the top, a correction still climbs.
    below = Attempt(Cell("opus", "high"), 60, False, Failure.CODE_BUG, "x", blockers=(BUG,))
    assert decide(ladder, [below]).move is Move.MORE_EFFORT


def test_every_move_taken_for_a_correction_says_the_work_passed_its_own_check():
    """A correction climbed with only 'the work happened and was shallow': the person reading
    why saw a failed attempt, when the work had passed and only the independent check's blockers failed."""
    ladder = LADDERS["feature"]
    two = (BUG, OTHER)
    # More effort, below the top of the model.
    effort = decide(ladder, [Attempt(Cell("opus", "high"), 60, False, Failure.CODE_BUG, "x", blockers=two)])
    # The model's last cell: the next model takes over.
    handed = decide(ladder, [Attempt(Cell("sonnet", "xhigh"), 60, False, Failure.CODE_BUG, "x", blockers=two)])
    # Two cells that agreed.
    same = Attempt(Cell("sonnet", "medium"), 60, False, Failure.CODE_BUG, "same", blockers=two)
    again = Attempt(Cell("sonnet", "high"), 60, False, Failure.CODE_BUG, "same", blockers=two)
    agreed = decide(ladder, [same, again])
    moved = [effort, handed, agreed]
    assert [d.move for d in moved][:2] == [Move.MORE_EFFORT, Move.NEXT_MODEL]
    for decision in moved:
        text = " | ".join(decision.because)
        assert "passed its own check" in text and "2 blocker(s)" in text, decision
    # An attempt that is no correction is not told it passed.
    plain = decide(ladder, [Attempt(Cell("opus", "high"), 60, False, Failure.CODE_BUG, "red")])
    assert "passed its own check" not in " ".join(plain.because)


def test_the_attempt_limit_with_only_the_checks_blockers_open_keeps_the_work_too(git_repo, store, verifier_on):
    script = Script(passing(), check("fail", [BUG]), write="app.py")
    report = run("add the cart export", git_repo, store,
                 Options(start=Cell("opus", "high"), verify="true", max_attempts=1), registry={}, launcher=script,
                 classifier=kind("feature"))
    assert report.status == "blocked" and report.stop.cause == "review" and BUG["what"] in report.stop.account
    assert report.branch and isolate.kept(git_repo, report.task_id)


def test_a_ladder_that_ran_out_without_any_check_passing_still_drops_its_work(git_repo, store, verifier_on):
    script = Script(bad(Failure.CODE_BUG), bad(Failure.CODE_BUG, "different words entirely"), write="app.py")
    report = run("add the cart export", git_repo, store, Options(start=Cell("opus", "high"), verify="true"),
                 registry={}, launcher=script, classifier=kind("feature"))
    assert report.status == "failed" and report.stop.cause == "exhausted"
    assert report.branch is None and not isolate.kept(git_repo, report.task_id)


def test_an_undecided_check_or_a_fail_without_findings(git_repo, store, verifier_on):
    script = Script(passing(), check("inconclusive", summary="no browser"), write="app.py")
    report = run("fix it", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done"
    assert "independent check inconclusive: no browser" in store.attempts(report.task_id)[0]["evidence"]
    script = Script(passing(), check("fail", summary="the totals are wrong"), passing(), check("pass"),
                    write="app.py")
    report = run("fix it", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and "the totals are wrong" in script.specs[2].prompt


def test_no_check_for_a_change_without_risk_or_a_reading_task(git_repo, store, verifier_on):
    script = Script(passing(), write="notes.md")
    run("rename the docs heading", git_repo, store, registry={}, launcher=script, classifier=kind("refactor"))
    assert len(script.specs) == 1  # documentation only: medium, from a strong cell
    script = Script(passing())
    run("review the cart", git_repo, store, registry={}, launcher=script, classifier=kind("review-routine"))
    assert len(script.specs) == 1
    script = Script(passing(), check("pass"), write="notes.md")
    run("rename the docs heading", git_repo, store, Options(start=Cell("haiku", "medium")), registry={},
        launcher=script, classifier=kind("refactor"))
    assert len(script.specs) == 2 and script.specs[1].cell == Cell("sonnet", "high")  # a cheap cell's pass


def test_the_verifier_cell_and_brief():
    assert verify_cell(Cell("haiku", "low")) == Cell("sonnet", "high")
    assert verify_cell(Cell("opus", "medium")) == Cell("opus", "high")
    assert verify_cell(Cell("opus", "max")) == Cell("opus", "high")  # independence is the session, not effort
    text = verifier_brief("Do:\n- a\n- b", "implement", home(), None, [f"f{i}" for i in range(25)])
    assert "S2: b" in text and "uncommitted" in text and "and 5 more" in text and "Why this check" not in text
    fixing = verifier_brief("the export drops rows", "debug-repro", home(), "abc123def4567", ["x.py"],
                            why=["the request is about data"])
    assert "Why this check runs: the request is about data" in fixing and "at the base commit abc123def456" in fixing
    # It is read-only, as its role says: never told to stash the work it checks.
    assert "never `git stash`" in fixing and "git worktree add" in fixing


def test_a_worker_that_climbed_to_many_turns_does_not_hand_them_to_its_check(git_repo, store, verifier_on):
    script = Script(passing(), check("pass"), write="app.py")
    run("fix the cart total", git_repo, store, Options(max_turns=150), registry={}, launcher=script,
        classifier=kind("implement"))
    worker, verifier = script.specs
    assert worker.max_turns == 150 and verifier.max_turns == orchestrate.VERIFIER_TURNS


def test_a_person_s_verifier_role_takes_the_bundled_one_s_place():
    from locks import roles

    from .test_roles import write_role

    assert roles.internal("verifier").scope == "bundled"
    assert "verifier" not in roles.load().roles  # in no team
    write_role(home() / "roles", "verifier", "Mine.", description="mine", kinds=["review-routine"])
    assert roles.internal("verifier").prompt == "Mine."


# --- surfaces -------------------------------------------------------------------------


def test_surfaces_hold_what_they_name_and_nothing_else():
    from locks import surfaces

    s = surfaces.check(["src/api/", "docs/*.md", "tests/**", "lib", "*.cfg", "./", " "])
    assert s == ("src/api/**", "docs/*.md", "tests/**", "lib", "*.cfg", "**")
    assert surfaces.allows(("src/api/**",), "src/api/x/y.py") and not surfaces.allows(("src/api/**",), "src/apix.py")
    assert surfaces.allows(("docs/*.md",), "docs/a.md") and not surfaces.allows(("docs/*.md",), "docs/x/a.md")
    assert surfaces.allows(("lib",), "lib/a/b.c") and not surfaces.allows(("lib",), "libx")
    assert surfaces.allows(("a[bc].py",), "ab.py") and surfaces.allows(("a[b",), "a[b")
    assert surfaces.allows(("src/?.py",), "src/a.py") and not surfaces.allows(("src/?.py",), "src/ab.py")
    assert surfaces.outside((), ["anything"]) == [] and surfaces.outside(("src/**",), ["src/a", "b"]) == ["b"]
    with pytest.raises(surfaces.SurfaceError, match="inside the project"):
        surfaces.check(["../up"])
    with pytest.raises(surfaces.SurfaceError, match="at most"):
        surfaces.check([f"d{i}" for i in range(30)])
    assert surfaces.disjoint(["src/api/**"], ["src/web/**"]) and surfaces.disjoint(["src/a.py"], ["src/b.py"])
    assert not surfaces.disjoint(["src/**"], ["src/web/a.ts"]) and not surfaces.disjoint(["*.md"], ["src/x"])
    assert not surfaces.disjoint([], ["src/x"]) and not surfaces.disjoint(["src/x"], ["src/x"])
    assert surfaces.brief(()) == "" and "src/api/**" in surfaces.brief(("src/api/**",))
    # Each pair of surfaces of two groups that may touch, named by the group that declared it.
    assert surfaces.overlapping({"api": ["src/api/**"], "web": ["src/web/**", "docs/web.md"]}) == []
    assert surfaces.overlapping({"api": ["src/api/**", "docs/**"], "web": ["src/**"], "doc": ["docs/a.md"]}) == [
        "api's src/api/** and web's src/**", "api's docs/** and doc's docs/a.md"]


def test_a_change_outside_the_surfaces_is_undone_and_fails_the_attempt(git_repo, store):
    seen = []

    def launcher(spec):
        seen.append(spec)
        (spec.target_dir / "app.py").write_text("changed\n")  # tracked at the base: restored
        if len(seen) == 1:
            (spec.target_dir / "stray.txt").write_text("x\n")  # new: removed
            return WorkerResult(True, None, "done", "1 passed", changed_paths=("app.py", "stray.txt"))
        return WorkerResult(True, None, "done", "1 passed", changed_paths=("src/ok.py",))

    report = run("fix it", git_repo, store, Options(surfaces=("src/**",)), registry={}, launcher=launcher,
                 classifier=kind("implement"))
    assert "Change only files inside these surfaces: src/**" in seen[0].prompt
    first = store.attempts(report.task_id)[0]
    assert not first["passed"] and first["failure"] == "approach" and "outside its surfaces" in first["summary"]
    event = store.last_event(report.task_id, "outside_surfaces")["data"]
    assert event["paths"] == ["app.py", "stray.txt"] and event["undone"]
    assert Options(surfaces=("a",)).stored()["surfaces"] == ["a"]


def test_in_the_checkout_itself_nothing_is_undone(git_repo, store):
    def launcher(spec):
        return WorkerResult(True, None, "done", "1 passed", changed_paths=("app.py",))

    report = run("fix it", git_repo, store, Options(surfaces=("src/**",), isolate=False, max_attempts=1),
                 registry={}, launcher=launcher, classifier=kind("implement"))
    assert "left in your checkout" in store.attempts(report.task_id)[0]["summary"]


def test_tasks_whose_surfaces_cannot_touch_run_side_by_side_without_a_model(store):
    from locks import dispatch, flow

    def queued(text, surf):
        return store.enqueue(text, repo="k", cwd="/x", options={"surfaces": surf} if surf else {})

    a, b = queued("a", ["src/api/**"]), queued("b", ["src/web/**"])
    assert flow.by_surfaces(b, [a]).parallel
    c = queued("c", ["src/**"])
    decision = flow.by_surfaces(c, [a])
    assert not decision.parallel and f"#{a['id']}" in decision.reason
    assert flow.by_surfaces(queued("d", None), [a]) is None and flow.by_surfaces(b, [queued("e", None)]) is None
    assert flow.by_surfaces(b, []) is None
    assert dispatch.declared_surfaces({"options": "{"}) == ()


def test_a_workflow_step_carries_its_surfaces(git_repo, store, monkeypatch):
    from locks import workflows

    monkeypatch.setattr(workflows, "_kick", lambda cwd, key: None)
    with pytest.raises(workflows.WorkflowError, match="surfaces are a list"):
        workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "p", "surfaces": "src"}]})
    with pytest.raises(workflows.WorkflowError, match="inside the project"):
        workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "p", "surfaces": ["../x"]}]})
    assert "surfaces" not in workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "p",
                                                                         "surfaces": []}]})["steps"][0]
    workflows.save({"name": "bounded", "steps": [{"id": "a", "prompt": "Do it", "surfaces": ["src/"]}]}, git_repo,
                   "user")
    started = workflows.start(store, "bounded", git_repo)
    assert json.loads(store.get_task(started["queued"][0])["options"])["surfaces"] == ["src/**"]


def test_the_cli_takes_surfaces(capsys, git_repo):
    from locks import cli

    assert cli.main(["queue", "add", "x", "--repo", str(git_repo), "--surface", "src/", "--json"]) == 0
    from locks.store import Store

    queued = json.loads(capsys.readouterr().out)["id"]
    store = Store.open()
    assert json.loads(store.get_task(queued)["options"])["surfaces"] == ["src/**"]
    store.close()
    with pytest.raises(SystemExit, match="inside the project"):
        cli.main(["run", "x", "--surface", "/abs", "--repo", str(git_repo), "--dry-run"])


# --- the blind dual review --------------------------------------------------------------


def test_the_dual_review_starts_two_blind_reviews_on_different_models(git_repo, store, monkeypatch):
    from locks import workflows

    monkeypatch.setattr(workflows, "_kick", lambda cwd, key: None)
    defn = workflows.load("dual-review", git_repo)
    steps = {s["id"]: s for s in defn["steps"]}
    assert steps["review-a"]["start"] != steps["review-b"]["start"]
    assert steps["refute"]["needs"] == ["review-a", "review-b"] and steps["recheck"]["needs"] == ["fix"]
    started = workflows.start(store, "dual-review", git_repo, {"target": "the cart module"})
    first = [store.get_task(i) for i in started["queued"]]
    assert len(first) == 2 and all("one of two reviewers working apart" in t["body"] for t in first)
    # neither review is shown the other's: they start together, before anything is found
    assert all("What the steps before it found" not in t["body"] for t in first)


def test_the_panel_starts_four_blind_reviews_each_with_its_own_lens(git_repo, store, monkeypatch):
    from locks import ledger, workflows

    monkeypatch.setattr(workflows, "_kick", lambda cwd, key: None)
    defn = workflows.load("panel", git_repo, "bundled")
    lenses = ["correctness", "security", "performance", "tests"]
    assert next(s for s in defn["steps"] if s["id"] == "refute")["needs"] == lenses
    started = workflows.start(store, "panel", git_repo, {"target": "the cart module"})
    first = [store.get_task(i) for i in started["queued"]]
    assert [t["workflow_step"] for t in first] == lenses
    assert all(lens in t["body"].split("yours is", 1)[1][:20] and "What the steps before it found" not in t["body"]
               for lens, t in zip(lenses, first, strict=True))
    assert "4 reviewers, each through its own lens, worked apart" in ledger.brief(
        "refute", [], round_=1, sha="abc", reviewers=4)


# --- what a running attempt is doing, and the change it kept -----------------------------


def log(entries):
    import os
    from datetime import UTC, datetime

    from locks import signature

    folder = signature.log_dir(dict(os.environ))
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{datetime.now(UTC):%Y-%m-%d}.ndjson").open("a") as fh:
        for task_id, attempt, sig in entries:
            fh.write(json.dumps({"task_id": task_id, "attempt": attempt, "sig": sig}) + "\n")
        fh.write("not json\n")


def test_a_running_attempt_s_phase_is_read_from_its_last_calls():
    from locks import phase

    assert phase.of(1, 1) is None
    log([(1, 1, "read:py"), (1, 1, "grep")])
    assert phase.of(1, 1) == "exploring"
    log([(1, 1, "edit:py"), (2, 1, "bash:pytest")])
    assert phase.of(1, 1) == "implementing" and phase.of(2, 1) == "checking"
    log([(1, 1, "bash:npm:test")])
    assert phase.of(1, 1) == "checking" and phase.of(1, None) == "checking" and phase.of(1, 2) is None
    assert phase.kind_of("write:ts") == "implementing" and phase.kind_of("bash:ls") == "exploring"
    # A worker reads again after it writes: the write, out of its last calls, still counts.
    log([(3, 1, "write:py")] + [(3, 1, "read:py")] * 8)
    assert phase.of(3, 1) == "implementing"


def test_the_board_and_the_drawer_show_the_phase_the_risk_and_the_checks(git_repo, store, verifier_on):
    from locks.ui import api

    script = Script(passing(), check("fail", [BUG]), passing("fixed"), check("pass"), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    detail = api.task_detail(store, report.task_id)
    assert detail["risk"]["tier"] == "high" and len(detail["checks"]) == 2
    assert detail["checks"][0]["findings"][0]["where"] == "app.py:3" and detail["phase"] is None
    running = store.create_task("busy", status="running", source="locks", repo="k", cwd=str(git_repo))
    store.add_event(running["id"], "attempt_started", seq=1, cell="sonnet/medium")
    log([(running["id"], 1, "edit:py")])
    board = api.board(store)
    assert next(t for t in board["running"] if t["id"] == running["id"])["phase"] == "implementing"
    assert api.task_detail(store, running["id"])["phase"] == "implementing"
    diff = api.task_diff(store, report.task_id)
    assert diff["branch"] == report.branch and "app.py" in diff["diff"] and not diff["cut"]
    assert api.task_diff(store, 999) is None
    assert "no worktree" in api.task_diff(store, running["id"])["why"]  # a running task: its worktree, none here
    gone = store.create_task("x", status="done", source="locks", cwd="/nowhere")
    assert "not here" in api.task_diff(store, gone["id"])["why"]


def test_the_diff_route_answers(ui, store, git_repo):  # noqa: F811
    from .test_ui import call

    task = store.create_task("x", status="done", source="locks", cwd=str(git_repo))
    res, data = call(ui, "GET", f"/api/tasks/{task['id']}/diff")
    assert res.status == 200 and "branch" in json.loads(data)
    res, _ = call(ui, "GET", "/api/tasks/99999/diff")
    assert res.status == 404


# --- the ledger: a refuter, never a vote --------------------------------------------------


def _found(*rows):
    from locks import findings

    return findings.read({"findings": [dict(zip(("where", "what", "evidence_class", "cause"), r, strict=True),
                                            proof="a probe shows it" if r[2] == "deterministic" else "")
                                       for r in rows]})


def _decides(summary, **verdicts):
    from locks import ledger
    from locks.launch import WorkerResult

    return WorkerResult(True, None, summary, evidence="read it",
                        verdicts=ledger.read({"verdicts": [{"id": k, "verdict": v} for k, v in verdicts.items()]}))


def _reviewed(summary, *rows):
    from locks.launch import WorkerResult

    return WorkerResult(True, None, summary, evidence="read it", findings=_found(*rows))


def test_the_ledger_adds_reviews_together_and_sends_only_one_sided_readings_to_a_refuter():
    from locks import ledger

    rows = ledger.collect([
        ("review-a", _found(("cart.py:10", "total ignores the discount", "deterministic", "introduced"),
                            ("cart.py:40", "rounding drops a cent", "inferential", "introduced"),
                            ("old.py:1", "an old leak", "deterministic", "pre-existing"))),
        ("review-b", _found(("cart.py:10", "the discount is never applied", "inferential", "introduced"),
                            ("cart.py:41", "rounding drops a cent", "inferential", "introduced"),
                            ("tax.py:5", "tax on the gross", "inferential", "worsened"))),
    ])
    # one reviewer is enough to put a problem on it; a pre-existing one never is
    assert [(r["id"], r["where"], r["seen_by"]) for r in rows] == [
        ("F1", "cart.py:10", ["review-a", "review-b"]), ("F2", "cart.py:40", ["review-a", "review-b"]),
        ("F3", "tax.py:5", ["review-b"])]
    assert [r["id"] for r in rows if ledger.disputed(r)] == ["F3"]
    assert ledger.digest(rows) == ledger.digest(ledger.collect([("review-a", _found(
        ("cart.py:10", "total ignores the discount", "deterministic", "introduced"),
        ("cart.py:40", "rounding drops a cent", "inferential", "introduced"))), ("review-b", _found(
        ("cart.py:10", "the discount is never applied", "inferential", "introduced"),
        ("cart.py:41", "rounding drops a cent", "inferential", "introduced"),
        ("tax.py:5", "tax on the gross", "inferential", "worsened")))]))
    text = ledger.brief("refute", rows[2:], round_=1, sha="abc")
    assert "never decide a row by who reported it" in text and "F3" in text and "confirmed, refuted, unsure" in text
    assert "Two reviewers worked apart" in text
    # One review before it (the bundled feature, bugfix and review-fix): its brief says so.
    alone_text = ledger.brief("refute", rows[2:], round_=1, sha="abc", reviewers=1)
    assert "A review read each row" in alone_text and "Two reviewers" not in alone_text

    refuted = ledger.outcome({"role": "refute", "ids": ["F3"], "agreed": ["F1"]}, [{"id": "F3", "verdict": "x"}])
    assert refuted.confirmed == ["F1"] and refuted.held == ["F3"] and not refuted.stop
    alone = ledger.outcome({"role": "refute", "ids": ["F3"], "agreed": []}, [])
    assert "could not decide F3" in alone.stop
    checked = ledger.outcome({"role": "recheck", "round": 2, "ids": ["F1", "F2"]},
                             [{"id": "F1", "verdict": "fixed"}, {"id": "F2", "verdict": "regression"}])
    assert checked.survivors == ["F2"] and "after 2 fix rounds" in checked.stop
    clean = ledger.outcome({"role": "recheck", "round": 1, "ids": ["F1"], "held": ["F3"]},
                           [{"id": "F1", "verdict": "fixed"}])
    assert not clean.survivors and "could not decide F3" in clean.stop
    # A fix that only wrote things down fixed no row about code, whatever the re-check says.
    noted = ledger.outcome({"role": "recheck", "round": 1, "ids": ["F1", "F2"], "noted_only": ["F1"]},
                           [{"id": "F1", "verdict": "fixed"}, {"id": "F2", "verdict": "fixed"}])
    assert noted.survivors == ["F1"]
    assert ledger.written_down("CLAUDE.md") and ledger.written_down("docs/AGENTS.md")
    assert not ledger.written_down("src/chart.tsx") and ledger.about_notes("README.md:12")
    assert ledger.read({"verdicts": [{"id": "f1", "verdict": " Fixed "}, "junk", {"id": 3}]}) == (
        {"id": "F1", "verdict": "fixed", "proof": ""},)
    assert ledger.read({}) == ()


def test_a_deciding_pass_answers_every_row(tmp_path):
    from locks.launch import parse

    spec = LaunchSpec("p", Cell("sonnet", "medium"), tmp_path, verdicts=("F1", "F2"),
                      verdict_words=("fixed", "not_fixed", "regression"))
    block = {"verdict": "pass", "summary": "checked", "evidence": "read it",
             "verdicts": [{"id": "F1", "verdict": "fixed"}, {"id": "F2", "verdict": "maybe"}]}
    result = parse(proc(block), spec)
    assert not result.passed and "without a verdict on F2" in result.summary
    block["verdicts"][1]["verdict"] = "not_fixed"
    assert parse(proc(block), spec).passed


def _ledger_run(git_repo, store, results, monkeypatch):
    from locks import workflows

    monkeypatch.setattr(workflows, "_kick", lambda cwd, key: None)
    started = workflows.start(store, "dual-review", git_repo, {"target": "the cart"}, session_id="s1")
    seen: list[dict] = []
    from .test_workflows import _runner

    flow.work(store, parallel=False, runner=_runner(results, seen))
    return started, seen, {t["workflow_step"]: t for t in store.run_tasks(started["id"])}


@pytest.mark.usefixtures("no_verifier")
def test_the_dual_review_fixes_only_what_is_confirmed_and_rechecks_each_row(git_repo, store, monkeypatch):
    from locks import workflows

    results = [
        _reviewed("a", ("cart.py:10", "total ignores the discount", "deterministic", "introduced"),
                  ("cart.py:40", "rounding drops a cent", "inferential", "introduced")),
        _reviewed("b", ("cart.py:10", "the discount is never applied", "inferential", "introduced"),
                  ("tax.py:5", "tax on the gross", "inferential", "worsened")),
        _decides("decided", F2="confirmed", F3="refuted"),
        ok("fixed both"),
        _decides("one left", F1="fixed", F2="not_fixed"),
        ok("fixed the rounding"),
        _decides("all fixed", F2="fixed"),
    ]
    started, seen, steps = _ledger_run(git_repo, store, results, monkeypatch)
    _, _, refute, fix1, recheck1, fix2, recheck2 = (s["prompt"] for s in seen)
    # the refuter is given only what one reviewer saw and could only read, never the reviews' own words
    assert "F2" in refute and "F3" in refute and "- F1 " not in refute and "What the steps before" not in refute
    assert "- F1 " in fix1 and "- F2 " in fix1 and "- F3 " not in fix1 and "round 1 of 2" in fix1
    assert "The fix is `git diff" in recheck1 and "fixed, not_fixed, regression" in recheck1
    assert "- F2 " in fix2 and "- F1 " not in fix2 and "round 2 of 2" in fix2
    assert "- F2 " in recheck2 and "- F1 " not in recheck2
    status = workflows.status(store, started["id"])
    assert status["state"] == "done" and status["steps"][3]["tries"] == 2
    decided = store.last_event(steps["refute"]["id"], "finished")["data"]["ledger"]
    assert decided["confirmed"] == ["F1", "F2"] and decided["refuted"] == ["F3"]
    # the session hears of the run once, at its end
    assert [t["workflow_step"] for t in store.unreported("s1")] == ["recheck"]


def test_the_ledger_keeps_every_row_and_a_brief_carries_what_fits():
    from locks import ledger

    assert len(criteria.items("\n".join(f"- item {n}" for n in range(25)))) == 25

    many = _found(*((f"m{n}.py:1", f"problem {n}", "inferential", "introduced") for n in range(45)))
    rows = ledger.collect([("review-a", many), ("review-b", _found(("m3.py:1", "problem 3", "deterministic",
                                                                     "introduced")))])
    assert len(rows) == 45 and rows[-1]["id"] == "F45"
    # shown and agreed rows go first; one row always goes
    one = len(ledger.rows_text([rows[3]])) + 1
    given, waiting = ledger.fit(rows, limit=one)
    assert [r["id"] for r in given] == ["F4"] and len(waiting) == 44
    assert [r["id"] for r in ledger.fit(rows, limit=1)[0]] == ["F4"]
    assert ledger.fit(rows)[1] == []
    text = ledger.brief("fix", given, round_=1, sha="abc", waiting=44)
    assert "44 more row(s)" in text and "the next fix round takes them" in text
    # a refute's rows that did not fit go to the person; a fix's wait for the next round
    refute = ledger.outcome({"role": "refute", "ids": ["F1"], "deferred": ["F2"]}, [{"id": "F1", "verdict": "refuted"}])
    assert refute.held == ["F2"] and "did not fit in the refuter's brief" in refute.stop
    later = ledger.outcome({"role": "recheck", "round": 1, "ids": ["F1"], "deferred": ["F2", "F3"]},
                           [{"id": "F1", "verdict": "fixed"}])
    assert later.survivors == ["F2", "F3"] and not later.stop
    last = ledger.outcome({"role": "recheck", "round": 2, "ids": ["F2"], "deferred": ["F3"]},
                          [{"id": "F2", "verdict": "fixed"}])
    assert last.survivors == ["F3"] and "F3 still open after 2 fix rounds" in last.stop
    assert len(ledger.read({"verdicts": [{"id": f"F{n}", "verdict": "fixed"} for n in range(1, 100)]})) == 99


def test_a_fix_s_rows_that_did_not_fit_go_to_the_next_round(git_repo, store, monkeypatch):
    from locks import ledger

    shown = ("cart.py:10", "total ignores the discount", "deterministic", "introduced")
    monkeypatch.setattr(ledger, "BRIEF_CHARS", 1)
    results = [_reviewed("a", shown, ("tax.py:5", "tax on the gross", "deterministic", "worsened")),
               _reviewed("b"), ok("fixed the total"), _decides("one", F1="fixed"), ok("fixed the tax"),
               _decides("two", F2="fixed")]
    _, seen, steps = _ledger_run(git_repo, store, results, monkeypatch)
    fix1, fix2 = seen[2]["prompt"], seen[4]["prompt"]
    assert "- F1 " in fix1 and "- F2 " not in fix1 and "1 more row(s)" in fix1
    assert "- F2 " in fix2 and "round 2 of 2" in fix2
    assert steps["recheck"]["status"] == "done"


def test_what_the_refuter_cannot_decide_goes_to_the_person(git_repo, store, monkeypatch):
    from locks import workflows

    results = [_reviewed("a", ("tax.py:5", "tax on the gross", "inferential", "introduced")), _reviewed("b"),
               _decides("cannot tell", F1="unsure")]
    started, seen, steps = _ledger_run(git_repo, store, results, monkeypatch)
    assert len(seen) == 3 and steps["refute"]["status"] == "blocked"
    stop = store.last_event(steps["refute"]["id"], "finished")["data"]["stop"]
    assert stop["cause"] == "review" and "could not decide F1" in stop["reason"]
    assert workflows.status(store, started["id"])["state"] == "waits on the orchestrator"


@pytest.mark.usefixtures("no_verifier")
def test_rows_still_open_after_two_rounds_go_to_the_person(git_repo, store, monkeypatch):
    results = [_reviewed("a", ("cart.py:10", "total ignores the discount", "deterministic", "introduced")),
               _reviewed("b"), ok("fixed"), _decides("no", F1="not_fixed"), ok("again"),
               _decides("still no", F1="regression")]
    _, seen, steps = _ledger_run(git_repo, store, results, monkeypatch)
    assert len(seen) == 6 and steps["recheck"]["status"] == "blocked"
    assert "F1 still open after 2 fix rounds" in steps["recheck"]["result"]


def test_with_no_finding_the_ledger_steps_pass_without_a_worker(git_repo, store, monkeypatch):
    from locks import workflows

    results = [_reviewed("a", ("old.py:1", "an old leak", "deterministic", "pre-existing")), _reviewed("b")]
    started, seen, steps = _ledger_run(git_repo, store, results, monkeypatch)
    assert len(seen) == 2 and workflows.status(store, started["id"])["state"] == "done"
    assert steps["fix"]["result"] == "no confirmed finding to fix"
    assert store.last_event(steps["refute"]["id"], "finished")["data"]["skipped"]


def test_a_ledger_step_is_checked_where_it_stands():
    from locks import workflows

    def steps(*more):
        return {"name": "l", "steps": [{"id": "r", "prompt": "Review", "kind": "review-routine"}, *more]}

    with pytest.raises(workflows.WorkflowError, match="ledger is one of"):
        workflows.normalize(steps({"id": "x", "prompt": "p", "ledger": "vote"}))
    with pytest.raises(workflows.WorkflowError, match="after the reviews"):
        workflows.normalize({"name": "l", "steps": [{"id": "x", "prompt": "p", "ledger": "refute"}]})
    with pytest.raises(workflows.WorkflowError, match="after the fix it checks"):
        workflows.normalize(steps({"id": "x", "prompt": "p", "ledger": "recheck"}))
    assert workflows.normalize(steps({"id": "f", "prompt": "p", "ledger": "fix"},
                                     {"id": "c", "prompt": "p", "ledger": "recheck"}))["steps"][2]["ledger"]


def test_the_cli_sets_a_step_s_ledger_part(git_repo, capsys, monkeypatch):
    from locks import cli

    monkeypatch.chdir(git_repo)
    assert cli.main(["workflow", "new", "rv"]) == 0
    assert cli.main(["workflow", "step", "set", "rv", "do", "--prompt", "Review it", "--kind", "review-routine"]) == 0
    assert cli.main(["workflow", "step", "add", "rv", "decide", "--prompt", "Decide", "--ledger", "refute"]) == 0
    assert cli.main(["workflow", "step", "add", "rv", "bad", "--prompt", "x", "--ledger", "vote"]) == 1
    assert "ledger is one of refute, fix, recheck" in capsys.readouterr().err
    assert cli.main(["workflow", "step", "set", "rv", "decide", "--clear", "ledger"]) == 0
    capsys.readouterr()
    assert cli.main(["workflow", "show", "rv", "--json"]) == 0
    assert "ledger" not in json.loads(capsys.readouterr().out)["steps"][1]


def test_the_check_keeps_only_the_neighbours_that_measure(git_repo, store, verifier_on):
    from locks import capabilities as caps

    registry = {"layout-inspector": caps.Capability("layout-inspector", {"command": "li"}),
                "rastro": caps.Capability("rastro", {"command": "rastro"})}
    script = Script(passing(), check("pass"), write="app.py")
    report = run("fix the cart total", git_repo, store, registry=registry, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "done"
    work, verifier = script.specs
    assert set(work.mcp_servers) == {"layout-inspector", "rastro"}
    assert set(verifier.mcp_servers) == {"layout-inspector"}
    assert "mcp__layout-inspector" in verifier.allowed_tools and "mcp__rastro" not in verifier.allowed_tools
    assert "layout-inspector: measure the rendered page" in verifier.append_system_prompt


def test_the_check_of_a_change_to_rendered_ui_measures_it_even_when_the_work_did_not(git_repo, store, verifier_on):
    from locks import capabilities as caps

    registry = {"measure": caps.Capability("measure", {"command": "m"}, when=(), hint="measure the page",
                                           touches=("*.tsx",), measures=True)}
    script = Script(passing(), check("pass"), write="app.py")
    run("fix the cart total", git_repo, store, registry=registry, launcher=script, classifier=kind("implement"))
    assert "measure" not in script.specs[1].mcp_servers  # app.py is no rendered UI
    script = Script(passing(), check("pass"), write="Cart.tsx")
    run("fix the cart colours", git_repo, store, registry=registry, launcher=script, classifier=kind("implement"))
    work, verifier = script.specs
    assert "measure" not in work.mcp_servers and set(verifier.mcp_servers) == {"measure"}
    assert "mcp__measure" in verifier.allowed_tools and "measure: measure the page" in verifier.append_system_prompt


def test_a_problem_in_work_that_ran_out_of_turns_gets_more_turns_not_a_stronger_model(git_repo, store, verifier_on):
    """The palette task: the worker ran out of turns, its check passed in its place, and the
    independent check found a problem. It failed for room: same cell, twice the turns."""
    out_of_turns = WorkerResult(False, Failure.TURNS_EXHAUSTED, "used all 60 turns without a verdict",
                                cost_usd=0.1)
    script = Script(out_of_turns, check("fail", [BUG]), passing("fixed"), check("pass"), write="app.py")
    report = run("fix the cart total", git_repo, store, Options(verify="true"), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "done"
    first, _, again, _ = script.specs
    assert again.cell == first.cell and again.max_turns == first.max_turns * 2
    assert again.timeout_s == launch.timeout_for(again.max_turns)
    attempts = store.attempts(report.task_id)
    assert attempts[0]["failure"] == "turns_exhausted" and "run out of turns" in attempts[0]["summary"]
    # A turn ceiling is no dead end: only the fix that worked is remembered.
    assert [f["outcome"] for f in store.recent_problems()[0]["fixes"]] == ["worked"]


def test_a_spanish_event_name_among_others_is_no_money():
    assert "money" not in _areas("Eventos de bloqueo: `aprobación_cliente`, `pago`, `acceso`")
    assert "money" in _areas("Corrige el pago duplicado del anticipo")


def test_a_workflow_step_s_kept_commit_is_named_by_its_goal_not_the_step_title():
    """Run #19's build ran out of turns with no `commit` line, and its kept commit was titled
    "[feature#19 build] Build it, with its tests": the step's title, with the run's tag."""
    step = {"title": "[feature#19 build] Build it, with its tests",
            "body": "Build this feature: Make workflows faster by running parts in parallel\n\nRead the code."}
    assert orchestrate.commit_title(step) == "Make workflows faster by running parts in parallel"
    assert orchestrate.commit_title({"title": "[bugfix#3 fix] Fix it", "body": ""}) == "Fix it"
    assert orchestrate.commit_title({"title": "Add a flag to the importer"}) == "Add a flag to the importer"


def test_a_step_a_blind_review_follows_runs_no_verifier_of_its_own(git_repo, store, verifier_on):
    """The verifier was 12% of the time, mostly before a review step that reads the same diff."""
    script = Script(passing(), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
                 options=orchestrate.Options(reviewed_next=True))
    assert report.status == "done" and len(script.specs) == 1  # the worker only, no verifier
    assert store.events_of(report.task_id, "verified") == []
    assert any("the blind review step that follows" in r for r in report.plan.reasons)
    assert orchestrate.Options(reviewed_next=True).stored()["reviewed_next"] is True


@pytest.mark.usefixtures("no_verifier")
def test_a_pass_keeps_the_verdict_it_was_held_to(git_repo, store):
    answered = WorkerResult(True, None, "done", "1 passed", cost_usd=0.1,
                            criteria=({"id": "S1", "met": True, "evidence": "  the   export test passes "},
                                      "not a record"))
    script = Script(answered, write="app.py")
    check = "true"
    report = run("Fix the export:\n- S1 keep the header", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), options=Options(verify=check))
    verdict = store.final_event(report.task_id)["data"]["verdict"]
    assert verdict["check"] == {"command": "true", "passed": True} and verdict["attempts"] == 1
    assert verdict["criteria"] == [{"id": "S1", "met": True, "evidence": "the export test passes"}]
    assert verdict["independent"] == {"ran": False, "why": "it changed nothing to check, or the verifier is off"}
    reviewed = run("Fix the export again", git_repo, store, registry={}, launcher=Script(ok(), write="app.py"),
                   classifier=kind("implement"), options=Options(reviewed_next=True))
    found = store.final_event(reviewed.task_id)["data"]["verdict"]
    assert found["check"] is None and "blind review" in found["independent"]["why"]


def test_the_verdict_names_the_independent_check_or_why_it_did_not_run(git_repo, store, verifier_on):
    script = Script(passing(), check("pass"), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert store.final_event(report.task_id)["data"]["verdict"]["independent"] == {
        "ran": True, "passed": True, "blocking": 0}
    calm = run("Explain the cache in the docs", git_repo, store, registry={},
               launcher=Script(ok(), check("pass"), write="NOTES.md"), classifier=kind("docs"))
    independent = store.final_event(calm.task_id)["data"]["verdict"]["independent"]
    assert independent.get("ran") or independent["why"].startswith("its risk")


def _beside(cost=0.05):
    """A launcher whose worker writes app.py (attempt n) and whose verifier passes, recording both."""
    seen: list[LaunchSpec] = []

    def launcher(spec):
        seen.append(spec)
        if "Check this change" in spec.prompt:
            return check("pass")
        workers = sum("Check this change" not in s.prompt for s in seen)
        (spec.target_dir / "app.py").write_text(f"attempt {workers}\n")
        return passing()

    return launcher, seen


def test_the_independent_check_runs_beside_locks_s_check_and_is_told_not_to_run_it(
        git_repo, store, verifier_on, monkeypatch):
    monkeypatch.setenv("LOCKS_OVERLAP", "on")
    launcher, seen = _beside()
    report = run("fix the cart total", git_repo, store, Options(verify="python3 -c 'import time; time.sleep(0.3)'"),
                 registry={}, launcher=launcher, classifier=kind("implement"))
    assert report.status == "done"
    _, verifier = seen
    assert "beside you" in verifier.prompt and "do not run it yourself" in verifier.prompt
    assert "time.sleep(0.3)" in verifier.prompt.split("locks runs the project's check,")[1]
    checked = store.last_event(report.task_id, "verified")["data"]
    assert checked["passed"] and checked["beside_check"]
    # With the setting off, it waits for the check and is told nothing of it.
    monkeypatch.setenv("LOCKS_OVERLAP", "off")
    launcher, seen = _beside()
    run("fix the cart total again", git_repo, store, Options(verify="true"), registry={}, launcher=launcher,
        classifier=kind("implement"))
    assert "beside you" not in seen[1].prompt


def test_a_check_that_fails_drops_what_ran_beside_it_and_keeps_its_cost(git_repo, store, verifier_on, monkeypatch):
    monkeypatch.setenv("LOCKS_OVERLAP", "on")
    launcher, _ = _beside()
    second = ("python3 -c \"import pathlib, sys; sys.exit(0 if 'attempt 2' in pathlib.Path('app.py').read_text() "
              "else 1)\"")
    report = run("fix the cart total", git_repo, store, Options(verify=second), registry={}, launcher=launcher,
                 classifier=kind("implement"))
    assert report.status == "done"
    dropped = store.events_of(report.task_id, "verifier_dropped")
    assert len(dropped) == 1 and dropped[0]["data"]["cost_usd"] == pytest.approx(0.05)
    assert [e["data"]["seq"] for e in store.events_of(report.task_id, "verified")] == [2]
    assert store.attempts(report.task_id)[0]["cost_usd"] == pytest.approx(0.15)


def test_the_independent_check_is_handed_the_code_index_read_only_where_the_repository_has_one(
        git_repo, store, tmp_path, verifier_on):
    from .livespec_fixture import build
    from .test_orchestrate import _livespec

    build(git_repo)
    script = Script(passing(), check("pass"), write="app.py")
    run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
        adapters=[_livespec(tmp_path)])
    worker, verifier = script.specs
    assert worker.mcp_servers["livespec"]["command"] == "uvx"
    server = verifier.mcp_servers["livespec"]
    assert server["command"] == sys.executable and server["args"][:4] == ["-m", "locks.mcp_guard", "livespec", "--"]
    assert "mcp__livespec" in verifier.allowed_tools
    assert "Read-only: a call that would change the index is refused" in verifier.append_system_prompt
    assert f'workspace="{git_repo.resolve()}"' in verifier.append_system_prompt


def test_the_independent_check_gets_no_code_index_where_there_is_none(git_repo, store, tmp_path, verifier_on):
    from .test_orchestrate import _livespec

    script = Script(passing(), check("pass"), write="app.py")
    run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
        adapters=[_livespec(tmp_path)])
    assert "livespec" not in script.specs[1].mcp_servers and "mcp__livespec" not in script.specs[1].allowed_tools


def test_a_late_independent_check_is_handed_the_index_too(git_repo, store, tmp_path, late):
    from .livespec_fixture import build
    from .test_orchestrate import _livespec

    build(git_repo)
    script = Script(passing(), check("pass"), write="app.py")
    run("fix the cart total", git_repo, store, Options(verify="true"), registry={}, launcher=script,
        classifier=kind("implement"), adapters=[_livespec(tmp_path)])
    verifier = script.specs[1]
    assert verifier.mcp_servers["livespec"]["args"][2] == "livespec" and "mcp__livespec" in verifier.allowed_tools




# --- the late independent check -------------------------------------------------------


@pytest.fixture
def late(monkeypatch, verifier_on):
    monkeypatch.setenv("LOCKS_LATE_VERIFIER", "on")


def test_a_late_check_runs_after_the_ending_and_a_blocker_reopens_the_task(git_repo, store, late):
    script = Script(passing(), check("fail", [BUG]), write="app.py")
    report = run("fix the cart total", git_repo, store, Options(verify="true"), registry={}, launcher=script,
                 classifier=kind("implement"))
    # The run ended as a pass before its check ran, and said the check comes after it.
    assert report.status == "done" and report.late_check and "runs now, after this ending" in report.text()
    kinds = [e["kind"] for e in store.events(task_id=report.task_id)]
    assert "verifier_deferred" in kinds
    first_end, late_check = kinds.index("finished"), kinds.index("verified")
    assert first_end < late_check
    # Its first ending's verdict says the check comes after it, never that the risk called for none.
    card = store.events(task_id=report.task_id)[first_end]["data"]["verdict"]["independent"]
    assert card["late"] and "after this ending" in card["why"]
    verified = store.last_event(report.task_id, "verified")["data"]
    assert verified["late"] and verified["blocking"] == 1
    # The blocker reopens it as a review, with an ending of its own, its work on its branch.
    assert kinds.count("finished") == 2 and kinds.index("finished", late_check) > late_check
    finished = store.last_event(report.task_id, "finished")["data"]
    assert finished["stop"]["cause"] == "review"
    assert store.get_task(report.task_id)["status"] != "done"
    assert isolate.branch_for(report.task_id) in subprocess.run(
        ["git", "branch"], cwd=git_repo, capture_output=True, text=True).stdout
    # Its cost is the task's.
    assert store.get_task(report.task_id)["cost_usd"] == pytest.approx(0.15)


def test_a_late_check_that_passes_leaves_the_pass(git_repo, store, late):
    script = Script(passing(), check("pass"), write="app.py")
    report = run("fix the cart total", git_repo, store, Options(verify="true"), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "done" and store.get_task(report.task_id)["status"] == "done"
    assert store.last_event(report.task_id, "verified")["data"]["passed"]
    assert [e["kind"] for e in store.events(task_id=report.task_id)].count("finished") == 1


def test_work_something_waits_on_is_checked_before_its_ending(git_repo, store, late):
    script = Script(passing(), check("fail", [BUG]), passing("fixed"), check("pass"), write="app.py")
    task = store.create_task("fix the cart total", status="queued", source="locks")
    store.create_task("then ship it", status="queued", source="locks",
                      options=json.dumps({"after": {"task": task["id"]}}))
    assert orchestrate._awaited(store, task["id"])
    report = run("fix the cart total", git_repo, store, Options(verify="true"), registry={}, launcher=script,
                 classifier=kind("implement"), task_id=task["id"])
    assert report.status == "done" and not report.late_check
    assert not store.events_of(report.task_id, "verifier_deferred")
    assert [e["data"]["blocking"] for e in store.events_of(report.task_id, "verified")] == [1, 0]


def test_a_fix_of_ledger_rows_is_never_checked_late(git_repo, store, late):
    script = Script(passing(), check("pass"), write="app.py")
    report = run("fix the cart total", git_repo, store, Options(verify="true", ledger={"role": "fix", "ids": ["F1"]}),
                 registry={}, launcher=script, classifier=kind("implement"))
    assert not report.late_check and not store.events_of(report.task_id, "verifier_deferred")


def test_a_pass_with_no_check_of_locks_s_is_never_checked_late(git_repo, store, late):
    script = Script(passing(), check("pass"), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert not report.late_check and not store.events_of(report.task_id, "verifier_deferred")
    assert store.last_event(report.task_id, "verified")["data"]["passed"]


def test_opus_at_low_effort_is_no_cheap_cell_and_a_model_below_it_is():
    assert not orchestrate._cheap(Cell("opus", "low"))
    assert orchestrate._cheap(Cell("sonnet", "low")) and orchestrate._cheap(Cell("haiku", "medium"))
    assert not orchestrate._cheap(Cell("sonnet", "medium"))


def test_what_follows_an_ending_detached_goes_to_a_fresh_process_through_a_file(git_repo, store, late,
                                                                                  monkeypatch):
    started = []
    monkeypatch.setattr(orchestrate, "_spawn", lambda argv, **kw: started.append((argv, kw)))
    script = Script(passing(), check("fail", [BUG]), write="app.py")
    report = run("fix the cart total", git_repo, store, Options(verify="true", detach_after=True), registry={},
                 launcher=script, classifier=kind("implement"))
    # The run ended without its check: a fresh process, never a fork, does it from a file.
    assert report.status == "done" and report.late_check
    assert not store.events_of(report.task_id, "verified")
    (argv, kw), = started
    assert kw["start_new_session"] and argv[0] == sys.executable and "_detached(" in argv[-1]
    spool = re.search(r"_detached\('([^']+)'\)", argv[-1]).group(1)
    orchestrate._detached(spool)
    assert not os.path.exists(spool)
    verified = store.last_event(report.task_id, "verified")["data"]
    assert verified["late"] and verified["blocking"] == 1
    assert store.final_event(report.task_id)["data"]["stop"]["cause"] == "review"


def test_a_detached_process_that_fails_keeps_its_error(store, tmp_path):
    spool = tmp_path / "after-x.pickle"
    spool.write_bytes(b"not a pickle")
    orchestrate._detached(str(spool))
    assert "after-ending" in (home() / "hook-errors.log").read_text()


def test_the_independent_check_is_told_what_the_code_index_says_the_change_reaches(git_repo, store, verifier_on,
                                                                                  tmp_path):
    from locks.adapters.livespec import Livespec

    from .livespec_fixture import build

    build(git_repo)
    index = Livespec(env={"HOME": str(tmp_path / "home"), "PATH": "", "CLAUDE_CONFIG_DIR": str(tmp_path / "cc"),
                          "LOCKS_HOME": str(home())})
    (git_repo / "src").mkdir(exist_ok=True)
    (git_repo / "src" / "billing.py").write_text("total = 0\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "billing"], cwd=git_repo, check=True)
    script = Script(passing(), check("pass"), write="src/billing.py")
    report = run("fix the cart total in billing", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[index])
    assert report.status == "done"
    verifier = script.specs[1].prompt
    assert "code index says this change reaches" in verifier and "SPEC-7" in verifier


def test_what_the_independent_check_writes_in_the_work_it_reads_is_put_back(git_repo, store, verifier_on):
    """The check reads: its write tools are taken away, but a shell writes too. What it wrote in the
    worktree is put back as it was before it ran, so the kept branch holds the worker's work alone."""
    from .conftest import git

    def launcher(spec):
        if spec.env.get("LOCKS_WORKER_ATTEMPT", "").endswith("-verify"):
            (spec.target_dir / "app.py").write_text("the check's edit\n")
            (spec.target_dir / "probe.py").write_text("print('probe')\n")
            return check("pass")
        (spec.target_dir / "app.py").write_text("the worker's fix\n")
        return passing()

    report = run("fix the cart total", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    assert report.status == "done"
    wrote = store.events_of(report.task_id, "verifier_wrote")
    assert wrote and set(wrote[0]["data"]["files"]) == {"app.py", "probe.py"} and wrote[0]["data"]["left"] == []
    branch = isolate.branch_for(report.task_id)
    assert git(git_repo, "show", f"{branch}:app.py") == "the worker's fix"
    assert "probe.py" not in git(git_repo, "ls-tree", "--name-only", branch)


def test_a_stray_path_keeps_an_environment_failure_and_the_check_s_output(git_repo, store):
    """A file outside the surfaces never rewrites how the check failed (issue #119)."""
    def launcher(spec):
        (spec.target_dir / "stray.txt").write_text("x\n")
        return WorkerResult(False, Failure.ENVIRONMENT, "Sail is not running.", "the check could not run",
                            changed_paths=("stray.txt",), check_tail="Sail is not running.")

    report = run("fix it", git_repo, store, Options(surfaces=("src/**",), max_attempts=1), registry={},
                 launcher=launcher, classifier=kind("implement"))
    first = store.attempts(report.task_id)[0]
    assert first["failure"] == "environment" and "outside its surfaces" in first["summary"]
    assert "Sail is not running." in store.check_tails(report.task_id).get(1, "")


def test_carries_work_only_for_a_check_failure_on_changed_files():
    cell = Cell.parse("sonnet/high")
    assert orchestrate.carries_work(Attempt(cell, 40, False, Failure.CODE_BUG, "", changed=("a.py",),
                                            check_tail="FAILED test_a"))
    assert not orchestrate.carries_work(Attempt(cell, 40, False, Failure.APPROACH, "", changed=("a.py",),
                                                check_tail="FAILED test_a"))
    assert not orchestrate.carries_work(Attempt(cell, 40, False, Failure.CODE_BUG, "", changed=("a.py",)))
    assert not orchestrate.carries_work(Attempt(cell, 40, False, Failure.CODE_BUG, "", check_tail="x"))
