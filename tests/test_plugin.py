"""The plugin's files point at things that exist."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from locks import __version__, cli, hooks

ROOT = Path(__file__).resolve().parents[1]


def test_every_hook_registered_is_one_locks_handles():
    registered = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    from locks import signature

    traced_only = {"SubagentStop", "Notification"}  # the opt-in trace's own entries (`locks.trace`)
    # A call that failed or was refused: the PostToolUse fast path's alone (`__main__`, `signature.FAILED`).
    assert set(registered) == {*hooks.WITH_ENV, *hooks.HANDLERS, *traced_only, *signature.FAILED}
    for event, entries in registered.items():
        commands = [h["command"] for e in entries for h in e["hooks"]]
        assert all("${CLAUDE_PLUGIN_ROOT}/bin/locks" in c for c in commands)
        assert all(c.endswith((f"hook {event}", f"hook-trace {event}")) for c in commands)
        assert any(c.endswith(f"hook {event}") for c in commands) or event in traced_only


def test_versions_agree():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert plugin["version"] == market["plugins"][0]["version"] == __version__
    assert f'version = "{__version__}"' in pyproject


def test_the_launcher_is_executable_and_the_entry_command_uses_it():
    assert os.access(ROOT / "bin" / "locks", os.X_OK)
    command = (ROOT / "commands" / "orchestration.md").read_text()
    assert "locks run" in command and "$ARGUMENTS" in command


def test_the_mod_is_declared_beside_the_classic_hooks():
    hooks_json = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    for module in hooks_json["modules"]:
        assert (ROOT / "hooks" / module).is_file()
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert (ROOT / plugin["types"]).is_file()
    assert plugin["userConfig"]["wake"]["default"] is True
    # the classic hooks stay: without the mod, endings and notes still reach the session
    assert {"SessionStart", "UserPromptSubmit", "Stop", "PreCompact"} <= set(hooks_json["hooks"])


def test_every_setting_is_an_option_on_the_plugin_s_settings_screen():
    from locks import config

    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert set(config.DEFAULTS) <= set(plugin["userConfig"])
    for key, default in config.DEFAULTS.items():
        option = plugin["userConfig"][key]["default"]
        assert type(option) is type(default) and option == default
        if key in config.CHOICES:
            assert plugin["userConfig"][key]["options"] == list(config.CHOICES[key])


def test_no_guidance_tells_a_session_to_pass_a_budget():
    """Cost is measured, never a brake: guidance that runs in other people's
    repositories never hands a session a budget flag that no command takes."""
    root = Path(__file__).resolve().parent.parent
    texts = [*root.glob("commands/*.md"), *root.glob("skills/*/SKILL.md"), *root.glob("hooks/*.tsx"),
             *root.glob("src/locks/roles*/*.md"), *root.glob("src/locks/guides/*.md")]
    assert texts and not [p.name for p in texts if "--budget" in p.read_text(encoding="utf-8")]


def test_the_skill_is_short_and_every_guide_it_names_exists(capsys):
    from locks import cli

    command = (ROOT / "commands" / "orchestration.md").read_text()
    assert len(command.splitlines()) < 120
    named = {"when", "dispatch", "endings", "orchestrator", "workflows", "panels", "loops", "notes", "memory",
             "relay", "models"}
    shipped = {p.stem for p in (ROOT / "src" / "locks" / "guides").glob("*.md")}
    assert named == shipped and all(f"`{n}`" in command for n in named)
    assert cli.main(["guide"]) == 0
    assert "endings" in capsys.readouterr().out
    assert cli.main(["guide", "endings"]) == 0
    assert "`replan` with `split`" in capsys.readouterr().out
    assert cli.main(["guide", "nope"]) == 2


def test_the_guidance_lists_every_kind_a_session_may_pass():
    """A session that found no list invented `--kind research`."""
    from locks import matrix

    skill = (ROOT / "commands" / "orchestration.md").read_text(encoding="utf-8")
    guide = (ROOT / "src" / "locks" / "guides" / "dispatch.md").read_text(encoding="utf-8")
    given = [k for k in matrix.KINDS if k not in ("chat", "classify", "frontier")]
    for text in (skill, guide):
        assert [k for k in given if f"`{k}`" not in text] == []


def test_an_unknown_kind_is_refused_with_the_kinds(capsys):
    from locks import matrix

    with pytest.raises(SystemExit):
        cli.main(["run", "--kind", "spelunking", "--dry-run", "x"])
    err = capsys.readouterr().err
    assert "is not a kind" in err and all(k in err for k in matrix.KINDS)


def test_the_dispatch_instructions_send_the_request_through_a_quoted_heredoc():
    """Request text in a double-quoted argument has its backticks run by the shell."""
    skill = (ROOT / "commands" / "orchestration.md").read_text(encoding="utf-8")
    guide = (ROOT / "src" / "locks" / "guides" / "dispatch.md").read_text(encoding="utf-8")
    for text in (skill, guide):
        assert "locks run - " in text and "<<'EOF'" in text
        assert 'locks run "<the task' not in text


#: What the Agent Skills spec allows in a SKILL.md's frontmatter (anthropics/skills' validator), and
#: the keys Claude Code itself reads on a plugin's skills.
SPEC_KEYS = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
CLAUDE_CODE_KEYS = {"argument-hint", "disable-model-invocation", "model", "user-invocable"}


def _frontmatter(text: str) -> dict[str, str]:
    """A SKILL.md's frontmatter read as a strict YAML reader would: a plain value holding `: ` or
    ` #` is no value YAML parses (the spec's validator refuses the file), so it is reported."""
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "no frontmatter"
    out: dict[str, str] = {}
    key = None
    for line in match.group(1).splitlines():
        if line.startswith((" ", "\t")) and key:
            out[key] += " " + line.strip()
            continue
        key, sep, value = line.partition(":")
        assert sep, f"not a key: {line!r}"
        value = value.strip()
        if value and value[0] not in "'\"|>":
            assert ": " not in value and " #" not in value, f"{key}: a plain value YAML cannot parse"
        out[key] = "" if value in (">-", ">", "|", "|-") else value.strip("'\"")
    return out


@pytest.mark.parametrize("skill", sorted([*ROOT.glob("skills/*/SKILL.md"), *ROOT.glob("src/locks/skills/*/SKILL.md")]),
                         ids=lambda p: p.parent.name)
def test_every_skill_meets_the_agent_skills_spec(skill):
    meta = _frontmatter(skill.read_text(encoding="utf-8"))
    assert set(meta) <= SPEC_KEYS | CLAUDE_CODE_KEYS, set(meta) - SPEC_KEYS - CLAUDE_CODE_KEYS
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", meta.get("name", "")) and len(meta["name"]) <= 64
    assert meta["name"] == skill.parent.name
    assert 0 < len(meta.get("description", "")) <= 1024
    assert "<" not in meta["description"] and ">" not in meta["description"]
    assert skill.read_text(encoding="utf-8").count("\n") < 500


def test_no_test_undoes_the_suite_s_isolation():
    """`monkeypatch.undo()` undoes every patch of the test, the autouse home and session among them: a
    test that called it queued real work into the person's store under the session running the suite,
    and a real dispatcher ran it with real workers. Restore what one test patched by name instead."""
    used = [str(path.relative_to(ROOT)) for path in sorted((ROOT / "tests").glob("*.py"))
            if ".undo()" in path.read_text() and path.name != "test_plugin.py"]
    assert used == []
