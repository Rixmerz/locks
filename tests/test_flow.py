from __future__ import annotations

from datetime import UTC, datetime, timedelta

from locks import dispatch, flow, workflows
from locks.escalate import Failure
from locks.launch import WorkerResult
from locks.orchestrate import Options, run
from locks.store import Store

from .test_orchestrate import bad, kind, ok


def _runner(results):
    results = list(results)

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=lambda spec: results.pop(0),
                   classifier=kind("implement"), adapters=[], **kw)

    return runner


def test_a_lane_runs_in_order_and_pauses_on_the_first_failure(git_repo, store: Store):
    key = str(git_repo.resolve())
    for text in ("first", "second", "third"):
        store.enqueue(text, repo=key, cwd=str(git_repo))
    report = flow.work(store, runner=_runner([ok(), bad(Failure.SPEC_BUG, "cannot be done")]))
    assert [r.status for r in report.ran] == ["done", "replan"]
    assert "paused" in report.stopped_because or "nothing runnable" in report.stopped_because
    lane = store.lanes()[0]
    assert lane["paused"] and "replan" in lane["reason"] and lane["queued"] == 1
    first = store.list_tasks(status=["done"])[0]
    assert first["title"] == "first" and first["source"] == "locks" and first["dispatched"] == 1
    assert next(m["role"] for m in store.messages(first["id"])) == "user"  # the queued text kept its id

    store.unpause_lane(key)
    again = flow.work(store, runner=_runner([ok()]))
    assert [r.status for r in again.ran] == ["done"] and not store.lanes()[0]["paused"]


def test_a_task_a_person_cancelled_does_not_pause_the_queue(git_repo, store: Store):
    key = str(git_repo.resolve())
    for text in ("first", "second"):
        store.enqueue(text, repo=key, cwd=str(git_repo))

    def cancel_first(spec):
        store.request_cancel(store.list_tasks(status=["running"])[0]["id"], via="cli")
        return bad(Failure.CODE_BUG, "half done")

    results = [cancel_first, lambda spec: ok()]

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=results.pop(0),
                   classifier=kind("implement"), adapters=[], **kw)

    report = flow.work(store, runner=runner)
    assert [r.status for r in report.ran] == ["cancelled", "done"]
    assert not any(lane["paused"] for lane in store.lanes())


def test_the_unattended_limit_and_a_vanished_directory(git_repo, store: Store, tmp_path):
    key = str(git_repo.resolve())
    for i in range(3):
        store.enqueue(f"t{i}", repo=key, cwd=str(git_repo))
    report = flow.work(store, max_tasks=2, runner=_runner([ok(), ok()]))
    assert len(report.ran) == 2 and "unattended limit" in report.stopped_because and report.at_limit
    assert not flow.work(store, max_tasks=2, runner=_runner([ok()])).at_limit  # nothing left queued

    gone = store.enqueue("x", repo="elsewhere", cwd=str(tmp_path / "deleted"))
    flow.work(store, repo="elsewhere", runner=_runner([]))
    assert store.get_task(gone["id"])["status"] == "blocked"
    stop = store.last_event(gone["id"], "finished")["data"]["stop"]
    assert stop["cause"] == "missing_dir" and str(tmp_path / "deleted") in stop["reason"]
    assert any(lane["repo"] == "elsewhere" and lane["paused"] for lane in store.lanes())


def _never(task, ahead):
    raise AssertionError("nothing should be decided here")


def test_a_busy_lane_waits_and_a_claim_happens_once(store: Store):
    a = store.enqueue("a", repo="r", cwd="/x")
    b = store.enqueue("b", repo="r", cwd="/x")
    other = store.enqueue("c", repo="other", cwd="/x")
    ids = lambda **kw: [t["id"] for t in flow.runnable(store, parallel=False, decide=_never, **kw)]  # noqa: E731
    # Lanes are served in the order their oldest work was queued: r's a before other's c.
    assert ids(repo=None) == [a["id"], other["id"]]
    assert store.claim(a["id"]) and not store.claim(a["id"])
    assert ids(repo="r") == []  # serial: a is running from the dispatcher
    assert ids(repo=None) == [other["id"]]
    store.update_task(a["id"], status="done")
    assert ids(repo="r") == [b["id"]]


def test_haiku_lets_independent_work_start_beside_running_work(store: Store):
    running = store.enqueue("rewrite the importer", repo="r", cwd="/x")
    store.claim(running["id"])
    same = store.enqueue("add a flag to the importer", repo="r", cwd="/x")
    docs = store.enqueue("fix a typo in the README", repo="r", cwd="/x")
    asked = []

    def decide(task, ahead):
        asked.append((task["id"], [t["id"] for t in ahead]))
        if task["id"] == same["id"]:
            return dispatch.Decision(False, "touches the importer #1 rewrites", 0.002)
        return dispatch.Decision(True, "only the README", 0.001)

    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=decide)] == [docs["id"]]
    # Each one was judged against everything running and queued ahead of it.
    assert asked == [(same["id"], [running["id"]]), (docs["id"], [running["id"], same["id"]])]
    waiting = store.get_task(same["id"])
    assert waiting["parallel"] == 0 and "importer" in waiting["parallel_reason"] and waiting["cost_usd"] == 0.002
    # Judged once: the next look asks nothing.
    store.claim(docs["id"])
    assert flow.runnable(store, "r", parallel=True, decide=_never) == []
    # Once the lane is idle, the task that waited is first and starts.
    for t in (running, docs):
        store.update_task(t["id"], status="done")
    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=_never)] == [same["id"]]


