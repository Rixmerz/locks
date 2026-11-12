from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from locks.adapters import ABSENT, PRESENT, UNREADABLE, Status
from locks.adapters import livespec as livespec_mod
from locks.adapters.livespec import _HINTS, PINNED, TOOLS, Livespec, is_test_path
from locks.adapters.livespec import _lock as lock

from .conftest import git
from .livespec_fixture import build


def adapter(tmp_path: Path, path: str = "", **kw) -> Livespec:
    env = {"HOME": str(tmp_path / "home"), "PATH": path, "CLAUDE_CONFIG_DIR": str(tmp_path / "cc"),
           "LOCKS_HOME": str(tmp_path / "locks")}
    return Livespec(env=env, **kw)


def tool(tmp_path: Path, name: str) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / name
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return str(bindir)


def test_how_livespec_runs_plugin_then_path_then_pinned(tmp_path):
    assert adapter(tmp_path).server() is None
    assert adapter(tmp_path, tool(tmp_path, "uvx")).server() == {"command": "uvx", "args": [f"livespec@{PINNED}"]}
    assert adapter(tmp_path, tool(tmp_path, "livespec")).server() == {"command": "livespec", "args": []}
    for version in ("0.31.0", "0.32.1"):
        cfg = tmp_path / "cc" / "plugins" / "cache" / "rixmerz" / "livespec" / version / ".mcp.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"mcpServers": {"livespec": {"command": "uvx", "args": [f"livespec@{version}"]}}}))
    a = adapter(tmp_path)
    assert a.server()["args"] == ["livespec@0.32.1"] and a._version() == "0.32.1"


def test_absent_present_stale_and_unreadable(tmp_path, git_repo):
    a = adapter(tmp_path, tool(tmp_path, "uvx"))
    status = a.inspect(git_repo)
    assert status.state == ABSENT and "not indexed" in status.detail
    assert adapter(tmp_path).inspect(git_repo).detail.startswith("cannot run")

    build(git_repo)
    status = a.inspect(git_repo / "src" if (git_repo / "src").exists() else git_repo)
    assert status.state == PRESENT and not status.stale and status.data["project"] == 1

    build_stale = git_repo.parent / "stale"
    subprocess.run(["git", "clone", "-q", str(git_repo), str(build_stale)], check=True)
    build(build_stale, indexed_at="2000-01-01 00:00:00")
    assert a.inspect(build_stale).stale

    other = git_repo.parent / "other"
    subprocess.run(["git", "clone", "-q", str(git_repo), str(other)], check=True)
    build(other, project_root=Path("/somewhere/else"))
    assert a.inspect(other).state == ABSENT

    (git_repo / ".mcp-docs" / "docs.db").write_bytes(b"not a database at all, not even close")
    assert a.inspect(git_repo).state == UNREADABLE


def test_group_db_is_honoured(tmp_path, git_repo):
    shared = tmp_path / "shared"
    db = build(shared, project_root=git_repo)
    (git_repo / ".livespec.toml").write_text(f'[workspace]\ngroup_db = "{db}"\n')
    assert Livespec.db_path(git_repo) == db
    assert adapter(tmp_path).inspect(git_repo).state == PRESENT
    (git_repo / ".livespec.toml").write_text("[workspace]\ngroup_db = \"../shared/.mcp-docs/docs.db\"\n")
    assert Livespec.db_path(git_repo) == db.resolve()
    (git_repo / ".livespec.toml").write_text("not = [toml")
    assert Livespec.db_path(git_repo) == git_repo / ".mcp-docs" / "docs.db"


def test_the_briefing_maps_the_code_and_raises_the_start(tmp_path, git_repo):
    build(git_repo)
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    briefing = a.brief("charge_card double-charges when the amount has cents", git_repo, status)
    assert briefing.lines == ()  # the map is the dossier's; brief() only routes
    text = a.dossier("charge_card double-charges when the amount has cents", git_repo, status).text
    assert "`src.billing.charge_card`" in text and "SPEC-7 Card charges are idempotent [critical]" in text
    assert "tests.test_parse" not in text  # tests reach the map through what they call
    assert briefing.critical and briefing.raise_rungs == 1
    widely = a.brief("parse_amount rounds wrong", git_repo, status)
    wide_text = a.dossier("parse_amount rounds wrong", git_repo, status).text
    assert "and 16 more" in wide_text
    assert "tests/test_parse.py" in wide_text
    assert widely.raise_rungs == 1 and not widely.critical
    assert a.dossier("nothing here matches zzzz", git_repo, status).text == ""
    assert a.dossier("x", git_repo, a.inspect(tmp_path)).text == ""


def test_the_assessment_names_specs_and_untouched_callers(tmp_path, git_repo):
    build(git_repo)
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    lines = a.assess(["src/billing.py"], git_repo, status)
    assert "SPEC-7" in lines[0] and "src/api.py" in lines[1]
    tested = a.assess(["src/parse.py"], git_repo, status)
    assert any("tests that exercise" in line and "tests/test_parse.py" in line for line in tested)
    assert a.assess(["README.md"], git_repo, status) == ()
    assert a.assess([], git_repo, status) == ()


