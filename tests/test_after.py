from __future__ import annotations

import json

import pytest

from locks import after, cli, dispatch, flow, stops, workflows
from locks.escalate import Failure
from locks.launch import WorkerResult
from locks.orchestrate import Options, run
from locks.store import Store
from locks.ui import api

from .conftest import git
from .test_orchestrate import Script, bad, kind, ok
from .test_workflows import CHAIN, _runner, _save


def _queue(store: Store, where, text: str, after_task: int | None = None) -> dict:
    """`locks queue add` as a person types it, and the task it queued."""
    args = ["queue", "add", text, "--repo", str(where), "--json"]
    if after_task is not None:
        args += ["--after", str(after_task)]
    assert cli.main(args) == 0
    store._conn.commit()
    return store.list_tasks(status=["queued", "dismissed", "cancelled"], limit=1)[0]


def _ran(store: Store, task: dict, where, *results: WorkerResult, write: str | None = None, **kw):
    """A queued task run under its own id, as the dispatcher runs it."""
    return run(task["body"], where, store, Options(**kw), registry={}, launcher=Script(*results, write=write),
               classifier=kind("implement"), task_id=task["id"])


def _picked(store: Store) -> list[int]:
    return [t["id"] for t in flow.runnable(store, None, parallel=False, decide=lambda t, a: None)]


# --- held until it passes, then released on what it kept ----------------------------------


@pytest.mark.usefixtures("no_verifier")
def test_work_queued_after_a_task_waits_for_it_and_starts_on_the_branch_it_kept(git_repo, store, kicks, capsys):
    first = _queue(store, git_repo, "add the totals module")
    capsys.readouterr()
    second = _queue(store, git_repo, "use the totals in the report", after_task=first["id"])
    said = json.loads(capsys.readouterr().out)
    assert said["waits"] == f"waits for #{first['id']} to pass"
    assert after.hold(second) == {"task": first["id"]} and after.held(second)
    # the dispatcher never starts it, and it holds no place in the lane
    assert _picked(store) == [first["id"]]
    for view in (["queue", "list", "--repo", str(git_repo)], ["tasks", "--repo", str(git_repo)],
                 ["show", str(second["id"])]):
        assert cli.main(view) == 0
        assert f"waits for #{first['id']} to pass" in capsys.readouterr().out, view
    board = api.board(store)
    shown = [t for lane in board["queued"] for t in lane["tasks"] if t["id"] == second["id"]]
    assert shown[0]["waits"] == f"waits for #{first['id']} to pass"

    report = _ran(store, first, git_repo, ok("totals"), write="totals.py")
    assert report.status == "done" and report.branch == f"locks/task-{first['id']}"
    tip = git(git_repo, "rev-parse", report.branch)
    freed = store.get_task(second["id"])
    assert not after.held(freed) and json.loads(freed["options"])["base"] == tip
    released = store.last_event(second["id"], "released")["data"]
    assert released["after"] == {"task": first["id"]} and released["base"] == tip
    assert kicks[-1] == (str(git_repo), freed["repo"])  # the engine starts its lane
    assert dispatch.waiting(store, freed) is None and _picked(store) == [second["id"]]

    seen = []

    def launcher(spec):
        seen.append(sorted(p.name for p in spec.target_dir.iterdir()))
        return ok("report")

    done = run(freed["body"], git_repo, store, flow.queued_options(freed, Options()), registry={},
               launcher=launcher, classifier=kind("implement"), task_id=second["id"])
    assert done.status == "done" and "totals.py" in seen[0]  # it starts on what the first one kept
    line = (f"queued after #{first['id']}, which passed: this starts on locks/task-{first['id']} ({tip[:12]}), "
            "the branch it kept; nothing is merged or rebased")
    assert line in done.plan.reasons
    assert line in store.last_event(second["id"], "planned")["data"]["reasons"]
    # nothing was merged into the person's checkout
    assert not (git_repo / "totals.py").exists()


