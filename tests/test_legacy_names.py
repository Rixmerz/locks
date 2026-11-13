"""The old name, cauce: everything an install from before the rename left still works.

This file is on `scripts/rename_to_locks.py`'s allow-list: it writes what an old install left
(its environment, its home and database, a project's `.cauce/`, kept `cauce/task-N` branches,
`cauce-result` blocks, a neighbour's `cauce` block) and checks Locks reads all of it.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from locks import compact, compaction, habits, isolate, launch, legacy, link, manifests, project, store, teams
from locks.store import Store
from locks.ui import server

from .conftest import git

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FAKE = Path(__file__).parent / "scenarios" / "fake_claude.py"


def _env_without_new(**extra: str) -> dict[str, str]:
    """This process's environment with every `LOCKS_*` taken away: what an old install's
    shell holds, before anything of the new name was ever set."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LOCKS_", "CAUCE_"))}
    return {**env, "PYTHONPATH": str(SRC), **extra}


# --- 1. the environment ------------------------------------------------------------------

def test_an_old_variable_reads_as_its_new_name_and_a_new_one_wins():
    env = {"CAUCE_HOME": "/old", "CAUCE_VERIFIER": "off", "LOCKS_VERIFIER": "on"}
    legacy.adopt_env(env)
    assert env["LOCKS_HOME"] == "/old" and env["LOCKS_VERIFIER"] == "on" and env["CAUCE_HOME"] == "/old"
    assert legacy.getenv({"CAUCE_HOME": "/x"}, "LOCKS_HOME") == "/x"
    assert legacy.getenv({"CAUCE_HOME": "/x", "LOCKS_HOME": "/y"}, "LOCKS_HOME") == "/y"
    assert legacy.getenv({}, "LOCKS_HOME", "d") == "d" and legacy.getenv({}, "OTHER") == ""


def test_a_child_gets_both_names_and_loses_both_when_one_is_taken_away():
    env = {"LOCKS_HOOKS_OFF": "1", "LOCKS_WORKER_TASK": "4", "CAUCE_SESSION_ID": "s", "PATH": "/bin"}
    legacy.mirror(env)
    assert env == {"LOCKS_HOOKS_OFF": "1", "LOCKS_WORKER_TASK": "4", "CAUCE_HOOKS_OFF": "1",
                   "CAUCE_WORKER_TASK": "4", "PATH": "/bin"}
    from locks import classify, dispatch

    assert classify._quiet_env()["CAUCE_HOOKS_OFF"] == "1"
    os.environ["CAUCE_WORKER_TASK"] = "9"
    try:
        child = dispatch.child_env()
    finally:
        del os.environ["CAUCE_WORKER_TASK"]
    assert "LOCKS_WORKER_TASK" not in child and "CAUCE_WORKER_TASK" not in child


def test_a_process_started_with_only_old_variables_reads_them(tmp_path):
    code = "import locks.store as s, os; print(s.home()); print(os.environ.get('LOCKS_VERIFIER'))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env=_env_without_new(CAUCE_HOME=str(tmp_path / "h"), CAUCE_VERIFIER="off")).stdout.split()
    assert out == [str(tmp_path / "h"), "off"]


def test_the_test_profile_s_old_variable_still_turns_every_setting_on():
    code = "import tests.conftest as c; print(c.ALL_ON)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT,
                         env=_env_without_new(CAUCE_TEST_ALL_ON="1", PYTHONPATH=f"{SRC}{os.pathsep}{ROOT}")).stdout
    assert out.strip() == "True"


# --- 2. the data home ---------------------------------------------------------------------

def _old_home(base: Path, *, task: str = "an old task") -> Path:
    old = base / "cauce"
    old.mkdir(parents=True)
    conn = sqlite3.connect(str(old / "cauce.db"))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE kept (what TEXT)")
    conn.execute("INSERT INTO kept VALUES (?)", (task,))
    conn.commit()
    conn.close()
    (old / "config.json").write_text('{"verifier": "off"}')
    return old


