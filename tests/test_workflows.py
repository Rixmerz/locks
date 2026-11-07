from __future__ import annotations

import json

import pytest

from locks import cli, flow, project, workflows
from locks.escalate import Failure
from locks.launch import WorkerResult
from locks.orchestrate import Options, run
from locks.store import Store

from .test_orchestrate import bad, kind, ok


def _worker(results, seen):
    """A launcher that writes one file per writing attempt and records what each attempt saw."""
    results = list(results)

    def launch(spec):
        files = sorted(p.name for p in spec.target_dir.iterdir() if p.suffix == ".txt")
        seen.append({"prompt": spec.prompt, "files": files, "dir": spec.target_dir,
                     "reads_only": "Edit" in spec.disallowed_tools})
        if "Edit" not in spec.disallowed_tools:
            (spec.target_dir / f"step{len(seen)}.txt").write_text("work\n")
        return results.pop(0)

    return launch


def _runner(results, seen):
    launch = _worker(results, seen)

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    return runner


def _save(defn, cwd, scope="user"):
    return workflows.save(defn, cwd, scope).path


CHAIN = {"name": "chain", "inputs": ["goal"], "steps": [
    {"id": "build", "prompt": "Build {goal}", "kind": "implement"},
    {"id": "check", "prompt": "Review, without changing any file, {goal}", "kind": "review-routine"},
    {"id": "polish", "prompt": "Polish {goal}", "kind": "implement"},
]}


# --- definitions ---------------------------------------------------------------------


def test_a_definition_is_checked_and_completed():
    defn = workflows.normalize(CHAIN)
    assert [s["needs"] for s in defn["steps"]] == [[], ["build"], ["check"]]
    assert defn["steps"][0]["title"] == "build"
    with pytest.raises(workflows.WorkflowError) as err:
        workflows.normalize({"name": "x", "inputs": ["goal"], "steps": [
            {"id": "a", "prompt": "do {other}", "kind": "nope", "allow_tools": ["Bash(*)"]},
            {"id": "a", "prompt": "", "needs": ["z"], "start": "gpt/high", "max_turns": 0}]})
    said = str(err.value)
    for part in ("allow_tools", "flags of `locks workflow run`", "{other}", "no kind 'nope'", "used twice",
                 "needs a prompt", "'z'", "no cell", "max_turns is a whole number from 1"):
        assert part in said
    # A step an older locks saved with a dollar budget still loads; the key is dropped, even a bad value.
    # Turns over the ceiling (an older locks took any number) read as the ceiling.
    old = workflows.normalize({"name": "old", "inputs": ["goal"], "steps": [
        {"id": "a", "prompt": "do {goal}", "kind": "implement", "budget_usd": 2.0, "max_turns": 300},
        {"id": "b", "prompt": "check", "kind": "review-routine", "needs": ["a"], "budget_usd": -1}]})
    assert all("budget_usd" not in step for step in old["steps"])
    assert old["steps"][0]["max_turns"] == 200
    with pytest.raises(workflows.WorkflowError, match="no workflow name"):
        workflows.skeleton("Bad Name")


def test_make_copy_change_and_remove_workflows(git_repo):
    project.enroll(git_repo)
    names = {(w["name"], w["scope"]) for w in workflows.listing(git_repo)}
    assert {("feature", "bundled"), ("bugfix", "bundled"), ("review-fix", "bundled")} <= names

    # A copy of a template, in the project, hides the template there.
    workflows.copy_to("feature", "feature", git_repo, scope="project")
    found = [w for w in workflows.listing(git_repo) if w["name"] == "feature"]
    assert [(w["scope"], w["shadowed"]) for w in found] == [("project", False), ("bundled", True)]
    assert workflows.load("feature", git_repo)["scope"] == "project"

    # A change to a bundled template lands in the user scope; the template stays.
    where, path, _ = workflows.edit("bugfix", git_repo, lambda d: d.update(description="mine"))
    assert where == "user" and json.loads(path.read_text())["description"] == "mine"
    assert workflows.load("bugfix", git_repo, "bundled")["description"] != "mine"

    _save(workflows.skeleton("mine"), git_repo)
    with pytest.raises(workflows.WorkflowError, match="exists"):
        _save(workflows.skeleton("mine"), git_repo)

    def grow(d):
        workflows.add_step(d, {"id": "test", "prompt": "Test {goal}"})
        workflows.add_step(d, {"id": "plan", "prompt": "Plan {goal}"}, after=None)
        workflows.add_step(d, {"id": "first", "prompt": "First {goal}"}, after="do")
        workflows.set_step(d, "test", {"kind": "test", "verify": "pytest -q"})

    workflows.edit("mine", git_repo, grow)
    steps = workflows.load("mine", git_repo)["steps"]
    assert [(s["id"], s["needs"]) for s in steps] == [
        ("do", []), ("first", ["do"]), ("test", ["first"]), ("plan", ["test"])]
    workflows.edit("mine", git_repo, lambda d: (workflows.remove_step(d, "first"),
                                                workflows.set_step(d, "test", {"verify": None})))
    steps = workflows.load("mine", git_repo)["steps"]
    assert [(s["id"], s["needs"]) for s in steps] == [("do", []), ("test", ["do"]), ("plan", ["test"])]
    assert "verify" not in steps[1]
    with pytest.raises(workflows.WorkflowError, match="no step"):
        workflows.edit("mine", git_repo, lambda d: workflows.set_step(d, "ghost", {"kind": "docs"}))

    with pytest.raises(workflows.WorkflowError, match="bundled template is not removed"):
        workflows.remove("feature", git_repo, "bundled")
    workflows.remove("mine", git_repo, "user")
    with pytest.raises(workflows.WorkflowError, match="no workflow 'mine'"):
        workflows.load("mine", git_repo)
    with pytest.raises(workflows.WorkflowError, match="no enrolled project"):
        workflows.save(workflows.skeleton("x"), git_repo.parent, "project")


# --- runs ----------------------------------------------------------------------------


def test_a_run_goes_on_by_itself_each_step_on_the_work_before_it(git_repo, store: Store, kicks):
    _save(CHAIN, git_repo)
    started = workflows.start(store, "chain", git_repo, {"goal": "the export"}, session_id="s1",
                              options={"allow_tools": ["Bash(make:*)"]})
    assert len(started["queued"]) == 1 and kicks
    seen: list[dict] = []
    report = flow.work(store, runner=_runner([ok("built it"), ok("no problems"), ok("polished")], seen))
    assert [r.status for r in report.ran] == ["done", "done", "done"]

    build, check, polish = seen
    assert "Task (implement):\nBuild the export" in build["prompt"]
    # The reading step reads the build in a worktree of its own, and cannot write.
    assert check["reads_only"] and "step1.txt" in check["files"]
    assert check["dir"] != git_repo and "git diff" in check["prompt"]
    # The last step starts from the build, and hears what the steps before found.
    assert "step1.txt" in polish["files"]
    assert "- build (task #" in polish["prompt"] and "no problems" in polish["prompt"]

    status = workflows.status(store, started["id"])
    assert status["state"] == "done" and status["branch"] == f"locks/task-{status['steps'][2]['task']}"
    assert [s["state"] for s in status["steps"]] == ["done", "done", "done"]
    tasks = store.run_tasks(started["id"])
    assert all(t["session_id"] == "s1" for t in tasks)
    assert "budget_usd" not in json.loads(tasks[0]["options"])
    assert json.loads(tasks[0]["options"])["allow_tools"] == ["Bash(make:*)"]
    # The session hears of the run once, at its end: not of each step it went past.
    assert [t["id"] for t in store.unreported("s1")] == [tasks[-1]["id"]]
    # A watcher sees the run, and each step's task says which run it belongs to.
    from locks.ui import api

    seen_now = api.overview(store, hours=1)
    assert [w["id"] for w in seen_now["workflows"]] == [started["id"]]
    assert any(t.get("workflow_step") == "polish" for s in seen_now["sessions"] for t in s["tasks"]) or \
        any(t.get("workflow_step") == "polish" for t in seen_now["other"])


def test_a_run_says_its_first_steps_warnings_and_an_ending_repeats_them(git_repo, store: Store, kicks, capsys,
                                                                        monkeypatch):
    from locks import hooks

    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("LOCKS_SESSION_ID", "s1")
    _save({"name": "look", "inputs": ["page"], "steps": [
        {"id": "edit", "prompt": "Change {page}", "kind": "implement"},
        {"id": "see", "prompt": "Open {page} in the browser and fix what is off", "kind": "implement",
         "needs": ["edit"]}]}, git_repo)
    _save({"name": "shot", "steps": [
        {"id": "snap", "prompt": "Take a screenshot of the home page and fix it", "kind": "implement"}]}, git_repo)
    assert cli.main(["workflow", "run", "shot"]) == 0
    out = capsys.readouterr().out
    snap = store.run_tasks(store.list_runs()[0]["id"])[0]["id"]
    assert f"warning: step snap (#{snap}): this task needs a browser and workers have none" in out
    assert cli.main(["workflow", "run", "shot", "--json"]) == 0
    assert "needs a browser" in json.loads(capsys.readouterr().out)["warnings"][0]["warning"]
    assert cli.main(["workflow", "cancel", "1"]) == 0
    assert cli.main(["workflow", "cancel", "2"]) == 0
    # A step that waits on another is not planned yet: nothing to say of it.
    assert cli.main(["workflow", "run", "look", "the settings page"]) == 0
    assert "warning:" not in capsys.readouterr().out
    run_id = store.list_runs()[0]["id"]
    flow.work(store, runner=_runner([ok(), ok()], []))
    edit, see = store.run_tasks(run_id)
    assert store.last_event(edit["id"], "planned")["data"]["warnings"] == []
    assert "needs a browser" in store.last_event(see["id"], "planned")["data"]["warnings"][0]
    # The run's ending is its last step's, and it repeats the warning that step ran with.
    text, ids = hooks.endings(store, "s1")
    assert see["id"] in ids and edit["id"] not in ids
    ending = text[text.index(f"#{see['id']} [done]"):]
    assert ending.splitlines()[1].startswith("  it ran with a warning: this task needs a browser and workers have none")


def test_a_run_s_add_dir_reaches_every_step_and_a_retry(git_repo, store: Store, kicks, monkeypatch, tmp_path):
    monkeypatch.chdir(git_repo)
    beside = tmp_path / "library"
    beside.mkdir()
    _save({"name": "two", "steps": [
        {"id": "a", "prompt": "Copy the library", "kind": "implement"},
        {"id": "b", "prompt": "Adapt it", "kind": "implement", "needs": ["a"]}]}, git_repo)
    assert cli.main(["workflow", "run", "two", "--add-dir", str(beside)]) == 0
    run_id = store.list_runs()[0]["id"]
    assert json.loads(store.get_run(run_id)["options"])["add_dirs"] == [str(beside.resolve())]
    flow.work(store, runner=_runner([ok(), bad(Failure.SPEC_BUG, "x")], []))
    first, second = store.run_tasks(run_id)
    for task in (first, second):
        assert flow.queued_options(task, Options()).add_dirs == (str(beside.resolve()),)
    again = workflows.retry(store, run_id, "b")
    assert json.loads(again["options"])["add_dirs"] == [str(beside.resolve())]
    # The try it replaces offers nothing to resume, and no session hears of it as an ending.
    from locks import stops

    old = store.get_task(second["id"])
    stop = stops.of(store, old)
    assert old["status"] == "dismissed" and stop.cause == "superseded" and stop.extra["by_task"] == again["id"]
    assert stops.next_step(old["id"], stop) is None and f"queued again as #{again['id']}" in stops.what_to_do(stop)
    assert store.last_event(old["id"], "reported")["data"]["via"] == "retried"


