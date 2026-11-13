from __future__ import annotations

import pytest

from locks.escalate import Attempt, Failure, Move, Skip, decide, repeated_answer
from locks.matrix import LADDERS, Cell

IMPLEMENT = LADDERS["implement"]  # sonnet/medium, sonnet/high, sonnet/xhigh, opus/high
PLAN = LADDERS["plan"]  # opus/xhigh, opus/max, fable/high


def fail(cell: str, failure: Failure, summary: str = "", turns: int = 30) -> Attempt:
    return Attempt(Cell.parse(cell), turns, False, failure, summary)


def broke(cell: str) -> Attempt:
    """A launch that broke (a timeout, no JSON, an errored session): not the work."""
    return Attempt(Cell.parse(cell), 30, False, Failure.ENVIRONMENT, "the CLI produced no JSON", transient=True)


def test_shallow_work_climbs_effort_on_the_same_model():
    d = decide(IMPLEMENT, [fail("sonnet/medium", Failure.CODE_BUG)])
    assert d.move is Move.MORE_EFFORT and d.cell == Cell("sonnet", "high")


def test_when_the_model_has_no_more_effort_the_next_model_starts():
    d = decide(IMPLEMENT, [fail("sonnet/xhigh", Failure.CODE_BUG)])
    assert d.move is Move.NEXT_MODEL and d.cell == Cell("opus", "high")


def test_a_wrong_approach_jumps_model_without_spending_effort_rungs():
    d = decide(IMPLEMENT, [fail("sonnet/medium", Failure.APPROACH)])
    assert d.move is Move.NEXT_MODEL and d.cell == Cell("opus", "high")


def test_two_cells_saying_the_same_thing_jump_model():
    attempts = [
        fail("sonnet/medium", Failure.CODE_BUG, "the parser still accepts unicode digits"),
        fail("sonnet/high", Failure.CODE_BUG, "the parser still accepts unicode digits."),
    ]
    assert repeated_answer(attempts)
    assert decide(IMPLEMENT, attempts).move is Move.NEXT_MODEL


def test_the_same_cell_agreeing_with_itself_is_not_a_repeat():
    attempts = [fail("sonnet/high", Failure.CODE_BUG, "same"), fail("sonnet/high", Failure.CODE_BUG, "same")]
    assert not repeated_answer(attempts)
    empty = [fail("sonnet/medium", Failure.CODE_BUG, ""), fail("sonnet/high", Failure.CODE_BUG, "")]
    assert not repeated_answer(empty)


def test_a_broken_launch_retries_once_then_blocks():
    first = decide(IMPLEMENT, [broke("sonnet/medium")])
    assert first.move is Move.RETRY
    assert first.cell == Cell("sonnet", "medium")
    second = decide(IMPLEMENT, [broke("sonnet/medium")] * 2)
    assert second.move is Move.BLOCKED


def test_an_environment_the_worker_named_blocks_at_once():
    found = decide(IMPLEMENT, [fail("sonnet/medium", Failure.ENVIRONMENT, "Node v22.16 is below the CLI's minimum")])
    assert found.move is Move.BLOCKED
    assert found.cell is None
    assert "the worker found the environment in the way" in found.reason
    assert any("no launch broke" in step for step in found.because)


def test_turn_ceiling_is_raised_once_then_the_task_is_split():
    first = decide(IMPLEMENT, [fail("sonnet/medium", Failure.TURNS_EXHAUSTED, turns=30)])
    assert first.move is Move.MORE_TURNS and first.max_turns == 60 and first.cell == Cell("sonnet", "medium")
    second = decide(IMPLEMENT, [fail("sonnet/medium", Failure.TURNS_EXHAUSTED, turns=30),
                                fail("sonnet/medium", Failure.TURNS_EXHAUSTED, turns=60)])
    assert second.move is Move.REPLAN


@pytest.mark.parametrize("failure", [Failure.SPEC_BUG, Failure.ARCHITECTURE_BUG])
def test_a_wrong_task_replans(failure):
    assert decide(IMPLEMENT, [fail("sonnet/medium", failure)]).move is Move.REPLAN