def test_refresh_uses_livespecs_own_cli(tmp_path, git_repo):
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    a = adapter(tmp_path, tool(tmp_path, "uvx"), runner=runner)
    assert a.refresh(git_repo) == "livespec index refreshed"
    assert calls[0] == ["uvx", f"livespec@{PINNED}", "index", str(git_repo.resolve())]
    failing = adapter(tmp_path, tool(tmp_path, "uvx"),
                      runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom"))
    assert "failed" in failing.refresh(git_repo)

    def raising(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    assert "did not run" in adapter(tmp_path, tool(tmp_path, "uvx"), runner=raising).refresh(git_repo)
    assert "cannot run" in adapter(tmp_path).refresh(git_repo)


def test_a_plugin_naming_an_unpublished_release_falls_back_to_the_pinned_one(tmp_path, git_repo):
    cfg = tmp_path / "cc" / "plugins" / "cache" / "rixmerz" / "livespec" / "0.33.0" / ".mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"livespec": {"command": "uvx", "args": ["livespec@0.33.0"]}}}))
    # A real plugin also declares the same release in its manifest, which `pin()` reads.
    (cfg.parent / ".claude-plugin").mkdir()
    (cfg.parent / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "livespec", "version": "0.33.0"}))
    calls = []

    def runner(argv, **kw):
        calls.append(argv[1])
        if argv[1] == "livespec@0.33.0":
            return subprocess.CompletedProcess(argv, 1, "", "  × No solution found when resolving tool "
                                               "dependencies:\n  ╰─▶ Because livespec==0.33.0 was not found in "
                                               "the package registry, we can conclude ...")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    a = adapter(tmp_path, tool(tmp_path, "uvx"), runner=runner)
    assert a.server()["args"] == ["livespec@0.33.0"]
    said = a.refresh(git_repo)
    assert calls == ["livespec@0.33.0", f"livespec@{PINNED}"]
    assert "livespec 0.33.0 is not published, so the pinned" in said and said.startswith("livespec index refreshed")
    # Remembered: workers' servers and the next index use the pinned release at once.
    assert a.server() == {"command": "uvx", "args": [f"livespec@{PINNED}"]} and a._version() == PINNED
    calls.clear()
    assert a.refresh(git_repo) == "livespec index refreshed" and calls == [f"livespec@{PINNED}"]
    # Without uvx left to run the pinned one, the plugin's entry is all there is.
    assert adapter(tmp_path).server()["args"] == ["livespec@0.33.0"]
    # A failure that is not a missing release is no fallback.
    cfg.write_text(json.dumps({"mcpServers": {"livespec": {"command": "uvx", "args": ["livespec@0.34.0"]}}}))
    other = adapter(tmp_path, tool(tmp_path, "uvx"),
                    runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 2, "", "index crashed"))
    assert other.refresh(git_repo) == "livespec index failed: index crashed"
    assert "0.34.0" not in other.unpublished()
    assert livespec_mod._uvx_release({"command": "livespec"}) is None
    assert livespec_mod._uvx_release({"command": "uvx", "args": ["--from", "x", "livespec==1.2"]}) == "1.2"


def test_two_refreshes_of_one_index_never_run_at_once(tmp_path, git_repo, monkeypatch):
    """Parallel tasks in one repository each plan, and each would refresh a stale
    index: livespec then fails with "database is locked" in all but one."""
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    a = adapter(tmp_path, tool(tmp_path, "uvx"), runner=runner)
    with lock(a.lock_path(git_repo), wait_s=0):
        assert "still running" in a.refresh(git_repo, wait_s=0)
    assert calls == []
    # One that waited, and finds the index current, does not index again.
    fresh = Status("livespec", PRESENT, "indexed now", PINNED, stale=False)
    monkeypatch.setattr(a, "inspect", lambda repo_dir: fresh)
    waits = iter([(True, True)])
    monkeypatch.setattr(livespec_mod, "_lock", lambda path, wait_s: _fixed(next(waits)))
    assert "the run this one waited for" in a.refresh(git_repo)
    assert calls == []
    # The lock is per index file: a repository whose index is another file has its own.
    other = git_repo.parent / "other"
    other.mkdir()
    assert a.lock_path(other) != a.lock_path(git_repo)


@contextlib.contextmanager
def _fixed(value):
    yield value


def test_the_lock_waits_then_gives_up(tmp_path):
    path = tmp_path / "x.lock"
    ticks = iter([0.0, 0.5, 2.0])
    with lock(path, wait_s=0), lock(path, wait_s=1.0, pause=lambda s: None, clock=lambda: next(ticks)) as got:
        assert got == (False, True)


def test_background_refresh_never_waits(tmp_path, git_repo):
    started = []
    a = adapter(tmp_path, tool(tmp_path, "uvx"), popen=lambda argv, **kw: started.append((argv, kw)))
    assert a.refresh_in_background(git_repo, tmp_path / "logs" / "livespec.log")
    argv, kw = started[0]
    # Through locks, so the refresh holds the same lock as every other one.
    assert argv[1:] == ["-m", "locks", "index-livespec", str(git_repo.resolve())] and kw["start_new_session"]
    assert kw["env"]["LOCKS_HOOKS_OFF"] == "1"
    with lock(a.lock_path(git_repo), wait_s=0):
        assert a.indexing(git_repo) and not a.refresh_in_background(git_repo, tmp_path / "logs" / "livespec.log")
    assert len(started) == 1 and not a.indexing(git_repo)
    assert not adapter(tmp_path).refresh_in_background(git_repo, tmp_path / "x.log")

    def broken(*a, **k):
        raise OSError("no")

    assert not adapter(tmp_path, tool(tmp_path, "uvx"), popen=broken).refresh_in_background(git_repo, tmp_path / "y")


def test_every_tool_a_hint_names_exists_in_livespec():
    named = set()
    for hint in _HINTS.values():
        named |= {w.strip(",.;()") for w in hint.split() if "_" in w or w.strip(",.;()") in {"search"}}
    named = {n for n in named if n.replace("_", "").isalpha()}
    assert named <= TOOLS, named - TOOLS


def test_hint_names_the_workspace_and_where_edits_go(tmp_path, git_repo):
    a = adapter(tmp_path)
    assert f'workspace="{git_repo.resolve()}"' in a.hint("debug-repro", git_repo, git_repo)
    assert "not your edits in /w" in a.hint("nope", git_repo, Path("/w"))


def test_test_paths():
    for path in ("tests/x.py", "pkg/test_x.py", "a/b_test.go", "web/x.test.ts", "src/__tests__/a.js"):
        assert is_test_path(path)
    assert not is_test_path("src/contest.py")


def test_the_real_schema_is_read_read_only(tmp_path, git_repo):
    db = build(git_repo)
    a = adapter(tmp_path)
    a.brief("charge_card", git_repo, a.inspect(git_repo))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM spec").fetchone()[0] == 1


