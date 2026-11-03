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
