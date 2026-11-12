from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from locks import cli, dispatch, flow, hooks, launch
from locks.store import Store

from .test_classify import _reply, _runner

RUNNING = {"id": 1, "status": "running", "body": "rewrite the importer"}
NEW = {"id": 2, "status": "queued", "body": "add a --dry-run flag to the importer"}


def test_nothing_ahead_needs_no_model():
    run = _runner(raises=AssertionError("no call"))
    assert dispatch.decide(NEW, [], runner=run) == dispatch.Decision(True, "nothing runs or waits ahead of it")
    assert run.calls == []


def test_haiku_reads_the_new_task_and_everything_ahead_of_it():
    run = _runner(_reply(parallel=False, reason="it changes the importer #1 rewrites"))
    got = dispatch.decide(NEW, [RUNNING], runner=run)
    assert got == dispatch.Decision(False, "it changes the importer #1 rewrites", 0.001)
    argv, kwargs = run.calls[0]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == dispatch.SCHEMA
    assert "#2" in kwargs["input"] and "- #1 (running): rewrite the importer" in kwargs["input"]
    long = {**NEW, "body": "x " * 2000}
    assert len(dispatch.question(long, [])) < dispatch.BODY_CHARS + 100


def test_any_failure_means_waiting_never_a_guess():
    for run in (_runner(raises=OSError("no claude")), _runner("not json"), _runner(_reply(parallel="yes", reason=""))):
        got = dispatch.decide(NEW, [RUNNING], runner=run)
        assert got.parallel is False and got.reason.startswith("waits its turn")
    blank = dispatch.decide(NEW, [RUNNING], runner=_runner(_reply(parallel=True, reason="  ")))
    assert blank == dispatch.Decision(True, "independent", 0.001)
    # A wait the model gives names its collision: "when unsure" held independent work back.
    assert "only for a collision you can name" in dispatch.SYSTEM_PROMPT
    assert "When unsure" not in dispatch.SYSTEM_PROMPT


def _build_step(version: int) -> str:
    """A `feature` workflow's build step: a goal long past the opening, its own
    migration number and the line saying the others run beside it at its end."""
    spec = "It lists, filters and exports what an operator keeps, sorted by any column, a page at a time. " * 12
    goal = (f"Add module {version} to the back office. {spec}\nMigration version: {version}\n"
            "Other tasks running in parallel take the other numbers.")
    return (f"Build this feature: {goal}\n\nRead the code it touches and decide the approach yourself, then build "
            "it with the tests that prove it works.")


def test_haiku_reads_the_migration_each_task_declares_not_only_its_opening():
    """Four builds queued with their own migration numbers ran one after another:
    Haiku read the first 600 characters of each and took #47 to need #46's migration."""
    tasks = [{"id": 46 + i, "status": "running" if i == 0 else "queued", "body": _build_step(2 + i)}
             for i in range(4)]
    assert all(t["body"].index("Migration version") > 600 for t in tasks)
    run = _runner(_reply(parallel=True, reason="each takes its own migration number"))
    got = dispatch.decide(tasks[3], tasks[:3], runner=run)
    assert got == dispatch.Decision(True, "each takes its own migration number", 0.001)
    asked = run.calls[0][1]["input"]
    new, ahead = asked.split("\n\nAlready running or queued ahead of it:\n")
    assert new.startswith("New task #49:\nBuild this feature: Add module 5") and "\n… Migration version: 5\n" in new
    blocks = ahead.split("\n- ")
    assert [b.split(":")[0].lstrip("- ") for b in blocks] == ["#46 (running)", "#47 (queued)", "#48 (queued)"]
    for block, version in zip(blocks, (2, 3, 4), strict=True):
        assert f"\n  … Migration version: {version}\n" in block
    assert asked.count("… Other tasks running in parallel take the other numbers.") == 4
    # Each task stays bounded: its opening and the lines that fit.
    assert all(len(part) < dispatch.BODY_CHARS + 50 for part in (new, *blocks))


def test_a_long_text_keeps_its_coordination_lines_first_and_within_its_bound():
    files = "".join(f"\n- Change src/area_{i}/handler.py to read the new column." for i in range(40))
    body = ("Rework the ledger. " * 30 + files + "\nIt runs in parallel with #12 and takes migration 7."
            "\nUsa la migración 8 del esquema." + "\nA closing note.")
    opening, facts = dispatch._excerpt({"body": body})
    assert opening.startswith("Rework the ledger.") and len(opening) == dispatch.OPENING_CHARS
    # Coordination lines are kept before the many files, and every line keeps the text's order.
    assert facts[-2:] == ["It runs in parallel with #12 and takes migration 7.", "Usa la migración 8 del esquema."]
    assert facts[0] == "- Change src/area_0/handler.py to read the new column." and len(facts) < 40
    assert len(opening) + sum(len(f) + 3 for f in facts) <= dispatch.BODY_CHARS
    # A long line is cut around what made it a fact.
    long = "Rework the ledger. " * 70 + "word " * 100 + "then migration 9 lands " + "word " * 100
    (fact,) = dispatch._excerpt({"body": long})[1]
    assert "migration 9" in fact and len(fact) <= dispatch.FACT_CHARS and fact.startswith("…") and fact.endswith("…")
    # A text that fits is read whole, as before.
    assert dispatch._excerpt(NEW) == ("add a --dry-run flag to the importer", [])