def test_the_top_of_the_ladder_is_the_end():
    assert decide(IMPLEMENT, [fail("opus/high", Failure.CODE_BUG)]).move is Move.EXHAUSTED
    assert decide(IMPLEMENT, [fail("opus/high", Failure.APPROACH)]).move is Move.REPLAN


def test_fable_waits_for_a_person():
    d = decide(PLAN, [fail("opus/max", Failure.CODE_BUG)])
    assert d.move is Move.NEEDS_APPROVAL and d.cell == Cell("fable", "high")
    assert decide(PLAN, [fail("opus/max", Failure.CODE_BUG)], allow_approval=True).move is Move.NEXT_MODEL


def test_a_cell_off_the_ladder_still_climbs():
    d = decide(IMPLEMENT, [fail("sonnet/low", Failure.CODE_BUG)])
    assert d.move is Move.MORE_EFFORT and d.cell == Cell("sonnet", "medium")
    d = decide(IMPLEMENT, [fail("opus/low", Failure.CODE_BUG)])
    assert d.cell == Cell("opus", "high")


def test_decide_needs_a_failure():
    with pytest.raises(ValueError):
        decide(IMPLEMENT, [])
    with pytest.raises(ValueError):
        decide(IMPLEMENT, [Attempt(Cell("sonnet", "medium"), 30, True)])
    assert decide(IMPLEMENT, [Attempt(Cell("sonnet", "medium"), 30, False)]).move is Move.MORE_EFFORT


def test_a_refused_command_blocks_at_once_and_names_what_to_allow():
    refused = Attempt(Cell("haiku"), 30, False, Failure.PERMISSION, "build refused",
                      ("Bash(npm run build)", "Bash(node server.js)", "Bash(ls)", "Read(x)", "Read(y)"))
    d = decide(LADDERS["implement"], [refused])
    assert d.move is Move.BLOCKED and not d.continues
    assert "Bash(npm run build), Bash(node server.js), Bash(ls) and 2 more" in d.reason and "--allow" in d.reason
    assert "a tool call" in decide(LADDERS["implement"], [Attempt(Cell("haiku"), 30, False, Failure.PERMISSION)]).reason


def test_every_move_says_what_the_attempt_ended_with_the_rule_and_where_it_goes():
    d = decide(IMPLEMENT, [fail("sonnet/medium", Failure.CODE_BUG)])
    assert d.axis == "effort" and d.skipped == ()
    assert d.because[0] == ("attempt 1 at sonnet/medium ended code_bug: the work happened and left a bug or a "
                            "missed edge case")
    assert "shallow" in d.because[1] and d.because[-1] == "next: sonnet/high (the floor for work that needs judgment)"

    d = decide(IMPLEMENT, [fail("sonnet/medium", Failure.APPROACH)])
    assert d.axis == "model" and "the bet that already lost" in d.because[1]
    assert d.because[-1] == "next: opus/high (the floor for work that needs judgment)"
    assert [s.split(":")[0] for s in d.skipped] == ["sonnet/high", "sonnet/xhigh"]

    pair = [fail("sonnet/medium", Failure.CODE_BUG, "the parser still accepts unicode digits"),
            fail("sonnet/high", Failure.CODE_BUG, "the parser still accepts unicode digits.")]
    d = decide(IMPLEMENT, pair)
    assert "attempts 1 (sonnet/medium) and 2 (sonnet/high) failed the same way (code_bug)" in d.because[1]
    assert d.skipped == ("sonnet/xhigh: more effort from sonnet, which is not what failed",)

    d = decide(IMPLEMENT, [fail("sonnet/xhigh", Failure.INCONCLUSIVE)])
    assert "sonnet/xhigh was sonnet's last cell" in d.because[2] and d.because[-1].startswith("next: opus/high")


