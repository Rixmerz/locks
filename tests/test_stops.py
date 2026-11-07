from __future__ import annotations

import pytest

from locks import orchestrate, stops
from locks.escalate import Failure
from locks.launch import WorkerResult
from locks.orchestrate import Options, run
from locks.store import Store

from .test_orchestrate import Script, bad, kind, refused


def _stop(store: Store, task_id: int) -> stops.Stop:
    return stops.read(store.last_event(task_id, "finished")["data"]["stop"])


@pytest.mark.parametrize(
    ("results", "options", "cause", "who"),
    [
        ([refused()], Options(), "permission", "settings"),
        ([bad(Failure.ENVIRONMENT), bad(Failure.ENVIRONMENT)], Options(), "environment", "environment"),
        ([bad(Failure.SPEC_BUG, "the API has no such field")], Options(), "spec", "worker"),
        ([bad(Failure.TURNS_EXHAUSTED)] * 2, Options(), "turns", "locks"),
        ([bad(Failure.CODE_BUG, "still broken")] * 3, Options(), "ladder", "locks"),
        ([bad(Failure.CODE_BUG, s) for s in ("a", "b")], Options(max_attempts=2), "attempts", "locks"),
        ([bad(Failure.CODE_BUG, s) for s in ("missed a", "broke b", "left c", "forgot d")], Options(),
         "exhausted", "locks"),
    ],
)
def test_every_ending_short_of_a_pass_says_why_and_who(git_repo, store, results, options, cause, who):
    report = run("do the thing", git_repo, store, options, registry={}, launcher=Script(*results),
                 classifier=kind("implement"))
    stop = _stop(store, report.task_id)
    assert stop.cause == cause and stop.who == who and stop.status == report.status
    assert stop.reason and stop.attempt == len(report.cells)
    assert report.stop.data() == stop.data()
    assert f"stopped by {stops.WHO[who]}" in report.text()


def test_a_refusal_names_the_rules_and_the_command_that_allows_them(git_repo, store):
    report = run("create the routes", git_repo, store, registry={}, launcher=Script(refused(), write="routes.js"),
                 classifier=kind("implement"))
    stop = _stop(store, report.task_id)
    assert stop.denied == ("Bash(node server.js)",)
    assert stop.account == "created the routes; node was refused"
    assert stops.next_step(report.task_id, stop) == f"locks resume {report.task_id} --allow 'Bash(node:*)'"
    assert f"  locks resume {report.task_id} --allow 'Bash(node:*)'" in report.text()


def test_approval_and_an_old_budget_stop_say_how_to_go_on(tmp_path, store):
    script = Script(bad(Failure.INCONCLUSIVE, "could not read the queue"),
                    bad(Failure.INCONCLUSIVE, "the queue is still unclear"),
                    bad(Failure.INCONCLUSIVE, "the scheduler is unclear"),
                    bad(Failure.INCONCLUSIVE, "the workers are unclear"))
    report = run("design it", tmp_path, store, registry={}, launcher=script, classifier=kind("plan"))
    stop = _stop(store, report.task_id)
    assert stop.cause == "approval" and stop.next_cell == "fable/high"
    assert stops.next_step(report.task_id, stop).endswith("--allow-approval")
    # A stop an older locks wrote on a dollar budget reads back, its amount kept, and resumes plainly.
    old = stops.read({"cause": "budget", "reason": "the $5.00 budget is spent", "budget_usd": 5.0})
    assert old.status == "blocked" and old.data()["budget_usd"] == 5.0 and "older locks" in old.line()
    assert stops.next_step(7, old) == "locks resume 7" and "no longer exists" in stops.what_to_do(old)
    assert stops.resume_args(7, {"cause": "budget", "budget_usd": 5.0, "next": "locks resume 7"}) == [
        "resume", "7", "--detach"]
    assert stops.next_step(7, stops.Stop("spec", "wrong")) is None
    assert stops.next_step(7, stops.Stop("cancelled", "x"), resumable=False) is None