def test_a_declared_migration_outranks_passing_mentions_of_a_version():
    """Several early lines about some API or Node version used up the room before the
    line declaring the task's own migration, which a `;` had also cut from its clause."""
    versions = "".join(f"\nBump the API version field on endpoint {i} to match the new schema." for i in range(12))
    body = ("Rework. " * 80 + versions
            + "\nOther tasks running in parallel take the other migration numbers; this is migration 4.")
    opening, facts = dispatch._excerpt({"body": body})
    assert facts[-1] == "Other tasks running in parallel take the other migration numbers; this is migration 4."
    assert len(opening) + sum(len(f) + 3 for f in facts) <= dispatch.BODY_CHARS
    # What is left of the room goes to the version lines, in the text's order.
    kept = [f"Bump the API version field on endpoint {i} to match the new schema." for i in range(len(facts) - 1)]
    assert 0 < len(kept) < 12 and facts[:-1] == kept


def test_the_question_names_declared_surfaces_and_what_running_work_changed():
    running = {**RUNNING, "options": json.dumps({"surfaces": ["src/importer/**"]}),
               "changed": ["src/importer/read.py"]}
    fresh = {"id": 3, "status": "running", "body": "add an export", "changed": []}
    queued = {"id": 4, "status": "queued", "body": "write the docs"}
    asked = dispatch.question({**NEW, "options": json.dumps({"surfaces": ["src/cli.py"]})}, [running, fresh, queued])
    assert "add a --dry-run flag to the importer\nSurfaces it declared: src/cli.py\n" in asked
    assert ("- #1 (running): rewrite the importer\n  Surfaces it declared: src/importer/**\n"
            "  Files it changed so far (git): src/importer/read.py\n") in asked
    assert "- #3 (running): add an export\n  Files it changed so far (git): none yet\n" in asked
    assert asked.endswith("- #4 (queued): write the docs")  # git could not say: nothing is claimed for it
    many = {**RUNNING, "changed": [f"f{i}.py" for i in range(dispatch.CHANGED_FILES + 5)]}
    assert f"f{dispatch.CHANGED_FILES - 1}.py and 5 more" in dispatch.question(NEW, [many])


def test_the_dispatcher_asks_about_what_running_work_changed_and_keeps_the_answer(store: Store, git_repo):
    from locks import isolate
    from locks.store import home

    from .conftest import git
    from .test_flow import _never

    key = str(git_repo.resolve())
    first = store.enqueue(_build_step(2), repo=key, cwd=str(git_repo))
    store.claim(first["id"])
    ws = isolate.prepare(git_repo, first["id"], home())
    store.add_event(first["id"], "attempt_started", seq=1, cell="opus/high", base=ws.base, links=list(ws.links))
    (ws.path / "schema.sql").write_text("create table crm (id int);\n")
    git(ws.path, "add", "schema.sql")
    git(ws.path, "commit", "-qm", "crm")
    (ws.path / "crm.py").write_text("x = 1\n")
    second = store.enqueue(_build_step(3), repo=key, cwd=str(git_repo))
    run = _runner(_reply(parallel=False, reason=f"#{first['id']} changes schema.sql"))
    assert flow.runnable(store, key, parallel=True, decide=lambda t, ahead: dispatch.decide(t, ahead, runner=run)) == []
    asked = run.calls[0][1]["input"]
    assert f"- #{first['id']} (running): Build this feature: Add module 2" in asked
    assert asked.endswith("\n  Files it changed so far (git): crm.py, schema.sql")
    assert "\n… Migration version: 3\n" in asked and "\n  … Migration version: 2\n" in asked
    # The decision and its reason are kept on the task, once, as before.
    kept = store.get_task(second["id"])
    assert (kept["parallel"], kept["parallel_reason"], kept["cost_usd"]) == (0, f"#{first['id']} changes schema.sql",
                                                                           0.001)
    assert flow.runnable(store, key, parallel=True, decide=_never) == []
    # Where git cannot say, nothing is claimed: no list, rather than an empty one.
    assert isolate.changed_so_far(home(), git_repo, first["id"], {"base": "no-such-commit"}) is None
    assert flow.with_changes(store, {"id": first["id"], "cwd": None}) == {"id": first["id"], "cwd": None}


def test_surfaces_that_may_touch_never_start_side_by_side_whatever_the_others_declare(store: Store):
    def queued(text, surf=None):
        return store.enqueue(text, repo="k", cwd="/x", options={"surfaces": surf} if surf else {})

    api, free = queued("a", ["src/api/**"]), queued("b")
    for t in (api, free):
        store.claim(t["id"])
    mine = queued("c", ["src/api/routes.py"])
    # One task ahead declares none: the overlap with the other is still a wait, never the model's call.
    yes = lambda task, ahead: dispatch.Decision(True, "independent")  # noqa: E731
    assert flow.runnable(store, "k", parallel=True, decide=yes) == []
    kept = store.get_task(mine["id"])
    assert (kept["parallel"], kept["parallel_reason"]) == (0, f"its surfaces may touch those of #{api['id']}")
    # A file a running task already changed inside the surfaces the new task declared.
    web = queued("d", ["src/web/**"])
    touched = {**free, "changed": ["README.md", "src/web/app.ts"]}
    assert flow.by_surfaces(web, [touched]) == dispatch.Decision(
        False, f"#{free['id']} already changed src/web/app.ts, inside its surfaces")
    # Outside them it is the model's call again; every task declaring, none touching, side by side.
    assert flow.by_surfaces(web, [{**free, "changed": ["README.md"]}]) is None
    assert flow.by_surfaces(web, [api]).parallel


