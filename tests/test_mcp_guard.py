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
    received = tmp_path / "received.txt"
    server.write_text(textwrap.dedent("""
        import sys, json
        log = open(sys.argv[1], "w", encoding="utf-8")
        for line in sys.stdin:
            log.write(line)
            log.flush()
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
    assert mcp_guard.serve("rastro", [sys.executable, str(server), str(received)], stdin, stdout) == 0
    got = {m["id"]: m for m in map(json.loads, stdout.getvalue().splitlines())}
    # The line that is not JSON is answered with a parse error, and the child never receives it: it received
    # the two calls the policy lets through (the refused one, id 2, never reached it either).
    assert got[None]["error"]["code"] == -32700
    assert [json.loads(line)["id"] for line in received.read_text(encoding="utf-8").splitlines()] == [1, 3]
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


def test_a_line_that_does_not_parse_is_answered_and_never_forwarded(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(textwrap.dedent("""
        import sys, json
        for line in sys.stdin:
            msg = json.loads(line)
            print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"echo": msg}}), flush=True)
    """))
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    lines = [
        b"\xff\xfe not UTF-8\n",  # an invalid byte: UnicodeDecodeError, a ValueError
        b"[" * 100_000 + b"\n",  # nested past the parser's depth: RecursionError
        b'{"method": "initialize"\n',  # truncated
        json.dumps(init).encode() + b"\n",
    ]
    stdout = io.BytesIO()
    assert mcp_guard.serve("rastro", [sys.executable, str(server)], io.BytesIO(b"".join(lines)), stdout) == 0
    msgs = [json.loads(line) for line in stdout.getvalue().splitlines()]
    errors = [m for m in msgs if "error" in m]
    assert [(m["id"], m["error"]["code"]) for m in errors] == [(None, -32700)] * 3
    # The guard went on past every bad line: the one that parses reached the server and came back.
    assert [m["result"]["echo"] for m in msgs if "result" in m] == [init]


def test_a_message_whose_keys_differ_only_in_case_is_refused_and_not_forwarded():
    policy = mcp_guard.rastro
    refused = [
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "Method": "tools/call", "params": {"name": "rastro_view"}},
        {"jsonrpc": "2.0", "id": 5, "method": "initialize", "params": {}, "PARAMS": {}},
        {"jsonrpc": "2.0", "id": 6, "method": "initialize", "params": {"a": 1, "A": 2}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "rastro_view", "NAME": "rastro_profiles"}},
        {"jsonrpc": "2.0", "id": 8, "result": {}, "Result": {}},
        {"method": "initialize", "Method": "initialize"},  # no id: the refusal carries null
    ]
    for message in refused:
        sent, answers = mcp_guard.screen(message, policy)
        assert sent is None, message
        assert len(answers) == 1 and answers[0]["id"] == message.get("id"), message
        assert answers[0]["error"]["code"] == -32600 and "result" not in answers[0], message


def test_a_forwarded_message_keeps_only_the_keys_the_protocol_names():
    policy = mcp_guard.rastro
    sent, answers = mcp_guard.screen(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}, "extra": 1, "_meta": {}}, policy)
    assert sent == {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}} and answers == []
    sent, _ = mcp_guard.screen({"jsonrpc": "2.0", "id": 2, "result": {"ok": True}, "params": {}, "extra": 1}, policy)
    assert sent == {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}
    sent, _ = mcp_guard.screen({"jsonrpc": "2.0", "id": 3, "error": {"code": 1, "message": "x"}, "Junk": 1}, policy)
    assert sent == {"jsonrpc": "2.0", "id": 3, "error": {"code": 1, "message": "x"}}


def test_a_tools_call_keeps_only_its_name_arguments_and_meta():
    message = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": "rastro_view", "arguments": {}, "_meta": {"progressToken": 1}, "task": {"x": 1}}}
    sent, answers = mcp_guard.screen(message, mcp_guard.rastro)
    assert sent == {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "rastro_view", "arguments": {"session": "locks-worker"},
                               "_meta": {"progressToken": 1}}}
    assert answers == []


def test_a_batch_is_screened_item_by_item():
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}, "junk": 1}
    collide = {"jsonrpc": "2.0", "id": 2, "method": "initialize", "Method": "initialize"}
    refused = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "rastro_profiles"}}
    sent, answers = mcp_guard.screen([init, collide, refused], mcp_guard.rastro)
    assert sent == [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
    assert [a["id"] for a in answers] == [2, 3]
    assert answers[0]["error"]["code"] == -32600 and answers[1]["result"]["isError"] is True
