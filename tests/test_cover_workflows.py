"""The corners of workflows, workflow_cli, after, loops and loop_cli the scenario tests do not
reach: each test asserts what the code returns, records or prints there."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import pytest

from locks import after, cli, loops, stops, workflows
from locks import workflow_cli as wc
from locks.store import Store

from .conftest import git

# --- after ---------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_critique(monkeypatch):
    # These tests script the launcher's results one per worker; a plan's reader would take the next.
    monkeypatch.setenv("LOCKS_CRITIQUE", "off")



def _held(store: Store, wanted: dict, *, repo: str = "r", cwd: str | None = None, **fields) -> dict:
    return store.create_task("held work", status="queued", source="queue", repo=repo, cwd=cwd,
                             options=json.dumps({after.KEY: wanted}), **fields)


def test_a_hold_unreadable_options_hold_nothing():
    assert after.hold({"options": "not json"}) is None
    assert after.hold({"options": "[1, 2]"}) is None  # a list has no .get
    assert after.held({"status": "queued", "options": "{oops"}) is False
    assert after._part_id({"options": "{oops"}) is None
    assert after._part_id({"options": "[]"}) is None


def test_a_caveat_names_the_other_repository_and_this_one_s_run_of_the_same_number(store: Store):
    theirs = store.create_task("their work", status="done", source="locks", repo="other")
    assert after.caveat(store, {"task": theirs["id"]}, "other") is None  # this repository's own
    said = after.caveat(store, {"task": theirs["id"]}, "mine")
    assert said == (f"#{theirs['id']} is a task in other: this starts on this repository's base, and reads "
                    "what that work kept where it lives")
    # a run of this repository with the same number is named as the `--after run:N` it may have meant
    run = store.create_run(name="w", scope="user", source="x", definition="{}", repo="mine")
    while run["id"] < theirs["id"]:
        run = store.create_run(name="w", scope="user", source="x", definition="{}", repo="mine")
    assert run["id"] == theirs["id"]
    said = after.caveat(store, {"task": theirs["id"]}, "mine")
    assert f"; run #{run['id']} of this repository is `--after run:{run['id']}`" in said
    # a run in another repository
    elsewhere = store.create_run(name="w", scope="user", source="x", definition="{}", repo=None)
    said = after.caveat(store, {"run": elsewhere["id"]}, "mine")
    assert said.startswith(f"run #{elsewhere['id']} is a run in no repository: this starts")
    assert after.caveat(store, {"run": 9999}, "mine") is None


def test_held_work_on_a_task_in_another_repository_that_stopped_says_how(store: Store):
    other = store.create_task("x" * 120, status="blocked", source="locks", repo="other")
    stops.record(store, other["id"], stops.Stop("review", "the verifier found a blocker"))
    task = _held(store, {"task": other["id"]}, repo="mine")
    line = after.waiting(store, task)
    title = store.get_task(other["id"])["title"]
    shown = " ".join(title.split())
    shown = shown if len(shown) <= 80 else shown[:79] + "…"
    assert line == (f"waits for task #{other['id']} in other ({shown}), which stopped as review: a resume of it "
                    "to a pass releases this")


def test_another_worktree_of_the_same_repository_is_the_same_repository(git_repo, tmp_path):
    wt = tmp_path / "wt"
    git(git_repo, "worktree", "add", "-q", "-b", "side", str(wt))
    # With no remote each checkout is keyed by its path: the shared git store still makes them one.
    assert after._elsewhere(str(git_repo), wt) is None
    assert after._elsewhere(str(wt), git_repo) is None
    assert after.reads(("side", [], "#1", str(git_repo)), wt) is None
    git(git_repo, "remote", "add", "origin", "https://example.invalid/team/app.git")
    assert after._elsewhere(str(git_repo), wt) is None
    assert after._elsewhere(None, wt) is None
    assert after._elsewhere(str(git_repo), None) is None
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    assert after._elsewhere(str(other), wt) == other


def test_a_task_released_meanwhile_is_not_released_again(store: Store, monkeypatch):
    first = store.create_task("first", status="done", source="locks", repo="r")
    task = _held(store, {"task": first["id"]})
    stale = dict(task)
    # Another process released it between the listing and the write: the transaction reads it again.
    store.update_task(task["id"], options="{}")
    monkeypatch.setattr(after, "behind", lambda s, w: [stale])
    assert after.landed(store, first["id"], "main") == []
    assert store.last_event(task["id"], "released") is None

    real = store.list_tasks
    monkeypatch.setattr(store, "list_tasks", lambda **kw: [stale] if kw.get("status") == ["queued"] else real(**kw))
    assert after.release(store, task=first) == []
    assert store.last_event(task["id"], "released") is None


def test_a_held_step_outside_a_checkout_has_no_head(store: Store, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert after._now({"cwd": str(plain), "workflow_run": 1}) is None
    assert after._now({"cwd": str(plain), "workflow_run": None}) is None
    assert after._now({"cwd": None, "workflow_run": 1}) is None


def test_releasing_a_step_frees_its_held_run_with_what_it_reads_and_where_it_is_now(store: Store, tmp_path):
    run = store.create_run(name="w", scope="user", source="x", definition="{}", repo="r",
                           options=json.dumps({after.KEY: {"task": 1}, "add_dirs": ["/kept"]}))
    task = _held(store, {"task": 1}, workflow_run=run["id"], workflow_step="a")
    after._free(store, task, {"task": 1}, None, "why", head=None, read="/other/repo")
    freed = json.loads(store.get_run(run["id"])["options"])
    assert after.KEY not in freed
    assert freed["add_dirs"] == ["/kept", "/other/repo"]
    assert "origin" not in freed and "base" not in freed  # nothing to start on, and no head read
    assert json.loads(store.get_task(task["id"])["options"])["add_dirs"] == ["/other/repo"]
    released = store.last_event(task["id"], "released")["data"]
    assert released == {"after": {"task": 1}, "base": None, "why": "why"}


# --- loops ---------------------------------------------------------------------------------


def _a_loop(cwd, **over):
    from .test_loops import _loop

    return _loop(cwd, **over)


def test_a_diary_skips_lines_that_are_no_round(tmp_path):
    _a_loop(tmp_path)
    loops._write("leads", {"round": 1, "run": 5})
    path = loops._file("leads", ".diary.jsonl")
    with path.open("a", encoding="utf-8") as out:
        out.write("[1, 2]\n{\"round\": \"two\"}\nnot json\n")
    loops._write("leads", {"round": 1, "ended": "t"})
    assert loops.diary("leads") == [{"round": 1, "run": 5, "ended": "t"}]


def test_an_unmeasured_round_counts_for_no_arm_and_a_row_of_no_arm_is_left_out(tmp_path):
    loop = _a_loop(tmp_path, metric="x", variants=["a", "b"], trial=1)
    rows = [{"round": 1, "arm": 0, "before": None, "after": 3}]  # never measured before it
    assert loops._gain(loop, None, 3) is None
    assert loops.arm(loop, rows, {}) == (0, "on trial (0 of 1 measured rounds)")
    rows += [{"round": 2, "arm": "zero", "before": 1, "after": 9}, {"round": 3, "arm": 7, "before": 1, "after": 9}]
    assert loops.judge(loop, rows) is None  # neither row is an arm's
    rows += [{"round": 4, "arm": 0, "before": 1, "after": 2}, {"round": 5, "arm": 1, "before": 2, "after": 5}]
    assert loops.judge(loop, rows) == (1, {0: 1.0, 1: 3.0})


def test_the_best_before_the_stale_window_skips_unmeasured_and_worse_rounds(tmp_path):
    loop = _a_loop(tmp_path, metric="x", stale=1)
    now = datetime(2026, 10, 26, 8, 0).astimezone()
    rows = [{"round": 1, "before": 5, "after": None, "ended": "t"},  # unmeasured: no best
            {"round": 2, "after": 4, "ended": "t"},  # worse than 5: not the best
            {"round": 3, "after": 6, "ended": "t"}]
    assert loops.stop_reason(loop, rows, now=now) == ""  # 6 beats the best before it, 5
    rows[2]["after"] = 5
    assert loops.stop_reason(loop, rows, now=now) == "1 round(s) in a row did not improve its metric"


def test_a_round_s_summary_skips_steps_that_said_nothing_and_names_what_waits_on_the_person(store: Store):
    run = store.create_run(name="w", scope="user", source="x", definition="{}", repo="r")
    store.create_task("a", status="done", source="queue", workflow_run=run["id"], workflow_step="plan", result="")
    blocked = store.create_task("b", status="blocked", source="queue", workflow_run=run["id"],
                                workflow_step="send", result="wrote it\nNeeds the person:\n- a price for ACME\n")
    stops.record(store, blocked["id"], stops.Stop("permission", "Bash(curl:*) was refused"), result=False)
    said, needs = loops._round_summary(store, run["id"])
    assert said.startswith("send: wrote it") and "plan:" not in said
    assert needs == ["a price for ACME"]
    assert loops._person_only(store, run["id"]) == f"#{blocked['id']} permission (Bash(curl:*) was refused)"


def test_a_round_adopts_the_strategy_once_every_arm_has_its_trial_and_builds_on_the_last(git_repo, store: Store,
                                                                                          monkeypatch):
    loop = _a_loop(git_repo, metric="x", variants=["short", "long"], trial=1, stale=9)
    loops.set_state("leads", status="running")
    loops._write("leads", {"round": 1, "run": 101, "started": "t", "before": 1.0, "arm": 0})
    loops._write("leads", {"round": 1, "ended": "t", "state": "done", "after": 2.0, "changed_files": 3})
    loops._write("leads", {"round": 2, "run": 102, "started": "t", "before": 2.0, "arm": 1})
    monkeypatch.setattr(workflows, "status", lambda s, run_id: {"state": "done", "cost_usd": 0.5})
    started = []

    def start(store_, name, cwd, inputs, **kw):
        started.append(kw)
        return {"id": 103}

    now = datetime(2026, 10, 26, 8, 0).astimezone()
    assert loops.tick(store, "leads", now=now, start=start, measure_fn=lambda lp: (6.0, "")) == "started"
    st = loops.state("leads")
    assert st["adopted"] == 1 and st["adoption"]["means"] == {"0": 1.0, "1": 4.0}
    second, third = loops.diary("leads")[1:]
    # round 2 changed no file but moved the metric: not empty, and still nothing to build on
    assert second["empty"] is False and second["changed_files"] == 0
    assert third["arm"] == 1 and third["arm_why"] == "adopted" and third["before"] == 6.0
    # round 2 kept nothing, so round 3 builds on round 1's run, the last that changed files
    assert started[0]["after_run"] == 101 and started[0]["options"]["loop"]["arm"] == 1
    assert loops._checkout(str(git_repo)) is True
    plain = git_repo.parent / "plain"
    plain.mkdir()
    assert loops._checkout(str(plain)) is False
    told = loops.summary("leads")
    assert "adopted strategy: long" in told and loop.name in told


def test_a_round_s_brief_is_read_only_for_a_loop_that_exists(tmp_path, store: Store):
    assert loops.round_brief(store, {}) == ""
    assert loops.round_brief(store, {"loop": "leads"}) == ""  # an older run's plain name: no round
    assert loops.round_brief(store, {"loop": {"name": "gone"}}) == ""
    _a_loop(tmp_path, variants=["a", "b"])
    text = loops.round_brief(store, {"loop": {"name": "leads", "round": 2, "arm": 5}})
    assert "This is round 2 of the loop 'leads'" in text and "runs the strategy" not in text  # arm 5 is none
    assert "runs the strategy: b" in loops.round_brief(store, {"loop": {"name": "leads", "round": 2, "arm": 1}})


def test_a_summary_counts_what_waits_past_fifteen_and_names_a_round_stopped_for_the_person(tmp_path):
    _a_loop(tmp_path)
    loops._write("leads", {"round": 1, "run": 1, "started": "t", "before": None})
    loops._write("leads", {"round": 1, "ended": "t", "state": "blocked", "needs": [f"item {n}" for n in range(17)],
                           "person": "#4 permission", "cost_usd": 1.25})
    told = loops.summary("leads").splitlines()
    assert "  waits on the person (17):" in told
    assert "    - round 1: item 14" in told and "    - round 1: item 15" not in told
    assert "    … 2 more: `locks loop show leads --all`" in told
    assert told[-1] == "  round 1 stopped for the person: #4 permission"
    assert "  cost: USD 1.25" in told


def test_endings_pass_over_a_loop_whose_file_is_broken(tmp_path):
    _a_loop(tmp_path, session_id="s1")
    loops.set_state("leads", status="stopped", why="done", reported=False)
    (loops.folder() / "broken.json").write_text("{")
    said = loops.endings("s1")
    assert len(said) == 1 and said[0].startswith("loop leads (draft, stopped)")
    assert loops.endings("s1") == []


def test_the_cli_takes_named_inputs_and_starts_an_acting_loop_without_the_draft_line(tmp_path, monkeypatch,
                                                                                   capsys):
    from .test_loops import _save as save_workflow

    folder = tmp_path / "f"
    folder.mkdir()
    save_workflow({"name": "two", "inputs": ["goal", "list"], "steps": [{"id": "s", "prompt": "{goal} {list}"}]},
                  folder)
    assert cli.main(["loop", "add", "leads", "--goal", "find clients", "--workflow", "two", "--dir", str(folder),
                     "--input", " list =the catalog"]) == 0
    assert loops.load("leads").inputs == {"list": "the catalog", "goal": "find clients"}
    assert cli.main(["loop", "act", "leads"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(loops, "launch", lambda name, env: 77)
    assert cli.main(["loop", "start", "leads"]) == 0
    assert capsys.readouterr().out == "loop leads running (pid 77); `locks loop show leads` says how it goes.\n"


# --- workflow_cli: how runs and definitions are said ----------------------------------------


def test_a_step_line_says_its_cell_tries_phase_cost_and_how_far_behind_it_is():
    assert wc._files([], 0) == ""
    step = {"state": "running", "task": 4, "cell": "sonnet/high", "tries": 2, "phase": "implementing",
            "cost_usd": 0.5, "cost_live": True, "behind": "2 commits behind main"}
    assert wc._step_line(step) == ("running #4 · at sonnet/high · try 2 · implementing · $0.50 so far · "
                                   "2 commits behind main")
    assert wc._step_line({"state": "running", "cost_running": True}) == "running · running"
    assert wc._money({"cost_usd": 1.5, "cost_running": True}) == "$1.50 + running"


def test_a_run_line_with_no_step_is_its_head_and_names_an_older_last_commit():
    empty = {"id": 3, "name": "w", "state": "done", "cost_usd": 0, "waiting": [], "steps": [], "points": 5}
    assert wc._run_line(empty) == "#3 w — done · $0.00 · 5 points"
    run = {"id": 3, "name": "w", "state": "running", "cost_usd": 0, "waiting": [], "steps": [
        {"id": "a", "state": "done", "commit": "abc1234 built it"},
        {"id": "b", "state": "running", "task": 9}]}
    assert wc._run_line(run) == "#3 w — running · $0.00 · step b (2/2): running #9 · last commit abc1234 built it"


def test_a_compact_step_says_its_tries_sub_run_answer_and_why_it_was_skipped():
    assert wc._compact_step({"state": "done", "task": 2, "tries": 3, "cost_usd": 0.25, "kept_branch": "locks/task-2",
                             "subrun": 8}) == ("done #2 · try 3 · $0.25 · branch locks/task-2 · runs run #8 "
                                               "(`locks workflow status 8`)")
    assert wc._compact_step({"state": "done", "task": 5, "answer": "yes", "options": ["yes", "no"]}) == \
        "done #5 · $0.00 · answered yes"
    assert wc._compact_step({"state": "done", "skipped": "when check is findings: it was clean"}) == \
        "done · when check is findings: it was clean"


def test_a_run_in_full_says_each_part_s_detail(capsys):
    wc._print_run({"id": 1, "name": "w", "state": "running", "cost_usd": 0, "cwd": "/x", "inputs": {},
                   "waiting": [], "branch": None, "steps": [
                       {"id": "build", "state": "running", "parts": [
                           {"id": "web", "state": "done", "task": 3, "phase": "checking", "commit": "c1 web",
                            "files": ["a.py", "b.py", "c.py", "d.py"], "files_n": 5}]}]}, full=True)
    out = capsys.readouterr().out.splitlines()
    assert out[-2].strip().startswith("✓ web")
    assert out[-1].strip() == "checking · c1 web · 5 file(s): a.py, b.py, c.py +2"


def test_a_definition_says_its_question_and_sub_workflow_steps(capsys):
    defn = workflows.normalize({"name": "ask", "inputs": ["goal"], "steps": [
        {"id": "plan", "prompt": "Plan {goal}", "kind": "plan"},
        {"id": "go", "ask": "Build it?", "options": ["yes", "no"]},
        {"id": "sub", "workflow": "bugfix", "with": {"goal": "{goal}"}, "when": {"go": "yes"}},
        {"id": "bare", "workflow": "bugfix", "needs": ["sub"]}]})
    wc._print_definition({**defn, "scope": "user", "path": "/p"})
    out = capsys.readouterr().out
    assert "      asks the person: Build it?\n      answers: yes, no" in out
    assert "  sub — sub after go when go is yes\n      runs the workflow bugfix with goal={goal}" in out
    assert out.rstrip().endswith("      runs the workflow bugfix")


# --- workflow_cli: the commands ------------------------------------------------------------


def _wf(*args: str) -> int:
    return cli.main(["workflow", *args])


def test_the_cli_lists_a_broken_definition_shows_a_bundled_one_and_refuses_what_is_no_json(
        git_repo, monkeypatch, capsys, tmp_path):
    from locks.store import home

    monkeypatch.chdir(git_repo)
    folder = home() / "workflows"
    folder.mkdir(parents=True)
    (folder / "broken.json").write_text("{")
    assert _wf("list") == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("broken"))
    assert "is not readable JSON" in line
    assert _wf("show", "bugfix") == 0
    out = capsys.readouterr().out
    assert out.startswith("bugfix v") and "(bundled: " in out and "in use by run" not in out
    assert "revision sha256:" in out
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert _wf("save", str(bad)) == 1
    assert capsys.readouterr().err.startswith("locks workflow: not JSON: ")


def test_the_cli_applies_lists_and_says_no_pending_change(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    path = workflows.save(workflows.skeleton("ship"), git_repo).path
    assert _wf("pending") == 0 and capsys.readouterr().out == "no change waits for a run to end\n"
    waiting = workflows.pending_path(path)
    waiting.parent.mkdir()
    waiting.write_text(json.dumps({**workflows.skeleton("ship", "changed"), "version": 2}))
    assert _wf("pending", "--json") == 0
    (found,) = json.loads(capsys.readouterr().out)
    assert found["name"] == "ship" and found["held_by"] == [] and found["scope"] == "user"
    assert _wf("pending", "ship", "--apply") == 0
    assert capsys.readouterr().out == f"applied: {path}\n"
    assert json.loads(path.read_text())["description"] == "changed" and not waiting.exists()


def test_an_unknown_workflow_command_is_refused_and_a_step_set_with_nothing_changes_nothing(git_repo, monkeypatch,
                                                                                           capsys):
    assert wc.cmd_workflow(argparse.Namespace(workflow_command="bogus")) == 2
    monkeypatch.chdir(git_repo)
    workflows.save(workflows.skeleton("ship"), git_repo)
    assert _wf("step", "set", "ship", "do") == 1
    assert "nothing to change" in capsys.readouterr().err
    assert _wf("step", "set", "ship", "do", "--surfaces", "src/, docs/*.md,") == 0
    assert workflows.load("ship", git_repo)["steps"][0]["surfaces"] == ["src/**", "docs/*.md"]  # a folder: all under it


def _editor(tmp_path, body: str) -> str:
    script = tmp_path / "editor.py"
    script.write_text("import json, sys\npath = sys.argv[1]\n" + body)
    return f"{sys.executable} {script}"


def test_an_edit_in_the_editor_is_kept_when_it_holds_and_said_when_it_does_not(git_repo, monkeypatch, capsys,
                                                                               tmp_path):
    monkeypatch.chdir(git_repo)
    path = workflows.save(workflows.skeleton("ship"), git_repo).path
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", _editor(tmp_path, "d = json.load(open(path))\nd['description'] = 'edited'\n"
                                                   "json.dump(d, open(path, 'w'))\n"))
    assert _wf("edit", "ship") == 0
    assert capsys.readouterr().out == f"saved in the user scope: {path}\n"
    assert json.loads(path.read_text())["description"] == "edited" and json.loads(path.read_text())["version"] == 2
    # A pending change is what the editor opens, and a bundled one is copied to the user scope.
    waiting = workflows.pending_path(path)
    waiting.parent.mkdir()
    waiting.write_text(json.dumps({**workflows.skeleton("ship", "waiting"), "version": 3}))
    monkeypatch.setenv("VISUAL", _editor(tmp_path, "assert json.load(open(path))['description'] == 'waiting'\n"))
    assert _wf("edit", "ship") == 0
    monkeypatch.setenv("VISUAL", _editor(tmp_path, "open(path, 'w').write('{broken')\n"))
    assert _wf("edit", "ship") == 1
    err = capsys.readouterr().err
    assert "not kept, nothing changed" in err and "(your edit is in " in err
    monkeypatch.setenv("VISUAL", _editor(tmp_path, "pass\n"))
    assert _wf("edit", "bugfix") == 0
    assert "saved in the user scope" in capsys.readouterr().out
    assert workflows.load("bugfix", git_repo)["scope"] == "user"


def test_a_run_s_check_fills_its_inputs_without_a_text(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    workflows.save({"name": "checked", "inputs": ["goal", "cmd"], "steps": [
        {"id": "do", "prompt": "{goal}", "verify": "{cmd}"}]}, git_repo)
    args = argparse.Namespace(input=["goal=the export"], text=None, verify="pytest -q", name="checked", scope=None)
    assert wc._inputs(args, git_repo) == {"goal": "the export", "cmd": "pytest -q"}


def test_a_run_s_help_without_a_name_or_with_a_wrong_one_still_helps(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    parser = argparse.ArgumentParser(prog="run", add_help=False)
    parser.add_argument("-h", action=wc._RunHelp)
    with pytest.raises(SystemExit):
        parser.parse_args(["-h"])
    assert capsys.readouterr().out.startswith("usage: run")
    with pytest.raises(SystemExit):
        _wf("run", "Not-A-Name", "--help")
    out = capsys.readouterr().out
    assert "\nNot-A-Name: 'Not-A-Name' is no workflow name" in out


def test_a_run_of_many_features_is_told_so_and_a_dry_run_checks_the_run_it_waits_for(git_repo, monkeypatch,
                                                                                    capsys, kicks):
    monkeypatch.chdir(git_repo)
    workflows.save(workflows.skeleton("ship"), git_repo)
    monkeypatch.setattr(workflows, "many_features", lambda goal: "this goal lists 7 items")
    assert _wf("run", "ship", "the export", "--dry-run", "--after", "999") == 1
    out, err = capsys.readouterr()
    assert "locks: this goal lists 7 items" in out and "there is no workflow run #999" in err


def test_a_run_queued_after_another_says_what_it_waits_for(git_repo, monkeypatch, capsys, kicks, store: Store):
    monkeypatch.chdir(git_repo)
    workflows.save(workflows.skeleton("ship"), git_repo)
    assert _wf("run", "ship", "first") == 0
    first = store.list_runs()[0]["id"]
    capsys.readouterr()
    assert _wf("run", "ship", "second", "--after", str(first)) == 0
    out = capsys.readouterr().out
    assert f"\nit waits for run #{first} to pass\n" in out
    assert _wf("status", "--full") == 0
    out = capsys.readouterr().out
    # two runs in full, a blank line between them
    assert out.count("run #") >= 2 and "\n\nrun #" in out


# --- workflows: definitions ----------------------------------------------------------------


def _refused(defn) -> str:
    with pytest.raises(workflows.WorkflowError) as err:
        workflows.normalize(defn)
    return str(err.value)


def test_a_definition_s_every_problem_at_the_top_is_said_at_once():
    said = _refused({"name": "Bad Name", "version": 0, "description": 3, "inputs": ["Goal"], "steps": [],
                     "budget": 1})
    for part in ("unknown keys ['budget']", "'Bad Name' is no workflow name", "version is a whole number from 1",
                 "description is text", "inputs is a list of names", "steps is a list of at least one step"):
        assert part in said
    assert "steps is a list" in _refused({"name": "x", "steps": "do it"})
    assert "version is a whole number" in _refused({"name": "x", "version": True, "steps": [{"id": "a",
                                                                                           "prompt": "p"}]})


def test_a_step_s_every_problem_is_said_at_once():
    long = "x" * (workflows.PROMPT_CHARS + 1)
    said = _refused({"name": "x", "inputs": ["goal"], "steps": [
        "not a step",
        {"prompt": "no id"},
        {"id": "Bad Id", "prompt": "p", "integrate": True},
        {"id": "long", "prompt": long},
        {"id": "fan", "prompt": "p", "fanout": 3, "verify": ["pytest"], "needs": "long"},
        {"id": "q1", "ask": 5, "options": ["yes", "no"]},
        {"id": "q2", "ask": long, "options": ["yes", "no"]},
        {"id": "sub", "workflow": "bugfix", "with": ["goal"]},
    ]})
    for part in ("step 1 is not an object", "step 2 needs an id", "step 'long''s prompt is over",
                 "step 'fan': fanout is the id of the plan step", "step 'fan': verify is a command, as text",
                 "step 'fan': needs is a list of step ids", "step 'q1': ask is the question, as text",
                 "step 'q2''s question is over", "step 'sub': with maps the inputs"):
        assert part in said, part


def test_an_integrate_step_writes_and_an_empty_memory_is_dropped():
    said = _refused({"name": "x", "inputs": ["goal"], "steps": [
        {"id": "plan", "prompt": "plan {goal}", "kind": "plan"},
        {"id": "build", "prompt": "build", "fanout": "plan"},
        {"id": "join", "prompt": "join", "integrate": True, "kind": "explore"}]})
    assert "step 'join': an integrate step merges, so it writes: no ledger and no reading kind" in said
    defn = workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "p", "memory": []}]})
    assert "memory" not in defn["steps"][0]


def test_a_listing_leaves_out_a_file_that_is_no_workflow_name_and_a_file_that_holds_no_object(tmp_path):
    from locks.store import home

    folder = home() / "workflows"
    folder.mkdir(parents=True)
    (folder / "Not A Name.json").write_text("{}")
    (folder / "alist.json").write_text("[]")
    found = {w["name"]: w for w in workflows.listing(tmp_path) if w["scope"] == "user"}
    assert list(found) == ["alist"] and found["alist"]["error"].endswith("holds no workflow object")


def test_a_step_s_memory_says_what_is_no_longer_kept(store: Store):
    note = store.add_note("p", "conventions", "tabs", "use tabs", author="person", filed_by="person")
    store.update_note(note, state="dropped")
    text = workflows.memory_text(store, "p", [f"note:{note}", "problem:999"])
    assert f"- note #{note}: no longer kept; go on without it" in text
    assert "- problem #999: no longer kept" in text
    assert workflows.memory_text(store, "p", []) == ""


def test_a_pending_revision_that_cannot_apply_waits_and_a_bundled_scope_is_never_written(git_repo, monkeypatch):
    path = workflows.save(workflows.skeleton("ship"), git_repo).path
    waiting = workflows.pending_path(path)
    waiting.parent.mkdir()
    waiting.write_text("{broken")
    assert workflows.promote(path) is False and waiting.exists()  # unreadable: kept for the person
    with pytest.raises(workflows.WorkflowError, match="project or the user scope, not 'bundled'"):
        workflows.save(workflows.skeleton("ship"), git_repo, "bundled")
    waiting.unlink()
    with pytest.raises(workflows.WorkflowError, match=r"ship \(user\) has no pending change"):
        workflows.apply_pending("ship", git_repo)


def test_a_step_added_in_a_chain_takes_its_place_and_one_named_twice_is_refused():
    defn = workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "p"}, {"id": "b", "prompt": "q"}]})
    with pytest.raises(workflows.WorkflowError, match="step 'a' exists"):
        workflows.add_step(defn, {"id": "a", "prompt": "again"})
    workflows.add_step(defn, {"id": "m", "prompt": "middle"}, after="a")
    assert [(s["id"], s["needs"]) for s in defn["steps"]] == [("a", []), ("m", ["a"]), ("b", ["m"])]
    # a follower that waits on something else keeps its needs
    defn = workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "p"},
                                                       {"id": "b", "prompt": "q", "needs": []}]})
    workflows.add_step(defn, {"id": "m", "prompt": "middle"}, after="a")
    assert defn["steps"][2]["needs"] == []


def test_a_graph_says_parts_joins_answers_sub_runs_and_its_description():
    defn = workflows.normalize({"name": "par", "description": "Parts, then a join.", "inputs": ["goal"], "steps": [
        {"id": "plan", "prompt": "plan {goal}", "kind": "plan"},
        {"id": "go", "ask": "Go?", "options": ["yes", "no"]},
        {"id": "build", "prompt": "build", "fanout": "plan", "needs": ["plan", "go"]},
        {"id": "join", "prompt": "join", "integrate": True},
        {"id": "more", "workflow": "bugfix", "with": {"goal": "{goal}"}}]})
    run = {"id": 4, "state": "running", "steps": [{"id": "go", "state": "done", "task": 2, "answer": "yes"},
                                                  {"id": "more", "state": "running", "task": 9, "subrun": 5}]}
    text = workflows.render_markdown(defn, run)
    assert text.startswith("## par — run #4, running\n\nParts, then a join.\n\n```mermaid\n")
    assert "parts of plan" in text and "joins the parts" in text
    assert "done #2: yes" in text and "running #9 (run #5)" in text


def test_a_check_input_named_by_the_person_is_not_filled_from_the_run_s_check():
    defn = workflows.normalize({"name": "x", "inputs": ["goal", "cmd"], "steps": [
        {"id": "a", "prompt": "{goal}", "verify": "{cmd}"}]})
    assert workflows.fill_checks(defn, {"cmd": "make test"}, "pytest") == {"cmd": "make test"}
    assert workflows.fill_checks(defn, {}, "pytest") == {"cmd": "pytest"}


def test_unreadable_part_options_are_a_whole_step_and_no_entry_stands_for_no_task(store: Store):
    assert workflows._part({"options": "{oops"}) is None
    assert workflows._part(None) is None
    assert workflows._tasks_of(None) == [] and workflows._tasks_of({}) == []
    assert workflows.outcome(store, None) == {}


def test_a_when_on_changed_files_reads_the_step_s_ending(store: Store):
    wrote = store.create_task("a", status="done", source="queue")
    store.add_event(wrote["id"], "finished", status="done", changed=["a.py"])
    kept = store.create_task("b", status="done", source="queue")
    store.add_event(kept["id"], "finished", status="done", changed=[])
    step = {"id": "c", "when": {"a": "changed"}}
    assert workflows._holds(store, step, {"a": wrote}) == (True, "")
    assert workflows._holds(store, step, {"a": kept}) == (
        False, "its condition a is 'changed' did not hold: a was 'unchanged'")
    assert workflows._holds(store, {"id": "c", "when": {"a": "unchanged"}}, {"a": kept}) == (True, "")


def test_an_answer_cancel_or_retry_names_the_run_that_is_not_there_or_not_active(store: Store):
    for call in (lambda: workflows.answer(store, 99, "a", "yes"), lambda: workflows.cancel(store, 99),
                 lambda: workflows.retry(store, 99, "a")):
        with pytest.raises(workflows.WorkflowError, match="no workflow run #99"):
            call()
    run = store.create_run(name="w", scope="user", source="x", status="cancelled",
                           definition=json.dumps({"name": "w", "steps": [{"id": "a", "prompt": "p"}]}))
    with pytest.raises(workflows.WorkflowError, match=f"run #{run['id']} is cancelled"):
        workflows.answer(store, run["id"], "a", "yes")


# --- workflows: runs -----------------------------------------------------------------------


def _a_run(store: Store, defn: dict, *, cwd=None, options: dict | None = None, **fields) -> dict:
    checked = workflows.normalize(defn)
    return store.create_run(name=checked["name"], scope="user", source="/nowhere.json",
                            definition=json.dumps(workflows._stored(checked)), inputs=json.dumps({"goal": "g"}),
                            options=json.dumps(options or {}), repo="r", cwd=str(cwd) if cwd else None, **fields)


def _step_task(store: Store, run: dict, step: str, status: str = "done", **fields) -> dict:
    return store.create_task(f"step {step}", status=status, source="queue", repo="r", cwd=run["cwd"],
                             workflow_run=run["id"], workflow_step=step, **fields)


def test_a_run_started_on_a_detached_head_keeps_where_it_started_and_no_branch(git_repo, store: Store, kicks):
    workflows.save(workflows.skeleton("ship"), git_repo)
    git(git_repo, "checkout", "-q", "--detach")
    started = workflows.start(store, "ship", git_repo, {"goal": "the export"})
    options = json.loads(started["options"])
    assert options["origin"] == git(git_repo, "rev-parse", "HEAD") and "origin_branch" not in options


def test_a_run_no_longer_active_queues_nothing(store: Store):
    run = _a_run(store, {"name": "w", "steps": [{"id": "a", "prompt": "p"}]}, status="done")
    assert workflows._queue_ready(store, run) == []
    assert store.run_tasks(run["id"]) == []


def test_a_sub_workflow_its_run_did_not_freeze_is_refused_with_why(store: Store):
    run = _a_run(store, {"name": "w", "steps": [{"id": "sub", "workflow": "bugfix", "with": {"goal": "x"}}]})
    with pytest.raises(workflows.WorkflowError, match=f"step 'sub': bugfix was not frozen with run #{run['id']}"):
        workflows._queue_ready(store, run)


def test_a_sub_workflow_that_kept_no_branch_passes_its_step_changing_nothing(store: Store, tmp_path, kicks):
    parent = store.create_task("run it", status="queued", source="queue", repo="r",
                               options=json.dumps({after.KEY: {"run": 1}}))
    child = _a_run(store, {"name": "look", "steps": [{"id": "read", "prompt": "read", "kind": "explore"}]},
                   cwd=tmp_path, options={"parent": {"task": parent["id"]}}, status="done")
    read = _step_task(store, child, "read", cost_usd=0.75)
    store.add_event(read["id"], "finished", status="done", changed=[])
    assert workflows._finish_parent(store, child, None) == []
    done = store.get_task(parent["id"])
    assert done["status"] == "done" and done["result"] == f"run #{child['id']} of look passed, changing nothing"
    assert after.KEY not in json.loads(done["options"]) and done["cost_usd"] == 0.75
    finished = store.final_event(parent["id"])["data"]
    assert finished["branch"] is None and finished["tip"] is None and finished["subrun"] == child["id"]
    # one already passed is not passed again
    assert workflows._finish_parent(store, child, None) == []


def test_a_retried_part_after_one_that_passed_starts_from_what_that_one_kept(git_repo, store: Store):
    run = _a_run(store, {"name": "w", "steps": [{"id": "a", "prompt": "p"}]}, cwd=git_repo)
    git(git_repo, "branch", "locks/task-90")
    tip = git(git_repo, "rev-parse", "locks/task-90")
    api = _step_task(store, run, "build")
    store.add_event(api["id"], "finished", status="done", branch="locks/task-90")
    part = {"id": "web", "after": "api"}
    assert workflows._ordered(store, run, part, {}, {"api": api}) == {"start": tip}
    stopped = _step_task(store, run, "build", status="blocked")
    assert workflows._ordered(store, run, part, {}, {"api": stopped}) == {}
    assert workflows._ordered(store, run, part, {}, {}) == {}
    assert workflows._ordered(store, run, {"id": "web"}, {}, {}) == {}


def test_nothing_changed_is_nothing_generated_and_a_plan_no_dispatcher_took_pauses_no_lane(store: Store):
    run = _a_run(store, {"name": "w", "steps": [{"id": "a", "prompt": "p"}]})
    assert workflows._generated(store, run, []) == set()
    plan = _step_task(store, run, "plan", status="running")
    workflows._take_back(store, run, plan, "its parts overlap")
    assert store.get_task(plan["id"])["status"] == "replan"
    assert stops.of(store, store.get_task(plan["id"])).cause == "parts"
    assert store.lanes() == []


def test_a_brief_leaves_out_a_step_that_said_nothing_and_a_loop_that_is_gone(store: Store):
    defn = workflows.normalize({"name": "w", "inputs": ["goal"], "steps": [
        {"id": "a", "prompt": "a {goal}", "kind": "explore"}, {"id": "b", "prompt": "b {goal}"}]})
    run = _a_run(store, defn)
    quiet = _step_task(store, run, "a")
    task = workflows._queue_step(store, run, defn, defn["steps"][1], {"goal": "g"}, {"loop": {"name": "gone"}},
                                 {"a": quiet})
    assert task["body"] == "b g"  # nothing the step before said, and no round of a loop that is gone
    assert json.loads(task["options"])["loop"] == "gone"


def test_a_step_with_no_branch_before_it_starts_from_the_run_s_base(store: Store, tmp_path):
    defn = {"steps": [{"id": "a", "needs": []}, {"id": "fix", "ledger": "fix", "needs": ["a"]}]}
    run = {"id": 1, "cwd": str(tmp_path / "gone"), "options": json.dumps({"base": "abc123"})}
    fixed = store.create_task("fix", status="done", source="queue")
    store.add_event(fixed["id"], "finished", status="done", branch=None)
    built = store.create_task("a", status="done", source="queue")
    store.add_event(built["id"], "finished", status="done", branch="locks/task-1")
    assert workflows._kept_tip(store, fixed, str(tmp_path)) is None
    # the fix kept no branch, and the one branch before it is in no checkout here
    assert workflows._base(store, run, defn, "fix", {"fix": fixed, "a": built}) == "abc123"


def test_drift_is_said_only_where_git_can_say_it(git_repo, store: Store):
    loose = store.create_task("x", status="done", source="queue")
    assert workflows.drift(store, loose, "HEAD", "end") is None
    run = _a_run(store, {"name": "w", "steps": [{"id": "a", "prompt": "p"}]}, cwd=git_repo,
                 options={"origin_branch": "no-such-branch"})
    task = _step_task(store, run, "a")
    assert workflows.drift(store, task, "HEAD", "end") is None
    assert store.last_event(task["id"], "behind_base") is None


def test_advance_of_nothing_and_an_ending_with_no_time_queues_nothing(store: Store):
    assert workflows.advance(store, None) == []
    task = store.create_task("x", status="done", source="queue")
    store.add_event(task["id"], "finished", status="done")
    other = store.create_task("y", status="queued", source="queue")
    workflows._advanced(store, task, [other["id"]])
    assert store.last_event(other["id"], "advanced") is None


def test_a_recheck_does_not_queue_a_fix_a_later_round_already_queued(store: Store):
    defn = {"name": "w", "inputs": ["goal"], "steps": [
        {"id": "review", "prompt": "review {goal}", "kind": "review-routine"},
        {"id": "fix", "prompt": "fix", "ledger": "fix"},
        {"id": "recheck", "prompt": "recheck", "ledger": "recheck"}]}
    run = _a_run(store, defn)
    _step_task(store, run, "review")
    recheck = _step_task(store, run, "recheck", options=json.dumps({"ledger": {"role": "recheck"}}))
    store.add_event(recheck["id"], "finished", status="done", ledger={"survivors": ["F1"], "round": 1})
    _step_task(store, run, "fix", status="queued")  # newer than the recheck
    assert workflows._next_round(store, run, recheck) == []
    # a recheck that waits on no fix has none to queue
    loose = {**defn, "steps": [*defn["steps"][:2], {**defn["steps"][2], "needs": ["review"]}]}
    run = _a_run(store, loose)
    recheck = _step_task(store, run, "recheck", options=json.dumps({"ledger": {"role": "recheck"}}))
    store.add_event(recheck["id"], "finished", status="done", ledger={"survivors": ["F1"], "round": 1})
    assert workflows._next_round(store, run, recheck) == []


def test_a_running_step_outside_a_worktree_names_no_files(store: Store, tmp_path):
    run = {"cwd": str(tmp_path)}
    task = store.create_task("x", status="running", source="queue")
    found = workflows.progress(store, run, task)
    assert "files" not in found and "phase" in found


def test_a_run_whose_steps_passed_but_one_never_queued_is_stalled(store: Store):
    run = _a_run(store, {"name": "w", "steps": [{"id": "a", "prompt": "p"}, {"id": "b", "prompt": "q"}]})
    _step_task(store, run, "a")
    shown = workflows.status(store, run["id"])
    assert shown["state"] == "stalled" and [s["state"] for s in shown["steps"]] == ["done", "pending"]


def test_a_stopped_step_says_the_branch_it_kept(store: Store):
    run = _a_run(store, {"name": "w", "steps": [{"id": "a", "prompt": "p"}]})
    task = _step_task(store, run, "a", status="blocked")
    stops.record(store, task["id"], stops.Stop("review", "a blocker stands"), result=False)
    store.add_event(task["id"], "finished", status="blocked", branch="locks/task-7",
                    stop=stops.Stop("review", "a blocker stands").data())
    (step,) = workflows.status(store, run["id"])["steps"]
    assert step["kept_branch"] == "locks/task-7" and step["stopped"] == "a blocker stands"
    # A step an older locks dismissed kept no account of why: its branch is said, no reason made up.
    old = _a_run(store, {"name": "w", "steps": [{"id": "a", "prompt": "p"}]})
    task = _step_task(store, old, "a", status="dismissed")
    store.add_event(task["id"], "finished", status="dismissed", branch="locks/task-8")
    (step,) = workflows.status(store, old["id"])["steps"]
    assert step["kept_branch"] == "locks/task-8" and "stopped" not in step


def test_cancelling_a_run_asks_its_running_step_to_stop(store: Store, monkeypatch):
    import os
    import signal

    run = _a_run(store, {"name": "w", "steps": [{"id": "a", "prompt": "p"}, {"id": "b", "prompt": "q",
                                                                            "needs": []}]})
    running = _step_task(store, run, "a", status="running", pid=424242)
    gone = _step_task(store, run, "b", status="running")
    killed = []

    def kill(pid, sig):
        killed.append((pid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", kill)
    assert workflows.cancel(store, run["id"]) == [running["id"], gone["id"]]
    assert killed == [(424242, signal.SIGTERM)]
    assert store.cancel_requested(running["id"]) and store.cancel_requested(gone["id"])
    assert store.get_run(run["id"])["status"] == "cancelled"


def test_a_step_never_queued_is_queued_by_a_retry_with_nothing_to_supersede(store: Store, kicks):
    run = _a_run(store, {"name": "w", "steps": [{"id": "a", "prompt": "p"}, {"id": "b", "prompt": "q"}]})
    _step_task(store, run, "a")
    task = workflows.retry(store, run["id"], "b")
    assert task["status"] == "queued" and task["workflow_step"] == "b"
    assert workflows.status(store, run["id"])["state"] == "running"


@pytest.mark.usefixtures("no_verifier")
def test_a_part_that_changed_nothing_leaves_one_branch_and_one_area_reviewed_whole(git_repo, store: Store, kicks,
                                                                                    monkeypatch):
    from locks import flow
    from locks.orchestrate import run as run_task

    from .test_orchestrate import kind, ok
    from .test_workflows import JOINED, planned

    monkeypatch.setattr(workflows, "AREA_REVIEW_LINES", 1)
    workflows.save(JOINED, git_repo)
    seen = []

    def launch(spec):
        seen.append(spec.prompt)
        if "Your part: api" in spec.prompt:
            (spec.target_dir / "api").mkdir()
            (spec.target_dir / "api" / "api.py").write_text("rows = []\n" * 5)
        return [planned(), ok("api built"), ok("nothing to do for web"), ok("reads fine")][len(seen) - 1]

    def runner(text, where, store_, options, **kw):
        return run_task(text, where, store_, options, registry={}, launcher=launch, classifier=kind("implement"),
                        adapters=[], **kw)

    started = workflows.start(store, "joined", git_repo, {"goal": "the orders view"})
    flow.work(store, runner=runner)
    tasks = {t["workflow_step"] + (("/" + json.loads(t["options"])["part"]["id"])
                                   if json.loads(t["options"]).get("part") else ""): t
             for t in store.run_tasks(started["id"])}
    # the web part kept no branch: the join has one, and nothing to join
    assert tasks["join"]["result"] == "one part kept a branch: nothing to join, and what follows starts from it"
    # every changed line is in the api part's surfaces: one area, so one review of the whole change
    assert "review" in tasks and not any(k.startswith("review/") for k in tasks)
    assert len(seen) == 4 and workflows.status(store, started["id"])["state"] == "done"


def test_a_retry_over_a_plan_whose_parts_overlap_takes_the_plan_back_and_says_so(store: Store):
    defn = {"name": "w", "inputs": ["goal"], "steps": [
        {"id": "plan", "prompt": "plan {goal}", "kind": "plan"},
        {"id": "build", "prompt": "build", "fanout": "plan"}]}
    overlapping = [{"id": "api", "prompt": "a", "surfaces": ["src/**"]},
                   {"id": "web", "prompt": "w", "surfaces": ["src/web/**"]}]
    run = _a_run(store, defn)
    plan = _step_task(store, run, "plan")
    store.add_event(plan["id"], "finished", status="done", parts=overlapping)
    part = _step_task(store, run, "build", status="blocked", options=json.dumps({"part": {"id": "web", "n": 2}}))
    stops.record(store, part["id"], stops.Stop("review", "x"), result=False)
    with pytest.raises(workflows.WorkflowError, match="step 'build': its parts could not be queued again"):
        workflows.retry(store, run["id"], "build")
    assert stops.of(store, store.get_task(plan["id"])).cause == "parts"
    whole = _a_run(store, defn)
    plan = _step_task(store, whole, "plan")
    store.add_event(plan["id"], "finished", status="done", parts=overlapping)
    with pytest.raises(workflows.WorkflowError, match="the plan it fans out over was taken back; retry the plan"):
        workflows.retry(store, whole["id"], "build")