def test_a_held_back_task_says_what_it_waits_for_wherever_it_is_shown(capsys, store: Store):
    from locks import cli
    from locks.ui import api

    first = store.enqueue("rewrite the importer", repo="r", cwd="/x")
    store.claim(first["id"])
    second = store.enqueue("refactor the parser", repo="r", cwd="/x")
    held = store.enqueue("add a flag to the importer", repo="r", cwd="/x")

    def decide(task, ahead):
        if task["id"] == held["id"]:
            return dispatch.Decision(False, f"it changes the importer #{first['id']} rewrites")
        return dispatch.Decision(False, "the model answered outside the schema")

    assert flow.runnable(store, "r", parallel=True, decide=decide) == []
    # The decision is an event when it is made, with the task it waits for: the one
    # its reason names, else the first ahead of it.
    decided = store.last_event(held["id"], "dispatch_decided")["data"]
    assert decided == {"parallel": False, "reason": f"it changes the importer #{first['id']} rewrites",
                       "behind": first["id"], "ahead": [first["id"], second["id"]], "by": "haiku", "seen": {}}
    assert store.last_event(second["id"], "dispatch_decided")["data"]["behind"] == first["id"]
    line = f"waits for #{first['id']}: it changes the importer #{first['id']} rewrites"
    assert dispatch.waiting(store, store.get_task(held["id"])) == line

    assert cli.main(["queue", "list", "--all"]) == 0
    out = capsys.readouterr().out
    assert line in out and out.index("add a flag") < out.index(line)
    assert cli.main(["tasks", "--all"]) == 0
    assert line in capsys.readouterr().out
    assert cli.main(["show", str(held["id"])]) == 0
    assert line in capsys.readouterr().out
    assert cli.main(["show", str(first["id"])]) == 0
    assert "waits" not in capsys.readouterr().out  # a running task waits for nothing

    lane = next(q for q in api.board(store)["queued"] if q["repo"] == "r")
    assert {t["id"]: t["waits"] for t in lane["tasks"]}[held["id"]] == line
    assert api.task_detail(store, held["id"])["task"]["waits"] == line

    # A decision kept before the event existed names the first task now ahead of it.
    old = store.enqueue("older", repo="r", cwd="/x")
    store.set_parallel(old["id"], False, "may conflict")
    assert dispatch.waiting(store, store.get_task(old["id"])) == f"waits for #{first['id']}: may conflict"
    store.set_parallel(old["id"], True, "independent")
    assert dispatch.waiting(store, store.get_task(old["id"])) is None


def test_what_a_held_back_task_waits_for_moves_on_with_its_lane(capsys, store: Store):
    from locks import cli

    first = store.enqueue("rewrite the importer", repo="r", cwd="/x")
    store.claim(first["id"])
    second = store.enqueue("refactor the parser", repo="r", cwd="/x")
    held = store.enqueue("add a flag to the importer", repo="r", cwd="/x")
    assert flow.runnable(store, "r", parallel=True,
                         decide=lambda task, ahead: dispatch.Decision(False, "both change the importer")) == []
    assert dispatch.waiting(store, store.get_task(held["id"])) == f"waits for #{first['id']}: both change the importer"

    # The task it was held behind ends and the next one starts: the decision stands, made
    # once, and the line names the task it is behind now, and says whom the reason was about.
    store.update_task(first["id"], status="done")
    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=_never)] == [second["id"]]
    store.claim(second["id"])
    assert flow.runnable(store, "r", parallel=True, decide=_never) == []
    line = f"waits for #{second['id']}: both change the importer (judged against #{first['id']})"
    assert dispatch.waiting(store, store.get_task(held["id"])) == line
    assert cli.main(["queue", "list", "--all"]) == 0
    out = capsys.readouterr().out
    assert line in out and f"waits for #{first['id']}" not in out

    # Every task it was judged against has gone: the one ahead now, and the reason says whom it was about.
    store.update_task(second["id"], status="done")
    later = store.enqueue("a typo in the readme", repo="r", cwd="/x")
    store.claim(later["id"])
    assert dispatch.waiting(store, store.get_task(held["id"])) == (
        f"waits for #{later['id']}: both change the importer (judged against #{first['id']})")
    assert cli.main(["show", str(held["id"])]) == 0
    assert f"waits for #{later['id']}: " in capsys.readouterr().out

    # Nothing ahead of it any more: it waits for no task, and starts next.
    store.update_task(later["id"], status="done")
    assert dispatch.waiting(store, store.get_task(held["id"])) is None
    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=_never)] == [held["id"]]


def test_a_reason_about_a_task_that_ended_says_it_when_another_one_is_still_ahead(store: Store):
    first = store.enqueue("rewrite the importer", repo="r", cwd="/x")
    second = store.enqueue("refactor the parser", repo="r", cwd="/x")
    store.claim(first["id"])
    store.claim(second["id"])
    held = store.enqueue("add a flag to the importer", repo="r", cwd="/x")
    reason = f"it changes the importer #{first['id']} rewrites"
    assert flow.runnable(store, "r", parallel=True, decide=lambda task, ahead: dispatch.Decision(False, reason)) == []
    assert dispatch.waiting(store, store.get_task(held["id"])) == f"waits for #{first['id']}: {reason}"

    # #1 ended and #2, which it was judged against too, still runs: the line names #2,
    # and the reason about #1 does not read as one about #2.
    store.update_task(first["id"], status="done")
    assert dispatch.waiting(store, store.get_task(held["id"])) == (
        f"waits for #{second['id']}: {reason} (judged against #{first['id']})")


def test_a_model_that_did_not_answer_reads_as_waiting_once(store: Store):
    running = store.enqueue("a", repo="r", cwd="/x")
    store.claim(running["id"])
    held = store.enqueue("b", repo="r", cwd="/x")
    flow.runnable(store, "r", parallel=True,
                  decide=lambda task, ahead: dispatch.Decision(False, "waits its turn: claude not found"))
    assert dispatch.waiting(store, store.get_task(held["id"])) == f"waits for #{running['id']}: claude not found"