@pytest.mark.parametrize(("via", "how"), [("ui", "from the UI"), ("cli", "with `locks cancel`")])
def test_a_cancel_says_a_person_did_it_and_how(git_repo, store, via, how):
    def launcher(spec):
        store.request_cancel(store.list_tasks()[0]["id"], via=via)
        return bad(Failure.CODE_BUG, "first")

    report = run("x", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    stop = _stop(store, report.task_id)
    assert stop.who == "you" and how in stop.reason and stop.extra == {"via": via}
    assert store.get_task(report.task_id)["result"] == stop.reason


def test_a_signal_nobody_asked_for_and_ctrl_c_are_told_apart(git_repo, store):
    def killed(spec):
        raise orchestrate.Cancelled

    report = run("x", git_repo, store, registry={}, launcher=killed, classifier=kind("implement"))
    stop = _stop(store, report.task_id)
    assert stop.cause == "signal" and stop.who == "outside" and stop.status == "cancelled"

    def interrupted(spec):
        raise KeyboardInterrupt

    report = run("y", git_repo, store, registry={}, launcher=interrupted, classifier=kind("implement"))
    stop = _stop(store, report.task_id)
    assert stop.who == "you" and "Ctrl-C" in stop.reason


def test_an_error_in_locks_itself_is_its_own_account(git_repo, store):
    def broken(spec):
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        run("x", git_repo, store, registry={}, launcher=broken, classifier=kind("implement"))
    task = store.list_tasks()[0]
    stop = _stop(store, task["id"])
    assert task["status"] == "failed" and stop.cause == "crashed"
    assert "RuntimeError: disk full" in stop.reason and task["result"] == stop.reason


def test_a_pass_has_no_account(git_repo, store):
    report = run("x", git_repo, store, registry={}, launcher=Script(WorkerResult(True, None, "ok", "1 passed")),
                 classifier=kind("implement"))
    assert report.stop is None and store.last_event(report.task_id, "finished")["data"]["stop"] is None
    assert stops.of(store, store.get_task(report.task_id)) is None


def test_older_tasks_get_an_account_read_back_from_their_records(store: Store):
    blocked = store.create_task("routes", status="blocked", source="locks", cwd="/x", result="refused node")
    store.add_attempt(blocked["id"], cell="sonnet/medium", max_turns=30, passed=0, failure="permission")
    store.set_move(blocked["id"], 1, "blocked", "the worker was refused Bash(node x.js)")
    store.add_event(blocked["id"], "attempt_finished", seq=1, denied=["Bash(node x.js)"])
    stop = stops.of(store, store.get_task(blocked["id"]))
    assert stop.recovered and stop.cause == "permission" and stop.denied == ("Bash(node x.js)",)
    assert stop.reason == "the worker was refused Bash(node x.js)"

    cases = [
        ("blocked", "the $5.00 budget is spent", None, "budget"),
        ("blocked", "its directory no longer exists", None, "missing_dir"),
        ("blocked", "the environment failed twice", "environment", "environment"),
        ("needs_approval", "", "code_bug", "approval"),
        ("replan", "", "spec_bug", "spec"),
        ("replan", "", "turns_exhausted", "turns"),
        ("replan", "", "code_bug", "ladder"),
        ("failed", "6 attempts without a pass", "code_bug", "attempts"),
        ("failed", "", "code_bug", "exhausted"),
        ("cancelled", "cancelled before it finished", None, "cancelled"),
        ("interrupted", "", None, "died"),
    ]
    for status, result, failure, cause in cases:
        t = store.create_task("t", status=status, source="locks", result=result)
        if failure:
            store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=0, failure=failure)
        assert stops.of(store, store.get_task(t["id"])).cause == cause, status
    assert stops.of(store, store.create_task("t", status="done", source="locks")) is None
    assert stops.recovered("weird") is None
    cancelled = stops.of(store, store.list_tasks(status=["cancelled"])[0])
    assert cancelled.who == "unknown" and stops.WHO[cancelled.who] == "not recorded"


def test_a_recorded_account_that_no_longer_matches_the_status_is_read_again(store: Store):
    t = store.create_task("t", status="running", source="locks")
    stops.record(store, t["id"], stops.Stop("died", "gone"))
    assert stops.of(store, store.get_task(t["id"])).cause == "died"
    store.update_task(t["id"], status="cancelled")  # changed after, without an account
    assert stops.of(store, store.get_task(t["id"])).recovered