def test_held_work_stays_held_while_what_it_waits_for_stopped_and_a_resume_to_a_pass_releases_it(
        git_repo, store, kicks):
    first = _queue(store, git_repo, "migrate the totals table")
    second = _queue(store, git_repo, "read the new totals", after_task=first["id"])
    refused = WorkerResult(False, Failure.PERMISSION, "the migration was refused", denied=("Bash(alembic upgrade)",),
                           allow=("Bash(alembic:*)",))
    stopped = _ran(store, first, git_repo, refused)
    assert stopped.status == "blocked" and after.held(store.get_task(second["id"]))
    waits = dispatch.waiting(store, store.get_task(second["id"]))
    assert waits == f"waits for #{first['id']}, which stopped as permission: a resume of it to a pass releases this"
    assert _picked(store) == [] and not store.events_of(second["id"], "released")
    # a resume of it to a pass releases it then
    resumed = run(first["body"], git_repo, store, Options(allow_tools=("Bash(alembic:*)",)), registry={},
                  launcher=Script(ok("migrated")), classifier=kind("implement"), task_id=first["id"])
    assert resumed.status == "done" and not after.held(store.get_task(second["id"]))
    assert _picked(store) == [second["id"]]


def test_held_work_says_how_the_work_it_waits_for_stopped_by_its_cause(git_repo, store):
    first = store.create_task("fix the cart", status="blocked", source="locks", repo="r", cwd=str(git_repo))
    stops.record(store, first["id"], stops.Stop("review", "the independent check still finds problems"))
    held = store.enqueue("then the invoice", repo="r", cwd=str(git_repo), options={"after": {"task": first["id"]}})
    assert dispatch.waiting(store, held).startswith(f"waits for #{first['id']}, which stopped as review")


def test_dismissing_or_cancelling_held_work_takes_it_off_the_queue(git_repo, store, kicks, capsys):
    first = _queue(store, git_repo, "add the totals module")
    dropped = _queue(store, git_repo, "use the totals", after_task=first["id"])
    cancelled = _queue(store, git_repo, "document the totals", after_task=first["id"])
    capsys.readouterr()
    assert cli.main(["dismiss", str(dropped["id"])]) == 0
    assert (f"took task #{dropped['id']} off the queue (it waits for #{first['id']} to pass)"
            in capsys.readouterr().out)
    assert cli.main(["cancel", str(cancelled["id"])]) == 0
    store._conn.commit()
    for task, cause in ((dropped, "dismissed"), (cancelled, "cancelled")):
        now = store.get_task(task["id"])
        assert now["status"] == cause and stops.of(store, now).cause == cause
        assert store.final_event(task["id"])["data"]["stop"]["cause"] == cause
    assert stops.of(store, store.get_task(dropped["id"])).extra["was"] == "queued"
    assert store.last_event(dropped["id"], "reported") is not None  # the person did it: no session is told
    view = stops.view(store, store.get_task(dropped["id"]))
    assert view["next"] is None and view["todo"] == "it never ran: queue it again if you still want it"
    # what it waited for passes later: nothing comes back
    _ran(store, first, git_repo, ok())
    assert {store.get_task(t["id"])["status"] for t in (dropped, cancelled)} == {"dismissed", "cancelled"}
    assert not store.events_of(dropped["id"], "released")


def test_dismissing_what_held_work_waits_for_takes_it_off_the_queue_and_tells_its_session(
        git_repo, store, kicks, capsys, monkeypatch):
    monkeypatch.setenv("LOCKS_SESSION_ID", "s-1")
    first = _queue(store, git_repo, "migrate the totals table")
    held = _queue(store, git_repo, "read the new totals", after_task=first["id"])
    _ran(store, first, git_repo, bad(Failure.CODE_BUG, "the migration fails"), max_attempts=1)
    stopped = store.get_task(first["id"])
    assert stopped["status"] != "done" and after.held(store.get_task(held["id"]))
    # the ending of what it waits for says so
    from locks import hooks

    notice, _ = hooks.endings(store, "s-1")
    assert f"1 queued task(s) wait for it to pass: #{held['id']} read the new totals" in notice
    capsys.readouterr()
    assert cli.main(["dismiss", str(first["id"]), "--why", "not needed"]) == 0
    assert f"and took #{held['id']} off the queue" in capsys.readouterr().out
    store._conn.commit()
    gone = store.get_task(held["id"])
    stop = stops.of(store, gone)
    assert gone["status"] == "cancelled" and stop.cause == after.ABANDONED and stop.who == "locks"
    assert f"it waited for #{first['id']} to pass, which left the board" in stop.reason
    assert not after.hold(gone) and _picked(store) == []
    assert stops.of(store, store.get_task(first["id"])).extra["took_along"] == [held["id"]]
    # its session hears of it as an ending, with the resume that starts it on the base
    notice, ids = hooks.endings(store, "s-1")
    assert held["id"] in ids and f"locks resume {held['id']}" in notice
    view = stops.view(store, gone)
    assert view["next"] == f"locks resume {held['id']}" and view["todo"].startswith("it never started")