def test_surfaces_that_clash_name_the_task_they_wait_for(store: Store):
    running = store.enqueue("a", repo="r", cwd="/x", options={"surfaces": ["src/**"]})
    store.claim(running["id"])
    clash = store.enqueue("b", repo="r", cwd="/x", options={"surfaces": ["src/api/**"]})
    assert flow.runnable(store, "r", parallel=True, decide=_never) == []
    decided = store.last_event(clash["id"], "dispatch_decided")["data"]
    assert decided["by"] == "surfaces" and decided["behind"] == running["id"] and not decided["parallel"]
    assert dispatch.waiting(store, store.get_task(clash["id"])).startswith(f"waits for #{running['id']}: ")

def _writes_ahead(store: Store, text: str = "add migration 3 to the schema") -> dict:
    running = store.enqueue(text, repo="r", cwd="/x", options={"kind": "feature"})
    store.claim(running["id"])
    return running


def test_reading_work_runs_beside_writing_work_without_asking_haiku(store: Store):
    """A review, refute or recheck step of a workflow changes no file and holds no
    checkout: Haiku read the goal embedded in its text ("adds migration 3") as what
    the step does, and queued it behind the build for an hour."""
    _writes_ahead(store)
    reads = {
        "review": store.enqueue("Review: add migration 3 to the schema", repo="r", cwd="/x",
                                options={"kind": "review-routine"}),
        "refute": store.enqueue("Refute: add migration 3 to the schema", repo="r", cwd="/x",
                                options={"kind": "review-critical", "ledger": {"role": "refute"}}),
        "explore": store.enqueue("Where is the schema migrated?", repo="r", cwd="/x", options={"kind": "explore"}),
        "plan": store.enqueue("Plan migration 4", repo="r", cwd="/x", options={"kind": "plan"}),
    }

    def no(task, ahead):
        raise AssertionError(f"Haiku must not be asked about #{task['id']}: it reads only")

    picked = flow.runnable(store, "r", parallel=True, decide=no)
    # Within the machine's level (3 here): the build plus two readers; the rest wait for a slot, undecided.
    assert [t["id"] for t in picked] == [reads["review"]["id"], reads["refute"]["id"]]
    for name in ("review", "refute"):
        row = store.get_task(reads[name]["id"])
        assert row["parallel"] == 1 and row["parallel_reason"] == "reads only: runs beside writing work"
    assert store.get_task(reads["explore"]["id"])["parallel"] is None


def test_a_text_that_declares_itself_read_only_reads_unless_it_is_a_build(store: Store):
    _writes_ahead(store)
    audit = store.enqueue("Read-only audit: list the call sites of the importer. Change nothing.",
                          repo="r", cwd="/x")
    tagged = store.enqueue("#feature read-only: add the flag", repo="r", cwd="/x")
    built = store.enqueue("Read-only audit of the importer", repo="r", cwd="/x", options={"kind": "feature"})
    asked = []

    def decide(task, ahead):
        asked.append(task["id"])
        return dispatch.Decision(False, "writes", 0.0)

    picked = flow.runnable(store, "r", parallel=True, decide=decide)
    assert [t["id"] for t in picked] == [audit["id"]]
    assert "reads only" in store.get_task(audit["id"])["parallel_reason"]
    # A kind that builds writes whatever its text says, by tag or by option: judged as ever.
    assert asked == [tagged["id"], built["id"]]


def test_a_task_is_read_by_its_kind_from_the_options_or_from_the_column():
    assert flow.reads_only({"options": '{"kind": "plan"}', "body": "x"})
    assert flow.reads_only({"options": "not json", "kind": "explore", "body": "x"})
    assert not flow.reads_only({"options": "not json", "kind": "feature", "body": "x"})
    assert not flow.reads_only({"options": None, "body": "add migration 3", "title": "t"})
    assert flow.reads_only({"options": None, "body": None, "title": "Read-only audit: list the importers"})


def test_the_serial_setting_still_holds_reading_work_behind_running_work(store: Store):
    _writes_ahead(store)
    store.enqueue("Review the change", repo="r", cwd="/x", options={"kind": "review-routine"})
    assert flow.runnable(store, "r", parallel=False, decide=_never) == []


def test_a_decision_made_before_the_rule_does_not_hold_a_reader(store: Store):
    """Steps queued behind a build with Haiku's "no" already recorded start now."""
    _writes_ahead(store)
    held = store.enqueue("Recheck the fix", repo="r", cwd="/x", options={"kind": "review-routine"})
    store.set_parallel(held["id"], False, "migration 4 depends on migration 3's numbering")
    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=_never)] == [held["id"]]
    row = store.get_task(held["id"])
    assert row["parallel"] == 1 and row["parallel_reason"] == "reads only: runs beside writing work"


def test_a_writer_held_behind_a_build_starts_when_only_readers_are_left(store: Store):
    """Readers run for an hour beside builds: a writer a "no" held behind the build
    must not go on waiting for a reader that writes nothing."""
    build = _writes_ahead(store)
    review = store.enqueue("Review the change", repo="r", cwd="/x", options={"kind": "review-routine"})
    store.claim(review["id"])
    held = store.enqueue("add a flag to the importer", repo="r", cwd="/x", options={"kind": "implement"})
    store.set_parallel(held["id"], False, "touches the schema the build changes")
    later = store.enqueue("rename the importer", repo="r", cwd="/x", options={"kind": "implement"})
    asked = []

    def decide(task, ahead):
        asked.append((task["id"], [t["id"] for t in ahead]))
        return dispatch.Decision(False, "renames what the flag uses", 0.0)

    # The build still runs: both wait, and the next writer is judged against writers alone.
    assert flow.runnable(store, "r", parallel=True, decide=decide) == []
    assert asked == [(later["id"], [build["id"], held["id"]])]
    store.update_task(build["id"], status="done")
    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=_never)] == [held["id"]]
    row = store.get_task(held["id"])
    assert row["parallel"] == 1 and row["parallel_reason"] == "nothing that writes runs or waits ahead of it"