def test_the_view_says_who_in_words_and_what_to_do(store: Store):
    ran = store.create_task("ran", status="running", source="locks", cwd="/x")
    stops.record(store, ran["id"], stops.Stop("permission", "refused", denied=("Bash(ls -la)",)))
    view = stops.view(store, store.get_task(ran["id"]))
    assert view["who"] == "your permission settings" and view["status"] == "blocked"
    assert view["next"] == f"locks resume {ran['id']} --allow 'Bash(ls:*)'" and "allow" in view["todo"]
    never = store.enqueue("never ran", repo="r", cwd="/x")
    stops.record(store, never["id"], stops.Stop("cancelled", "you cancelled it", by="you"))
    view = stops.view(store, store.get_task(never["id"]))
    # a queued task cancelled before it started runs from the base on a resume
    assert view["next"] == f"locks resume {never['id']}" and view["todo"] == "resume it if you still want it"
    dropped = store.enqueue("dismissed off the queue", repo="r", cwd="/x", options={"after": {"task": ran["id"]}})
    stops.dismiss(store, store.get_task(dropped["id"]), via="cli")
    view = stops.view(store, store.get_task(dropped["id"]))
    assert view["next"] is None and view["todo"].startswith("it never ran")
    assert stops.read(None) is None and stops.read({"reason": "no cause"}) is None
    assert stops.what_to_do(stops.Stop("mystery", "x")) == "read the attempts"
    assert stops.Stop("mystery", "x").status == "failed"


def test_a_review_stop_says_what_its_resume_does(store: Store):
    """The ending's next line, as `hooks` writes it (`todo: next`): a resume briefs the worker with the
    independent check's findings, and that check reads them first."""
    held = store.create_task("add the export", status="running", source="locks", cwd="/x")
    stops.record(store, held["id"], stops.Stop("review", "the independent check still finds problems"))
    view = stops.view(store, store.get_task(held["id"]))
    assert f"{view['todo']}: {view['next']}" == (
        "the independent check kept finding problems: resume it: the worker is briefed with the findings, "
        f"and the check reads them first: locks resume {held['id']}")


def test_a_dismissed_task_leaves_the_board_and_says_who_and_what_it_was(store):
    from locks.ui import api

    task = store.create_task("audit it", status="blocked", source="locks", repo="r", cwd="/x")
    store.add_event(task["id"], "finished", status="blocked",
                    stop=stops.Stop("environment", "the environment failed twice", attempt=2).data())
    stop = stops.dismiss(store, store.get_task(task["id"]), via="ui")
    assert stop is not None
    assert stop.who == "you"
    assert stop.extra["was"] == "blocked"
    assert stop.extra["had"] == "the environment failed twice"
    task = store.get_task(task["id"])
    assert task["status"] == "dismissed"
    assert stops.of(store, task) == stop
    assert "from the UI" in stops.view(store, task)["reason"]
    assert not api.board(store, {"r"})["needs_you"]
    assert stops.dismiss(store, task, via="ui") is None  # waits on no one now


def test_a_pass_takes_its_stopped_copies_off_the_board(git_repo, store):
    text = "audit the roles across both repos"
    first = run(text, git_repo, store, registry={}, launcher=Script(bad(Failure.SPEC_BUG, "wrong")),
                classifier=kind("explore"))
    other = run("something else entirely", git_repo, store, registry={},
                launcher=Script(bad(Failure.SPEC_BUG, "wrong")), classifier=kind("explore"))
    assert first.status == "replan"
    from .test_orchestrate import ok

    passed = run(f"  {text}\n", git_repo, store, registry={}, launcher=Script(ok()), classifier=kind("explore"))
    assert passed.status == "done"
    assert passed.superseded == [first.task_id]
    assert f"off the board: #{first.task_id}" in passed.text()
    gone = store.get_task(first.task_id)
    assert gone["status"] == "dismissed"
    stop = stops.of(store, gone)
    assert stop.cause == "superseded"
    assert stop.who == "locks"
    assert stop.extra["by_task"] == passed.task_id
    assert store.get_task(other.task_id)["status"] == "replan"