@pytest.mark.parametrize(
    ("ladder", "attempts", "axis", "phrase"),
    [
        (IMPLEMENT, [broke("sonnet/medium")], "retry", "a retry is not an escalation"),
        (IMPLEMENT, [broke("sonnet/medium")] * 2, "stop", "failed to run 2 times"),
        (IMPLEMENT, [fail("sonnet/medium", Failure.TURNS_EXHAUSTED)], "turns", "60 turns instead of 30"),
        (IMPLEMENT, [fail("sonnet/medium", Failure.TURNS_EXHAUSTED)] * 2, "stop", "raised attempt changed nothing"),
        (IMPLEMENT, [fail("sonnet/medium", Failure.TURNS_EXHAUSTED, turns=200)], "stop", "200 turns is the cap"),
        (IMPLEMENT, [fail("sonnet/medium", Failure.SPEC_BUG)], "stop", "rewritten or split"),
        (IMPLEMENT, [Attempt(Cell("sonnet", "medium"), 30, False, Failure.PERMISSION, "", ("Bash(make)",))],
         "stop", "refused: Bash(make)"),
        (IMPLEMENT, [fail("opus/high", Failure.CODE_BUG)], "stop", "the top of this ladder"),
        (IMPLEMENT, [fail("opus/high", Failure.APPROACH)], "stop", "no model above opus"),
        (PLAN, [fail("opus/max", Failure.CODE_BUG)], "stop", "resume with --allow-approval"),
    ],
)
def test_moves_that_stop_or_stay_explain_themselves(ladder, attempts, axis, phrase):
    d = decide(ladder, attempts)
    assert d.axis == axis and d.because[0].startswith(f"attempt {len(attempts)} at ")
    assert any(phrase in line for line in d.because), d.because


def test_a_raised_attempt_that_still_moved_the_work_is_raised_again_up_to_the_cap():
    """A large removal used all its turns twice while changing dozens of files each
    time; it was sent back to be split when the work was nearly done."""
    def ran_out(turns, changed=()):
        return Attempt(Cell("sonnet", "medium"), turns, False, Failure.TURNS_EXHAUSTED, "", changed=changed)

    d = decide(IMPLEMENT, [ran_out(30, ("a.ts",)), ran_out(60, ("b.ts", "c.ts"))])
    assert d.move is Move.MORE_TURNS and d.max_turns == 120
    assert "changed 2 file(s), so it is raised again" in d.because[1] and "once" not in d.because[2]
    d = decide(IMPLEMENT, [ran_out(30, ("a.ts",)), ran_out(60, ("b.ts",)), ran_out(120, ("c.ts",))])
    assert d.move is Move.MORE_TURNS and d.max_turns == 200
    d = decide(IMPLEMENT, [ran_out(30), ran_out(60), ran_out(120), ran_out(200, ("d.ts",))])
    assert d.move is Move.REPLAN and "200 turns is the cap" in d.because[1]
    assert decide(IMPLEMENT, [ran_out(30, ("a.ts",)), ran_out(60)]).move is Move.REPLAN


def test_a_refusal_names_the_rules_that_let_it_through():
    refused = Attempt(Cell("sonnet", "medium"), 30, False, Failure.PERMISSION, "",
                      ("Bash(tg -n x src; npx tsc --noEmit)",), allow=("Bash(tg:*)", "Bash(npx tsc:*)"))
    d = decide(IMPLEMENT, [refused])
    assert "allow it (--allow 'Bash(tg:*)' --allow 'Bash(npx tsc:*)'), then resume" in d.reason
    assert d.because[-1] == "the rules that let it through, one per program: Bash(tg:*), Bash(npx tsc:*)"


def test_an_attempt_that_was_refused_blocks_whatever_else_it_ended_with():
    """A worker refused its tools, then its check went red: that was read as shallow
    work and climbed to more effort, which met the same refusals."""
    refused = Attempt(Cell("opus", "high"), 30, False, Failure.CODE_BUG, "tests failed", ("Bash(node x.js)",),
                      allow=("Bash(node:*)",))
    d = decide(LADDERS["debug-unclear"], [refused])
    assert d.move is Move.BLOCKED and "--allow 'Bash(node:*)'" in d.reason
    wrong = Attempt(Cell("opus", "high"), 30, False, Failure.SPEC_BUG, "", ("Bash(x)",))
    assert decide(LADDERS["debug-unclear"], [wrong]).move is Move.REPLAN  # a wrong task is still wrong


