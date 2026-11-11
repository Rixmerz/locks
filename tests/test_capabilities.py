from __future__ import annotations

import json
from pathlib import Path

import pytest

from locks import capabilities as caps


def write(tmp_path: Path, data) -> Path:
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps(data))
    return path


def test_selection_by_kind_and_after_failure(tmp_path):
    registry = caps.load(write(tmp_path, caps.EXAMPLE))
    ui = caps.select(registry, "ui", workdir=Path("/w"))
    assert ui.names == ("livespec", "layout-inspector")
    first = caps.select(registry, "implement", workdir=Path("/w"))
    assert first.names == ("livespec",)
    retry = caps.select(registry, "implement", failed_before=True, workdir=Path("/w"))
    assert retry.names == ("livespec", "layout-inspector")
    assert 'workspace="/w"' in first.system_prompt()
    assert caps.select(registry, "test", workdir=Path("/w")).names == ("livespec",)
    asked = caps.select(registry, "test", workdir=Path("/w"), asked=True)
    assert asked.names == ("livespec", "browser") and "asks to browse" in asked.system_prompt()
    assert caps.Selection().system_prompt() == ""


def test_workdir_reaches_args_and_env(tmp_path):
    registry = caps.load(write(tmp_path, {"idx": {"server": {"command": "x", "args": ["--root", "{workdir}"],
                                                             "env": {"ROOT": "{workdir}"}, "port": 3}}}))
    server = caps.select(registry, "docs", workdir=Path("/repo")).servers["idx"]
    assert server == {"command": "x", "args": ["--root", "/repo"], "env": {"ROOT": "/repo"}, "port": 3}


def test_absent_registry_is_empty_and_bad_ones_are_named(tmp_path):
    assert caps.load(tmp_path / "missing.json") == {}
    for bad in ([], {"x": {}}, {"x": {"server": {}, "when": "sometimes"}}):
        with pytest.raises(caps.RegistryError):
            caps.load(write(tmp_path, bad))
    (tmp_path / "capabilities.json").write_text("{nope")
    with pytest.raises(caps.RegistryError):
        caps.load(tmp_path / "capabilities.json")


def test_a_capability_follows_what_the_work_touches(tmp_path):
    registry = caps.load(write(tmp_path, {
        "measure": {"server": {"command": "m"}, "when": [], "touches": "ui", "measures": True, "hint": "measure it"},
        "drive": {"server": {"command": "d"}, "when": [], "touches": ["e2e/**"], "hint": "drive it"},
    }))
    assert registry["measure"].touches == caps.UI_FILES and registry["measure"].measures
    # Nothing touched: kind alone decides, and neither belongs to a feature.
    assert caps.select(registry, "feature").names == ()
    # A build that touches rendered UI gets what measures it, from the first attempt, and says why.
    built = caps.select(registry, "feature", paths=["lib/x.ts", "components/Chart.tsx"])
    assert built.names == ("measure",) and "given because this work touches components/Chart.tsx" in built.hints[0]
    # Reading work (a review, a re-check) gets only what measures; writing work gets both.
    assert caps.select(registry, "review-routine", paths=["styles/app.css", "e2e/home.spec.ts"],
                       reading=True).names == ("measure",)
    assert caps.select(registry, "implement", paths=["styles/app.css", "e2e/home.spec.ts"]).names == (
        "measure", "drive")
    assert caps.measuring(registry) == {"measure": registry["measure"]}
    with pytest.raises(caps.RegistryError, match="touches"):
        caps.load(write(tmp_path, {"x": {"server": {}, "touches": "everything"}}))


@pytest.mark.parametrize(("path", "ui"), [
    ("components/charts/Bar.tsx", True), ("src/islands/Kpi.tsx", True), ("assets/styles.css", True),
    ("web/routes/app/index.tsx", True), ("x/pages/a.ts", True), ("lib/server/db.ts", False),
    ("README.md", False), ("tests/db_test.py", False),
])
def test_rendered_ui_is_read_from_folders_and_file_types(path, ui):
    assert caps.matches(path, caps.UI_FILES) is ui


def test_an_entry_named_after_a_neighbour_takes_what_it_does_not_declare(tmp_path):
    # Written before `touches` existed: kinds only, and an unpinned browser beside it.
    old = write(tmp_path, {
        "layout-inspector": {"server": {"command": "layout-inspector"}, "when": ["implement", "feature"]},
        "browser": {"server": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}, "when": ["test"]},
    })
    off = {"LOCKS_PLUGINS": "off"}
    registry = caps.load_all(old, env=off)
    inspector = registry["layout-inspector"]
    assert inspector.measures and inspector.touches and inspector.when == ("implement", "feature")
    # So a review of a page now gets it, by the files it reads.
    assert caps.select(registry, "review-routine", paths=["islands/Card.tsx"], reading=True).names == (
        "layout-inspector",)
    said = caps.notices(old)
    assert any("shadows the layout-inspector neighbour" in line
               and "takes what it touches and that it only measures" in line
               and "(implement, feature) still gives it" in line for line in said)
    assert any("'browser' runs `@playwright/mcp@latest` with no fixed version" in line for line in said)
    # What the entry declares wins, and it is said without "takes".
    mine = write(tmp_path, {"layout-inspector": {"server": {"command": "x"}, "measures": False}})
    assert caps.load_all(mine, env=off)["layout-inspector"].measures is False
    assert "takes" not in caps.notices(mine)[0]
    assert caps.notices(write(tmp_path, ["not", "an", "object"])) == []


@pytest.mark.parametrize(("command", "args", "loose"), [
    ("npx", ["@playwright/mcp@latest"], "@playwright/mcp@latest"),
    ("npx", ["-y", "@playwright/mcp"], "@playwright/mcp"),
    ("/usr/bin/npx", ["some-mcp"], "some-mcp"),
    ("npx", ["some-mcp@next"], "some-mcp@next"),
    ("npx", ["@playwright/mcp@0.0.83"], None),
    ("npx", ["some-mcp@1.2.3"], None),
    ("npx", ["-y"], None),
    ("node", ["server.js"], None),
])
def test_an_unpinned_package_is_named(command, args, loose):
    assert caps.unpinned({"command": command, "args": args}) == loose


def test_the_example_registry_pins_what_it_fetches():
    assert all(caps.unpinned(entry["server"]) is None for entry in caps.EXAMPLE.values())


def test_on_request_joins_only_when_asked_and_a_registry_key_sets_it(tmp_path):
    registry = caps.load(write(tmp_path, {"b": {"server": {"command": "b"}, "when": [], "on_request": True,
                                                "hint": "navigate"}}))
    assert registry["b"].on_request
    for kind in ("test", "ui", "implement"):
        assert caps.select(registry, kind, failed_before=True, workdir=Path("/w")).names == ()
    got = caps.select(registry, "implement", workdir=Path("/w"), asked=True)
    assert got.names == ("b",) and "given because the request asks to browse" in got.hints[0]
