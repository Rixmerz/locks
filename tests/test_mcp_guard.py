from __future__ import annotations

import io
import json
import sys
import textwrap

import pytest

from locks import mcp_guard


@pytest.mark.parametrize(("host", "ok"), [
    ("localhost", True), ("localhost:3000", True), ("http://127.0.0.1:5173/app", True), ("app.localhost", True),
    ("[::1]:8080", True), ("::1", True), ("127.8.8.8", True), ("user@localhost", True),
    ("example.com", False), ("https://shop.example.com", False), ("10.0.0.2", False), ("localhost.evil.com", False),
])
def test_only_this_machine_is_loopback(host, ok):
    assert mcp_guard.loopback(host) is ok


def test_the_rastro_policy_keeps_a_worker_to_the_app_it_runs():
    assert mcp_guard.rastro("rastro_view", {"session": "mine"}) == (None, {"session": "locks-worker"})
    why, args = mcp_guard.rastro("rastro_open", {"url": "http://localhost:3000", "allowWrite": ["localhost:3000"]})
    assert why is None and args["session"] == "locks-worker"
    assert "profile" in mcp_guard.rastro("rastro_open", {"profile": "chrome:Default"})[0]
    assert "example.com" in mcp_guard.rastro("rastro_open", {"allowWrite": ["localhost", "example.com"]})[0]
    assert "allowUpload" in mcp_guard.rastro("rastro_open", {"allowUpload": ["files.example.com"]})[0]
    assert "list of hosts" in mcp_guard.rastro("rastro_open", {"allowWrite": "localhost"})[0]
    for name in ("rastro_profiles", "rastro_routines", "rastro_routine_run", "publish_assignment"):
        assert "not for a worker" in mcp_guard.rastro(name, {})[0]


def test_screen_passes_what_is_not_a_refused_call_and_answers_the_refused_ones():
    policy = mcp_guard.rastro
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    assert mcp_guard.screen(init, policy) == (init, [])
    ok = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "rastro_view", "arguments": {}}}
    sent, answers = mcp_guard.screen(ok, policy)
    assert sent["params"]["arguments"] == {"session": "locks-worker"} and answers == []
    bad = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "rastro_profiles"}}
    sent, answers = mcp_guard.screen(bad, policy)
    assert sent is None and answers[0]["id"] == 3 and answers[0]["result"]["isError"] is True
    assert "locks refused this call" in answers[0]["result"]["content"][0]["text"]
    sent, answers = mcp_guard.screen([init, bad], policy)
    assert sent == [init] and len(answers) == 1
    assert mcp_guard.screen([bad], policy)[0] is None
    assert mcp_guard.screen({"method": "tools/call", "params": "x"}, policy)[1][0]["id"] is None
    assert mcp_guard.screen("text", policy) == ("text", [])


def test_the_guard_relays_a_real_server_and_answers_what_it_refuses(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(textwrap.dedent("""
        import sys, json
        for line in sys.stdin:
            msg = json.loads(line)
            print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"echo": msg}}), flush=True)
    """))
    calls = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "rastro_profiles"}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "rastro_open", "arguments": {"allowWrite": ["localhost"]}}},
    ]
    stdin = io.BytesIO(b"".join(json.dumps(c).encode() + b"\n" for c in calls) + b"not json\n")
    stdout = io.BytesIO()
    assert mcp_guard.serve("rastro", [sys.executable, str(server)], stdin, stdout) == 1  # it chokes on "not json"
    got = {m["id"]: m for m in map(json.loads, stdout.getvalue().splitlines())}
    assert got[1]["result"]["echo"]["method"] == "initialize"
    assert got[2]["result"]["isError"] is True
    assert got[3]["result"]["echo"]["params"]["arguments"] == {"allowWrite": ["localhost"], "session": "locks-worker"}


def test_main_refuses_what_it_does_not_know(capsys):
    assert mcp_guard.main(["nope", "--", "x"]) == 2 and "usage" in capsys.readouterr().err
    assert mcp_guard.main(["rastro", "x"]) == 2


def test_livespec_policy_lets_only_the_reading_tools_through():
    from locks.adapters.livespec import TOOLS

    for name in sorted(mcp_guard.LIVESPEC_READ):
        assert mcp_guard.livespec(name, {"workspace": "/r"}) == (None, {"workspace": "/r"})
    # Every tool locks names to a worker reads, but the two that write the index (a worker's own
    # index of a fresh repository, the sync of its specs).
    assert {"index_project", "sync_openspec"} == TOOLS - mcp_guard.LIVESPEC_READ
    for name in ("index_project", "sync_openspec", "export_openspec", "bulk_link_spec_symbols", "create_spec",
                 "delete_spec", "import_specs_from_markdown", "record_track", "a_tool_added_next_year"):
        why, _ = mcp_guard.livespec(name, {})
        assert why and name in why


def test_a_refused_livespec_call_never_reaches_the_server():
    sent, answers = mcp_guard.screen(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "index_project", "arguments": {}}},
        mcp_guard.POLICIES["livespec"])
    assert sent is None and answers[0]["id"] == 7 and answers[0]["result"]["isError"]
    sent, answers = mcp_guard.screen(
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "who_calls", "arguments": {}}},
        mcp_guard.POLICIES["livespec"])
    assert sent is not None and answers == []


def test_a_guard_held_to_some_tools_lists_only_those_and_refuses_the_rest(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(textwrap.dedent("""
        import sys, json
        for line in sys.stdin:
            msg = json.loads(line)
            if msg.get("method") == "tools/list":
                result = {"tools": [{"name": n} for n in ("find_symbol", "who_calls", "index_project")]}
            else:
                result = {"echo": msg}
            print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": result}), flush=True)
    """))
    calls = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "who_calls"}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "find_symbol"}},
    ]
    stdin = io.BytesIO(b"".join(json.dumps(c).encode() + b"\n" for c in calls))
    stdout = io.BytesIO()
    mcp_guard.serve("livespec", [sys.executable, str(server)], stdin, stdout,
                    env={mcp_guard.ONLY: "find_symbol"})
    got = {m["id"]: m for m in map(json.loads, stdout.getvalue().splitlines())}
    assert [t["name"] for t in got[1]["result"]["tools"]] == ["find_symbol"]
    assert got[2]["result"]["isError"] is True and "not among the tools" in got[2]["result"]["content"][0]["text"]
    assert got[3]["result"]["echo"]["params"]["name"] == "find_symbol"


def test_a_guard_held_to_nothing_lists_and_lets_through_nothing_but_one_not_held_is_unchanged():
    assert mcp_guard.only_from({}) is None
    assert mcp_guard.only_from({mcp_guard.ONLY: ""}) == frozenset()
    answer = {"id": 7, "result": {"tools": [{"name": "a"}]}}
    assert mcp_guard.listed(answer, {7}, None) is answer
    assert mcp_guard.listed(answer, set(), frozenset()) is answer  # not an answer to a list it was asked
    assert mcp_guard.listed({"id": 7, "result": "odd"}, {7}, frozenset({"a"})) == {"id": 7, "result": "odd"}