def test_a_plan_that_cannot_be_foreseen_never_undoes_the_start_of_a_run(git_repo, store: Store, kicks, capsys,
                                                                       monkeypatch):
    from locks import orchestrate

    def broken(*args, **kwargs):
        raise ValueError("the registry is unreadable")

    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(orchestrate, "foresee", broken)
    _save({"name": "shot", "steps": [
        {"id": "snap", "prompt": "Take a screenshot of the home page and fix it", "kind": "implement"}]}, git_repo)
    assert cli.main(["workflow", "run", "shot"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("started run #1 of shot")
    assert "note: the warnings of the first steps could not be foreseen (ValueError: the registry is unreadable)" in out
    assert "warning:" not in out
    assert [r["id"] for r in store.list_runs()] == [1]
    assert cli.main(["workflow", "run", "shot", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["warnings"] == [] and shown["warnings_error"] == "ValueError: the registry is unreadable"


def test_the_warning_of_a_step_the_engine_went_past_is_said_by_the_run_s_next_ending(git_repo, store: Store,
                                                                                      kicks):
    from locks import hooks

    _save({"name": "look", "steps": [
        {"id": "see", "prompt": "Open the settings page in the browser and fix what is off", "kind": "implement"},
        {"id": "check", "prompt": "Make the settings form validate its fields", "kind": "implement"},
        {"id": "polish", "prompt": "Polish the settings module", "kind": "implement", "needs": ["check"]}]},
        git_repo)
    started = workflows.start(store, "look", git_repo, session_id="s1")
    # see and check start together; see passes, and the engine goes past it while check runs.
    flow.work(store, parallel=False, runner=_runner([ok(), bad(Failure.SPEC_BUG, "cannot be read")], []))
    see, check = store.run_tasks(started["id"])
    assert see["status"] == "done" and check["status"] == "replan"
    text, ids = hooks.endings(store, "s1", claim="stop")
    assert ids == [check["id"]]
    ending = text[text.index(f"#{check['id']} [replan]"):].splitlines()
    assert ending[1].startswith(f"  step see (#{see['id']}) ran with a warning: this task needs a browser "
                                "and workers have none")

    # Said once: the run's later endings do not repeat what the session already heard.
    workflows.retry(store, started["id"], "check")
    flow.work(store, runner=_runner([ok(), ok()], []))
    assert workflows.status(store, started["id"])["state"] == "done"
    text, ids = hooks.endings(store, "s1", claim="stop")
    polish = store.run_tasks(started["id"])[-1]
    assert ids == [polish["id"]] and "ran with a warning" not in text


def test_a_step_that_stops_waits_for_the_person_then_the_run_goes_on(git_repo, store: Store, kicks):
    _save(CHAIN, git_repo)
    started = workflows.start(store, "chain", git_repo, {"goal": "the export"}, session_id="s1")
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok(), bad(Failure.SPEC_BUG, "cannot be read")], seen))
    status = workflows.status(store, started["id"])
    assert status["state"] == "waits on the orchestrator" and status["waiting"] == ["check"]
    assert [s["state"] for s in status["steps"]] == ["done", "replan", "pending"]
    # The session hears of the stop.
    assert status["steps"][1]["task"] in [t["id"] for t in store.unreported("s1")]

    with pytest.raises(workflows.WorkflowError, match="waits on"):
        workflows.retry(store, started["id"], "polish")
    again = workflows.retry(store, started["id"], "check")
    assert again["workflow_step"] == "check"
    with pytest.raises(workflows.WorkflowError, match="nothing to retry"):
        workflows.retry(store, started["id"], "check")
    assert not any(lane["paused"] for lane in store.lanes())  # the retry opened the lane its stop paused
    flow.work(store, runner=_runner([ok(), ok()], seen))
    status = workflows.status(store, started["id"])
    assert status["state"] == "done" and status["steps"][1]["tries"] == 2


def test_two_steps_a_third_waits_on_queue_it_once(git_repo, store: Store, kicks):
    _save({"name": "fan", "steps": [
        {"id": "a", "prompt": "Write part a", "kind": "implement"},
        {"id": "b", "prompt": "Write part b", "kind": "implement", "needs": ["a"]},
        {"id": "c", "prompt": "Write part c", "kind": "implement", "needs": ["a"]},
        {"id": "d", "prompt": "Join them", "kind": "implement", "needs": ["b", "c"]}]}, git_repo)
    started = workflows.start(store, "fan", git_repo)
    flow.work(store, parallel=False, runner=_runner([ok()] * 4, []))
    tasks = store.run_tasks(started["id"])
    assert [t["workflow_step"] for t in tasks] == ["a", "b", "c", "d"]
    assert workflows.status(store, started["id"])["state"] == "done"
    # Advancing again finds nothing more to queue.
    assert workflows.advance(store, tasks[1]) == []


def test_a_cancelled_run_starts_nothing_more(git_repo, store: Store, kicks):
    _save(CHAIN, git_repo)
    started = workflows.start(store, "chain", git_repo, {"goal": "x"})
    stopped = workflows.cancel(store, started["id"])
    assert stopped == [store.run_tasks(started["id"])[0]["id"]]
    status = workflows.status(store, started["id"])
    assert status["state"] == "cancelled" and status["steps"][0]["state"] == "cancelled"
    with pytest.raises(workflows.WorkflowError, match="cancelled"):
        workflows.retry(store, started["id"], "build")
    assert workflows.advance(store, {**store.run_tasks(started["id"])[0], "status": "done"}) == []


def test_a_run_needs_its_inputs_and_only_those(git_repo, store: Store, kicks):
    _save(CHAIN, git_repo)
    with pytest.raises(workflows.WorkflowError, match="needs goal"):
        workflows.start(store, "chain", git_repo, {})
    with pytest.raises(workflows.WorkflowError, match="takes no input colour"):
        workflows.start(store, "chain", git_repo, {"goal": "x", "colour": "red"})
    assert store.list_runs() == []


def test_the_cli_makes_changes_runs_and_shows(git_repo, kicks, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    assert cli.main(["workflow", "new", "ship", "--description", "build and test"]) == 0
    assert cli.main(["workflow", "step", "set", "ship", "do", "--prompt", "Build {goal}", "--kind", "implement"]) == 0
    assert cli.main(["workflow", "step", "add", "ship", "test", "--prompt", "Test {goal}", "--kind", "test",
                     "--verify", "pytest -q"]) == 0
    assert cli.main(["workflow", "step", "set", "ship", "test", "--clear", "verify"]) == 0
    assert cli.main(["workflow", "copy", "ship", "ship2"]) == 0
    assert cli.main(["workflow", "step", "rm", "ship2", "test"]) == 0
    assert cli.main(["workflow", "validate", "ship"]) == 0
    capsys.readouterr()
    assert cli.main(["workflow", "show", "ship", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in shown["steps"]] == ["do", "test"] and "verify" not in shown["steps"][1]
    assert cli.main(["workflow", "list"]) == 0
    assert "ship2" in capsys.readouterr().out
    assert cli.main(["workflow", "step", "add", "ship", "x", "--prompt", "{nope}"]) == 1
    assert "not among the inputs" in capsys.readouterr().err

    assert cli.main(["workflow", "run", "ship", "the export", "--json"]) == 0
    run_status = json.loads(capsys.readouterr().out)
    assert run_status["state"] == "running" and run_status["now"] == ["do"]
    assert cli.main(["workflow", "status"]) == 0
    out = capsys.readouterr().out
    assert f"#{run_status['id']} ship — running" in out and "step do (1/2): queued" in out
    assert cli.main(["workflow", "retry", str(run_status["id"]), "do"]) == 1
    assert cli.main(["workflow", "cancel", str(run_status["id"])]) == 0
    assert cli.main(["workflow", "rm", "ship2", "--scope", "user"]) == 0
    assert cli.main(["workflow", "run", "feature"]) == 1
    assert "needs goal" in capsys.readouterr().err


def test_the_cli_reads_writes_files_and_says_where_a_stopped_run_waits(git_repo, store: Store, kicks, capsys,
                                                                       monkeypatch, tmp_path):
    monkeypatch.chdir(git_repo)
    project.enroll(git_repo)
    draft = tmp_path / "w.json"
    draft.write_text(json.dumps(CHAIN))
    assert cli.main(["workflow", "save", str(draft), "--scope", "project", "--name", "chained"]) == 0
    assert cli.main(["workflow", "save", str(draft), "--scope", "project", "--name", "chained"]) == 1
    assert cli.main(["workflow", "validate", "--file", str(draft)]) == 0
    assert cli.main(["workflow", "list", "--json"]) == 0
    assert "chained" in {w["name"] for w in json.loads(capsys.readouterr().out.splitlines()[-1])}
    assert cli.main(["workflow", "show", "chained"]) == 0
    out = capsys.readouterr().out
    assert "check — check after build [kind review-routine]" in out and "inputs: goal" in out
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    assert cli.main(["workflow", "edit", "chained"]) == 1
    assert "no editor here" in capsys.readouterr().err
    assert cli.main(["workflow", "new", "fresh", "--from", "bugfix", "--scope", "project"]) == 0
    assert cli.main(["workflow", "run", "chained", "--input", "goal=the export", "--input", "oops"]) == 1
    assert cli.main(["workflow", "run", "chained", "--input", "goal=the export"]) == 0
    capsys.readouterr()
    run_id = store.list_runs()[0]["id"]
    flow.work(store, runner=_runner([ok(), bad(Failure.SPEC_BUG, "cannot")], []))
    assert cli.main(["workflow", "status", str(run_id)]) == 0
    out = capsys.readouterr().out
    assert "waits on the orchestrator" in out and "✓ build" in out and f"retry {run_id} <step>" in out
    assert "work so far: branch locks/task-" in out
    assert cli.main(["workflow", "status", "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["state"] == "waits on the orchestrator"
    assert cli.main(["overview"]) == 0
    out = capsys.readouterr().out
    assert f"run #{run_id} chained — waits on the orchestrator" in out and "(workflow run #" in out
    assert cli.main(["workflow", "retry", str(run_id), "check"]) == 0
    assert cli.main(["workflow", "status", "999"]) == 1


def test_a_step_carries_the_memory_attached_to_it(git_repo, store: Store, kicks):
    from locks import repo

    key = repo.key(git_repo.resolve())
    zone = store.add_note(key, "conventions", "Zones", "Zone ids are UTC offsets, never city names.",
                          author="person", filed_by="person")
    old = store.add_note(key, "decisions", "Old rule", "Use floats for money.", author="person", filed_by="person")
    new = store.add_note(key, "decisions", "Money", "Money is integer cents.", author="person", filed_by="person")
    store.update_note(old, state="replaced", replaced_by=new)
    store.add_note(key, "zones", "Daylight", "Daylight saving shifts zone offsets twice a year.",
                   author="person", filed_by="person")
    elsewhere = store.add_note("other/repo", "zones", "Not ours", "Never shown here.", author="person",
                               filed_by="person")
    problem = store.open_problem("Totals off by one cent", repo=key, symptom="the cart total rounds twice")
    store.add_fix(problem, "round each line", outcome="failed", repo=key, why="the error adds up")

    _save({"name": "zoned", "inputs": ["goal"], "steps": [
        {"id": "build", "prompt": "Build {goal}", "kind": "implement",
         "memory": [zone, f"note:{old}", "topic:zones", f"problem:{problem}"]},
        {"id": "doc", "prompt": "Document {goal}", "kind": "docs"}]}, git_repo)
    assert workflows.load("zoned", git_repo)["steps"][0]["memory"] == [
        f"note:{zone}", f"note:{old}", "topic:zones", f"problem:{problem}"]
    started = workflows.start(store, "zoned", git_repo, {"goal": "the scheduler"})
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok(), ok()], seen))
    build, doc = seen[0]["prompt"], seen[1]["prompt"]
    assert "Memory this workflow attaches to this step" in build
    assert "Zone ids are UTC offsets" in build and "Daylight saving shifts" in build
    assert "Money is integer cents." in build and "Use floats" not in build  # the note that replaced it
    assert "Totals off by one cent" in build and "failed: round each line — the error adds up" in build
    assert "Never shown here" not in build and "Memory this workflow" not in doc
    assert workflows.status(store, started["id"])["state"] == "done"

    # What cannot be read here stops the run before it starts.
    store.update_note(zone, state="dropped")
    with pytest.raises(workflows.WorkflowError) as err:
        workflows.start(store, "zoned", git_repo, {"goal": "x"})
    assert f"note #{zone} was dropped" in str(err.value)
    workflows.edit("zoned", git_repo, lambda d: workflows.set_step(d, "build", {"memory": [
        f"note:{elsewhere}", "topic:nothing", "problem:999"]}))
    with pytest.raises(workflows.WorkflowError) as err:
        workflows.start(store, "zoned", git_repo, {"goal": "x"})
    for part in (f"no note #{elsewhere} in this project", "no problem #999"):
        assert part in str(err.value)
    # A topic with no note yet (a new project has none) is no reason not to start: it is said.
    workflows.edit("zoned", git_repo, lambda d: workflows.set_step(d, "build", {"memory": ["topic:nothing"]}))
    started = workflows.start(store, "zoned", git_repo, {"goal": "x"})
    assert started["gaps"] and "topic 'nothing' holds no note in this project yet" in started["gaps"][0]
    with pytest.raises(workflows.WorkflowError, match="memory is a list"):
        workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "do", "memory": ["zone:1"]}]})