def test_the_old_home_moves_once_with_a_link_left_and_its_database_renamed(tmp_path):
    old = _old_home(tmp_path)
    new = store.home({"XDG_DATA_HOME": str(tmp_path)})
    assert new == tmp_path / "locks" and new.is_dir() and old.is_symlink() and old.resolve() == new.resolve()
    assert (new / "locks.db").is_file() and (new / "cauce.db").is_symlink()
    assert (new / "config.json").read_text() == '{"verifier": "off"}'
    conn = sqlite3.connect(str(legacy.database(new)))
    assert conn.execute("SELECT what FROM kept").fetchall() == [("an old task",)]
    conn.close()
    # An old process opening its old path reaches the same file.
    conn = sqlite3.connect(str(old / "cauce.db"))
    assert conn.execute("SELECT what FROM kept").fetchall() == [("an old task",)]
    conn.close()
    assert store.home({"XDG_DATA_HOME": str(tmp_path)}) == new  # once


def test_a_database_another_process_holds_stays_where_it_is_and_is_read_there(tmp_path):
    old = _old_home(tmp_path)
    holder = sqlite3.connect(str(old / "cauce.db"))
    holder.execute("SELECT * FROM kept").fetchall()
    try:
        new = legacy.home(tmp_path)
        assert not (new / "locks.db").exists() and legacy.database(new) == new / "cauce.db"
    finally:
        holder.close()
    assert legacy.database(new) == new / "locks.db"  # renamed the first time nothing holds it
    assert legacy.rename_database(new) is False  # nothing left to rename
    plain = tmp_path / "plain"
    plain.mkdir()
    sqlite3.connect(str(plain / "cauce.db")).execute("CREATE TABLE t (a)").connection.close()
    assert legacy.rename_database(plain) is False  # a rollback journal proves nothing: left as it is
    (plain / "broken").mkdir()
    (plain / "broken" / "cauce.db").write_text("not a database")
    assert legacy.rename_database(plain / "broken") is False


def test_a_home_that_cannot_move_is_used_where_it_is_and_said_once(tmp_path, monkeypatch, capsys):
    old = _old_home(tmp_path)

    def refuse(src, dst):
        raise PermissionError("read-only")

    monkeypatch.setattr(legacy.os, "rename", refuse)
    assert legacy.home(tmp_path) == old and "kept the old home" in capsys.readouterr().err
    assert legacy.home(tmp_path) == old and capsys.readouterr().err == ""
    assert legacy.database(old) == old / "cauce.db"