def test_the_reason_is_plain_text_on_one_line():
    """Markup the model leaked past its answer ("…as #55.</reason> </invoke>") reached the board."""
    leaked = "it changes the schema\n#55 migrates.</reason>\n</invoke>"
    got = dispatch.decide(NEW, [RUNNING], runner=_runner(_reply(parallel=False, reason=leaked)))
    assert got.reason == "it changes the schema #55 migrates." and "\n" not in got.reason
    for raw, plain in (("touches #1.</reason> </invoke>", "touches #1."),
                       ("<reason>touches #1</reason>", "touches #1"),
                       ("touches #1.</rea", "touches #1."),
                       ("a < b and b > a", "a < b and b > a"),
                       ("</invoke>", "may conflict")):
        got = dispatch.decide(NEW, [RUNNING], runner=_runner(_reply(parallel=False, reason=raw)))
        assert got.reason == plain, raw
    long = dispatch.decide(NEW, [RUNNING], runner=_runner(_reply(parallel=True, reason="word " * 100)))
    assert len(long.reason) <= 200 and not long.reason.endswith(" ")


def test_a_leaked_answer_that_is_not_a_boolean_still_fails_closed():
    for parallel in ("true", "true</parallel>", 1, None):
        got = dispatch.decide(NEW, [RUNNING], runner=_runner(_reply(parallel=parallel, reason="fine")))
        assert got.parallel is False and got.reason.startswith("waits its turn"), parallel


def test_one_dispatcher_per_repository(tmp_path):
    with dispatch.hold(tmp_path, "r") as mine:
        assert mine and dispatch.held(tmp_path, "r") and not dispatch.held(tmp_path, "other")
        with dispatch.hold(tmp_path, "r") as second:
            assert not second
        assert dispatch.start(tmp_path, tmp_path, "r", popen=_no_popen) is False
    assert not dispatch.held(tmp_path, "r")


def _no_popen(*a, **kw):
    raise AssertionError("must not start")


def test_start_launches_a_detached_dispatcher_without_the_session_s_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKS_SESSION_ID", "the-session")
    started = []
    assert dispatch.start(tmp_path, tmp_path, "r", popen=lambda argv, **kw: started.append((argv, kw)))
    argv, kw = started[0]
    assert argv[1:] == ["-m", "locks", "work", "--repo", str(tmp_path), "--detached"] and kw["start_new_session"]
    assert "LOCKS_SESSION_ID" not in kw["env"] and str(Path(dispatch.__file__).parents[1]) in kw["env"]["PYTHONPATH"]

    def broken(*a, **kw):
        raise OSError("no fork")

    assert dispatch.start(tmp_path, tmp_path, "s", popen=broken) is False
    assert "LOCKS_SESSION_ID" not in launch.worker_env()


def test_double_plus_starts_the_dispatcher_when_autowork_is_on(store: Store, git_repo, monkeypatch):
    monkeypatch.setenv("LOCKS_AUTOWORK", "off")  # what follows reads its absence, then turns it on
    monkeypatch.setenv("LOCKS_PARALLEL", "off")  # and the same of running beside the work ahead
    calls = []
    monkeypatch.setattr(dispatch, "start", lambda repo_dir, root, scope: calls.append(repo_dir) or True)
    event = {"session_id": "s", "prompt_id": "1", "cwd": str(git_repo), "prompt": "++ write the changelog"}
    assert "`locks work` runs the queue" in hooks.user_prompt_submit(event, store)["reason"] and not calls
    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    reason = hooks.user_prompt_submit({**event, "prompt_id": "2"}, store)["reason"]
    assert calls == [git_repo] and "when the tasks ahead of it are done" in reason
    monkeypatch.setenv("LOCKS_PARALLEL", "on")
    assert "beside the running ones" in hooks.user_prompt_submit({**event, "prompt_id": "3"}, store)["reason"]
    monkeypatch.setattr(dispatch, "start", lambda *a: False)
    assert "Starting a worker failed" in hooks.user_prompt_submit({**event, "prompt_id": "4"}, store)["reason"]
    assert all(t["session_id"] == "s" for t in store.list_tasks(status=["queued"]))


def test_a_task_queued_by_hand_starts_the_dispatcher_too(git_repo, monkeypatch, capsys):
    """A session that queued with `locks queue add` waited for a `locks work`
    nobody ran: the task sat queued."""
    calls = []
    monkeypatch.setattr(dispatch, "start", lambda repo_dir, root, scope: calls.append(repo_dir) or True)
    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    assert cli.main(["queue", "add", "write the docs", "--repo", str(git_repo)]) == 0
    assert calls == [git_repo.resolve()] and "A worker takes it" in capsys.readouterr().out