def test_a_resume_starts_held_work_that_lost_what_it_waited_for_on_the_base(git_repo, store, kicks):
    first = store.create_task("fix the cart", status="blocked", source="locks", repo="r", cwd=str(git_repo))
    held = store.enqueue("then the invoice", repo="r", cwd=str(git_repo), options={"after": {"task": first["id"]}})
    stops.dismiss(store, store.get_task(first["id"]), via="cli")
    assert store.get_task(held["id"])["status"] == "cancelled"
    launched = []
    monkeypatch_run = run

    def fake(text, where, store_, options, **kw):
        launched.append((text, kw.get("task_id")))
        return monkeypatch_run(text, where, store_, options, registry={}, launcher=Script(ok()),
                               classifier=kind("implement"), **kw)

    import locks.orchestrate as orchestrate_mod

    real = orchestrate_mod.run
    orchestrate_mod.run = fake
    try:
        assert cli.main(["resume", str(held["id"]), "--no-report"]) == 0
    finally:
        orchestrate_mod.run = real
    assert launched == [("then the invoice", held["id"])]
    assert store.get_task(held["id"])["status"] == "done"


@pytest.mark.usefixtures("no_verifier")
def test_held_work_follows_the_task_that_superseded_what_it_waited_for(git_repo, store, kicks):
    first = _queue(store, git_repo, "add the totals module")
    held = _queue(store, git_repo, "use the totals in the report", after_task=first["id"])
    _ran(store, first, git_repo, bad(Failure.CODE_BUG, "no"), max_attempts=1)
    again = _queue(store, git_repo, "add the totals module")
    report = _ran(store, again, git_repo, ok("totals"), write="totals.py")
    assert report.status == "done" and first["id"] in report.superseded
    freed = store.get_task(held["id"])
    assert not after.held(freed) and json.loads(freed["options"])["base"] == git(git_repo, "rev-parse", report.branch)
    moved = store.last_event(held["id"], "after_moved")["data"]
    assert moved["was"] == {"task": first["id"]} and moved["now"] == {"task": again["id"]}