def test_mcp_guard_and_the_tool_log_find_the_moved_home(tmp_path, monkeypatch):
    from locks import mcp_guard, signature

    _old_home(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    env = {"XDG_DATA_HOME": str(tmp_path)}
    assert mcp_guard.locks_home(env) == tmp_path / "locks"
    assert signature.log_dir(env) == tmp_path / "locks" / "tool-events"
    assert mcp_guard.locks_home({"CAUCE_HOME": "/old/home"}) == Path("/old/home")


# --- 3. a project's folder ------------------------------------------------------------------

def test_an_old_project_folder_is_read_where_it_is_and_renamed_when_work_is_written(git_repo):
    (git_repo / ".cauce").mkdir()
    (git_repo / ".cauce" / ".gitignore").write_text("# cauce's own state for this project; never committed.\n*\n")
    (git_repo / ".cauce" / "sessions.json").write_text('{"s1": {"name": "mine", "haiku_name": "h"}}')
    (git_repo / ".gitignore").write_text("node_modules/\n.cauce/\n")
    assert project.find(git_repo) == git_repo / ".cauce" and project.name_of(git_repo, "s1")["name"] == "mine"
    assert project.own(git_repo) == git_repo / ".cauce"
    folder = project.enroll(git_repo)
    assert folder == git_repo / ".locks" and not (git_repo / ".cauce").exists()
    assert project.name_of(git_repo, "s1")["name"] == "mine"
    assert (git_repo / ".gitignore").read_text() == "node_modules/\n.locks/\n"
    assert "locks' own state" in (folder / ".gitignore").read_text()
    assert legacy.rename_project_folder(git_repo) is None  # never two folders


def test_an_old_folder_with_no_ignore_file_of_its_own_is_renamed_and_other_lines_left(git_repo):
    (git_repo / ".cauce").mkdir()
    (git_repo / ".gitignore").write_text("dist/\n")
    assert legacy.rename_project_folder(git_repo) == git_repo / ".locks"
    assert (git_repo / ".gitignore").read_text() == "dist/\n" and not (git_repo / ".locks" / ".gitignore").exists()


def test_a_home_another_process_moved_first_is_taken_as_it_is(tmp_path, monkeypatch):
    import contextlib

    old = _old_home(tmp_path)

    @contextlib.contextmanager
    def raced(path):
        (tmp_path / "locks").mkdir()  # the other process finished while this one waited
        yield

    monkeypatch.setattr(legacy, "_flock", raced)
    assert legacy.home(tmp_path) == tmp_path / "locks" and old.is_dir() and not old.is_symlink()


def test_the_rename_is_said_once_per_home_and_only_to_the_old_command(tmp_path, monkeypatch):
    assert legacy.say_renamed(tmp_path / "h", {"LOCKS_INVOKED_AS": "cauce"}).startswith("cauce is now Locks")
    assert legacy.say_renamed(tmp_path / "h", {"LOCKS_INVOKED_AS": "cauce"}) is None
    monkeypatch.setattr(legacy.sys, "argv", ["/usr/local/bin/cauce", "tasks"])
    assert legacy.say_renamed(tmp_path / "h2", {}) is not None
    monkeypatch.setattr(legacy.sys, "argv", ["/usr/local/bin/locks", "tasks"])
    assert legacy.say_renamed(tmp_path / "h3") is None


def test_both_folders_never_merge_by_guess_the_new_one_wins(git_repo):
    (git_repo / ".cauce").mkdir()
    (git_repo / ".locks").mkdir()
    assert project.find(git_repo) == git_repo / ".locks" and legacy.rename_project_folder(git_repo) is None


def test_a_folder_that_cannot_be_renamed_is_kept_and_used(git_repo, monkeypatch):
    (git_repo / ".cauce").mkdir()
    monkeypatch.setattr(legacy.os, "rename", lambda a, b: (_ for _ in ()).throw(OSError("busy")))
    assert project.enroll(git_repo) == git_repo / ".cauce"


# --- 4. kept branches and refs ----------------------------------------------------------

def _old_branch(repo: Path, n: int, name: str = "hi.txt") -> str:
    git(repo, "checkout", "-q", "-b", f"cauce/task-{n}")
    (repo / name).write_text("old work\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "-m", "old work",
        "-m", "Cauce-Task: #1\nCauce-Verified: no (blocked)")
    tip = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return tip


def test_an_old_task_s_branch_and_aside_refs_take_the_new_name_with_their_tips(git_repo):
    tip = _old_branch(git_repo, 1)
    git(git_repo, "update-ref", "refs/cauce/aside/task-1-1", tip)
    git(git_repo, "branch", "cauce/foreign/task-2-abc", tip)
    git(git_repo, "branch", "locks/task-3", tip)
    git(git_repo, "branch", "cauce/task-3", tip)  # the new name is taken: left as it is
    git(git_repo, "branch", "locks/task-5/x", tip)
    git(git_repo, "branch", "cauce/task-5", tip)  # the new name cannot be made: left as it is, nothing lost
    isolate._ADOPTED.clear()
    assert isolate.kept(git_repo, 1)
    assert git(git_repo, "rev-parse", "locks/task-1") == tip
    assert git(git_repo, "rev-parse", "refs/locks/aside/task-1-1") == tip
    assert git(git_repo, "rev-parse", "locks/foreign/task-2-abc") == tip
    refs = git(git_repo, "for-each-ref", "--format=%(refname)")
    assert "refs/heads/cauce/task-1" not in refs and "refs/cauce/aside" not in refs
    assert "refs/heads/cauce/task-3" in refs and "refs/heads/cauce/task-5" in refs
    assert isolate.adopt_legacy(git_repo) == []  # once per checkout and process
    assert isolate._unverified(git(git_repo, "log", "-1", "--format=%B", "locks/task-1"))


def test_a_branch_checked_out_in_an_old_worktree_is_renamed_and_the_worktree_found(git_repo, tmp_path):
    _old_branch(git_repo, 1)
    old_home = tmp_path / "share" / "cauce"
    (old_home / "worktrees").mkdir(parents=True)
    git(git_repo, "worktree", "add", "-q", str(old_home / "worktrees" / "task-1"), "cauce/task-1")
    (old_home / "worktrees" / "task-1" / "left.txt").write_text("not committed\n")
    new_home = legacy.home(tmp_path / "share")
    isolate._ADOPTED.clear()
    ws = isolate.prepare(git_repo, 1, new_home)
    assert ws.branch == "locks/task-1" and (ws.path / "left.txt").read_text() == "not committed\n"
    assert (ws.path / "hi.txt").read_text() == "old work\n"


def test_a_remote_copy_under_the_old_name_holds_no_branch_as_merged(git_repo):
    tip = _old_branch(git_repo, 1)
    isolate._ADOPTED.clear()
    isolate.adopt_legacy(git_repo)
    git(git_repo, "update-ref", "refs/remotes/origin/cauce/task-1", tip)
    assert isolate.prunable(git_repo, "locks/task-1") is None
    assert legacy.old_refs("refs/heads/cauce/task-1\nrefs/heads/locks/task-1\nrefs/heads/main\n") == []


def test_a_checkout_git_cannot_read_adopts_nothing(tmp_path, monkeypatch):
    assert isolate.adopt_legacy(tmp_path) == []
    monkeypatch.setattr(isolate, "_git", lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert isolate.adopt_legacy(tmp_path / "x") == []


def test_a_survey_counts_branches_kept_under_either_name(git_repo):
    from locks import survey

    _old_branch(git_repo, 1)
    assert survey.branches(git_repo)["locks_kept"]["count"] == 1


# --- 5. protocol blocks and trailers ----------------------------------------------------

def test_an_old_result_block_and_report_block_are_still_read():
    text = ('Done.\n```cauce-result\n{"verdict": "pass", "summary": "s", "evidence": "1 passed"}\n```\n'
            '```cauce-report\n{"flows": 3}\n```\n')
    assert launch.result_block(text)["verdict"] == "pass"
    assert launch.report_block(text) == {"flows": 3}


def test_an_old_compaction_marker_is_still_its_own():
    assert compaction.MARKER_RE.search("/compact [cauce-compaction #7] keep it").group(1) == "7"
    assert compact.NOT_TYPED_RE.match("/compact [cauce-compaction #3] something")


# --- 6. a neighbour's manifest ----------------------------------------------------------

BLOCK = {"format": 1, "tools": {"detect_issues": "finds overlaps"}, "hint": "run detect_issues"}


def _install(plugin: str, version: str, manifest: dict, beside: dict | None = None,
             beside_name: str = "cauce.json") -> Path:
    root = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "plugins" / "cache" / "rixmerz" / plugin / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": plugin, **manifest}))
    if beside is not None:
        (root / ".claude-plugin" / beside_name).write_text(json.dumps(beside))
    return root


def test_a_neighbour_s_old_block_is_read_and_a_new_one_comes_first():
    manifests._CACHE.clear()
    _install("layout-inspector", "1.0.0", {"cauce": BLOCK})
    assert manifests.read("layout-inspector").hint == "run detect_issues"
    manifests._CACHE.clear()
    _install("layout-inspector", "1.1.0", {"cauce": {**BLOCK, "hint": "old detect_issues"},
                                           "locks": {**BLOCK, "hint": "new detect_issues"}})
    assert manifests.read("layout-inspector").hint == "new detect_issues"
    manifests._CACHE.clear()
    _install("layout-inspector", "1.2.0", {}, beside={**BLOCK, "hint": "file detect_issues"})
    assert manifests.read("layout-inspector").hint == "file detect_issues"
    manifests._CACHE.clear()
    _install("layout-inspector", "1.3.0", {}, beside={**BLOCK, "hint": "newer file detect_issues"},
             beside_name="locks.json")
    (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "plugins/cache/rixmerz/layout-inspector/1.3.0/.claude-plugin/cauce.json"
     ).write_text(json.dumps({**BLOCK, "hint": "old file detect_issues"}))
    assert manifests.read("layout-inspector").hint == "newer file detect_issues"


# --- 7. engram ------------------------------------------------------------------------------

def test_engram_writes_under_the_new_key_and_reads_whatever_wrote_it(tmp_path):
    from .test_engram import Fake, engram

    hits = [{"id": 3, "type": "learning", "title": "Cents everywhere", "content": "Saved by cauce.",
             "state": "active", "tool_name": "cauce"}]
    fake = Fake(**{"GET /search": (200, hits)})
    adapter = engram(fake)
    status = adapter.inspect(tmp_path)
    assert "Cents everywhere" in "\n".join(adapter.brief("cents prices everywhere", tmp_path, status).lines)
    adapter.remember(["Prices are kept in cents."], project="shop", task_id=5, title="t", directory=tmp_path)
    saved = fake.posted("/observations")[0]
    assert saved["tool_name"] == "locks"
    assert saved["operation_id"] == hashlib.sha256(b"locks|shop|5|Prices are kept in cents.").hexdigest()[:32]
    assert fake.posted("/sessions")[0]["id"] == "locks-task-5"


# --- 8. plugin installs and the old command --------------------------------------------

def _plugin(config: Path, name: str, version: str) -> Path:
    root = config / "plugins" / "cache" / "rixmerz" / name / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name, "version": version}))
    (root / "bin").mkdir()
    (root / "bin" / name).write_text("#!/bin/sh\n")
    (root / "bin" / name).chmod(0o755)
    return root