def test_a_death_names_the_crash_its_run_wrote_and_says_when_it_is_the_same_again(store: Store, tmp_path):
    log = tmp_path / "task.log"
    log.write_text("Traceback (most recent call last):\n  old\nKeyError: 'old'\n")
    start = log.stat().st_size
    with open(log, "a") as fh:
        fh.write("Traceback (most recent call last):\n  File x\nlocks.isolate.IsolationError: git commit failed\n")
    assert flow.crash_line(log, start) == "locks.isolate.IsolationError: git commit failed"
    assert flow.crash_line(log, log.stat().st_size) == "" and flow.crash_line(tmp_path / "none") == ""
    task = store.enqueue("x", repo="r", cwd="/x")
    store.claim(task["id"])
    for nth in (1, 2):
        store.update_task(task["id"], status="running")
        handle = object.__new__(flow._Process)
        handle.task_id, handle.store, handle.log, handle.log_from = task["id"], store, log, start
        handle.proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
        handle.proc.wait()
        assert handle.poll() == "interrupted"
        stop = store.last_event(task["id"], "finished")["data"]["stop"]
        assert stop["crash"] == "locks.isolate.IsolationError: git commit failed" and stop["log"] == str(log)
        assert stop["reason"].endswith("git commit failed") == (nth == 1)
        assert ("the same crash as its last death" in stop["reason"]) == (nth == 2)


def test_a_task_process_that_dies_unannounced_is_interrupted(store: Store):
    task = store.enqueue("x", repo="r", cwd="/x")
    store.claim(task["id"])
    handle = object.__new__(flow._Process)
    handle.task_id, handle.store = task["id"], store
    handle.proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    handle.proc.wait()
    assert handle.poll() == "interrupted"
    assert store.get_task(task["id"])["status"] == "interrupted" and store.lanes()[0]["paused"]
    stop = store.last_event(task["id"], "finished")["data"]["stop"]
    assert stop["cause"] == "died" and stop["by"] == "locks" and "without saying how" in stop["reason"]
    store.update_task(task["id"], status="done")
    assert handle.poll() == "done"
    store.update_task(task["id"], status="running")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    handle.proc = sleeper
    assert handle.poll() is None
    sleeper.kill()
    sleeper.wait()


def test_a_task_process_runs_through_locks_run_queued(store: Store, git_repo, monkeypatch, capsys):
    task = store.enqueue("write the docs", repo=str(git_repo.resolve()), cwd=str(git_repo),
                         options={"kind": "docs"})
    assert cli.main(["run-queued", str(task["id"])]) == 1  # not claimed by a dispatcher
    assert "not a task the dispatcher claimed" in capsys.readouterr().err
    store.claim(task["id"])
    seen = {}

    def fake_run(text, repo_dir, st, options, **kw):
        seen.update(text=text, kind=options.kind, task_id=kw["task_id"], model=options.use_model_classifier)
        return type("R", (), {"status": "done", "text": lambda self: "task: done"})()

    monkeypatch.setattr(cli.orchestrate, "run", fake_run)
    assert cli.main(["run-queued", str(task["id"]), "--no-model"]) == 0
    assert seen == {"text": "write the docs", "kind": "docs", "task_id": task["id"], "model": False}


def test_the_process_handle_starts_run_queued(store: Store, tmp_path, monkeypatch):
    task = store.enqueue("x", repo="r", cwd=str(tmp_path))
    store.claim(task["id"])
    started = {}

    class Proc:
        def __init__(self, argv, **kw):
            started.update(argv=argv, **kw)

    monkeypatch.setattr(flow.subprocess, "Popen", Proc)
    flow._Process(store.get_task(task["id"]), tmp_path, store, flow.orchestrate.Options(use_model_classifier=False))
    assert started["argv"][1:] == ["-m", "locks", "run-queued", str(task["id"]), "--no-model"]
    assert started["cwd"] == str(tmp_path)


def test_a_second_dispatcher_for_the_same_repository_steps_aside(git_repo, capsys):
    from locks.store import home

    key = str(git_repo.resolve())
    with dispatch.hold(home(), key):
        assert cli.main(["work", "--repo", str(git_repo)]) == 0
    assert "already runs" in capsys.readouterr().out
    assert cli.main(["work", "--repo", str(git_repo)]) == 0
    assert "nothing runnable" in capsys.readouterr().out


def test_a_detached_dispatcher_at_its_limit_hands_queued_work_to_a_fresh_one(git_repo, monkeypatch, capsys):
    from locks.flow import WorkReport

    monkeypatch.setattr(cli.flow, "work", lambda store, **kw: WorkReport([], "started 10 tasks", at_limit=True))
    handed = []
    monkeypatch.setattr(dispatch, "start_lane", lambda store, root, key: handed.append(key) or True)
    assert cli.main(["work", "--repo", str(git_repo)]) == 0
    assert handed == []  # a person's own `locks work` keeps its bound
    assert cli.main(["work", "--repo", str(git_repo), "--detached"]) == 0
    assert handed == [str(git_repo.resolve())] and "a fresh dispatcher takes the rest" in capsys.readouterr().out


