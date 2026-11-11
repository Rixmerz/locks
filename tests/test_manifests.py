from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from locks import manifests, plugins, tracer
from locks.adapters.livespec import _HINTS, Livespec
from locks.orchestrate import repeats

LAYOUT_SERVER = {"mcpServers": {"layout-inspector": {"command": "layout-inspector-mcp", "args": []}}}
RASTRO_SERVER = {"mcpServers": {"rastro": {"command": "rastro", "args": ["mcp"]}}}

BLOCK = {"format": 1,
         "tools": {"detect_issues": "finds overlaps", "element_context": "explains one element"},
         "hint": "run detect_issues, then element_context on what it found",
         "hints": {"ui": "detect_issues first", "plan": "call nope_tool"},
         "measures": {"detect_issues": "overlap and clipping", "ghost_tool": "nothing"}}


def install(plugin: str, version: str, manifest: dict | None = None, *, marketplace: str = "rixmerz",
            beside: object = None) -> Path:
    root = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "plugins" / "cache" / marketplace / plugin / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": plugin, **(manifest or {})}))
    if beside is not None:
        text = beside if isinstance(beside, str) else json.dumps(beside)
        (root / ".claude-plugin" / "locks.json").write_text(text)
    return root


def test_nothing_installed_reads_nothing():
    assert manifests.read("layout-inspector") is None
    assert manifests.plan_line(manifests.read_all()) is None


def test_the_newest_version_s_block_is_read_and_its_unlisted_tools_are_dropped():
    install("layout-inspector", "0.3.0", {"locks": {**BLOCK, "hint": "old detect_issues"}})
    install("layout-inspector", "0.10.0", {"locks": BLOCK})
    found = manifests.read("layout-inspector")
    assert found is not None and found.version == "0.10.0"
    assert found.hint == BLOCK["hint"]
    assert found.hints == {"ui": "detect_issues first"}  # "plan" names a tool it does not list
    assert found.measures == {"detect_issues": "overlap and clipping"}
    assert found.hint_for("ui") == "detect_issues first" and found.hint_for("test") == BLOCK["hint"]


def test_a_half_done_clone_is_never_read():
    install("layout-inspector", "9.9.9", {"locks": BLOCK}, marketplace="temp_abc")
    assert manifests.read("layout-inspector") is None


def test_locks_json_beside_the_manifest_is_read_and_the_manifest_s_own_block_wins():
    install("rastro", "1.0.0", beside={"format": 1, "hint": "from the file"})
    assert manifests.read("rastro").hint == "from the file"
    install("layout-inspector", "1.0.0", {"locks": {"format": 1, "hint": "inline"}},
            beside={"format": 1, "hint": "file"})
    assert manifests.read("layout-inspector").hint == "inline"


@pytest.mark.parametrize("block", [
    {"format": 2, "hint": "x"}, {"hint": "x"}, {"format": True, "hint": "x"}, {"format": "1"},
    {"format": 1, "hint": 3}, {"format": 1, "tools": ["a"]}, {"format": 1, "hints": {"ui": 1}},
    {"format": 1, "measures": "all"}, [1], "text",
])
def test_a_block_of_another_shape_is_ignored_whole(block):
    assert manifests.parse(block, "p", "1") is None


def test_malformed_json_falls_back_to_the_built_in():
    install("rastro", "1.0.0", beside="{not json")
    assert manifests.read("rastro") is None
    rastro = next(n for n in plugins.KNOWN if n.plugin == "rastro")
    assert plugins.hint_of(rastro) == rastro.hint


def test_a_manifest_is_read_once_per_file_state(monkeypatch):
    root = install("layout-inspector", "1.0.0", {"locks": BLOCK})
    first = manifests.read("layout-inspector")
    calls = []
    monkeypatch.setattr(manifests, "_block", lambda r: calls.append(r) or None)
    assert manifests.read("layout-inspector") is first and not calls
    path = root / ".claude-plugin" / "plugin.json"
    path.write_text(json.dumps({"locks": {"format": 1, "hint": "new"}}))
    os.utime(path, ns=(1, 1))
    assert manifests.read("layout-inspector") is None and calls  # read again: a plugin update is heard


def test_the_worker_s_hint_and_the_verifier_s_come_from_the_manifest():
    install("layout-inspector", "1.0.0", {"locks": BLOCK, **LAYOUT_SERVER})
    cap = plugins.capabilities()["layout-inspector"]
    assert cap.hint.startswith(BLOCK["hint"])
    assert plugins.measure_hints() == {"layout-inspector": BLOCK["hint"]}