def test_an_old_install_is_named_once_and_never_forwarded_to(tmp_path):
    from locks import __version__, hooks

    config = Path(os.environ["CLAUDE_CONFIG_DIR"])
    old = _plugin(config, "cauce", "0.67.0")
    env = {"CLAUDE_CONFIG_DIR": str(config)}
    assert [p.parents[1] for _, p in link.installed(env, old=True)] == [old]
    assert link.installed(env) == []
    said = hooks._older_installs(tmp_path, env)
    assert "cauce (0.67.0)" in said and "claude plugin uninstall cauce@rixmerz" in said and str(old) in said
    assert hooks._older_installs(tmp_path, env) is None  # once per set
    _plugin(config, "locks", "0.1.0")
    (tmp_path / "work" / hooks.OLDER_SAID).unlink()
    said = hooks._older_installs(tmp_path, env)
    assert "older locks installs remain" in said and __version__ in said
    own = str(config / "plugins" / "cache" / "rixmerz" / "locks" / "0.0.1" / "src")
    assert link.forward_to(["tasks"], env, own, "0.0.1") is not None  # a locks install, never the old name's


def test_the_old_link_shim_is_rewritten_to_run_locks_and_nothing_else_is(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "cauce"
    shim.write_text("#!/usr/bin/env bash\n# Written by `cauce link`. It runs the newest cauce\nexec x\n")
    assert link.write_old(bin_dir, ROOT / "bin" / "locks") == shim
    text = shim.read_text()
    assert "plugins/cache/*/locks/*/bin/locks" in text and "LOCKS_INVOKED_AS=cauce exec" in text
    assert link.write_old(bin_dir, ROOT / "bin" / "locks") is None  # ours now, not the old command's
    shim.unlink()
    shim.write_text("#!/bin/sh\necho mine\n")
    assert link.write_old(bin_dir, ROOT / "bin" / "locks") is None and "mine" in shim.read_text()
    assert link.write_old(tmp_path / "none", ROOT / "bin" / "locks") is None


def test_the_cauce_command_runs_locks_and_says_so_once(tmp_path):
    env = _env_without_new(LOCKS_HOME=str(tmp_path / "h"))
    first = subprocess.run([str(ROOT / "bin" / "cauce"), "matrix"], capture_output=True, text=True, env=env)
    second = subprocess.run([str(ROOT / "bin" / "cauce"), "matrix"], capture_output=True, text=True, env=env)
    assert first.returncode == 0 and second.returncode == 0 and first.stdout == second.stdout
    assert "cauce is now Locks" in first.stderr and "cauce is now Locks" not in second.stderr
    plain = subprocess.run([str(ROOT / "bin" / "locks"), "matrix"], capture_output=True, text=True,
                           env=_env_without_new(LOCKS_HOME=str(tmp_path / "h2")))
    assert "cauce is now Locks" not in plain.stderr
    assert legacy.say_renamed(tmp_path / "h3", {"LOCKS_INVOKED_AS": "locks"}) is None


def test_the_old_python_variable_still_picks_the_interpreter(tmp_path):
    env = _env_without_new(LOCKS_HOME=str(tmp_path / "h"), CAUCE_PYTHON="no-such-python")
    proc = subprocess.run([str(ROOT / "bin" / "locks"), "matrix"], capture_output=True, text=True, env=env)
    assert proc.returncode == 127 and "no-such-python" in proc.stderr


def test_the_console_script_keeps_the_old_name():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'locks = "locks.cli:main"' in text and 'cauce = "locks.cli:main"' in text


def test_the_old_command_is_said_once_in_the_session(tmp_path, monkeypatch):
    from locks import hooks

    calls = []
    monkeypatch.setattr(link, "write_old", lambda *a: calls.append(a))
    hooks._keep_old_command(tmp_path)
    monkeypatch.setattr(link, "write_old", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    hooks._keep_old_command(tmp_path)  # fails open
    assert len(calls) == 1 and (tmp_path / hooks.ERROR_LOG).exists()


# --- 9. the UI --------------------------------------------------------------------------

@pytest.fixture
def ui(_isolated_home):
    srv = server.serve(0, _isolated_home)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.stop_event.set()
    srv.shutdown()
    srv.server_close()


def test_an_open_page_of_the_old_name_still_writes_with_its_header(ui, _isolated_home):
    from .test_ui import call, token

    key = token(ui)
    assert call(ui, "POST", "/api/lanes/unpause", {"repo": "r"}, headers={"X-Cauce-Token": key})[0].status == 200
    assert call(ui, "POST", "/api/lanes/unpause", {"repo": "r"}, headers={"X-Cauce-Token": "no"})[0].status == 403


def test_an_older_ui_of_the_old_name_is_found_on_its_port(monkeypatch):
    class Done:
        def __init__(self, out):
            self.stdout = out

    answers = iter([Done("4242\n"), Done("python3 -m cauce ui --port 8765\n")])
    assert server.listener(8765, run=lambda *a, **k: next(answers)) == 4242


# --- 10. the store's contents -------------------------------------------------------------

def _downgrade(db: Path) -> None:
    """Write back what an old install wrote: its `source`, its stops' `by`, its branches."""
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE tasks SET source = 'cauce' WHERE source = 'locks'")
    conn.execute("UPDATE events SET data = replace(replace(replace(data, '\"locks/', '\"cauce/'), "
                 "'\"by\": \"locks\"', '\"by\": \"cauce\"'), '\"checked\": \"locks\"', '\"checked\": \"cauce\"')")
    conn.execute("DELETE FROM meta WHERE key = ?", (legacy.UPGRADED,))
    conn.commit()
    conn.close()


def test_rows_written_under_the_old_name_read_as_the_new_one(tmp_path):
    path = tmp_path / "s.db"
    s = Store(path)
    task = s.create_task("fix it", status="blocked", source="locks", repo="r", cwd=str(tmp_path))
    s.add_event(task["id"], "finished", status="blocked", branch="locks/task-1", checked="locks",
                stop={"cause": "review", "by": "locks", "reason": "src/cauce/x.py stays"})
    s.close()
    _downgrade(path)
    s = Store(path)
    try:
        assert s.get_task(task["id"])["source"] == "locks"
        data = s.final_event(task["id"])["data"]
        assert data["branch"] == "locks/task-1" and data["checked"] == "locks" and data["stop"]["by"] == "locks"
        assert data["stop"]["reason"] == "src/cauce/x.py stays"  # a word inside text is never rewritten
        assert s.meta(legacy.UPGRADED) == "1"
        # An old version still running writes again: read on the next open.
        s.create_task("from the old one", status="done", source="cauce")
    finally:
        s.close()
    s = Store(path)
    try:
        assert {t["source"] for t in s.list_tasks()} == {"locks"}
    finally:
        s.close()
    s = Store(path)
    try:
        assert legacy.upgrade_store(s._conn) == 0  # nothing left to read
    finally:
        s.close()


def test_an_upgrade_that_fails_changes_nothing(tmp_path):
    path = tmp_path / "s.db"
    Store(path).close()
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("DELETE FROM meta")
    conn.execute("INSERT INTO tasks (title, status, source, created_at, updated_at) "
                 "VALUES ('t', 'done', 'cauce', 'x', 'x')")

    class Failing:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *args):
            if sql.startswith("UPDATE events"):
                raise sqlite3.OperationalError("disk I/O error")
            return self.inner.execute(sql, *args)

    with pytest.raises(sqlite3.OperationalError):
        legacy.upgrade_store(Failing(conn))
    assert conn.execute("SELECT source FROM tasks").fetchone()[0] == "cauce"
    conn.close()


def test_the_bundled_team_answers_to_its_old_name():
    assert teams.get("cauce").name == teams.BUNDLED
    with pytest.raises(teams.TeamError, match="the team locks ships"):
        teams.create("cauce", ["implementer"])
    teams.use("r", "cauce")
    assert "r" not in teams.assignments()


def test_a_habit_installed_before_the_rename_keeps_its_variable(tmp_path):
    assert not habits._SHELL.search('ruff check "$CAUCE_EDITED"')
    assert not habits._SHELL.search('ruff check "$LOCKS_EDITED"')
    assert habits._SHELL.search('ruff check "$HOME"')
    assert habits.OLD_EDITED_VAR == "CAUCE_EDITED"


# --- the whole of it: an old install, then Locks ----------------------------------------------

def _run(env: dict[str, str], cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "locks", *args], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=120)


