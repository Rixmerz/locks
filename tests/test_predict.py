"""A start raised one rung on the record of the task's shape, where the rung above rescued its failures."""
from __future__ import annotations

from locks import analysis, predict
from locks.matrix import LADDERS, Cell
from locks.orchestrate import run
from locks.store import Store

from .test_orchestrate import Script, kind, ok

SHAPE = {"kind": "implement", "points": 3, "tier": "medium", "areas": "one"}


def planned_task(store: Store, *tries, repo="r", points=3, tier="medium", areas=0, predicted=None, pinned=False):
    """A finished task of a shape: its planned event and each try as (cell, passed)."""
    task = store.create_task("t", status="done" if any(p for _, p in tries) else "failed", source="locks",
                             repo=repo, kind="implement", points=points, pinned=int(pinned))
    risk = {"tier": tier, "reasons": [f"about {chr(97 + n)}: word" for n in range(areas)]}
    store.add_event(task["id"], "planned", kind="implement", points=points, risk=risk, start=tries[0][0],
                    **({predict.KEY: predicted} if predicted else {}))
    for cell, passed in tries:
        store.add_attempt(task["id"], cell=cell, max_turns=30, passed=int(passed))
    return task["id"]


def test_a_shape_that_failed_its_first_tries_here_starts_where_its_climbs_were_rescued(store: Store):
    ladder = LADDERS["implement"]
    start = Cell("sonnet", "medium")
    assert predict.shape("implement", 3, "medium", 2) == {**SHAPE, "areas": "several"}
    assert predict.of_planned({"kind": "implement", "points": 3, "risk": {"tier": "medium", "reasons": []}}) == SHAPE
    assert predict.of_planned({"kind": "implement"}) is None
    # Under `SAMPLES` first tries of the shape: nothing.
    for _ in range(predict.SAMPLES - 1):
        planned_task(store, ("sonnet/medium", False), ("sonnet/high", True))
    assert predict.start(store, "r", "implement", SHAPE, start, ladder) is None
    # Four first tries, one passed: under `BELOW`; three climbed to sonnet/high and passed: rescued.
    planned_task(store, ("sonnet/medium", True))
    got = predict.start(store, "r", "implement", SHAPE, start, ladder)
    assert got is not None and got[0] == Cell("sonnet", "high")
    assert got[1] == {"shape": SHAPE, "from": "sonnet/medium", "to": "sonnet/high", "tries": 4, "passed": 1,
                      "climbs": 3, "rescued": 3}
    assert predict.line(got[1]) == ("one rung up, predicted from this repository's record: implement tasks of 3 "
                                    "points, medium risk, one area(s) passed their first try 1 of 4 time(s) at "
                                    "sonnet/medium, and the climb to sonnet/high rescued 3 of 3 of them")
    # Another shape (high risk) has its own record; a reading kind, no repository, the ladder's top: none.
    assert predict.start(store, "r", "implement", {**SHAPE, "tier": "high"}, start, ladder) is None
    assert predict.start(store, "r", "explore", SHAPE, start, ladder) is None
    assert predict.start(store, None, "implement", SHAPE, start, ladder) is None
    assert predict.start(store, "r", "implement", SHAPE, ladder[-1], ladder) is None


def test_a_rung_that_did_not_rescue_the_shape_s_failures_predicts_nothing(store: Store):
    ladder, start = LADDERS["implement"], Cell("sonnet", "medium")
    for _ in range(predict.SAMPLES):
        planned_task(store, ("sonnet/medium", False), ("sonnet/high", False), ("sonnet/xhigh", True))
    # Every first try failed, and every climb to sonnet/high failed too: the rung above is no prediction.
    assert predict.start(store, "r", "implement", SHAPE, start, ladder) is None
    # A pinned task and a predicted start teach the shape nothing of its start.
    planned_task(store, ("sonnet/medium", False), ("sonnet/high", True), pinned=True)
    planned_task(store, ("sonnet/high", True), predicted={"from": "sonnet/medium"})
    seen = predict.record(predict.history(store, "r", "implement"), SHAPE, "sonnet/medium", "sonnet/high")
    assert seen == {"tries": predict.SAMPLES, "passed": 0, "climbs": predict.SAMPLES, "rescued": 0}


def test_the_plan_starts_one_rung_up_on_the_prediction_and_says_why(git_repo, store: Store, monkeypatch):
    from locks import repo as repo_mod

    monkeypatch.setenv("LOCKS_PREDICT", "on")
    key = repo_mod.key(git_repo)
    for _ in range(predict.SAMPLES):
        planned_task(store, ("sonnet/medium", False), ("sonnet/high", True), repo=key, points=1)  # the request's size
    script = Script(ok())
    result = run("add a --json flag to the export command", git_repo, store, registry={},
                 classifier=kind("implement"), launcher=script)
    assert result.plan.start == Cell("sonnet", "high") and result.plan.predicted["tries"] == predict.SAMPLES
    assert any(r.startswith("one rung up, predicted from this repository's record") for r in result.plan.reasons)
    assert script.specs[0].cell == Cell("sonnet", "high")
    planned = store.last_event(result.task_id, "planned")["data"]
    assert planned[predict.KEY]["to"] == "sonnet/high" and planned["start"] == "sonnet/high"
    # Off, or a pinned start: the rules alone.
    monkeypatch.setenv("LOCKS_PREDICT", "off")
    result = run("add a --csv flag to the export command", git_repo, store, registry={},
                 classifier=kind("implement"), launcher=Script(ok()))
    assert result.plan.start == Cell("sonnet", "medium") and result.plan.predicted is None
    # `locks stats` shows the predicted starts and how their first tries went.
    report = analysis.compute(store, days=0, repo=key)
    rows = [r for r in report["predicted"] if r["start"] == "sonnet/high"]
    assert rows and rows[0]["predicted"] == 1 and rows[0]["passed_first"] == 1
    assert "starts raised on the record of the task's shape" in analysis.text(report)


def test_a_predicted_start_never_lowers_and_never_moves_a_probation_start(store: Store):
    ladder = LADDERS["implement"]
    for _ in range(predict.SAMPLES):
        planned_task(store, ("sonnet/high", False), ("sonnet/xhigh", True))
    # The record is of sonnet/high: a task starting at sonnet/medium reads nothing from it.
    assert predict.start(store, "r", "implement", SHAPE, Cell("sonnet", "medium"), ladder) is None
    got = predict.start(store, "r", "implement", SHAPE, Cell("sonnet", "high"), ladder)
    assert got is not None and got[0] == Cell("sonnet", "xhigh")
