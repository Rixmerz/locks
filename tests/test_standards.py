from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from locks import __version__, standards
from locks.escalate import MAX_TURNS_CEILING
from locks.matrix import LADDERS, Cell, turns_for
from locks.orchestrate import Options, run
from locks.store import Store

from .test_orchestrate import Script, kind, ok

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
IMPLEMENT = LADDERS["implement"]  # sonnet/medium → sonnet/high → sonnet/xhigh → opus/high


def stamp(moment: datetime) -> str:
    """A time as the store writes it: UTC."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def finished(store: Store, *tries, repo="r", kind="implement", points=3, cost=1.0, wall=600.0, at=None,
             pinned=False, used=(), status=None, options=None) -> int:
    """A fake finished task: each try is (cell, passed[, failure[, move[, turns]]])."""
    passed = any(t[1] for t in tries)
    task = store.create_task("t", status=status or ("done" if passed else "failed"), source="locks", repo=repo,
                             kind=kind, points=points, pinned=int(pinned), cost_usd=cost,
                             options=json.dumps(options or {}))
    store.add_event(task["id"], "planned", kind=kind, points=points, standard={"changes": list(used)})
    for seq, (cell, ok_, *rest) in enumerate(tries, 1):
        failure, move, turns = [*rest, None, None, 20][:3] if len(rest) < 3 else rest
        store.add_attempt(task["id"], cell=cell, max_turns=30, passed=int(ok_),
                          failure=None if ok_ else failure or "code_bug", turns=turns or 20)
        if move:
            store.set_move(task["id"], seq, move, "x")
    event = store.add_event(task["id"], "finished", status=task["status"], wall_s=wall)
    store._conn.execute("UPDATE events SET ts = ? WHERE id = ?", (stamp(at or NOW - timedelta(days=1)), event))
    return task["id"]


def many(store, n, *tries, **kw):
    return [finished(store, *tries, **kw) for _ in range(n)]


# --- what a cell's first tries say ---------------------------------------------------------------

def test_a_sample_is_a_fresh_start_on_a_cell_and_room_is_never_its_fault():
    a = lambda cell, passed, failure=None, move=None, turns=20: {  # noqa: E731
        "cell": cell, "passed": passed, "failure": failure, "move": move, "turns": turns}
    # first try passes
    assert standards.samples([a("sonnet/medium", 1)]) == [("sonnet/medium", True)]
    # more effort continues the same session: the next cell is no fresh start
    assert standards.samples([a("sonnet/medium", 0, "code_bug", "more_effort"), a("sonnet/high", 1)]) == [
        ("sonnet/medium", False)]
    # a new model starts from the base: a fresh start on its cell
    assert standards.samples([a("sonnet/xhigh", 0, "approach", "next_model"), a("opus/high", 1)]) == [
        ("sonnet/xhigh", False), ("opus/high", True)]
    # out of turns, then more turns on the same cell passed: the cell passed its first try
    assert standards.samples([a("sonnet/medium", 0, "turns_exhausted", "more_turns"), a("sonnet/medium", 1)]) == [
        ("sonnet/medium", True)]
    # a launch that broke and a retry that passed: still that cell's first try
    assert standards.samples([a("opus/high", 0, "environment", "retry"), a("opus/high", 1)]) == [
        ("opus/high", True)]
    # the environment or a refusal alone says nothing of the work
    assert standards.samples([a("opus/high", 0, "environment", "blocked")]) == []
    assert standards.samples([a("opus/high", 0, "permission", "blocked")]) == []
    # a resumed run goes on from kept work: no fresh start
    assert standards.samples([a("sonnet/medium", 0, "permission", "blocked"), a("sonnet/medium", 1)]) == []


def test_the_turns_a_pass_needed_count_the_room_it_ran_out_of():
    a = lambda cell, passed, failure=None, turns=20: {  # noqa: E731
        "cell": cell, "passed": passed, "failure": failure, "move": None, "turns": turns}
    assert standards.turns_needed([a("sonnet/medium", 1, turns=24)]) == 24
    assert standards.turns_needed([a("sonnet/medium", 0, "turns_exhausted", 30), a("sonnet/medium", 1, turns=18)]) \
        == 48
    assert standards.turns_needed([a("sonnet/medium", 0, "code_bug", 30), a("sonnet/high", 1, turns=18)]) == 18
    assert standards.turns_needed([a("sonnet/medium", 0, "code_bug", 30)]) is None
    assert standards.turns_needed([a("sonnet/medium", 1, turns=0)]) is None  # never recorded


# --- the start cell: the cheapest rung that passes first often enough ------------------------------

def cell(label: str) -> Cell:
    return Cell.parse(label)


def test_the_start_cell_rule_at_its_threshold():
    below = {"sonnet/medium": (6, 10)}
    assert standards.start_cell(IMPLEMENT, cell("sonnet/high"), below)[0] is None
    at = {"sonnet/medium": (7, 10)}
    moved, evidence = standards.start_cell(IMPLEMENT, cell("sonnet/high"), at)
    assert moved == cell("sonnet/medium")
    assert evidence["target"] == "sonnet/medium" and evidence["cells"]["sonnet/medium"] == [7, 10]
    assert evidence["rates"] == {"sonnet/medium": 0.7}
    assert evidence["threshold"] == standards.PASS_FIRST and evidence["min_samples"] == standards.MIN_SAMPLES


def test_the_start_cell_rule_needs_its_sample_minimum():
    few = {"sonnet/medium": (standards.MIN_SAMPLES - 1, standards.MIN_SAMPLES - 1)}
    assert standards.start_cell(IMPLEMENT, cell("sonnet/high"), few)[0] is None
    enough = {"sonnet/medium": (standards.MIN_SAMPLES, standards.MIN_SAMPLES)}
    assert standards.start_cell(IMPLEMENT, cell("sonnet/high"), enough)[0] == cell("sonnet/medium")


def test_the_start_cell_moves_one_rung_per_calibration():
    far = {"sonnet/medium": (1, 10), "sonnet/high": (2, 10), "sonnet/xhigh": (3, 10), "opus/high": (9, 10)}
    assert standards.start_cell(IMPLEMENT, None, far)[0] == cell("sonnet/high")
    assert standards.start_cell(IMPLEMENT, cell("sonnet/high"), far)[0] == cell("sonnet/xhigh")
    assert standards.start_cell(IMPLEMENT, cell("opus/high"), far)[0] is None  # already there


def test_the_start_cell_goes_down_to_the_first_rung_never_below():
    cheap = {"sonnet/medium": (10, 10)}
    assert standards.start_cell(IMPLEMENT, cell("opus/high"), cheap)[0] == cell("sonnet/xhigh")
    assert standards.start_cell(IMPLEMENT, cell("sonnet/high"), cheap)[0] == cell("sonnet/medium")
    assert standards.start_cell(IMPLEMENT, cell("sonnet/medium"), cheap)[0] is None
    # a cell below the ladder that passes is no start: the floor is the ladder's first rung
    assert standards.start_cell(IMPLEMENT, cell("sonnet/medium"), {"haiku/medium": (10, 10)})[0] is None
    # locks' own rules start at the first rung: a standard that would say the same is none
    assert standards.start_cell(IMPLEMENT, None, cheap)[0] is None


def test_the_start_cell_is_never_one_that_needs_an_approval():
    plan = LADDERS["plan"]  # opus/xhigh → opus/max → fable/high
    assert plan[-1].needs_approval
    only_fable = {"opus/xhigh": (0, 10), "opus/max": (1, 10), "fable/high": (10, 10)}
    assert standards.start_cell(plan, cell("opus/max"), only_fable)[0] is None
    assert standards.start_cell(plan, None, only_fable)[0] is None


# --- the start turns: the 75th percentile of what passes needed -----------------------------------

def test_the_start_turns_rule_at_its_boundaries():
    few = [40] * (standards.MIN_SAMPLES - 1)
    assert standards.start_turns(30, few)[0] is None
    needed = [20, 22, 24, 26, 40, 41, 42, 43]  # the 75th percentile, by nearest rank, is 41
    moved, evidence = standards.start_turns(30, needed)
    assert moved == 30 + standards.TURNS_STEP and evidence["p75"] == 41 and evidence["samples"] == 8
    assert standards.start_turns(38, needed)[0] == 41
    assert standards.start_turns(41, needed)[0] is None
    assert standards.start_turns(60, needed)[0] == 60 - standards.TURNS_STEP
    assert standards.start_turns(60, needed, floor=55)[0] == 55  # never under the kind's own turns
    assert standards.start_turns(80, needed, floor=80)[0] is None
    huge = [900] * 8
    assert standards.start_turns(MAX_TURNS_CEILING - 3, huge)[0] == MAX_TURNS_CEILING
    assert standards.start_turns(MAX_TURNS_CEILING, huge)[0] is None


# --- calibration: recorded, one change per band at a time, money never moves it ---------------------

def test_a_calibration_records_its_change_and_keeps_the_measure(store: Store):
    many(store, 6, ("sonnet/medium", 0, "code_bug", "next_model"), ("opus/high", 1), wall=1200, cost=3.0)
    many(store, 2, ("sonnet/medium", 1), wall=300, cost=0.5)
    changes = standards.calibrate(store, "r", now=NOW)
    assert len(changes) == 1
    change = store.calibrations(repo="r")[0]
    assert {k: change[k] for k in ("repo", "kind", "band", "field", "old_value", "new_value", "version")} == {
        "repo": "r", "kind": "implement", "band": 3, "field": "start_cell", "old_value": None,
        "new_value": "sonnet/high", "version": __version__}
    assert change["at"] == stamp(NOW) and change["reverts"] is None
    evidence = json.loads(change["evidence"])
    assert evidence["cells"] == {"sonnet/medium": [2, 8], "opus/high": [6, 6]} and evidence["target"] == "opus/high"
    assert "one rung" in change["rule"]
    row = store.standard("r", "implement", 3)
    assert row["start_cell"] == "sonnet/high" and row["start_turns"] is None
    measured = json.loads(row["measured"])
    assert measured["tasks"] == 8 and measured["cells"]["opus/high"] == [6, 6]
    assert measured["cost_per_point"] == round((6 * 3.0 + 2 * 0.5) / (8 * 3), 4)
    assert measured["time_per_point_s"] == round((6 * 1200 + 2 * 300) / (8 * 3), 1)


def test_money_never_moves_a_standard(store: Store, tmp_path):
    many(store, 6, ("sonnet/medium", 1), cost=0.01, repo="cheap")
    many(store, 6, ("sonnet/medium", 1), cost=90.0, repo="dear")
    many(store, 6, ("sonnet/high", 1), cost=0.01, repo="cheap", points=5)
    many(store, 6, ("sonnet/high", 1), cost=90.0, repo="dear", points=5)
    said = [[(c["field"], c["new_value"]) for c in standards.calibrate(store, repo, now=NOW)]
            for repo in ("cheap", "dear")]
    assert said[0] == said[1]


def test_a_band_with_a_change_not_judged_yet_does_not_move(store: Store):
    many(store, 6, ("sonnet/medium", 0, "code_bug", "next_model"), ("opus/high", 1))
    assert len(standards.calibrate(store, "r", now=NOW)) == 1
    many(store, 3, ("sonnet/high", 0, "code_bug", "next_model"), ("opus/high", 1), used=[1], at=NOW)
    assert standards.calibrate(store, "r", now=NOW + timedelta(hours=1)) == []
    assert store.standard("r", "implement", 3)["start_cell"] == "sonnet/high"


def test_turns_move_when_the_cell_stays(store: Store):
    many(store, 8, ("sonnet/medium", 1, None, None, turns_for("implement") + 22))
    change, = standards.calibrate(store, "r", now=NOW)
    # from locks' own rules (the kind's turns), one step toward what passes needed
    assert (change["field"], change["old_value"], change["new_value"]) == (
        "start_turns", None, str(turns_for("implement") + standards.TURNS_STEP))
    assert json.loads(store.calibrations(repo="r")[0]["evidence"])["p75"] == turns_for("implement") + 22


def test_turns_never_move_under_the_kind_s_own(store: Store):
    many(store, 8, ("sonnet/medium", 1, None, None, 12))
    assert standards.calibrate(store, "r", now=NOW) == []


def test_a_pinned_start_teaches_no_cell(store: Store):
    many(store, 8, ("opus/high", 1, None, None, turns_for("implement")), pinned=True)
    assert [c["field"] for c in standards.calibrate(store, "r", now=NOW)] == []


# --- the cadence: after every few endings in a repository, from hooks or the dispatcher -------------

def test_a_calibration_runs_after_every_few_endings_of_a_repository(store: Store):
    store.set_meta(standards.BACKFILL_KEY, "done")  # a store that settled before
    many(store, standards.CALIBRATE_EVERY - 1, ("sonnet/medium", 0, "code_bug", "next_model"), ("opus/high", 1))
    many(store, 2, ("sonnet/medium", 0, "code_bug", "next_model"), ("opus/high", 1), repo="other")
    finished(store, ("sonnet/medium", 1), options={"investigation": {"project": "x"}})  # not the person's work
    assert standards.settle(store, now=NOW)["calibrated"] == []
    # no calibration ran at all: none measured a band (four tries would change nothing anyway)
    assert store.standards() == []
    many(store, 2, ("sonnet/medium", 0, "code_bug", "next_model"), ("opus/high", 1))
    done = standards.settle(store, now=NOW)
    assert [c["repo"] for c in done["calibrated"]] == ["r"]
    assert [row["repo"] for row in store.standards()] == ["r"]
    # the count starts again: the next ending is not enough
    finished(store, ("sonnet/medium", 1))
    assert standards.settle(store, now=NOW)["calibrated"] == []


# --- judging: the window before and after, and a revert by the same rules ---------------------------

def _changed(store: Store) -> dict:
    many(store, 6, ("sonnet/medium", 0, "code_bug", "next_model"), ("sonnet/high", 1), at=NOW - timedelta(days=3))
    many(store, 4, ("sonnet/medium", 1), at=NOW - timedelta(days=3))
    standards.calibrate(store, "r", now=NOW - timedelta(days=2))
    return store.calibrations(repo="r")[0]


def test_a_change_is_judged_after_its_tasks_and_kept_when_pass_first_held(store: Store):
    change = _changed(store)
    assert change["new_value"] == "sonnet/high"
    many(store, standards.JUDGE_AFTER - 1, ("sonnet/high", 1), used=[change["id"]], at=NOW)
    assert standards.judge(store, "r", now=NOW) == []
    finished(store, ("sonnet/high", 1), used=[change["id"]], at=NOW)
    verdict, = standards.judge(store, "r", now=NOW)
    assert verdict["verdict"] == "kept"
    row = store.verdicts()[change["id"]]
    assert json.loads(row["window_before"])["pass_first"] == 0.4 and json.loads(row["window_after"])["pass_first"] == 1
    assert json.loads(row["window_after"])["tasks"] == standards.JUDGE_AFTER and row["version"] == __version__
    assert standards.judge(store, "r", now=NOW) == []  # judged once


def test_a_change_that_made_pass_first_worse_by_more_than_the_margin_is_reverted(store: Store):
    change = _changed(store)  # 4 of 10 passed first before
    many(store, standards.JUDGE_AFTER, ("sonnet/high", 0, "code_bug", "more_effort"), ("sonnet/xhigh", 1),
         used=[change["id"]], at=NOW)
    verdict, = standards.judge(store, "r", now=NOW)
    assert verdict["verdict"] == "reverted"
    revert = store.calibrations(repo="r")[-1]
    assert revert["reverts"] == change["id"] and revert["field"] == "start_cell"
    assert (revert["old_value"], revert["new_value"]) == ("sonnet/high", None)
    assert json.loads(revert["evidence"])["after"]["pass_first"] == 0.0
    assert store.standard("r", "implement", 3)["start_cell"] is None


def test_worse_by_exactly_the_margin_is_kept(store: Store):
    change = _changed(store)  # 4 of 10 passed first before
    passes = round((0.4 - standards.REVERT_MARGIN) * standards.JUDGE_AFTER)
    assert passes / standards.JUDGE_AFTER == pytest.approx(0.4 - standards.REVERT_MARGIN)  # the boundary itself
    for n in range(standards.JUDGE_AFTER):
        finished(store, ("sonnet/high", int(n < passes)), used=[change["id"]], at=NOW)
    assert standards.judge(store, "r", now=NOW)[0]["verdict"] == "kept"
    assert store.standard("r", "implement", 3)["start_cell"] == "sonnet/high"


# --- the plan: which standard it used and why; a person's flags win ---------------------------------

def test_the_plan_starts_where_the_standard_says_and_says_why(git_repo, store: Store):
    from locks import repo

    key = repo.key(git_repo)
    many(store, 6, ("sonnet/medium", 0, "code_bug", "next_model"), ("opus/high", 1), repo=key, points=1)
    many(store, 8, ("sonnet/medium", 1, None, None, 52), repo=key, points=1, at=NOW - timedelta(days=2))
    standards.calibrate(store, key, now=NOW - timedelta(days=1))
    change = store.calibrations(repo=key)[0]
    dry = run("tweak it", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"),
              adapters=[])
    assert dry.plan.start == cell("sonnet/high")
    line = next(r for r in dry.plan.reasons if r.startswith("standard for implement at 1 point here"))
    assert f"change #{change['id']}" in line
    assert "8 of 14 passed first at sonnet/medium and 6 of 6 at opus/high" in line
    assert dry.plan.standard == {"band": 1, "cell": "sonnet/high", "turns": None, "changes": [change["id"]]}
    pinned = run("tweak it", git_repo, store, Options(dry_run=True, start=cell("opus/high"), max_turns=12),
                 registry={}, classifier=kind("implement"), adapters=[])
    assert pinned.plan.start == cell("opus/high") and pinned.plan.standard["changes"] == []
    other = run("tweak it", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("feature"),
                adapters=[])
    assert any(r.startswith("no standard for feature at 2 points here yet") for r in other.plan.reasons)
    report = run("tweak it", git_repo, store, registry={}, launcher=Script(ok()), classifier=kind("implement"),
                 adapters=[])
    planned = store.last_event(report.task_id, "planned")["data"]
    assert planned["standard"]["changes"] == [change["id"]] and report.cells == ["sonnet/high"]


def test_an_explicit_max_turns_wins_over_the_standard(git_repo, store: Store):
    from locks import repo

    key = repo.key(git_repo)
    store.set_standard(key, "implement", 1, start_turns=100)
    store.add_calibration(at=stamp(NOW), repo=key, kind="implement", band=1, field="start_turns", old_value="80",
                          new_value="100", rule="r", evidence="{}", version=__version__)
    plain = run("tweak it", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"),
                adapters=[])
    assert plain.plan.turns == 100 and plain.plan.standard["turns"] == 100
    asked = run("tweak it", git_repo, store, Options(dry_run=True, max_turns=12), registry={},
                classifier=kind("implement"), adapters=[])
    assert asked.plan.turns is None and asked.plan.standard["turns"] is None


def test_what_says_nothing_of_the_work_is_no_first_try(store: Store):
    finished(store, ("sonnet/medium", 0, "environment", "blocked"), status="blocked")
    finished(store, ("gpt/huge", 1), ("sonnet/medium", 1))  # a cell locks has no such model for is no sample
    nothing = store.create_task("never ran", status="failed", source="locks", repo="r", kind="implement", points=3)
    store.add_event(nothing["id"], "finished", status="failed")
    tasks = standards.ended(store, repo="r")
    assert [t.first for t in tasks] == [None, True, None]
    assert standards.metrics(tasks)["pass_first"] == 1.0 and standards.rates(tasks) == {}


def test_a_current_cell_off_the_ladder_counts_at_the_rung_below_it():
    cheap = {"sonnet/medium": (10, 10)}
    assert standards.start_cell(IMPLEMENT, cell("opus/max"), cheap)[0] == cell("sonnet/xhigh")
    assert standards.start_cell(IMPLEMENT, cell("haiku/low"), cheap)[0] is None


def test_a_task_without_points_has_no_band(store: Store):
    many(store, 8, ("sonnet/medium", 1, None, None, 52), points=None)
    assert standards.calibrate(store, "r", now=NOW) == [] and store.standards() == []


def test_the_first_settle_sizes_past_work_and_calibrates_from_it(store: Store):
    """Standards waited for five endings with points, and tasks that ended before locks
    kept points never counted: a store full of history calibrated nothing."""
    old = many(store, standards.MIN_SAMPLES + 1, ("sonnet/medium", 0, "code_bug", "next_model"), ("opus/high", 1),
               points=None)
    no_kind = finished(store, ("sonnet/medium", 1), points=None, kind=None)
    planned = store.events_of(old[0], "planned")[0]
    store._conn.execute("UPDATE events SET data = ? WHERE id = ?",
                        (json.dumps({**planned["data"], "risk": {"tier": "high", "reasons": ["x"]}}), planned["id"]))
    found = standards.settle(store, now=NOW)
    assert found["ended"] == len(old) + 1 and len(found["calibrated"]) == 1
    assert store.get_task(old[0])["points"] != store.get_task(old[1])["points"]  # the risk its plan recorded
    sized = store.get_task(old[1])
    assert sized["points"] and sized["points_why"].startswith(standards.AFTER_THE_FACT)
    assert store.get_task(no_kind)["points"] is None
    done = json.loads(store.meta(standards.BACKFILL_KEY))
    assert done["sized"] == len(old) and done["version"] == __version__
    # Once: the next settle neither sizes again nor calibrates without new endings.
    assert standards.settle(store, now=NOW)["calibrated"] == []
    # A task that ends later with no size is sized when its ending is settled.
    late = finished(store, ("sonnet/medium", 1), points=None, at=NOW)
    standards.settle(store, now=NOW)
    assert store.get_task(late)["points"]
    assert standards.backfill(store, ids=[999999]) == 0


def test_two_settles_at_once_calibrate_once(store: Store, monkeypatch):
    store.set_meta(standards.BACKFILL_KEY, "done")
    many(store, standards.CALIBRATE_EVERY, ("sonnet/medium", 0, "code_bug", "next_model"), ("opus/high", 1))
    other = Store(store.path)
    real = store.endings_after
    calls = []

    def racing(after):
        found = real(after)
        if not calls:
            calls.append(standards.settle(other, now=NOW))  # the dispatcher settles first
        return found

    monkeypatch.setattr(store, "endings_after", racing)
    try:
        assert standards.settle(store, now=NOW) == {"ended": 0, "calibrated": [], "judged": []}
    finally:
        other.close()
    assert len(calls[0]["calibrated"]) == 1 and len(store.calibrations()) == 1


def test_the_plan_names_a_revert_and_a_standard_off_the_ladder(git_repo, store: Store):
    from locks import repo

    key = repo.key(git_repo)
    store.set_standard(key, "implement", 1, start_cell="sonnet/xhigh")
    first = store.add_calibration(at=stamp(NOW - timedelta(days=3)), repo=key, kind="implement", band=1,
                                  field="start_cell", old_value="sonnet/high", new_value="sonnet/xhigh", rule="r",
                                  evidence="{}", version=__version__)
    store.add_calibration(at=stamp(NOW - timedelta(days=1)), repo=key, kind="implement", band=1, field="start_cell",
                          old_value="sonnet/xhigh", new_value="sonnet/high", reverts=first,
                          rule=f"revert #{first}: it made pass-first worse", evidence="{}", version=__version__)
    store.set_standard(key, "implement", 1, start_cell="sonnet/high")
    used = standards.for_plan(store, key, "implement", 1, pinned=False, explicit_turns=False)
    assert used.cell == cell("sonnet/high")
    assert f"(change #{first + 1}: revert #{first}: it made pass-first worse)" in used.reasons[0]
    store.set_standard(key, "implement", 1, start_cell="haiku/low")
    off = standards.for_plan(store, key, "implement", 1, pinned=False, explicit_turns=False)
    assert off.cell is None and "the implement ladder no longer holds" in off.reasons[0]
    store.set_standard(key, "docs", 2, measured={"tasks": 4})
    said = standards.for_plan(store, key, "docs", 2, pinned=False, explicit_turns=False).reasons
    assert said == ("no standard for docs at 2 points here yet: locks' rules choose (measured over 4 tasks so far)",)
    # A standard's turns set before the kind's rose never take room away from it.
    store.set_standard(key, "implement", 13, start_turns=40)
    low = standards.for_plan(store, key, "implement", 13, pinned=False, explicit_turns=False)
    assert low.turns is None and "under the kind's 80: the kind's win" in low.reasons[0]
    store.set_standard(key, "implement", 8, start_turns=100)
    assert standards.for_plan(store, key, "implement", 8, pinned=False, explicit_turns=False).turns == 100


def test_a_repository_that_joins_a_workspace_takes_its_standards_and_open_changes_with_it(store: Store):
    """`Store.rekey` moved a repository's tasks to the workspace but left its standards, its
    count toward the next calibration and a change waiting for its verdict under the old key:
    the folder started on the default rules, and the change was never judged."""
    change = _changed(store)
    store.set_meta("standards:since:r", "3")
    store.rekey("r", "ws")
    assert store.standard("ws", "implement", 3)["start_cell"] == "sonnet/high"
    assert store.standard("r", "implement", 3) is None
    assert store.meta("standards:since:ws") == "3" and store.meta("standards:since:r") is None
    carried, = store.calibrations(repo="ws")
    assert json.loads(carried["evidence"])["carried_from"] == change["id"]
    assert carried["new_value"] == "sonnet/high" and carried["at"] == change["at"]
    assert store.verdicts()[change["id"]]["verdict"] == "carried"
    # The tasks that used it under its old id judge it in the workspace.
    many(store, standards.JUDGE_AFTER, ("sonnet/high", 1), used=[change["id"]], repo="ws", at=NOW)
    verdict, = standards.judge(store, "ws", now=NOW)
    assert verdict["change"] == carried["id"] and verdict["verdict"] == "kept"
    assert standards.judge(store, "r", now=NOW) == []


def test_a_size_with_too_few_tasks_starts_where_the_kind_s_tasks_of_every_size_pass(store):
    for points in (1, 2, 3, 5, 8):
        finished(store, ("sonnet/xhigh", True), kind="ui", points=points)
    changes = standards.calibrate(store, "r", now=NOW)
    pooled = [c for c in changes if c["band"] == standards.ANY_SIZE]
    assert [(c["field"], c["new_value"]) for c in pooled] == [("start_cell", "sonnet/xhigh")]
    assert not [c for c in changes if c["band"] != standards.ANY_SIZE and c["field"] == "start_cell"]
    used = standards.for_plan(store, "r", "ui", 13, pinned=False, explicit_turns=False)
    assert used.cell.label == "sonnet/xhigh" and used.changes == (pooled[0]["id"],)
    assert any("ui here (every size: 13 points has too few tasks)" in r for r in used.reasons)
    assert standards.for_plan(store, "r", "ui", 13, pinned=True, explicit_turns=False).cell is None
    assert standards.size(standards.ANY_SIZE) == "any size"


def test_an_estimate_is_raised_a_step_where_the_kind_turned_out_bigger(store):
    from locks import points

    for _ in range(5):
        task = finished(store, ("opus/high", True), kind="feature", points=3)
        store.add_event(task, "finished", status="done", wall_s=60, realized=8)
    ratio, n = standards.size_ratio(store, "r", "feature")
    assert n == 5 and ratio > 2
    raised = points.corrected(points.Score(3, ("kind feature",)), "feature", ratio, n)
    assert raised.points == 5 and raised.reasons[-1].startswith("feature tasks here turned out")
    assert points.corrected(points.Score(3), "feature", ratio, 4).points == 3
    assert points.corrected(points.Score(3), "feature", 1.2, 9).points == 3
    assert points.corrected(points.Score(13), "feature", 3.0, 9).points == 13


def test_a_task_whose_time_was_never_measured_is_left_out_of_the_time_per_point(store):
    # Two 3-point passes, one of 600 s and one whose attempts kept no wall time: the unmeasured one
    # used to count as 0 s and halve the time per point (and double the velocity).
    finished(store, ("sonnet/medium", True), wall=600.0)
    finished(store, ("sonnet/medium", True), wall=0)
    measured = standards.metrics(standards.ended(store, repo="r"))
    assert measured["time_per_point_s"] == 200.0
    assert measured["cost_per_point"] == round(2.0 / 6, 4)


def test_time_per_point_is_none_when_no_task_was_timed(store):
    finished(store, ("sonnet/medium", True), wall=0)
    assert standards.metrics(standards.ended(store, repo="r"))["time_per_point_s"] is None


# --- skipping a rung that rarely helps ------------------------------------------------------------

#: A climb into sonnet/xhigh that did not pass there, and opus/high after it that did.
WASTED = (("sonnet/high", False, "code_bug", "more_effort"), ("sonnet/xhigh", False, "code_bug", "next_model"),
          ("opus/high", True))


def skips_of(changes):
    return [c for c in changes if c["field"] == standards.SKIP]


def test_a_rung_its_climbs_rarely_pass_is_skipped_here(store):
    many(store, standards.SKIP_MIN_MOVES, *WASTED)
    [change] = skips_of(standards.calibrate(store, "r", now=NOW))
    assert change["new_value"] == "sonnet/high>sonnet/xhigh" and change["band"] == standards.ANY_SIZE
    assert change["version"] == __version__ and change["old_value"] is None
    ev = change["evidence"]
    assert ev["pooled"] is False and ev["passed"] == 0 and ev["climbs"] == 4 and ev["onto"] == "opus/high"
    [(pair, skip)] = standards.active_skips(store, "r", "implement").items()
    assert pair == ("sonnet/high", "sonnet/xhigh") and skip["onto"] == Cell("opus", "high")
    assert skip["change"] == change["id"] and "0 of 4 more_effort climbs" in skip["because"]
    # The climbs into opus/high passed: that rung stays. And another kind is untouched.
    assert standards.active_skips(store, "r", "feature") == {}
    # Not again while it waits for its verdict.
    assert skips_of(standards.calibrate(store, "r", now=NOW)) == []


def test_too_few_climbs_here_read_every_repository_and_say_so(store):
    finished(store, *WASTED, repo="r")
    many(store, standards.SKIP_MIN_MOVES, *WASTED, repo="o")
    [change] = skips_of(standards.calibrate(store, "r", now=NOW))
    assert change["evidence"]["pooled"] is True and change["evidence"]["repos"] == 2
    assert "in every repository" in change["rule"] and "in 2 repositories" in standards.because(change)


def test_a_climb_that_passes_often_enough_or_onto_the_last_rung_is_never_skipped(store):
    many(store, 3, *WASTED)
    many(store, 2, ("sonnet/high", False, "code_bug", "more_effort"), ("sonnet/xhigh", True))
    # 2 of 5 passed: over the threshold. And sonnet/xhigh → opus/high failing: opus/high is the last rung.
    many(store, 4, ("sonnet/xhigh", False, "approach", "next_model"), ("opus/high", False, "code_bug"),
         status="failed")
    assert skips_of(standards.calibrate(store, "r", now=NOW)) == []
    assert standards.skip_onto(IMPLEMENT, "sonnet/xhigh", "opus/high") is None
    assert standards.skip_onto(LADDERS["plan"], "opus/xhigh", "opus/max") is None  # onto fable: never


def test_a_retry_or_more_turns_is_no_climb(store):
    task = finished(store, ("sonnet/high", False, "environment", "retry"), ("sonnet/high", False,
                    "turns_exhausted", "more_turns"), ("sonnet/high", True))
    [t] = [t for t in standards.ended(store, repo="r") if t.id == task]
    assert standards.climbs(t) == []


def test_a_skip_is_judged_once_by_its_climbs_and_kept(store):
    many(store, standards.SKIP_MIN_MOVES, *WASTED, wall=1800.0)
    [change] = skips_of(standards.calibrate(store, "r", now=NOW))
    later = NOW + timedelta(hours=1)
    many(store, standards.JUDGE_AFTER, ("sonnet/high", False, "code_bug", "next_model"), ("opus/high", True),
         at=later, wall=1200.0)
    [verdict] = standards.judge(store, "r", now=later + timedelta(minutes=1))
    assert verdict["change"] == change["id"] and verdict["verdict"] == "kept"
    assert verdict["after"]["pass_first"] == 1.0 and verdict["before"]["pass_first"] == 0.0
    assert standards.judge(store, "r", now=later + timedelta(minutes=2)) == []
    assert standards.active_skips(store, "r", "implement")


def test_a_skip_that_makes_passing_slower_is_reverted_as_its_own_change(store):
    many(store, standards.SKIP_MIN_MOVES, *WASTED, wall=600.0)
    [change] = skips_of(standards.calibrate(store, "r", now=NOW))
    later = NOW + timedelta(hours=1)
    many(store, standards.JUDGE_AFTER, ("sonnet/high", False, "code_bug", "next_model"), ("opus/high", True),
         at=later, wall=600.0 * standards.SKIP_SLOWER * 2)
    [verdict] = standards.judge(store, "r", now=later + timedelta(minutes=1))
    assert verdict["verdict"] == "reverted"
    revert = store.calibrations(repo="r")[-1]
    assert revert["reverts"] == change["id"] and revert["field"] == standards.SKIP
    assert revert["old_value"] == change["new_value"] and revert["new_value"] is None
    assert standards.active_skips(store, "r", "implement") == {}
    from locks import evolution
    assert "again since" in evolution.change_line(revert, None, [])
    assert "skip sonnet/xhigh for opus/high" in evolution.change_line(
        store.calibrations(repo="r")[-2], store.verdicts()[change["id"]], [])


def test_a_skip_holds_no_start_cell_back(store):
    many(store, standards.SKIP_MIN_MOVES, *WASTED)
    standards.calibrate(store, "r", now=NOW)
    # The skip waits for its verdict; the band's start cell still moves by its own rules.
    many(store, standards.MIN_SAMPLES, ("opus/high", True))
    changed = standards.calibrate(store, "r", now=NOW + timedelta(minutes=1))
    assert any(c["field"] == "start_cell" for c in changed)


# --- a pass nobody read moves no start cell cheaper ------------------------------------------------

def test_a_first_try_the_sampling_let_through_is_no_evidence_for_a_cheaper_start(store: Store):
    from locks.matrix import Cell

    ladder = [Cell("sonnet", "medium"), Cell("sonnet", "high")]
    read = {"sonnet/medium": [5, 5]}
    # Every first try at the cheaper rung passed, but none was read: the start stays.
    moved, evidence = standards.start_cell(ladder, Cell("sonnet", "high"), read, read=({"sonnet/medium": [0, 0]}))
    assert moved is None and "held" in evidence and evidence["read"] == {"sonnet/medium": [0, 0]}
    # Read ones move it; a move up never asks for them.
    assert standards.start_cell(ladder, Cell("sonnet", "high"), read, read=read)[0] == Cell("sonnet", "medium")
    up = {"sonnet/high": [5, 5], "sonnet/medium": [1, 5]}
    assert standards.start_cell(ladder, None, up, Cell("sonnet", "medium"), read={})[0] == Cell("sonnet", "high")


def test_the_calibration_leaves_unread_passes_out_of_a_move_down(store: Store):
    store.set_standard("r", "implement", 3, start_cell="sonnet/high")
    for _ in range(standards.MIN_SAMPLES):
        task = finished(store, ("sonnet/medium", True), points=3)
        store.add_event(task, "verifier_sampled", seq=1, worker="sonnet/medium", every=2)
    ended = standards.ended(store, repo="r")
    assert all(t.unread for t in ended)
    assert standards.rates(ended) == {"sonnet/medium": [5, 5]} and standards.rates(ended, read=True) == {}
    assert not [c for c in standards.calibrate(store, "r") if c["field"] == "start_cell"]
    assert store.standard("r", "implement", 3)["start_cell"] == "sonnet/high"
    # The same passes read by the independent check move it down one rung.
    for _ in range(standards.MIN_SAMPLES):
        finished(store, ("sonnet/medium", True), points=3)
    changes = [c for c in standards.calibrate(store, "r") if c["field"] == "start_cell"]
    assert changes and changes[0]["new_value"] == "sonnet/medium"
