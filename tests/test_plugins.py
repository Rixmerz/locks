from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from locks import capabilities as caps
from locks import cli, config, plugins
from locks.orchestrate import run
from locks.store import home

from .test_orchestrate import Script, kind, ok


def claude() -> Path:
    return Path(os.environ["CLAUDE_CONFIG_DIR"])


def install(plugin: str, version: str, *, manifest: dict | None = None, files: dict[str, object] | None = None,
            skill: str | None = None) -> Path:
    root = claude() / "plugins" / "cache" / "rixmerz" / plugin / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": plugin, **(manifest or {})}))
    for rel, data in (files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(json.dumps(data) if not isinstance(data, str) else data)
    if skill:
        (root / "skills" / skill).mkdir(parents=True)
        (root / "skills" / skill / "SKILL.md").write_text("---\nname: x\n---\nmeasure\n")
    return root


LAYOUT = {"mcpServers": {"layout-inspector": {"command": "uv", "args": [
    "run", "--frozen", "--project", "${CLAUDE_PLUGIN_ROOT}", "layout-inspector-mcp"], "env": {}}}}


def test_an_installed_plugin_s_server_is_read_newest_first_with_its_root_filled_in():
    install("layout-inspector", "0.3.0", manifest={"mcpServers": "./.claude-plugin/mcp.json"},
            files={".claude-plugin/mcp.json": LAYOUT})
    newest = install("layout-inspector", "0.4.0", manifest={"mcpServers": "./.claude-plugin/mcp.json"},
                     files={".claude-plugin/mcp.json": LAYOUT}, skill="layout-inspection")
    found = plugins.installed()["layout-inspector"]
    assert found.source == "plugin 0.4.0" and found.root == newest
    assert found.server["args"] == ["run", "--frozen", "--project", str(newest), "layout-inspector-mcp"]
    cap = plugins.capabilities()["layout-inspector"]
    assert cap.when == ("ui",) and cap.after_failure == ("implement", "feature")
    assert f"read {newest / 'skills' / 'layout-inspection' / 'SKILL.md'}" in cap.hint
    assert cap.read_dir == str(newest / "skills" / "layout-inspection")


def test_a_clone_plugin_install_left_half_done_is_never_read_as_the_plugin():
    install("layout-inspector", "0.3.0", files={".mcp.json": LAYOUT})
    clone = claude() / "plugins" / "cache" / "temp_git_1" / "layout-inspector"
    (clone / "9.9.9").mkdir(parents=True)
    (clone / "9.9.9" / ".mcp.json").write_text(json.dumps(LAYOUT))
    (clone / "README.md").write_text("x")
    assert plugins.installed()["layout-inspector"].source == "plugin 0.3.0"
    assert [p.name for p in plugins.versions(os.environ)] == ["0.3.0"]


def test_rastro_runs_without_its_saved_routines_from_a_plugin_or_from_path(tmp_path, monkeypatch):
    install("rastro", "0.4.0", files={".mcp.json": {"mcpServers": {"rastro": {"command": "rastro", "args": ["mcp"]}}}})
    found = plugins.installed()["rastro"]
    assert found.server["command"] == sys.executable and found.skill_dir is None
    assert found.server["args"] == ["-m", "locks.mcp_guard", "rastro", "--", "rastro", "mcp", "--routines=off"]
    assert found.server["env"]["PYTHONPATH"].startswith(str(Path(plugins.__file__).resolve().parents[1]))
    assert "Before the first call" not in plugins.capabilities()["rastro"].hint
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rastro").write_text("#!/bin/sh\n")
    (bin_dir / "rastro").chmod(0o755)
    on_path = plugins.find(plugins.KNOWN[1], {"PATH": str(bin_dir), "CLAUDE_CONFIG_DIR": str(tmp_path / "none")})
    assert on_path.source == "PATH" and on_path.server["args"][-3:] == ["rastro", "mcp", "--routines=off"]
    kept = plugins.guarded({"command": "x", "env": {"PYTHONPATH": "/mine"}}, "rastro")
    assert kept["env"]["PYTHONPATH"].endswith(os.pathsep + "/mine") and kept["args"][-1] == "x"
    assert plugins.find(plugins.KNOWN[1], {"PATH": "", "CLAUDE_CONFIG_DIR": str(tmp_path / "none")}) is None


def _rastro_in(directory: Path) -> Path:
    (directory / "rastro").write_text("#!/bin/sh\n")
    (directory / "rastro").chmod(0o755)
    return directory


def test_a_server_entry_inline_broken_or_outside_the_plugin_is_read_or_left_out():
    root = install("layout-inspector", "1.0.0", manifest={"mcpServers": LAYOUT["mcpServers"]})
    assert plugins.installed()["layout-inspector"].root == root
    install("rastro", "2.0.0", manifest={"mcpServers": "../../../../escape.json"})
    install("rastro", "1.0.0", files={".mcp.json": "not json"})
    install("rastro", "0.9.0", files={".mcp.json": {"rastro": {"args": []}}})
    assert "rastro" not in plugins.installed({"PATH": "", "CLAUDE_CONFIG_DIR": str(claude())})
    (claude() / "plugins" / "cache" / "rixmerz" / "rastro" / "0.8.0").mkdir()
    assert plugins._servers_of(claude() / "plugins" / "cache" / "rixmerz" / "rastro" / "0.8.0") == {}
    (root / ".claude-plugin" / "plugin.json").write_text("[]")
    assert plugins._servers_of(root) == {}
    assert plugins.claude_dir({"HOME": "/h"}) == Path("/h/.claude")


def test_the_registry_wins_and_the_setting_leaves_installed_plugins_out(tmp_path):
    install("rastro", "0.4.0", files={".mcp.json": {"mcpServers": {"rastro": {"command": "rastro", "args": ["mcp"]}}}})
    path = tmp_path / "caps.json"
    assert set(caps.load_all(path)) == {"rastro"}
    path.write_text(json.dumps({"rastro": {"server": {"command": "mine"}}}))
    assert caps.load_all(path)["rastro"].server == {"command": "mine"}
    path.unlink()
    config.save({"plugins": False})
    assert caps.load_all(path) == {}


def test_every_tool_a_hint_names_is_one_the_plugin_ships():
    for neighbour in plugins.KNOWN:
        named = set(re.findall(r"\b([a-z]+_[a-z_]+)\b", neighbour.hint))
        assert named and named <= neighbour.tools, neighbour.plugin


def test_a_worker_gets_the_installed_neighbour_and_reads_its_skill(git_repo, store):
    root = install("layout-inspector", "0.4.0", manifest={"mcpServers": "./.claude-plugin/mcp.json"},
                   files={".claude-plugin/mcp.json": LAYOUT}, skill="layout-inspection")
    script = Script(ok())
    run("the header overlaps the menu", git_repo, store, launcher=script, classifier=kind("ui"))
    spec = script.specs[0]
    assert "layout-inspector" in spec.mcp_servers and "mcp__layout-inspector" in spec.allowed_tools
    skill = (root / "skills" / "layout-inspection").resolve()
    assert f"Read(/{skill}/**)" in spec.allowed_tools
    assert "layout-inspector: measure the rendered page" in spec.append_system_prompt


def test_capabilities_and_neighbours_say_where_each_comes_from(capsys, git_repo):
    assert cli.main(["capabilities"]) == 0
    assert "no neighbour installed" in capsys.readouterr().out
    install("layout-inspector", "0.4.0", manifest={"mcpServers": "./.claude-plugin/mcp.json"},
            files={".claude-plugin/mcp.json": LAYOUT}, skill="layout-inspection")
    (home()).mkdir(parents=True, exist_ok=True)
    (home() / "capabilities.json").write_text(json.dumps({"index": {"server": {"command": "idx"}}}))
    assert cli.main(["capabilities"]) == 0
    out = capsys.readouterr().out
    assert "layout-inspector     ui; after a failed implement, feature  (installed: plugin 0.4.0)" in out
    assert "index                always  (your capabilities.json)" in out
    assert cli.main(["neighbours", "--repo", str(git_repo)]) == 0
    out = capsys.readouterr().out
    assert "layout-inspector: plugin 0.4.0" in out and "skill " in out
    assert "rastro: not installed (claude plugin install rastro@rixmerz)" in out
    config.save({"plugins": False})
    assert cli.main(["capabilities"]) == 0
    assert "installed plugins are left out" in capsys.readouterr().out


def test_a_browser_neighbour_is_not_handed_without_a_chromium(tmp_path, monkeypatch):
    from locks import plugins

    monkeypatch.delenv("LOCKS_BROWSER_CHECK", raising=False)
    monkeypatch.delenv("LAYOUT_INSPECTOR_CHROMIUM", raising=False)
    empty = tmp_path / "browsers"
    empty.mkdir()
    env = {"HOME": str(tmp_path), "PLAYWRIGHT_BROWSERS_PATH": str(empty)}
    assert not plugins.browser_found(env)
    (empty / "chromium-1243").mkdir()
    assert plugins.browser_found(env)
    assert plugins.browser_found({"HOME": str(tmp_path), "LOCKS_BROWSER_CHECK": "off"})
    chrome = tmp_path / "chrome"
    chrome.write_text("")
    assert plugins.browser_found({"HOME": str(tmp_path), "LAYOUT_INSPECTOR_CHROMIUM": str(chrome)})