def test_a_dismissed_task_resumes_as_what_it_was(git_repo, store, capsys):
    from locks import cli

    replanned = run("x", git_repo, store, registry={}, launcher=Script(bad(Failure.SPEC_BUG, "wrong")),
                    classifier=kind("implement"))
    store.close()
    assert cli.main(["dismiss", str(replanned.task_id)]) == 0
    assert "dismissed task" in capsys.readouterr().out
    # found wrong before it was dismissed: still rewritten, never resumed
    assert cli.main(["resume", str(replanned.task_id)]) == 1
    assert "is replan" in capsys.readouterr().err
    assert cli.main(["dismiss", "999"]) == 1
    assert cli.main(["dismiss", str(replanned.task_id)]) == 1
    assert "only a task that waits" in capsys.readouterr().err


def test_a_dismissed_blocked_task_resumes(git_repo, store, monkeypatch):
    from locks import cli

    blocked = run("x", git_repo, store, registry={},
                  launcher=Script(bad(Failure.ENVIRONMENT), bad(Failure.ENVIRONMENT)), classifier=kind("implement"))
    assert blocked.status == "blocked"
    stops.dismiss(store, store.get_task(blocked.task_id), via="cli")
    store.close()
    seen = []
    monkeypatch.setattr(cli.orchestrate, "run", lambda *a, **kw: seen.append(kw["task_id"]) or orchestrate.Report(
        kw["task_id"], "done", None))
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "done")
    assert cli.main(["resume", str(blocked.task_id)]) == 0
    assert seen == [blocked.task_id]


def test_a_dismissal_or_a_supersession_is_no_ending_a_session_is_told_of(git_repo, store):
    from .test_orchestrate import ok

    sent = run("same text", git_repo, store, registry={}, launcher=Script(bad(Failure.SPEC_BUG, "wrong")),
               classifier=kind("explore"), session_id="s1")
    other = run("other text", git_repo, store, registry={}, launcher=Script(bad(Failure.SPEC_BUG, "wrong")),
                classifier=kind("explore"), session_id="s1")
    store.claim_unreported("s1", via="stop")
    stops.dismiss(store, store.get_task(other.task_id), via="mod")
    run("same text", git_repo, store, registry={}, launcher=Script(ok()), classifier=kind("explore"))
    assert store.get_task(sent.task_id)["status"] == "dismissed"
    assert store.unreported("s1") == []


def test_a_dismissed_replan_offers_no_resume_unless_it_only_outgrew_its_turns(store):
    for cause, offered in (("spec", None), ("turns", "locks resume")):
        task = store.create_task(f"t-{cause}", status="replan", source="locks", repo="r", cwd="/x")
        store.add_event(task["id"], "finished", status="replan", stop=stops.Stop(cause, "r").data())
        stops.dismiss(store, store.get_task(task["id"]), via="cli")
        nxt = stops.view(store, store.get_task(task["id"]))["next"]
        assert (nxt or "").startswith(offered) if offered else nxt is None


def test_dismissing_the_task_a_lane_paused_on_opens_the_lane(store):
    """A queued task waited 16 hours behind a lane paused on a task the person had
    already dismissed."""
    stopped = store.create_task("wrong task", status="replan", source="locks", repo="r", cwd="/x")
    other = store.create_task("other", status="blocked", source="locks", repo="r", cwd="/x")
    store.create_task("next", status="queued", source="locks", repo="r", cwd="/x")
    store.pause_lane("r", f"task #{stopped['id']} ended replan: spec_bug")
    assert store.lane_paused_on("r", stopped["id"]) == 1
    assert store.lane_paused_on("r", other["id"]) == 0
    stops.dismiss(store, store.get_task(other["id"]), via="cli")
    assert store.lanes()[0]["paused"] == 1  # a pause on another task stays
    stops.dismiss(store, store.get_task(stopped["id"]), via="cli")
    assert store.lanes()[0]["paused"] == 0
    assert store.lane_paused_on("r", stopped["id"]) == 0


