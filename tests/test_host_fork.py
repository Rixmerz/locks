"""A task sent with `--fork-session` forks the session that sent it (`locks.host_fork`)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from locks import host_fork, launch
from locks.matrix import Cell
from locks.store import Store


def _transcript(config: Path, session: str, *, tokens: int = 1000, model: str = "claude-sonnet-4-5",
                where: str = "-home-person-repo") -> Path:
    folder = config / "projects" / where
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session}.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": "fix that one"}}) + "\n"
                    + json.dumps({"type": "assistant", "message": {"id": "m1", "model": model,
                                                                   "usage": {"input_tokens": tokens}}}) + "\n")
    return path


def test_the_source_is_the_sending_session_or_why_there_is_none():
    assert host_fork.source({"LOCKS_SESSION_ID": "abc-1"}) == {"session": "abc-1"}
    assert host_fork.source({"CLAUDE_CODE_SESSION_ID": "x_2"}) == {"session": "x_2"}
    assert "no session sent it" in host_fork.source({})["missing"]
    assert "missing" in host_fork.source({"LOCKS_SESSION_ID": "../etc"})


def test_prepare_forks_a_readable_transcript_under_its_bound_only(tmp_path: Path):
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    _transcript(tmp_path, "s1")
    ready, why = host_fork.prepare({"session": "s1"}, worktree=True, env=env)
    assert why == "" and ready["model"] == "claude-sonnet-4-5" and ready["tokens"] == 1000
    assert host_fork.same_model(ready, "sonnet") and not host_fork.same_model(ready, "opus")
    assert "no worktree" in host_fork.prepare({"session": "s1"}, worktree=False, env=env)[1]
    assert "not where Claude Code keeps it" in host_fork.prepare({"session": "nope"}, worktree=True, env=env)[1]
    assert host_fork.prepare({"missing": "no session"}, worktree=True, env=env) == (None, "no session")
    assert host_fork.prepare(None, worktree=True, env=env) == (None, "")
    _transcript(tmp_path, "big", tokens=host_fork.MAX_TOKENS + 1)
    assert "over" in host_fork.prepare({"session": "big"}, worktree=True, env=env)[1]
    assert host_fork.context_of(tmp_path / "missing.jsonl") == (None, "")


def test_place_copies_the_transcript_where_resume_in_the_worktree_finds_it(tmp_path: Path):
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}
    source = _transcript(tmp_path / "cfg", "s1")
    worktree = tmp_path / "wt.5"
    worktree.mkdir()
    placed = host_fork.place(source, "s1", worktree, env)
    folder = host_fork.project_folder(worktree.resolve(), env)
    assert placed == folder / "s1.jsonl" and placed.read_text() == source.read_text()
    assert "." not in folder.name and "/" not in folder.name
    assert host_fork.place(source, "s1", worktree, env) is None  # already there: left alone, never removed


def test_launch_places_the_copy_for_the_attempt_and_removes_it_after(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    source = _transcript(cfg, "host-1")
    seen = {}

    def runner(argv, **kw):
        seen["argv"], seen["input"] = argv, kw["input"]
        seen["there"] = (host_fork.project_folder(Path(kw["cwd"]).resolve()) / "host-1.jsonl").exists()
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    spec = launch.LaunchSpec(prompt="the brief", cell=Cell("sonnet", "high"), launch_dir=tmp_path,
                             resume_session="host-1", fork_session=True, session_copy=str(source),
                             resume_prompt="locks: a new worker", resume_note="the brief")
    launch.run(spec, runner=runner)
    assert seen["there"] and "--fork-session" in seen["argv"] and "host-1" in seen["argv"]
    assert seen["input"].startswith("locks: a new worker\n\nthe brief")
    assert not (host_fork.project_folder(tmp_path.resolve()) / "host-1.jsonl").exists()
    assert source.exists()


def test_the_trial_alternates_and_reads_back_the_arm_a_brief_drew(store: Store):
    task = store.create_task("t", status="done", source="locks", repo="r", cwd="/tmp")
    store.add_event(task["id"], "briefed", **{host_fork.KEY: "fork", host_fork.WHY_KEY: "the host fork trial"})
    store.add_attempt(task["id"], cell="sonnet/high", max_turns=10, passed=1)
    found = host_fork.trial(store, "r")
    assert found["fork"]["tasks"] == 1 and found["fork"]["passed"] == 1 and found["fresh"]["tasks"] == 0
    arm, why = host_fork.arm(store, "r", 99)
    assert arm in host_fork.ARMS and "host fork" in why
    assert host_fork.arm(store, None, 1)[0] == "fork"


def test_the_note_gives_the_role_the_hints_and_the_directory(tmp_path: Path):
    said = host_fork.note(role="You implement.", capabilities="Use x.", workdir=tmp_path)
    assert "You implement." in said and "Use x." in said and str(tmp_path) in said
    assert "send no work to locks" in said
    assert host_fork.session_cost({"path": str(tmp_path / "none.jsonl")}) == 0.0


def test_the_cli_keeps_the_sending_session_on_a_queued_task(git_repo, store, monkeypatch):
    from locks import cli

    monkeypatch.setenv("LOCKS_SESSION_ID", "host-9")
    monkeypatch.chdir(git_repo)
    assert cli.main(["queue", "add", "--fork-session", "--whole", "apply this to the export command"]) == 0
    task = Store.open().queued_everywhere()[-1]
    options = json.loads(task["options"]) if isinstance(task["options"], str) else task["options"]
    assert options["host_fork"] == {"session": "host-9"}
    from locks import flow
    from locks.orchestrate import Options

    assert flow.queued_options(task, Options()).host_fork == {"session": "host-9"}


@pytest.mark.usefixtures("no_verifier")
def test_orchestrate_forks_the_host_on_the_first_try_only(git_repo, store, monkeypatch, tmp_path):
    from locks import orchestrate
    from locks.orchestrate import Options, run

    from .test_cover_engine import _orch

    monkeypatch.setenv("LOCKS_HOST_FORK", "on")
    _transcript(Path(os.environ["CLAUDE_CONFIG_DIR"]), "host-1", model="claude-sonnet-4-5")
    monkeypatch.setattr(host_fork, "arm", lambda store, repo, task_id: ("fork", "the fork arm's turn"))
    t = _orch()
    said = []
    script = t.Script(t.ok(), write="feature.py")
    report = run("apply this to the export command", git_repo, store,
                 Options(host_fork={"session": "host-1"}, start=Cell("sonnet", "high")), registry={},
                 launcher=script, classifier=t.kind("implement"), progress=said.append)
    assert report.status == "done"
    spec = script.specs[0]
    assert spec.resume_session == "host-1" and spec.fork_session and spec.session_copy.endswith("host-1.jsonl")
    assert spec.resume_prompt.startswith("locks: everything above is the conversation")
    assert spec.resume_note == spec.prompt
    assert store.last_event(report.task_id, "briefed")["data"][host_fork.KEY] == "fork"
    assert store.last_event(report.task_id, "attempt_started")["data"]["forked_host"] == "host-1"
    assert any("forks the session that sent it" in s for s in said)
    assert orchestrate.Options(host_fork={"session": "h"}).stored()["host_fork"] == {"session": "h"}


@pytest.mark.usefixtures("no_verifier")
def test_orchestrate_another_model_or_no_session_starts_fresh_and_says_why(git_repo, store, monkeypatch):
    from locks.orchestrate import Options, run

    from .test_cover_engine import _orch

    monkeypatch.setenv("LOCKS_HOST_FORK", "on")
    _transcript(Path(os.environ["CLAUDE_CONFIG_DIR"]), "host-2", model="claude-opus-4-5")
    monkeypatch.setattr(host_fork, "arm", lambda store, repo, task_id: ("fork", "the fork arm's turn"))
    t = _orch()
    said = []
    script = t.Script(t.ok(), write="feature.py")
    report = run("apply this to the export command", git_repo, store,
                 Options(host_fork={"session": "host-2"}, start=Cell("sonnet", "high")), registry={},
                 launcher=script, classifier=t.kind("implement"), progress=said.append)
    assert report.status == "done" and not script.specs[0].resume_session
    assert any("that session ran on claude-opus-4-5" in s for s in said)
    said.clear()
    script = t.Script(t.ok(), write="other.py")
    report = run("apply this to the import command", git_repo, store,
                 Options(host_fork={"missing": "no session sent it"}), registry={},
                 launcher=script, classifier=t.kind("implement"), progress=said.append)
    assert not script.specs[0].resume_session
    assert "no session sent it" in store.last_event(report.task_id, "briefed")["data"]["host_fork_why"]
