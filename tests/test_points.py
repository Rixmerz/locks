from __future__ import annotations

import pytest

from locks import points, risk
from locks.points import Score, estimate, realized


def size(**kw) -> Score:
    given = {"criteria": 0, "kind": "implement", "tier": "medium", "areas": (), "named": 0, "reached": None,
             "chars": 40}
    return estimate(**{**given, **kw})


def test_the_example_reads_as_the_person_wrote_it():
    found = size(criteria=4, tier="high", areas=[("data", "schema")])
    assert found == Score(5, ("4 criteria", "touches schema", "risk high"))
    assert found.line() == "5: 4 criteria, touches schema, risk high"
    assert found.data() == {"points": 5, "reasons": ["4 criteria", "touches schema", "risk high"]}


def test_a_request_with_nothing_to_say_is_one_point():
    found = size()
    assert found.points == 1 and found.reasons == ()
    assert found.line() == "1: nothing says it is bigger"


@pytest.mark.parametrize(("weight", "expected"), [(0, 1), (1, 2), (2, 3), (3, 5), (4, 5), (5, 8), (6, 8), (7, 13),
                                                   (40, 13)])
def test_the_weight_maps_onto_the_fibonacci_scale_at_its_boundaries(weight, expected):
    assert points.on_scale(weight) == expected


@pytest.mark.parametrize(("count", "weight"), [(0, 0), (1, 0), (2, 1), (3, 1), (4, 2), (6, 2), (7, 3), (20, 3)])
def test_criteria_count_at_their_thresholds(count, weight):
    found = size(criteria=count)
    assert found.points == points.on_scale(weight)
    assert (f"{count} criteria" in found.reasons) is (weight > 0)


def test_the_kind_weighs_only_where_it_says_more_work():
    assert size(kind="implement").points == 1 and size(kind="chat").points == 1
    assert size(kind="feature") == Score(2, ("kind feature",))
    assert size(kind="frontier").points == 3


def test_areas_count_up_to_two_and_risk_high_once():
    assert size(tier="high").reasons == ("risk high",)
    assert size(tier="passive").points == 1 and size(tier="medium").points == 1
    two = size(tier="high", areas=[("data", "schema"), ("security", "auth")])
    assert two.reasons == ("touches schema and auth", "risk high") and two.points == 5
    three = size(tier="high", areas=[("data", "schema"), ("security", "auth"), ("money", "billing")])
    assert three.points == two.points
    assert three.reasons[0] == "touches schema, auth and billing"


@pytest.mark.parametrize(("named", "weight"), [(0, 0), (2, 0), (3, 1), (5, 1), (6, 2), (30, 2)])
def test_named_files_at_their_thresholds(named, weight):
    found = size(named=named)
    assert found.points == points.on_scale(weight)
    assert (f"names {named} files" in found.reasons) is (weight > 0)


@pytest.mark.parametrize(("reached", "weight"), [(None, 0), (0, 0), (4, 0), (5, 1), (19, 1), (20, 2), (300, 2)])
def test_the_symbols_livespec_reached_at_their_thresholds(reached, weight):
    found = size(reached=reached)
    assert found.points == points.on_scale(weight)
    assert (f"reaches {reached} symbols" in found.reasons) is (weight > 0)


def test_the_length_is_the_weakest_signal():
    assert size(chars=points.LONG_CHARS - 1).points == 1
    assert size(chars=points.LONG_CHARS) == Score(2, (f"long request ({points.LONG_CHARS} chars)",))
    assert size(chars=50 * points.LONG_CHARS).points == 2


def test_every_signal_at_once_tops_the_scale():
    found = size(criteria=9, kind="frontier", tier="high", areas=[("data", "x"), ("money", "y")], named=9,
                 reached=40, chars=9000)
    assert found.points == 13 and len(found.reasons) == 7