def test_a_failed_index_is_not_tried_again_until_the_checkout_moves(tmp_path, git_repo):
    failing = adapter(tmp_path, tool(tmp_path, "uvx"), runner=lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, "", "Workspace directory not found: /old/path"))
    assert failing.last_failure(git_repo) is None
    failing.refresh(git_repo)
    assert failing.last_failure(git_repo) == "Workspace directory not found: /old/path"
    (git_repo / "next.py").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "next"], cwd=git_repo, check=True)
    assert failing.last_failure(git_repo) is None  # the checkout moved: worth one more try
    failing.refresh(git_repo)
    working = adapter(tmp_path, tool(tmp_path, "uvx"),
                      runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "{}", ""))
    working.refresh(git_repo)
    assert working.last_failure(git_repo) is None
    failing._failure_path(git_repo).write_text("not json")
    assert failing.last_failure(git_repo) is None


def test_the_briefing_says_how_many_symbols_the_request_reaches(tmp_path, git_repo):
    build(git_repo)
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    # parse_amount and its 21 callers outside tests
    assert a.brief("parse_amount rounds wrong", git_repo, status).reached == 22
    assert a.brief("nothing here matches zzzz", git_repo, status).reached == 0
    # no index to read says nothing, rather than zero
    assert a.brief("x", git_repo, a.inspect(tmp_path)).reached is None


def test_a_resolver_error_on_a_plugin_release_is_read_as_unpublished_never_as_a_failure(tmp_path, git_repo):
    """uv's resolver says "No solution found … livespec==X" or "there is no version of
    livespec==X": the release is not published, so the pinned one runs, and a failure an older
    locks recorded that way is not repeated (nor retried) at that commit."""
    assert livespec_mod.unpublished_in("× No solution found when resolving tool dependencies:\n"
                                       "╰─▶ Because there is no version of livespec==0.33.0 and you require "
                                       "livespec==0.33.0, we can conclude ...") == "0.33.0"
    assert livespec_mod.unpublished_in("Because livespec==0.33.0 was not found in the package registry") == "0.33.0"
    assert livespec_mod.unpublished_in("index crashed at livespec==0.33.0") is None
    assert livespec_mod._uvx_release({"command": "/opt/uv/bin/uv", "args": ["tool", "run", "livespec@0.33.0"]}) \
        == "0.33.0"
    cfg = tmp_path / "cc" / "plugins" / "cache" / "rixmerz" / "livespec" / "0.33.0" / ".mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"livespec": {"command": "uvx", "args": ["livespec@0.33.0"]}}}))
    # The manifest names the same release, as a real plugin's does: `pin()` must not take it for the pinned one.
    (cfg.parent / ".claude-plugin").mkdir()
    (cfg.parent / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "livespec", "version": "0.33.0"}))
    a = adapter(tmp_path, tool(tmp_path, "uvx"))
    a._remember_failure(git_repo, "× No solution found when resolving tool dependencies:\n  ╰─▶ Because there "
                                  "is no version of livespec==0.33.0 and you require livespec==0.33.0, we can ...")
    assert a.last_failure(git_repo) is None and "0.33.0" in a.unpublished()
    assert a.server() == {"command": "uvx", "args": [f"livespec@{PINNED}"]}
    assert not a._failure_path(git_repo).exists()
    # A plugin whose uvx is a full path off the PATH runs the pinned release with it.
    full = tmp_path / "opt" / "uvx"
    cfg.write_text(json.dumps({"mcpServers": {"livespec": {"command": str(full), "args": ["livespec@0.33.0"]}}}))
    assert adapter(tmp_path).server() == {"command": str(full), "args": [f"livespec@{PINNED}"]}
    # Any other failure is said once by the plans, while it stands.
    a._remember_failure(git_repo, "Workspace directory not found: /old")
    assert a.last_failure(git_repo) == "Workspace directory not found: /old"
    assert a.failure_said(git_repo) is False and a.failure_said(git_repo) is True
    assert a.last_failure(git_repo) == "Workspace directory not found: /old"


# --- index before dispatch ------------------------------------------------------


class Handle:
    """The `locks index-livespec` the adapter started: what its `wait` does is the test's."""

    def __init__(self, on_wait):
        self.on_wait, self.killed = on_wait, False

    def wait(self, timeout=None):
        return self.on_wait(timeout)

    def kill(self):
        self.killed = True


def make_repo(parent: Path, name: str) -> Path:
    where = parent / name
    where.mkdir(parents=True)
    git(where, "init", "-q", "-b", "main")
    (where / "a.py").write_text("x = 1\n")
    git(where, "add", "-A")
    git(where, "commit", "-q", "-m", "init")
    return where.resolve()


def spawner(started: list, on_wait):
    """A `popen` that records the argv and hands back a process whose `wait` is `on_wait(root, timeout)`."""
    def popen(argv, **kw):
        started.append(argv)
        return Handle(lambda timeout: on_wait(Path(argv[-1]), timeout))
    return popen


def builds(root, timeout):
    build(root)
    return 0


def test_roots_are_the_checkout_or_each_repository_inside_a_folder(tmp_path, git_repo):
    a = adapter(tmp_path)
    assert a.roots(git_repo) == [git_repo.resolve()]
    (git_repo / "sub").mkdir()
    assert a.roots(git_repo / "sub") == [git_repo.resolve()]
    folder = tmp_path / "folder"
    two, one = make_repo(folder, "two"), make_repo(folder, "one")
    (folder / "notes").mkdir()
    assert a.roots(folder) == [one, two]
    assert a.indexable(folder)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert a.roots(empty) == []
    assert not a.indexable(empty)