def test_work_queued_while_the_dispatcher_was_leaving_gets_a_fresh_one(git_repo, monkeypatch, capsys):
    """A step queued by a run that just ended kicks the lane while its last dispatcher, which read
    the queue before that step was there, still holds the lock: the kick is never lost."""
    import time

    from locks.flow import WorkReport
    from locks.store import home

    key = str(git_repo.resolve())
    started = []
    with dispatch.hold(home(), key):
        assert dispatch.start(git_repo, home(), key, popen=lambda *a, **kw: started.append(a)) is False
    assert started == [] and dispatch.woken(home(), key, time.time() - 5)

    def leaving(store, **kw):
        report = WorkReport([], "nothing runnable is queued", looked_at=time.time())
        dispatch.wake(home(), key)  # the step queued after its last look, before its lock was let go
        return report

    handed = []
    monkeypatch.setattr(cli.flow, "work", leaving)
    monkeypatch.setattr(dispatch, "start_lane", lambda store, root, k: handed.append(k) or True)
    assert cli.main(["work", "--repo", str(git_repo)]) == 0
    assert handed == [key] and "takes what was queued as this one left" in capsys.readouterr().out
    # Word left before its last look was seen by that look: no second dispatcher.
    handed.clear()
    monkeypatch.setattr(cli.flow, "work", lambda store, **kw: WorkReport([], "nothing", looked_at=time.time() + 1))
    assert cli.main(["work", "--repo", str(git_repo)]) == 0
    assert handed == []


def test_a_run_or_queue_typed_in_a_session_is_that_session_s(git_repo, monkeypatch, capsys):
    monkeypatch.setenv("LOCKS_AUTOWORK", "off")
    monkeypatch.setenv("LOCKS_SESSION_ID", "sess-1")
    assert cli.main(["queue", "add", "write the docs", "--repo", str(git_repo), "--json"]) == 0
    queued = json.loads(capsys.readouterr().out)
    assert queued["then"] == "`locks work` runs the queue."  # autowork is off here
    store = Store.open()
    assert store.get_task(queued["id"])["session_id"] == "sess-1"
    store.close()
    seen = {}
    monkeypatch.setattr(cli.orchestrate, "run", lambda *a, **kw: seen.update(kw) or type(
        "R", (), {"status": "done", "text": lambda self: ""})())
    assert cli.main(["run", "x", "--repo", str(git_repo)]) == 0
    assert seen["session_id"] == "sess-1"


def test_a_lane_whose_dispatcher_died_gets_one_from_the_next_hook(git_repo, store: Store):
    from locks import after
    from locks.store import home

    key = str(git_repo.resolve())
    store.enqueue("write the docs", repo=key, cwd=str(git_repo))
    store.enqueue("held work", repo="held", cwd=str(git_repo), options={after.KEY: {"task": 999}})
    store.enqueue("paused work", repo="paused", cwd=str(git_repo))
    store.pause_lane("paused", "task #1 stopped")
    started = []
    popen = lambda argv, **kw: started.append(kw["cwd"])  # noqa: E731
    assert dispatch.revive(store, home(), {"LOCKS_AUTOWORK": "off"}, popen=popen) == []
    on = {"LOCKS_AUTOWORK": "on"}
    assert dispatch.revive(store, home(), on, popen=popen, now=1000.0) == [key]
    assert started == [str(git_repo)]  # held work and a paused lane are left alone
    # Tried again at once (its dispatcher is still starting): left alone for a while.
    assert dispatch.revive(store, home(), on, popen=popen, now=1010.0) == []
    assert dispatch.revive(store, home(), on, popen=popen, now=1000.0 + dispatch.REVIVE_EVERY_S) == [key]
    # One that holds the lane, or every lane, is enough.
    with dispatch.hold(home(), key):
        assert dispatch.revive(store, home(), on, popen=popen, now=5000.0) == []
    with dispatch.hold(home(), "*"):
        assert dispatch.revive(store, home(), on, popen=popen, now=9000.0) == []


def test_the_hooks_revive_lanes_and_a_failure_costs_the_turn_nothing(store: Store, monkeypatch, tmp_path):
    from locks.store import home

    seen = []
    monkeypatch.setattr(dispatch, "revive", lambda store, root, env: seen.append(env.get("X")))
    monkeypatch.setenv("X", "1")
    hooks.stop({"session_id": "s1"}, store)
    hooks.session_start({"session_id": "s2", "cwd": str(tmp_path)}, store, home(), {})
    assert seen == ["1", "1"]
    monkeypatch.setattr(dispatch, "revive", lambda store, root, env: 1 / 0)
    hooks._revive(store, home(), {})
    assert "revive" in (home() / hooks.ERROR_LOG).read_text()


def test_without_an_env_file_nothing_is_exported(tmp_path):
    assert hooks._export({}, "X", "1") is False
    assert hooks._export({"CLAUDE_ENV_FILE": str(tmp_path / "no" / "such")}, "X", "1") is False


# --- a worker that died is noticed at a turn's end, and resumed once -----------------------------------