def test_a_reading_task_whose_cells_agree_has_its_answer():
    """An audit found the same stray references at three cells and climbed to the top
    of its ladder; finding them was the job, done at the first cell."""
    said = "the API still has 'admin' in the baseline migration and the seed script"
    attempts = [fail("opus/high", Failure.CODE_BUG, said), fail("opus/xhigh", Failure.CODE_BUG, said + ".")]
    d = decide(LADDERS["review-critical"], attempts, reading=True)
    assert d.move is Move.CONVERGED and not d.continues and d.axis == "stop"
    assert "the answer is confirmed" in d.because[1]
    # a task that writes still changes model on a repeated answer
    assert decide(LADDERS["review-critical"], attempts).move is Move.REPLAN  # no stronger model left


def test_an_environment_failure_after_work_ran_blocks_at_once():
    import dataclasses

    ran = dataclasses.replace(fail("sonnet/medium", Failure.ENVIRONMENT), changed=("package-lock.json",))
    d = decide(IMPLEMENT, [ran])
    assert d.move is Move.BLOCKED and d.cell is None
    assert "stopped work that had started" in d.reason
    assert any("changed 1 file(s) first" in step for step in d.because)
    # after a retry that never ran, work that then ran still blocks, with no third attempt
    assert decide(IMPLEMENT, [broke("sonnet/medium"), ran]).move is Move.BLOCKED
    # a launch that broke is retried once, as before
    assert decide(IMPLEMENT, [broke("sonnet/medium")]).move is Move.RETRY
    # changed files on another failure change nothing about how it climbs
    bug = dataclasses.replace(fail("sonnet/medium", Failure.CODE_BUG), changed=("a.py",))
    assert decide(IMPLEMENT, [bug]).move is Move.MORE_EFFORT


def dropped(cell: str, changed: tuple[str, ...] = ("app.py",)) -> Attempt:
    """The network went away under the work, after it changed files."""
    return Attempt(Cell.parse(cell), 30, False, Failure.ENVIRONMENT, "the session errored: Connection error",
                   changed=changed, transient=True, network="Connection error")


def test_a_network_that_dropped_earns_the_same_cell_after_a_wait_and_stops_only_when_it_stays_down():
    from locks.escalate import NETWORK_WAITS

    attempts = [fail("sonnet/medium", Failure.CODE_BUG), dropped("sonnet/high")]
    waits = []
    for _ in NETWORK_WAITS:
        d = decide(IMPLEMENT, attempts)
        # Work that changed files is not blocked for a network drop: it goes on from the worktree.
        assert d.move is Move.RETRY and d.cell == Cell("sonnet", "high") and "stay in the worktree" in d.because[2]
        waits.append(d.wait_s)
        attempts.append(dropped("sonnet/high"))
    assert waits == list(NETWORK_WAITS)
    d = decide(IMPLEMENT, attempts)
    assert d.move is Move.BLOCKED and "stayed down" in d.reason and d.wait_s == 0
    # A drop after a try that got somewhere starts the count again.
    again = [*attempts[:-1], fail("sonnet/high", Failure.CODE_BUG), dropped("sonnet/xhigh", changed=())]
    d = decide(IMPLEMENT, again)
    assert d.move is Move.RETRY and d.wait_s == NETWORK_WAITS[0] and "starts it again" in d.because[2]


def timed_out(cell: str) -> Attempt:
    """The worker's run passed; locks' check timed its tests out on a loaded machine."""
    return Attempt(Cell.parse(cell), 30, False, Failure.ENVIRONMENT, "claimed a pass, but the check timed out",
                   changed=("cocha-config.json",), load="thrown: Exceeded timeout of 5000 ms for a test.")