def test_a_folder_of_repositories_is_never_indexed_itself(tmp_path):
    folder = tmp_path / "folder"
    make_repo(folder, "one")
    make_repo(folder, "two")
    started, ran = [], []
    a = adapter(tmp_path, tool(tmp_path, "uvx"), popen=spawner(started, builds),
                runner=lambda argv, **kw: ran.append(argv))
    status = a.inspect(folder)
    assert status.state == ABSENT
    assert "folder of 2 repositories (one, two)" in status.detail and "never the folder" in status.detail
    assert "not indexed yet" not in status.detail
    assert "never indexes a folder" in a.refresh(folder)
    assert a.start_refresh(folder, tmp_path / "log") is None
    assert started == [] and ran == []
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "no git repository: nothing to index" in a.inspect(empty).detail


def test_ensure_fresh_leaves_a_current_index_alone(tmp_path, git_repo):
    build(git_repo)
    started = []
    a = adapter(tmp_path, tool(tmp_path, "uvx"), popen=spawner(started, builds))
    found = a.ensure_fresh(git_repo, 30)
    assert [(f.root, f.outcome) for f in found] == [(git_repo.resolve(), livespec_mod.FRESH)]
    assert found[0].line() == "" and started == []


def test_ensure_fresh_builds_a_missing_index_and_waits_for_it(tmp_path, git_repo):
    started, said = [], []
    a = adapter(tmp_path, tool(tmp_path, "uvx"), popen=spawner(started, builds))
    found = a.ensure_fresh(git_repo, 30, say=said.append)
    assert [(f.root, f.outcome) for f in found] == [(git_repo.resolve(), livespec_mod.BUILT)]
    assert started[0][1:] == ["-m", "locks", "index-livespec", str(git_repo.resolve())]
    assert said == ["locks: livespec is indexing repo before the worker starts (waits up to 30 s)"]
    assert found[0].line().startswith("livespec: repo: indexed before the worker started")
    assert a.inspect(git_repo).present


def test_ensure_fresh_gives_a_slow_index_the_wait_and_never_kills_it(tmp_path, git_repo):
    slow = []

    def too_long(root, timeout):
        slow.append(timeout)
        raise subprocess.TimeoutExpired("index-livespec", timeout)

    handles = []

    def popen(argv, **kw):
        handles.append(Handle(lambda timeout: too_long(Path(argv[-1]), timeout)))
        return handles[-1]

    a = adapter(tmp_path, tool(tmp_path, "uvx"), popen=popen)
    found = a.ensure_fresh(git_repo, 7)
    assert [f.outcome for f in found] == [livespec_mod.LATE]
    assert 0 < slow[0] <= 7
    assert "still indexing after the 7 s locks waits; this task runs without it" in found[0].line()
    assert not handles[0].killed


def test_ensure_fresh_reports_a_failed_index_and_does_not_retry_it_at_the_same_commit(tmp_path, git_repo):
    started = []
    a = adapter(tmp_path, tool(tmp_path, "uvx"))

    def fails(root, timeout):
        a._remember_failure(root, "index crashed")
        return 1

    a.popen = spawner(started, fails)
    found = a.ensure_fresh(git_repo, 30)
    assert [f.outcome for f in found] == [livespec_mod.FAILED]
    assert "indexing failed (index crashed); this task runs without it" in found[0].detail
    again = a.ensure_fresh(git_repo, 30)
    assert [f.outcome for f in again] == [livespec_mod.FAILED]
    assert "not tried again until the checkout moves" in again[0].detail
    assert len(started) == 1
    nowhere = adapter(tmp_path / "other").ensure_fresh(git_repo, 30)
    assert "cannot index here" in nowhere[0].detail


def stale_index(git_repo, monkeypatch):
    """An index that exists and is older than the last commit: readable, so a task can still use it."""
    build(git_repo)
    monkeypatch.setattr(livespec_mod, "_older_than_head", lambda indexed_at, root: True)


def test_past_the_wait_a_stale_index_is_said_to_be_used_as_it_is_not_to_be_missing(tmp_path, git_repo, monkeypatch):
    stale_index(git_repo, monkeypatch)

    def too_long(root, timeout):
        raise subprocess.TimeoutExpired("index-livespec", timeout)

    a = adapter(tmp_path, tool(tmp_path, "uvx"), popen=spawner([], too_long))
    found = a.ensure_fresh(git_repo, 7)
    assert [f.outcome for f in found] == [livespec_mod.LATE]
    assert "still indexing after the 7 s locks waits; this task uses the index as it is" in found[0].line()
    assert "runs without it" not in found[0].line()


def test_a_refresh_that_leaves_the_index_stale_says_the_task_uses_it_as_it_is(tmp_path, git_repo, monkeypatch):
    stale_index(git_repo, monkeypatch)
    a = adapter(tmp_path, tool(tmp_path, "uvx"), popen=spawner([], lambda root, timeout: 0))
    found = a.ensure_fresh(git_repo, 30)
    assert [f.outcome for f in found] == [livespec_mod.FAILED]
    assert "indexing failed (it left no usable index); this task uses the index as it is" in found[0].line()


def test_a_failure_at_this_commit_with_a_stale_index_says_the_task_uses_it_as_it_is(tmp_path, git_repo, monkeypatch):
    stale_index(git_repo, monkeypatch)
    a = adapter(tmp_path, tool(tmp_path, "uvx"), popen=spawner([], lambda root, timeout: 0))
    a._remember_failure(git_repo, "index crashed")
    found = a.ensure_fresh(git_repo, 30)
    assert "not tried again until the checkout moves; this task uses the index as it is" in found[0].line()


def test_with_no_livespec_a_stale_index_is_said_to_be_used_as_it_is(tmp_path, git_repo, monkeypatch):
    stale_index(git_repo, monkeypatch)
    found = adapter(tmp_path / "other").ensure_fresh(git_repo, 30)
    assert "cannot index here" in found[0].line()
    assert "this task uses the index as it is" in found[0].line()


