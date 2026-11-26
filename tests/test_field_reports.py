"""Fixes from the field reports #207 and #208, each pinned to what the report measured."""
from __future__ import annotations

from types import SimpleNamespace

from locks import capabilities, capacity, launch, suspend, trust


def test_a_backend_route_is_no_rendered_ui_and_a_page_component_still_is():
    assert not capabilities.renders("src/routes/orders.routes.ts")
    assert not capabilities.renders("server/components/mailer.js")
    assert capabilities.renders("src/routes/+page.svelte") and capabilities.renders("app/pages/index.tsx")
    assert capabilities.renders("components/Card.vue") and capabilities.renders("styles/site.css")
    assert not capabilities.renders("components/Card.test.tsx")


def test_a_server_whose_command_is_a_missing_path_is_not_handed(tmp_path):
    there = tmp_path / "graph"
    there.write_text("")
    program = tmp_path / "bin" / "some-program-on-path"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    registry = {
        "gone": capabilities.Capability("gone", {"command": str(tmp_path / "nowhere")}),
        "here": capabilities.Capability("here", {"command": str(there)}),
        "path": capabilities.Capability(
            "path", {"command": "some-program-on-path", "env": {"PATH": str(program.parent)}}),
        "blank": capabilities.Capability("blank", {"command": ""}),
    }
    assert set(capabilities.select(registry, "implement").servers) == {"here", "path"}


def test_a_blocker_in_another_language_naming_the_same_code_is_a_repeat():
    es = {"where": "src/a.py", "what": "falta el comentario `@spec:billing-total` en openspec/specs/billing"}
    en = {"where": "src/b.py", "what": "missing `@spec:billing-total` link for openspec/specs/billing"}
    assert trust._repeats(es, [en])
    near = {"where": "src/a.py", "what": "the guard in `apply_discount` lets a negative through"}
    again = {"where": "src/a.py:40", "what": "la condición de `apply_discount` deja pasar negativos"}
    assert trust._repeats(near, [again])
    assert not trust._repeats(es, [{"where": "x.py", "what": "a different problem altogether"}])


def test_busiest_reads_ps_where_there_is_no_proc(tmp_path):
    out = "R    /usr/bin/node\nS    /bin/zsh\nU    /usr/bin/node\nR+   jest\n"
    found = capacity._busiest_ps(lambda *a, **k: SimpleNamespace(returncode=0, stdout=out))
    assert found == [{"program": "node", "threads": 2}, {"program": "jest", "threads": 1}]
    assert capacity._busiest_ps(lambda *a, **k: SimpleNamespace(returncode=1, stdout="")) is None

    def broken(*a, **k):
        raise OSError("no ps")

    assert capacity._busiest_ps(broken) is None


def test_a_sleep_is_read_from_how_the_clocks_difference_grows_even_when_it_starts_below_zero():
    assert suspend.slept(-5.6, 3594.4) == 3600.0  # macOS: the difference starts negative (#208)
    assert suspend.slept(-5.6, -5.9) == 0.0  # drift, not a sleep
    assert suspend.slept(100.0, 10.0) is None  # another boot


def test_an_api_that_cannot_be_reached_is_the_network():
    assert launch.NETWORK_DOWN.search("Can't reach the API server, retrying")


def test_a_first_try_the_machine_turned_away_is_no_sample_of_a_trial(store):
    for n, (failure, passed) in enumerate([("environment", 0), ("permission", 0), ("code_bug", 0), (None, 1)]):
        t = store.create_task(f"t{n}", status="done", source="locks", repo="r")
        store.add_event(t["id"], "briefed", primer_arm="with")
        store.add_attempt(t["id"], cell="sonnet/medium", max_turns=10, passed=passed, input_tokens=100,
                          failure=failure)
    rows = store.arm_trial("r", "primer_arm")
    assert sorted(r["passed_first"] for r in rows) == [False, True]