def test_an_ending_that_paused_its_lane_says_what_waits(store):
    from locks import hooks

    task = store.create_task("wrong task", status="replan", source="locks", repo="r", cwd="/x", session_id="s")
    store.add_event(task["id"], "finished", status="replan",
                    stop=stops.Stop("spec", "the task is wrong", by="worker").data())
    store.create_task("next", status="queued", source="locks", repo="r", cwd="/x")
    store.pause_lane("r", f"task #{task['id']} ended replan: wrong")
    text, _ = hooks.endings(store, "s")
    assert "its lane is paused on it: 1 queued task(s) wait" in text
    assert "locks lanes --unpause r" in text


def test_a_worktree_that_cannot_be_made_stops_the_task_resumable_instead_of_leaving_it_running(git_repo, store,
                                                                                               monkeypatch):
    """After a restart, `isolate.prepare` raised before the first attempt and the task stayed
    `running` with the pid of a process that was gone: nothing but the sweep freed it."""
    from locks import isolate

    def refuse(*args, **kwargs):
        raise isolate.IsolationError("git worktree add -b locks/task-1 /w HEAD failed: fatal: '/w' is a missing "
                                     "but already registered worktree;\nuse 'add -f' to override")

    monkeypatch.setattr(isolate, "prepare", refuse)
    script = Script()
    report = run("fix it", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    task = store.get_task(report.task_id)
    assert not script.specs and report.status == "blocked"
    assert task["status"] == "blocked" and task["pid"] is None
    stop = store.last_event(report.task_id, "finished")["data"]["stop"]
    assert stop["cause"] == "environment" and "\n" not in stop["reason"]
    assert "already registered worktree; use 'add -f' to override" in stop["reason"]
    assert stops.next_step(report.task_id, stops.read(stop)) == f"locks resume {report.task_id}"
    assert f"locks resume {report.task_id}" in report.text()


def test_a_stopped_task_whose_kept_work_landed_leaves_the_board_from_the_hooks(store: Store, git_repo, tmp_path,
                                                                                monkeypatch):
    from locks import hooks

    # A death is resumed on its own with autowork (`dispatch.recover`): here only the landing is read.
    monkeypatch.setenv("LOCKS_AUTOWORK", "off")

    from .conftest import git

    def stopped(text: str, branch_file: str) -> dict:
        task = store.create_task(text, status="running", source="locks", cwd=str(git_repo), repo="r")
        stops.record(store, task["id"], stops.Stop("died", "its process went away"))
        git(git_repo, "checkout", "-q", "-b", f"locks/task-{task['id']}")
        (git_repo / branch_file).write_text(f"{text}\n")
        git(git_repo, "add", "-A")
        git(git_repo, "commit", "-q", "-m", text)
        git(git_repo, "checkout", "-q", "main")
        return task

    merged = stopped("add the export", "export.py")
    squashed = stopped("add the import", "import.py")
    open_work = stopped("add the report", "report.py")
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merge", f"locks/task-{merged['id']}")
    git(git_repo, "merge", "-q", "--squash", f"locks/task-{squashed['id']}")
    git(git_repo, "commit", "-q", "-m", "add the import")
    # A GET never sweeps: the board reads it as it is until a hook runs.
    assert store.get_task(merged["id"])["status"] == "interrupted"
    hooks.session_start({"session_id": "s", "cwd": str(git_repo), "source": "startup"}, store, tmp_path)
    for task, how in ((merged, "merged"), (squashed, "squash-merged")):
        after = store.get_task(task["id"])
        stop = stops.of(store, after)
        assert after["status"] == "dismissed" and stop.cause == "landed" and stop.who == "locks"
        assert stop.reason == f"its change is already in main ({how})"
        assert stops.next_step(task["id"], stop) is None
        assert {e["data"]["via"] for e in store.events_of(task["id"], "reported")} == {"landed"}
    assert store.get_task(open_work["id"])["status"] == "interrupted"
    # The Stop hook sweeps too, and a second sweep finds nothing new.
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merge", f"locks/task-{open_work['id']}")
    hooks.stop({"session_id": "s", "cwd": str(git_repo)}, store)
    assert stops.of(store, store.get_task(open_work["id"])).cause == "landed"
    assert stops.landed(store, git_repo) == []


def test_a_death_whose_work_landed_is_never_resumed_on_its_own_at_session_start(store: Store, git_repo, tmp_path,
                                                                                 monkeypatch):
    """SessionStart resumed the deaths (`dispatch.recover`) before it read what landed: a task whose
    kept work was already merged got a `locks resume --unattended` and was then dismissed as landed."""
    from locks import dispatch, hooks

    from .conftest import git

    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    resumed: list[int] = []
    monkeypatch.setattr(dispatch, "_start_resume", lambda task, *a, **kw: resumed.append(task["id"]) or True)

    def died(text: str, name: str) -> dict:
        task = store.create_task(text, status="interrupted", source="locks", cwd=str(git_repo), repo="r")
        stops.record(store, task["id"], stops.Stop("died", "its process went away"))
        git(git_repo, "checkout", "-q", "-b", f"locks/task-{task['id']}")
        (git_repo / name).write_text(f"{text}\n")
        git(git_repo, "add", "-A")
        git(git_repo, "commit", "-q", "-m", text)
        git(git_repo, "checkout", "-q", "main")
        return task

    landed = died("add the export", "export.py")
    open_work = died("add the report", "report.py")
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merge", f"locks/task-{landed['id']}")
    hooks.session_start({"session_id": "s", "cwd": str(git_repo), "source": "startup"}, store, tmp_path)
    assert stops.of(store, store.get_task(landed["id"])).cause == "landed"
    assert resumed == [open_work["id"]]


@pytest.mark.parametrize("cause", ["base_unknown", "same_failures"])
def test_a_stop_that_waits_on_a_comparison_the_orchestrator_makes_resumes_with_a_plain_line(cause):
    stop = stops.Stop(cause, "x")
    assert stop.status == "blocked"
    assert stop.who == "locks"
    assert stops.next_step(7, stop) == "locks resume 7"
    assert stops.what_to_do(stop) != "read the attempts"
    assert not stops.for_person(stop)
    assert cause not in stops.PERSON_ONLY


def test_a_paused_lane_holding_work_the_session_queued_is_told_once_per_pause(store):
    """A workflow run waited behind a lane a replan had paused, and the session that
    queued it was never told: the ending came before the run's step was queued."""
    from locks import hooks

    stopped = store.create_task("wrong task", status="replan", source="locks", repo="r", cwd="/x")
    store.pause_lane("r", f"task #{stopped['id']}: its process exited unexpectedly")
    assert hooks._paused_lanes(store, "s") is None  # nothing of this session waits
    store.create_task("step", status="queued", source="locks", repo="r", cwd="/x", session_id="s")
    assert store.lane_paused_on("r", stopped["id"]) == 1  # the colon form is a pause on it too
    text = hooks._paused_lanes(store, "s")
    assert "r: 1 queued task(s) of yours wait" in text and "locks lanes --unpause" in text
    assert hooks._paused_lanes(store, "s") is None  # told once
    store.pause_lane("r", f"task #{stopped['id']}: its process exited unexpectedly")
    assert hooks._paused_lanes(store, "s") is None  # the sweep pausing it again is no new pause
    store.pause_lane("r", f"task #{stopped['id']} ended replan: again")
    assert hooks._paused_lanes(store, "s") is not None  # a new pause is told again
    store.pause_lane("r", f"task #{stopped['id']} ended replan: once more")
    assert hooks._paused_lanes(store, "s", said=[stopped["id"]]) is None  # its ending says it
    # The route the mod wakes an idle session by: `locks endings`, with nothing ended.
    store.pause_lane("r", f"task #{stopped['id']} ended replan: last")
    text, ids = hooks.endings(store, "s", claim="mod")
    assert ids == [] and "nothing in it starts" in text
    assert hooks.endings(store, "s", claim="mod") == (None, [])


def test_a_step_whose_run_changed_a_spec_is_not_told_the_specs_are_behind(store):
    from locks import hooks

    run_id = 7
    spec = store.create_task("specs", status="done", source="locks", repo="r", cwd="/x", session_id="s")
    store._conn.execute("UPDATE tasks SET workflow_run = ? WHERE id = ?", (run_id, spec["id"]))
    store.add_event(spec["id"], "finished", status="done", changed=["openspec/specs/a/spec.md"])
    assert hooks._run_touched_specs(store, {**spec, "workflow_run": run_id})
    assert not hooks._run_touched_specs(store, {**spec, "workflow_run": None})