def test_the_paths_a_request_names_and_its_surfaces():
    text = ("Change src/locks/points.py and tests/test_points.py, see CLAUDE.md and `README.md`; "
            "not https://example.com/a/b.html nor and/or, nor escalate.MAX_TURNS_CEILING or 0.20.0. "
            "Also docs/images/ and src/locks/ui/static/*.js")
    found = points.named(text, ["src/locks/points.py", "hooks/**"])
    assert found == ["CLAUDE.md", "README.md", "docs/images/", "hooks/**", "src/locks/points.py",
                     "src/locks/ui/static/*.js", "tests/test_points.py"]
    assert points.named("make the button blue", ()) == []


def test_of_request_reads_the_plan():
    text = "Add the schema:\n- a table\n- a column\n- an index\n- a backfill in db/schema.sql"
    found = points.of_request(text, "implement", risk.of_request(text, "implement"), surfaces=(), reached=3)
    assert found.reasons[:3] == ("4 criteria", "touches schema", "risk high")
    assert found.points == 5


def test_risk_names_the_areas_its_reasons_hold():
    found = risk.of_request("drop the users table schema and rotate the auth token", "implement")
    assert risk.areas(found) == [("data", "schema"), ("security", "auth")]
    assert risk.areas(risk.of_request("review it", "review-critical")) == []
    assert risk.areas(risk.Risk("high", ["the request is about money (\"payment's\")"])) == [("money", "payment's")]


@pytest.mark.parametrize(("kw", "reason"), [
    ({"files": 2}, None), ({"files": 3}, "3 files"), ({"files": 8}, "8 files"), ({"files": 20}, "20 files"),
    ({"lines": 49}, None), ({"lines": 50}, "50 lines"), ({"lines": 1500}, "1500 lines"),
    ({"turns": 29}, None), ({"turns": 30}, "30 turns"), ({"turns": 160}, "160 turns"),
    ({"minutes": 9.9}, None), ({"minutes": 10}, "10 minutes"), ({"minutes": 90.4}, "90 minutes"),
    ({"attempts": 1}, None), ({"attempts": 2}, "2 attempts"), ({"attempts": 4}, "4 attempts"),
])
def test_the_realized_size_at_its_thresholds(kw, reason):
    given = {"files": 0, "lines": 0, "turns": 0, "minutes": 0.0, "attempts": 1}
    found = realized(**{**given, **kw})
    assert found.reasons == ((reason,) if reason else ())


def test_the_realized_size_sums_what_was_measured():
    small = realized(files=1, lines=12, turns=14, minutes=3.0, attempts=1)
    assert small.points == 1
    feature = realized(files=8, lines=400, turns=60, minutes=25.0, attempts=1)
    assert feature == Score(8, ("8 files", "400 lines", "60 turns", "25 minutes"))
    large = realized(files=20, lines=1500, turns=180, minutes=120.0, attempts=3)
    assert large.points == 13
    # what git could not say is left out, never guessed
    assert realized(files=3, lines=None, turns=0, minutes=None, attempts=1) == Score(2, ("3 files",))


def test_a_request_that_asks_for_it_everywhere_is_larger():
    from locks import points
    from locks import risk as risk_mod

    one = points.of_request("remove the legacy banner from the dashboard", "feature", risk_mod.Risk("medium"))
    every = points.of_request("remove the legacy banner everywhere", "feature", risk_mod.Risk("medium"))
    assert every.points > one.points
    assert any("everywhere" in r for r in every.reasons)
    assert points.sweeping("quita el banner en todas las pantallas")
    assert points.sweeping("fix the typo in the header") is None


def test_a_large_task_starts_on_more_turns_after_one_that_outgrew_them():
    from locks.escalate import Failure
    from locks.matrix import turns_for
    from locks.orchestrate import turns_from_history

    own = turns_for("feature")
    runs = [[(own, False, Failure.TURNS_EXHAUSTED.value), (own + 30, True, None)]]
    assert turns_from_history(runs, "feature") is None
    turns, why = turns_from_history(runs, "feature", large=True)
    assert turns == own + 30 and "needed more" in why