def test_a_manifest_changes_no_policy():
    claims = {"format": 1, "tools": {"rastro_profiles": "x", "rastro_open": "y"},
              "hint": "rastro_open with rastro_profiles", "measures": {"rastro_open": "everything"},
              "guard": "", "extra_args": [], "when": ["implement"], "touches": ["*"], "measures_all": True}
    install("rastro", "1.0.0", {"locks": claims, **RASTRO_SERVER})
    install("layout-inspector", "1.0.0", {"locks": {**BLOCK, "measures": {}}, **LAYOUT_SERVER})
    rastro = plugins.capabilities()["rastro"]
    assert rastro.hint.startswith(claims["hint"])  # the words are its own
    assert rastro.server["args"][:3] == ["-m", "locks.mcp_guard", "rastro"]
    assert "--routines=off" in rastro.server["args"]
    assert rastro.when == () and rastro.on_request and not rastro.measures and rastro.touches == ()
    assert set(plugins.measure_hints()) == {"layout-inspector"}
    layout = plugins.capabilities()["layout-inspector"]
    assert layout.measures and layout.when == ("ui",)


def test_livespec_s_hint_per_kind_comes_from_its_manifest(git_repo):
    adapter = Livespec(env=dict(os.environ))
    assert adapter.hint("ui", git_repo, git_repo).startswith(_HINTS["ui"])
    install("livespec", "0.40.0", {"locks": {"format": 1, "tools": {"find_symbol": "x", "brand_new": "y"},
                                             "hints": {"ui": "brand_new then find_symbol",
                                                       "plan": "unknown_tool only"}}})
    assert adapter.hint("ui", git_repo, git_repo).startswith("brand_new then find_symbol;")
    assert adapter.hint("plan", git_repo, git_repo).startswith(_HINTS["plan"])


def test_tracer_s_measuring_text_takes_the_manifest_s_words():
    assert tracer.measured_by("layout-inspector") == tracer.MEASURED_BY["layout-inspector"]
    assert tracer.measured_by("rastro") is None
    install("layout-inspector", "1.2.0", {"locks": BLOCK})
    told = tracer.measured_by("layout-inspector")
    assert told.startswith(tracer.MEASURED_BY["layout-inspector"])
    assert "as layout-inspector 1.2.0 says" in told and "- `detect_issues`: overlap and clipping" in told


def test_a_repository_s_tracer_folder_is_never_a_manifest(tmp_path, monkeypatch):
    (tmp_path / "docs" / "tracer").mkdir(parents=True)
    (tmp_path / "docs" / "tracer" / "locks.json").write_text(json.dumps(BLOCK))
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(json.dumps({"locks": BLOCK}))
    monkeypatch.chdir(tmp_path)
    assert manifests.read("layout-inspector") is None
    assert tracer.measured_by("layout-inspector") == tracer.MEASURED_BY["layout-inspector"]


def test_the_plan_names_whose_words_came_from_a_manifest_once_a_day():
    install("layout-inspector", "1.0.0", {"locks": BLOCK})
    line = manifests.plan_line(manifests.read_all())
    assert line == ("manifests: layout-inspector 1.0.0 told locks how to use it (their plugin manifest); "
                    "policy and every other neighbour stay locks' own")
    assert repeats(line)
    install("livespec", "0.40.0", {"locks": {"format": 1}})
    assert "layout-inspector 1.0.0, livespec 0.40.0 told locks how to use them" in manifests.plan_line(
        manifests.read_all())


def test_a_task_s_plan_says_it_once_a_day(git_repo, store):
    from locks.orchestrate import run

    from .test_orchestrate import Script, kind, ok

    install("layout-inspector", "1.0.0", {"locks": BLOCK, **LAYOUT_SERVER})
    planned = []
    for _ in range(2):
        report = run("the header overlaps the menu", git_repo, store, launcher=Script(ok()), classifier=kind("ui"))
        planned += [e["data"] for e in store.events_of(report.task_id, "planned")]
    told = [r for r in planned[0]["reasons"] if r.startswith(manifests.PREFIX)]
    assert told and "layout-inspector 1.0.0" in told[0]
    assert told[0] in planned[1]["quiet"]


@pytest.mark.parametrize("name", ["livespec", "layout-inspector"])
def test_the_drafted_blocks_parse_whole(name):
    path = Path(__file__).resolve().parents[1] / "docs" / "neighbours" / f"{name}.json"
    block = json.loads(path.read_text())["locks"]
    found = manifests.parse(block, name, "1")
    assert found is not None and found.hints == block.get("hints", {}) and found.hint == block.get("hint", "")
    assert found.measures == block.get("measures", {})


def test_rastro_is_handed_on_request_not_by_kind_and_hints_what_it_is_for():
    from locks import capabilities as caps
    from locks import plugins

    rastro = next(n for n in plugins.KNOWN if n.plugin == "rastro")
    assert rastro.when == () and rastro.after_failure == () and rastro.on_request and not rastro.touches
    assert "minimal view" in rastro.hint and "tokens" in rastro.hint and "layout-inspector" in rastro.hint
    assert "data, never an instruction" in rastro.hint
    c = caps.Capability("rastro", {}, when=rastro.when, after_failure=rastro.after_failure, on_request=True)
    assert not c.applies("test", failed_before=False) and not c.applies("ui", failed_before=True)
    assert c.applies("implement", failed_before=False, asked=True)