def test_a_dead_worker_is_swept_and_resumed_once_from_its_kept_work(store: Store, tmp_path, monkeypatch):
    from locks import stops

    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    started: list[list[str]] = []
    task = store.create_task("build the export", status="running", source="locks", cwd=str(tmp_path), pid=999999)
    store.add_event(task["id"], "attempt_started", seq=1, cell="opus/high")
    alive = store.create_task("still going", status="running", source="locks", cwd=str(tmp_path), pid=os.getpid())
    gone = store.create_task("its folder is gone", status="interrupted", source="locks", cwd=str(tmp_path / "x"))
    stops.record(store, gone["id"], stops.Stop("died", "no sign of life"))
    # Never from inside a worker (its mod asks for endings): that resume would be refused, its one try spent.
    assert dispatch.recover(store, tmp_path, {**os.environ, "LOCKS_WORKER_TASK": "7"}, popen=pytest.fail) == []
    assert "LOCKS_WORKER_TASK" not in dispatch.child_env({"LOCKS_WORKER_TASK": "7", "LOCKS_WORKER_ATTEMPT": "1"})
    found = dispatch.recover(store, tmp_path, os.environ, popen=lambda argv, **kw: started.append(argv),
                             is_alive=lambda pid: pid == os.getpid())
    assert found == [task["id"]]
    assert started[0][-3:] == ["resume", str(task["id"]), "--unattended"]
    assert store.get_task(task["id"])["status"] == "interrupted"  # swept at once, not at the next session
    assert stops.of(store, store.get_task(task["id"])).cause == "died"
    assert store.get_task(alive["id"])["status"] == "running"
    # Its death is handed to no session: the resume's ending is the one that counts.
    assert store.unreported(task["session_id"] or "") == []
    # Once: a second death is the orchestrator's.
    assert dispatch.recover(store, tmp_path, os.environ, popen=lambda argv, **kw: started.append(argv)) == []
    assert len(started) == 1


def test_a_death_that_repeats_its_crash_is_never_resumed_on_its_own(store: Store, tmp_path, monkeypatch):
    from locks import stops

    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    task = store.create_task("x", status="interrupted", source="locks", cwd=str(tmp_path))
    stops.record(store, task["id"], stops.Stop("died", "the same crash", extra={"crash": "E: x", "repeated": True}))
    assert dispatch.recover(store, tmp_path, os.environ, popen=pytest.fail) == []


def test_no_resume_without_autowork_for_an_old_death_or_one_that_cannot_start(store: Store, tmp_path,
                                                                                monkeypatch):
    monkeypatch.setenv("LOCKS_AUTOWORK", "off")  # what follows reads its absence, then turns it on
    from locks import stops

    task = store.create_task("x", status="interrupted", source="locks", cwd=str(tmp_path))
    stops.record(store, task["id"], stops.Stop("died", "no sign of life"))
    assert dispatch.recover(store, tmp_path, os.environ, popen=pytest.fail) == []  # autowork off in tests
    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    later = __import__("time").time() + dispatch.AUTO_RESUME_WITHIN_S + 60
    store.add_event(task["id"] + 1, "attempt_started", seq=1)  # a worker started since: the death was seen
    assert dispatch.recover(store, tmp_path, os.environ, popen=pytest.fail, now=later) == []
    much_later = __import__("time").time() + dispatch.AUTO_RESUME_ABSENT_S + 60
    store._conn.execute("DELETE FROM events WHERE kind = 'attempt_started'")
    assert dispatch.recover(store, tmp_path, os.environ, popen=pytest.fail, now=much_later) == []

    def broken(*a, **kw):
        raise OSError("no fork")

    assert dispatch.recover(store, tmp_path, os.environ, popen=broken) == []
    store._conn.execute("UPDATE events SET ts = 'garbled' WHERE task_id = ? AND kind = 'finished'", (task["id"],))
    assert dispatch.recover(store, tmp_path, os.environ, popen=pytest.fail) == []
    assert dispatch._alive(None) is False and dispatch._alive(os.getpid()) and not dispatch._alive(2 ** 22 + 7)


def test_mechanical_stops_are_resumed_without_waking_the_session(store: Store, tmp_path, monkeypatch):
    from locks import stops

    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    started: list[list[str]] = []
    made = {}
    for cause in ("turns", "network", "load", "review", "permission"):
        task = store.create_task(cause, status=stops.Stop(cause, "r").status, source="locks", cwd=str(tmp_path),
                                 session_id="s")
        stops.record(store, task["id"], stops.Stop(cause, "r"))
        made[cause] = task["id"]
    found = dispatch.recover(store, tmp_path, os.environ, popen=lambda argv, **kw: started.append(argv))
    assert sorted(found) == sorted(made[c] for c in ("turns", "network", "load"))
    turns = next(a for a in started if a[a.index("resume") + 1] == str(made["turns"]))
    assert turns[-2:] == ["--max-turns", str(stops.RESUME_TURNS)]
    # What needs judgment or a person still reaches the session; the rest does not.
    assert {t["id"] for t in store.unreported("s")} == {made["review"], made["permission"]}
    assert store.events_of(made["load"], "auto_resumed")[0]["data"]["cause"] == "load"