def test_an_old_install_s_task_its_branch_and_its_folder_carry_over_to_locks(git_repo, tmp_path):
    """A home, a task, its kept branch and its project folder as an old install left them, and
    only old variables set: Locks finds the task, its resume starts from the branch, nothing
    is lost."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    (bin_dir / "claude").chmod(0o755)
    xdg = tmp_path / "xdg"
    script = tmp_path / "script.json"
    script.write_text(json.dumps([{"match": "greeting", "do": [
        {"pass": True, "write": {"hi.txt": "hi\n"}, "block": {"verdict": "fail", "failure": "environment",
                                                              "summary": "the database it needs is down"}},
        {"pass": True, "write": {"bye.txt": "bye\n"}}]}]))
    env = _env_without_new(
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", XDG_DATA_HOME=str(xdg), HOME=str(tmp_path / "home"),
        CLAUDE_CONFIG_DIR=str(tmp_path / "claude-config"), FAKE_CLAUDE_SCRIPT=str(script),
        FAKE_CLAUDE_LOG=str(tmp_path / "calls.jsonl"), **{f"CAUCE_{k}": "off" for k in (
            "LIVESPEC", "VERIFIER", "TRUST", "PROBATION", "EVIDENCE", "NOTES", "NAMES", "AUTOWORK", "PARALLEL",
            "RACE", "ROUTE", "PRIMER", "TRUNK", "OVERLAP", "LATE_VERIFIER", "ROLE_TRIALS", "NOTE_TRIALS",
            "METHODS", "RECALL_FUSION", "HABIT_REVIEW", "HABIT_AUTO", "ENGRAM", "ENGRAM_WRITE", "LEAN")},
        CAUCE_LIVESPEC_WAIT="0")
    # The old install ran a task that stopped with its work kept.
    first = _run(env, git_repo, "run", "--kind", "implement", "make the greeting file for the app")
    assert "#1" in first.stdout + first.stderr, first.stdout + first.stderr
    home = xdg / "locks"
    db = home / "locks.db"
    assert db.is_file() and git(git_repo, "rev-parse", "--verify", "locks/task-1")
    # Now make it the old install's: its home, its database, its branch, its folder, its rows.
    _downgrade(db)
    (home / "locks.db").rename(home / "cauce.db")
    home.rename(xdg / "cauce")
    git(git_repo, "branch", "-m", "locks/task-1", "cauce/task-1")
    (git_repo / ".locks").rename(git_repo / ".cauce")
    worktrees = git(git_repo, "worktree", "list", "--porcelain")
    assert "locks/task-1" not in worktrees  # the work was kept on the branch, its worktree gone

    listed = _run(env, git_repo, "tasks")
    assert listed.returncode == 0 and "#1" in listed.stdout, listed.stdout + listed.stderr
    # The store is renamed the first time nothing holds it: the first run's detached ending may still have
    # it open (CI saw that once), and a later command renames it then.
    deadline = time.monotonic() + 10
    while not (xdg / "locks" / "locks.db").is_file() and time.monotonic() < deadline:
        time.sleep(0.2)
        listed = _run(env, git_repo, "tasks")
    assert (xdg / "cauce").is_symlink() and (xdg / "locks" / "locks.db").is_file()

    resumed = _run(env, git_repo, "resume", "1", "--unattended")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert git(git_repo, "show", "locks/task-1:hi.txt") == "hi"  # the old work, kept
    assert git(git_repo, "show", "locks/task-1:bye.txt") == "bye"
    assert "cauce/task-1" not in git(git_repo, "branch", "--list")
    assert (git_repo / ".locks").is_dir() and not (git_repo / ".cauce").exists()
    s = Store(xdg / "locks" / "locks.db")
    try:
        assert s.get_task(1)["status"] == "done" and s.get_task(1)["source"] == "locks"
    finally:
        s.close()


def test_link_rewrites_the_old_command_s_shim_beside_its_own(tmp_path, monkeypatch, capsys):
    from locks import cli

    target = tmp_path / "bin"
    target.mkdir()
    (target / "cauce").write_text("#!/usr/bin/env bash\n# Written by `cauce link`.\nexec x\n")
    assert cli.main(["link", "--dir", str(target)]) == 0
    out = capsys.readouterr().out
    assert f"rewrote {target / 'cauce'}" in out and (target / "locks").is_file()
    monkeypatch.setenv("LOCKS_INVOKED_AS", "cauce")
    assert cli.main(["matrix"]) == 0
    assert "cauce is now Locks" in capsys.readouterr().err and "LOCKS_INVOKED_AS" not in os.environ


def test_a_shim_that_cannot_be_rewritten_is_left(tmp_path, monkeypatch):
    target = tmp_path / "bin"
    target.mkdir()
    shim = target / "cauce"
    shim.write_text("# Written by `cauce link`.\n")
    monkeypatch.setattr(Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(PermissionError("ro")))
    assert link.write_old(target, ROOT / "bin" / "locks") is None
