"""The dossier trial: whether livespec's dossier in a brief helps, tried against its absence."""
from __future__ import annotations

import pytest

from locks import dossier
from locks.store import Store

from .test_analysis import Calling, _indexed_run


def _seed(store: Store, arm: str, passed: bool, *, nature: str = "write", repo: str = "r", seconds: float = 5.0) -> int:
    task = store.create_task("t", status="done", source="locks", repo=repo, kind="implement")
    store.add_event(task["id"], "briefed", **{dossier.KEY: arm, dossier.NATURE_KEY: nature})
    store.add_attempt(task["id"], cell="sonnet/high", max_turns=10, passed=int(passed), duration_s=seconds,
                      input_tokens=100)
    return task["id"]


def _fill(store: Store, *, dossier_passes: int, none_passes: int, n: int = dossier.TRIAL, seconds=(5.0, 5.0)):
    for i in range(n):
        _seed(store, "dossier", i < dossier_passes, seconds=seconds[0])
        _seed(store, "none", i < none_passes, seconds=seconds[1])


def test_the_arms_alternate_until_each_has_the_trial_s_first_tries(store: Store):
    arm, why = dossier.arm(store, "r", 1, "write")
    assert arm in dossier.ARMS and "write dossier trial here" in why and f"{dossier.TRIAL} each" in why
    for _ in range(dossier.TRIAL - 1):
        _seed(store, "dossier", True)
        _seed(store, "none", True)
    assert "trial here" in dossier.arm(store, "r", 99, "write")[1]


def test_a_full_trial_keeps_the_dossier_only_where_its_arm_passed_more_often(store: Store):
    _fill(store, dossier_passes=10, none_passes=6)
    arm, why = dossier.arm(store, "r", 500, "write")
    assert arm == "dossier" and "won its trial" in why
    # the other nature of work has its own trial, still short
    assert "trial here" in dossier.arm(store, "r", 500, "read")[1]


def test_a_full_trial_drops_the_dossier_where_its_arm_passed_less_often(store: Store):
    _fill(store, dossier_passes=3, none_passes=9)
    arm, why = dossier.arm(store, "r", 500, "write")
    assert arm == "none" and "lost its trial" in why


def test_a_tie_goes_to_the_arm_without_the_dossier(store: Store):
    _fill(store, dossier_passes=8, none_passes=8)
    arm, why = dossier.arm(store, "r", 500, "write")
    assert arm == "none" and "a tie goes to the arm without the change" in why


def test_a_dossier_that_passes_as_often_but_slower_loses(store: Store):
    _fill(store, dossier_passes=8, none_passes=8, seconds=(100.0, 10.0))
    arm, why = dossier.arm(store, "r", 500, "write")
    assert arm == "none" and "median" in why


def test_a_repository_short_of_the_trial_takes_the_verdict_of_every_repository(store: Store):
    for i in range(dossier.TRIAL):
        _seed(store, "dossier", True, repo="other")
        _seed(store, "none", i < 4, repo="other")
    arm, why = dossier.arm(store, "r", 500, "write")
    assert arm == "dossier" and "pooled from every repository" in why


def test_a_resume_keeps_the_arm_its_first_brief_drew(store: Store):
    _fill(store, dossier_passes=3, none_passes=9)  # a new task would draw `none`
    task = store.create_task("t", status="done", source="locks", repo="r", kind="implement")
    store.add_event(task["id"], "briefed", **{dossier.KEY: "dossier", dossier.NATURE_KEY: "write"})
    assert dossier.arm_for(store, "r", task["id"], "write") == ("dossier", "kept from its first brief")


def test_a_task_with_no_repository_is_in_no_trial(store: Store):
    assert dossier.arm(store, None, 1, "write")[0] == "dossier"


@pytest.mark.parametrize("kind,nature", [("implement", "write"), ("explore", "read"), ("plan", "read")])
def test_a_kind_s_nature_is_reading_or_writing(kind, nature):
    assert dossier.nature_of(kind) == nature


MARKER = "What livespec's index holds on this request"


def _prompt(git_repo, tmp_path, store) -> str:
    from locks.orchestrate import run

    from .test_analysis import kind
    from .test_orchestrate import _livespec

    launcher = Calling(["Read"], 1000, store)
    run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"),
        adapters=[_livespec(tmp_path)])
    return "\n".join(s.prompt for s in launcher.specs)


def test_the_none_arm_leaves_the_dossier_out_of_the_workers_brief_and_locks_still_reads_the_index(
        git_repo, tmp_path, store, monkeypatch):
    from .livespec_fixture import build

    build(git_repo)
    monkeypatch.setenv("LOCKS_DOSSIER_TRIAL", "on")
    _fill(store, dossier_passes=3, none_passes=9)
    # `_fill` seeds repo "r"; the run's repository key is its own, so make its arms the lost trial there too.
    monkeypatch.setattr(dossier, "arm", lambda *a, **k: ("none", "the dossier lost its trial here"))
    assert MARKER not in _prompt(git_repo, tmp_path, store)
    report = _indexed_run(git_repo, tmp_path, store, "parse_amount rounds wrong", ["Read"], 1000)
    briefed = store.events_of(report.task_id, "briefed")[-1]["data"]
    assert briefed["dossier_arm"] == "none" and briefed["dossier_nature"] == "write"
    assert briefed["dossier_why"] and "livespec_chars" not in briefed
    planned = store.events_of(report.task_id, "planned")[-1]["data"]
    assert any("from its index are in the brief" in r for r in planned["reasons"])  # locks read it for itself


def test_the_dossier_arm_briefs_the_dossier_and_keeps_its_size(git_repo, tmp_path, store, monkeypatch):
    from .livespec_fixture import build

    build(git_repo)
    monkeypatch.setenv("LOCKS_DOSSIER_TRIAL", "on")
    monkeypatch.setattr(dossier, "arm", lambda *a, **k: ("dossier", "the dossier won its trial here"))
    assert MARKER in _prompt(git_repo, tmp_path, store)
    report = _indexed_run(git_repo, tmp_path, store, "parse_amount rounds wrong", ["Read"], 1000)
    briefed = store.events_of(report.task_id, "briefed")[-1]["data"]
    assert briefed["dossier_arm"] == "dossier" and briefed["livespec_chars"] > 0


def test_the_trial_off_briefs_the_dossier_and_draws_no_arm(git_repo, tmp_path, store, monkeypatch):
    from .livespec_fixture import build

    build(git_repo)
    monkeypatch.setenv("LOCKS_DOSSIER_TRIAL", "off")
    assert MARKER in _prompt(git_repo, tmp_path, store)
    report = _indexed_run(git_repo, tmp_path, store, "parse_amount rounds wrong", ["Read"], 1000)
    briefed = store.events_of(report.task_id, "briefed")[0]["data"]
    assert "dossier_arm" not in briefed and briefed["livespec_chars"] > 0