def test_a_text_the_run_classifies_as_a_build_writes_whatever_its_words(store: Store):
    """With no kind of its own a task gets the one `classify` would give it without the
    model: asking for a change makes it `implement`, and a text no rule reads is one too."""
    audit = "Read-only audit: list the importers and fix nothing"
    unruled = "the importer drops rows when the batch is empty"
    assert not flow.reads_only({"options": None, "body": audit})
    assert not flow.reads_only({"options": None, "body": unruled})
    assert flow.reads_only({"options": None, "body": "Read-only audit: list the call sites of the importer"})
    _writes_ahead(store)
    asked = []

    def decide(task, ahead):
        asked.append(task["id"])
        return dispatch.Decision(False, "writes", 0.0)

    first, second = (store.enqueue(t, repo="r", cwd="/x") for t in (audit, unruled))
    assert flow.runnable(store, "r", parallel=True, decide=decide) == []
    assert asked == [first["id"], second["id"]]


def test_a_writing_task_is_never_judged_against_reading_work(store: Store):
    build = _writes_ahead(store)
    review = store.enqueue("Review the change", repo="r", cwd="/x", options={"kind": "review-routine"})
    write = store.enqueue("add a flag to the importer", repo="r", cwd="/x", options={"kind": "implement"})
    asked = []

    def decide(task, ahead):
        asked.append((task["id"], [t["id"] for t in ahead]))
        return dispatch.Decision(False, "touches the schema #1 changes", 0.0)

    picked = flow.runnable(store, "r", parallel=True, decide=decide)
    assert [t["id"] for t in picked] == [review["id"]]
    # The reader runs ahead of it, and is no conflict: only the build is put to the model.
    assert asked == [(write["id"], [build["id"]])]
    assert store.get_task(write["id"])["parallel"] == 0


def test_behind_nothing_but_reading_work_a_writing_task_has_nothing_to_conflict_with(store: Store):
    reading = store.enqueue("Review the change", repo="r", cwd="/x", options={"kind": "review-routine"})
    store.claim(reading["id"])
    write = store.enqueue("add a flag to the importer", repo="r", cwd="/x", options={"kind": "implement"})
    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=_never)] == [write["id"]]
    row = store.get_task(write["id"])
    assert row["parallel"] == 1 and row["parallel_reason"] == "nothing that writes runs or waits ahead of it"


def test_never_more_than_the_machine_s_level(store: Store):
    for i in range(4):
        store.enqueue(f"read file {i}", repo="r", cwd="/x")
    yes = lambda task, ahead: dispatch.Decision(True, "read only")  # noqa: E731
    assert len(flow.runnable(store, "r", parallel=True, decide=yes, level=2)) == 2
    # Without one given, the level this machine keeps (three, on the suite's fake machine).
    assert len(flow.runnable(store, "r", parallel=True, decide=yes)) == 3


def test_the_dispatcher_runs_parallel_work_and_waits_for_it(git_repo, store: Store):
    """Handles that finish on the dispatcher's own clock: two start together,
    the third waits for a slot, and the report holds all three."""
    key = str(git_repo.resolve())
    for text in ("a", "b", "c"):
        store.enqueue(text, repo=key, cwd=str(git_repo))
    live, ticks = [], []

    class Slow:
        def __init__(self, task_id):
            self.task_id, self.left = task_id, 2

        def poll(self):
            self.left -= 1
            if self.left > 0:
                return None
            store.update_task(self.task_id, status="done")
            return "done"

    def start(task, where):
        live.append(task["id"])
        return Slow(task["id"])

    report = flow.work(store, parallel=True, start=start, pause=lambda: ticks.append(1),
                       decide=lambda task, ahead: dispatch.Decision(True, "independent"))
    assert sorted(r.task_id for r in report.ran) == sorted(live) and len(live) == 3
    assert {r.status for r in report.ran} == {"done"} and "nothing runnable" in report.stopped_because
    assert ticks  # it waited on work in flight instead of returning early


def test_queued_options_reach_the_run():
    # A budget an older locks stored is left where it is: nothing reads it.
    task = {"options": '{"budget_usd": "x", "verify": "pytest", "kind": "docs", "start": "sonnet/high"}'}
    options = flow.queued_options(task, Options(max_attempts=3))
    assert (options.verify, options.kind, options.start.label, options.max_attempts) == (
        "pytest", "docs", "sonnet/high", 3)
    assert not hasattr(options, "budget_usd")
    assert flow.queued_options({"options": "{}"}, Options()).verify is None


def test_the_sweep_marks_dead_runs_and_abandoned_prompts(store: Store):
    dead = store.create_task("dead", status="running", source="locks", pid=999999, repo="r", dispatched=1)
    live = store.create_task("live", status="running", source="locks", pid=1, repo="r")
    prompt = store.create_task("old prompt", status="running", source="hook", session_id="s")
    child = store.create_task("child", status="running", source="delegation", session_id="s")
    later = datetime.now(UTC) + timedelta(hours=13)
    swept = flow.sweep(store, now=later, is_alive=lambda pid: pid == 1)
    assert set(swept) == {dead["id"], prompt["id"]}
    assert store.get_task(live["id"])["status"] == "running"
    assert store.get_task(child["id"])["status"] == "running"
    assert store.lanes()[0]["paused"]
    assert "999999" in store.get_task(dead["id"])["result"]
    assert store.get_task(prompt["id"])["result"] is None  # a prompt keeps its own answer
    assert store.last_event(prompt["id"], "finished")["data"]["stop"]["cause"] == "turn"  # a turn, not work to resume
    assert flow.alive(None) is False and flow.alive(1) is True and flow.alive(999999) is False
    # A subagent whose ending never reached locks goes after a day, as nothing to resume.
    gone = flow.sweep(store, now=datetime.now(UTC) + timedelta(hours=25), is_alive=lambda pid: pid == 1)
    assert gone == [child["id"]] and store.get_task(child["id"])["status"] != "running"
    stop = store.last_event(child["id"], "finished")["data"]["stop"]
    assert stop["cause"] == "turn" and "subagent" in stop["reason"]