def test_tests_that_timed_out_under_load_earn_the_same_cell_after_a_wait_never_a_climb():
    from locks.escalate import LOAD_WAITS, waited

    attempts = [timed_out("sonnet/medium")]
    waits = []
    for _ in LOAD_WAITS:
        d = decide(IMPLEMENT, attempts)
        assert d.move is Move.RETRY and d.cell == Cell("sonnet", "medium") and "timed out under load" in d.reason
        assert "level halves" in d.because[2]
        waits.append(d.wait_s)
        attempts.append(timed_out("sonnet/medium"))
    assert waits == list(LOAD_WAITS) and waited(attempts[0]) and not waited(fail("sonnet/medium", Failure.CODE_BUG))
    d = decide(IMPLEMENT, attempts)
    assert d.move is Move.BLOCKED and "kept timing out under load" in d.reason and "work is kept" in d.reason
    # A load timeout between two answers that repeat does not hide them.
    same = "the total ignores the discount"
    pair = [fail("sonnet/medium", Failure.CODE_BUG, same), timed_out("sonnet/high"),
            fail("sonnet/high", Failure.CODE_BUG, same)]
    assert decide(IMPLEMENT, pair).move is Move.NEXT_MODEL


def test_a_network_try_between_two_answers_does_not_hide_that_they_repeat():
    same = "the total ignores the discount"
    pair = [fail("sonnet/medium", Failure.CODE_BUG, same), fail("sonnet/high", Failure.CODE_BUG, same)]
    assert decide(IMPLEMENT, pair).move is Move.NEXT_MODEL
    between = [pair[0], dropped("sonnet/high", changed=()), pair[1]]
    assert decide(IMPLEMENT, between).move is Move.NEXT_MODEL


def unreadable(cell: str) -> Attempt:
    return Attempt(Cell.parse(cell), 80, False, Failure.INCONCLUSIVE, "claimed a pass, but its plan cannot run in "
                   "parts: part 'x' has no prompt", shape="its plan cannot run in parts: part 'x' has no prompt")


def test_an_answer_of_the_wrong_form_is_asked_again_of_the_same_cell_once_then_climbs():
    first = decide(PLAN, [unreadable("opus/xhigh")])
    assert first.move is Move.RETRY and first.cell == Cell.parse("opus/xhigh") and first.reshape
    assert first.max_turns == 80 and "only the answer's form failed" in " ".join(first.because)
    again = decide(PLAN, [unreadable("opus/xhigh"), unreadable("opus/xhigh")])
    assert again.move is Move.MORE_EFFORT and not again.reshape
    # Shallow work stays on the effort axis: a form is only a form.
    assert not decide(PLAN, [fail("opus/xhigh", Failure.INCONCLUSIVE, "no evidence")]).reshape


def test_a_second_correction_in_a_row_on_one_model_hands_the_work_to_a_stronger_one():
    def corrected(cell: str, what: str) -> Attempt:
        return Attempt(Cell.parse(cell), 30, False, Failure.CODE_BUG, f"{cell} passed its own check; {what}",
                       blockers=({"where": "src/zip.py:10", "what": what},))

    # One correction is the shallow kind: the same model, more thorough.
    first = decide(IMPLEMENT, [corrected("sonnet/medium", "the declared size is trusted")])
    assert first.move is Move.MORE_EFFORT and first.cell == Cell("sonnet", "high")
    # A second one that still leaves blockers goes to the next model, not the next effort.
    second = decide(IMPLEMENT, [corrected("sonnet/medium", "the declared size is trusted"),
                                corrected("sonnet/high", "local headers are not checked")])
    assert second.move is Move.NEXT_MODEL and second.cell.model == "opus"
    assert any("two corrections in a row" in line for line in second.because)
    # A plain shallow failure after a correction still climbs effort.
    third = decide(IMPLEMENT, [corrected("sonnet/medium", "x"), fail("sonnet/high", Failure.CODE_BUG)])
    assert third.move is Move.MORE_EFFORT


