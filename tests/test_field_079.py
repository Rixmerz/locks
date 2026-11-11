"""0.79.0: a resume a session's shell started outlives that shell, idle worktrees give back the
disk their rebuilt folders hold, and `locks stats` sets livespec's tools offered beside called."""
from __future__ import annotations

import argparse
import subprocess
import sys

from locks import cli, flow, isolate
from locks.store import Store, home


def _ignored_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".gitignore").write_text("target/\nnode_modules/\n")
    for name in ("target", "node_modules"):
        (path / name).mkdir()
        (path / name / "blob").write_bytes(b"x" * 4096)
    (path / "build").mkdir()  # not ignored: git may keep it, so it stays
    (path / "build" / "kept").write_text("kept")


def test_shed_frees_rebuilt_folders_of_idle_worktrees_only(store: Store):
    idle = store.create_task("old", status="blocked", source="locks", repo="r", kind="implement")
    busy = store.create_task("now", status="running", source="locks", repo="r", kind="implement")
    for task in (idle, busy):
        path = home() / "worktrees" / f"task-{task['id']}"
        path.mkdir(parents=True)
        _ignored_repo(path)
    linked = home() / "worktrees" / f"task-{idle['id']}" / ".venv"
    linked.symlink_to(home())  # a link to the checkout's folder is never followed
    freed = flow.shed(store)
    gone = home() / "worktrees" / f"task-{idle['id']}"
    assert freed >= 8192 and not (gone / "target").exists() and not (gone / "node_modules").exists()
    assert (gone / "build" / "kept").exists() and linked.is_symlink()
    assert (home() / "worktrees" / f"task-{busy['id']}" / "target").exists()
    said = [e for e in store.events_of_kinds(["shed"]) if e["task_id"] == idle["id"]]
    assert said and sorted(said[0]["data"]["folders"]) == ["node_modules", "target"]
    assert isolate.shed(home(), lambda t: t == idle["id"]) == (0, [])  # nothing left to free


def test_a_resume_in_a_session_shell_runs_in_its_own_process_group(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert cli._under_a_session_shell()
    monkeypatch.setenv(cli.SHIELDED, "1")
    assert not cli._under_a_session_shell()
    monkeypatch.delenv(cli.SHIELDED)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert not cli._under_a_session_shell()  # a person's terminal: as before


def test_a_shielded_resume_follows_its_run_and_returns_its_exit(monkeypatch, capsys):
    started = {}

    class Child:
        pid, returncode, polls = 4242, 3, 0

        def __init__(self, argv, **kw):
            started.update(argv=argv, **kw)
            kw["stdout"].write("the report\n")
            kw["stdout"].flush()

        def poll(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", Child)
    args = argparse.Namespace(id=7, verify=None, start=None, max_turns=None, allow=["Bash(npm *)"],
                              surface=["src/a"], allow_approval=False, no_isolate=False, no_model=False,
                              unattended=True)
    assert cli._shielded(args) == 3
    assert started["start_new_session"] and started["env"][cli.SHIELDED] == "1"
    assert "--no-report" not in started["argv"] and started["argv"][-3:-1] == ["--surface", "src/a"]
    assert "the report" in capsys.readouterr().out