def test_held_work_behind_a_task_whose_change_landed_starts_on_the_base(git_repo, store, kicks):
    first = store.create_task("add the totals module", status="running", source="locks", cwd=str(git_repo), repo="r")
    stops.record(store, first["id"], stops.Stop("died", "its process went away"))
    held = store.enqueue("use the totals", repo="r", cwd=str(git_repo), options={"after": {"task": first["id"]}})
    branch = f"locks/task-{first['id']}"
    git(git_repo, "checkout", "-q", "-b", branch)
    (git_repo / "totals.py").write_text("totals\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "add the totals")
    git(git_repo, "checkout", "-q", "main")
    git(git_repo, "merge", "-q", "--no-ff", "-m", "take the totals", branch)
    assert stops.landed(store, git_repo) == [first["id"]]
    freed = store.get_task(held["id"])
    assert freed["status"] == "queued" and not after.held(freed) and "base" not in json.loads(freed["options"])
    assert "is already in main" in store.last_event(held["id"], "released")["data"]["why"]


def test_cancelling_a_run_takes_the_work_queued_after_it_off_the_queue(git_repo, store, kicks):
    _save(CHAIN, git_repo)
    started = workflows.start(store, "chain", git_repo, {"goal": "add the export"})
    held = store.enqueue("then the docs", repo=started["repo"], cwd=str(git_repo),
                         options={"after": {"run": started["id"]}})
    workflows.cancel(store, started["id"])
    gone = store.get_task(held["id"])
    assert gone["status"] == "cancelled" and stops.of(store, gone).cause == after.ABANDONED
    assert f"run #{started['id']}" in stops.of(store, gone).reason


@pytest.mark.usefixtures("no_verifier")
def test_a_chain_is_released_one_link_at_a_time(git_repo, store, kicks):
    a = _queue(store, git_repo, "add the schema")
    b = _queue(store, git_repo, "add the API on the schema", after_task=a["id"])
    c = _queue(store, git_repo, "add the page on the API", after_task=b["id"])
    assert _picked(store) == [a["id"]]
    _ran(store, a, git_repo, ok(), write="schema.py")
    assert not after.held(store.get_task(b["id"])) and after.held(store.get_task(c["id"]))
    assert dispatch.waiting(store, store.get_task(c["id"])) == f"waits for #{b['id']} to pass"
    assert _picked(store) == [b["id"]]
    _ran(store, store.get_task(b["id"]), git_repo, ok(), write="api.py")
    assert not after.held(store.get_task(c["id"])) and _picked(store) == [c["id"]]
    # each link starts on the one before it, which holds the links before that
    b_tip = git(git_repo, "rev-parse", f"locks/task-{b['id']}")
    assert json.loads(store.get_task(c["id"])["options"])["base"] == b_tip


# --- refused when it cannot be waited for, released at once when it already passed -------


def test_a_missing_or_failed_task_or_run_is_refused_when_queued(git_repo, store, capsys):
    failed = store.create_task("x", status="failed", source="locks", repo="r", cwd=str(git_repo))
    stops.record(store, failed["id"], stops.Stop("attempts", "6 attempts without a pass"))
    turn = store.create_task("a question", status="done", source="hook", repo="r", cwd=str(git_repo))
    store._conn.commit()
    for argv, said in (
            (["queue", "add", "y", "--repo", str(git_repo), "--after", "999"], "there is no task #999"),
            (["run", "y", "--repo", str(git_repo), "--after", "999"], "there is no task #999"),
            (["queue", "add", "y", "--repo", str(git_repo), "--after", str(failed["id"])],
             f"#{failed['id']} stopped as attempts, and its work was dropped: resume it first"),
            (["run", "y", "--repo", str(git_repo), "--after", str(turn["id"])], "a session's turn")):
        assert cli.main(argv) == 2, argv
        assert said in capsys.readouterr().err
    assert store.list_tasks(status=["queued"]) == []  # nothing was queued
    assert cli.main(["workflow", "run", "chain", "--input", "goal=x", "--after", "999", "--repo", str(git_repo)]) == 1
    assert "there is no workflow run #999" in capsys.readouterr().err
    with pytest.raises(after.AfterError, match="was cancelled"):
        after.target(store, run_id=store.create_run(name="w", scope="user", source="s", definition="{}",
                                                      status="cancelled")["id"])


@pytest.mark.usefixtures("no_verifier")
def test_work_queued_after_work_that_passed_starts_at_once_on_its_branch_or_the_base_once_merged(
        git_repo, store, capsys):
    done = run("add the totals module", git_repo, store, registry={}, launcher=Script(ok(), write="totals.py"),
               classifier=kind("implement"))
    tip = git(git_repo, "rev-parse", done.branch)
    capsys.readouterr()
    assert cli.main(["run", "use the totals", "--repo", str(git_repo), "--after", str(done.task_id)]) == 0
    out = capsys.readouterr().out
    assert f"this starts on {done.branch} ({tip[:12]})" in out
    queued = store.list_tasks(status=["queued"], limit=1)[0]
    assert not after.held(queued) and json.loads(queued["options"])["base"] == tip
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merge totals", done.branch)
    later = _queue(store, git_repo, "document the totals", after_task=done.task_id)
    assert "base" not in json.loads(later["options"])
    assert after.released_line(store, later["id"]) == (
        f"queued after #{done.task_id}, which passed on {done.branch}: that branch is already merged into the base, "
        "so this starts on the base")
    git(git_repo, "branch", "-D", done.branch)  # gone since: the base, and the line says why
    gone = _queue(store, git_repo, "test the totals", after_task=done.task_id)
    assert "base" not in json.loads(gone["options"]) and "that branch is not in this checkout" in (
        after.released_line(store, gone["id"]))


@pytest.mark.usefixtures("no_verifier")
def test_work_in_another_repository_waits_and_starts_on_its_own_base_reading_the_other(
        git_repo, store, kicks, tmp_path):
    other = tmp_path / "library"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / "lib.py").write_text("x = 1\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "init")
    first = _queue(store, other, "build the shared library")
    second = _queue(store, git_repo, "adapt the library here", after_task=first["id"])
    assert after.held(second)
    # Numbers are global: what it waits on is named with its repository and its title.
    assert after.waiting(store, second) == (
        f"waits for task #{first['id']} in {first['repo']} (build the shared library) to pass")
    assert "is a task in" in after.caveat(store, {"task": first["id"]}, second["repo"])
    assert after.caveat(store, {"task": first["id"]}, first["repo"]) is None
    report = _ran(store, first, other, ok("built"), write="shared.py")
    assert report.status == "done"
    freed = store.get_task(second["id"])
    options = json.loads(freed["options"])
    # Nothing of the other repository crosses: this one's base, the other given to read.
    assert not after.held(freed) and "base" not in options and options["add_dirs"] == [str(other.resolve())]
    line = after.released_line(store, second["id"])
    assert f"passed in another repository, {other.resolve()}, on {report.branch}" in line
    assert f"git -C {other.resolve()} diff HEAD...{report.branch}" in line and "shared.py" in line
    assert flow.queued_options(freed, Options()).add_dirs == (str(other.resolve()),)


def test_after_takes_a_task_or_a_run_by_name(git_repo, store, capsys):
    assert after.ref("5") == {"task": 5} and after.ref("task:5") == {"task": 5} and after.ref("run:5") == {"run": 5}
    # non-ASCII digits are no task number (`²` passes isdigit, and int() refuses it)
    for bad_ref in ("run:x", "step:5", "", "²", "task:\u0663"):
        with pytest.raises(ValueError, match="--after takes"):
            after.ref(bad_ref)
    with pytest.raises(SystemExit):
        cli.main(["queue", "add", "x", "--repo", str(git_repo), "--after", "step:5"])
    assert cli.main(["queue", "add", "x", "--repo", str(git_repo), "--after", "run:99"]) == 2
    assert "there is no workflow run #99" in capsys.readouterr().err


def test_a_task_that_kept_no_branch_releases_onto_the_base(git_repo, store, kicks):
    first = _queue(store, git_repo, "explain the totals, without changing any file")
    second = _queue(store, git_repo, "then fix the totals", after_task=first["id"])
    run(first["body"], git_repo, store, Options(), registry={}, launcher=Script(ok("it sums twice")),
        classifier=kind("explore"), task_id=first["id"])
    assert "base" not in json.loads(store.get_task(second["id"])["options"])
    assert after.released_line(store, second["id"]) == (
        f"queued after #{first['id']}, which passed and kept no branch: this starts on the base")


# --- a workflow run after another -------------------------------------------------------


def test_a_workflow_run_after_another_waits_for_it_and_builds_on_its_last_step(git_repo, store, kicks, capsys):
    _save(CHAIN, git_repo)
    first = workflows.start(store, "chain", git_repo, {"goal": "the export"})
    second = workflows.start(store, "chain", git_repo, {"goal": "the import"}, after_run=first["id"])
    shown = workflows.status(store, second["id"])
    assert shown["state"] == "queued" and shown["waits"] == f"waits for run #{first['id']} to pass"
    assert shown["steps"][0]["waits"] == f"waits for run #{first['id']} to pass"
    store._conn.commit()
    assert cli.main(["workflow", "status", str(second["id"])]) == 0
    assert f"waits for run #{first['id']} to pass" in capsys.readouterr().out
    held = store.get_task(second["queued"][0])
    assert after.hold(held) == {"run": first["id"]}
    assert _picked(store) == first["queued"]

    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built"), ok("fine"), ok("polished"), ok("built"), ok("fine"),
                                     ok("polished")], seen))
    assert workflows.status(store, first["id"])["state"] == "done"
    assert workflows.status(store, second["id"])["state"] == "done"
    last = workflows.status(store, first["id"])["branch"]
    tip = git(git_repo, "rev-parse", last)
    assert store.last_event(held["id"], "released")["data"]["base"] == tip
    # the second run's first step built on everything the first run kept, and so did its last
    assert {"step1.txt", "step3.txt"} <= set(seen[3]["files"])
    options = json.loads(store.get_run(second["id"])["options"])
    assert options["base"] == options["origin"] == tip and after.KEY not in options
    assert f"run #{first['id']}'s last step" in store.last_event(held["id"], "released")["data"]["why"]