def test_pinned_runs_never_teach_the_router(git_repo, store: Store):
    from locks.matrix import Cell

    for _ in range(3):
        run("x", git_repo, store, Options(start=Cell("opus", "max")), registry={},
            launcher=lambda spec: WorkerResult(True, None, "ok", "1 passed"), classifier=kind("implement"),
            adapters=[])
    assert store.landings("implement") == []
    assert store.list_tasks()[0]["pinned"] == 1


def test_prune_deletes_only_the_branches_whose_work_landed(store, git_repo, monkeypatch, capsys):
    """Merged, squash-merged and artifact-only branches go, each kept on its task's
    events with its tip; work not landed, a task that may resume, and a branch
    checked out in a worktree stay."""
    from locks import cli, flow, isolate

    from .conftest import git

    def branch(task_id, files, status="done"):
        task = store.create_task(f"task {task_id}", status="running", source="locks", cwd=str(git_repo))
        store.update_task(task["id"], status=status)
        name = isolate.branch_for(task["id"])
        git(git_repo, "checkout", "-qb", name)
        for path, text in files.items():
            (git_repo / path).parent.mkdir(parents=True, exist_ok=True)
            (git_repo / path).write_text(text)
        git(git_repo, "add", "-A")
        git(git_repo, "commit", "-qm", name)
        git(git_repo, "checkout", "-q", "main")
        return task["id"], name

    merged = branch(1, {"a.txt": "a"})
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merge", merged[1])
    squashed = branch(2, {"b.txt": "b"})
    (git_repo / "b.txt").write_text("b")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "squash")
    artifacts = branch(3, {".playwright-mcp/console.log": "x"})
    pending = branch(4, {"c.txt": "c"})
    blocked = branch(5, {"e.txt": "e"}, status="blocked")
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merge", blocked[1])
    checked_out = branch(6, {"d.txt": "d"})
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merge", checked_out[1])
    git(git_repo, "worktree", "add", "-q", str(git_repo.parent / "wt"), checked_out[1])

    assert {x["task"] for x in flow.prune(store, dry_run=True)} == {merged[0], squashed[0], artifacts[0]}
    assert len(isolate.kept_branches(git_repo)) == 6  # a dry run deletes nothing

    monkeypatch.setenv("LOCKS_PRUNE", "off")
    assert flow.prune(store) == []
    assert cli.main(["prune"]) == 0
    assert "pruning is off" in capsys.readouterr().out

    monkeypatch.setenv("LOCKS_PRUNE", "on")
    gone = {x["task"]: x["why"] for x in flow.prune(store, [git_repo])}
    assert gone == {merged[0]: "merged", squashed[0]: "squash-merged", artifacts[0]: "artifacts only"}
    assert set(isolate.kept_branches(git_repo)) == {pending[0], blocked[0], checked_out[0]}
    event = store.last_event(merged[0], "branch_pruned")["data"]
    assert event["branch"] == merged[1]
    assert git(git_repo, "cat-file", "-t", event["tip"]) == "commit"  # still there to bring back
    assert cli.main(["prune", "--repo", str(git_repo)]) == 0
    assert "no branch locks kept has landed" in capsys.readouterr().out


def test_an_investigator_that_stops_leaves_the_person_s_lane_open(git_repo, store: Store):
    key = str(git_repo.resolve())
    store.enqueue("investigate", repo=key, cwd=str(git_repo), options={"investigation": "inv-1", "kind": "explore"})
    store.enqueue("the person's own task", repo=key, cwd=str(git_repo))
    report = flow.work(store, runner=_runner([bad(Failure.SPEC_BUG, "cannot read it"), ok()]))
    assert [r.status for r in report.ran] == ["replan", "done"]
    assert not any(lane["paused"] for lane in store.lanes())


def test_prune_deletes_a_finished_copy_of_work_another_branch_keeps(store, git_repo):
    from locks import flow, isolate

    from .conftest import git

    ids = []
    for _ in range(3):
        task = store.create_task("rename the field", status="done", source="locks", cwd=str(git_repo))
        ids.append(task["id"])
        git(git_repo, "checkout", "-qb", isolate.branch_for(task["id"]))
        (git_repo / "field.py").write_text("renamed\n")
        git(git_repo, "add", "-A")
        git(git_repo, "commit", "-qm", f"try {task['id']}")
        git(git_repo, "checkout", "-q", "main")
    store.update_task(ids[2], status="blocked")  # the newest may resume: it stays
    pruned = flow.prune(store, [git_repo])
    # A copy goes only for a twin that passed: the blocked one may resume, fail and drop its branch.
    assert [(p["task"], p["why"]) for p in pruned] == [(ids[0], f"the same work as locks/task-{ids[1]}")]
    assert list(isolate.kept_branches(git_repo)) == [ids[1], ids[2]]


