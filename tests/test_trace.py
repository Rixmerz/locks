"""The opt-in observability trace (`locks.trace`)."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from locks import config, hooks, trace
from locks.store import Store, home


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("LOCKS_TRACE", "on")
    return dict(os.environ)


def test_off_by_default() -> None:
    env = {k: v for k, v in os.environ.items() if k != "LOCKS_TRACE"}
    assert config.DEFAULTS["trace"] is False
    assert not trace.enabled(env)
    config.save({"trace": True}, env)
    assert trace.enabled(env)
    assert not trace.enabled({**env, "LOCKS_TRACE": "off"})


def test_tool_call_is_tagged_and_masked(on: dict[str, str]) -> None:
    event = {"session_id": "s1", "tool_name": "mcp__plugin_livespec_livespec__search_code",
             "tool_input": {"q": "token sk-abcdefghijklmnopqrstuv"}, "tool_response": {"ok": True}}
    trace.hook("PostToolUse", event, on)
    rows = trace.records(on, "s1")
    assert [r["tag"] for r in rows] == ["hook:PostToolUse", "tool:mcp__plugin_livespec_livespec__search_code"]
    assert rows[1]["livespec_tool"] == "search_code"
    assert "[secret]" in rows[1]["input"]["q"] and "sk-abc" not in json.dumps(rows)
    info = trace.summary(on, "s1")
    assert info["livespec_calls"] == {"search_code": 1}


def test_hook_main_traces_event_injection_and_transcript(on: dict[str, str], tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"message": {"id": "m1", "model": "opus",
                                                  "usage": {"input_tokens": 3, "output_tokens": 5}}}) + "\n")
    event = {"session_id": "s2", "transcript_path": str(transcript), "cwd": str(tmp_path)}
    hooks.main("Stop", io.StringIO(json.dumps(event)), io.StringIO(), on)
    info = trace.summary(on, "s2")
    assert info["by_tag"].get("hook:Stop") == 1
    assert info["tokens"] == {"opus": {"input": 3, "output": 5, "cache_read": 0, "cache_write": 0, "messages": 1}}


def test_store_events_reach_the_session_trace(on: dict[str, str]) -> None:
    store = Store(home() / "locks.db")
    try:
        task = store.create_task("do it", status="running", source="cli", session_id="s3")
        store.add_event(int(task["id"]), "briefed", notes=[1, 2])
    finally:
        store.close()
    info = trace.summary(on, "s3")
    assert info["locks_events"]["briefed"] == 1
    assert info["notes_briefed"]


def test_hook_trace_entry_point(on: dict[str, str]) -> None:
    event = {"session_id": "s4", "tool_name": "Read", "tool_input": {"file_path": "a.py"}}
    subprocess.run([sys.executable, "-m", "locks", "hook-trace", "PreToolUse"], input=json.dumps(event),
                   text=True, env=on, check=True)
    tags = [r["tag"] for r in trace.records(on, "s4")]
    assert tags == ["hook:PreToolUse", "tool:Read"]
    out = subprocess.run([sys.executable, "-m", "locks", "trace", "s4", "--json"], text=True, env=on,
                         capture_output=True, check=True).stdout
    assert json.loads(out)["tools"] == {"Read": 1}


def test_hook_trace_off_writes_nothing() -> None:
    env = {**os.environ, "LOCKS_TRACE": "off"}
    subprocess.run([sys.executable, "-m", "locks", "hook-trace", "PreToolUse"],
                   input=json.dumps({"session_id": "s5", "tool_name": "Read"}), text=True, env=env, check=True)
    assert trace.sessions(env) == []


def test_every_record_names_the_release(on: dict[str, str]) -> None:
    from locks import __version__

    trace.write(on, "v1", "hook:Stop")
    assert trace.records(on, "v1")[0]["version"] == __version__
    assert trace.summary(on, "v1")["versions"] == [__version__]


def test_a_session_action_is_kept_only_with_the_trace_on(on: dict[str, str]) -> None:
    trace.action({**on, "LOCKS_TRACE": "off"}, "a0", "relay_taken", source="old")
    trace.action(on, None, "relay_taken", source="old")
    assert trace.records(on, "a0") == []
    trace.action(on, "a1", "launch", print=True, tty=False, scripted="its input is no terminal")
    trace.action(on, "a1", "scripted", event="UserPromptSubmit", why="its input is no terminal")
    trace.action(on, "a1", "orchestrator_taken", via="started", took_from="p1")
    rows = trace.records(on, "a1")
    assert [r["tag"] for r in rows] == ["locks:launch", "locks:scripted", "locks:orchestrator_taken"]
    assert all(r["task_id"] is None for r in rows)
    info = trace.summary(on, "a1")
    assert info["launch"] == {"print": True, "tty": False, "scripted": "its input is no terminal"}
    assert [a["action"] for a in info["actions"]] == ["scripted", "orchestrator_taken"]
    assert info["actions"][1]["took_from"] == "p1"


def test_an_action_that_cannot_be_written_never_raises(on: dict[str, str], monkeypatch) -> None:
    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(trace, "write", broken)
    trace.action(on, "a2", "relay_taken", source="old")


def test_the_hooks_trace_how_a_session_was_launched_and_what_they_skipped(on: dict[str, str], tmp_path: Path,
                                                                          monkeypatch) -> None:
    shape = {"print": True, "json_schema": True, "output_format": None, "input_format": None, "tty": False}
    monkeypatch.setattr(hooks.scripted, "look", lambda env: ("it asks for structured output", shape))
    event = {"session_id": "p1", "cwd": str(tmp_path), "source": "startup"}
    for name in ("SessionStart", "UserPromptSubmit"):
        out = io.StringIO()
        hooks.main(name, io.StringIO(json.dumps({**event, "hook_event_name": name})), out, on)
        assert out.getvalue() == ""
    info = trace.summary(on, "p1")
    assert info["launch"] == {**shape, "scripted": "it asks for structured output"}
    assert [(a["action"], a["event"]) for a in info["actions"]] == [("scripted", "SessionStart"),
                                                                   ("scripted", "UserPromptSubmit")]
    assert info["injected"] == 0
    # A person's session: its launch is kept, nothing is skipped; one that could not be read says so.
    monkeypatch.setattr(hooks.scripted, "look", lambda env: (None, None))
    hooks.main("SessionStart", io.StringIO(json.dumps({**event, "session_id": "p2"})), io.StringIO(), on)
    info = trace.summary(on, "p2")
    assert info["launch"] == {"read": False, "scripted": None}
    assert info["actions"] == []


def test_the_orchestrator_role_moving_is_in_both_sessions_traces(on: dict[str, str], tmp_path: Path) -> None:
    from locks import orchestrator

    folder = tmp_path / ".locks"
    folder.mkdir()
    assert orchestrator.claim(folder, "person", str(tmp_path), via="sent")[0]
    assert orchestrator.claim(folder, "script", str(tmp_path), via="claimed", force=True) == (True, "person")
    assert orchestrator.ended(folder, "script", "clear")
    taken = trace.summary(on, "script")["actions"]
    assert [a["action"] for a in taken] == ["orchestrator_taken", "orchestrator_ended"]
    assert taken[0]["took_from"] == "person" and taken[1]["reason"] == "clear"
    lost = trace.summary(on, "person")["actions"]
    assert [(a["action"], a["to"]) for a in lost if a["action"] == "orchestrator_lost"] == [("orchestrator_lost",
                                                                                              "script")]


def test_a_revived_dispatcher_is_in_the_session_trace(on: dict[str, str], monkeypatch) -> None:
    from locks import dispatch

    monkeypatch.setattr(dispatch, "recover", lambda store, root, env: [7])
    monkeypatch.setattr(dispatch, "revive", lambda store, root, env: ["repo-a"])
    hooks._revive(None, home(), on, "r1")
    monkeypatch.setattr(dispatch, "revive", lambda store, root, env: [])
    monkeypatch.setattr(dispatch, "recover", lambda store, root, env: [])
    hooks._revive(None, home(), on, "r1")
    acts = trace.summary(on, "r1")["actions"]
    assert [(a["action"], a["lanes"], a["resumed"]) for a in acts] == [("dispatcher_revived", ["repo-a"], [7])]


def test_the_trace_command_says_the_launch_and_the_hooks_actions(on: dict[str, str], capsys) -> None:
    from locks import cli

    trace.action(on, "c1", "launch", print=True, json_schema=True, output_format="json", input_format=None,
                 tty=False, scripted="it asks for structured output")
    trace.action(on, "c1", "orchestrator_taken", via="started", took_from="p1")
    assert cli.main(["trace", "c1"]) == 0
    out = capsys.readouterr().out
    assert "locks " in out.splitlines()[0]
    assert ("launch: claude -p --json-schema --output-format json, no terminal; a program's: "
            "it asks for structured output, so the hooks gave it nothing") in out
    assert "orchestrator_taken" in out and '"took_from": "p1"' in out
    trace.action(on, "c2", "launch", print=False, json_schema=False, output_format=None, input_format=None,
                 tty=True, scripted=None)
    trace.action(on, "c3", "launch", read=False, scripted=None)
    trace.write(on, "c4", "hook:Stop")
    for sid in ("c2", "c3", "c4"):
        assert cli.main(["trace", sid]) == 0
    out = capsys.readouterr().out
    assert "launch: claude (interactive), a terminal; a person's" in out
    assert "launch: its claude process could not be read" in out
    assert "launch: not traced" in out and "what the hooks did beyond injecting: nothing recorded" in out


def test_the_fast_path_traces_a_person_and_a_worker_never_locks_own_model_calls(on: dict[str, str]) -> None:
    """locks' own `claude -p` (classify, notes) runs with `LOCKS_HOOKS_OFF=1` and no task: its tool calls
    filled three of twelve recent sessions' traces. A worker runs hooks-off too, and its calls are what its
    attempt's record reads, so it stays."""
    def call(session: str, **env: str) -> list[str]:
        event = {"session_id": session, "tool_name": "Read", "tool_input": {"file_path": "a.py"}}
        subprocess.run([sys.executable, "-m", "locks", "hook", "PostToolUse"], input=json.dumps(event),
                       text=True, env={**on, **env}, check=True)
        return [r["tag"] for r in trace.records(on, session)]

    assert call("person") == ["hook:PostToolUse", "tool:Read"]
    assert call("haiku", LOCKS_HOOKS_OFF="1") == []
    assert call("worker", LOCKS_HOOKS_OFF="1", LOCKS_WORKER_TASK="7") == ["hook:PostToolUse", "tool:Read"]