def test_a_task_failure_on_work_that_stands_is_a_decision_not_a_replan():
    from locks.escalate import Attempt, Failure, Move, decide
    from locks.matrix import ladder_for

    ladder = ladder_for("implement")
    cell = ladder[0]
    stands = Attempt(cell, 30, False, Failure.ARCHITECTURE_BUG, "one point needs a design decision",
                     changed=("a.py",), stands=True)
    decision = decide(ladder, [stands])
    assert decision.move is Move.DECIDE and not decision.continues
    assert '"one point needs a design decision"' in decision.reason and "kept on its branch" in decision.reason
    # Work that does not stand is a wrong task, as before, and so is one found wrong under a refusal.
    plain = Attempt(cell, 30, False, Failure.ARCHITECTURE_BUG, "x", changed=("a.py",))
    assert decide(ladder, [plain]).move is Move.REPLAN
    refused = Attempt(cell, 30, False, Failure.ARCHITECTURE_BUG, "x", denied=("Bash(npm test)",), stands=True)
    assert decide(ladder, [refused]).move is Move.REPLAN


def test_several_pieces_that_outgrow_their_turns_are_split_not_given_more():
    out = [fail("opus/high", Failure.TURNS_EXHAUSTED, turns=120)]
    d = decide(IMPLEMENT, out, several="8 points: 3 criteria, reaches 27 symbols")
    assert d.move is Move.REPLAN and d.split and "split what is left" in d.reason
    assert "8 points" in " ".join(d.because)
    # one piece outgrowing its turns is room: more turns, as before
    assert decide(IMPLEMENT, out).move is Move.MORE_TURNS


def failing(cell: str, *ids: str, summary: str = "the check still fails") -> Attempt:
    """An attempt whose claimed pass locks' check turned down, naming the tests that failed it."""
    return Attempt(Cell.parse(cell), 30, False, Failure.CODE_BUG, summary, failing=frozenset(ids))


def test_the_same_failing_tests_on_two_cells_never_earn_a_second_climb():
    first = failing("sonnet/medium", "FAIL a.test.js", "FAIL b.test.js")
    assert decide(IMPLEMENT, [first]).move is Move.MORE_EFFORT  # the first climb is earned
    again = failing("sonnet/high", "FAIL b.test.js", "FAIL a.test.js", summary="the check fails differently worded")
    d = decide(IMPLEMENT, [first, again])
    assert d.move is Move.BLOCKED
    assert not d.continues
    assert d.stuck
    assert "2 failing test(s)" in d.reason
    assert "sonnet/medium" in d.reason
    assert "sonnet/high" in d.reason
    assert "repeat" in " ".join(d.because)
    # Not the same tests, or none read from the output: climbs as before.
    assert decide(IMPLEMENT, [first, failing("sonnet/high", "FAIL a.test.js")]).continues
    assert decide(IMPLEMENT, [fail("sonnet/medium", Failure.CODE_BUG, "x"),
                              fail("sonnet/high", Failure.CODE_BUG, "y")]).continues
    # A try the network dropped under says nothing about them, and does not hide them.
    assert decide(IMPLEMENT, [first, dropped("sonnet/high"), again]).move is Move.BLOCKED
    # Nor does a task that only reads: two cells agreeing is its answer.
    alike = failing("sonnet/high", "FAIL b.test.js", "FAIL a.test.js")
    assert decide(IMPLEMENT, [first, alike], reading=True).move is Move.CONVERGED


def test_a_check_that_could_not_be_compared_with_its_base_blocks_without_a_climb():
    unknown = Attempt(Cell.parse("sonnet/medium"), 30, False, Failure.ENVIRONMENT, "claimed a pass, but the check "
                      "failed", changed=("f.py",), base_unknown="the base run timed out; 3 failing test(s)")
    d = decide(IMPLEMENT, [unknown])
    assert d.move is Move.BLOCKED
    assert not d.continues
    assert d.reason.startswith("base unknown")
    assert "the base run timed out" in d.reason
    assert "resume" in d.reason
    assert "no stronger cell" in " ".join(d.because)