def test_ensure_fresh_waits_on_a_refresh_another_process_holds(tmp_path, git_repo):
    started, clock_now = [], [0.0]
    a = adapter(tmp_path, tool(tmp_path, "uvx"), popen=spawner(started, builds))

    def pause(seconds):
        clock_now[0] += seconds

    with lock(a.lock_path(git_repo), wait_s=0):
        late = a.ensure_fresh(git_repo, 3, pause=pause, clock=lambda: clock_now[0])
    assert [f.outcome for f in late] == [livespec_mod.LATE] and started == []
    assert clock_now[0] >= 3

    clock_now[0] = 0.0
    held = contextlib.ExitStack()
    held.enter_context(lock(a.lock_path(git_repo), wait_s=0))

    def release_after_a_while(seconds):
        clock_now[0] += seconds
        if clock_now[0] >= 1:
            build(git_repo)
            held.close()

    done = a.ensure_fresh(git_repo, 30, pause=release_after_a_while, clock=lambda: clock_now[0])
    assert [f.outcome for f in done] == [livespec_mod.BUILT] and started == []


def test_ensure_fresh_indexes_each_repository_of_a_folder_and_never_the_folder(tmp_path):
    folder = tmp_path / "folder"
    one, two = make_repo(folder, "one"), make_repo(folder, "two")
    started = []
    a = adapter(tmp_path, tool(tmp_path, "uvx"), popen=spawner(started, builds))
    found = a.ensure_fresh(folder, 30)
    assert sorted(f.root for f in found) == [one, two]
    assert {f.outcome for f in found} == {livespec_mod.BUILT}
    assert sorted(Path(argv[-1]) for argv in started) == [one, two]
    assert folder.resolve() not in {Path(argv[-1]) for argv in started}
    assert not (folder / ".mcp-docs").exists()


def test_ensure_fresh_honours_a_shared_group_index(tmp_path):
    folder = tmp_path / "folder"
    one, two = make_repo(folder, "one"), make_repo(folder, "two")
    shared = tmp_path / "shared"
    db = build(shared, project_root=one)
    for member in (one, two):
        (member / ".livespec.toml").write_text(f'[workspace]\ngroup_db = "{db}"\n')
    started = []
    a = adapter(tmp_path, tool(tmp_path, "uvx"), popen=spawner(started, lambda root, timeout: 1))
    found = {f.root: f.outcome for f in a.ensure_fresh(folder, 30)}
    # `one` has its project in the shared file and is current; `two` has none there and is indexed on its own.
    assert found[one] == livespec_mod.FRESH
    assert found[two] != livespec_mod.FRESH
    assert [Path(argv[-1]) for argv in started] == [two]
    assert a.lock_path(one) == a.lock_path(two)


def _add_project(db: Path, root: Path) -> None:
    """What `livespec index <root>` leaves in a shared index: its project and a finished run."""
    with sqlite3.connect(db) as conn:
        project = conn.execute("INSERT INTO project (name, root) VALUES (?, ?)", (root.name, str(root))).lastrowid
        conn.execute("INSERT INTO index_run (project_id, finished_at) VALUES (?, '2999-01-01 00:00:00')", (project,))


def test_a_shared_group_index_indexes_the_next_repository_once_the_first_lets_its_lock_go(tmp_path):
    folder = tmp_path / "folder"
    one, two = make_repo(folder, "one"), make_repo(folder, "two")
    db = build(tmp_path / "shared", project_root=tmp_path / "elsewhere")
    for member in (one, two):
        (member / ".livespec.toml").write_text(f'[workspace]\ngroup_db = "{db}"\n')
    a = adapter(tmp_path, tool(tmp_path, "uvx"))
    held, started = contextlib.ExitStack(), []

    def popen(argv, **kw):
        root = Path(argv[-1])
        started.append(root)
        # The child holds the one lock of the shared file while it indexes its repository.
        held.enter_context(lock(a.lock_path(root), wait_s=0))

        def wait(timeout):
            _add_project(db, root)
            held.close()
            return 0
        return Handle(wait)

    a.popen = popen
    found = {f.root: f.outcome for f in a.ensure_fresh(folder, 30)}
    assert started == [one, two]
    assert found == {one: livespec_mod.BUILT, two: livespec_mod.BUILT}


# --- spec-first orientation ------------------------------------------------------


def _named(lines) -> set[str]:
    import re

    return {m for line in lines for m in re.findall(r"`([a-z_]+)`", line)}


def test_every_tool_the_spec_links_name_exists_in_livespec(tmp_path, git_repo):
    named = _named(adapter(tmp_path).spec_links_brief(git_repo))
    assert {"index_project", "sync_openspec"} <= named
    assert named <= TOOLS, named - TOOLS


def test_the_spec_links_brief_tells_a_shared_index_not_to_be_indexed_from_a_worktree(tmp_path, git_repo):
    a = adapter(tmp_path)
    own = "\n".join(a.spec_links_brief(git_repo))
    assert "`@spec:<capability>-<requirement-name>`" in own
    # A spec the worker just wrote is in the index only after `sync_openspec` (index_project reads specs only
    # where `.livespec.toml` asks it to), and that is what links its comments.
    assert "`sync_openspec`" in own
    assert "`index_project`" in own
    assert "validación becomes validacion" in own
    assert "livespec:id" in own
    assert "a warning" in own
    assert "blocks" in own
    (git_repo / ".livespec.toml").write_text(f'[workspace]\ngroup_db = "{tmp_path / "shared" / "docs.db"}"\n')
    shared = "\n".join(a.spec_links_brief(git_repo))
    assert "do not index the directory you work in" in shared
    assert "index_project" not in shared
    assert "sync_openspec" not in shared


def plugin_manifest(tmp_path: Path, version: str, *, mcp: bool = False) -> None:
    # Under the test's own CLAUDE_CONFIG_DIR (`adapter`'s `cc`), never this machine's plugin cache.
    folder = tmp_path / "cc" / "plugins" / "cache" / "market" / "livespec" / version
    (folder / ".claude-plugin").mkdir(parents=True)
    (folder / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "livespec", "version": version}))
    if mcp:
        (folder / ".mcp.json").write_text(json.dumps(
            {"mcpServers": {"livespec": {"command": "uvx", "args": [f"livespec@{version}"]}}}))