def test_prune_leaves_the_branches_of_a_run_that_is_still_going(store, git_repo):
    from locks import flow, isolate

    from .conftest import git
    from .test_workflows import TWO, _save

    _save(TWO, git_repo)
    started = workflows.start(store, "two", git_repo, {"goal": "the export"})
    build = store.run_tasks(started["id"])[0]
    store.update_task(build["id"], status="done")
    git(git_repo, "checkout", "-qb", isolate.branch_for(build["id"]))
    (git_repo / "built.py").write_text("x\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "built")
    git(git_repo, "checkout", "-q", "main")
    git(git_repo, "merge", "-q", "--ff-only", isolate.branch_for(build["id"]))
    # Merged, so lossless to delete; but the run may still start a step from it or join it.
    assert flow.prune(store, [git_repo]) == []
    workflows.cancel(store, started["id"])
    assert [p["task"] for p in flow.prune(store, [git_repo])] == [build["id"]]


def test_a_task_asked_again_points_at_the_one_that_keeps_its_work(git_repo, store):
    from locks.escalate import Failure as F

    from .test_orchestrate import Script

    first = run("migrate the totals to cents", git_repo, store, registry={}, adapters=[],
                launcher=Script(WorkerResult(False, F.PERMISSION, "refused", denied=("Bash(x)",)), write="m.py"),
                classifier=kind("implement"))
    said = []
    run("migrate the totals to cents", git_repo, store, registry={}, adapters=[], launcher=Script(ok()),
        classifier=kind("implement"), progress=said.append)
    assert any(f"task #{first.task_id} asked the same and keeps its work" in s and f"locks resume {first.task_id}" in s
               for s in said)


def test_the_unattended_limit_ends_the_dispatcher_even_when_every_worker_was_busy(store: Store):
    """The pass that reached the limit found every worker busy and the lane with work left:
    once its tasks end, the dispatcher reports the limit instead of waiting on."""
    for i in range(4):
        store.enqueue(f"read file {i}", repo="r", cwd="/")

    class Ends:
        def __init__(self, task_id):
            self.task_id = task_id

        def poll(self):
            store.update_task(self.task_id, status="done")
            return "done"

    pauses = []

    def pause():
        pauses.append(1)
        if len(pauses) > 50:
            raise RuntimeError("the dispatcher never returned")

    report = flow.work(store, max_tasks=1, parallel=True, start=lambda task, where: Ends(task["id"]), pause=pause,
                       decide=lambda task, ahead: dispatch.Decision(True, "independent"))
    assert len(report.ran) == 1 and "unattended limit" in report.stopped_because


def test_a_run_of_its_own_that_stopped_is_resumed_by_the_dispatcher_not_a_later_turn(store: Store, monkeypatch):
    """A worker killed under the dispatcher left its workflow waiting for some session's next
    turn; the dispatcher that saw it end now runs `dispatch.recover` at once, and only then."""
    for i in range(2):
        store.enqueue(f"read file {i}", repo="r", cwd="/")
    recovered = []
    monkeypatch.setattr(dispatch, "recover", lambda *a, **k: recovered.append(1) or [])

    class Ends:
        def __init__(self, task_id, status):
            self.task_id, self.status = task_id, status

        def poll(self):
            store.update_task(self.task_id, status=self.status)
            return self.status

    statuses = iter(["done", "interrupted"])
    flow.work(store, max_tasks=1, parallel=True, start=lambda task, where: Ends(task["id"], next(statuses)),
              pause=lambda: None, decide=lambda task, ahead: dispatch.Decision(True, "independent"))
    assert recovered == []
    flow.work(store, max_tasks=1, parallel=True, start=lambda task, where: Ends(task["id"], next(statuses)),
              pause=lambda: None, decide=lambda task, ahead: dispatch.Decision(True, "independent"))
    assert recovered == [1]
    monkeypatch.setattr(dispatch, "recover", lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    flow._recover(store)
    assert "OSError: gone" in store.events_of(0, "recover_error")[0]["data"]["error"]


def test_another_dispatcher_s_unjudged_writing_work_borrows_no_worker(store: Store):
    """Beside its running work, another lane's writing task no decision has let start yet is
    no demand: the worker it would borrow sits idle while this lane has work to run."""
    store.claim(store.enqueue("rewrite the importer", repo="other", cwd="/x")["id"])
    for text in ("add migration 3 to the schema", "add migration 4 to the schema"):
        store.enqueue(text, repo="other", cwd="/x")
    mine = [store.enqueue(f"read file {i}", repo="mine", cwd="/x")["id"] for i in range(3)]
    yes = lambda task, ahead: dispatch.Decision(True, "independent")  # noqa: E731
    picked, shares = flow.pick(store, "mine", parallel=True, decide=yes, level=3)
    assert [t["id"] for t in picked] == mine[:2]
    assert shares["other"].started == 0 and shares["mine"].borrowed == 1
    # One its own dispatcher let start beside the running work is demand, and takes its loan.
    store.set_parallel(store.queued_in("other")[0]["id"], True, "independent")
    picked, shares = flow.pick(store, "mine", parallel=True, decide=yes, level=3)
    assert len(picked) == 1 and shares["other"].started == 1


def test_what_reads_as_a_full_disk_and_what_is_locks_s_own_error():
    import errno
    import sqlite3

    assert flow.disk_full(sqlite3.OperationalError("database or disk is full"))
    assert flow.disk_full(sqlite3.OperationalError("disk I/O error"))
    assert flow.disk_full(OSError(errno.ENOSPC, "No space left on device"))
    assert not flow.disk_full(sqlite3.OperationalError("no such table: tasks"))
    assert not flow.disk_full(OSError(errno.ENOENT, "gone"))
    assert not flow.disk_full(ValueError("disk is full"))


def test_a_full_disk_does_not_end_the_dispatcher_it_waits_and_goes_on(store: Store, monkeypatch):
    import sqlite3

    from locks import capacity
    from locks.store import home

    capacity.settle(store, home(), machine=capacity.Machine(4, None, None), ceiling=None, wanting=False)
    task = store.enqueue("read the docs", repo="r", cwd="/")
    real, failures = flow.pick, [sqlite3.OperationalError("database or disk is full")] * 2

    def pick(*a, **kw):
        if failures:
            raise failures.pop()
        return real(*a, **kw)

    monkeypatch.setattr(flow, "pick", pick)
    waits = []

    class Ends:
        def __init__(self, task_id):
            self.task_id = task_id

        def poll(self):
            store.update_task(self.task_id, status="done")
            return "done"

    report = flow.work(store, start=lambda t, where: Ends(t["id"]), pause=lambda: None, backoff=waits.append)
    assert [r.task_id for r in report.ran] == [task["id"]] and waits == list(flow.DISK_WAITS_S[:2])
    changed = store.events_of(0, "level_changed")[-1]["data"]
    assert changed["level"] == 2 and changed["reason"].startswith("halved, the disk filled up (the dispatcher "
                                                                   "waited 20s")


def test_a_disk_that_stays_full_stops_the_dispatcher_and_any_other_error_is_raised(store: Store, monkeypatch):
    import sqlite3

    import pytest

    store.enqueue("read the docs", repo="r", cwd="/")
    monkeypatch.setattr(flow, "pick", lambda *a, **kw: (_ for _ in ()).throw(
        sqlite3.OperationalError("database or disk is full")))
    waits = []
    report = flow.work(store, pause=lambda: None, backoff=waits.append)
    assert sum(waits) >= flow.DISK_GIVE_UP_S and waits[-1] == flow.DISK_WAITS_S[-1]
    assert report.stopped_because.startswith("the disk stayed full for") and not report.ran
    monkeypatch.setattr(flow, "pick", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("locks' own")))
    with pytest.raises(RuntimeError):
        flow.work(store, pause=lambda: None, backoff=waits.append)


def test_a_task_whose_process_could_not_start_for_the_disk_goes_back_to_the_queue(store: Store):
    import errno

    task = store.enqueue("read the docs", repo="r", cwd="/")
    tries = []

    class Ends:
        def __init__(self, task_id):
            self.task_id = task_id

        def poll(self):
            store.update_task(self.task_id, status="done")
            return "done"

    def start(t, where):
        tries.append(t["id"])
        if len(tries) == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return Ends(t["id"])

    report = flow.work(store, start=start, pause=lambda: None, backoff=lambda s: None)
    assert tries == [task["id"], task["id"]] and [r.status for r in report.ran] == ["done"]


def test_the_files_a_request_names_hold_it_back_never_start_it_beside(store: Store):
    from locks import flow

    api = {"id": 1, "body": "add the service in src/app/curriculum-api.service.ts", "options": "{}"}
    admin = {"id": 2, "body": "write the spec src/app/curriculum-api.service.ts for the admin", "options": "{}"}
    forum = {"id": 3, "body": "add the forum in src/forum/", "options": "{}"}
    held = flow.by_surfaces(admin, [api])
    assert held is not None and not held.parallel and "#1" in held.reason
    # names that cannot touch say nothing: the dispatcher's judgment decides, never a start
    assert flow.by_surfaces(forum, [api]) is None
    running = {"id": 4, "body": "refactor things", "options": "{}", "changed": ["src/forum/x.ts"]}
    touched = flow.by_surfaces(forum, [running])
    assert touched is not None and "already changed src/forum/x.ts" in touched.reason
    assert flow.by_surfaces({"id": 5, "body": "tidy up", "options": "{}"}, [api]) is None


def test_a_wait_judged_against_work_that_ended_is_judged_again(store: Store):
    first = store.enqueue("rewrite the importer", repo="r", cwd="/x")
    store.claim(first["id"])
    held = store.enqueue("add a flag to the importer", repo="r", cwd="/x")
    asked = []

    def decide(task, ahead):
        asked.append([t["id"] for t in ahead])
        if any(t["id"] == first["id"] for t in ahead):
            return dispatch.Decision(False, f"it changes the importer #{first['id']} rewrites")
        return dispatch.Decision(True, "the long build ahead touches other files")

    assert flow.runnable(store, "r", parallel=True, decide=decide) == []
    # The work it was judged against ended, and other work runs now: the old "wait" says
    # nothing of it, so it is judged again, against what is ahead now.
    store.update_task(first["id"], status="done")
    long = store.enqueue("build the long feature", repo="r", cwd="/x")
    store.update_task(long["id"], parallel=1)
    store.claim(long["id"])
    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=decide)] == [held["id"]]
    assert asked == [[first["id"]], [long["id"]]]
    again = store.last_event(held["id"], "dispatch_decided")["data"]
    assert again["again"] == [first["id"]] and again["ahead"] == [long["id"]]
    # A wait judged against work still ahead stands, and asks nothing.
    store.claim(held["id"])
    other = store.enqueue("rename the importer's flag", repo="r", cwd="/x")
    store.set_parallel(other["id"], False, "it changes the importer")
    store.add_event(other["id"], "dispatch_decided", parallel=False, reason="x", ahead=[long["id"]])
    assert flow.runnable(store, "r", parallel=True, decide=_never) == []