def test_the_cli_attaches_and_clears_memory(git_repo, store: Store, kicks, capsys, monkeypatch):
    from locks import repo

    monkeypatch.chdir(git_repo)
    note = store.add_note(repo.key(git_repo.resolve()), "zones", "Zones", "UTC offsets.", author="person",
                          filed_by="person")
    assert cli.main(["workflow", "new", "z"]) == 0
    assert cli.main(["workflow", "step", "set", "z", "do", "--memory", f"{note},topic:zones"]) == 0
    capsys.readouterr()
    assert cli.main(["workflow", "show", "z"]) == 0
    assert f"+ memory: note:{note}, topic:zones" in capsys.readouterr().out
    assert cli.main(["workflow", "step", "set", "z", "do", "--memory", "zone:3"]) == 1
    assert cli.main(["workflow", "step", "set", "z", "do", "--clear", "memory"]) == 0
    assert "memory" not in workflows.load("z", git_repo)["steps"][0]


def test_a_global_workflow_in_use_is_held_and_its_changes_wait(git_repo, tmp_path, store: Store, kicks, capsys,
                                                                monkeypatch):
    """One workflow in the user scope, used by two projects at once: no change reaches it under them."""
    other = tmp_path / "other"
    other.mkdir()
    import subprocess

    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *args], cwd=other, check=True, capture_output=True)
    path = _save(CHAIN, git_repo)
    assert json.loads(path.read_text())["version"] == 1
    first = workflows.start(store, "chain", git_repo, {"goal": "a"})
    second = workflows.start(store, "chain", other, {"goal": "b"})
    assert workflows.in_use(path) == [first["id"], second["id"]]

    saved = workflows.edit("chain", git_repo, lambda d: workflows.set_step(d, "polish", {"title": "Shine"}))
    assert saved.held_by == [first["id"], second["id"]] and saved.path == workflows.pending_path(path)
    assert json.loads(path.read_text())["version"] == 1  # the definition itself did not move
    # A second change builds on the waiting one.
    workflows.edit("chain", git_repo, lambda d: workflows.set_step(d, "check", {"title": "Look"}))
    waiting = json.loads(workflows.pending_path(path).read_text())
    assert waiting["version"] == 2 and {s["id"]: s.get("title") for s in waiting["steps"]} == {
        "build": None, "check": "Look", "polish": "Shine"}
    with pytest.raises(workflows.WorkflowError, match="in use by run"):
        workflows.remove("chain", git_repo, "user")
    with pytest.raises(workflows.WorkflowError, match="in use by run"):
        workflows.apply_pending("chain", git_repo)
    assert [w["held_by"] for w in workflows.pending(git_repo)] == [[first["id"], second["id"]]]

    # A run keeps the copy it started with; the change waits for the last run to end.
    workflows.cancel(store, second["id"])
    assert workflows.pending_path(path).is_file()
    flow.work(store, runner=_runner([ok(), ok(), ok()], []))
    assert workflows.status(store, first["id"])["state"] == "done"
    assert workflows.status(store, first["id"])["steps"][2]["title"] == "polish"
    assert not workflows.pending_path(path).exists()
    live = json.loads(path.read_text())
    assert live["version"] == 2 and live["steps"][2]["title"] == "Shine"
    assert workflows.in_use(path) == []

    # --now writes at once, and a run started now uses the new version.
    third = workflows.start(store, "chain", git_repo, {"goal": "c"})
    assert workflows.edit("chain", git_repo, lambda d: d.update(description="now"), now=True).held_by == []
    assert json.loads(path.read_text())["version"] == 3
    assert workflows.status(store, third["id"])["version"] == 2

    monkeypatch.chdir(git_repo)
    assert cli.main(["workflow", "step", "set", "chain", "build", "--title", "Make"]) == 0
    assert "in use by run" in capsys.readouterr().out
    assert cli.main(["workflow", "show", "chain"]) == 0
    out = capsys.readouterr().out
    assert "chain v3" in out and f"in use by run #{third['id']}" in out and "a pending change waits" in out
    assert cli.main(["workflow", "pending"]) == 0
    assert f"waits for run #{third['id']}" in capsys.readouterr().out
    assert cli.main(["workflow", "pending", "chain", "--apply"]) == 1
    assert cli.main(["workflow", "pending", "chain", "--drop"]) == 0
    assert cli.main(["workflow", "pending", "chain", "--drop"]) == 1
    assert cli.main(["workflow", "step", "set", "chain", "build", "--title", "Make", "--now"]) == 0
    assert cli.main(["workflow", "rm", "chain", "--scope", "user"]) == 1
    assert cli.main(["workflow", "rm", "chain", "--scope", "user", "--force"]) == 0
    assert workflows.status(store, third["id"])["state"] == "running"  # it goes on with its copy


def test_a_step_runs_as_the_role_it_names_only_when_the_project_s_team_has_it(git_repo, store: Store, kicks):
    from locks import repo, teams

    with pytest.raises(workflows.WorkflowError, match="role is a role's name"):
        workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "p", "role": "Bad Name"}]})
    _save({"name": "roled", "steps": [
        {"id": "audit", "prompt": "Review, without changing any file, the login", "kind": "review-critical",
         "role": "security-auditor"},
        {"id": "fix", "prompt": "Fix the login", "kind": "implement", "role": "security-auditor"}]}, git_repo)
    started = workflows.start(store, "roled", git_repo)
    first = store.get_task(started["queued"][0])
    assert json.loads(first["options"])["step_role"] == "security-auditor"
    roles_seen: list[str] = []

    def runner(text, where, store, options, **kw):
        report = run(text, where, store, options, registry={}, launcher=lambda spec: ok(), adapters=[],
                     classifier=kind("review-critical"), **kw)
        roles_seen.append(report.plan.role.name)
        return report

    teams.create("narrow", ["reviewer", "implementer"])
    teams.use(repo.key(git_repo.resolve()), "narrow")
    flow.work(store, runner=runner)
    # not in the project's team: the team picks, and the plan says why
    assert roles_seen[0] == "reviewer"
    reasons = store.last_event(first["id"], "planned")["data"]["reasons"]
    assert any("names security-auditor, which is not in the narrow team" in r for r in reasons)


def test_a_step_role_in_the_team_is_the_one_it_runs_as(git_repo, store):
    from locks.orchestrate import Options

    dry = run("x", git_repo, store, Options(dry_run=True, step_role="security-auditor"), registry={},
              classifier=kind("review-critical"))
    assert dry.plan.role.name == "security-auditor"
    assert any("the workflow step runs as security-auditor" in r for r in dry.plan.reasons)
    assert Options(step_role="x").stored()["step_role"] == "x"


# --- context boundaries --------------------------------------------------------------


def test_what_the_steps_before_said_is_context_never_the_step_s_criteria(git_repo, store: Store, kicks):
    from locks import criteria

    _save(CHAIN, git_repo)
    started = workflows.start(store, "chain", git_repo, {"goal": "the export"})
    flow.work(store, runner=_runner([ok("built it"), ok("no problems"), ok("polished")], []))
    polish = store.run_tasks(started["id"])[2]
    assert "> - build (task #" in polish["body"] and "> - check (task #" in polish["body"]
    assert criteria.items(polish["body"]) == []


def test_a_review_step_is_blind_and_the_step_after_it_gets_its_findings_as_records(git_repo, store: Store, kicks):
    _save(CHAIN, git_repo)
    workflows.start(store, "chain", git_repo, {"goal": "the export"})
    found = ({"where": "export.py:12", "what": "drops the last row", "evidence_class": "deterministic",
              "cause": "introduced", "proof": "python probe.py"},)
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built it, all green"), WorkerResult(True, None, "one problem", "read it",
                                                                            findings=found), ok("polished")], seen))
    _build, check, polish = seen
    # The review reads the change, never what the build said of it.
    assert "built it, all green" not in check["prompt"] and "git diff" in check["prompt"]
    assert "Its findings:" in polish["prompt"] and "export.py:12: drops the last row" in polish["prompt"]
    # A step further back is still summarized, without its findings.
    assert "- build (task #" in polish["prompt"]


def test_a_review_of_a_change_checks_the_spec_links_where_workers_were_told_to_make_them(
        git_repo, store: Store, kicks, monkeypatch):
    from locks import specs

    from .livespec_fixture import build
    from .test_specs import _spec_repo

    monkeypatch.setenv("LOCKS_LIVESPEC", "on")
    _spec_repo(git_repo)
    build(git_repo)
    _save(CHAIN, git_repo)
    workflows.start(store, "chain", git_repo, {"goal": "the export"})
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built it"), ok("no problems"), ok("polished")], seen))
    building, review = seen[0], seen[1]
    assert specs.LINK_CHECK in review["prompt"] and specs.LINK_CHECK not in building["prompt"]
    # With no livespec index there, workers were never told to link: the review is not asked to check it.
    (git_repo / ".mcp-docs" / "docs.db").unlink()
    workflows.start(store, "chain", git_repo, {"goal": "the export again"})
    again: list[dict] = []
    flow.work(store, runner=_runner([ok("built it"), ok("no problems"), ok("polished")], again))
    assert len(again) >= 2 and all(specs.LINK_CHECK not in step["prompt"] for step in again)


def test_a_review_with_no_change_to_read_gets_what_came_before(git_repo, store: Store, kicks):
    _save({"name": "critique", "inputs": ["goal"], "steps": [
        {"id": "plan", "prompt": "Plan {goal}", "kind": "plan"},
        {"id": "critique", "prompt": "Review the plan the step before wrote", "kind": "review-critical",
         "needs": ["plan"]}]}, git_repo)
    workflows.start(store, "critique", git_repo, {"goal": "the export"})
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("step 1: split the writer"), ok("the plan holds")], seen))
    # A plan keeps no branch: there is no diff to read, so the plan itself is what it reviews.
    assert "step 1: split the writer" in seen[1]["prompt"] and "git diff" not in seen[1]["prompt"]


def test_the_bundled_feature_builds_with_its_tests_and_a_clean_review_ends_it(git_repo, store: Store, kicks):
    defn = workflows.load("feature", git_repo, "bundled")
    assert [s["id"] for s in defn["steps"]] == ["build", "review", "refute", "fix", "recheck"]
    assert "with the tests that prove it works" in defn["steps"][0]["prompt"]
    started = workflows.start(store, "feature", git_repo, {"goal": "the export"})
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built it"), ok("no problems")], seen))
    # Two workers: the build and its review. The ledger steps had nothing to decide.
    assert len(seen) == 2 and workflows.status(store, started["id"])["state"] == "done"
    assert workflows.load("bugfix", git_repo, "bundled")["steps"][0]["kind"] == "debug-repro"
    # review-fix reads code with no change under review: its findings are not excused as pre-existing.
    assert "so `unknown`" in workflows.load("review-fix", git_repo, "bundled")["steps"][0]["prompt"]