def test_the_pin_follows_the_installed_plugins_own_manifest(tmp_path):
    assert adapter(tmp_path).pin() == PINNED
    plugin_manifest(tmp_path, "0.31.0")
    plugin_manifest(tmp_path, "0.99.0")  # newest by version, not by name order
    a = adapter(tmp_path, tool(tmp_path, "uvx"))
    assert a.pin() == "0.99.0"
    # No `.mcp.json` to run: the manifest's release is what `uvx` is given.
    assert a.server() == {"command": "uvx", "args": ["livespec@0.99.0"]}


def test_a_manifest_release_uv_found_unpublished_is_not_pinned(tmp_path):
    plugin_manifest(tmp_path, "0.31.0")
    plugin_manifest(tmp_path, "0.99.0", mcp=True)
    a = adapter(tmp_path, tool(tmp_path, "uvx"))
    a._remember_unpublished("0.99.0")
    assert a.pin() == "0.31.0"  # the newest one not found unpublished
    a._remember_unpublished("0.31.0")
    assert a.pin() == PINNED
    assert a.server() == {"command": "uvx", "args": [f"livespec@{PINNED}"]}


def test_a_broken_manifest_leaves_the_built_in_pin(tmp_path):
    folder = tmp_path / "cc" / "plugins" / "cache" / "market" / "livespec" / "0.40.0" / ".claude-plugin"
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text("{not json")
    assert adapter(tmp_path).pin() == PINNED


def test_an_index_that_finishes_in_time_gives_its_code_and_both_outputs():
    done = livespec_mod._run_in_group(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"], timeout=10,
                                      env={"PATH": "/usr/bin:/bin"})
    assert (done.returncode, done.stdout, done.stderr) == (3, "out\n", "err\n")


def _running(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                           check=False).stdout.strip()
    return bool(state) and not state.startswith("Z")  # a zombie is dead, only not reaped yet


def test_an_index_that_times_out_ends_the_whole_group_not_only_uvx(tmp_path, git_repo, monkeypatch):
    # `uvx` starts the `livespec index` that does the work as its child. A timeout that killed
    # only `uvx` left that child indexing with nobody holding the lock: `indexing()` then said
    # False and the next refresh started a second index on the same file.
    pidfile = tmp_path / "child.pid"
    exe = tmp_path / "bin" / "uvx"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f"#!/bin/sh\nPATH=/usr/bin:/bin\nsleep 60 &\necho $! > {shlex.quote(str(pidfile))}\nwait\n")
    exe.chmod(0o755)
    monkeypatch.setattr(livespec_mod, "INDEX_TIMEOUT_S", 1)
    a = adapter(tmp_path, str(exe.parent))
    try:
        assert "did not run" in a.refresh(git_repo)
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while _running(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _running(child), "the index child outlived the timeout that cut it"
        assert not a.indexing(git_repo)
    finally:
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)


FAKE_SERVER = '''#!{python}
import json, os, sys
mode = {mode!r}
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    message["cwd"], message["livespec_home"] = os.getcwd(), os.environ.get("LIVESPEC_HOME")
    message["openspec"] = sorted(os.listdir("openspec")) if os.path.isdir("openspec") else None
    open({log!r}, "a").write(json.dumps(message) + "\\n")
    if mode == "hang":
        continue
    if message["method"] == "initialize":
        reply = {{"protocolVersion": "2024-11-05", "capabilities": {{}}}}
    elif mode == "die" and message["params"]["name"] == "validate_openspec":
        sys.exit(0)
    elif mode == "error":
        reply = {{"content": [{{"type": "text", "text": "boom"}}], "isError": True}}
    elif message["params"]["name"] == "sync_openspec":
        print("a log line that is no message", flush=True)
        reply = {{"content": [{{"type": "text", "text": json.dumps({{"synced": 1}})}}]}}
    else:
        data = {{"valid": True, "error_count": 0, "warning_count": 1, "findings": []}}
        reply = {{"content": [{{"type": "text", "text": "[]"}}], "structuredContent": data}}
    print(json.dumps({{"jsonrpc": "2.0", "id": message["id"], "result": reply}}), flush=True)
'''


def fake_livespec(tmp_path: Path, mode: str = "ok", runner=None) -> tuple[Livespec, Path]:
    import sys

    log = tmp_path / "asked.log"
    exe = tmp_path / "bin" / "livespec"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(FAKE_SERVER.format(python=sys.executable, mode=mode, log=str(log)))
    exe.chmod(0o755)
    runner = runner or (lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "{}", ""))
    return adapter(tmp_path, str(exe.parent), runner=runner), log


def _with_specs(git_repo: Path) -> None:
    (git_repo / "openspec" / "specs" / "cart").mkdir(parents=True)
    (git_repo / "openspec" / "specs" / "cart" / "spec.md").write_text("# cart\n")
    (git_repo / ".livespec.toml").write_text('[workspace]\ngroup_db = "/elsewhere/docs.db"\n')
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "specs")


def test_validate_openspec_runs_on_a_temporary_copy_and_writes_nothing_in_the_checkout(tmp_path, git_repo):
    _with_specs(git_repo)
    indexed = []

    def runner(argv, **kw):
        indexed.append((argv, kw["env"].get("LIVESPEC_HOME")))
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    a, log = fake_livespec(tmp_path, runner=runner)
    assert a.validate_openspec(git_repo) == {"valid": True, "error_count": 0, "warning_count": 1, "findings": []}
    asked = [json.loads(line) for line in log.read_text().splitlines()]
    assert [m["method"] for m in asked] == ["initialize", "tools/call", "tools/call"]
    assert [m["params"]["name"] for m in asked[1:]] == ["sync_openspec", "validate_openspec"]
    (argv, home_dir), = indexed
    copy = Path(argv[-1])
    assert argv[:2] == ["livespec", "index"] and not copy.is_relative_to(git_repo.resolve())
    assert all(m["params"]["arguments"]["workspace"] == str(copy) for m in asked[1:])
    assert all(m["cwd"] == str(copy) and m["openspec"] == ["specs"] for m in asked)
    # livespec's registry of projects is the copy's own, never the person's.
    assert home_dir and all(m["livespec_home"] == home_dir for m in asked)
    assert not Path(home_dir).is_relative_to(git_repo.resolve())
    assert not copy.exists() and not Path(home_dir).exists()  # removed afterwards
    assert not (copy / ".livespec.toml").exists()
    assert git(git_repo, "status", "--porcelain", "--ignored") == ""