def test_the_session_lets_a_queued_task_start_beside_the_work_ahead(capsys, store: Store, monkeypatch):
    from locks import cli

    monkeypatch.setenv("LOCKS_AUTOWORK", "off")
    first = store.enqueue("build the long feature", repo="r", cwd="/x")
    store.claim(first["id"])
    held = store.enqueue("fix the BLE test", repo="r", cwd="/x")
    store.set_parallel(held["id"], False, "it may touch the feature")
    assert cli.main(["lanes", "--beside", str(held["id"])]) == 0
    assert f"task #{held['id']} starts beside the work ahead" in capsys.readouterr().out
    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=_never)] == [held["id"]]
    assert store.last_event(held["id"], "dispatch_decided")["data"]["by"] == "session"
    store.claim(held["id"])
    assert cli.main(["lanes", "--beside", str(held["id"])]) == 1
    assert "only a queued task" in capsys.readouterr().err


def test_a_running_task_longer_than_nine_in_ten_of_its_kind_s_passes_is_slow(store: Store):
    import time

    for wall in (600, 700, 800, 900, 1000):
        done = store.create_task("ui", status="done", source="locks", repo="r", kind="ui")
        store.add_event(done["id"], "finished", status="done", wall_s=wall)
    running = store.create_task("the slow one", status="running", source="locks", repo="r", kind="ui")
    store.add_event(running["id"], "planned", start="sonnet/high")
    began = datetime.fromisoformat(store.last_event(running["id"], "planned")["ts"]).timestamp()
    assert flow.slow(store, store.get_task(running["id"]), now=began + 60) is None  # under SLOW_MIN_S
    line = flow.slow(store, store.get_task(running["id"]), now=began + 101 * 60)
    assert line == (f"running 101m: longer than 9 in 10 of the 5 ui tasks that passed here (p90 17m); "
                    f"`locks show {running['id']}` says what it is doing")
    other = store.create_task("docs", status="running", source="locks", repo="r", kind="docs")
    store.add_event(other["id"], "planned", start="haiku/medium")
    assert flow.slow(store, store.get_task(other["id"]), now=time.time() + 3 * 3600) is None  # no history


