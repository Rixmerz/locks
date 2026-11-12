"""What the CLI, the hooks, the launcher's entry point and their small helpers do on the paths the
rest of the suite leaves alone: each test asserts what the code returns, prints, records or exits
with, never only that it ran."""
from __future__ import annotations

import io
import json
import os
import runpy
import sys
from pathlib import Path

import pytest

from locks import cli, hooks, lazy, link, stops, teams
from locks.store import Store, home
from tests.test_roles import user_roles, write_role

# --- lazy -------------------------------------------------------------------------------


def test_a_lazy_module_that_does_not_exist_fails_as_an_import_would():
    with pytest.raises(ModuleNotFoundError):
        lazy.module("locks_no_such_module_anywhere")


def test_a_top_level_module_loads_on_its_first_attribute_read(tmp_path, monkeypatch):
    (tmp_path / "locks_lazy_probe.py").write_text("import sys\nsys.locks_lazy_probe_ran = True\nVALUE = 7\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "locks_lazy_probe", raising=False)
    try:
        found = lazy.module("locks_lazy_probe")
        assert sys.modules["locks_lazy_probe"] is found
        assert not getattr(sys, "locks_lazy_probe_ran", False)  # nothing ran yet
        assert found.VALUE == 7 and sys.locks_lazy_probe_ran is True
        assert lazy.module("locks_lazy_probe") is found  # the same object every importer gets
    finally:
        sys.modules.pop("locks_lazy_probe", None)
        if hasattr(sys, "locks_lazy_probe_ran"):
            del sys.locks_lazy_probe_ran


# --- __main__ ---------------------------------------------------------------------------


class _Exec(Exception):
    """Raised by a fake `os.execv`: the re-run it asked for, never made."""


@pytest.fixture
def entry(monkeypatch):
    """`python -m locks` run in this process: argv, stdin and the exec it would make faked."""
    execs: list[tuple[str, list[str]]] = []

    def fake_execv(path, argv):
        execs.append((str(path), list(argv)))
        raise _Exec

    monkeypatch.setattr(os, "execv", fake_execv)
    monkeypatch.setattr(link, "LOADED", None)
    for name in ("LOCKS_REEXEC", "LOCKS_FORWARDED", "LOCKS_PYTHON"):
        monkeypatch.setenv(name, "x")  # recorded, so the test's own changes are undone
        monkeypatch.delenv(name)

    def run(*argv: str, stdin: str = "") -> int | None:
        monkeypatch.setattr(sys, "argv", ["locks", *argv])
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        with pytest.raises(SystemExit) as stopped:
            runpy.run_module("locks.__main__", run_name="__main__")
        return stopped.value.code

    run.execs = execs
    return run


def test_an_old_python_reruns_locks_on_a_newer_one_beside_it(entry, monkeypatch):
    from locks import interpreter

    monkeypatch.setattr(sys, "version_info", (3, 9, 0))
    monkeypatch.setattr(interpreter, "newer_python", lambda env, which=None: "/opt/py/python3.12")
    with pytest.raises(_Exec):
        entry("tasks", "--all")
    assert entry.execs == [("/opt/py/python3.12", ["/opt/py/python3.12", "-m", "locks", "tasks", "--all"])]
    assert os.environ["LOCKS_REEXEC"] == "1"


def test_an_old_python_with_none_newer_says_what_is_wrong(entry, monkeypatch, capsys):
    from locks import interpreter

    monkeypatch.setattr(sys, "version_info", (3, 9, 0))
    monkeypatch.setattr(interpreter, "newer_python", lambda env, which=None: None)
    assert entry("tasks") == 1
    assert "locks needs Python 3.11+" in capsys.readouterr().err and entry.execs == []


def test_a_forwarded_command_keeps_the_version_it_came_from_and_never_forwards_again(entry, monkeypatch, capsys):
    monkeypatch.setenv("LOCKS_FORWARDED", "0.1.0")
    monkeypatch.setenv("LOCKS_REEXEC", "1")
    monkeypatch.setattr(link, "forward_to", lambda *a: pytest.fail("a forwarded command forwards again"))
    assert entry("matrix") == 0
    assert link.LOADED == "0.1.0" and "LOCKS_FORWARDED" not in os.environ and "LOCKS_REEXEC" not in os.environ
    assert "opus/high" in capsys.readouterr().out


def test_a_command_from_an_older_install_runs_the_newest_one(entry, monkeypatch):
    from locks import __version__

    monkeypatch.setattr(link, "forward_to", lambda argv, env, own, version: Path("/cache/locks/9.9.9/bin/locks"))
    with pytest.raises(_Exec):
        entry("tasks")
    assert entry.execs == [("/cache/locks/9.9.9/bin/locks", ["/cache/locks/9.9.9/bin/locks", "tasks"])]
    assert os.environ["LOCKS_FORWARDED"] == __version__


def _tool_events(root: Path) -> list[dict]:
    return [json.loads(line) for f in sorted((root / "tool-events").glob("*.ndjson"))
            for line in f.read_text().splitlines()]


def test_the_post_tool_use_fast_path_appends_one_line_and_exits(entry):
    event = {"session_id": "s", "tool_name": "Read", "tool_input": {"file_path": "/x/secret.py"}}
    assert entry("hook", "PostToolUse", stdin=json.dumps(event)) == 0
    logged = _tool_events(home())
    assert len(logged) == 1 and logged[0]["sig"] == "read:.py" and "secret" not in json.dumps(logged)
    assert not (home() / "locks.db").exists()  # no database on the fast path


def test_the_fast_path_fails_open_and_writes_the_error_down(entry):
    assert entry("hook", "PostToolUse", stdin="not json") == 0
    assert "PostToolUse" in (home() / hooks.ERROR_LOG).read_text()


def test_a_delegation_takes_the_full_hook_unless_hooks_are_off(entry, monkeypatch, store):
    hooks.user_prompt_submit({"session_id": "s", "prompt": "review the change", "prompt_id": "p1"}, store)
    hooks.pre_tool_use({"session_id": "s", "tool_name": "Agent", "tool_use_id": "tu1",
                        "tool_input": {"prompt": "review"}}, store)
    event = {"session_id": "s", "tool_name": "Agent", "tool_use_id": "tu1", "tool_input": {},
             "tool_response": {"content": "lgtm"}}

    def child() -> dict:
        return next(t for t in store.list_tasks() if t["source"] == "delegation")

    monkeypatch.setenv("LOCKS_HOOKS_OFF", "1")
    assert entry("hook", "PostToolUse", stdin=json.dumps(event)) == 0
    assert child()["status"] == "running"  # hooks off: only the signature
    monkeypatch.delenv("LOCKS_HOOKS_OFF")
    assert entry("hook", "PostToolUse", stdin=json.dumps(event)) == 0
    assert child()["status"] == "done" and "lgtm" in child()["result"]
    assert len(_tool_events(home())) == 2  # the signature is logged both times


def test_any_other_hook_takes_the_hook_fast_path(entry, capsys):
    assert entry("hook", "NoSuchEvent", stdin="{}") == 0
    assert capsys.readouterr().out == ""


# --- team_cli ---------------------------------------------------------------------------


def test_roles_list_names_the_refused_ones_with_why(capsys):
    write_role(user_roles(), "x", description="d", kinds=["implement"], extends="ghost")
    assert cli.main(["roles", "list"]) == 0
    line = next(r for r in capsys.readouterr().out.splitlines() if r.startswith("x "))
    assert "refused" in line and "no role" in line


def test_roles_show_names_a_skill_it_cannot_find(capsys):
    write_role(user_roles(), "mine", description="d", kinds=["implement"], skills=["ghost-skill"])
    assert cli.main(["roles", "show", "mine"]) == 0
    assert "missing       ghost-skill (not found in any skill folder)" in capsys.readouterr().out


def test_roles_revisions_with_trials_on_says_no_off_notice(capsys, monkeypatch):
    monkeypatch.setenv("LOCKS_ROLE_TRIALS", "on")
    assert cli.main(["roles", "revisions"]) == 0
    out = capsys.readouterr().out
    assert "role trials are off" not in out and out.startswith("no role has a revision yet")


def test_roles_import_of_many_agents_lists_forty_and_counts_the_rest(tmp_path, capsys):
    agents = tmp_path / "agents"
    agents.mkdir()
    for n in range(42):
        (agents / f"a{n:02}.md").write_text(f"---\nname: a{n:02}\ndescription: Reviews code {n}\n---\nReview.\n")
    assert cli.main(["roles", "import", str(agents), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("would import 42 role(s)") and "  and 2 more (`locks roles list`)" in out
    assert "locks team change" not in out  # a dry run writes nothing, so it advises nothing


def test_team_show_names_its_projects_and_a_role_it_lacks(capsys):
    folder = teams.folder()
    folder.mkdir(parents=True)
    (folder / "ok.json").write_text('{"roles": ["tester", "ghost"], "description": "mine"}')
    teams.use("github.com/o/r", "ok")
    assert cli.main(["team", "show", "ok"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ok: mine") and "used by  github.com/o/r" in out
    assert "  - tester" in out and "  - ghost              (no such role)" in out


def test_team_show_of_a_team_no_project_uses_names_none(capsys):
    assert cli.main(["team", "show", "locks"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("locks: ") and "used by" not in out


# --- cli: run, queue, resume, cancel -----------------------------------------------------


@pytest.fixture
def no_kick(monkeypatch):
    """No dispatcher is started: each kick the CLI asks for is recorded."""
    from locks import dispatch

    kicked: list[tuple[str, str | None]] = []
    monkeypatch.setattr(dispatch, "kick", lambda cwd, key, env: kicked.append((str(cwd), key))
                        or "A worker takes it.")
    return kicked


def test_a_run_in_a_session_that_loaded_an_older_locks_says_so_first(git_repo, monkeypatch, capsys):
    monkeypatch.setattr(link, "LOADED", "0.0.1")
    assert cli.main(["run", "where is the router?", "--repo", str(git_repo), "--no-model", "--dry-run"]) == 0
    out, err = capsys.readouterr()
    assert err.startswith("locks: this session loaded locks 0.0.1") and "plan: dry run" in out


def test_a_queued_run_refuses_a_launch_dir(git_repo, capsys, no_kick):
    assert cli.main(["run", "fix the totals", "--repo", str(git_repo), "--detach", "--launch-dir",
                     str(git_repo)]) == 2
    assert "drop --launch-dir" in capsys.readouterr().err and no_kick == []
    assert Store.open().list_tasks() == []


def test_work_queued_after_a_task_of_another_repository_says_which_and_how_long(git_repo, capsys, monkeypatch,
                                                                               no_kick):
    from locks import progress

    store = Store.open()
    other = store.create_task("build the api", status="running", source="locks", repo="github.com/o/api",
                              cwd="/elsewhere")
    store.close()
    monkeypatch.setattr(progress, "expected_line", lambda store, task: "tasks like it took ≈4m here")
    assert cli.main(["run", "use the new api", "--repo", str(git_repo), "--after", str(other["id"])]) == 0
    out = capsys.readouterr().out
    assert f"waits for task #{other['id']} in github.com/o/api" in out
    assert f"#{other['id']} is a task in github.com/o/api" in out
    assert "tasks like it took ≈4m here" in out and len(no_kick) == 1


def test_a_press_for_a_task_whose_directory_is_gone_resumes_from_here(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Store.open()
    gone = store.create_task("x", status="blocked", source="locks", repo="r", cwd=str(tmp_path / "gone"))
    stops.record(store, gone["id"], stops.Stop("permission", "refused", allow=("Bash(npm:*)",)))
    store.close()
    ran = []
    monkeypatch.setattr(cli, "_detach", lambda args: ran.append((args.id, os.getcwd())) or 0)
    assert cli.main(["resume", str(gone["id"]), "--pressed"]) == 0
    assert ran == [(gone["id"], str(tmp_path))]


def test_cancelling_a_running_task_with_no_process_only_raises_its_flag(capsys):
    store = Store.open()
    task = store.create_task("x", status="running", source="locks", repo="r")
    store.close()
    assert cli.main(["cancel", str(task["id"])]) == 0
    assert f"cancel requested for task #{task['id']}" in capsys.readouterr().out
    store = Store.open()
    try:
        assert store.cancel_requested(task["id"]) and store.get_task(task["id"])["status"] == "running"
        assert store.final_event(task["id"]) is None  # no stop recorded: its run says how it ended
    finally:
        store.close()


# --- cli: merge, ci, pr, split -----------------------------------------------------------


def test_merge_outside_a_checkout_says_so(tmp_path, capsys):
    store = Store.open()
    task = store.create_task("x", status="done", source="locks", repo="r", cwd=str(tmp_path))
    store.close()
    assert cli.main(["merge", str(task["id"]), "--repo", str(tmp_path)]) == 1
    assert f"{tmp_path} is no git checkout" in capsys.readouterr().err


def test_ci_names_a_workflow_it_cannot_read_and_says_when_there_is_none(git_repo, capsys):
    assert cli.main(["ci", "--repo", str(git_repo)]) == 0
    assert capsys.readouterr().out == "no CI jobs found in .github/workflows\n"
    folder = git_repo / ".github" / "workflows"
    folder.mkdir(parents=True)
    (folder / "broken.yml").write_text("- just\n- a list\n")
    assert cli.main(["ci", "--repo", str(git_repo)]) == 0
    assert capsys.readouterr().out == ".github/workflows/broken.yml: could not be read\n"


def test_a_pull_request_needs_a_kept_branch_and_gh(git_repo, capsys, monkeypatch):
    import shutil

    from locks import merging
    from tests.test_merging import _kept

    store = Store.open()
    bare = store.create_task("nothing kept", status="done", source="locks", repo="r", cwd=str(git_repo))
    task = _kept(store, git_repo, "add the export", {"export.py": "x\n"})
    store.close()
    assert cli.main(["pr", str(bare["id"])]) == 1
    assert "keeps no branch here" in capsys.readouterr().err

    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: None)
    assert cli.main(["pr", str(task["id"])]) == 1
    out, err = capsys.readouterr()
    assert out.startswith("title: add the export") and f"git push -u origin locks/task-{task['id']}" in err

    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: f"/usr/bin/{name}")
    answers = iter([(True, "https://example.test/pr/1"), (False, "git push failed: no")])
    seen = []
    monkeypatch.setattr(merging, "open_pr", lambda top, branch, title, body, base=None:
                        seen.append((branch, title)) or next(answers))
    assert cli.main(["pr", str(task["id"])]) == 0
    assert capsys.readouterr().out.strip() == "https://example.test/pr/1"
    assert cli.main(["pr", str(task["id"])]) == 1
    assert capsys.readouterr().err.strip() == "git push failed: no"
    assert seen == [(f"locks/task-{task['id']}", "add the export")] * 2


LISTED = ("Build the student side of the platform:\n- the opinion form in src/app/opinion/form.ts\n"
          "- the quiz per module in src/app/quiz/\n- the forum in src/app/forum/forum.ts")


def test_split_of_a_task_that_is_not_there(capsys):
    assert cli.main(["split", "#42"]) == 1
    assert capsys.readouterr().err.strip() == "no task #42"


def test_split_as_json_and_with_a_check_for_every_piece(git_repo, capsys, no_kick):
    assert cli.main(["split", LISTED, "--repo", str(git_repo), "--no-model", "--json"]) == 0
    assert [p["id"] for p in json.loads(capsys.readouterr().out)["parts"]] == ["p1", "p2", "p3"]
    assert cli.main(["split", "one thing only", "--repo", str(git_repo), "--no-model", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["problem"]
    assert cli.main(["split", LISTED, "--repo", str(git_repo), "--no-model", "--queue", "--verify", "npm test"]) == 0
    out = capsys.readouterr().out
    store = Store.open()
    try:
        pieces = store.list_tasks(status=["queued"], limit=10)
        assert len(pieces) == 3 and all(json.loads(p["options"])["verify"] == "npm test" for p in pieces)
    finally:
        store.close()
    assert "leaves the board" not in out and no_kick  # a request, not a task: nothing to dismiss


def test_a_piece_that_builds_on_another_waits_for_it(git_repo, capsys, monkeypatch, no_kick):
    from locks import after, split

    store = Store.open()
    done = store.create_task(LISTED, status="done", source="locks", repo="r", cwd=str(git_repo))
    store.close()
    parts = ({"id": "p1", "prompt": "the model", "surfaces": ["src/model/**"]},
             {"id": "p2", "prompt": "the view on it", "surfaces": ["src/view/**"], "after": "p1"})
    monkeypatch.setattr(split, "propose", lambda text, where, done=(): split.Proposal(parts=parts, source="model"))
    assert cli.main(["split", str(done["id"]), "--queue"]) == 0
    out = capsys.readouterr().out
    store = Store.open()
    try:
        first, second = sorted(store.list_tasks(status=["queued"], limit=10), key=lambda t: t["id"])
        assert after.hold(store.get_task(second["id"])) == {"task": first["id"]}
        assert store.get_task(done["id"])["status"] == "done"  # a pass waits on no one: not dismissed
    finally:
        store.close()
    assert "waited for" not in out and "leaves the board" not in out


# --- cli: dismiss, events, tasks, peek, show ---------------------------------------------


def test_a_dismissal_keeps_going_when_git_cannot_keep_what_was_left(tmp_path, monkeypatch, capsys):
    from locks import isolate

    def broken(*a, **kw):
        raise isolate.IsolationError("git refused")

    monkeypatch.setattr(isolate, "keep_left", broken)
    store = Store.open()
    task = store.create_task("x", status="blocked", source="locks", repo="r", cwd=str(tmp_path))
    store.close()
    assert cli.main(["dismiss", str(task["id"])]) == 0
    out = capsys.readouterr().out
    assert f"dismissed task #{task['id']}" in out and "kept what" not in out
    store = Store.open()
    try:
        assert store.get_task(task["id"])["status"] == "dismissed" and not store.events_of(task["id"], "branch_kept")
    finally:
        store.close()


def test_events_follow_prints_what_came_and_stops_on_ctrl_c(monkeypatch, capsys):
    from locks import cli as cli_module

    store = Store.open()
    task = store.create_task("x", status="running", source="locks", repo="r")
    store.add_event(task["id"], "planned", kind="docs")
    store.close()
    slept = []

    def interrupted(seconds):
        slept.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module.time, "sleep", interrupted)
    assert cli.main(["events", "--follow"]) == 0
    assert f"#{task['id']} planned" in capsys.readouterr().out and slept == [1]


def _running_in_worktree(store: Store, git_repo: Path, body: str, *, repo_key: str = "r") -> dict:
    """A running task whose worker wrote a file in its own worktree."""
    from tests.conftest import git

    task = store.create_task(body, status="running", source="locks", repo=repo_key, kind="implement",
                             cwd=str(git_repo))
    base = git(git_repo, "rev-parse", "HEAD")
    store.add_event(task["id"], "planned", kind="implement")
    store.add_event(task["id"], "attempt_started", seq=1, base=base)
    store.add_attempt(task["id"], cell="sonnet/medium", max_turns=30, passed=0)
    path = home() / "worktrees" / f"task-{task['id']}"
    git(git_repo, "worktree", "add", "-q", "-b", f"locks/task-{task['id']}", str(path))
    (path / "logout.py").write_text("def logout():\n    return True\n")
    return task


def test_tasks_by_worker_groups_projects_and_names_the_files_a_running_one_wrote(git_repo, capsys):
    store = Store.open()
    try:
        running = _running_in_worktree(store, git_repo, "add a logout button", repo_key="github.com/o/web")
        other = store.create_task("fix the api", status="done", source="locks", repo="github.com/o/api")
        store.add_attempt(other["id"], cell="haiku", max_turns=30, passed=1, cost_usd=0.5)
    finally:
        store.close()
    assert cli.main(["tasks", "--all", "--workers"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("api (github.com/o/api)") and out[1].startswith(f"#{other['id']}")
    assert out[1].endswith("fix the api  (1 attempt(s), $0.50)")
    web = out.index(next(x for x in out if x.startswith("web (github.com/o/web)")))
    assert out[web + 1].startswith(f"#{running['id']}") and "       wrote logout.py" in out


def test_peek_of_a_task_that_is_not_there_or_has_no_worktree(capsys, store):
    assert cli.main(["peek", "77"]) == 1
    assert capsys.readouterr().err.strip() == "no task #77"
    task = store.create_task("x", status="running", source="locks", repo="r")
    store.add_event(task["id"], "attempt_started", seq=1)
    assert cli.main(["peek", str(task["id"])]) == 1
    assert "works in no worktree of its own" in capsys.readouterr().err


def test_peek_stat_of_nothing_written_and_a_diff_cut_at_its_bound(git_repo, capsys, monkeypatch, store):
    from locks import progress
    from tests.conftest import git

    task = store.create_task("x", status="running", source="locks", repo="r", cwd=str(git_repo))
    base = git(git_repo, "rev-parse", "HEAD")
    store.add_event(task["id"], "attempt_started", seq=1, base=base)
    path = home() / "worktrees" / f"task-{task['id']}"
    git(git_repo, "worktree", "add", "-q", "-b", f"locks/task-{task['id']}", str(path))
    assert cli.main(["peek", str(task["id"]), "--stat"]) == 0
    assert capsys.readouterr().out == "nothing written yet\n"
    (path / "big.py").write_text("x = 1\n" * 200)
    monkeypatch.setattr(progress, "PEEK_BYTES", 100)
    assert cli.main(["peek", str(task["id"])]) == 0
    out = capsys.readouterr().out
    assert f"… cut at 100 bytes; the whole of it: git -C {path} diff {base}" in out


def test_show_of_a_running_task_says_how_far_it_got_and_what_was_asked(git_repo, capsys):
    store = Store.open()
    try:
        task = _running_in_worktree(store, git_repo, "Do these:\n- add a logout button\n- add its test")
    finally:
        store.close()
    assert cli.main(["show", str(task["id"])]) == 0
    out = capsys.readouterr().out
    assert "now: wrote logout.py" in out
    assert "  asked S1: add a logout button" in out and "  asked S2: add its test" in out
    assert f"  its diff so far: locks peek {task['id']}" in out


# --- cli: overview, spend, allow, lanes, work --------------------------------------------


def test_overview_in_words_names_a_session_s_last_prompt_and_work_no_session_sent(tmp_path, capsys):
    from locks import repo

    shop = tmp_path / "shop"
    (shop / ".locks").mkdir(parents=True)
    key = repo.key(shop)
    store = Store.open()
    try:
        store.touch_session("s1", str(shop), key)
        hooks.user_prompt_submit({"session_id": "s1", "prompt": "make the cart faster", "prompt_id": "p1",
                                  "cwd": str(shop)}, store)
    finally:
        store.close()
    assert cli.main(["overview"]) == 0
    out = capsys.readouterr().out
    assert "  last prompt: make the cart faster" in out and "Work no listed session sent" not in out
    store = Store.open()
    try:
        store.enqueue("tidy the api", repo=key, cwd=str(shop))
    finally:
        store.close()
    assert cli.main(["overview"]) == 0
    out = capsys.readouterr().out
    assert "Work no listed session sent" in out and "tidy the api" in out


def test_spend_names_what_the_sessions_cost_by_model(tmp_path, capsys):
    from locks import usage
    from tests.test_usage import line

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(line("m1", out=1000))
    store = Store.open()
    try:
        usage.ingest(store, transcript, session_id="s", task_id=None, repo=None)
    finally:
        store.close()
    assert cli.main(["spend", "--all", "--days", "100000"]) == 0
    row = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("  opus"))
    assert "1000 out" in row and " in  $" in row


def test_allow_needs_a_directory_to_key_its_rules_to(tmp_path, capsys):
    assert cli.main(["allow", "Bash(ls:*)", "--repo", str(tmp_path / "nowhere")]) == 1
    assert "is no repository locks can key rules to" in capsys.readouterr().err


def test_lanes_beside_refuses_work_held_after_other_work(git_repo, capsys):
    store = Store.open()
    try:
        ahead = store.create_task("build the api", status="running", source="locks", repo="r", cwd=str(git_repo))
        held = store.enqueue("use it", repo="r", cwd=str(git_repo), options={"after": {"task": ahead["id"]}})
    finally:
        store.close()
    assert cli.main(["lanes", "--beside", str(held["id"])]) == 1
    err = capsys.readouterr().err
    assert f"task #{held['id']} waits for #{ahead['id']}" in err and "`--after` is its order" in err


def test_a_dispatcher_at_its_limit_that_hands_nothing_on_says_only_why_it_stopped(git_repo, monkeypatch, capsys):
    from locks import dispatch
    from locks.flow import WorkReport

    monkeypatch.setattr(cli.flow, "work", lambda store, **kw: WorkReport([], "started 10 tasks", at_limit=True))
    monkeypatch.setattr(dispatch, "start_lane", lambda store, root, key: False)
    assert cli.main(["work", "--repo", str(git_repo), "--detached"]) == 0
    assert capsys.readouterr().out.strip() == "started 10 tasks"


def test_the_background_index_refresh_says_what_it_did(tmp_path, monkeypatch, capsys):
    from locks.adapters.livespec import Livespec

    monkeypatch.setattr(Livespec, "refresh", lambda self, where: f"indexed {where.name}")
    assert cli.main(["index-livespec", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == f"indexed {tmp_path.name}"


def test_naming_a_session_runs_once_at_a_time(monkeypatch, capsys):
    from locks import dispatch, naming

    names = iter(["Cart speed", None])
    monkeypatch.setattr(naming, "name_session", lambda store, sid, **kw: next(names))
    assert cli.main(["name-session", "s1"]) == 0
    assert capsys.readouterr().out.strip() == "Cart speed"
    assert cli.main(["name-session", "s1"]) == 0
    assert capsys.readouterr().out.strip() == "unchanged"
    with dispatch.hold(home(), "name:s1"):
        assert cli.main(["name-session", "s1"]) == 0
    assert capsys.readouterr().out == ""  # another process names it


def test_tasks_by_worker_in_one_project_lists_no_project_headers(git_repo, capsys):
    from locks import repo

    store = Store.open()
    try:
        t = store.create_task("fix the api", status="done", source="locks", repo=repo.key(git_repo))
        store.add_attempt(t["id"], cell="haiku", max_turns=30, passed=1, cost_usd=0.25)
    finally:
        store.close()
    assert cli.main(["tasks", "--workers", "--repo", str(git_repo)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1 and out[0].startswith(f"#{t['id']}") and out[0].endswith("(1 attempt(s), $0.25)")


# --- cli: notes, workspaces --------------------------------------------------------------


def test_notes_outside_any_directory_stop_with_why(tmp_path):
    with pytest.raises(SystemExit, match="is not a directory"):
        cli.main(["notes", "--repo", str(tmp_path / "nowhere")])


def test_notes_show_a_text_longer_than_its_title_and_a_topic_haiku_proposed(git_repo, capsys):
    from locks import repo

    key = repo.key(git_repo)
    store = Store.open()
    try:
        store.add_note(key, "code", "Cents", "Money is kept in integer cents everywhere.", author="person",
                       filed_by="person", proposed_topic="billing")
    finally:
        store.close()
    assert cli.main(["notes", "--repo", str(git_repo)]) == 0
    out = capsys.readouterr().out
    assert "] Cents\n  Money is kept in integer cents everywhere." in out
    assert cli.main(["notes", "--repo", str(git_repo), "topics"]) == 0
    assert "  proposed topic 'billing' for #1 Cents" in capsys.readouterr().out


def test_extract_notes_outside_any_directory_files_nothing(tmp_path, capsys):
    assert cli.main(["extract-notes", "s1", str(tmp_path / "t.jsonl"), str(tmp_path / "nowhere")]) == 0
    assert capsys.readouterr().out == ""


def test_workspace_list_as_json_and_when_there_is_none(capsys):
    assert cli.main(["workspace", "list"]) == 0
    assert capsys.readouterr().out.startswith("no workspace: `locks workspace add")
    assert cli.main(["workspace", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_a_workspace_keeps_a_member_whose_checkout_is_gone_when_another_joins(tmp_path, capsys):
    import shutil

    from tests.test_workspaces import checkout

    folder = tmp_path / "shop"
    checkout(folder / "api")
    checkout(folder / "web")
    assert cli.main(["workspace", "add", str(folder)]) == 0
    shutil.rmtree(folder / "web")
    checkout(folder / "db")
    capsys.readouterr()
    assert cli.main(["workspace", "add", str(folder), "--with", str(folder / "db")]) == 0
    said = capsys.readouterr().out
    assert said.startswith(f"workspace {folder.resolve()}: ") and "web (" in said and "db (" in said


# --- cli: habits, ui, endings, compaction, hook, unknown words ---------------------------


def _habit_events(store: Store, repo_key: str, cwd: str, sessions: int = 3) -> None:
    for n in range(sessions):
        sid = f"{repo_key}-{n}"
        store.touch_session(sid, cwd, repo_key)
        for sig in ("edit:.ts", "bash:npm-run"):
            store.add_tool_event(session_id=sid, tool="x", sig=sig, arg_hash="h", ok=1)


def test_habits_list_offers_what_other_repositories_do_apart(tmp_path, capsys):
    node = tmp_path / "node"
    node.mkdir()
    (node / "package.json").write_text("{}")
    store = Store.open()
    try:
        _habit_events(store, "web", "/web")
        _habit_events(store, "api", "/api", sessions=2)
    finally:
        store.close()
    assert cli.main(["habits", "list", "--repo", str(node)]) == 0
    out = capsys.readouterr().out
    assert "  no repeated sequence passes the gates here yet" in out
    assert "or more other repositories, not here yet" in out and "edit:.ts → bash:npm-run" in out


def test_habits_install_outside_a_repository_takes_a_habit_of_every_repository(tmp_path, capsys):
    store = Store.open()
    try:
        for s in range(3):
            for sig in ("edit:.py", "bash:ruff-format"):
                store.add_tool_event(session_id=f"s{s}", tool="x", sig=sig, arg_hash="h", ok=1)
    finally:
        store.close()
    assert cli.main(["habits", "list", "--all"]) == 0
    cid = capsys.readouterr().out.splitlines()[1].split()[0]
    missing = tmp_path / "missing"  # a typo is refused, never made a directory with settings in it
    assert cli.main(["habits", "install", cid, "--command", "true", "--repo", str(missing)]) == 2
    assert "is not a directory" in capsys.readouterr().err and not missing.exists()
    fresh = tmp_path / "fresh"  # a folder that is no repository: no project to key a habit of its own to
    fresh.mkdir()
    assert cli.main(["habits", "install", cid, "--command", "true", "--repo", str(fresh)]) == 0
    assert capsys.readouterr().out.startswith("installed habit #1: after edit:.py, run `true`")
    assert "PostToolUse" in (fresh / ".claude" / "settings.local.json").read_text()


def test_habits_review_queues_one_reader_for_a_repository_s_new_habits(git_repo, tmp_path, monkeypatch, capsys):
    from locks import dispatch, repo

    assert cli.main(["habits", "review", "--repo", str(tmp_path / "nowhere")]) == 1
    assert capsys.readouterr().err.strip() == "not in a repository"
    assert cli.main(["habits", "review", "--repo", str(git_repo)]) == 0
    assert capsys.readouterr().out.startswith("no habit here that a review has not seen")
    (git_repo / "package.json").write_text("{}")
    store = Store.open()
    try:
        _habit_events(store, repo.key(git_repo), str(git_repo))
    finally:
        store.close()
    started = []
    monkeypatch.setattr(dispatch, "start", lambda where, root, key: started.append((str(where), key)) or True)
    assert cli.main(["habits", "review", "--repo", str(git_repo)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("queued #") and "[habits review]" in out
    assert started == [(str(git_repo.resolve()), repo.key(git_repo))]
    assert (git_repo / ".locks").is_dir()  # the project is enrolled


def test_ui_stops_itself_when_asked_once_no_tab_is_open(monkeypatch, capsys):
    import threading

    from locks.ui import server

    asked = threading.Event()
    seen = []

    class Fake:
        server_address = ("127.0.0.1", 4321)
        stop_event = threading.Event()

        def exit_when_idle(self, seconds):
            seen.append(seconds)
            asked.set()

        def serve_forever(self):
            assert asked.wait(5)
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(server, "serve", lambda port: Fake())
    assert cli.main(["ui", "--idle-exit", "30"]) == 0
    assert "it stops 30s after its last tab closes" in capsys.readouterr().out and seen == [30.0]


def test_ui_already_served_by_this_locks_opens_nothing_unless_asked(monkeypatch, capsys):
    import errno

    from locks import __version__
    from locks.ui import server

    def taken(port):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(server, "serve", taken)
    monkeypatch.setattr(server, "occupant", lambda port: {"locks": True, "version": __version__})
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail("opened a tab nobody asked for"))
    assert cli.main(["ui", "--port", "8791"]) == 0
    assert capsys.readouterr().out.strip() == f"locks UI {__version__} is already at http://127.0.0.1:8791/"


def test_endings_in_words_print_the_notice(capsys):
    store = Store.open()
    try:
        mine = store.create_task("write docs", status="running", source="locks", session_id="s1", final_cell="haiku")
        store.add_event(mine["id"], "finished", status="done", final_cell="haiku")
        store.update_task(mine["id"], status="done", result="wrote them")
    finally:
        store.close()
    assert cli.main(["endings", "--session", "s1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(hooks.ENDINGS_HEADER) and f"#{mine['id']} [done] write docs" in out
    assert cli.main(["endings", "--session", "s1"]) == 0 and capsys.readouterr().out == ""  # told once


def test_a_worker_never_takes_a_compaction(monkeypatch, capsys):
    monkeypatch.setenv("LOCKS_WORKER_TASK", "7")
    assert cli.main(["compaction", "next", "--session", "s1"]) == 0
    assert capsys.readouterr().out == ""
    assert not (home() / "locks.db").exists()  # it never opened the store
    assert cli.main(["compaction", "next", "--session", "s1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"action": None}


def test_the_hook_command_answers_a_hook_and_never_raises(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert cli.main(["hook", "Stop"]) == 0
    assert capsys.readouterr().out == "" and "Stop" in (home() / hooks.ERROR_LOG).read_text()


def test_more_words_than_a_command_takes_say_to_quote_them(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["show", "1", "and", "more"])
    assert stopped.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments: and more" in err and "more words than it takes: quote a text" in err


# --- hooks: a prompt ---------------------------------------------------------------------


def _prompt(store: Store, text: str, prompt_id: str = "p1", session: str = "s", cwd: str | None = None):
    return hooks.user_prompt_submit({"session_id": session, "prompt": text, "prompt_id": prompt_id, "cwd": cwd},
                                    store)


def _context_of(answer: dict | None) -> str:
    return ((answer or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")


def test_a_dead_end_says_what_worked_instead_when_something_did(store):
    p = store.open_problem("websocket reconnect loop", repo="github.com/x/y")
    store.add_fix(p, "raise the backoff to 30s", "failed", repo="github.com/x/y", why="loop is client side")
    store.add_fix(p, "debounce the reconnect on the client", "worked", repo="github.com/x/y")
    context = _context_of(_prompt(store, "the websocket reconnect loop is back in the dashboard"))
    assert "tried raise the backoff to 30s; failed: loop is client side; what worked: debounce the reconnect" \
        in context


def test_a_queued_prompt_with_no_directory_is_queued_for_locks_work(store):
    answer = _prompt(store, "++ write the release notes")
    assert answer["decision"] == "block"
    assert answer["reason"].startswith("locks: queued #1 — write the release notes. `locks work` runs the queue.")
    task = store.get_task(1)
    assert task["status"] == "queued" and task["cwd"] is None


def test_a_bare_continue_after_a_turn_that_ended_well_is_a_turn_of_its_own(store):
    _prompt(store, "continue", prompt_id="p0")  # nothing before it: a turn of its own
    hooks.stop({"session_id": "s", "prompt_id": "p0", "last_assistant_message": "ok"}, store)
    _prompt(store, "continue", prompt_id="p1")
    first, second = sorted(store.list_tasks(), key=lambda t: t["id"])
    assert first["status"] == "done" and second["status"] == "running" and second["prompt_id"] == "p1"


def test_a_prompt_no_note_speaks_to_gets_no_notes_block(store, git_repo, monkeypatch):
    from locks import project

    monkeypatch.setenv("LOCKS_NOTES", "on")
    monkeypatch.setenv("LOCKS_ROUTE", "off")
    project.enroll(git_repo)
    answer = _prompt(store, "rename the login button label", cwd=str(git_repo))
    assert "locks notes" not in _context_of(answer)
    assert not store.events_of(store.list_tasks()[0]["id"], "notes_shown")


def test_a_prompt_in_a_session_behind_the_newest_install_hears_of_it(store):
    from locks import __version__
    from tests.test_link import _plugin

    _plugin(Path(os.environ["CLAUDE_CONFIG_DIR"]), "999.0.0")
    context = _context_of(_prompt(store, "rename the button"))
    assert context.startswith(f"locks: locks 999.0.0 is installed, and this session loaded {__version__}")


# --- hooks: what never costs a turn ------------------------------------------------------


def _errors() -> str:
    log = home() / hooks.ERROR_LOG
    return log.read_text() if log.is_file() else ""


def test_older_installs_are_named_again_when_the_set_changes(tmp_path):
    from locks import __version__
    from tests.test_link import _plugin

    config = tmp_path / "config"
    env = {"CLAUDE_CONFIG_DIR": str(config)}
    _plugin(config, __version__)
    _plugin(config, "0.1.0")
    assert "(0.1.0)" in hooks._older_installs(tmp_path, env)
    _plugin(config, "0.2.0")
    assert "(0.1.0, 0.2.0)" in hooks._older_installs(tmp_path, env)
    assert hooks._older_installs(tmp_path, env) is None


def test_reading_installs_that_fails_costs_nothing_but_a_note(monkeypatch, tmp_path, store):
    def broken(*a, **kw):
        raise RuntimeError("cache unreadable")

    monkeypatch.setattr(link, "installed", broken)
    monkeypatch.setattr(link, "record_loaded", broken)
    assert hooks._older_installs(home(), {}) is None
    assert hooks._newer_plugin(home(), "s", {}) is None
    hooks._record_loaded(home(), "s")
    errors = _errors()
    assert "older installs" in errors and "newer plugin" in errors and "loaded plugin" in errors
    assert errors.count("RuntimeError: cache unreadable") == 3
    # and the prompt that ran into it is still a task
    assert _prompt(store, "rename the button") is None and len(store.list_tasks()) == 1


def test_a_landing_that_fails_is_noted(monkeypatch, git_repo, store):
    def broken(*a, **kw):
        raise RuntimeError("git gone")

    monkeypatch.setattr(stops, "landed", broken)
    hooks._landed(store, git_repo, home())
    assert "landed" in _errors() and "git gone" in _errors()


def test_the_orchestrator_adopts_quiet_sessions_work_and_hears_it_at_its_start(store, git_repo, monkeypatch):
    from locks import orchestrator, project, repo
    from tests.test_orchestrator import _old

    key = repo.key(git_repo)
    folder = project.enroll(git_repo)
    orchestrator.claim(folder, "me", str(git_repo), via="sent work")
    store._conn.execute("INSERT INTO sessions (id, cwd, repo, started_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                        ("old", str(git_repo), key, _old(30), _old(20)))
    left = store.create_task("x", status="blocked", source="locks", repo=key, session_id="old")
    said = hooks._orchestrates(store, "me", str(git_repo))
    assert said.endswith("1 task(s) sent by a session that ended or went quiet report here now.")
    assert store.get_task(left["id"])["session_id"] == "me"
    other = store.create_task("y", status="blocked", source="locks", repo=key, session_id="old")
    assert hooks._adopt(store, "me", str(git_repo)) == 1 and store.get_task(other["id"])["session_id"] == "me"
    context = _context_of(hooks.session_start({"session_id": "me", "cwd": str(git_repo)}, store, home(), {},
                                              adapters=[]))
    assert "this session orchestrates this project" in context

    def unreadable(*a, **kw):
        raise OSError("no .locks")

    monkeypatch.setattr(orchestrator, "is_it", unreadable)
    monkeypatch.setattr(orchestrator, "started", unreadable)
    assert hooks._adopt(store, "me", str(git_repo)) == 0
    assert hooks._orchestrates(store, "me", str(git_repo)) is None


def test_a_habits_review_is_queued_once_a_day_and_its_failure_is_noted(store, git_repo, monkeypatch):
    from locks import habits

    queued = []
    monkeypatch.setattr(habits, "queue_review", lambda store, **kw: queued.append(kw) or None)
    on = {"LOCKS_HABIT_REVIEW": "on", "LOCKS_AUTOWORK": "on"}
    hooks._review_habits(store, home(), {"LOCKS_HABIT_REVIEW": "off"}, "s", str(git_repo), "k")
    assert queued == []
    hooks._review_habits(store, home(), on, "s", str(git_repo), "k")
    hooks._review_habits(store, home(), on, "s", str(git_repo), "k")  # mined today already
    assert [(q["repo"], q["cwd"], q["session_id"]) for q in queued] == [("k", str(git_repo), "s")]

    def broken(store, **kw):
        raise RuntimeError("mining broke")

    monkeypatch.setattr(habits, "queue_review", broken)
    hooks._review_habits(store, home(), on, "s", str(git_repo), "k2")
    assert "SessionStart (habits)" in _errors() and "mining broke" in _errors()


def test_a_relay_with_no_transcript_and_no_work_takes_the_role_with_the_thread(store, git_repo):
    from locks import orchestrator, project, scribe

    folder = project.enroll(git_repo)
    orchestrator.claim(folder, "old", str(git_repo), via="sent work")
    state = {**scribe.fresh("old", Path("unused")), "transcript": "",
             "requests": [{"text": "Plan the cart migration to cents"}]}
    scribe.save(home(), state)
    scribe.mark_relay(home(), "old", git_repo)
    event = {"session_id": "new", "cwd": str(git_repo), "source": "clear"}
    context = _context_of(hooks.session_start(event, store, home(), {}, adapters=[]))
    assert "locks relay: this session continues old" in context and "Plan the cart migration to cents" in context
    assert "locks task(s) that session sent" not in context
    assert orchestrator.is_it(folder, "new") and scribe.load(home(), "new") is None


def test_post_tool_use_of_any_other_tool_or_no_session_is_nothing(store):
    assert hooks.post_tool_use({"session_id": "s", "tool_name": "Bash"}, store) is None
    assert hooks.post_tool_use({"tool_name": "Agent", "tool_use_id": "t"}, store) is None


def test_a_turn_that_failed_with_no_turn_running_changes_nothing(store):
    _prompt(store, "deploy staging")
    hooks.stop({"session_id": "s", "prompt_id": "p1", "last_assistant_message": "done"}, store)
    assert hooks.stop_failure({"session_id": "s", "error": "rate limited"}, store) is None
    assert store.list_tasks()[0]["status"] == "done"


# --- hooks: a session's start ------------------------------------------------------------


def test_a_session_start_names_older_installs_left_in_the_cache(store):
    from locks import __version__
    from tests.test_link import _plugin

    config = Path(os.environ["CLAUDE_CONFIG_DIR"])
    _plugin(config, __version__)
    _plugin(config, "0.1.0")
    context = _context_of(hooks.session_start({"session_id": "s"}, store, home(), {}, adapters=[]))
    assert "older locks installs remain in Claude Code's plugin cache (0.1.0)" in context


def test_a_session_start_names_what_the_doctor_finds_stuck(store, git_repo, monkeypatch):
    from locks import doctor

    found = doctor.Finding("x", doctor.BROKEN, "task #3 runs with no process", "locks cancel 3")
    suspect = doctor.Finding("y", "suspect", "a slow one", "look")
    monkeypatch.setattr(doctor, "check", lambda store, repo=None, days=1: [found, suspect])
    context = _context_of(hooks.session_start({"session_id": "s", "cwd": str(git_repo)}, store, home(), {},
                                              adapters=[]))
    assert "locks doctor: work in this repository is stuck" in context
    assert "- task #3 runs with no process → locks cancel 3" in context and "a slow one" not in context


def test_a_session_start_with_notes_on_and_none_kept_says_no_index(store, git_repo, monkeypatch):
    from locks import project

    monkeypatch.setenv("LOCKS_NOTES", "on")
    project.enroll(git_repo)
    context = _context_of(hooks.session_start({"session_id": "s", "cwd": str(git_repo)}, store, home(), {},
                                              adapters=[]))
    assert "notes" not in context.lower()


def test_freshen_reads_the_neighbours_from_the_settings_when_none_are_given(tmp_path):
    assert hooks._freshen(tmp_path, tmp_path, {"LOCKS_LIVESPEC": "off", "LOCKS_ENGRAM": "off"}, None) == []


def test_main_answers_a_session_start_on_stdout(tmp_path, git_repo):
    root = tmp_path / "h"
    root.mkdir()
    (root / hooks.ERROR_LOG).write_text("--- a Stop\nboom\n")
    out = io.StringIO()
    env = {"LOCKS_HOME": str(root), "LOCKS_LIVESPEC": "off", "LOCKS_ENGRAM": "off"}
    assert hooks.main("SessionStart", io.StringIO(json.dumps({"session_id": "s"})), out, env) == 0
    answer = json.loads(out.getvalue())
    assert answer["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "1 hook error(s)" in answer["hookSpecificOutput"]["additionalContext"]


def test_a_note_that_cannot_be_written_is_lost_quietly(tmp_path):
    root = tmp_path / "file"
    root.write_text("not a directory")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        hooks.note_error(root, "Stop")  # never raises
    assert root.read_text() == "not a directory"


def test_registry_notices_that_cannot_be_kept_are_not_said(tmp_path):
    root = tmp_path / "h"
    root.mkdir()
    (root / "capabilities.json").write_text(json.dumps({
        "layout-inspector": {"server": {"command": "layout-inspector"}, "when": ["implement"]}}))
    (root / hooks.REGISTRY_SAID).mkdir()  # a directory where the record goes
    assert hooks._registry_notices(root) is None


def test_an_error_count_that_cannot_be_read_counts_none(tmp_path):
    (tmp_path / hooks.ERROR_LOG).write_text("--- a Stop\nboom\n")
    (tmp_path / (hooks.ERROR_LOG + ".reported")).write_text("not a number")
    assert hooks._drain_errors(tmp_path) == 0


# --- hooks: endings ----------------------------------------------------------------------


def _ended(store: Store, title: str, status: str, session: str = "s", **data) -> dict:
    task = store.create_task(title, status="running", source="locks", session_id=session, repo="r")
    store.add_event(task["id"], "finished", status=status, **data)
    store.update_task(task["id"], status=status)
    return store.get_task(task["id"])


def test_endings_are_delivered_once(store):
    task = _ended(store, "write docs", "done", final_cell="haiku")
    shown = hooks._endings(store, "s", via="stop")
    assert shown is not None and f"#{task['id']} [done] write docs" in shown
    assert hooks._endings(store, "s", via="stop") is None


def test_a_loop_that_cannot_be_read_never_costs_the_endings(store, monkeypatch):
    from locks import loops

    def broken(session_id):
        raise RuntimeError("diary unreadable")

    monkeypatch.setattr(loops, "endings", broken)
    task = _ended(store, "write docs", "done", final_cell="haiku")
    assert f"#{task['id']} [done] write docs" in hooks._endings(store, "s", via="stop")


def test_a_habits_review_whose_verdict_is_text_or_options_are_broken(store, monkeypatch):
    from locks import habits

    assert hooks.habit_lines({"options": "{broken", "status": "done"}) == []
    task = {"options": json.dumps({"habit_review": ["c1"]}), "status": "done", "finished_data": "{}", "result": ""}
    assert hooks.habit_lines(task) == ["  habits review: nothing worth a hook (0 skipped)",
                                       "    not answered: c1 (`locks habits` lists them)"]

    def broken(*a, **kw):
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(habits, "apply_reviews", broken)
    review = store.create_task("[habits review]", status="done", source="queue", repo="r",
                               options=json.dumps({"habit_review": ["c1"]}))
    lines = hooks.habit_lines({**review, "finished_data": {}}, store)
    assert lines[0].startswith("  habits review: nothing worth a hook")
    assert "endings (habits)" in _errors()


def test_a_pass_whose_check_was_flaky_says_which_tests(store):
    task = _ended(store, "fix the totals", "done", final_cell="haiku")
    store.add_event(task["id"], "check_flaky", command="npm test", failures=["totals adds", "totals rounds"])
    text, _ = hooks.endings(store, "s", claim="stop")
    assert "  flaky: `npm test` failed 2 test(s) once, and they passed when it ran again: totals adds; " \
           "totals rounds" in text


def test_an_ending_with_no_account_of_its_stop_says_no_next_step(store):
    task = _ended(store, "old work", "dismissed")  # an older record: no account kept
    text, ids = hooks.endings(store, "s", claim="stop")
    assert ids == [task["id"]] and f"#{task['id']} [dismissed] old work" in text
    assert "stopped by" not in text and "next (" not in text


def test_an_ending_says_a_lane_of_the_session_s_is_paused_after_the_endings(store):
    task = _ended(store, "write docs", "done", final_cell="haiku")
    store.enqueue("more docs", repo="r2", cwd="/x", session_id="s")
    store.pause_lane("r2", "task #999 stopped for a person")
    text, _ = hooks.endings(store, "s", claim="stop")
    head, paused = text.split("\n\nlocks: a lane holding work you queued is paused")
    assert f"#{task['id']} [done] write docs" in head and "r2: 1 queued task(s) of yours wait" in paused


def test_a_verdict_summary_with_no_independent_check_says_nothing_of_one():
    said = hooks.verdict_summary({"check": {"command": "pytest -q"}, "criteria": [{"met": True}],
                                  "independent": {}})
    assert said == "locks' check `pytest -q` passed, 1/1 criteria met"


def test_what_moved_since_is_empty_when_git_cannot_say(store, git_repo, monkeypatch):
    from locks import isolate

    def broken(*a, **kw):
        raise RuntimeError("git cannot say")

    monkeypatch.setattr(isolate, "moved_since", broken)
    task = store.create_task("x", status="done", source="locks", cwd=str(git_repo))
    store.add_event(task["id"], "finished", status="done", base="abc")
    assert hooks._moved_since(store, store.get_task(task["id"]), [store.get_task(task["id"])]) == set()


def test_overlaps_count_the_tasks_past_the_shown_ones_and_survive_git_failing(store, git_repo, monkeypatch):
    from locks import isolate

    def broken(*a, **kw):
        raise RuntimeError("git cannot say")

    monkeypatch.setattr(isolate, "kept_branches", broken)
    for n in range(hooks.OVERLAP_SHOWN + 1):
        running = store.create_task(f"beside {n}", status="running", source="locks", repo="r", cwd=str(git_repo))
        store.add_attempt(running["id"], cell="sonnet/medium", max_turns=40, passed=0, changed_paths=["src/a.ts"])
    mine = store.create_task("mine", status="done", source="locks", repo="r", cwd=str(git_repo))
    lines = hooks._overlaps(store, {**mine, "finished_data": {"changed": ["src/a.ts"]}})
    assert len([x for x in lines if "(running now)" in x]) == hooks.OVERLAP_SHOWN
    assert lines[-1] == "    and 1 more task(s)"


def test_a_step_that_ended_unheard_and_unreported_is_no_step_the_engine_went_past(store):
    run = store.create_run(name="feature", scope="bundled", source="x", definition="{}", repo="r",
                           session_id="s", status="active")
    first = store.create_task("plan", status="running", source="queue", session_id="s", repo="r",
                              workflow_run=run["id"], workflow_step="plan")
    store.add_event(first["id"], "planned", warnings=["a browser is needed"])
    store.add_event(first["id"], "finished", status="done")
    second = _ended(store, "build", "done")
    store._conn.execute("UPDATE tasks SET workflow_run = ?, workflow_step = 'build' WHERE id = ?",
                        (run["id"], second["id"]))
    ended = {**store.get_task(second["id"]), "finished_id": store.final_event(second["id"])["id"]}
    assert hooks._went_past(store, ended) == []


def test_a_limit_wait_whose_reset_has_passed_resumes_at_once(monkeypatch, capsys):
    import argparse
    import time as clock

    from locks import dispatch

    monkeypatch.setattr(dispatch, "recover", lambda *a, **kw: [])
    monkeypatch.setattr(dispatch, "revive", lambda *a, **kw: [])
    slept = []
    assert cli.cmd_limit_wait(argparse.Namespace(until=clock.time() - 60), sleep=slept.append) == 0
    assert slept == [] and "resumed nothing; started 0 dispatcher(s)" in capsys.readouterr().out
