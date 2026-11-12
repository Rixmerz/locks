"""LOCKS_TRUNK_ARM forces the trunk's arm for one process and keeps the step out of the trial."""
from __future__ import annotations

import pytest

from locks import trunks
from locks.store import Store


def brief(store: Store, arm: str, why: str, *, passed: bool = True) -> int:
    task = store.create_task("t", status="done", source="locks", repo="r")
    store.add_event(task["id"], "briefed", trunk_arm=arm, trunk_why=why)
    store.add_attempt(task["id"], cell="sonnet/high", max_turns=30, passed=int(passed), input_tokens=10)
    return task["id"]


@pytest.mark.parametrize("value", ["fork", "fresh", " Fresh "])
def test_the_environment_forces_the_arm_before_the_trial(store: Store, value: str):
    got, why = trunks.arm(store, "r", 1, {trunks.FORCE: value})
    assert got == value.strip().lower()
    assert why.startswith(trunks.FORCED_WHY) and trunks.FORCE in why


def test_forcing_needs_no_repository(store: Store):
    assert trunks.arm(store, None, 1, {trunks.FORCE: "fresh"})[0] == "fresh"


@pytest.mark.parametrize("value", ["", "both", "FORK!", "1"])
def test_any_other_value_is_ignored(store: Store, value: str):
    got, why = trunks.arm(store, "r", 1, {trunks.FORCE: value})
    assert not why.startswith(trunks.FORCED_WHY) and got in trunks.ARMS


def test_the_process_environment_is_read_by_default(store: Store, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(trunks.FORCE, "fresh")
    assert trunks.arm(store, "r", 1)[0] == "fresh"


def test_a_forced_step_is_left_out_of_the_trial_counts(store: Store):
    brief(store, "fork", "the trunk's trial here (0 forked, 0 fresh, 10 each)")
    forced = brief(store, "fresh", trunks.arm(store, "r", 2, {trunks.FORCE: "fresh"})[1])
    assert trunks.forced(store, forced)
    for repo in ("r", None):
        found = trunks.trial(store, repo)
        assert found["fork"]["tasks"] == 1 and found["fresh"]["tasks"] == 0