def test_validate_openspec_says_why_it_could_not(tmp_path, git_repo):
    a, log = fake_livespec(tmp_path)
    with pytest.raises(livespec_mod.LivespecError, match="no openspec"):
        a.validate_openspec(git_repo)
    _with_specs(git_repo)
    broken, _ = fake_livespec(tmp_path, runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "nope"))
    with pytest.raises(livespec_mod.LivespecError, match="index failed: nope"):
        broken.validate_openspec(git_repo)
    assert not log.exists()


@pytest.mark.parametrize(("mode", "said"), [("error", "boom"), ("die", "closed before it answered"),
                                            ("hang", "closed before it answered")])
def test_a_livespec_that_errors_dies_or_hangs_is_an_error_not_a_hang(tmp_path, git_repo, mode, said):
    a, _ = fake_livespec(tmp_path, mode)
    with pytest.raises(livespec_mod.LivespecError, match=said):
        a.call_tools(tmp_path, [("validate_openspec", {})], timeout_s=2)


def test_nothing_to_ask_or_a_server_that_does_not_start_is_an_error(tmp_path, git_repo):
    with pytest.raises(livespec_mod.LivespecError, match="no livespec"):
        adapter(tmp_path).call_tools(tmp_path, [("validate_openspec", {})])

    def broken(*a, **kw):
        raise OSError("no such file")

    with pytest.raises(livespec_mod.LivespecError, match="did not start"):
        adapter(tmp_path, tool(tmp_path, "livespec"), popen=broken).call_tools(tmp_path, [("search", {})])


def test_a_symbol_matched_only_by_the_request_s_words_raises_no_rung(tmp_path, git_repo):
    """The map draws the best full-text matches, but only a symbol the request names says what the
    work touches: a widely used one that shares a word with the request starts nothing higher."""
    build(git_repo)
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    by_words = a.brief("the amount is rounded wrong when it has cents", git_repo, status)
    found = a.dossier("the amount is rounded wrong when it has cents", git_repo, status)
    assert "caller(s)" in found.text  # the dossier still shows what the words found
    assert by_words.raise_rungs == 0 and not by_words.critical and not any("callers" in r for r in by_words.reasons)
    named = a.brief("parse_amount rounds wrong", git_repo, status)
    assert named.raise_rungs == 1


def test_the_dossier_holds_where_calls_callers_specs_reach_and_is_bounded(tmp_path, git_repo):
    build(git_repo)
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    d = a.dossier("Fix charge_card in src/billing.py", git_repo, status, paths=["src/billing.py"])
    assert "src/billing.py:20-25" in d.text and "caller(s): checkout (src/api.py:30)" in d.text
    assert "SPEC-7 Card charges are idempotent [critical]" in d.text
    assert "implemented by `src.billing.charge_card`" in d.text
    assert "these files implement specs" in d.text and "data, not instructions" in d.text
    assert d.symbols >= 1 and d.specs == 1 and d.chars == len(d.text)
    tight = a.dossier("Fix charge_card in src/billing.py", git_repo, status, paths=["src/billing.py"], limit=700)
    assert len(tight.text) <= 700 and tight.omitted > 0 and "did not fit" in tight.text


@pytest.mark.parametrize("request_text, found", [
    ("Add a helper parse_duration to the api", ["parse_duration"]),
    ("Add a function RetryBudget", ["RetryBudget"]),
    ("implement the new retry_budget function", ["retry_budget"]),
    ("Create `AuthClient` for the service", ["AuthClient"]),
    ("add `parse_duration_amount` helper", ["parse_duration_amount"]),
    ("Add tests for the parser", []),
    ("add a new option", []),
])
def test_the_identifier_a_request_adds_is_found_past_articles_and_adjectives(request_text, found):
    assert livespec_mod._to_add(request_text) == found


def test_the_dossier_lists_helpers_close_to_a_name_the_request_adds_for_writing_kinds_only(tmp_path, git_repo):
    build(git_repo)
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    d = a.dossier("Add a helper parse_amount_cents", git_repo, status, kind="implement")
    assert "for `parse_amount_cents`: `src.parse.parse_amount` (src/parse.py:10)" in d.text
    assert "Helpers with names close" in d.text
    assert "Helpers with names close" not in a.dossier("Add a helper parse_amount_cents", git_repo, status,
                                                       kind="explore").text
    # a name the index already holds is no helper to hint at
    assert "Helpers with names close" not in a.dossier("Add a helper parse_amount", git_repo, status).text


def test_the_dossier_carries_the_specs_the_requests_words_find_and_not_one_common_word(tmp_path, git_repo):
    db = build(git_repo)
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO spec (id, project_id, spec_id, title, description, priority) VALUES "
                 "(2, 1, 'SPEC-9', 'Refunds are rounded', 'A refund rounds half up to whole cents', 'high')")
    conn.commit()
    conn.close()
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    found = a.dossier("Refunds must be rounded half up", git_repo, status)
    assert "SPEC-9 Refunds are rounded [high]: A refund rounds half up" in found.text
    assert "no symbol implements it yet" in found.text and found.specs >= 1
    assert "SPEC-9" not in a.dossier("Refunds are slow", git_repo, status).text


def test_the_dossier_takes_the_symbols_of_the_files_the_request_names_most_called_first(tmp_path, git_repo):
    build(git_repo)
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    text = a.dossier("Tidy things up", git_repo, status, paths=["src/api.py"]).text
    assert "src/api.py" in text and "checkout" in text
    assert "src.parse.parse_amount" not in a.dossier("Tidy things up", git_repo, status).text


