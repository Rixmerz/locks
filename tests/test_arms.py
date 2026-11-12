"""A full two-arm trial's verdict: a change must earn its place (`arms.verdict`).

The change wins only on a higher first-try rate, or on an equal rate with both arms timed and the change's
median pass strictly faster. A tie goes to the arm without the change.
"""
from __future__ import annotations

from locks import arms

ARMS = ("with", "without")


def rows(arm: str, *, passed: int, of: int = 4, took: float | None = None) -> list[dict]:
    """`of` first tries on `arm`, `passed` of them passing; each pass timed at `took` when given."""
    out = []
    for i in range(of):
        row = {"arm": arm, "tries": 1, "passed": int(i < passed)}
        if took is not None:
            row["duration_s"] = took
        out.append(row)
    return out


def verdict_of(*rows_: dict) -> tuple[str, str]:
    return arms.verdict(arms.tally(rows_, ARMS), ARMS, "diagnosis")


def test_a_higher_first_try_rate_keeps_the_change_even_untimed():
    side, why = verdict_of(*rows("with", passed=4), *rows("without", passed=3))
    assert side == "with" and why == "the diagnosis won its trial here: first tries passed 100% with and 75% without"


def test_an_equal_rate_keeps_the_change_only_when_it_is_timed_and_faster():
    side, why = verdict_of(*rows("with", passed=4, took=60.0), *rows("without", passed=4, took=100.0))
    assert side == "with"
    assert why == ("the diagnosis won its trial here: first tries passed 100% with and 100% without, "
                   "passes took 60s with and 100s without")


def test_an_equal_rate_with_no_timing_goes_to_the_arm_without_the_change():
    side, why = verdict_of(*rows("with", passed=4), *rows("without", passed=4))
    assert side == "without"
    assert why == ("the diagnosis lost its trial here: first tries passed 100% with and 100% without; "
                   "a tie goes to the arm without the change")


def test_an_equal_rate_with_the_change_slower_loses_within_the_slower_margin():
    # 110s against 100s: within SLOWER, but not faster, so the tie goes to the arm without the change.
    side, why = verdict_of(*rows("with", passed=4, took=110.0), *rows("without", passed=4, took=100.0))
    assert side == "without"
    assert why.startswith("the diagnosis lost its trial here: first tries passed 100% with and 100% without, "
                          "passes took 110s with and 100s without")
    assert why.endswith("a tie goes to the arm without the change")


def test_an_equal_rate_with_the_change_as_fast_as_the_other_loses():
    side, why = verdict_of(*rows("with", passed=4, took=100.0), *rows("without", passed=4, took=100.0))
    assert side == "without" and "a tie goes to the arm without the change" in why


def test_an_equal_rate_with_the_change_slower_past_the_margin_loses_on_time():
    side, why = verdict_of(*rows("with", passed=4, took=130.0), *rows("without", passed=4, took=100.0))
    assert side == "without"
    assert why == ("the diagnosis lost its trial here: first tries passed 100% with and 100% without, "
                   "and the passes took 130s at the median with against 100s without; "
                   "a tie goes to the arm without the change")


def test_a_lower_first_try_rate_loses_even_when_the_change_is_faster():
    side, why = verdict_of(*rows("with", passed=3, took=50.0), *rows("without", passed=4, took=100.0))
    assert side == "without"
    assert why == "the diagnosis lost its trial here: first tries passed 75% with and 100% without"


def test_a_higher_rate_still_loses_when_its_passes_are_slower_past_the_margin():
    # The SLOWER rule is unchanged: a higher rate does not excuse a median more than 1.25 times the other's.
    side, why = verdict_of(*rows("with", passed=4, took=200.0), *rows("without", passed=3, took=100.0))
    assert side == "without" and why.startswith("the diagnosis lost its trial here: first tries passed 100%")


def test_a_full_trial_verdict_reads_the_whole_trial_here_or_everywhere():
    found = arms.tally(rows("with", passed=4, took=60.0) + rows("without", passed=4, took=100.0), ARMS)
    assert arms.verdict(found, ARMS, "x", where="everywhere")[1].startswith("the x won its trial everywhere")


def test_a_task_a_verdict_placed_is_no_sample_of_the_trial(store) -> None:  # type: ignore[no-untyped-def]
    """Counted, the tasks after a verdict fed only the arm that won; its rate drifted to the mean and the
    verdict turned over: on one machine the primer 'won' for 29 briefs and 'lost' for 21."""
    def task(arm: str, why: str, passed: bool) -> None:
        t = store.create_task("x", status="done", source="queue", repo="r")["id"]
        store.add_event(t, "briefed", recall_arm=arm, recall_why=why)
        store.add_attempt(t, seq=1, cell="sonnet/medium", max_turns=10, passed=passed, failure=None, summary="",
                          evidence="", cost_usd=0.0, input_tokens=10, output_tokens=1, turns=1, duration_s=1.0,
                          changed_paths=[], capabilities=[])

    for _ in range(3):
        task("fused", "the recall order trial here (1 fused, 1 ordered, 6 each)", True)
        task("ordered", "the recall order trial here (1 fused, 1 ordered, 6 each)", True)
    for _ in range(5):
        task("ordered", "the recall order lost its trial here: first tries passed 100% fused and 100% ordered", False)
        task("fused", "the recall order won its trial everywhere: …; pooled from every repository while …", False)
    rows = store.arm_trial("r", "recall_arm")
    assert sorted(r["arm"] for r in rows) == ["fused"] * 3 + ["ordered"] * 3
    assert all(r["passed_first"] for r in rows)