def test_a_confirmed_row_the_fix_only_wrote_down_stays_open_and_goes_to_the_person(git_repo, store: Store, kicks):
    found = ({"where": "src/chart.py:3", "what": "the animation has no safety timeout",
              "evidence_class": "deterministic", "cause": "introduced", "proof": "grep -n setTimeout src/chart.py"},)
    fixed = WorkerResult(True, None, "documented it as known debt", "wrote it down",
                         verdicts=({"id": "F1", "verdict": "fixed", "proof": "the note is in CLAUDE.md"},))
    results = [ok("built"), WorkerResult(True, None, "one problem", "read it", findings=found),
               ok("wrote it down"), fixed, ok("wrote it down again"), fixed]
    seen: list[str] = []

    def launch(spec):
        seen.append(spec.prompt)
        if "Edit" not in spec.disallowed_tools:
            fixing = "Ledger (fix" in spec.prompt
            target = spec.target_dir / ("CLAUDE.md" if fixing else "src/chart.py")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(target.read_text() + "known debt: F1\n" if fixing and target.exists() else "x = 1\n")
        return results.pop(0)

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    started = workflows.start(store, "feature", git_repo, {"goal": "the chart"})
    flow.work(store, runner=runner)
    fix_brief = next(p for p in seen if "Ledger (fix" in p)
    assert "writing it down" in fix_brief and "never edit those files unless the row is about them" in fix_brief
    rechecks = [t for t in store.run_tasks(started["id"]) if t["workflow_step"] == "recheck"]
    assert json.loads(rechecks[0]["options"])["ledger"]["noted_only"] == ["F1"]
    # The re-check called it fixed twice; the fix only wrote it down, so it is still open: the person decides.
    assert [store.last_event(t["id"], "ledger_decided")["data"]["survivors"] for t in rechecks] == [["F1"], ["F1"]]
    assert store.get_task(rechecks[-1]["id"])["status"] == "blocked"
    assert workflows.status(store, started["id"])["state"] != "done"