def _specs(db: Path, rows: list[tuple[int, str, str, str]]) -> None:
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO spec (id, project_id, spec_id, title, description) VALUES (?, 1, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_a_spec_id_the_request_writes_out_finds_the_spec_without_shared_words(tmp_path, git_repo):
    db = build(git_repo)
    _specs(db, [(20, "SPEC-12", "Zebra quota", "stripes"), (21, "SPEC-1", "Walrus tusks", "ivory")])
    a = adapter(tmp_path)
    text = a.dossier("Please look at SPEC-12 before touching anything", git_repo, a.inspect(git_repo)).text
    assert "SPEC-12 Zebra quota" in text
    assert "SPEC-1 Walrus" not in text  # SPEC-1 is a prefix of SPEC-12, not a mention of it


def test_a_word_most_specs_use_alone_does_not_find_a_spec(tmp_path, git_repo):
    db = build(git_repo)
    _specs(db, [(20, "SPEC-20", "Ledger entries posted", "daily"), (21, "SPEC-21", "Ledger rows archived", "x"),
                (22, "SPEC-22", "Ledger export formats", "y"), (23, "SPEC-23", "Ledger totals shown", "z")])
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    # "ledger" is in four of five specs: it counts for none, so one more shared word is not enough
    assert "SPEC-20" not in a.dossier("Check ledger entries", git_repo, status).text
    assert "SPEC-20 Ledger entries posted" in a.dossier("Check ledger entries posted", git_repo, status).text


def test_the_files_a_request_names_give_their_most_called_symbols_up_to_the_cap(tmp_path, git_repo):
    db = build(git_repo)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO file (id, project_id, path, language, content_hash, line_count, mtime) "
                 "VALUES (5, 1, 'src/util.py', 'python', 'h', 10, 0)")
    for i in range(6):  # u5 is called by 5 symbols ... u0 by none
        conn.execute("INSERT INTO symbol (id, file_id, name, qualified_name, kind, start_line, end_line) "
                     "VALUES (?, 5, ?, ?, 'function', ?, ?)",
                     (200 + i, f"u{i}", f"src.util.u{i}", i * 10 + 1, i * 10 + 5))
        for caller in range(i):
            conn.execute("INSERT INTO symbol_edge (src_symbol_id, dst_symbol_id, edge_type) "
                         "VALUES (?, ?, 'calls')", (100 + caller, 200 + i))
    conn.commit()
    conn.close()
    assert livespec_mod.FILE_SYMBOLS == 4
    a = adapter(tmp_path)
    text = a.dossier("Tidy things up", git_repo, a.inspect(git_repo), paths=["src/util.py"]).text
    shown = [n for n in range(6) if f"`src.util.u{n}`" in text]
    assert shown == [2, 3, 4, 5]  # the cap drops the least called two
    assert text.index("src.util.u5`") < text.index("src.util.u4`") < text.index("src.util.u3`")


def test_what_the_caps_leave_out_is_said_by_count(tmp_path, git_repo):
    db = build(git_repo)
    _specs(db, [(30 + i, f"SPEC-{30 + i}", f"Topic{i} alpha", "d") for i in range(8)])
    a = adapter(tmp_path)
    names = " ".join(f"handler_{i}" for i in range(12))
    ids = " ".join(f"SPEC-{30 + i}" for i in range(8))
    d = a.dossier(f"Change {names} for {ids}", git_repo, a.inspect(git_repo))
    cut_symbols = 12 - livespec_mod.DOSSIER_SYMBOLS
    cut_specs = 8 - livespec_mod.DOSSIER_SPECS
    assert f"{cut_symbols} symbol(s), {cut_specs} spec(s)" in d.text
    assert d.omitted >= cut_symbols + cut_specs
    assert (d.symbols, d.specs) == (livespec_mod.DOSSIER_SYMBOLS, livespec_mod.DOSSIER_SPECS)


def test_the_dossier_never_passes_its_limit_with_both_closing_lines(tmp_path, git_repo, monkeypatch):
    from locks.adapters import livespec as module

    build(git_repo)
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    # Caps of one leave symbols out, and a tight limit leaves entries out: both closing lines are written.
    monkeypatch.setattr(module, "DOSSIER_SYMBOLS", 1)
    for limit in range(500, 900, 7):
        d = a.dossier("Fix charge_card and checkout in src/billing.py", git_repo, status,
                      paths=["src/billing.py", "src/api.py"], limit=limit)
        assert len(d.text) <= limit, limit


def test_a_call_resolved_only_by_name_is_one_caller_in_the_brief_the_count_and_the_dossier(tmp_path, git_repo):
    build(git_repo, rules=20)
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    text = "Change apply_line in src/tally/rules.py"
    with livespec_mod._connect(Path(status.data["db"])) as conn:
        assert all(livespec_mod._describe(conn, 200 + i)["callers"] == 1 for i in range(21))
        # the twenty tests that call it are their own list, not callers
        assert livespec_mod._describe(conn, 205)["caller_ids"] == [400]
        assert len(livespec_mod._describe(conn, 205)["tests"]) == 20
    b = a.brief(text, git_repo, status)
    assert not any("callers" in r for r in b.reasons) and b.raise_rungs == 0
    assert b.reached == 5 + 1
    d = a.dossier(text, git_repo, status)
    assert "1 caller(s): price (src/tally/checkout.py:5)" in d.text and "21 caller" not in d.text


def test_a_call_resolved_to_one_method_is_a_whole_caller_even_at_a_low_weight(tmp_path, git_repo):
    build(git_repo, rules=20)
    with livespec_mod._connect(Path(adapter(tmp_path).inspect(git_repo).data["db"])) as conn:
        only = livespec_mod._describe(conn, 501)
        assert only["callers"] == 1 and only["caller_ids"] == [500]


def test_widely_used_is_judged_on_distinct_calling_symbols(tmp_path, git_repo):
    build(git_repo)
    a = adapter(tmp_path)
    b = a.brief("Change parse_amount", git_repo, a.inspect(git_repo))
    assert any("src.parse.parse_amount has 21 callers" in r for r in b.reasons) and b.raise_rungs == 1