def test_a_wait_is_judged_again_once_the_work_ahead_changed_files(store: Store, monkeypatch):
    first = store.enqueue("build the course API", repo="r", cwd="/x")
    store.claim(first["id"])
    held = store.enqueue("add the student page", repo="r", cwd="/x")
    changed: list[str] = []
    monkeypatch.setattr(flow, "with_changes", lambda store, t: {**t, "changed": list(changed)})
    asked = []

    def decide(task, ahead):
        asked.append([t.get("changed") for t in ahead])
        files = ahead[0].get("changed")
        if not files:
            return dispatch.Decision(False, f"unsure: #{first['id']} changed nothing yet")
        return dispatch.Decision(True, "the API changes no page")

    # Judged when the work ahead had changed nothing yet: it waits, and git is read again only
    # once the wait is old enough.
    assert flow.runnable(store, "r", parallel=True, decide=decide) == []
    changed[:] = ["src/api/courses.ts"]
    assert flow.runnable(store, "r", parallel=True, decide=decide) == []
    assert asked == [[[]]]
    monkeypatch.setattr(flow, "REJUDGE_S", 0.0)
    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=decide)] == [held["id"]]
    again = store.last_event(held["id"], "dispatch_decided")["data"]
    assert again["moved"] == [first["id"]] and again["seen"] == {str(first["id"]): ["src/api/courses.ts"]}


def test_a_wait_stands_while_the_work_ahead_changed_nothing_new(store: Store, monkeypatch):
    first = store.enqueue("build the course API", repo="r", cwd="/x")
    store.claim(first["id"])
    held = store.enqueue("add the student page", repo="r", cwd="/x")
    monkeypatch.setattr(flow, "with_changes", lambda store, t: {**t, "changed": ["src/api/courses.ts"]})
    monkeypatch.setattr(flow, "REJUDGE_S", 0.0)
    assert flow.runnable(store, "r", parallel=True,
                         decide=lambda task, ahead: dispatch.Decision(False, "both change the API")) == []
    # Nothing new ahead: the wait stands and asks nothing, however old it is.
    assert flow.runnable(store, "r", parallel=True, decide=_never) == []
    # A wait surfaces or names gave is a fact: never asked again.
    store.add_event(held["id"], "dispatch_decided", parallel=False, reason="names", ahead=[first["id"]],
                    by="surfaces", seen={})
    assert flow.runnable(store, "r", parallel=True, decide=_never) == []


def test_a_start_that_raises_leaves_no_claimed_task_running_without_a_process(store: Store, tmp_path):
    import pytest

    first = store.enqueue("read the docs", repo="r", cwd=str(tmp_path))
    second = store.enqueue("read the faq", repo="r2", cwd=str(tmp_path))

    def start(task, where):
        raise RuntimeError("could not spawn")

    with pytest.raises(RuntimeError):
        flow.work(store, start=start, pause=lambda: None)
    for task in (first, second):
        row = store.get_task(task["id"])
        assert row["status"] == "queued" and not row["dispatched"]


def test_the_sweep_releases_a_claim_nobody_started_and_leaves_a_fresh_one(store: Store):
    stale = store.enqueue("stale claim", repo="r", cwd="/")
    fresh = store.enqueue("fresh claim", repo="r2", cwd="/")
    assert store.claim(stale["id"]) and store.claim(fresh["id"])
    now = datetime.now(UTC)
    store._conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?",
                        ((now - timedelta(minutes=6)).isoformat(timespec="seconds"), stale["id"]))
    store._conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?",
                        ((now - timedelta(minutes=1)).isoformat(timespec="seconds"), fresh["id"]))
    assert flow.CLAIM_STALE_S == 300
    swept = flow.sweep(store, now=now)
    assert swept == []  # a released claim is no dead worker
    row = store.get_task(stale["id"])
    assert row["status"] == "queued" and not row["dispatched"]
    released = store.last_event(stale["id"], "claim_released")
    assert 350 <= released["data"]["sat_s"] <= 370
    kept = store.get_task(fresh["id"])
    assert kept["status"] == "running" and kept["dispatched"]
    assert store.last_event(fresh["id"], "claim_released") is None
