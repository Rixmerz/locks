from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from locks import legacy, plugins, workflows
from locks.store import Store

#: The profile where every adaptive setting is on (`LOCKS_TEST_ALL_ON=1`): read once, before any
#: test sets the switches below in this process's environment.
ALL_ON = (os.environ.get("LOCKS_TEST_ALL_ON")
          or os.environ.get("CAUCE_TEST_ALL_ON")) == "1"  # legacy-name: the profile's old variable still works
FAKE_CLAUDE = Path(__file__).parent / "scenarios" / "fake_claude.py"
#: The settings a test turns on itself when it wants them: each would call a model, start a
#: process or a second worker, read a record, or change what a worker is told.
SWITCHES = (
    "LOCKS_PARALLEL", "LOCKS_AUTOWORK", "LOCKS_NAMES", "LOCKS_NOTES", "LOCKS_VERIFIER", "LOCKS_TRUST",
    "LOCKS_PROBATION", "LOCKS_EVIDENCE", "LOCKS_NOTE_TRIALS", "LOCKS_METHODS", "LOCKS_RECALL_FUSION",
    "LOCKS_RACE", "LOCKS_ROUTE", "LOCKS_PRIMER", "LOCKS_TRUNK", "LOCKS_OVERLAP", "LOCKS_LATE_VERIFIER",
    "LOCKS_LEAN", "LOCKS_ROLE_TRIALS", "LOCKS_HABIT_REVIEW", "LOCKS_HABIT_AUTO", "LOCKS_ENGRAM",
    "LOCKS_ENGRAM_WRITE", "LOCKS_TRACE", "LOCKS_NOTES_FORK", "LOCKS_AUTOSPLIT", "LOCKS_LIVESPEC_TRIAL", "LOCKS_PREDICT",
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
                   monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own locks home. Never bypass this: a test writing to
    the real database would put fake dead ends in front of real sessions."""
    home = tmp_path / "locks-home"
    # What the shell running the suite set under the old name (a session of an old install, one
    # of its workers) would be read again by every process a test starts (`legacy.adopt_env`).
    for name in [n for n in os.environ if n.startswith(legacy.OLD_ENV)]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("LOCKS_HOME", str(home))
    monkeypatch.delenv("LOCKS_HOOKS_OFF", raising=False)
    # livespec is on by default in real use. Here it is off unless a test turns
    # it on with a fake: a real `uvx livespec index` in a unit test is minutes
    # of network and indexing nobody asked for.
    monkeypatch.setenv("LOCKS_LIVESPEC", "off")
    # And a task never waits on an index before its worker: a test that wants the wait sets it.
    monkeypatch.setenv("LOCKS_LIVESPEC_WAIT", "0")
    # A browser neighbour is handed as if the machine had Chromium: a test that wants the check sets it.
    monkeypatch.setenv("LOCKS_BROWSER_CHECK", "off")
    # Every adaptive setting is off: a test that wants one turns it on with a fake. Under
    # `LOCKS_TEST_ALL_ON=1` they stay as a person gets them (on, as their defaults say), with the
    # fake `claude` first on PATH: the profile where features meet, as they do in real use.
    if ALL_ON:
        for name in SWITCHES:
            monkeypatch.delenv(name, raising=False)
        # Beside the test's tree, never in it: a test that walks `tmp_path` as a project
        # would find the fake `claude`, its script or its log among the project's files.
        fake = tmp_path_factory.mktemp("fake-claude")
        fake_bin = fake / "bin"
        fake_bin.mkdir()
        (fake_bin / "claude").write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_CLAUDE} \"$@\"\n")
        (fake_bin / "claude").chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(fake / "script.json"))
        monkeypatch.setenv("FAKE_CLAUDE_LOG", str(fake / "calls.jsonl"))
    else:
        for name in SWITCHES:
            monkeypatch.setenv(name, "off")
    monkeypatch.delenv("LOCKS_SESSION_ID", raising=False)
    # locks' own suite may run inside a locks worker, which refuses to start work.
    monkeypatch.delenv("LOCKS_WORKER_TASK", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)  # a resume in a session shell runs shielded
    monkeypatch.delenv("LOCKS_SHIELDED", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    # A neighbour the person put on PATH (rastro in ~/.local/bin) is theirs, as their plugin cache is:
    # read through the real environment it is on no PATH. A test that wants one passes its own env.
    from locks import plugins

    on_disk = plugins.find
    monkeypatch.setattr(plugins, "find", lambda neighbour, env: on_disk(
        neighbour, {**env, "PATH": ""} if env is os.environ else env))
    # The dispatcher's level starts at what the machine holds: in a test, a fixed machine
    # of three CPUs whose memory, disk and load say nothing (calm), whatever runs the suite.
    from locks import capacity

    monkeypatch.setattr(capacity, "machine",
                        lambda root=None: capacity.Machine(cpus=3, memory_mb=None, disk_mb=None, load=None))
    monkeypatch.delenv("LOCKS_MAX_PARALLEL", raising=False)
    for var, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
    }.items():
        monkeypatch.setenv(var, value)
    return home


@pytest.fixture(autouse=True)
def _no_host_neighbours(monkeypatch: pytest.MonkeyPatch) -> None:
    """The programs locks adopts when they are on the PATH (`plugins.KNOWN`: `rastro`) are not
    found because the machine happens to have them: a test that wants one puts its own on a PATH
    it sets. Without this, `capabilities` and a plan's warnings change with what is installed
    where the suite runs (three tests failed on a machine with `rastro` in `~/.local/bin`)."""
    real = shutil.which
    hidden = {n.on_path[0] for n in plugins.KNOWN if n.on_path}
    host = {str(Path(entry)) for entry in os.environ.get("PATH", "").split(os.pathsep) if entry}

    def which(cmd, *args, **kwargs):
        found = real(cmd, *args, **kwargs)
        return None if found and cmd in hidden and str(Path(found).parent) in host else found

    monkeypatch.setattr(shutil, "which", which)


@pytest.fixture
def store(_isolated_home: Path) -> Store:
    s = Store(_isolated_home / "locks.db")
    yield s
    s.close()


@pytest.fixture
def kicks(monkeypatch):
    """No real dispatcher: each start the engine asks for is recorded."""
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(workflows, "_kick", lambda cwd, key: seen.append((cwd, key)))
    # `locks queue add` and a run's ending start one through `dispatch.start`: with autowork on
    # (`LOCKS_TEST_ALL_ON=1`) a real dispatcher would take the queue the test is reading.
    from locks import dispatch

    monkeypatch.setattr(dispatch, "start", lambda repo_dir, root, scope, **kw: False)
    return seen


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("print('hi')\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def no_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """For a test that drives a run with a fixed list of worker results and is about something
    else: the independent check (and its forms, beside the check and after the ending) is one
    more worker per pass, which the list does not script."""
    monkeypatch.setenv("LOCKS_VERIFIER", "off")
