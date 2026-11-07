from __future__ import annotations

import errno
import json
import os

import pytest

from locks import cli, dispatch, orchestrate, stops
from locks.launch import WorkerResult
from locks.store import Store


def test_matrix_and_classify(capsys):
    assert cli.main(["matrix"]) == 0
    assert "debug-unclear    opus/high → opus/xhigh → opus/max" in capsys.readouterr().out
    assert cli.main(["classify", "haz commit y push", "--no-model"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "docs"


def test_route_is_a_dry_run(capsys, git_repo):
    assert cli.main(["route", "where is the router defined?", "--repo", str(git_repo), "--no-model"]) == 0
    out = capsys.readouterr().out
    assert "plan: dry run" in out and "kind explore" in out and "start haiku" in out


def test_run_reports_and_fails_on_a_non_pass(capsys, git_repo, monkeypatch):
    def fake_run(text, repo_dir, store, options, **kw):
        kw["launcher"] = lambda spec: WorkerResult(False, None, "nope")
        kw["registry"] = {}
        return real(text, repo_dir, store, options, **kw)

    real = orchestrate.run
    monkeypatch.setattr(orchestrate, "run", fake_run)
    code = cli.main(["run", "fix it", "--repo", str(git_repo), "--no-model", "--kind", "docs",
                     "--max-attempts", "1", "--start", "haiku"])
    out, err = capsys.readouterr()
    assert code == 1 and "task #1: failed" in out
    # progress goes to stderr as it happens, so stdout stays the report alone
    assert err.splitlines()[:2] == ["locks: task #1 started: docs, at haiku/medium",
                                    "locks: #1 attempt 1 at haiku/medium"]
    assert "locks: task #1 started" not in out


def test_tasks_show_and_memory(capsys, git_repo):
    store = Store.open()
    t = store.create_task("a task", status="done", source="locks", repo="github.com/o/r", kind="docs")
    store.add_attempt(t["id"], cell="haiku", max_turns=30, passed=0, failure="code_bug", summary="missed")
    store.set_move(t["id"], 1, "more_effort", "shallow")
    store.close()
    assert cli.main(["tasks", "--all"]) == 0
    assert "a task" in capsys.readouterr().out
    assert cli.main(["show", str(t["id"])]) == 0
    out = capsys.readouterr().out
    assert "attempt 1 haiku" in out and "more_effort" in out and "missed" in out
    assert cli.main(["show", "999"]) == 1
    store = Store.open()
    blocked = store.create_task("routes", status="running", source="locks", repo="github.com/o/r", cwd=str(git_repo))
    store.add_attempt(blocked["id"], cell="sonnet/medium", max_turns=30, passed=0, failure="permission")
    store.add_event(blocked["id"], "attempt_finished", seq=1, denied=["Bash(node a.js)"])
    stops.record(store, blocked["id"], stops.Stop("permission", "the worker was refused Bash(node a.js)",
                                                  denied=("Bash(node a.js)",)))
    store.close()
    assert cli.main(["show", str(blocked["id"])]) == 0
    out = capsys.readouterr().out
    assert "refused: Bash(node a.js)" in out
    assert "stopped by your permission settings: the worker was refused" in out
    assert f"locks resume {blocked['id']} --allow 'Bash(node:*)'" in out
    store = Store.open()
    store.set_move(blocked["id"], 1, "more_effort", "code_bug: same model, more thorough")
    store.add_event(blocked["id"], "moved", seq=1, from_cell="sonnet/medium", to_cell="sonnet/high", axis="effort",
                    because=["attempt 1 ended code_bug", "next: sonnet/high"], skipped=["sonnet/xhigh: why"])
    store.close()
    assert cli.main(["show", str(blocked["id"])]) == 0
    out = capsys.readouterr().out
    assert "effort: sonnet/medium → sonnet/high" in out and "· next: sonnet/high" in out
    assert "· skipped sonnet/xhigh: why" in out

    assert cli.main(["memory", "record", "--problem", "slow startup", "--fix", "lazy import pandas",
                     "--outcome", "failed", "--why", "still slow", "--repo", str(git_repo)]) == 0
    assert cli.main(["memory", "search", "slow startup", "--repo", str(git_repo)]) == 0
    out = capsys.readouterr().out
    assert "lazy import pandas" in out and "still slow" in out
    assert cli.main(["memory", "search", "nothing like it"]) == 0
    assert "nothing recorded" in capsys.readouterr().out


def test_show_prints_what_the_independent_check_found_under_its_attempt(capsys):
    store = Store.open()
    t = store.create_task("build it", status="blocked", source="locks", repo="r", kind="implement")
    for seq in (1, 2):
        store.add_attempt(t["id"], cell="sonnet/high", max_turns=30, passed=1, summary=f"built it {seq}")
    blocker = {"where": "src/x.py:12", "what": "the total is rounded twice", "evidence_class": "deterministic",
               "cause": "introduced", "proof": "pytest -k total\nFAILED test_total"}
    nearby = {"where": "src/x.py:80", "what": "a stale docstring", "evidence_class": "inferential",
              "cause": "pre-existing", "proof": ""}
    old = {"where": "src/y.py:3", "what": "an unused import " + "long " * 80, "evidence_class": "inferential",
           "cause": "pre-existing", "proof": ""}
    store.add_event(t["id"], "verified", seq=1, cell="opus/high", verdict="fail", passed=False,
                    findings=[blocker, nearby, old], blockers=[blocker], blocking=1)
    store.add_event(t["id"], "verified", seq=2, cell="opus/high", verdict="pass", passed=True, findings=[],
                    blockers=[], blocking=0)
    store.close()
    assert cli.main(["show", str(t["id"])]) == 0
    lines = capsys.readouterr().out.splitlines()
    first = lines.index("    built it 1")
    assert lines[first + 1:first + 5] == [
        "    independent check at opus/high: fail, 1 blocker(s)",
        "      blocks: [introduced, deterministic] src/x.py:12: the total is rounded twice — pytest -k total "
        "FAILED test_total",
        # Another finding in a blocker's file is its own finding, not the blocker again.
        "      found: [pre-existing, inferential] src/x.py:80: a stale docstring",
        lines[first + 4]]
    # A finding that is not a blocker is printed once, as found, and clipped like the summary.
    assert lines[first + 4].startswith("      found: [pre-existing, inferential] src/y.py:3: an unused import")
    # At a word, marked: never mid-word.
    assert len(lines[first + 4]) <= 4 + 300 and lines[first + 4].endswith("long…")
    assert sum("the total is rounded twice" in line for line in lines) == 1
    second = lines.index("    built it 2")
    assert lines[second + 1] == "    independent check at opus/high: passed"


def test_config_model_shows_pins_and_what_served(capsys):
    store = Store.open()
    store.add_usage(message_id="m1", session_id="s", model="claude-sonnet-5-5", output_tokens=1, ts="2099-01-01")
    t = store.create_task("x", status="done", source="locks")
    store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=1, served_model="claude-sonnet-5")
    store.close()
    assert cli.main(["config", "model"]) == 0
    out = capsys.readouterr().out
    assert "sonnet  follows Claude Code; workers last ran claude-sonnet-5; your sessions use claude-sonnet-5-5" in out
    assert "opus    follows Claude Code" in out
    assert cli.main(["config", "model", "sonnet", "claude-sonnet-5-5"]) == 0
    assert capsys.readouterr().out.startswith("sonnet  pinned to claude-sonnet-5-5; workers last ran")
    assert cli.main(["config", "model", "sonnet", "default"]) == 0
    assert "follows Claude Code" in capsys.readouterr().out
    assert cli.main(["config", "model", "gpt"]) == 1
    assert cli.main(["config", "model", "opus", "two words"]) == 1
    assert cli.main(["show", str(t["id"])]) == 0
    assert "attempt 1 sonnet/medium [claude-sonnet-5]" in capsys.readouterr().out


def test_init_refuses_the_home_directory(capsys, tmp_path, monkeypatch):
    from locks import project

    monkeypatch.setattr(project.Path, "home", classmethod(lambda cls: tmp_path))
    assert cli.main(["init", str(tmp_path)]) == 1
    assert "is not a project" in capsys.readouterr().err and not (tmp_path / ".locks").exists()


def test_allow_keeps_rules_for_the_repository_and_config_mode_sets_modes(capsys, git_repo):
    assert cli.main(["allow", "--repo", str(git_repo)]) == 0
    assert "nothing beyond their mode" in capsys.readouterr().out
    assert cli.main(["allow", "Bash(make:*)", "--preset", "node", "--repo", str(git_repo)]) == 0
    assert "Bash(npm:*), Bash(npx:*), Bash(node:*), Bash(make:*)" in capsys.readouterr().out
    assert cli.main(["allow", "--rm", "Bash(make:*)", "--repo", str(git_repo)]) == 0
    assert "Bash(make:*)" not in capsys.readouterr().out
    assert cli.main(["allow", "--preset", "nope", "--repo", str(git_repo)]) == 1
    plain = git_repo.parent / "plain"
    plain.mkdir()
    assert cli.main(["allow", "--repo", str(plain)]) in (0, 1)
    capsys.readouterr()

    assert cli.main(["config", "mode"]) == 0
    out = capsys.readouterr().out
    assert "haiku   bypassPermissions" in out and "sonnet  auto" in out
    assert cli.main(["config", "mode", "haiku", "acceptEdits"]) == 0
    assert capsys.readouterr().out.strip() == "haiku   acceptEdits"
    assert cli.main(["config", "mode", "default", "dontAsk"]) == 0
    assert "opus    dontAsk" in capsys.readouterr().out
    assert cli.main(["config", "mode", "haiku", "reset"]) == 0
    assert "bypassPermissions" in capsys.readouterr().out
    assert cli.main(["config", "mode", "gpt"]) == 1
    assert cli.main(["config", "mode", "opus", "plan"]) == 1


def test_capabilities(capsys, _isolated_home):
    assert cli.main(["capabilities"]) == 0
    assert "no capabilities registered" in capsys.readouterr().out
    assert cli.main(["capabilities", "--example"]) == 0
    example = capsys.readouterr().out
    _isolated_home.mkdir(parents=True, exist_ok=True)
    (_isolated_home / "capabilities.json").write_text(example)
    assert cli.main(["capabilities"]) == 0
    out = capsys.readouterr().out
    assert "livespec" in out and "after a failed implement" in out


def test_config_and_neighbours(capsys, git_repo):
    assert cli.main(["config"]) == 0
    assert "livespec = true" in capsys.readouterr().out
    assert cli.main(["config", "livespec", "off"]) == 0
    assert cli.main(["config", "livespec"]) == 0
    assert capsys.readouterr().out.strip().endswith("false")
    assert cli.main(["config", "nope"]) == 1
    assert cli.main(["config", "livespec", "maybe"]) == 1
    assert cli.main(["neighbours", "--repo", str(git_repo)]) == 0
    out = capsys.readouterr().out
    assert "livespec setting: off" in out and "livespec: absent" in out


def test_delegate_is_on_until_the_person_turns_it_off_and_the_mod_reads_it_as_a_word(capsys):
    # the mod reads `locks config delegate` and lets queue and resume through only on `true`
    assert cli.main(["config", "delegate"]) == 0 and capsys.readouterr().out == "true\n"
    assert cli.main(["config", "delegate", "off"]) == 0
    capsys.readouterr()
    assert cli.main(["config", "delegate"]) == 0 and capsys.readouterr().out == "false\n"


def test_cancel_signals_the_run_and_events_print(capsys, monkeypatch):
    import signal

    store = Store.open()
    running = store.create_task("long job", status="running", source="locks", pid=4242)
    queued = store.create_task("later", status="queued", source="locks")
    done = store.create_task("old", status="done", source="locks")
    store.add_event(running["id"], "planned", kind="implement")
    store.close()
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert cli.main(["cancel", str(running["id"])]) == 0
    assert killed == [(4242, signal.SIGTERM)]
    assert cli.main(["cancel", str(queued["id"])]) == 0
    assert cli.main(["cancel", str(done["id"])]) == 0
    assert "nothing to cancel" in capsys.readouterr().out
    assert cli.main(["cancel", "999"]) == 1
    store = Store.open()
    assert store.get_task(queued["id"])["status"] == "cancelled"
    assert "with `locks cancel`" in store.last_event(queued["id"], "finished")["data"]["stop"]["reason"]
    assert store.last_event(running["id"], "cancel_requested")["data"] == {"via": "cli"}
    store.close()

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(cli.os, "kill", gone)
    store = Store.open()
    again = store.create_task("x", status="running", source="locks", pid=1)
    store.close()
    assert cli.main(["cancel", str(again["id"])]) == 0

    assert cli.main(["events"]) == 0
    assert '"kind": "implement"' in capsys.readouterr().out
    assert cli.main(["events", "--json", "--task", str(running["id"])]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["kind"] == "planned"
    assert cli.main(["events", "--json", "--kind", "cancel_requested"]) == 0
    assert {json.loads(x)["kind"] for x in capsys.readouterr().out.splitlines()} == {"cancel_requested"}
    assert cli.main(["events", "--new"]) == 0
    assert capsys.readouterr().out == ""
    # past the first page: every event, not the first 200
    store = Store.open()
    for i in range(250):
        store.add_event(again["id"], "noise", i=i)
    store.add_event(again["id"], "branch_pruned", branch="locks/task-1")
    store.close()
    assert cli.main(["events", "--kind", "branch_pruned"]) == 0
    assert "locks/task-1" in capsys.readouterr().out


def test_sigterm_becomes_a_cancel():
    import signal

    import pytest

    previous = cli._cancel_on_sigterm()
    try:
        with pytest.raises(orchestrate.Cancelled):
            signal.raise_signal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_queue_lanes_and_work(capsys, git_repo, monkeypatch):
    # With autowork, `queue add` starts a real dispatcher, which would hold the lock `work` asks for.
    monkeypatch.setenv("LOCKS_AUTOWORK", "off")
    assert cli.main(["queue", "add", "write docs", "--repo", str(git_repo), "--verify", "true",
                     "--kind", "docs"]) == 0
    assert "queued #1" in capsys.readouterr().out
    assert cli.main(["queue", "list", "--repo", str(git_repo)]) == 0
    assert "write docs" in capsys.readouterr().out
    assert cli.main(["queue"]) == 0
    assert cli.main(["queue", "add", "second", "--repo", str(git_repo)]) == 0
    assert cli.main(["queue", "rm", "2"]) == 0
    assert cli.main(["queue", "rm", "2"]) == 1
    store = Store.open()
    assert store.last_event(2, "finished")["data"]["stop"]["by"] == "you"
    store.close()

    seen = {}

    def fake_work(store, **kw):
        seen.update(kw)
        from locks.flow import WorkReport
        return WorkReport([], "nothing runnable is queued")

    monkeypatch.setattr(cli.flow, "work", fake_work)
    assert cli.main(["work", "--repo", str(git_repo), "--max", "3"]) == 0
    assert seen["max_tasks"] == 3 and seen["repo"] == str(git_repo.resolve())
    assert cli.main(["work", "--all"]) == 0 and seen["repo"] is None

    store = Store.open()
    store.pause_lane(str(git_repo.resolve()), "task #1 ended failed")
    store.close()
    capsys.readouterr()
    assert cli.main(["lanes"]) == 0
    assert "paused: task #1 ended failed" in capsys.readouterr().out
    assert cli.main(["lanes", "--unpause", str(git_repo)]) == 0
    assert cli.main(["lanes", "--unpause", "github.com/o/r"]) == 0
    assert cli.main(["lanes"]) == 0
    assert "open" in capsys.readouterr().out


def test_spend_and_invalidate(capsys):
    store = Store.open()
    t = store.create_task("x", status="done", source="locks", repo="r", kind="docs")
    store.add_attempt(t["id"], cell="haiku", max_turns=30, passed=1, cost_usd=0.1)
    p = store.open_problem("p", repo="r")
    fix = store.add_fix(p, "f", "worked", repo="r")
    store.close()
    assert cli.main(["spend", "--days", "3"]) == 0
    assert "haiku" in capsys.readouterr().out
    assert cli.main(["spend", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["workers"][0]["cell"] == "haiku"
    assert cli.main(["memory", "invalidate", str(fix), "--why", "came back"]) == 0
    assert "recurring" in capsys.readouterr().out
    assert cli.main(["memory", "invalidate", "999", "--why", "x"]) == 1
    assert cli.main(["memory", "record", "--problem", "q", "--fix", "g", "--outcome", "worked",
                     "--commit", "abc"]) == 0


def test_habits_commands(capsys, tmp_path, monkeypatch):
    store = Store.open()
    for s in range(3):
        for sig in ("edit:.py", "bash:ruff-format"):
            store.add_tool_event(session_id=f"s{s}", tool="x", sig=sig, arg_hash="h", ok=1)
    store.close()
    assert cli.main(["habits", "list", "--all"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("every repository:")
    cid = out.splitlines()[1].split()[0]
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    assert cli.main(["habits", "install", cid, "--command", "true", "--repo", str(repo_dir)]) == 0
    assert "installed habit #1" in capsys.readouterr().out
    assert cli.main(["habits", "install", cid, "--command", "true", "--repo", str(repo_dir)]) == 0
    assert "already installed here as habit #1" in capsys.readouterr().out
    assert cli.main(["habits", "install", "nope", "--command", "true", "--repo", str(repo_dir)]) == 1
    assert cli.main(["habits", "status"]) == 0
    assert "edit:.py → bash:ruff-format" in capsys.readouterr().out
    assert cli.main(["habits", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["repo"] == str(repo_dir)
    assert cli.main(["habits", "list", "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["here"][0]["steps"] == ["edit:.py", "bash:ruff-format"]
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({"tool_input": {"file_path": "a.py"}})))
    assert cli.main(["habit-run", "1"]) == 0
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("garbage"))
    assert cli.main(["habit-run", "1"]) == 0
    assert cli.main(["habits", "install", cid, "--command", "echo lint broke; exit 1", "--repo", str(repo_dir)]) == 0
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({"tool_input": {"file_path": "a.py"}})))
    assert cli.main(["habit-run", "2"]) == 2  # a failed check is said to the session
    assert "lint broke" in capsys.readouterr().err
    assert cli.main(["habits", "uninstall", "1"]) == 0
    assert cli.main(["habits", "uninstall", "1"]) == 0  # already removed from settings: still fine
    assert cli.main(["habits", "uninstall", "99"]) == 1
    (repo_dir / ".claude" / "settings.local.json").write_text("{broken")
    assert cli.main(["habits", "install", cid, "--command", "true", "--repo", str(repo_dir)]) == 1
    store = Store.open()
    store._conn.execute("DELETE FROM tool_events")
    store.close()
    assert cli.main(["habits", "list", "--days", "1"]) == 0


def test_board_counts_for_a_status_line(capsys):
    store = Store.open()
    store.create_task("x", status="failed", source="locks")  # the orchestrator decides it
    refused = store.create_task("z", status="blocked", source="locks")  # a refused command: the person's
    stops.record(store, refused["id"], stops.Stop("permission", "refused", denied=("Bash(npm ci)",)))
    store.enqueue("y", repo="r", cwd="/x")
    store.close()
    assert cli.main(["board", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"needs_you": 1, "orchestrator": 1, "running": 0, "workers": 0,
                                                   "queued": 1, "done": 0, "answering": 0}
    assert cli.main(["board"]) == 0
    assert capsys.readouterr().out.strip() == "locks ◆2 ▶0 ⏸1"


def test_the_json_surface_a_page_reads(capsys, git_repo):
    """What another program reads: the board scoped to some repositories, a task, a queued task."""
    from locks import repo

    assert cli.main(["queue", "add", "write the docs", "--repo", str(git_repo), "--json"]) == 0
    queued = json.loads(capsys.readouterr().out)
    assert queued["status"] == "queued" and queued["cwd"] == str(git_repo.resolve())
    store = Store.open()
    store.create_task("elsewhere", status="failed", source="locks", repo="github.com/o/other")
    store.close()

    assert cli.main(["board", "--full", "--repo", str(git_repo)]) == 0
    board = json.loads(capsys.readouterr().out)
    assert board["repos"] == {str(git_repo): repo.key(git_repo.resolve())}
    assert board["counts"] == {"needs_you": 0, "orchestrator": 0, "running": 0, "workers": 0, "queued": 1,
                               "done": 0, "answering": 0}
    assert board["queued"][0]["tasks"][0]["id"] == queued["id"]
    assert cli.main(["board", "--full"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["orchestrator"] == 1

    assert cli.main(["show", str(queued["id"]), "--json"]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["task"]["title"] == "write the docs" and detail["attempts"] == []
    assert cli.main(["show", "999", "--json"]) == 1


def test_projects_sessions_and_memory_as_json(capsys, git_repo):
    """Where locks has worked, the sessions it saw there with how to resume them, and the problems
    and fixes it remembers, everything or one repository's."""
    from locks import repo

    key = repo.key(git_repo.resolve())
    store = Store.open()
    store.touch_session("s-1", str(git_repo), key)
    store.create_task("fix the cart total", status="done", source="hook", session_id="s-1", repo=key,
                      cwd=str(git_repo))
    store.create_task("queued one", status="queued", source="queue", repo=key, cwd=str(git_repo))
    store.touch_session("s-2", "/elsewhere", "github.com/o/other")
    p = store.open_problem("cart total off by one", repo=key)
    store.add_fix(p, "round before summing", "failed", repo=key, why="still off")
    store.add_fix(p, "sum in cents", "worked", repo=key)
    q = store.open_problem("flaky login test", repo="github.com/o/other")
    store.add_fix(q, "retry", "failed", repo="github.com/o/other")
    store.close()

    # Only an enrolled project (a .locks/ folder) is listed; --all shows the rest.
    assert cli.main(["projects", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main(["init", str(git_repo)]) == 0 and "enrolled" in capsys.readouterr().out
    assert (git_repo / ".locks" / ".gitignore").read_text().endswith("*\n")
    assert cli.main(["init", str(git_repo / "nope")]) == 1
    assert cli.main(["projects", "--json"]) == 0
    projects = {x["repo"]: x for x in json.loads(capsys.readouterr().out)}
    assert list(projects) == [key] and projects[key]["enrolled"]
    assert projects[key]["dir"] == str(git_repo.resolve()) and projects[key]["exists"]
    assert projects[key]["sessions"] == 1 and projects[key]["tasks"] == {"queued": 1}
    assert cli.main(["projects", "--json", "--all"]) == 0
    projects = {x["repo"]: x for x in json.loads(capsys.readouterr().out)}
    assert projects["github.com/o/other"]["exists"] is False and not projects["github.com/o/other"]["enrolled"]
    assert cli.main(["projects", "--all"]) == 0
    out = capsys.readouterr().out
    assert "(gone)" in out and "not enrolled" in out

    assert cli.main(["sessions", "--json", "--repo", str(git_repo)]) == 0
    sessions = json.loads(capsys.readouterr().out)
    assert [x["id"] for x in sessions] == ["s-1"]
    assert sessions[0]["prompts"] == 1 and sessions[0]["last_prompt"] == "fix the cart total"
    assert sessions[0]["resume"].endswith("&& claude --resume s-1")
    out = (cli.main(["sessions"]), capsys.readouterr().out)[1]
    assert "claude --resume s-1" in out and "s-2" not in out  # /elsewhere is not enrolled

    assert cli.main(["memory", "list", "--json"]) == 0
    assert {x["title"] for x in json.loads(capsys.readouterr().out)} == {"cart total off by one", "flaky login test"}
    assert cli.main(["memory", "list", "--json", "--repo", str(git_repo)]) == 0
    mine = json.loads(capsys.readouterr().out)
    assert [x["state"] for x in mine] == ["solved"] and [f["outcome"] for f in mine[0]["fixes"]] == ["failed", "worked"]
    assert cli.main(["memory", "list", "--json", "--query", "login"]) == 0
    assert [x["title"] for x in json.loads(capsys.readouterr().out)] == ["flaky login test"]
    assert cli.main(["memory", "list", "--json", "--query", "login", "--repo", str(git_repo)]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main(["memory", "list"]) == 0 and "sum in cents" in capsys.readouterr().out


def test_ui_command_serves_until_interrupted(monkeypatch, capsys):
    from locks.ui import server

    class Fake:
        server_address = ("127.0.0.1", 4321)

        def __init__(self):
            import threading
            self.stop_event = threading.Event()
            self.claimed: list = []

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

        def claim_tab(self, session):
            self.claimed.append(session)
            return session not in self.claimed[:-1]

    fake = Fake()
    monkeypatch.setattr(server, "serve", lambda port: fake)
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    assert cli.main(["ui", "--open"]) == 0
    assert "http://127.0.0.1:4321/" in capsys.readouterr().out and opened

    # --tabs: one tab for the session, which replaces the plain board tab, never one per task.
    opened.clear()
    monkeypatch.setattr("webbrowser.open_new_tab", lambda url: opened.append(url))
    assert cli.main(["ui", "--open", "--tabs", "--session", "s9"]) == 0
    out = capsys.readouterr().out
    assert opened == ["http://127.0.0.1:4321/#session=s9"]
    assert "tab: session s9" in out and fake.claimed == ["s9"]

    # Without --open the session is only claimed (a handed-over server): no tab at all.
    opened.clear()
    assert cli.main(["ui", "--tabs", "--session", "s9"]) == 0
    assert opened == [] and fake.claimed == ["s9", "s9"]


def test_resume_continues_a_stopped_task_with_what_it_was_refused_granted(capsys, git_repo, monkeypatch):
    calls = []

    def fake_run(text, repo_dir, store, options, **kw):
        calls.append((text, repo_dir, options, kw))
        return orchestrate.Report(kw.get("task_id"), "done", None)  # type: ignore[arg-type]

    monkeypatch.setattr(orchestrate.Report, "text", lambda self: f"task #{self.task_id}: {self.status}")
    store = Store.open()
    t = store.create_task("create the routes", status="blocked", source="locks", repo="r", cwd=str(git_repo),
                          kind="implement", dispatched=1, cancel_requested=1,
                          options=json.dumps({"verify": "npm test", "allow_tools": ["Bash(npm run build)"]}))
    store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=0, failure="permission", summary="refused")
    store.add_event(t["id"], "attempt_finished", seq=1, denied=["Bash(node app.js)"])
    approval = store.create_task("plan it", status="needs_approval", source="locks", repo="r", cwd=str(git_repo),
                                 kind="plan")
    store.add_event(approval["id"], "moved", move="needs_approval", next_cell="fable/high")
    hook = store.create_task("a prompt", status="interrupted", source="hook", repo="r", cwd=str(git_repo))
    done = store.create_task("finished", status="done", source="locks", repo="r", cwd=str(git_repo))
    odd = store.create_task("odd", status="failed", source="locks", repo="r", cwd=str(git_repo), kind="implement")
    store.add_attempt(odd["id"], cell="sonnet/high", max_turns=30, passed=0, failure="no-such-failure")
    store.close()
    monkeypatch.setattr(orchestrate, "run", fake_run)

    assert cli.main(["resume", str(t["id"]), "--allow", "Bash(node:*)", "--no-model"]) == 0
    text, repo_dir, options, kw = calls[-1]
    assert text == "create the routes" and repo_dir == git_repo
    assert options.allow_tools == ("Bash(npm run build)", "Bash(node:*)") and options.verify == "npm test"
    assert options.kind == "implement" and options.start.label == "sonnet/medium"
    assert kw["task_id"] == t["id"] and kw["history"][0].failure.value == "permission"
    assert kw["history"][0].denied == ("Bash(node app.js)",)  # the resumed brief still says what was refused
    assert options.resume_why == ("resumed after it blocked (1 attempt(s) before): it starts at sonnet/medium, the "
                                  "cell its last attempt ran")
    assert options.isolate is True
    store = Store.open()
    row = store.get_task(t["id"])
    assert row["dispatched"] == 0 and row["cancel_requested"] == 0
    assert json.loads(row["options"])["allow_tools"] == ["Bash(npm run build)", "Bash(node:*)"]
    store.close()

    assert cli.main(["resume", str(approval["id"])]) == 1
    assert "--allow-approval" in capsys.readouterr().err
    assert cli.main(["resume", str(approval["id"]), "--allow-approval"]) == 0
    assert calls[-1][2].start.label == "fable/high" and calls[-1][2].allow_approval
    assert cli.main(["resume", str(odd["id"]), "--verify", "true", "--no-isolate"]) == 0
    assert calls[-1][2].isolate is False and calls[-1][2].verify == "true"
    assert calls[-1][3]["history"][0].failure is None  # an old or unknown failure reads back as no verdict
    from locks import stops

    store = Store.open()
    big = store.create_task("reduce it", status="running", source="locks", repo="r", cwd=str(git_repo), kind="refactor")
    stops.record(store, big["id"], stops.Stop("turns", "outgrew"))
    wrong = store.create_task("impossible", status="running", source="locks", repo="r", cwd=str(git_repo))
    stops.record(store, wrong["id"], stops.Stop("spec", "no such API"))
    store.close()
    assert cli.main(["resume", str(big["id"])]) == 0
    assert calls[-1][2].max_turns == 120
    assert cli.main(["resume", str(big["id"]), "--max-turns", "200"]) == 0 and calls[-1][2].max_turns == 200
    assert cli.main(["resume", str(wrong["id"])]) == 1
    assert "rewrite it as a new task" in capsys.readouterr().err
    for refused in (hook["id"], done["id"], 999):
        assert cli.main(["resume", str(refused)]) == 1
    assert "only a task that stopped" in capsys.readouterr().err


def test_queue_add_keeps_the_rules_a_person_granted(capsys, git_repo):
    assert cli.main(["queue", "add", "build it", "--repo", str(git_repo), "--allow", "Bash(npm run build)",
                     "--json"]) == 0
    task_id = json.loads(capsys.readouterr().out)["id"]
    store = Store.open()
    task = store.get_task(task_id)
    store.close()
    from locks import flow
    assert flow.queued_options(task, orchestrate.Options()).allow_tools == ("Bash(npm run build)",)
    store = Store.open()
    assert store.messages(task_id)[0]["role"] == "person"
    store.close()


def test_queue_add_from_a_session_is_the_orchestrator_s(capsys, git_repo, monkeypatch):
    monkeypatch.setenv("LOCKS_SESSION_ID", "s1")
    assert cli.main(["queue", "add", "build it", "--repo", str(git_repo), "--json"]) == 0
    task_id = json.loads(capsys.readouterr().out)["id"]
    store = Store.open()
    assert store.messages(task_id)[0]["role"] == "orchestrator"
    store.close()


@pytest.mark.parametrize(("found", "code", "says"), [
    ({"locks": True, "version": None}, 1, "from before 0.4.1"),
    ({"locks": True, "version": "0.1.1"}, 1, "0.1.1, started before"),
    ({"locks": False, "version": None}, 1, "another program"),
    (None, 1, "another program"),
])
def test_ui_on_a_taken_port_says_who_holds_it(monkeypatch, capsys, found, code, says):
    from locks.ui import server

    def taken(port):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(server, "serve", taken)
    monkeypatch.setattr(server, "occupant", lambda port: found)
    monkeypatch.setattr(server, "take_over", lambda port: None)  # it could not be stopped
    assert cli.main(["ui", "--port", "8791"]) == code
    assert says in capsys.readouterr().err


def test_ui_takes_the_port_from_an_older_locks_ui(monkeypatch, capsys):
    from locks.ui import server

    served = []

    class Up:
        server_address = ("127.0.0.1", 8791)
        def __init__(self):
            self.seen: set = set()
            self.stop_event = __import__("threading").Event()

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    def serve(port):
        served.append(port)
        if len(served) == 1:
            raise OSError(errno.EADDRINUSE, "Address already in use")
        return Up()

    monkeypatch.setattr(server, "serve", serve)
    monkeypatch.setattr(server, "occupant", lambda port: {"locks": True, "version": "0.31.0"})
    monkeypatch.setattr(server, "take_over", lambda port: 4242)
    assert cli.main(["ui", "--port", "8791"]) == 0
    out = capsys.readouterr().out
    assert "stopped the locks UI 0.31.0 that held port 8791 (pid 4242)" in out and served == [8791, 8791]


def test_only_an_older_locks_ui_found_by_its_command_line_is_stopped(monkeypatch):
    from locks import __version__
    from locks.ui import server

    assert server.older({"locks": True, "version": "0.31.0"}) and server.older({"locks": True, "version": None})
    assert not server.older({"locks": True, "version": __version__}) and not server.older({"locks": False})
    assert not server.older({"locks": True, "version": "999.0.0"}) and not server.older(None)

    class Ran:
        def __init__(self, out):
            self.stdout = out

    def run_as(command_line, pids="4242\n"):
        return lambda argv, **kw: Ran(pids if argv[0] == "lsof" else command_line)

    assert server.listener(8791, run_as("/usr/bin/python3 -m locks ui --port 8791\n")) == 4242
    assert server.listener(8791, run_as("node server.js\n")) is None
    assert server.listener(8791, run_as("python -m locks ui", pids="1\n2\n")) is None
    assert server.listener(8791, run_as("python -m locks ui", pids="")) is None
    killed = []
    gone = iter([{"locks": True}, None])
    monkeypatch.setattr(server, "occupant", lambda port: next(gone))
    assert server.take_over(8791, run=run_as("python -m locks ui\n"), kill=lambda pid, sig: killed.append(pid)) == 4242
    assert killed == [4242]
    monkeypatch.setattr(server, "occupant", lambda port: {"locks": True})
    assert server.take_over(8791, run=run_as("python -m locks ui\n"), kill=lambda pid, sig: None, wait_s=0.2) is None
    assert server.take_over(8791, run=run_as("vim\n"), kill=lambda pid, sig: killed.append(pid)) is None


def test_ui_on_a_port_this_locks_already_serves_opens_it(monkeypatch, capsys):
    from locks import __version__
    from locks.ui import server

    def taken(port):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(server, "serve", taken)
    monkeypatch.setattr(server, "occupant", lambda port: {"locks": True, "version": __version__})
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    assert cli.main(["ui", "--port", "8791", "--open"]) == 0
    assert "already at http://127.0.0.1:8791/" in capsys.readouterr().out and opened

    # With --tabs it asks that server for the session's one tab, and opens it only when told it may.
    tabs, answers = [], iter([True, False, None])
    monkeypatch.setattr("webbrowser.open_new_tab", lambda url: tabs.append(url))
    monkeypatch.setattr(server, "claim_tab_there", lambda port, session: next(answers))
    opened.clear()
    assert cli.main(["ui", "--port", "8791", "--open", "--tabs", "--session", "s9"]) == 0
    assert tabs == ["http://127.0.0.1:8791/#session=s9"] and opened == []
    capsys.readouterr()
    assert cli.main(["ui", "--port", "8791", "--open", "--tabs", "--session", "s9"]) == 0
    assert "has its tab already" in capsys.readouterr().out and len(tabs) == 1
    assert cli.main(["ui", "--port", "8791", "--open", "--tabs", "--session", "s9"]) == 0
    assert "could not be asked" in capsys.readouterr().err and len(tabs) == 1

    def broken(port):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(server, "serve", broken)
    with pytest.raises(OSError):
        cli.main(["ui", "--port", "80"])


def test_a_report_the_caller_read_is_not_reported_again(capsys, git_repo, monkeypatch):
    store = Store.open()
    t = store.create_task("x", status="running", source="locks", session_id="s", cwd=str(git_repo))
    store.close()

    def fake_run(text, repo_dir, store, options, **kw):
        store.add_event(t["id"], "finished", status="done")
        store.update_task(t["id"], status="done")
        return orchestrate.Report(t["id"], "done", None)  # type: ignore[arg-type]

    monkeypatch.setattr(orchestrate, "run", fake_run)
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "task: done")
    monkeypatch.setenv("LOCKS_SESSION_ID", "s")
    assert cli.main(["run", "x", "--repo", str(git_repo), "--no-model"]) == 0
    store = Store.open()
    assert store.unreported("s") == []
    store.close()


def test_an_unattended_resume_never_grants_what_only_a_person_can(capsys, git_repo, monkeypatch):
    """A model may continue work; it may not clear a refusal or an approval."""
    from locks import grants, stops

    ran = []
    monkeypatch.setattr(orchestrate, "run", lambda *a, **kw: ran.append(kw["task_id"]) or orchestrate.Report(
        kw["task_id"], "done", None))  # type: ignore[arg-type]
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "ok")
    store = Store.open()
    refused = store.create_task("build it", status="running", source="locks", repo="r", cwd=str(git_repo))
    stops.record(store, refused["id"], stops.Stop("permission", "refused", denied=("Bash(npm run build)",),
                                                  allow=("Bash(npm run build:*)",)))
    waiting = store.create_task("plan it", status="needs_approval", source="locks", repo="r", cwd=str(git_repo))
    outgrew = store.create_task("big", status="running", source="locks", repo="r", cwd=str(git_repo))
    stops.record(store, outgrew["id"], stops.Stop("turns", "outgrew"))
    store.close()

    # 1. refused, nothing granted: exit 3, the rule named, nothing ran
    assert cli.main(["resume", str(refused["id"]), "--unattended"]) == 3
    err = capsys.readouterr().err
    assert "only the person can allow Bash(npm run build:*)" in err and "locks allow 'Bash(npm run build:*)'" in err
    # 2. a rule passed by hand is the person's, as is one they kept for the repository
    assert cli.main(["resume", str(refused["id"]), "--unattended", "--allow", "Bash(npm run build:*)"]) == 0
    grants.grant("r", ["Bash(npm run build:*)"])
    assert cli.main(["resume", str(refused["id"]), "--unattended"]) == 0
    # 3. a cell waiting for approval stays waiting
    assert cli.main(["resume", str(waiting["id"]), "--unattended"]) == 3
    assert "needs the person's approval" in capsys.readouterr().err
    # 4. work that only outgrew its turns goes on
    assert cli.main(["resume", str(outgrew["id"]), "--unattended"]) == 0
    assert ran == [refused["id"], refused["id"], outgrew["id"]]


def test_a_detached_resume_returns_at_once_and_its_ending_is_left_for_the_session(capsys, git_repo, monkeypatch):
    from locks import stops

    store = Store.open()
    big = store.create_task("big", status="running", source="locks", repo="r", cwd=str(git_repo), session_id="s")
    stops.record(store, big["id"], stops.Stop("turns", "outgrew"))
    store.close()
    started = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kw: started.append((argv, kw)))
    assert cli.main(["resume", str(big["id"]), "--detach", "--max-turns", "150", "--allow", "Bash(ls:*)"]) == 0
    argv, kw = started[0]
    assert argv[1:] == ["-m", "locks", "resume", str(big["id"]), "--no-report", "--max-turns", "150",
                        "--allow", "Bash(ls:*)"]
    assert kw["start_new_session"] and "LOCKS_SESSION_ID" not in kw["env"]
    assert "in the background" in capsys.readouterr().out
    # a detached resume does not take the ending its session is owed
    monkeypatch.setattr(orchestrate, "run", lambda *a, **kw: orchestrate.Report(kw["task_id"], "done", None))
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "ok")
    assert cli.main(["resume", str(big["id"]), "--no-report"]) == 0
    store = Store.open()
    assert store.last_event(big["id"], "reported") is None
    store.close()


def test_keep_allows_once_for_the_repository_and_never_unattended(capsys, git_repo, monkeypatch):
    """One press per program per repository: the next task there is not refused it again."""
    from locks import grants, stops

    monkeypatch.setattr(orchestrate, "run", lambda *a, **kw: orchestrate.Report(kw["task_id"], "done", None))
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "ok")
    started = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    store = Store.open()
    refused = store.create_task("build it", status="blocked", source="locks", repo="r", cwd=str(git_repo))
    stops.record(store, refused["id"], stops.Stop("permission", "refused", denied=("Bash(npm test)",),
                                                  allow=("Bash(npm test:*)",)))
    other = store.create_task("other", status="blocked", source="locks", repo="elsewhere", cwd=str(git_repo))
    stops.record(store, other["id"], stops.Stop("permission", "refused", allow=("Bash(npm test:*)",)))
    keyless = store.create_task("no repo", status="blocked", source="locks", repo=None, cwd=str(git_repo))
    store.close()

    # 1. an unattended resume never keeps a rule, whatever it names: nothing granted, nothing run
    assert cli.main(["resume", str(refused["id"]), "--unattended", "--keep", "--allow", "Bash(npm test:*)"]) == 2
    assert "only a person passes it" in capsys.readouterr().err
    assert grants.granted("r") == ()
    # 2. --keep with no rule, or no repository to key them to, says so and keeps nothing
    assert cli.main(["resume", str(refused["id"]), "--keep"]) == 2
    assert "name them with --allow" in capsys.readouterr().err
    assert cli.main(["resume", str(keyless["id"]), "--keep", "--allow", "Bash(ls:*)", "--detach"]) == 2
    assert grants.granted("r") == () and started == []
    # 3. a person's press keeps the rule for the task's repository only, and a detached child does not keep twice
    assert cli.main(["resume", str(refused["id"]), "--keep", "--allow", " Bash(npm test:*) ", "--detach"]) == 0
    assert grants.granted("r") == ("Bash(npm test:*)",) and grants.granted("elsewhere") == ()
    assert "--keep" not in started[0]
    # 4. now an unattended resume of the next refusal in that repository goes on, elsewhere it still waits
    assert cli.main(["resume", str(refused["id"]), "--unattended"]) == 0
    assert cli.main(["resume", str(other["id"]), "--unattended"]) == 3
    # 5. keeping again adds nothing twice
    assert cli.main(["resume", str(refused["id"]), "--keep", "--allow", "Bash(npm test:*)"]) == 0
    assert grants.granted("r") == ("Bash(npm test:*)",)


def test_endings_are_peeked_or_delivered_once_and_only_to_their_session(capsys):
    store = Store.open()
    mine = store.create_task("write docs", status="running", source="locks", session_id="s1", final_cell="haiku")
    store.add_event(mine["id"], "finished", status="done", final_cell="haiku")
    store.update_task(mine["id"], status="done", result="wrote them")
    other = store.create_task("other session's", status="running", source="locks", session_id="s2")
    store.add_event(other["id"], "finished", status="failed")
    store.update_task(other["id"], status="failed")
    store.create_task("still running", status="running", source="locks", session_id="s1")
    store.close()

    assert cli.main(["endings", "--session", "s1", "--peek", "--json"]) == 0
    peeked = json.loads(capsys.readouterr().out)
    assert peeked["ids"] == [mine["id"]] and "#1 [done] write docs" in peeked["notice"]
    assert peeked["statuses"] == {str(mine["id"]): "done"}  # the mod's toast says how each ended
    assert "still running" not in peeked["notice"] and "other session" not in peeked["notice"]
    assert cli.main(["endings", "--session", "s1", "--json"]) == 0  # a peek took nothing
    assert json.loads(capsys.readouterr().out)["ids"] == [mine["id"]]
    assert cli.main(["endings", "--session", "s1", "--json"]) == 0  # delivered once
    nothing = json.loads(capsys.readouterr().out)
    assert nothing.pop("pulse").endswith("/pulse")  # where the mod looks between its polls
    assert nothing == {"notice": None, "ids": [], "statuses": {}}
    assert cli.main(["endings", "--session", "nobody"]) == 0 and capsys.readouterr().out == ""
    store = Store.open()
    assert store.last_event(mine["id"], "reported")["data"]["via"] == "mod"
    store.close()


def test_note_and_topics_answer_in_json(git_repo, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    assert cli.main(["note", "Invoices are numbered per country", "--topic", "business", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"id": 1, "new": True, "topic": "business",
                                                   "title": "Invoices are numbered per country"}
    assert cli.main(["notes", "topics", "--json"]) == 0
    topics = json.loads(capsys.readouterr().out)
    assert topics[0] == {"name": "business", "description": topics[0]["description"], "notes": 1}
    assert [t["name"] for t in topics] == ["business", "code", "decisions", "conventions", "environment"]


def test_memory_forget_takes_a_problem_and_its_fixes_out(capsys):
    store = Store.open()
    problem = store.open_problem("Cannot find module vite", repo="r")
    store.add_fix(problem, "reinstalled node_modules", "worked", repo="r")
    store.close()
    assert cli.main(["memory", "forget", str(problem)]) == 0
    assert f"forgot problem #{problem} and its 1 fix(es)" in capsys.readouterr().out
    assert cli.main(["memory", "forget", str(problem)]) == 1
    store = Store.open()
    assert store.problem(problem) is None
    store.close()


def test_a_start_cell_with_no_such_dial_is_a_usage_error(capsys):
    import pytest

    with pytest.raises(SystemExit) as exit_:
        cli.main(["run", "--start", "haiku/turbo", "x"])
    assert exit_.value.code == 2
    assert "haiku needs an effort" in capsys.readouterr().err


def test_a_kind_alias_is_read_as_the_kind_it_means(capsys):
    from locks import cli

    assert cli._kind("research") == "explore"
    assert "read as 'explore'" in capsys.readouterr().err
    assert cli._kind("Explore") == "explore"


def test_an_unknown_kind_names_the_kinds():
    import argparse

    import pytest

    from locks import cli

    with pytest.raises(argparse.ArgumentTypeError, match="leave --kind out"):
        cli._kind("whatever")


def test_events_for_a_watcher_outside_locks(tmp_path, capsys):
    """A neighbour that watches some repositories starts at the newest event and reads only theirs, each
    ending named without asking again."""
    from locks import repo

    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    mine.mkdir()
    theirs.mkdir()
    store = Store.open()
    ours = store.create_task("fix the cart", status="done", source="locks", repo=repo.key(mine), session_id="s1")
    other = store.create_task("elsewhere", status="done", source="locks", repo=repo.key(theirs))
    store.add_event(ours["id"], "finished", status="done")
    store.add_event(other["id"], "finished", status="done")
    store.add_event(ours["id"], "reported", via="stop")
    last = store.last_event_id()
    store.close()
    assert cli.main(["events", "--last-id"]) == 0
    assert capsys.readouterr().out.strip() == str(last)
    assert cli.main(["events", "--json", "--kind", "finished", "--repo", str(mine)]) == 0
    [line] = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert (line["task_id"], line["repo"], line["title"], line["session_id"]) == (
        ours["id"], repo.key(mine), "fix the cart", "s1")
    assert cli.main(["events", "--json", "--after", str(last), "--repo", str(mine)]) == 0
    assert capsys.readouterr().out == ""


def test_overview_keeps_the_repositories_asked_for(tmp_path, capsys):
    from locks import repo

    for name in ("shop", "api"):
        (tmp_path / name / ".locks").mkdir(parents=True)
    store = Store.open()
    for name in ("shop", "api"):
        store.touch_session(name, str(tmp_path / name), repo.key(tmp_path / name))
        store.create_task(f"work in {name}", status="done", source="locks", repo=repo.key(tmp_path / name),
                          session_id=name)
    store.enqueue("tidy the api", repo=repo.key(tmp_path / "api"), cwd=str(tmp_path / "api"))
    store.close()
    assert cli.main(["overview", "--json", "--repo", str(tmp_path / "shop")]) == 0
    seen = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in seen["sessions"]] == ["shop"]
    assert [t["title"] for t in seen["sessions"][0]["tasks"]] == ["work in shop"]
    assert seen["other"] == []


def test_a_press_elsewhere_runs_the_resume_locks_names_and_nothing_else(tmp_path, monkeypatch, capsys):
    """A page outside locks sends the task id; the rules or approval come from locks' account."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(tmp_path)
    store = Store.open()
    refused = store.create_task("x", status="blocked", source="locks", repo="r", cwd=str(work))
    stops.record(store, refused["id"], stops.Stop("permission", "refused", allow=("Bash(npm:*)",)))
    done = store.create_task("y", status="done", source="locks", repo="r", cwd=str(work))
    store.close()
    ran = []
    monkeypatch.setattr(cli, "_detach", lambda args: ran.append((args.id, args.allow, cli.os.getcwd())) or 0)
    assert cli.main(["resume", str(refused["id"]), "--pressed"]) == 0
    assert ran == [(refused["id"], ["Bash(npm:*)"], str(work))]
    assert cli.main(["resume", str(refused["id"]), "--pressed", "--allow", "Bash(rm:*)"]) == 2
    assert cli.main(["resume", str(refused["id"]), "--pressed", "--keep"]) == 2
    assert "give it the task id alone" in capsys.readouterr().err
    assert cli.main(["resume", str(done["id"]), "--pressed"]) == 1
    assert cli.main(["resume", "999", "--pressed"]) == 1
    assert "has no resume to press" in capsys.readouterr().err
    assert len(ran) == 1
    # A stop an older locks wrote on a dollar budget resumes plainly: the flag no longer exists.
    store = Store.open()
    spent = store.create_task("z", status="blocked", source="locks", repo="r", cwd=str(work),
                              options=json.dumps({"budget_usd": 0.5, "verify": "true"}))
    stops.record(store, spent["id"], stops.Stop("budget", "the $5.00 budget is spent", extra={"budget_usd": 5.0}))
    store.close()
    assert cli.main(["resume", str(spent["id"]), "--pressed"]) == 0
    assert ran[-1] == (spent["id"], None, str(work))


def test_a_dismissal_from_tanka_says_where_it_came_from(capsys):
    store = Store.open()
    t = store.create_task("x", status="failed", source="locks", repo="r")
    store.close()
    assert cli.main(["dismiss", str(t["id"]), "--via", "tanka"]) == 0
    store = Store.open()
    assert "from Tanka's page" in stops.of(store, store.get_task(t["id"])).reason
    store.close()


@pytest.mark.parametrize("argv", [
    ["run", "--budget", "5", "x"], ["resume", "1", "--budget", "5"], ["queue", "add", "x", "--budget", "5"],
    ["workflow", "run", "w", "--budget", "3"], ["investigate", "start", "--budget", "6"],
])
def test_no_command_takes_a_budget(argv, capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(argv)
    assert exit_.value.code == 2 and "--budget" in capsys.readouterr().err


def test_a_resume_nobody_approved_stops_after_two_runs_of_attempts_and_carries_no_approval(tmp_path, monkeypatch,
                                                                                           capsys):
    work = tmp_path / "work"
    work.mkdir()
    store = Store.open()
    task = store.create_task("x", status="failed", source="locks", repo="r", cwd=str(work),
                             options=json.dumps({"allow_approval": True}))
    for n in range(cli.UNATTENDED_ATTEMPTS):
        store.add_attempt(task["id"], cell="sonnet/high", max_turns=30, passed=0, failure="code_bug",
                          summary=f"try {n}", evidence="", cost_usd=1.0, input_tokens=0, output_tokens=0, turns=1,
                          duration_s=1, changed_paths=(), capabilities=())
    store.close()
    assert cli.main(["resume", str(task["id"]), "--unattended"]) == 3
    assert f"already had {cli.UNATTENDED_ATTEMPTS} attempts" in capsys.readouterr().err
    seen = []
    monkeypatch.setattr(cli.orchestrate, "run", lambda *a, **k: seen.append(a[3]) or orchestrate.Report(
        task["id"], "done", None))
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: f"task #{self.task_id}: {self.status}")
    monkeypatch.setattr(cli, "UNATTENDED_ATTEMPTS", 1000)
    cli.main(["resume", str(task["id"]), "--unattended", "--no-report"])
    assert seen and seen[0].allow_approval is False
    cli.main(["resume", str(task["id"]), "--no-report"])
    assert seen[-1].allow_approval is True


@pytest.mark.parametrize("argv", [
    ["run", "x"], ["resume", "1"], ["work"], ["queue", "add", "x"], ["workflow", "run", "w"],
    ["workflow", "retry", "1", "s"], ["investigate", "start"],
])
def test_a_worker_never_starts_locks_work(argv, monkeypatch, capsys):
    monkeypatch.setenv("LOCKS_WORKER_TASK", "41")
    assert cli.main(argv) == 2
    assert "worker of task #41" in capsys.readouterr().err


def test_a_worker_still_reads(monkeypatch, capsys):
    monkeypatch.setenv("LOCKS_WORKER_TASK", "41")
    assert cli.main(["tasks"]) == 0
    assert cli.main(["queue", "list"]) == 0


def test_a_task_the_check_stopped_resumes_with_what_the_check_found(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    store = Store.open()
    task = store.create_task("fix it", status="blocked", source="locks", repo="r", cwd=str(work), kind="implement")
    store.add_attempt(task["id"], cell="sonnet/high", max_turns=30, passed=0, failure="code_bug",
                      summary="still", evidence="The independent check found: x", cost_usd=0.1, input_tokens=0,
                      output_tokens=0, turns=1, duration_s=1, changed_paths=(), capabilities=())
    found = {"where": "app.py:3", "what": "drops the last item", "evidence_class": "deterministic",
             "cause": "introduced", "proof": "python probe.py"}
    old = {**found, "where": "old.py", "what": "always so", "cause": "pre-existing"}
    store.add_event(task["id"], "verified", seq=1, findings=[found, old], blocking=1)
    stops.record(store, task["id"], stops.Stop("review", "the check still finds problems"))
    store.close()
    seen = {}

    def fake_run(text, repo_dir, store, options, **kw):
        seen.update(kw)
        return orchestrate.Report(kw.get("task_id"), "done", None)

    monkeypatch.setattr(orchestrate, "run", fake_run)
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "done")
    assert cli.main(["resume", str(task["id"]), "--no-report"]) == 0
    assert [f["what"] for f in seen["known_findings"]] == ["drops the last item"]
    assert seen["history"][0].evidence == "The independent check found: x"


def test_an_unattended_resume_never_starts_on_a_cell_only_a_person_approves(capsys, git_repo, monkeypatch):
    ran = []
    monkeypatch.setattr(orchestrate, "run", lambda *a, **kw: ran.append(kw["task_id"]) or orchestrate.Report(
        kw["task_id"], "done", None))  # type: ignore[arg-type]
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "ok")
    store = Store.open()
    climbed = store.create_task("plan the migration", status="running", source="locks", repo="r",
                                cwd=str(git_repo), kind="plan", options=json.dumps({"allow_approval": True}))
    tagged = store.create_task("#fable plan it", status="running", source="locks", repo="r", cwd=str(git_repo),
                               kind="frontier")
    for task in (climbed, tagged):
        store.add_attempt(task["id"], cell="fable/high", max_turns=30, passed=0, failure="environment",
                          summary="no network", evidence="", cost_usd=1.0, input_tokens=0, output_tokens=0,
                          turns=1, duration_s=1, changed_paths=(), capabilities=())
        stops.record(store, task["id"], stops.Stop("environment", "no network"))
    store.close()
    # The approval its first run had is not a model's to use: refused before anything runs.
    assert cli.main(["resume", str(climbed["id"]), "--unattended", "--detach"]) == 3
    assert "would start again on fable/high" in capsys.readouterr().err
    # The person's own resume, or one they approve, goes on; a #fable task's tag is its approval.
    assert cli.main(["resume", str(climbed["id"]), "--unattended", "--allow-approval"]) == 0
    assert cli.main(["resume", str(tagged["id"]), "--unattended"]) == 0
    assert ran == [climbed["id"], tagged["id"]]


def test_a_dismissed_review_resumes_with_the_blockers_the_check_recorded(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    store = Store.open()
    task = store.create_task("fix it", status="blocked", source="locks", repo="r", cwd=str(work), kind="implement",
                             options=json.dumps({"investigation": "inv-1", "kind": "implement",
                                                 "part": {"id": "api", "n": 1, "of": 2, "plan": 9}}))
    shown = {"where": "app.py:3", "what": "drops the last item", "evidence_class": "deterministic",
             "cause": "introduced", "proof": "python probe.py"}
    carried = {"where": "app.py:9", "what": "may be slow", "evidence_class": "inferential", "cause": "introduced",
               "proof": ""}
    store.add_event(task["id"], "verified", seq=1, findings=[shown, carried], blocking=1, carried=1,
                    blockers=[shown])
    stops.record(store, task["id"], stops.Stop("review", "the check still finds problems"))
    assert stops.dismiss(store, store.get_task(task["id"]), via="cli") is not None
    store.close()
    seen = {}

    def fake_run(text, repo_dir, store, options, **kw):
        seen.update(kw)
        return orchestrate.Report(kw.get("task_id"), "done", None)

    monkeypatch.setattr(orchestrate, "run", fake_run)
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "done")
    assert cli.main(["resume", str(task["id"]), "--no-report"]) == 0
    # The blockers the check stopped on, never the finding that went with the pass.
    assert [f["what"] for f in seen["known_findings"]] == ["drops the last item"]
    store = Store.open()
    # An investigator stays one when it is resumed.
    assert json.loads(store.get_task(task["id"])["options"])["investigation"] == "inv-1"
    # A workflow step's part stays one: its integrate joins every part's branch.
    assert json.loads(store.get_task(task["id"])["options"])["part"]["id"] == "api"
    store.close()


def test_tries_the_network_dropped_under_are_no_attempts_for_an_unattended_resume(git_repo, monkeypatch):
    monkeypatch.setattr(orchestrate, "run", lambda *a, **kw: orchestrate.Report(kw["task_id"], "done", None))
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "ok")
    store = Store.open()
    task = store.create_task("sync it", status="running", source="locks", repo="r", cwd=str(git_repo))
    for seq in range(1, cli.UNATTENDED_ATTEMPTS + 1):
        store.add_attempt(task["id"], cell="sonnet/medium", max_turns=30, passed=0, failure="environment",
                          summary="dropped", evidence="", cost_usd=0.0, input_tokens=0, output_tokens=0, turns=1,
                          duration_s=1, changed_paths=(), capabilities=())
        if seq > 2:
            store.add_event(task["id"], "moved", seq=seq, move="retry", network="Connection error")
    stops.record(store, task["id"], stops.Stop("network", "the network stayed down"))
    store.close()
    assert cli.main(["resume", str(task["id"]), "--unattended"]) == 0


def _stub_run(monkeypatch):
    """A resume whose run is not the point: it passes, and records how it was called."""
    calls = []

    def fake_run(text, repo_dir, store, options, **kw):
        calls.append(kw)
        return orchestrate.Report(kw.get("task_id"), "done", None)  # type: ignore[arg-type]

    monkeypatch.setattr(orchestrate, "run", fake_run)
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: f"task #{self.task_id}: {self.status}")
    return calls


def _gone_pid() -> int:
    """The pid of a process that has exited, as a restart leaves a running task's."""
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_resume_of_a_running_task_whose_process_is_gone_sweeps_it_instead_of_refusing(capsys, git_repo, monkeypatch):
    calls = _stub_run(monkeypatch)
    store = Store.open()
    stuck = store.create_task("create the routes", status="running", source="locks", repo="r", cwd=str(git_repo),
                              kind="implement", pid=_gone_pid())
    alive = store.create_task("still going", status="running", source="locks", repo="r", cwd=str(git_repo),
                              kind="implement", pid=os.getpid())
    store.close()

    assert cli.main(["resume", str(stuck["id"])]) == 0
    assert calls[-1]["task_id"] == stuck["id"]
    store = Store.open()
    assert store.last_event(stuck["id"], "finished")["data"]["stop"]["cause"] == "died"
    store.close()
    # a task whose process lives is running: it is not taken from it
    assert cli.main(["resume", str(alive["id"])]) == 1
    assert f"task #{alive['id']} is running" in capsys.readouterr().err and len(calls) == 1


def test_resuming_the_task_a_lane_paused_on_opens_the_lane_and_a_pause_on_another_stays(git_repo, monkeypatch):
    from locks import flow

    _stub_run(monkeypatch)
    key = str(git_repo.resolve())
    store = Store.open()
    died = store.create_task("create the routes", status="running", source="locks", repo=key, cwd=str(git_repo),
                             kind="implement", pid=_gone_pid(), dispatched=1)
    other = store.create_task("other", status="failed", source="locks", repo=key, cwd=str(git_repo), kind="implement")
    assert flow.sweep(store) == [died["id"]]
    assert store.lanes()[0]["paused"]
    store.close()

    assert cli.main(["resume", str(other["id"])]) == 0
    store = Store.open()
    assert store.lanes()[0]["paused"]  # it waits on the task that died, not on the one resumed
    store.close()
    assert cli.main(["resume", str(died["id"])]) == 0
    store = Store.open()
    assert not store.lanes()[0]["paused"] and store.lanes()[0]["reason"] is None
    store.close()


def test_the_end_of_a_resume_starts_the_dispatcher_when_work_waits_behind_it(git_repo, monkeypatch):
    """After a restart no `locks work` existed: the task queued behind a resumed one stayed queued."""
    monkeypatch.setenv("LOCKS_AUTOWORK", "off")  # what follows reads its absence, then turns it on
    _stub_run(monkeypatch)
    started = []
    monkeypatch.setattr(dispatch, "start", lambda repo_dir, root, scope: started.append((repo_dir, scope)) or True)
    key = str(git_repo.resolve())
    store = Store.open()
    stopped = store.create_task("create the routes", status="blocked", source="locks", repo=key, cwd=str(git_repo),
                                kind="implement")
    store.close()

    store = Store.open()
    queued = store.enqueue("write the docs", repo=key, cwd=str(git_repo))
    store.close()
    assert cli.main(["resume", str(stopped["id"])]) == 0 and not started  # autowork is off here
    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    assert cli.main(["resume", str(stopped["id"])]) == 0
    assert started == [(git_repo.resolve(), key)]
    # nothing waits behind it: no dispatcher to start
    store = Store.open()
    store.update_task(queued["id"], status="cancelled")
    store.close()
    assert cli.main(["resume", str(stopped["id"])]) == 0 and len(started) == 1


def test_a_resume_that_reopens_a_paused_lane_starts_its_dispatcher_before_its_own_run(git_repo, monkeypatch):
    """The queue behind a resumed task waited for the resumed task to end, though it may run beside it."""
    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    started, during = [], []
    monkeypatch.setattr(dispatch, "start", lambda repo_dir, root, scope: started.append((repo_dir, scope)) or True)

    def fake_run(text, repo_dir, store, options, **kw):
        during.append(list(started))
        return orchestrate.Report(kw.get("task_id"), "done", None)  # type: ignore[arg-type]

    monkeypatch.setattr(orchestrate, "run", fake_run)
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: f"task #{self.task_id}: {self.status}")
    key = str(git_repo.resolve())
    store = Store.open()
    stopped = store.create_task("create the routes", status="blocked", source="locks", repo=key, cwd=str(git_repo),
                                kind="implement")
    store.enqueue("write the docs", repo=key, cwd=str(git_repo))
    store.pause_lane(key, f"task #{stopped['id']} ended blocked: refused")
    store.close()
    assert cli.main(["resume", str(stopped["id"])]) == 0
    # started when the lane reopened, while the resumed task ran
    assert during == [[(git_repo.resolve(), key)]]
    # a resume that reopened nothing has nothing to start before it runs
    started.clear()
    during.clear()
    assert cli.main(["resume", str(stopped["id"])]) == 0 and during == [[]]


def test_unpausing_a_lane_with_queued_work_starts_its_dispatcher(git_repo, monkeypatch, capsys):
    monkeypatch.setenv("LOCKS_AUTOWORK", "off")  # what follows reads its absence, then turns it on
    key = str(git_repo.resolve())
    store = Store.open()
    store.enqueue("write the docs", repo=key, cwd=str(git_repo))
    store.pause_lane(key, "task #1 stopped with no sign of life")
    store.close()
    started = []
    monkeypatch.setattr(dispatch, "start", lambda repo_dir, root, scope: started.append((repo_dir, scope)) or True)

    assert cli.main(["lanes", "--unpause", str(git_repo)]) == 0 and not started  # autowork is off here
    store = Store.open()
    store.pause_lane(key, "task #1 stopped with no sign of life")
    store.close()
    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    capsys.readouterr()
    assert cli.main(["lanes", "--unpause", str(git_repo)]) == 0
    assert started == [(git_repo.resolve(), key)] and "started its dispatcher" in capsys.readouterr().out
    # a lane with nothing queued has no work for one
    assert cli.main(["lanes", "--unpause", "github.com/o/empty"]) == 0 and len(started) == 1


def test_a_dispatcher_that_runs_is_not_started_again_by_an_unpause(git_repo, monkeypatch):
    from locks.store import home

    key = str(git_repo.resolve())
    store = Store.open()
    store.enqueue("write the docs", repo=key, cwd=str(git_repo))
    store.close()
    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    launched = []
    real = dispatch.start
    monkeypatch.setattr(dispatch, "start", lambda *a: real(*a, popen=lambda argv, **kw: launched.append(argv)))
    with dispatch.hold(home(), key):
        assert cli.main(["lanes", "--unpause", str(git_repo)]) == 0
    assert launched == []
    assert cli.main(["lanes", "--unpause", str(git_repo)]) == 0
    assert len(launched) == 1 and launched[0][1:5] == ["-m", "locks", "work", "--repo"]


def test_a_resume_locks_could_not_run_prints_one_line_and_keeps_the_traceback_in_the_hook_log(
        capsys, git_repo, monkeypatch):
    from locks import hooks, isolate
    from locks.store import home

    def boom(*args, **kwargs):
        raise isolate.IsolationError("git worktree add failed: fatal: a worktree is already\nregistered here")

    monkeypatch.setattr(orchestrate, "run", boom)
    store = Store.open()
    stopped = store.create_task("create the routes", status="blocked", source="locks", repo="r", cwd=str(git_repo),
                                kind="implement")
    store.close()

    assert cli.main(["resume", str(stopped["id"])]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "Traceback" not in err and len(err.strip().splitlines()) == 1
    assert (f"task #{stopped['id']}" in err and "a worktree is already registered here" in err
            and f"locks resume {stopped['id']}" in err and str(home() / hooks.ERROR_LOG) in err)
    log = (home() / hooks.ERROR_LOG).read_text()
    assert "Traceback" in log and "IsolationError" in log and f"resume #{stopped['id']}" in log
    assert hooks._drain_errors(home()) == 1  # the next SessionStart reports it


def test_a_resume_whose_worktree_cannot_be_made_stops_resumable_and_the_next_one_goes_on(capsys, git_repo,
                                                                                         monkeypatch):
    """The audited case end to end: `locks resume` raised `IsolationError` from `isolate.prepare`, left the
    task `running` with its dead pid, and every later `locks resume` refused it."""
    from locks import isolate
    from locks.classify import Classification

    real_run, real_prepare = orchestrate.run, isolate.prepare

    def fake_run(text, repo_dir, store, options, **kw):
        kw.update(registry={}, launcher=lambda spec: WorkerResult(True, None, "done", evidence="1 passed"),
                  classifier=lambda t: Classification("implement", "medium", "rule", "test"))
        return real_run(text, repo_dir, store, options, **kw)

    def refuse(*args, **kwargs):
        raise isolate.IsolationError("git worktree add failed: fatal: already registered")

    monkeypatch.setattr(orchestrate, "run", fake_run)
    store = Store.open()
    stopped = store.create_task("fix the cart", status="blocked", source="locks", repo="r", cwd=str(git_repo),
                                kind="implement")
    store.close()
    monkeypatch.setattr(isolate, "prepare", refuse)
    assert cli.main(["resume", str(stopped["id"]), "--no-model"]) == 1
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err and f"locks resume {stopped['id']}" in out
    store = Store.open()
    row = store.get_task(stopped["id"])
    assert row["status"] == "blocked" and row["pid"] is None
    store.close()

    monkeypatch.setattr(isolate, "prepare", real_prepare)
    assert cli.main(["resume", str(stopped["id"]), "--no-model"]) == 0


def test_tasks_and_show_say_the_points(capsys):
    store = Store.open()
    t = store.create_task("schema work", status="done", source="locks", repo="r", kind="implement", points=5,
                          points_why="4 criteria, touches schema, risk high")
    store.add_event(t["id"], "finished", status="done", realized=8, realized_why=["8 files", "400 lines"])
    store.create_task("older work", status="done", source="locks", repo="r", kind="docs")
    store.close()
    assert cli.main(["tasks", "--all"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith(f"#{t['id']}") and " 5 " in line and "schema work" in line for line in lines)
    assert cli.main(["show", str(t["id"])]) == 0
    out = capsys.readouterr().out
    assert "points 5: 4 criteria, touches schema, risk high" in out
    assert "realized 8: 8 files, 400 lines" in out


def test_a_dismissal_keeps_its_why_given_by_flag_or_after_the_ids(capsys):
    store = Store.open()
    one = store.create_task("x", status="failed", source="locks", repo="r")
    two = store.create_task("y", status="blocked", source="locks", repo="r")
    three = store.create_task("z", status="failed", source="locks", repo="r")
    store.close()
    assert cli.main(["dismiss", str(one["id"]), "--why", "the blockers are wrong"]) == 0
    # Words after the ids are the reason, not an id that fails to parse.
    assert cli.main(["dismiss", str(two["id"]), f"#{three['id']}", "superseded", "by", "the", "new", "design"]) == 0
    assert "dismissed task" in capsys.readouterr().out
    store = Store.open()
    first = stops.of(store, store.get_task(one["id"]))
    assert first.reason.endswith(": the blockers are wrong") and first.extra["why"] == "the blockers are wrong"
    for task in (two, three):
        stop = stops.of(store, store.get_task(task["id"]))
        assert stop.extra["why"] == "superseded by the new design"
    store.close()
    # Where the stop is shown, the why is shown with it.
    assert cli.main(["show", str(one["id"])]) == 0
    assert "the blockers are wrong" in capsys.readouterr().out
    assert cli.main(["dismiss", "--reason", "x", "not-a-number"]) == 2
    assert "locks dismiss N --why" in capsys.readouterr().err


def test_every_notice_that_offers_a_dismissal_prints_its_real_syntax():
    from pathlib import Path

    from locks import hooks

    said = Path(hooks.__file__).read_text(encoding="utf-8")
    assert "`locks dismiss N`" not in said and said.count("locks dismiss N --why '<why>'") == 2
    assert stops.dismiss_command(12) == "locks dismiss 12 --why '<why>'"


def test_tasks_lists_every_project_with_all_and_points_there_from_an_empty_one(capsys, tmp_path, monkeypatch):
    store = Store.open()
    store.create_task("api work", status="done", source="locks", repo="/work/api", kind="implement")
    store.create_task("web work", status="failed", source="locks", repo="/work/web", kind="implement")
    store.close()
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert cli.main(["tasks"]) == 0
    out = capsys.readouterr().out
    assert "no tasks in this project" in out and "2 in 2 other project(s)" in out and "locks tasks --all" in out
    assert cli.main(["tasks", "--all"]) == 0
    rows = capsys.readouterr().out.splitlines()
    assert any(" api " in row and "api work" in row for row in rows)
    assert any(" web " in row and "web work" in row for row in rows)


def test_run_detach_queues_the_task_and_starts_its_lane(capsys, git_repo, monkeypatch):
    kicked = []
    monkeypatch.setattr(dispatch, "kick", lambda cwd, key, env: kicked.append((str(cwd), key)) or "A worker takes it.")
    monkeypatch.setattr(orchestrate, "run", lambda *a, **kw: pytest.fail("a detached run never runs here"))
    assert cli.main(["run", "fix the totals", "--repo", str(git_repo), "--detach", "--verify", "true"]) == 0
    out = capsys.readouterr().out
    store = Store.open()
    (task,) = store.list_tasks(status=["queued"])
    store.close()
    assert f"queued #{task['id']}" in out and f"locks events --task {task['id']} --follow" in out
    assert json.loads(task["options"])["verify"] == "true"
    assert kicked == [(str(git_repo.resolve()), task["repo"])]


def test_clip_cuts_at_a_sentence_or_a_word_never_inside_a_bracket():
    from locks.text import clip

    assert clip("short", 30) == "short"
    cell = clip("attempt 3 sonnet/xhigh [claude-sonnet-4-5] → more effort on the same model", 26)
    assert cell == "attempt 3 sonnet/xhigh…" and "[c" not in cell
    said = clip("The patch checks the path but applies it later (line 197). A crafted file can escape the "
                "folder before the check runs.", 90)
    assert said == "The patch checks the path but applies it later (line 197). …"
    words = clip("word " * 40, 50)
    assert words.endswith("word…") and len(words) <= 50
    assert len(clip("x" * 100, 30)) == 30
    assert clip("a\n  b c", 10, one_line=True) == "a b c"


def test_run_and_queue_add_ask_a_build_of_many_items_split_and_start_nothing(git_repo, capsys, monkeypatch):
    big = ("Implement the student side:\n- the opinion form and grade before each quiz opens\n"
           "- a quiz per module and a final exam module\n- submodules the server locks, by 403 or a locked state\n"
           "- the start screen with the calendar the API returns\n- the questions and answers forum\n"
           "- the library of resources, with links")
    monkeypatch.setenv("LOCKS_AUTOSPLIT", "off")
    started = []
    monkeypatch.setattr(orchestrate, "run", lambda *a, **kw: started.append(a) or (_ for _ in ()).throw(
        AssertionError("started")))
    assert cli.main(["run", big, "--repo", str(git_repo)]) == cli.EXIT_SPLIT
    err = capsys.readouterr().err
    assert err.startswith("locks: not started: this request is several tasks (6 listed items)")
    assert "--surface" in err and "locks workflow run feature" in err and "--whole" in err
    assert cli.main(["queue", "add", big, "--repo", str(git_repo)]) == cli.EXIT_SPLIT
    store = Store.open()
    try:
        assert store.list_tasks(status=["queued"]) == [] and started == []
    finally:
        store.close()
    # --whole is the session's word that it is one task
    assert cli.main(["queue", "add", big, "--repo", str(git_repo), "--whole"]) == 0


def test_with_autosplit_a_request_that_is_several_tasks_is_split_and_queued(git_repo, capsys, monkeypatch):
    big = ("Implement the student side:\n- the opinion form and grade before each quiz opens\n"
           "- a quiz per module and a final exam module\n- submodules the server locks, by 403 or a locked state\n"
           "- the start screen with the calendar the API returns\n- the questions and answers forum\n"
           "- the library of resources, with links")
    monkeypatch.setenv("LOCKS_AUTOSPLIT", "on")
    split_with = []
    monkeypatch.setattr(cli, "cmd_split", lambda a: split_with.append(a) or 0)
    assert cli.main(["queue", "add", big, "--repo", str(git_repo)]) == 0
    assert split_with and split_with[0].queue and split_with[0].target == big
    assert "splitting it and queueing its pieces" in capsys.readouterr().err
    # nothing proposed: it stops as before and says how to split it
    monkeypatch.setattr(cli, "cmd_split", lambda a: 1)
    assert cli.main(["queue", "add", big, "--repo", str(git_repo)]) == cli.EXIT_SPLIT


def test_a_dismissal_or_a_resume_of_a_task_in_another_state_says_what_it_takes(store, capsys, tmp_path):
    running = store.create_task("build it", status="running", source="locks", cwd=str(tmp_path), pid=os.getpid())
    done = store.create_task("built", status="done", source="locks", cwd=str(tmp_path))
    assert cli.main(["dismiss", str(running["id"])]) == 1
    err = capsys.readouterr().err
    assert "It is running: its ending reaches you when it ends" in err and f"locks cancel {running['id']}" in err
    assert cli.main(["resume", str(running["id"])]) == 1
    assert "It is running" in capsys.readouterr().err
    assert cli.main(["resume", str(done["id"])]) == 1
    assert "It passed: nothing to take off the board or to resume" in capsys.readouterr().err


def test_a_resume_says_where_it_starts_and_when_that_is_below_what_it_ran():
    cells = ["sonnet/xhigh", "opus/high", "opus/xhigh"]
    assert cli.resume_line("failed", cells, "opus/xhigh", "last") == (
        "resumed after it failed (3 attempt(s) before): it starts at opus/xhigh, the cell its last attempt ran")
    lower = cli.resume_line("failed", cells, "opus/high", "asked")
    assert lower.endswith("it starts at opus/high, as `--start` asked; that is below opus/xhigh, the highest cell "
                          "it ran")
    assert cli.resume_line("cancelled", [], None, "last").startswith("resumed after it cancelled, with no attempt")


def test_show_tells_each_run_of_a_task_that_failed_and_was_resumed(capsys):
    store = Store.open()
    t = store.create_task("fix the radio", status="done", source="locks", repo="r", kind="debug-repro")
    store.add_event(t["id"], "planned", start="sonnet/xhigh", reasons=[])
    store.add_event(t["id"], "finished", status="failed", attempts=3)
    store.add_event(t["id"], "planned", start="opus/high", reasons=[
        "resumed after it failed (3 attempt(s) before): it starts at opus/high, as `--start` asked"])
    store.add_event(t["id"], "finished", status="done", attempts=1)
    store.close()
    assert cli.main(["show", str(t["id"])]) == 0
    out = capsys.readouterr().out
    assert "runs:\n  1. failed after 3 attempt(s), started at sonnet/xhigh\n  2. done after 1 attempt(s), started " \
           "at opus/high (resumed after it failed" in out


def test_tasks_workers_lists_only_work_a_worker_ran_by_project(capsys):
    store = Store.open()
    ran = store.create_task("fix the radio", status="done", source="locks", repo="/w/hertzbench", kind="bugfix",
                            cost_usd=3.2)
    store.add_attempt(ran["id"], cell="sonnet/high", max_turns=30, passed=1)
    other = store.create_task("add the button", status="done", source="locks", repo="/w/site", kind="ui")
    store.add_attempt(other["id"], cell="sonnet/high", max_turns=30, passed=1)
    store.create_task("never ran", status="cancelled", source="queue", repo="/w/site", kind="ui")
    store.create_task("a prompt", status="done", source="hook", repo="/w/site")
    store.close()
    assert cli.main(["tasks", "--all", "--workers", "--turns"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "hertzbench (/w/hertzbench)" and out[2] == "site (/w/site)"
    assert out[1].startswith(f"#{ran['id']}") and out[1].endswith("fix the radio  (1 attempt(s), $3.20)")
    assert len(out) == 4 and not any("never ran" in x or "(turn)" in x for x in out)


@pytest.mark.parametrize("argv, bad", [
    (["merge", "N"], "'N'"),
    (["merge", "3", "#x"], "'#x'"),
    (["merge", "²"], "'²'"),
    (["pr", "N"], "'N'"),
])
def test_a_command_that_wants_task_ids_refuses_a_word_that_is_none_in_one_line(argv, bad, capsys):
    """A placeholder (`N`) left in a request the shell had run backticks of crashed `cmd_merge` with
    `ValueError: invalid literal for int() with base 10: 'N'`."""
    assert cli.main(argv) == 2
    out, err = capsys.readouterr()
    assert err.strip() == f"locks {argv[0]}: {bad} is not a task number (N or #N)"
    assert out == ""


def test_a_task_number_may_carry_a_hash(git_repo, capsys):
    assert cli.main(["merge", "#999", "--repo", str(git_repo), "--dry-run"]) == 1
    assert "no task #999" in capsys.readouterr().err


def test_split_reads_a_non_ascii_digit_as_a_request_not_a_task(capsys, git_repo):
    cli.main(["split", "²", "--repo", str(git_repo), "--no-model"])  # `int("²")` raised ValueError
    out, err = capsys.readouterr()
    assert "no split" in out
    assert "no task" not in err


def test_queue_add_reads_the_request_from_stdin_as_it_is(git_repo, monkeypatch):
    """Backticks and `$(…)` in a request sent through a quoted heredoc are text: nothing runs them."""
    import io

    text = "fix `locks merge N`, then read `locks guide dispatch` and $(echo hi)\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    assert cli.main(["queue", "add", "-", "--repo", str(git_repo), "--json"]) == 0
    store = Store.open()
    try:
        assert store.list_tasks(status=["queued"], limit=1)[0]["body"] == text
    finally:
        store.close()


@pytest.mark.parametrize("argv", [["run", "-", "--dry-run", "--no-model"], ["queue", "add", "-"]])
def test_a_request_from_an_empty_stdin_is_refused_before_anything_starts(argv, git_repo, monkeypatch, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("  \n"))
    assert cli.main([*argv, "--repo", str(git_repo)]) == 2
    assert "the request read from stdin is empty" in capsys.readouterr().err
    store = Store.open()
    try:
        assert store.list_tasks(limit=5) == []
    finally:
        store.close()


@pytest.mark.parametrize("text", ["", "   ", "#review", "  #fable #plan \n"])
@pytest.mark.parametrize("argv", [["run", "--dry-run", "--no-model"], ["queue", "add"]])
def test_a_request_with_no_task_in_it_is_refused_before_anything_starts(text, argv, git_repo, capsys):
    """An empty or tag-only request is asked for again, never planned and climbed."""
    assert cli.main([argv[0], *argv[1:2], text, *argv[2:], "--repo", str(git_repo)]) == 2
    assert "holds no task" in capsys.readouterr().err
    store = Store.open()
    try:
        assert store.list_tasks(limit=5) == []
    finally:
        store.close()


def test_config_livespec_wait_takes_whole_seconds(capsys, monkeypatch):
    from locks import config

    monkeypatch.delenv("LOCKS_LIVESPEC_WAIT")  # the suite sets it to 0, and the variable outranks the file
    assert cli.main(["config", "livespec_wait", "20"]) == 0
    assert config.livespec_wait(os.environ) == 20
    assert cli.main(["config", "livespec_wait", "soon"]) == 1
    assert "whole seconds" in capsys.readouterr().err
    assert config.livespec_wait(os.environ) == 20


def test_dismissing_a_task_whose_run_died_keeps_its_worktree_work_on_its_branch(git_repo, store, capsys):
    from locks import isolate
    from locks.store import home

    from .conftest import git

    task = store.create_task("fix the cart total", status="interrupted", source="locks", cwd=str(git_repo))
    ws = isolate.prepare(git_repo, task["id"], home(), fresh=True)
    (ws.path / "app.py").write_text("print('fixed')\n")  # the run died before it committed this
    (ws.path / "new.py").write_text("x = 1\n")
    assert cli.main(["dismiss", str(task["id"])]) == 0
    assert "kept what its worktree held on locks/task-" in capsys.readouterr().out
    branch = isolate.branch_for(task["id"])
    assert git(git_repo, "show", f"{branch}:app.py") == "print('fixed')"
    assert git(git_repo, "show", f"{branch}:new.py") == "x = 1"
    assert isolate.UNVERIFIED in git(git_repo, "log", "-1", "--format=%B", branch)
    assert not ws.path.exists() and store.events_of(task["id"], "branch_kept")
    # Nothing left there: a dismissal of a task with no worktree keeps nothing and says nothing.
    other = store.create_task("another", status="blocked", source="locks", cwd=str(git_repo))
    assert cli.main(["dismiss", str(other["id"])]) == 0
    assert "kept what" not in capsys.readouterr().out and not isolate.kept(git_repo, other["id"])


def test_show_never_says_a_running_task_cost_nothing(capsys, monkeypatch):
    """`locks show` printed `cost $0.00 ... final None` for a task 58 minutes into its attempt."""
    from locks import usage

    store = Store.open()
    t = store.create_task("long work", status="running", source="locks", repo="r", kind="feature")
    store.add_event(t["id"], "attempt_started", seq=1, cell="sonnet/high")
    store.close()
    monkeypatch.setattr(usage, "running_cost", lambda *a, **k: 2.5)
    assert cli.main(["show", str(t["id"])]) == 0
    assert "cost $2.50 so far" in (out := capsys.readouterr().out) and "final (running)" in out
    monkeypatch.setattr(usage, "running_cost", lambda *a, **k: None)
    assert cli.main(["show", str(t["id"])]) == 0
    assert "$0.00 + the running attempt's, not read yet" in capsys.readouterr().out


def test_show_prints_a_reading_worker_s_whole_answer(capsys, git_repo):
    store = Store.open()
    t = store.create_task("where is x?", status="done", source="locks", repo="github.com/o/r", kind="explore")
    store.add_attempt(t["id"], cell="haiku/medium", max_turns=30, passed=1, summary="x lives in a.py")
    store.add_event(t["id"], "report", seq=1, passed=True, report={"unparsed": "# where\n\n| a | b |\n|---|---|"})
    other = store.create_task("survey", status="done", source="locks", repo="github.com/o/r", kind="explore")
    store.add_event(other["id"], "report", seq=1, passed=True, report={"project": "x", "flows": []})
    store.close()
    assert cli.main(["show", str(t["id"])]) == 0
    out = capsys.readouterr().out
    assert "--- report (attempt 1)\n# where\n\n| a | b |\n|---|---|" in out
    assert cli.main(["show", str(other["id"])]) == 0
    assert '"project": "x"' in capsys.readouterr().out