def test_the_endings_command_resumes_a_mechanical_stop_before_it_could_wake(store: Store, tmp_path, monkeypatch,
                                                                          capsys):
    from locks import stops

    calls = []
    monkeypatch.setattr(dispatch, "recover", lambda *a, **kw: calls.append(1) or [])
    assert cli.main(["endings", "--session", "s", "--json"]) == 0 and calls == [1]
    assert cli.main(["endings", "--session", "s", "--peek", "--json"]) == 0 and calls == [1]
    capsys.readouterr()
    assert stops.Stop("turns", "r").status == "replan"


def _age_resumes(store: Store, task_id: int) -> None:
    """Make the resumes already started old enough that `recover` no longer reads them as starting."""
    store._conn.execute("UPDATE events SET ts = '2000-01-01T00:00:00+00:00' WHERE task_id = ? "
                        "AND kind = 'auto_resumed'", (task_id,))


def test_two_recovers_at_once_start_one_resume_and_a_resume_that_never_took_hold_is_tried_again(
        store: Store, tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from locks import stops

    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    started: list[list[str]] = []
    task = store.create_task("build the export", status="blocked", source="locks", cwd=str(tmp_path))
    reset = datetime.now(UTC) - timedelta(hours=1)  # a limit (it allows more than one resume) already over
    stops.record(store, task["id"], stops.Stop("limit", "the account's usage limit stopped it",
                                               extra={"resets_at": reset.isoformat(timespec="seconds")}))

    def popen(argv, **kw):
        started.append(argv)

    # The dispatcher's loop, a waiter and a turn's end all call `recover`; the first resume has not
    # flipped its task to running yet, so the others see the same stop and must not start a second worker.
    assert dispatch.recover(store, tmp_path, os.environ, popen=popen) == [task["id"]]
    assert dispatch.recover(store, tmp_path, os.environ, popen=popen) == []
    assert len(started) == 1
    assert len(store.events_of(task["id"], "auto_resumed")) == 1
    # One that never took hold (no process came up) is tried again once the window has passed.
    _age_resumes(store, task["id"])
    assert dispatch.recover(store, tmp_path, os.environ, popen=popen) == [task["id"]]
    assert len(started) == 2
    # One that took hold and met the limit again is resumed at once: its new ending came after it.
    stops.record(store, task["id"], stops.Stop("limit", "the account's usage limit stopped it",
                                               extra={"resets_at": reset.isoformat(timespec="seconds")}))
    assert dispatch.recover(store, tmp_path, os.environ, popen=popen) == [task["id"]]
    assert len(started) == 3


def test_a_usage_limit_is_resumed_after_its_reset_and_a_waiter_wakes_it_then(store: Store, tmp_path, monkeypatch):
    import time as clock
    from datetime import UTC, datetime, timedelta

    from locks import stops

    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    resets = datetime.now(UTC) + timedelta(hours=2)
    task = store.create_task("build the export", status="blocked", source="locks", cwd=str(tmp_path), session_id="s")
    stops.record(store, task["id"], stops.Stop("limit", "the account's usage limit stopped it",
                                               extra={"resets_at": resets.isoformat(timespec="seconds")}))
    started: list[list[str]] = []
    # Before the reset: no resume, one waiter that sleeps until then, started once.
    assert dispatch.recover(store, tmp_path, os.environ, popen=lambda argv, **kw: started.append(argv)) == []
    assert dispatch.recover(store, tmp_path, os.environ, popen=lambda argv, **kw: started.append(argv)) == []
    assert len(started) == 1 and started[0][3:5] == ["limit-wait", "--until"]
    assert int(started[0][5]) == int(resets.timestamp()) + dispatch.LIMIT_GRACE_S
    # After it, resumed; and a limit met again after its reset is another wait, up to LIMIT_RESUMES.
    after = resets.timestamp() + dispatch.LIMIT_GRACE_S + 1
    for _ in range(dispatch.LIMIT_RESUMES):
        assert dispatch.recover(store, tmp_path, os.environ, popen=lambda argv, **kw: started.append(argv),
                                now=after) == [task["id"]]
        _age_resumes(store, task["id"])  # the resume took hold, met the limit again and ended after the window
    assert dispatch.recover(store, tmp_path, os.environ, popen=pytest.fail, now=after) == []
    # One that named no reset is tried an hour after it stopped.
    unknown = store.create_task("y", status="blocked", source="locks", cwd=str(tmp_path))
    stops.record(store, unknown["id"], stops.Stop("limit", "r"))
    soon = clock.time() + 60
    assert dispatch._due(stops.of(store, store.get_task(unknown["id"])), soon) == soon + dispatch.LIMIT_UNKNOWN_S


def test_limit_wait_sleeps_until_the_reset_then_resumes_and_revives(monkeypatch, capsys):
    import argparse
    import time as clock

    slept, calls = [], []
    monkeypatch.setattr(dispatch, "recover", lambda *a, **kw: calls.append("recover") or [7])
    monkeypatch.setattr(dispatch, "revive", lambda *a, **kw: calls.append("revive") or ["r"])
    assert cli.cmd_limit_wait(argparse.Namespace(until=clock.time() + 100), sleep=slept.append) == 0
    assert 90 < slept[0] <= 100 and calls == ["recover", "revive"]
    assert "resumed #7; started 1 dispatcher(s)" in capsys.readouterr().out


def test_a_death_nobody_could_see_is_resumed_past_the_window(store: Store, tmp_path, monkeypatch):
    from locks import stops

    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    task = store.create_task("x", status="interrupted", source="locks", cwd=str(tmp_path))
    stops.record(store, task["id"], stops.Stop("died", "no sign of life: its process (141) is gone"))
    started: list[list[str]] = []
    later = __import__("time").time() + dispatch.AUTO_RESUME_WITHIN_S + 3600  # the container came back hours on
    assert dispatch.recover(store, tmp_path, os.environ, popen=lambda argv, **kw: started.append(argv),
                            now=later) == [task["id"]]


def test_a_resume_that_never_took_hold_does_not_spend_the_one_resume(store: Store, tmp_path, monkeypatch):
    from locks import stops

    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    monkeypatch.setattr(dispatch, "RESUME_STARTING_S", 0)
    task = store.create_task("x", status="interrupted", source="locks", cwd=str(tmp_path))
    stops.record(store, task["id"], stops.Stop("died", "no sign of life"))
    started: list[list[str]] = []

    def go():
        return dispatch.recover(store, tmp_path, os.environ, popen=lambda argv, **kw: started.append(argv))

    # The machine went down as the resume started: it died before its first attempt, more than once.
    for _ in range(dispatch.AUTO_RESUME_TRIES):
        assert go() == [task["id"]]
    assert go() == []  # bounded: a resume that never takes hold is not tried forever
    assert len(started) == dispatch.AUTO_RESUME_TRIES

    other = store.create_task("y", status="interrupted", source="locks", cwd=str(tmp_path))
    stops.record(store, other["id"], stops.Stop("died", "no sign of life"))
    assert dispatch.recover(store, tmp_path, os.environ, popen=lambda argv, **kw: None) == [other["id"]]
    store.add_event(other["id"], "attempt_started", seq=2)  # this one took hold, and died again
    stops.record(store, other["id"], stops.Stop("died", "no sign of life"))
    assert dispatch.recover(store, tmp_path, os.environ, popen=pytest.fail) == []  # the orchestrator's now


def test_a_path_in_a_negated_sentence_is_no_named_surface():
    named = lambda body: dispatch.named_surfaces({"body": body})  # noqa: E731
    assert named("Do not edit docs/CHANGELOG.md") == ()
    assert named("Never touch src/a.py or src/b.py") == ()
    assert named("Edit docs/CHANGELOG.md") == ("docs/CHANGELOG.md",)
    assert named("Edit src/new.py. Do not edit docs/CHANGELOG.md or CLAUDE.md; also change src/other.py") == (
        "src/new.py", "src/other.py")
    assert named("Leave docs/q.md alone, add src/w.py") == ("src/w.py",)
    # "No part edits X", "nor", and a negation after the paths that points back at them.
    assert named("No new setting, and no part edits CHANGELOG.md, CLAUDE.md or the version. Add src/a.py") == (
        "src/a.py",)
    assert named("Writing it down (a line in CLAUDE.md, AGENTS.md) fixes nothing, so never edit those files") == ()
    assert named("Do not edit docs/a.md nor docs/b.md") == ()
    assert named("There is no test in tests/test_a.py for it") == ("tests/test_a.py",)


def test_a_folder_named_beside_files_inside_it_names_those_files_alone():
    named = lambda body: dispatch.named_surfaces({"body": body})  # noqa: E731
    assert named("Fix both in src/locks/a.py and src/locks/b.py; items that touch one module of src/ are one") == (
        "src/locks/a.py", "src/locks/b.py")
    assert named("Add a page under docs/ and change src/x.py") == ("docs/**", "src/x.py")


def test_a_bare_file_name_may_be_the_file_another_task_names_with_its_folder():
    from locks import flow

    assert dispatch.names_touch(["launch.py"], ["src/locks/launch.py"])
    assert dispatch.names_touch(["src/locks/launch.py"], ["launch.py", "docs/x.md"])
    assert not dispatch.names_touch(["launch.py"], ["src/locks/route.py"])
    assert not dispatch.names_touch(["src/a/launch.py"], ["src/b/launch.py"])  # both say where: two files
    ahead = {"id": 7, "body": "Change src/locks/launch.py to pass the flag"}
    waits = flow.by_surfaces({"id": 8, "body": "In launch.py add the prompt"}, [ahead])
    assert waits is not None and not waits.parallel and "#7" in waits.reason


def test_a_died_task_a_person_asked_to_cancel_is_never_resumed_on_its_own(store: Store, tmp_path, monkeypatch):
    from locks import stops

    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    started: list[list[str]] = []
    task = store.create_task("build the export", status="interrupted", source="locks", cwd=str(tmp_path))
    stops.record(store, task["id"], stops.Stop("died", "no sign of life"))
    store.request_cancel(task["id"])
    before = store.last_event(task["id"], "reported")
    assert dispatch.recover(store, tmp_path, os.environ, popen=lambda argv, **kw: started.append(argv)) == []
    assert started == [] and store.events_of(task["id"], "auto_resumed") == []
    assert store.last_event(task["id"], "reported") == before