def test_a_failed_or_refused_call_is_a_failed_call_in_the_log_and_the_trace(on: dict[str, str]) -> None:
    """Claude Code fires PostToolUseFailure (a call that failed) or PermissionDenied (refused before it ran)
    instead of PostToolUse: forensics read 0 failed of 16 450 calls while a refused `npm install` and an
    MCP error happened in the same session (#160)."""
    from locks import signature

    for name, said in (("PostToolUseFailure", {"error": "Exit code 1"}),
                       ("PermissionDenied", {"reason": "auto mode classifier"})):
        event = {"session_id": "f", "tool_name": "Bash", "tool_input": {"command": "npm install"}, **said}
        subprocess.run([sys.executable, "-m", "locks", "hook", name], input=json.dumps(event),
                       text=True, env=on, check=True)
    tools = [r for r in trace.records(on, "f") if r["tag"] == "tool:Bash"]
    assert [(r["phase"], r["failed"], r["error"]) for r in tools] == [
        ("PostToolUseFailure", True, "Exit code 1"), ("PermissionDenied", True, "auto mode classifier")]
    logged = [json.loads(line) for f in sorted(signature.log_dir(on).glob("*.ndjson"))
              for line in f.read_text().splitlines()]
    assert [e["ok"] for e in logged if e["session_id"] == "f"] == [0, 0]
    assert signature.entry({"tool_name": "Read", "tool_response": {}}, on)["ok"] == 1