# --- a standard's skip of a rung ------------------------------------------------------------------

def test_a_standard_skip_sends_the_climb_past_its_rung_and_names_the_change():
    feature = LADDERS["feature"]  # sonnet/high, sonnet/xhigh, opus/high, opus/xhigh
    skips = {("sonnet/xhigh", "opus/high"): Skip(Cell("opus", "xhigh"), 7, "0 of 4 passed")}
    d = decide(feature, [fail("sonnet/xhigh", Failure.APPROACH)], skips=skips)
    # A new model was due anyway; the skip sends it one rung further, fresh.
    assert d.move is Move.NEXT_MODEL and d.cell == Cell("opus", "xhigh") and d.standard == 7
    assert any("opus/high: skipped by standard change #7" in s for s in d.skipped)
    assert any("standard change #7" in b for b in d.because) and d.because[-1].startswith("next: opus/xhigh")
    # A climb the skip does not name is left as it was.
    other = decide(feature, [fail("sonnet/high", Failure.CODE_BUG)], skips=skips)
    assert other.cell == Cell("sonnet", "xhigh") and other.standard is None


def test_a_skip_never_turns_more_effort_into_another_model():
    # Effort and model are different dials: shallow work past sonnet's last effort stays the climb
    # it was, whatever a skip says.
    skips = {("sonnet/high", "sonnet/xhigh"): Skip(Cell("opus", "high"), 7, "0 of 4 passed")}
    d = decide(IMPLEMENT, [fail("sonnet/high", Failure.CODE_BUG)], skips=skips)
    assert d.move is Move.MORE_EFFORT and d.cell == Cell("sonnet", "xhigh") and d.standard is None


def test_a_skip_that_stays_on_the_model_is_more_effort():
    skips = {("sonnet/medium", "sonnet/high"): Skip(Cell("sonnet", "xhigh"), 2)}
    d = decide(IMPLEMENT, [fail("sonnet/medium", Failure.CODE_BUG)], skips=skips)
    assert d.move is Move.MORE_EFFORT and d.cell == Cell("sonnet", "xhigh") and d.standard == 2


def test_a_skip_never_lands_on_a_rung_that_needs_an_approval_nor_touches_a_retry():
    fable = {("opus/xhigh", "opus/max"): Skip(Cell("fable", "high"), 3)}
    d = decide(PLAN, [fail("opus/xhigh", Failure.CODE_BUG)], skips=fable)
    assert d.cell == Cell("opus", "max") and d.standard is None
    skips = {("sonnet/medium", "sonnet/medium"): Skip(Cell("sonnet", "xhigh"), 4)}
    assert decide(IMPLEMENT, [broke("sonnet/medium")], skips=skips).move is Move.RETRY
    turns = decide(IMPLEMENT, [fail("sonnet/medium", Failure.TURNS_EXHAUSTED)], skips=skips)
    assert turns.move is Move.MORE_TURNS and turns.standard is None


def test_a_worker_the_guard_refused_locks_work_is_a_decision_not_a_climb():
    """Task 132 asked its worker to start a workflow run: the guard refused it, and the
    inconclusive answer climbed effort twice before a third cell called it a spec_bug."""
    from locks.escalate import WORKER_GUARD
    met = Attempt(Cell("sonnet", "medium"), 30, False, Failure.INCONCLUSIVE, "could not reproduce",
                  evidence=f"$ bin/locks workflow run x -> 'locks: this is a worker of task #1: {WORKER_GUARD}.'")
    d = decide(IMPLEMENT, [met])
    assert d.move is Move.DECIDE and "never starts" in d.reason
    assert decide(IMPLEMENT, [Attempt(Cell("sonnet", "medium"), 30, False, Failure.INCONCLUSIVE,
                                          "no verdict")]).move is not Move.DECIDE
