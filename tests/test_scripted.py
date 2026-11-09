"""A program's `claude -p` gets nothing from locks' hooks; a person's session keeps them."""
from __future__ import annotations

import io
import json

import pytest

from locks import hooks, scripted


@pytest.fixture(autouse=True)
def _look(monkeypatch):
    """The suite turns the look off (conftest); these tests are the look."""
    monkeypatch.delenv(scripted.CHECK, raising=False)


SCHEMA = ["claude", "-p", "--model", "haiku", "--json-schema", '{"type":"object"}']


def tree(*levels):
    """A fake process table: the hook's parent first, each level's parent the next."""
    table = {}
    for i, (argv, tty) in enumerate(levels):
        table[100 + i] = (101 + i if i + 1 < len(levels) else 1, list(argv), tty)
    return table.get


def test_why_reads_print_mode_and_who_reads_it():
    assert scripted.why(SCHEMA, True) == "it asks for structured output"
    assert scripted.why(["claude", "--print", "--json-schema={}"], True) == "it asks for structured output"
    assert scripted.why(["claude", "-p", "--output-format", "json"], True) == "a program reads its output"
    assert scripted.why(["claude", "-p", "--output-format=stream-json"], True) == "a program reads its output"
    assert scripted.why(["claude", "-p", "hi"], False) == "its input is no terminal"
    # A person's one-shot in a terminal, an interactive session, an app holding a conversation.
    assert scripted.why(["claude", "-p", "hi"], True) is None
    assert scripted.why(["claude", "-p", "hi"], None) is None
    assert scripted.why(["claude", "--preload", "x.sock"], False) is None
    assert scripted.why(["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json"],
                        False) is None


def test_is_claude():
    assert scripted.is_claude(["claude", "-p"])
    assert scripted.is_claude(["/opt/claude-code/bin/claude"])
    assert scripted.is_claude(["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", "-p"])
    assert scripted.is_claude(["node", "/home/x/.local/bin/claude"])
    assert not scripted.is_claude(["/bin/sh", "-c", "claude -p"])
    assert not scripted.is_claude([])


def test_the_nearest_claude_above_the_hook_decides():
    read = tree((["python3", "-m", "locks", "hook", "SessionStart"], False), (["/bin/sh", "-c", "locks"], False),
                (SCHEMA, False), (["claude", "--preload", "s.sock"], False))
    assert scripted.scripted({}, start=100, read=read) == "it asks for structured output"
    # The person's session above a shell: no `-p`, so the hooks run.
    read = tree((["/bin/sh", "-c", "locks"], False), (["claude"], True))
    assert scripted.scripted({}, start=100, read=read) is None


def test_what_cannot_be_read_is_no_programs(monkeypatch):
    assert scripted.scripted({}, start=100, read=lambda pid: None) is None
    assert scripted.scripted({}, start=100, read=tree((["bash"], False), (["bash"], False))) is None
    assert scripted.scripted({}, start=1, read=tree((SCHEMA, False))) is None

    def broken(pid):
        raise RuntimeError("no /proc")

    assert scripted.scripted({}, start=100, read=broken) is None
    deep = tree(*[(["bash"], False)] * (scripted.MAX_DEPTH + 1), (SCHEMA, False))
    assert scripted.scripted({}, start=100, read=deep) is None


def test_the_check_can_be_turned_off(monkeypatch):
    read = tree((SCHEMA, False))
    monkeypatch.delenv(scripted.CHECK, raising=False)
    assert scripted.scripted({}, start=100, read=read)
    assert scripted.scripted({scripted.CHECK: "off"}, start=100, read=read) is None
    monkeypatch.setenv(scripted.CHECK, "OFF")
    assert scripted.scripted({}, start=100, read=read) is None


def test_the_proc_reader_reads_this_process():
    import os
    import sys

    if not sys.platform.startswith("linux"):
        return
    found = scripted.read_process(os.getpid())
    assert found is not None
    ppid, argv, _tty = found
    assert ppid == os.getppid() and argv
    assert scripted._proc(2**22 + 12345) is None


def test_ps_reader_parses_and_fails_closed(monkeypatch):
    class Done:
        stdout = "  42 /usr/local/bin/claude -p --json-schema {}\n"

    monkeypatch.setattr(scripted.subprocess, "run", lambda *a, **k: Done())
    assert scripted._ps(7) == (42, ["/usr/local/bin/claude", "-p", "--json-schema", "{}"], None)

    def boom(*a, **k):
        raise OSError("no ps")

    monkeypatch.setattr(scripted.subprocess, "run", boom)
    assert scripted._ps(7) is None
    monkeypatch.setattr(scripted.sys, "platform", "darwin")
    assert scripted.read_process(7) is None


def test_a_programs_session_gets_no_injection(tmp_path, monkeypatch):
    """The hook answers nothing and opens no store for a scripted session; a person's gets its answer."""
    monkeypatch.setattr(hooks.scripted, "scripted", lambda env: "it asks for structured output")
    opened = []
    monkeypatch.setattr(hooks, "Store", lambda *a, **k: opened.append(a))
    out = io.StringIO()
    event = {"session_id": "s1", "cwd": str(tmp_path), "prompt": "Say ok.", "hook_event_name": "UserPromptSubmit"}
    assert hooks.main("UserPromptSubmit", io.StringIO(json.dumps(event)), out, {"LOCKS_HOME": str(tmp_path)}) == 0
    assert out.getvalue() == "" and opened == []


def test_a_persons_session_is_handled(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks.scripted, "scripted", lambda env: None)
    seen = []
    monkeypatch.setattr(hooks, "handle", lambda name, event, store, root, env: seen.append(name) or {"ok": 1})
    out = io.StringIO()
    event = {"session_id": "s1", "cwd": str(tmp_path)}
    hooks.main("UserPromptSubmit", io.StringIO(json.dumps(event)), out, {"LOCKS_HOME": str(tmp_path)})
    assert seen == ["UserPromptSubmit"] and json.loads(out.getvalue()) == {"ok": 1}