def test_a_workflow_run_after_one_that_stopped_says_where_it_stopped(git_repo, store, kicks):
    _save(CHAIN, git_repo)
    first = workflows.start(store, "chain", git_repo, {"goal": "the export"})
    second = workflows.start(store, "chain", git_repo, {"goal": "the import"}, after_run=first["id"])
    flow.work(store, runner=_runner([bad(Failure.SPEC_BUG, "cannot be read")], []))
    build = first["queued"][0]
    held = store.get_task(second["queued"][0])
    assert after.held(held) and dispatch.waiting(store, held) == (
        f"waits for run #{first['id']}, which stopped at step 'build' (#{build}) as spec: a resume of it to a "
        "pass releases this")
    with pytest.raises(after.AfterError, match="stopped at step 'build'"):
        workflows.start(store, "chain", git_repo, {"goal": "more"}, after_run=first["id"])
    workflows.cancel(store, first["id"])
    assert dispatch.waiting(store, held) == (
        f"waits for run #{first['id']}, which stopped as cancelled: `locks dismiss {held['id']}` drops this")


def test_a_workflow_run_released_with_nothing_kept_reads_its_changes_from_head_as_it_is_now(git_repo, store, kicks):
    _save(CHAIN, git_repo)
    first = workflows.start(store, "chain", git_repo, {"goal": "the export"})
    second = workflows.start(store, "chain", git_repo, {"goal": "the import"}, after_run=first["id"])
    queued_at = json.loads(store.get_run(second["id"])["options"])["origin"]
    # another commit lands while it waits, and the run it waited for kept no branch
    (git_repo / "meanwhile.py").write_text("x = 1\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "meanwhile")
    store.update_run(first["id"], status="done")
    released = after.release(store, run=store.get_run(first["id"]))
    assert [t["id"] for t in released] == second["queued"]
    options = json.loads(store.get_run(second["id"])["options"])
    assert options["origin"] == git(git_repo, "rev-parse", "HEAD") != queued_at
    assert "base" not in options and after.KEY not in options


def test_held_work_whose_task_or_run_no_longer_exists_says_so(git_repo, store):
    on_task = store.enqueue("then the invoice", repo="r", cwd=str(git_repo), options={"after": {"task": 9999}})
    on_run = store.enqueue("then the receipt", repo="r", cwd=str(git_repo), options={"after": {"run": 9998}})
    assert dispatch.waiting(store, on_task) == (
        f"waits for #9999, which no longer exists: `locks dismiss {on_task['id']}` drops this")
    assert dispatch.waiting(store, on_run) == (
        f"waits for run #9998, which no longer exists: `locks dismiss {on_run['id']}` drops this")


# --- a task that waited for files other work changed starts on that work -------------------


def _waited(store: Store, git_repo, behind: dict, text: str) -> dict:
    """A queued task the dispatcher held behind `behind` for the files it names."""
    task = _queue(store, git_repo, text)
    store.add_event(task["id"], "dispatch_decided", parallel=False, by="surfaces", behind=behind["id"],
                    reason=f"the files it names may be the ones #{behind['id']} changes")
    return task


@pytest.mark.usefixtures("no_verifier")
def test_a_task_that_waited_for_a_file_starts_on_the_branch_that_changed_it(git_repo, store, kicks):
    first = _queue(store, git_repo, "add the totals module in totals.py")
    second = _waited(store, git_repo, first, "In totals.py add the monthly sum")
    assert flow.chain(store, dict(second), git_repo) is None  # the work ahead has not passed
    report = _ran(store, first, git_repo, ok("totals"), write="totals.py")
    tip = git(git_repo, "rev-parse", report.branch)
    now = store.get_task(second["id"])
    why = flow.chain(store, now, git_repo)
    assert why is not None and f"waited for #{first['id']}" in why and "totals.py" in why
    chained = store.last_event(second["id"], "chained")["data"]
    assert chained["base"] == tip and chained["after"] == {"task": first["id"]} and chained["files"] == ["totals.py"]
    assert json.loads(now["options"])["base"] == tip == json.loads(store.get_task(second["id"])["options"])["base"]
    assert flow.chain(store, store.get_task(second["id"]), git_repo) is None  # once
    seen = []

    def launcher(spec):
        seen.append(sorted(p.name for p in spec.target_dir.iterdir()))
        return ok("sum")

    freed = store.get_task(second["id"])
    done = run(freed["body"], git_repo, store, flow.queued_options(freed, Options()), registry={},
               launcher=launcher, classifier=kind("implement"), task_id=second["id"])
    assert done.status == "done" and "totals.py" in seen[0] and why in done.plan.reasons


@pytest.mark.usefixtures("no_verifier")
def test_a_task_that_waited_starts_on_the_base_where_the_work_ahead_gives_it_nothing(git_repo, store, kicks):
    first = _queue(store, git_repo, "add the totals module in totals.py")
    other = _waited(store, git_repo, first, "In report.py add the header")  # it names no file the first changed
    stopped = _queue(store, git_repo, "add the audit module in audit.py")
    behind_stopped = _waited(store, git_repo, stopped, "In audit.py add the trail")
    held = _waited(store, git_repo, first, "In totals.py add the yearly sum")
    _ran(store, first, git_repo, ok("totals"), write="totals.py")
    _ran(store, stopped, git_repo, bad(Failure.PERMISSION, "refused"), write="audit.py")
    assert flow.chain(store, store.get_task(other["id"]), git_repo) is None
    assert flow.chain(store, store.get_task(behind_stopped["id"]), git_repo) is None  # it did not pass
    # Its independent check is still to come: the work may yet be reopened.
    store.add_event(first["id"], "verifier_deferred")
    assert flow.chain(store, store.get_task(held["id"]), git_repo) is None
    store.add_event(first["id"], "verified", late=True)
    assert flow.chain(store, store.get_task(held["id"]), git_repo) is not None
    assert [store.last_event(t["id"], "chained") for t in (other, behind_stopped)] == [None, None]


# --- what queueing says beyond where the task waits ----------------------------------------


def test_queueing_says_the_same_text_is_queued_already_and_a_surface_that_is_a_whole_folder(git_repo, store,
                                                                                           kicks, capsys):
    first = _queue(store, git_repo, "In tests/test_totals.py add the yearly case")
    capsys.readouterr()
    assert cli.main(["queue", "add", "In tests/test_totals.py add the yearly case", "--repo", str(git_repo),
                     "--surface", "tests", "--json"]) == 0
    out = capsys.readouterr()
    said = json.loads(out.out.strip().splitlines()[-1])
    assert len(said["notes"]) == 2 and f"#{first['id']} here has the same text" in said["notes"][0]
    assert "surface `tests` is the whole folder" in said["notes"][1] and "tests/test_totals.py" in said["notes"][1]
    assert all(f"locks: {note}" in out.err for note in said["notes"])
    assert cli.main(["queue", "add", "In docs/a.md fix the typo", "--repo", str(git_repo), "--json"]) == 0
    assert "notes" not in json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_a_surface_is_broad_only_as_a_folder_holding_files_the_text_names():
    from locks import surfaces

    assert surfaces.broad(["tests", "src/a.py"], ["tests/test_a.py", "src/a.py"]) == [("tests", ["tests/test_a.py"])]
    assert surfaces.broad(["tests/**"], ["tests/test_a.py"]) == [("tests/**", ["tests/test_a.py"])]
    assert surfaces.broad(["tests/test_*.py", "src/a.py", "docs"], ["tests/test_a.py", "src/a.py"]) == []