def test_a_run_says_what_it_takes_before_the_first_try(git_repo, store: Store, kicks, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    _save({"name": "change", "inputs": ["goal", "check"], "steps": [
        {"id": "build", "prompt": "Build {goal}; then {check}", "kind": "implement", "memory": ["topic:conventions"]},
        {"id": "review", "prompt": "Review {goal}", "kind": "review-routine", "needs": ["build"]}]}, git_repo)
    # The listing says what a run asks for.
    assert cli.main(["workflow", "list"]) == 0
    listed = capsys.readouterr().out
    assert "inputs: goal (required), check (required) · memory: topic:conventions" in listed
    # Text without a name fills the one input left; with two left, the error names them all.
    assert cli.main(["workflow", "run", "change", "the export"]) == 1
    assert "--input goal='…' --input check='…'" in capsys.readouterr().err
    # A dry run checks everything and queues nothing; the memory it would start without is said.
    assert cli.main(["workflow", "run", "change", "the export", "--input", "check=npm test", "--dry-run",
                     "--verify", "npm run lint"]) == 0
    dry = capsys.readouterr().out
    assert "nothing is queued" in dry and "goal: the export" in dry and "holds no note in this project yet" in dry
    assert "build (implement), checked by `npm run lint`" in dry and "review (review-routine) after build\n" in dry
    assert store.list_runs() == []
    assert cli.main(["workflow", "run", "change", "the export", "--input", "check=npm test", "--dry-run",
                     "--verify", "npm run lint", "--json"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert [s["verify"] for s in planned["steps"]] == ["npm run lint", None] and planned["gaps"]
    # The run's check goes to the step that writes, never to the review.
    assert cli.main(["workflow", "run", "change", "the export", "--input", "check=npm test",
                     "--verify", "npm run lint"]) == 0
    assert "note: step 'build'" in capsys.readouterr().out
    (build,) = store.run_tasks(store.list_runs()[0]["id"])
    assert json.loads(build["options"])["verify"] == "npm run lint"
    assert workflows.step_check({"kind": "review-routine"}, {"verify": "npm run lint"}) is None


def test_a_flag_a_command_does_not_take_says_what_was_meant(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["workflow", "run", "change", "the goal", "two words", "--allw", "x"])
    assert stop.value.code == 2
    said = capsys.readouterr().err
    assert "--allw: did you mean --allow?" in said and "--input name=value" in said
    with pytest.raises(SystemExit):
        cli.main(["run", "fix it", "--input", "goal=x"])
    assert "--input is taken by locks workflow run, locks loop add, not by `locks run`" in capsys.readouterr().err


def test_the_status_says_how_far_each_run_got_in_a_line(git_repo, store: Store, kicks, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    _save(CHAIN, git_repo)
    brief = "Build the export of every order " + "with all its details " * 20
    started = workflows.start(store, "chain", git_repo, {"goal": brief})
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built it"), ok("no problems"), ok("polished")], seen))
    shown = workflows.status(store, started["id"], detail=True)
    build = shown["steps"][0]
    # A done step says the commit it kept and the files it touched, from git and its events.
    assert build["attempt"] == 1 and build["files"] == ["step1.txt"] and build["commit"].split()[1:]
    assert cli.main(["workflow", "status"]) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert line.startswith(f"#{started['id']} chain — done") and "step polish (3/3)" in line and "1 file(s)" in line
    assert brief not in line
    # One run step by step: its input on one line unless asked for whole.
    assert cli.main(["workflow", "status", str(started["id"])]) == 0
    out = capsys.readouterr().out
    assert "goal: Build the export" in out and brief.strip() not in out
    # A step a line: its state and task, cell, attempts, cost and branch.
    assert "done #1 · sonnet/medium · attempt 1 · $0.10 · branch locks/task-1" in out
    assert cli.main(["workflow", "status", str(started["id"]), "--full"]) == 0
    assert brief.strip() in capsys.readouterr().out


def test_the_status_says_what_a_held_back_step_waits_for(git_repo, store: Store, kicks, capsys, monkeypatch):
    from locks import dispatch

    monkeypatch.chdir(git_repo)
    _save(CHAIN, git_repo)
    other = store.enqueue("rewrite the exporter", repo=str(git_repo.resolve()), cwd=str(git_repo))
    store.claim(other["id"])
    started = workflows.start(store, "chain", git_repo, {"goal": "the export"})
    flow.runnable(store, None, parallel=True,
                  decide=lambda task, ahead: dispatch.Decision(False, f"both change the exporter #{other['id']}"))
    line = f"waits for #{other['id']}: both change the exporter #{other['id']}"
    assert workflows.status(store, started["id"])["steps"][0]["waits"] == line
    assert cli.main(["workflow", "status"]) == 0
    assert line in capsys.readouterr().out
    assert cli.main(["workflow", "status", str(started["id"])]) == 0
    assert line in capsys.readouterr().out


def test_a_running_step_says_its_attempt_and_the_files_it_touched_so_far(git_repo, store: Store):
    from locks import isolate
    from locks.store import home

    from .conftest import git

    task = store.create_task("build it", status="running", source="queue", cwd=str(git_repo))
    ws = isolate.prepare(git_repo, task["id"], home())
    store.add_event(task["id"], "attempt_started", seq=2, cell="sonnet/high", base=ws.base, links=list(ws.links))
    # What the worker committed counts as much as what it has not yet.
    (ws.path / "export.py").write_text("print('rows')\n")
    git(ws.path, "add", "export.py")
    git(ws.path, "commit", "-qm", "export")
    (ws.path / "more.py").write_text("x = 1\n")
    found = workflows.progress(store, {"cwd": str(git_repo)}, store.get_task(task["id"]))
    assert found["attempt"] == 2 and found["files"] == ["export.py", "more.py"] and "phase" in found


def test_the_run_check_reaches_a_step_with_no_kind_once_it_is_known_to_write(git_repo, store: Store):
    from locks.orchestrate import Options

    # A step classified into a writing kind runs the run's check, and a red one is no pass.
    red = run("add the export", git_repo, store, Options(run_verify="exit 1", max_attempts=1), registry={},
              launcher=_worker([ok("built it")], []), classifier=kind("implement"), adapters=[])
    assert red.status == "failed" and "`exit 1` exited 1" in store.attempts(red.task_id)[0]["summary"]
    # One classified into a reading kind never gets it.
    read = run("explain the export", git_repo, store, Options(run_verify="exit 1"), registry={},
               launcher=_worker([ok("it works so")], []), classifier=kind("explore"), adapters=[])
    assert read.status == "done"


def test_a_kindless_step_carries_the_run_check_and_the_dry_run_says_so(git_repo, store: Store, kicks, capsys,
                                                                       monkeypatch):
    monkeypatch.chdir(git_repo)
    _save({"name": "plain", "inputs": ["goal"], "steps": [{"id": "do", "prompt": "{goal}",
                                                           "memory": ["topic:style", "topic:contracts"]}]}, git_repo)
    assert cli.main(["workflow", "run", "plain", "the export", "--verify", "npm test", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "checked by `npm test` if it writes" in out
    # An alias reads as its topic; a topic the project lacks is added before a note can fill it.
    assert "topic 'style' holds no note" in out and "`locks note --topic conventions" in out
    assert "locks notes topic add contracts" in out
    assert cli.main(["workflow", "run", "plain", "the export", "--verify", "npm test"]) == 0
    (task,) = store.run_tasks(store.list_runs()[0]["id"])
    assert json.loads(task["options"])["run_verify"] == "npm test"
    capsys.readouterr()
    assert cli.main(["workflow", "status", "--repo", str(git_repo / "nowhere")]) == 0
    assert capsys.readouterr().out.splitlines() == ["no workflow run here; `locks workflow list` shows what can run"]


def test_the_line_of_a_run_names_the_step_that_waits_on_the_person():
    from locks import workflow_cli

    line = workflow_cli._run_line({"id": 7, "name": "dual", "state": "waits on the orchestrator", "cost_usd": 1.0,
                                   "waiting": ["a"], "steps": [
                                       {"id": "a", "state": "failed", "task": 10},
                                       {"id": "b", "state": "running", "task": 11, "attempt": 1}]})
    assert "step a (1/2): failed #10" in line


def test_a_resumed_step_counts_the_files_of_its_final_ending_not_an_attempts(git_repo, store: Store):
    from locks import workflow_cli

    task = store.create_task("build it", status="interrupted", source="queue", cwd=str(git_repo))
    for _ in range(2):
        store.add_event(task["id"], "finished", status="interrupted", reason="no sign of life",
                        stop={"cause": "died", "reason": "no sign of life"})
    store.update_task(task["id"], status="done")
    files = [f"src/f{i}.ts" for i in range(106)]
    # The run kept the head of the list; the count is the whole of it.
    store.add_event(task["id"], "finished", status="done", branch="locks/task-1", changed=files[:100], changed_n=106)
    found = workflows.progress(store, {"cwd": str(git_repo)}, store.get_task(task["id"]))
    assert found["files_n"] == 106 and len(found["files"]) == 100
    assert "106 file(s): src/f0.ts, src/f1.ts, src/f2.ts +103" in workflow_cli._step_line(
        {"state": "done", "task": task["id"], **found})
    # Without the count (an older ending) the list is the count.
    assert "3 file(s)" in workflow_cli._step_line({"state": "done", "files": ["a", "b", "c"]})


def test_a_steps_check_takes_the_run_inputs_and_an_unfilled_one_is_refused(git_repo, store: Store, kicks, capsys,
                                                                            monkeypatch):
    monkeypatch.chdir(git_repo)
    _save({"name": "checked", "inputs": ["goal", "check"], "steps": [
        {"id": "do", "prompt": "{goal}", "kind": "implement", "verify": "{check} && test -n \"${HOME}\" {a,b}"}]},
        git_repo)
    _save({"name": "loose", "inputs": ["goal"], "steps": [
        {"id": "do", "prompt": "{goal}", "kind": "implement", "verify": "{check}"}]}, git_repo)
    step = {"verify": "{check} && echo ${check} {x,y} {}", "kind": "implement"}
    # The shell's own braces stay; only an input's place is filled.
    assert workflows.step_check(step, {}, {"check": "npm test"}) == "npm test && echo ${check} {x,y} {}"
    # The person's own --verify is taken as typed.
    assert workflows.step_check({"kind": "implement"}, {"verify": "{check}"}, {"check": "x"}) == "{check}"
    assert cli.main(["workflow", "run", "checked", "--input", "goal=the export", "--input", "check=npm test",
                     "--dry-run"]) == 0
    assert 'checked by `npm test && test -n "${HOME}" {a,b}`' in capsys.readouterr().out
    # A check left with a placeholder never runs as `/bin/sh: {check}`: the run is refused, dry or not.
    for dry in (["--dry-run"], []):
        assert cli.main(["workflow", "run", "loose", "the export", *dry]) != 0
        said = capsys.readouterr()
        assert "leaves {check} unfilled" in said.out + said.err
        assert "add `check` to the workflow's inputs" in said.out + said.err
    assert not store.list_runs()
    assert cli.main(["workflow", "run", "checked", "--input", "goal=the export", "--input", "check=npm test"]) == 0
    (task,) = store.run_tasks(store.list_runs()[0]["id"])
    assert json.loads(task["options"])["verify"].startswith("npm test && ")
    capsys.readouterr()
    # An input the workflow takes but the check is given empty is the missing input, said once.
    with pytest.raises(workflows.WorkflowError) as err:
        workflows.prepare(store, "checked", git_repo, {"goal": "x"})
    assert "needs check" in str(err.value) and "unfilled" not in str(err.value)
    assert workflows.unfilled_checks(workflows.load("checked", git_repo), {"goal": "x"}) == [
        "step 'do' checks with `{check} && test -n \"${HOME}\" {a,b}`, which leaves {check} unfilled: "
        "pass it with `--input check='<command>'`"]


TWO = {"name": "two", "inputs": ["goal"], "steps": [
    {"id": "build", "prompt": "Build {goal}", "kind": "implement"},
    {"id": "polish", "prompt": "Polish {goal}", "kind": "implement"},
]}


def test_a_step_says_how_far_its_branch_is_behind_the_base_and_nothing_is_merged(git_repo, store: Store, kicks,
                                                                                 capsys, monkeypatch):
    from locks import hooks
    from locks.ui import api

    from .conftest import git

    base = git(git_repo, "symbolic-ref", "--short", "HEAD")
    _save(TWO, git_repo)
    started = workflows.start(store, "two", git_repo, {"goal": "the export"}, session_id="s1")
    moved = []

    def launch(spec):
        (spec.target_dir / "shared.txt").write_text(f"step {len(moved) + 1}\n")
        if not moved:
            # While the build runs, the person commits on the base: one file both change, one only they do.
            (git_repo / "shared.txt").write_text("theirs\n")
            (git_repo / "theirs.txt").write_text("theirs\n")
            git(git_repo, "add", "-A")
            git(git_repo, "commit", "-q", "-m", "the person's own change")
            moved.append(git(git_repo, "rev-parse", "HEAD"))
        return ok("did it")

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    report = flow.work(store, runner=runner)
    assert [r.status for r in report.ran] == ["done", "done"]
    said = f"behind {base} by 1 commit; both changed: shared.txt; a merge will conflict-check them"
    build, polish = store.run_tasks(started["id"])
    # the polish step started from the build, after the base moved
    at_start = store.events_of(polish["id"], "behind_base")[0]["data"]
    assert at_start["when"] == "start" and at_start["commits"] == 1 and at_start["both"] == ["shared.txt"]
    # the run's end compares each kept branch with the base as it is then
    assert {e["data"]["when"] for e in store.events_of(build["id"], "behind_base")} == {"start", "end"}
    status = workflows.status(store, started["id"], detail=True)
    assert [s.get("behind") for s in status["steps"]] == [said, said]
    # the board reads what was kept, the ending notice says it, and so does `locks workflow status`
    board = api.workflows_view(store, started["repo"])["runs"][0]
    assert [s.get("behind") for s in board["steps"]] == [said, said]
    notice, _ = hooks.endings(store, "s1")
    assert f"step polish (#{polish['id']}): {said}" in notice
    monkeypatch.chdir(git_repo)
    assert cli.main(["workflow", "status", str(started["id"])]) == 0
    assert said in capsys.readouterr().out
    # nothing was merged or rebased: the base is where the person left it
    assert git(git_repo, "rev-parse", base) == moved[0]


def test_a_run_that_stops_at_a_step_measures_its_branches_at_its_end(git_repo, store: Store, kicks):
    from .conftest import git

    _save(TWO, git_repo)
    started = workflows.start(store, "two", git_repo, {"goal": "the export"}, session_id="s1")
    results = [ok("built"), bad(Failure.ENVIRONMENT, "the database is down")]

    def launch(spec):
        (spec.target_dir / "shared.txt").write_text(f"step {3 - len(results)}\n")
        if len(results) == 1:
            # While the polish runs, the person commits on the base.
            (git_repo / "theirs.txt").write_text("theirs\n")
            git(git_repo, "add", "-A")
            git(git_repo, "commit", "-q", "-m", "the person's own change")
        return results.pop(0)

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    report = flow.work(store, runner=runner)
    assert [r.status for r in report.ran] == ["done", "blocked"]
    build, polish = store.run_tasks(started["id"])
    # the run ended at the polish step: each kept branch is measured then, not only when every step passed
    for step in (build, polish):
        ends = [e["data"] for e in store.events_of(step["id"], "behind_base") if e["data"]["when"] == "end"]
        assert ends and ends[-1]["commits"] == 1, step["id"]


def test_a_step_on_a_base_that_did_not_move_says_nothing(git_repo, store: Store, kicks):
    _save(TWO, git_repo)
    started = workflows.start(store, "two", git_repo, {"goal": "the export"}, session_id="s1")
    flow.work(store, runner=_runner([ok("built"), ok("polished")], []))
    status = workflows.status(store, started["id"], detail=True)
    assert [s.get("behind") for s in status["steps"]] == [None, None]


# --- parts: a step that fans out over a plan -----------------------------------------

PLAN_PARTS = ({"id": "api", "prompt": "Build the orders endpoint:\n- GET /orders lists them\n- GET /orders/1 is one",
               "surfaces": ["api/**"]},
              {"id": "web", "prompt": "Build the orders page", "surfaces": ["web/**"]})
CONTRACT = "- `Order` is {id: int, total_cents: int}\n- GET /orders returns a list of Order"
FANNED = {"name": "fanned", "inputs": ["goal"], "steps": [
    {"id": "plan", "prompt": "Plan {goal} in parts", "kind": "plan"},
    {"id": "build", "prompt": "Build your part of: {goal}", "kind": "implement", "fanout": "plan",
     "max_turns": 40},
    {"id": "after", "prompt": "Join and polish {goal}", "kind": "implement", "integrate": True},
]}


def planned(parts=PLAN_PARTS, contract=CONTRACT) -> WorkerResult:
    return WorkerResult(True, None, "split in parts", evidence="read the code", parts=tuple(parts),
                        contract=contract)


def _parts_runner(results, seen):
    """Each part writes inside its own surfaces; the others write a file at the top."""
    results = list(results)

    def launch(spec):
        seen.append({"prompt": spec.prompt, "parts": spec.parts, "dir": spec.target_dir,
                     "files": sorted(str(f.relative_to(spec.target_dir)) for f in spec.target_dir.rglob("*")
                                     if f.is_file() and ".git" not in f.relative_to(spec.target_dir).parts)})
        if "Edit" not in spec.disallowed_tools:
            mine = next((p["id"] for p in PLAN_PARTS if f"Your part: {p['id']}" in spec.prompt), None)
            target = spec.target_dir / (f"{mine}/{mine}.py" if mine else f"step{len(seen)}.txt")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{mine or 'work'}\n")
        return results.pop(0)

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    return runner


def test_a_fanout_names_a_plan_step_before_it_that_it_waits_on():
    def steps(*extra):
        return {"name": "x", "steps": [{"id": "plan", "prompt": "p", "kind": "plan"},
                                       {"id": "look", "prompt": "l", "kind": "explore", "needs": []}, *extra]}

    for step, said in (
            ({"id": "b", "prompt": "b", "fanout": "ghost"}, "fanout names 'ghost', which is no step before it"),
            ({"id": "b", "prompt": "b", "fanout": "look", "needs": ["look"]},
             "the parts come from a plan step's result"),
            ({"id": "b", "prompt": "b", "fanout": "plan", "needs": ["look"]}, "does not wait on it"),
            ({"id": "b", "prompt": "b", "fanout": "plan", "needs": ["plan"], "ledger": "fix"},
             "a fanout step is no ledger step"),
            ({"id": "b", "prompt": "b", "integrate": "yes"}, "integrate is true or false"),
            ({"id": "b", "prompt": "b", "integrate": True}, "joins the parts of a fanout step it waits on")):
        with pytest.raises(workflows.WorkflowError, match=said):
            workflows.normalize(steps(step))
    later = workflows.normalize(steps({"id": "b", "prompt": "b", "fanout": "plan", "needs": ["look", "plan"]}))
    assert later["steps"][2]["fanout"] == "plan"


def test_only_an_integrate_step_waits_on_a_step_run_in_parts():
    # What follows starts from one branch: a step run in parts keeps one per part, so only
    # an integrate step, which joins them, may wait on it.
    plan = {"id": "plan", "prompt": "p", "kind": "plan"}
    build = {"id": "build", "prompt": "b", "fanout": "plan", "needs": ["plan"]}
    for after_build in ({"id": "review", "prompt": "r", "kind": "review-routine", "needs": ["build"]},
                        {"id": "polish", "prompt": "o", "needs": ["plan", "build"]}):
        with pytest.raises(workflows.WorkflowError, match="waits on 'build', a step run in parts"):
            workflows.normalize({"name": "x", "steps": [plan, build, after_build]})
    joined = workflows.normalize({"name": "x", "steps": [plan, build, {"id": "join", "prompt": "j", "integrate": True},
                                                          {"id": "review", "prompt": "r", "needs": ["join"]}]})
    assert [s["id"] for s in joined["steps"]] == ["plan", "build", "join", "review"]


@pytest.mark.usefixtures("no_verifier")
def test_a_step_fans_out_into_one_task_per_part_and_what_needs_it_waits_for_all(git_repo, store: Store, kicks):
    _save(FANNED, git_repo)
    started = workflows.start(store, "fanned", git_repo, {"goal": "the orders view\n- it pages by 50"})
    seen: list[dict] = []
    flow.work(store, runner=_parts_runner([planned(), ok("api built"), ok("web built"), ok("polished")], seen))
    plan_task, api, web, polish = store.run_tasks(started["id"])
    # The plan was asked for its parts, and its finished event keeps them.
    assert seen[0]["parts"] and not seen[1]["parts"]
    kept = store.final_event(plan_task["id"])["data"]
    assert [p["id"] for p in kept["parts"]] == ["api", "web"] and kept["contract"] == CONTRACT
    # One task per part: the part's prompt after the step's, its surfaces, the step's other fields.
    for task, part in ((api, PLAN_PARTS[0]), (web, PLAN_PARTS[1])):
        opts = json.loads(task["options"])
        assert task["workflow_step"] == "build" and opts["surfaces"] == part["surfaces"]
        assert opts["part"]["id"] == part["id"] and opts["max_turns"] == 40 and opts["kind"] == "implement"
        assert task["body"].startswith("Build your part of:") and part["prompt"] in task["body"]
        # The contract verbatim, quoted as context; the request quoted too: its items are no criteria here.
        assert "\n".join(f"> {line}" for line in CONTRACT.splitlines()) in task["body"]
        assert "> - it pages by 50" in task["body"]
        assert f"[fanned#{started['id']} build/{part['id']}]" in task["title"]
    from locks import criteria

    # A part's criteria are its own prompt's items, never the whole request's nor what the plan said.
    assert criteria.items(api["body"]) == ["GET /orders lists them", "GET /orders/1 is one"]
    assert criteria.items(web["body"]) == [] and "it pages by 50" not in criteria.brief(api["body"])
    assert "> - plan (task #" in api["body"]
    # The step after waits for every part.
    assert polish["workflow_step"] == "after" and polish["id"] > max(api["id"], web["id"])
    assert workflows.status(store, started["id"])["state"] == "done"
    # A run's points are its steps', and a step run in parts counts each part.
    status = workflows.status(store, started["id"])
    each = {t["id"]: store.get_task(t["id"])["points"] for t in (plan_task, api, web, polish)}
    assert all(each.values()) and status["steps"][1]["points"] == each[api["id"]] + each[web["id"]]
    assert status["points"] == sum(each.values())


@pytest.mark.usefixtures("no_verifier")
def test_a_part_that_stops_stops_the_run_and_a_retry_runs_only_the_parts_that_did_not_pass(
        git_repo, store: Store, kicks, capsys, monkeypatch):
    _save(FANNED, git_repo)
    started = workflows.start(store, "fanned", git_repo, {"goal": "the orders view"}, session_id="s1")
    seen: list[dict] = []
    flow.work(store, runner=_parts_runner([planned(), ok("api built"),
                                           bad(Failure.SPEC_BUG, "the page has no route")], seen))
    status = workflows.status(store, started["id"], detail=True)
    build = status["steps"][1]
    assert status["state"] == "waits on the orchestrator" and status["waiting"] == ["build"]
    assert build["state"] == "replan"
    # The parts show under their step, each with its own task and state.
    assert [(p["id"], p["state"]) for p in build["parts"]] == [("api", "done"), ("web", "replan")]
    assert status["steps"][2]["state"] == "pending"
    monkeypatch.chdir(git_repo)
    assert cli.main(["workflow", "status", str(started["id"])]) == 0
    out = capsys.readouterr().out
    assert "    ✓ api" in out and "    ! web" in out and "replan #" in out
    from locks.ui import api as ui_api

    board = ui_api.workflows_view(store, started["repo"])["runs"][0]
    assert [p["id"] for p in board["steps"][1]["parts"]] == ["api", "web"]

    failed = build["parts"][1]["task"]
    assert cli.main(["workflow", "retry", str(started["id"]), "build"]) == 0
    assert "web queued again as #" in capsys.readouterr().out
    again = store.run_tasks(started["id"])[-1]
    assert json.loads(again["options"])["part"]["id"] == "web" and again["id"] > failed
    with pytest.raises(workflows.WorkflowError, match="nothing to retry"):
        workflows.retry(store, started["id"], "build")
    flow.work(store, runner=_parts_runner([ok("web built"), ok("polished")], seen))
    status = workflows.status(store, started["id"])
    assert status["state"] == "done" and [p["tries"] for p in status["steps"][1]["parts"]] == [1, 2]


@pytest.mark.usefixtures("no_verifier")
def test_work_queued_after_a_run_stopped_at_a_part_is_told_a_retry_that_runs(git_repo, store: Store, kicks):
    import re

    from locks import after

    _save(FANNED, git_repo)
    started = workflows.start(store, "fanned", git_repo, {"goal": "the orders view"})
    flow.work(store, runner=_parts_runner([planned(), ok("api built"),
                                           bad(Failure.SPEC_BUG, "the page has no route")], []))
    held = store.enqueue("document it", repo=started["repo"], cwd=str(git_repo),
                         options={after.KEY: {"run": started["id"]}})
    assert "stopped at step 'build' (part web" in after.waiting(store, held)
    with pytest.raises(after.AfterError) as refused:
        after.target(store, run_id=started["id"])
    said = str(refused.value)
    assert "step 'build' (part web" in said
    run_id, step = re.search(r"`locks workflow retry (\d+) (\S+)`", said).groups()
    # The command it names is one that runs: the step, whose stopped parts go again.
    assert workflows.retry(store, int(run_id), step)["parts_retried"][0][0] == "web"


def test_a_plan_of_one_part_is_today_s_single_step(git_repo, store: Store, kicks):
    _save(FANNED, git_repo)
    started = workflows.start(store, "fanned", git_repo, {"goal": "the orders view\n- it pages by 50"})
    one = ({"id": "all", "prompt": "Build it as one: the view and its endpoint", "surfaces": []},)
    flow.work(store, runner=_parts_runner([planned(one, ""), ok("built"), ok("polished")], []))
    _plan, build, _polish = store.run_tasks(started["id"])
    opts = json.loads(build["options"])
    assert "part" not in opts and "surfaces" not in opts and build["title"] == "[fanned#1 build] the orders view"
    # The request is read as today's step reads it: its items are the step's criteria.
    assert "Build your part of: the orders view\n- it pages by 50" in build["body"]
    assert "Build it as one" in build["body"]
    assert workflows.status(store, started["id"])["state"] == "done"


def test_parts_whose_surfaces_overlap_stop_the_run_at_the_plan_with_the_paths_named(git_repo, store: Store, kicks):
    _save(FANNED, git_repo)
    started = workflows.start(store, "fanned", git_repo, {"goal": "the orders view"}, session_id="s1")
    plan_task = store.run_tasks(started["id"])[0]
    store.claim(plan_task["id"])
    store.update_task(plan_task["id"], status="done")
    # A plan read before its parts were checked: the engine checks them again, with surfaces.disjoint.
    store.add_event(plan_task["id"], "finished", status="done", branch=None, changed=[],
                    parts=[*PLAN_PARTS[:1], {**PLAN_PARTS[1], "surfaces": ["api/web/**"]}], contract=CONTRACT)
    assert workflows.advance(store, store.get_task(plan_task["id"])) == []
    plan_task = store.get_task(plan_task["id"])
    assert plan_task["status"] == "replan" and len(store.run_tasks(started["id"])) == 1
    from locks import stops

    stop = stops.of(store, plan_task)
    assert stop.cause == "parts" and "api's api/** and web's api/web/**" in stop.reason
    status = workflows.status(store, started["id"])
    assert status["state"] == "waits on the orchestrator" and status["waiting"] == ["plan"]
    # The session hears of it; a retry plans again.
    assert plan_task["id"] in [t["id"] for t in store.unreported("s1")]
    assert workflows.retry(store, started["id"], "plan")["workflow_step"] == "plan"


JOINED = {"name": "joined", "inputs": ["goal"], "steps": [
    {"id": "plan", "prompt": "Plan {goal} in parts", "kind": "plan"},
    {"id": "build", "prompt": "Build your part of: {goal}", "kind": "implement", "fanout": "plan"},
    {"id": "join", "prompt": "Join the parts of: {goal}", "kind": "implement", "integrate": True},
    {"id": "review", "prompt": "Review, without changing any file, the change made for: {goal}",
     "kind": "review-routine"},
]}


def test_an_integrate_step_joins_the_parts_with_no_worker_when_they_merge_cleanly(git_repo, store: Store, kicks):
    from .conftest import git

    _save(JOINED, git_repo)
    started = workflows.start(store, "joined", git_repo, {"goal": "the orders view\n- it pages by 50"})
    seen: list[dict] = []
    flow.work(store, runner=_parts_runner([planned(), ok("api built"), ok("web built"), ok("no problems")], seen))
    _plan, api, web, join, review = store.run_tasks(started["id"])
    # Four workers: the plan, two parts, the review. The join needed none.
    assert len(seen) == 4 and join["status"] == "done" and not store.attempts(join["id"])
    opts = json.loads(join["options"])
    assert [b["part"] for b in opts["integrate"]["branches"]] == ["api", "web"]
    assert opts["integrate"]["contract"] == CONTRACT
    # Its base is the commit the parts started from, and the request is context in its brief.
    assert opts["base"] == store.last_event(api["id"], "attempt_started")["data"]["base"]
    assert "> - it pages by 50" in join["body"] and "api — locks/task-" in join["body"]
    ended = store.final_event(join["id"])["data"]
    assert ended["branch"] == f"locks/task-{join['id']}" and ended["changed"] == ["api/api.py", "web/web.py"]
    assert store.last_event(join["id"], "integrated")["data"]["merged"] == [f"locks/task-{api['id']}",
                                                                          f"locks/task-{web['id']}"]
    subjects = git(git_repo, "log", "--format=%s", "--merges", ended["branch"]).splitlines()
    assert subjects == ["Join the web part", "Join the api part"]
    # The review reads the combined diff, blind, in a worktree that holds both parts.
    assert "git diff" in seen[3]["prompt"] and {"api/api.py", "web/web.py"} <= set(seen[3]["files"])
    assert "api built" not in seen[3]["prompt"]
    assert workflows.status(store, started["id"])["state"] == "done" and review["status"] == "done"


ORDERED = ({**PLAN_PARTS[0]}, {**PLAN_PARTS[1], "after": "api"})


def test_a_part_that_builds_on_another_waits_for_it_and_starts_from_what_it_kept(git_repo, store: Store, kicks):
    from locks import after

    _save(JOINED, git_repo)
    started = workflows.start(store, "joined", git_repo, {"goal": "the orders view"})
    seen: list[dict] = []
    # The plan alone runs first; then only the api part can start: the web part is held behind it.
    flow.work(store, runner=_parts_runner([planned(ORDERED)], seen), max_tasks=1)
    _plan, api, web = store.run_tasks(started["id"])
    assert after.hold(web) == {"task": api["id"]} and after.held(web) and not after.held(api)
    assert "It builds on part api" in web["body"] and "It builds on part" not in api["body"]
    flow.work(store, runner=_parts_runner([ok("api built"), ok("web built"), ok("no problems")], seen))
    web = store.get_task(web["id"])
    # Released onto what the api part kept: its worktree holds the api part's file.
    assert web["status"] == "done" and store.last_event(web["id"], "released")["data"]["after"] == {"task": api["id"]}
    assert "api/api.py" in seen[2]["files"] and "api/api.py" not in seen[1]["files"]
    join = store.run_tasks(started["id"])[3]
    assert join["status"] == "done" and not store.attempts(join["id"])
    assert set(store.final_event(join["id"])["data"]["changed"]) == {"api/api.py", "web/web.py"}
    assert workflows.status(store, started["id"])["state"] == "done"


def test_a_retried_part_takes_the_parts_held_behind_it_along(git_repo, store: Store, kicks):
    from locks import after

    _save(JOINED, git_repo)
    started = workflows.start(store, "joined", git_repo, {"goal": "the orders view"})
    seen: list[dict] = []
    flow.work(store, runner=_parts_runner([planned(ORDERED), bad(Failure.SPEC_BUG, "no schema")], seen))
    _plan, api, web = store.run_tasks(started["id"])[:3]
    assert store.get_task(api["id"])["status"] == "replan" and after.held(store.get_task(web["id"]))
    retried = workflows.retry(store, started["id"], "build")
    # Only the api part goes again, and the web part now waits for that try.
    assert [pid for pid, _ in retried["parts_retried"]] == ["api"]
    assert after.hold(store.get_task(web["id"])) == {"task": retried["id"]}
    flow.work(store, runner=_parts_runner([ok("api built"), ok("web built"), ok("no problems")], seen))
    assert store.get_task(web["id"])["status"] == "done"
    assert workflows.status(store, started["id"])["state"] == "done"


def test_an_integrate_step_of_one_branch_passes_and_what_follows_starts_from_it(git_repo, store: Store, kicks):
    _save(JOINED, git_repo)
    started = workflows.start(store, "joined", git_repo, {"goal": "the orders view"})
    one = ({"id": "all", "prompt": "Build it as one", "surfaces": []},)
    seen: list[dict] = []
    flow.work(store, runner=_parts_runner([planned(one, ""), ok("built"), ok("no problems")], seen))
    _plan, _build, join, _review = store.run_tasks(started["id"])
    assert join["status"] == "done" and "nothing to join" in join["result"]
    assert "integrate" not in json.loads(join["options"])
    assert store.final_event(join["id"])["data"]["branch"] is None
    # The review starts from the one part's branch.
    assert "step2.txt" in seen[2]["files"]
    assert workflows.status(store, started["id"])["state"] == "done"


def test_the_bundled_feature_parallel_plans_builds_in_parts_joins_and_reviews_like_feature(git_repo, store: Store,
                                                                                           kicks):
    from locks import matrix

    defn = workflows.load("feature-parallel", git_repo, "bundled")
    steps = {s["id"]: s for s in defn["steps"]}
    assert list(steps) == ["plan", "build", "integrate", "review", "refute", "fix", "recheck"]
    # The plan reads only and starts on opus/high, where plans for parts were measured; its ladder never
    # holds Haiku.
    assert steps["plan"]["kind"] == "plan" and steps["plan"]["start"] == "opus/high"
    assert all(cell.model != "haiku" for cell in matrix.ladder_for("plan"))
    assert steps["build"]["fanout"] == "plan" and steps["integrate"]["integrate"] is True
    assert steps["review"]["needs"] == ["integrate"]
    feature = {s["id"]: s for s in workflows.load("feature", git_repo, "bundled")["steps"]}
    for sid in ("review", "refute", "fix", "recheck"):
        assert {k: v for k, v in steps[sid].items() if k != "needs"} == \
            {k: v for k, v in feature[sid].items() if k != "needs"}
    assert "several areas" in defn["description"] and "`feature`" in defn["description"]

    started = workflows.start(store, "feature-parallel", git_repo, {"goal": "the orders view"})
    seen: list[dict] = []
    flow.work(store, runner=_parts_runner([planned(), ok("api built"), ok("web built"), ok("no problems")], seen))
    # The plan, two parts and the review: the join and the ledger steps needed no worker.
    assert len(seen) == 4 and seen[0]["parts"]
    assert workflows.status(store, started["id"])["state"] == "done"


def test_a_large_change_is_reviewed_by_area_side_by_side_into_one_ledger(git_repo, store: Store, kicks,
                                                                          monkeypatch):
    monkeypatch.setattr(workflows, "AREA_REVIEW_LINES", 1)
    started = workflows.start(store, "feature-parallel", git_repo, {"goal": "the orders view"})
    found = ({"where": "api/api.py:1", "what": "the list is not paged", "evidence_class": "deterministic",
              "cause": "introduced", "proof": "python -c 'import api'"},)
    seen: list[dict] = []
    flow.work(store, runner=_parts_runner([
        planned(), ok("api built"), ok("web built"),
        WorkerResult(True, None, "one problem", "read it", findings=found), ok("web reads fine"),
        ok("paged it"),
        WorkerResult(True, None, "fixed", "ran it", verdicts=({"id": "F1", "verdict": "fixed", "proof": "ran"},))],
        seen))
    tasks = store.run_tasks(started["id"])
    reviews = [t for t in tasks if t["workflow_step"] == "review"]
    # One blind review per area (the parts' surfaces), side by side; each reads only its area's diff.
    assert [json.loads(t["options"])["part"]["id"] for t in reviews] == ["api", "web"]
    api_review = reviews[0]["body"]
    assert "Review, without changing any file, the change made for: the orders view" in api_review
    assert ":(glob)api/**" in api_review and "The other files the change touched" in api_review
    assert "web/web.py" in api_review and "api built" not in api_review
    # Their findings go into the one ledger: one refute, one fix, one recheck.
    for sid in ("refute", "fix", "recheck"):
        assert len([t for t in tasks if t["workflow_step"] == sid]) == 1, sid
    fix = next(t for t in tasks if t["workflow_step"] == "fix")
    assert json.loads(fix["options"])["ledger"]["ids"] == ["F1"] and "the list is not paged" in fix["body"]
    status = workflows.status(store, started["id"])
    assert status["state"] == "done" and [p["id"] for p in status["steps"][3]["parts"]] == ["api", "web"]


def test_what_no_part_declared_is_reviewed_as_its_own_area(git_repo, store: Store, kicks, monkeypatch):
    monkeypatch.setattr(workflows, "AREA_REVIEW_LINES", 1)
    _save({"name": "prepared", "inputs": ["goal"], "steps": [
        {"id": "prepare", "prompt": "Prepare {goal}", "kind": "implement"},
        *JOINED["steps"]]}, git_repo)
    started = workflows.start(store, "prepared", git_repo, {"goal": "the orders view"})
    seen: list[dict] = []
    flow.work(store, runner=_parts_runner([ok("prepared"), planned(), ok("api built"), ok("web built"),
                                           ok("api reads fine"), ok("web reads fine"), ok("the rest reads fine")],
                                          seen))
    reviews = [t for t in store.run_tasks(started["id"]) if t["workflow_step"] == "review"]
    # The file the step before the plan wrote is in no part's surfaces: it is the `rest` area's.
    assert [json.loads(t["options"])["part"]["id"] for t in reviews] == ["api", "rest", "web"]
    assert "-- step1.txt`" in reviews[1]["body"] and "api/api.py" in reviews[1]["body"]
    assert workflows.status(store, started["id"])["state"] == "done"


def test_a_large_change_with_no_parts_is_reviewed_whole(git_repo, store: Store, kicks, monkeypatch):
    monkeypatch.setattr(workflows, "AREA_REVIEW_LINES", 1)
    _save({"name": "wide", "inputs": ["goal"], "steps": [
        {"id": "build", "prompt": "Build {goal}", "kind": "implement"},
        {"id": "review", "prompt": "Review, without changing any file, {goal}", "kind": "review-routine"}]},
        git_repo)

    def launch(spec):
        if "Edit" not in spec.disallowed_tools:
            for rel in ("src/a.py", "docs/a.md", "README.txt"):
                (spec.target_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                (spec.target_dir / rel).write_text("x\n")
        return ok()

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    started = workflows.start(store, "wide", git_repo, {"goal": "the export"})
    flow.work(store, runner=runner)
    reviews = [t for t in store.run_tasks(started["id"]) if t["workflow_step"] == "review"]
    # Split by top-level folder it found no more, slower and dearer (bench/review/areas.py): one review.
    assert len(reviews) == 1 and "part" not in json.loads(reviews[0]["options"])
    # Below the threshold it is one review too.
    monkeypatch.setattr(workflows, "AREA_REVIEW_LINES", 600)
    again = workflows.start(store, "wide", git_repo, {"goal": "the import"})
    flow.work(store, runner=runner)
    assert len([t for t in store.run_tasks(again["id"]) if t["workflow_step"] == "review"]) == 1


def test_generated_files_get_no_review_of_their_own(git_repo, store: Store, kicks, monkeypatch):
    from .conftest import git

    monkeypatch.setattr(workflows, "AREA_REVIEW_LINES", 1)
    (git_repo / ".gitattributes").write_text("dist/** linguist-generated\n")
    git(git_repo, "add", ".gitattributes")
    git(git_repo, "commit", "-qm", "mark dist generated")
    _save({"name": "built", "inputs": ["goal"], "steps": [
        {"id": "build", "prompt": "Build {goal}", "kind": "implement"},
        {"id": "review", "prompt": "Review, without changing any file, {goal}", "kind": "review-routine"}]},
        git_repo)

    def launch(spec):
        if "Edit" not in spec.disallowed_tools:
            for rel in ("src/a.py", "dist/app.js", "out/bundle.js"):
                (spec.target_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                (spec.target_dir / rel).write_text("x\n")
        return ok()

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    started = workflows.start(store, "built", git_repo, {"goal": "the export"})
    build = store.run_tasks(started["id"])[0]
    # What the build's own check wrote is generated too.
    store.add_event(build["id"], "check_rewrote", command="make", n=1, files=["out/bundle.js"])
    flow.work(store, runner=runner)
    reviews = [t for t in store.run_tasks(started["id"]) if t["workflow_step"] == "review"]
    # dist/ and out/ hold only generated files: one review is left, which is told of them.
    assert len(reviews) == 1 and "part" not in json.loads(reviews[0]["options"])
    assert "2 of the changed files are generated" in reviews[0]["body"]
    # It is handed the files its diff touches: capabilities about them join it.
    assert "src/a.py" in json.loads(reviews[0]["options"])["touches"]
    assert "dist/app.js" in reviews[0]["body"] and "out/bundle.js" in reviews[0]["body"]


def test_the_cli_makes_a_step_fan_out_and_one_join_the_parts(git_repo, kicks, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    assert cli.main(["workflow", "new", "split"]) == 0
    assert cli.main(["workflow", "step", "set", "split", "do", "--kind", "plan", "--prompt", "Plan {goal}"]) == 0
    assert cli.main(["workflow", "step", "add", "split", "build", "--prompt", "Build {goal}", "--kind", "implement",
                     "--fanout", "do"]) == 0
    assert cli.main(["workflow", "step", "add", "split", "join", "--prompt", "Join {goal}", "--kind", "implement",
                     "--integrate"]) == 0
    steps = workflows.load("split", git_repo)["steps"]
    assert steps[1]["fanout"] == "do" and steps[2]["integrate"] is True
    capsys.readouterr()
    assert cli.main(["workflow", "show", "split"]) == 0
    out = capsys.readouterr().out
    assert "[kind implement, fanout do]" in out and "[kind implement, integrate]" in out
    assert cli.main(["workflow", "step", "set", "split", "build", "--fanout", "ghost"]) == 1
    assert "fanout names 'ghost'" in capsys.readouterr().err
    # Only an integrate step waits on a step run in parts: one that stops joining waits on the plan.
    assert cli.main(["workflow", "step", "set", "split", "join", "--clear", "integrate"]) == 1
    assert "waits on 'build', a step run in parts" in capsys.readouterr().err
    assert cli.main(["workflow", "step", "set", "split", "join", "--clear", "integrate", "--needs", "do"]) == 0
    assert "integrate" not in workflows.load("split", git_repo)["steps"][2]


def test_the_steps_a_blind_review_follows_run_no_verifier_of_their_own():
    feature = workflows.load("feature", None)
    marked = {s["id"]: workflows.reviewed_next(feature, s) for s in feature["steps"]}
    assert marked == {"build": True, "review": False, "refute": False, "fix": False, "recheck": False}
    parallel = workflows.load("feature-parallel", None)
    marked = {s["id"]: workflows.reviewed_next(parallel, s) for s in parallel["steps"]}
    assert marked["build"] and marked["integrate"] and not marked["plan"] and not marked["fix"]


def test_a_large_feature_runs_in_parts_by_default(git_repo, tmp_path, monkeypatch):
    small = "Add a --verbose flag to the CLI"
    big = ("Build the billing module: a schema migration for invoices, the payment webhook with its signature check, "
           "the admin screens to list and refund them, and tests for each: S1 the migration, S2 the webhook, S3 the "
           "refund, S4 the list, S5 the totals, S6 the emails, S7 the export, S8 the audit log, in "
           "src/billing/models.py, src/billing/webhook.py, src/admin/invoices.tsx, src/billing/refund.py")
    assert workflows.fanned("feature", git_repo, {"goal": small}) == ("feature", None)
    name, why = workflows.fanned("feature", git_repo, {"goal": big})
    assert name == "feature-parallel" and "points" in why and "--whole" in why
    assert workflows.fanned("bugfix", git_repo, {"goal": big}) == ("bugfix", None)
    assert workflows.fanned("feature", git_repo, {}) == ("feature", None)
    # A feature the person saved is theirs: never swapped.
    user = workflows.dirs(git_repo)["user"]
    user.mkdir(parents=True, exist_ok=True)
    (user / "feature.json").write_text((workflows.bundled_dir() / "feature.json").read_text())
    assert workflows.fanned("feature", git_repo, {"goal": big}) == ("feature", None)
    assert workflows.fanned("feature", git_repo, {"goal": big}, scope="nowhere") == ("feature", None)


def test_the_run_command_says_it_fans_out_and_whole_keeps_one_build(git_repo, store: Store, kicks, capsys,
                                                                     monkeypatch):
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(workflows, "fanned", lambda name, *a, **kw: ("feature-parallel", "runs as feature-parallel"))
    assert cli.main(["workflow", "run", "feature", "the export", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("locks: runs as feature-parallel") and "feature-parallel would run here" in out
    assert cli.main(["workflow", "run", "feature", "the export", "--dry-run", "--whole"]) == 0
    assert capsys.readouterr().out.startswith("feature would run here")
    assert cli.main(["workflow", "run", "feature", "the export", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["fanned"] == "runs as feature-parallel"


def test_a_later_fix_round_starts_from_what_the_round_before_it_kept(git_repo, store: Store, kicks):
    found = ({"where": "src/chart.py:3", "what": "the animation has no safety timeout",
              "evidence_class": "deterministic", "cause": "introduced", "proof": "grep -n setTimeout src/chart.py"},)
    fixed = WorkerResult(True, None, "documented it as known debt", "wrote it down",
                         verdicts=({"id": "F1", "verdict": "fixed", "proof": "the note is in CLAUDE.md"},))
    results = [ok("built"), WorkerResult(True, None, "one problem", "read it", findings=found),
               ok("wrote it down"), fixed, ok("wrote it down again"), fixed]
    rounds: list[str] = []

    def launch(spec):
        if "Ledger (fix" in spec.prompt:
            notes = spec.target_dir / "CLAUDE.md"
            rounds.append(notes.read_text() if notes.exists() else "")
            notes.write_text((notes.read_text() if notes.exists() else "") + f"round {len(rounds)}\n")
        elif "Edit" not in spec.disallowed_tools:
            (spec.target_dir / "src").mkdir(exist_ok=True)
            (spec.target_dir / "src" / "chart.py").write_text("x = 1\n")
        return results.pop(0)

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    started = workflows.start(store, "feature", git_repo, {"goal": "the chart"})
    flow.work(store, runner=runner)
    # The second round builds on the first one's fix, never on the build alone.
    assert rounds == ["", "round 1\n"]
    fixes = [t for t in store.run_tasks(started["id"]) if t["workflow_step"] == "fix"]
    first_tip = store.last_event(fixes[0]["id"], "finished")["data"]["tip"]
    assert json.loads(fixes[1]["options"])["ledger"]["base"] == first_tip


def test_a_step_whose_branch_was_deleted_brings_it_back_or_stops(git_repo, store: Store, kicks, monkeypatch):
    from locks import isolate

    from .conftest import git

    _save(TWO, git_repo)
    started = workflows.start(store, "two", git_repo, {"goal": "the export"})
    saw: list[bool] = []

    def launch(spec):
        saw.append((spec.target_dir / "built.txt").exists())
        (spec.target_dir / ("built.txt" if not saw[:-1] else "polished.txt")).write_text("x\n")
        return ok("did it")

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    # Something deletes the build's branch before `advance` queues the next step.
    real = workflows.advance

    def advance(store, task, env=None, kick=None):
        if task and task.get("workflow_step") == "build" and task["status"] == "done":
            git(git_repo, "branch", "-D", f"locks/task-{task['id']}")
        return real(store, task, env, kick)

    monkeypatch.setattr(workflows, "advance", advance)
    flow.work(store, runner=runner)
    build, polish = store.run_tasks(started["id"])
    # The polish step started on the build's work: the branch was put back at the tip it kept.
    assert saw == [False, True] and polish["status"] == "done"
    assert store.last_event(build["id"], "branch_restored")["data"]["branch"] == f"locks/task-{build['id']}"

    # A branch that cannot be put back stops the run with the reason, never a start on the base.
    started = workflows.start(store, "two", git_repo, {"goal": "the import"})
    monkeypatch.setattr(isolate, "restore", lambda *a: False)
    saw.clear()
    flow.work(store, runner=runner)
    build = store.run_tasks(started["id"])[0]
    assert len(store.run_tasks(started["id"])) == 1
    assert "is gone" in store.last_event(build["id"], "workflow_error")["data"]["error"]


CHECKED = {"name": "checked", "inputs": ["goal", "check"], "steps": [
    {"id": "build", "prompt": "Build {goal}", "kind": "implement", "verify": "{check}"},
    {"id": "review", "prompt": "Review, without changing any file, {goal}", "kind": "review-routine",
     "needs": ["build"]}]}


def test_an_input_only_a_check_uses_comes_from_verify_and_the_text_fills_the_one_required(
        git_repo, store: Store, kicks, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    _save(CHECKED, git_repo)
    assert workflows.input_specs(workflows.load("checked", git_repo)) == [
        {"name": "goal", "required": True}, {"name": "check", "required": False, "from_verify": True}]
    assert cli.main(["workflow", "list"]) == 0
    assert "inputs: goal (required), check (required, or --verify)" in capsys.readouterr().out
    assert cli.main(["workflow", "show", "checked"]) == 0
    assert "inputs: goal (required), check (required, or --verify)" in capsys.readouterr().out
    # `run <name> --help` lists what that workflow takes.
    with pytest.raises(SystemExit) as stop:
        cli.main(["workflow", "run", "checked", "--help"])
    assert stop.value.code == 0
    helped = capsys.readouterr().out
    assert "--check CMD" in helped and "checked (user) takes: goal (required)" in helped
    # The text is the one required input; without a check it says how to give one.
    assert cli.main(["workflow", "run", "checked", "the export", "--dry-run"]) == 1
    assert "`--verify` gives it" in capsys.readouterr().err
    # Either spelling of the run's check fills it.
    for flag in ("--verify", "--check"):
        assert cli.main(["workflow", "run", "checked", "the export", flag, "npm test", "--dry-run"]) == 0
        dry = capsys.readouterr().out
        assert "goal: the export" in dry and "check: npm test" in dry and "checked by `npm test`" in dry
    assert cli.main(["workflow", "run", "checked", "the export", "--check", "npm test"]) == 0
    (build,) = store.run_tasks(store.list_runs()[0]["id"])
    assert json.loads(build["options"])["verify"] == "npm test"


def test_the_status_of_one_run_is_a_line_a_step_with_why_a_step_stopped(
        git_repo, store: Store, kicks, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    _save(CHAIN, git_repo)
    brief = "Build the export of every order. " + "It carries all of its details and every line item. " * 10
    started = workflows.start(store, "chain", git_repo, {"goal": brief})
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built it"), bad(Failure.SPEC_BUG, "the orders table has no export"),
                                     bad(Failure.SPEC_BUG, "the orders table has no export")], seen))
    assert cli.main(["workflow", "status", str(started["id"])]) == 0
    out = capsys.readouterr().out
    goal = next(line for line in out.splitlines() if line.startswith("  goal: "))
    assert len(goal) <= len("  goal: ") + 100 and goal.endswith("…") and brief.strip() not in out
    build = next(line for line in out.splitlines() if " build " in line)
    assert "done #" in build and "attempt 1" in build and "$0.10" in build and "branch locks/task-" in build
    review = next(line for line in out.splitlines() if " check " in line)
    assert "stopped: " in review
    # The goal whole only when asked for.
    assert cli.main(["workflow", "status", str(started["id"]), "--verbose"]) == 0
    assert brief.strip() in capsys.readouterr().out


def test_a_goal_that_is_several_features_is_told_before_it_runs():
    many = "Build the course platform:\n" + "\n".join(
        f"- feature {n}: its database tables, its API endpoints with permissions and its admin screens"
        for n in range(7))
    said = workflows.many_features(many)
    assert said and "lists 7 items" in said and "--after run:<N>" in said
    assert workflows.many_features("Build the orders view:\n- it lists orders\n- it pages by 50") is None
    assert workflows.many_features("- a\n- b\n- c\n- d\n- e\n- f\n- g") is None  # many items, small: no advice


def test_what_each_blind_review_added_beside_the_other_is_measured(git_repo, store: Store, kicks):
    shown = {"where": "app.py:3", "what": "the total skips the last line", "evidence_class": "deterministic",
             "cause": "introduced", "proof": "python3 -c 'print(total([1, 2]))' prints 1"}
    read = {"where": "app.py:9", "what": "a negative quantity is accepted", "evidence_class": "inferential",
            "cause": "introduced"}
    results = [WorkerResult(True, None, "two problems", "read it", findings=(shown, read)),
               WorkerResult(True, None, "one problem", "read it", findings=(shown,)),
               WorkerResult(True, None, "decided", "showed it",
                            verdicts=({"id": "F2", "verdict": "confirmed", "proof": "total([-1]) is -1"},)),
               ok("fixed both"),
               WorkerResult(True, None, "checked", "both fixed",
                            verdicts=({"id": "F1", "verdict": "fixed", "proof": "p"},
                                      {"id": "F2", "verdict": "fixed", "proof": "p"}))]
    seen: list[dict] = []
    workflows.start(store, "dual-review", git_repo, {"target": "the branch"})
    flow.work(store, runner=_runner(results, seen))
    rows = {r["step"]: r for r in workflows.reviewer_yield(store)}
    assert {k: (r["runs"], r["rows"], r["alone"], r["alone_kept"]) for k, r in rows.items()} == {
        "review-a": (1, 2, 1, 1), "review-b": (1, 1, 0, 0)}
    from locks import analysis
    said = analysis.text(analysis.compute(store, days=0))
    assert "blind reviews beside another" in said and "review-b" in said


def test_a_re_check_the_fix_s_own_check_already_answered_runs_no_worker(git_repo, store: Store, kicks, monkeypatch):
    from locks import orchestrate

    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    monkeypatch.setattr(orchestrate.risk_mod, "needs_verifier", lambda risk, cheap=False: True)
    found = ({"where": "src/chart.py:3", "what": "the total skips the last line", "evidence_class": "deterministic",
              "cause": "introduced", "proof": "python3 -c 'print(total([1, 2]))' prints 1"},)
    asked: list[str] = []

    def launch(spec):
        asked.append(spec.prompt)
        if "Check this change" in spec.prompt:  # the fix's independent check
            assert "Answer every one in `verdicts`" in spec.prompt and spec.verdicts == ("F1",)
            return WorkerResult(True, None, "checked", "shown", verdicts=(
                {"id": "F1", "verdict": "fixed", "proof": "total([1, 2]) is 3"},))
        if "Edit" not in spec.disallowed_tools:
            (spec.target_dir / "src").mkdir(exist_ok=True)
            (spec.target_dir / "src" / "chart.py").write_text(f"x = {len(asked)}\n")
            return ok("built" if len(asked) == 1 else "fixed it")
        return WorkerResult(True, None, "one problem", "read it", findings=found)

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    started = workflows.start(store, "feature", git_repo, {"goal": "the chart"})
    flow.work(store, runner=runner)
    assert workflows.status(store, started["id"])["state"] == "done"
    assert not [p for p in asked if "Ledger (recheck" in p]  # build, review, fix, its check: four workers
    assert len(asked) == 4
    recheck = next(t for t in store.run_tasks(started["id"]) if t["workflow_step"] == "recheck")
    assert "no second reading" in recheck["result"]
    assert store.last_event(recheck["id"], "ledger_decided")["data"]["by"] == "verifier"
    # A fix whose check left a row open is re-checked by a worker, as before.
    assert workflows.reviewer_yield(store) == []


def test_a_row_the_fix_s_check_finds_open_earns_the_fix_a_correction(git_repo, store: Store, kicks, monkeypatch):
    from locks import orchestrate

    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    monkeypatch.setattr(orchestrate.risk_mod, "needs_verifier", lambda risk, cheap=False: True)
    found = ({"where": "src/chart.py:3", "what": "the total skips the last line", "evidence_class": "deterministic",
              "cause": "introduced", "proof": "python3 -c 'print(total([1, 2]))' prints 1"},)
    answers = iter(["not_fixed", "fixed"])
    asked: list[str] = []

    def launch(spec):
        asked.append(spec.prompt)
        if "Check this change" in spec.prompt:
            said = next(answers)
            return WorkerResult(True, None, "checked", "shown", verdicts=(
                {"id": "F1", "verdict": said, "proof": "total([1, 2]) is 1" if said != "fixed" else "it is 3"},))
        if "Edit" not in spec.disallowed_tools:
            (spec.target_dir / "src").mkdir(exist_ok=True)
            (spec.target_dir / "src" / "chart.py").write_text(f"x = {len(asked)}\n")
            return ok("did it")
        return WorkerResult(True, None, "one problem", "read it", findings=found)

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    started = workflows.start(store, "feature", git_repo, {"goal": "the chart"})
    flow.work(store, runner=runner)
    assert workflows.status(store, started["id"])["state"] == "done"
    fix = next(t for t in store.run_tasks(started["id"]) if t["workflow_step"] == "fix")
    checks = [e["data"] for e in store.events_of(fix["id"], "verified")]
    assert [c["passed"] for c in checks] == [False, True] and "F1 is not fixed" in checks[0]["blockers"][0]["what"]
    assert not [p for p in asked if "Ledger (recheck" in p]


def test_the_bundled_drift_is_one_read_only_explore_step_and_its_dry_run_queues_nothing(git_repo, store: Store, kicks,
                                                                                         capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    defn = workflows.load("drift", git_repo, "bundled")
    assert defn["inputs"] == ["dir"] and [(s["id"], s["kind"]) for s in defn["steps"]] == [("drift", "explore")]
    assert "{dir}" in defn["steps"][0]["prompt"] and "locks-report" in defn["steps"][0]["prompt"]
    assert cli.main(["workflow", "run", "drift", "--input", "dir=docs/designs", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "nothing is queued" in out and "docs/designs" in out
    assert store.list_runs() == []
