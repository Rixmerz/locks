"""A livespec newer than the one locks was checked against: its tools are read once per release, and
what it does not serve is left out of what a worker is told, said in the plan."""
from __future__ import annotations

import io
import json
from pathlib import Path

from locks.adapters import PRESENT, Status
from locks.adapters.livespec import PINNED, TOOLS, Livespec

from .test_livespec_adapter import adapter, tool

NEWER = "99.0.0"


class Server:
    """A livespec MCP server on stdio that answers initialize and tools/list."""

    def __init__(self, tools):
        self.tools, self.asked = tools, []
        self.stdin = self
        self.stdout = io.StringIO()
        self._lines = []

    def __call__(self, argv, **kw):
        self.asked.append(argv)
        self.stdout = io.StringIO()
        return self

    def write(self, text):
        for line in text.splitlines():
            msg = json.loads(line)
            if "id" not in msg:
                continue
            result = ({"protocolVersion": "2024-11-05"} if msg["method"] == "initialize"
                      else {"tools": [{"name": n} for n in self.tools]})
            pos = self.stdout.tell()
            self.stdout.seek(0, 2)
            self.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
            self.stdout.seek(pos)

    def flush(self):
        pass

    def kill(self):
        pass

    def wait(self):
        return 0


def _status(root: Path, version: str) -> Status:
    return Status("livespec", PRESENT, "indexed", version, False, {"db": str(root / "none.db"), "project": 1,
                                                                    "root": str(root)})


def test_the_checked_release_and_older_ones_are_never_asked(tmp_path):
    server = Server([])
    a = adapter(tmp_path, tool(tmp_path, "livespec"), popen=server)
    assert a.missing_tools(PINNED, tmp_path) == frozenset()
    assert a.missing_tools("0.32.0", tmp_path) == frozenset() and a.missing_tools(None, tmp_path) == frozenset()
    assert server.asked == []


def test_a_newer_release_is_read_once_and_what_it_lacks_is_left_out_and_said(tmp_path):
    server = Server(sorted(TOOLS - {"read_unit", "find_orphan_tests"}))
    a = adapter(tmp_path, tool(tmp_path, "livespec"), popen=server)
    assert a.missing_tools(NEWER, tmp_path) == {"read_unit", "find_orphan_tests"}
    assert a.missing_tools(NEWER, tmp_path) == {"read_unit", "find_orphan_tests"}
    assert len(server.asked) == 1  # kept per release in the locks home
    status = _status(tmp_path, NEWER)
    assert "does not serve find_orphan_tests, read_unit" in a._release_note(status)[0]


def test_a_newer_release_whose_tools_cannot_be_read_briefs_as_the_checked_one(tmp_path):
    def broken(argv, **kw):
        raise OSError("no such file")

    a = adapter(tmp_path, tool(tmp_path, "livespec"), popen=broken)
    assert a.missing_tools(NEWER, tmp_path) is None
    note = a._release_note(_status(tmp_path, NEWER))[0]
    assert f"newer than the {PINNED}" in note and "could not be read" in note
    assert not (tmp_path / "locks" / "work" / "livespec-tools.json").exists()  # tried again next time


def test_a_newer_release_that_serves_every_tool_says_nothing(tmp_path):
    a = adapter(tmp_path, tool(tmp_path, "livespec"), popen=Server(sorted(TOOLS | {"a_new_tool"})))
    assert a.missing_tools(NEWER, tmp_path) == frozenset() and a._release_note(_status(tmp_path, NEWER)) == ()


def test_the_hint_names_what_a_newer_release_does_not_serve(tmp_path, git_repo, monkeypatch):
    a = adapter(tmp_path, tool(tmp_path, "livespec"), popen=Server(sorted(TOOLS - {"find_orphan_tests"})))
    monkeypatch.setattr(Livespec, "inspect", lambda self, root: _status(Path(root), NEWER))
    said = a.hint("test", git_repo, git_repo)
    assert f"not served by livespec v{NEWER}: find_orphan_tests; do not call them" in said
    assert "not served" not in a.hint("explore", git_repo, git_repo)
