"""What locks learns is made to earn its place: a worker's candidate note weighed against its
absence, the notes of the files a later attempt changed, a method tried in arms, a plan's
pre-mortem handed to the checks after it, and a loop's round that moved nothing kept as such."""
from __future__ import annotations

from dataclasses import replace as dataclasses_replace
from datetime import datetime

import pytest

from locks import arms, flow, loops, methods, notes, orchestrate
from locks.orchestrate import Options, run
from locks.store import Store

from .test_loops import ROUND, _loop
from .test_orchestrate import Script, kind, ok
from .test_standards import finished
from .test_workflows import _runner, _save


def _briefed(store: Store, task_id: int, carried=(), withheld=()) -> None:
    store.add_event(task_id, "briefed", session=None, dead_ends=0, areas=[], context_chars=0,
                    **{notes.BRIEFED: list(carried), notes.WITHHELD: list(withheld)})


def _candidate(store: Store, text: str = "Totals are rounded half up") -> int:
    return store.add_note("r", "code", "t", text, author="worker", filed_by="haiku", state=notes.CANDIDATE)


# --- a candidate note weighed against its absence ---------------------------------------------------


def test_a_candidate_goes_without_in_turn_until_its_trial_is_full(store: Store, monkeypatch):
    note = _candidate(store)
    found = notes.expand(store, [note])
    assert notes.arms(store, found) == (found, [])  # the trial is off in tests
    monkeypatch.setenv("LOCKS_NOTE_TRIALS", "on")
    assert notes.arms(store, found) == (found, [])  # carried first
    _briefed(store, finished(store, ("sonnet/medium", 1)), carried=[note])
    carried, left = notes.arms(store, found)
    assert carried == [] and [n["id"] for n in left] == [note]
    for _ in range(notes.NOTE_TRIAL):
        _briefed(store, finished(store, ("sonnet/medium", 1)), withheld=[note])
        _briefed(store, finished(store, ("sonnet/medium", 1)), carried=[note])
    assert notes.arms(store, found) == (found, [])  # left out its full share: carried from now on
    rule = store.add_note("r", "rules", "c", "Gift cards are never discounted", author="worker", filed_by="haiku",
                          state=notes.CANDIDATE, kind=notes.CONSTRAINT)
    assert notes.arms(store, notes.expand(store, [rule])) == (notes.expand(store, [rule]), [])  # a rule never goes


def test_a_candidate_is_current_where_it_did_no_worse_than_its_absence_and_dropped_where_it_did(store: Store,
                                                                                            monkeypatch):
    monkeypatch.setenv("LOCKS_NOTE_TRIALS", "on")
    helps, hurts = _candidate(store), _candidate(store, "The cache is warmed by a cron job")
    for n in range(notes.NOTE_TRIAL):
        _briefed(store, finished(store, ("sonnet/medium", 1)), carried=[helps])
        _briefed(store, finished(store, ("sonnet/medium", int(n == 0), "code_bug")), withheld=[helps])
        _briefed(store, finished(store, ("sonnet/medium", int(n == 0), "code_bug")), carried=[hurts])
        _briefed(store, finished(store, ("sonnet/medium", 1)), withheld=[hurts])
    assert notes.weigh(store) == {"current": [helps], "dropped": [hurts]}
    assert store.get_note(helps)["state_reason"].startswith(notes.PROVEN + "the 3 tasks that carried it passed")
    assert store.get_note(hurts)["state_reason"].startswith(
        "disproven: the 3 tasks that carried it passed their first try 33%")


def test_a_candidate_waits_for_its_arm_even_after_two_passes(store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_NOTE_TRIALS", "on")
    note = _candidate(store)
    for _ in range(notes.PROVE_AFTER):
        _briefed(store, finished(store, ("sonnet/medium", 1)), carried=[note])
    assert notes.weigh(store) == {"current": [], "dropped": []}  # without the trial it would be proven
    monkeypatch.setenv("LOCKS_NOTE_TRIALS", "off")
    assert notes.weigh(store) == {"current": [note], "dropped": []}


def test_a_later_attempt_reads_the_notes_of_the_files_the_work_changed(git_repo, store: Store, monkeypatch):
    from locks import repo

    monkeypatch.setenv("LOCKS_NOTES", "on")
    key = repo.key(git_repo)
    note = store.add_note(key, "code", "Counters", "The counter wraps at 255", author="person", filed_by="person")
    store.add_anchor(note, "counter.txt")
    script = Script(ok("first"), ok("second"), write="counter.txt")
    script.results[0] = orchestrate.launch.WorkerResult(False, orchestrate.Failure.CODE_BUG, "wrong", cost_usd=0.1)
    run("Make the tally right", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    first, second = script.specs
    assert "The counter wraps at 255" not in first.prompt  # the request named no file the note is about
    assert "The counter wraps at 255 · about counter.txt" in second.prompt


# --- methods tried in arms ----------------------------------------------------------------------------


def test_a_method_alternates_then_stays_where_it_did_no_worse(store: Store):
    diagnosis = methods.METHODS["debug-unclear"]

    def task(side: str, passed: bool) -> None:
        t = store.create_task("t", status="done", source="locks", repo="r", kind="debug-unclear")
        store.add_attempt(t["id"], cell="opus/high", max_turns=30, passed=int(passed), input_tokens=10)
        store.add_event(t["id"], "briefed", method_arm=f"diagnosis:{side}")

    ids = {methods.ARMS[arms.side("diagnosis", "r", n, 0, 0)]: n for n in range(1, 30)}
    assert methods.arm(store, "r", diagnosis, ids["with"])[0] == "with"
    task("with", True)
    task("with", True)
    assert methods.arm(store, "r", diagnosis, ids["with"])[0] == "without"  # two apart: fewer goes next
    for n in range(methods.TRIAL):
        task("with", True)
        task("without", n < 3)
    side, why = methods.arm(store, "r", diagnosis, 2)
    assert side == "with" and why.startswith("the diagnosis won its trial here")
    for _ in range(9):  # 8 of 17 with it against 3 of 6 without: it lost
        task("with", False)
    assert methods.arm(store, "r", diagnosis, 2)[0] == "without"
    assert methods.arm(store, None, diagnosis, 1)[0] == "with"


def test_a_debug_brief_carries_the_diagnosis_on_its_arm(git_repo, store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_METHODS", "on")
    monkeypatch.setattr(methods, "arm", lambda store, repo, method, task_id: ("with", "on trial"))
    script = Script(ok(), write="x.txt")
    report = run("The export sometimes drops rows", git_repo, store, registry={}, launcher=script,
                 classifier=kind("debug-unclear"))
    assert "Diagnose before you change anything" in script.specs[0].prompt
    assert store.events_of(report.task_id, "briefed")[0]["data"]["method_arm"] == "diagnosis:with"
    monkeypatch.setattr(methods, "arm", lambda store, repo, method, task_id: ("without", "on trial"))
    script = Script(ok(), write="y.txt")
    run("The import sometimes drops rows", git_repo, store, registry={}, launcher=script,
        classifier=kind("debug-unclear"))
    assert "Diagnose before you change anything" not in script.specs[0].prompt


def test_a_plan_s_risks_reach_the_check_of_the_work_after_it(git_repo, store: Store, kicks, monkeypatch):
    assert methods.risks("Split in two.\nRisks:\n- the cache keeps old totals\n- 2. a race on save\n\nLater.") == [
        "the cache keeps old totals", "a race on save"]
    assert methods.risks("Risks: none worth a probe") == ["none worth a probe"]
    assert methods.risks("no section") == []
    _save({"name": "planned", "inputs": ["goal"], "steps": [
        {"id": "plan", "prompt": "Plan {goal}", "kind": "plan"},
        {"id": "build", "prompt": "Build {goal}", "kind": "implement", "start": "haiku/high"}]}, git_repo)
    from locks import workflows

    started = workflows.start(store, "planned", git_repo, {"goal": "the export"}, session_id="s1")
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    from .test_checking import check

    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("Two steps.\nRisks:\n- the totals cache keeps old values"), ok("built"),
                                     check("pass")], seen))
    assert workflows.status(store, started["id"])["state"] == "done"
    assert "the plan before this work named a risk: the totals cache keeps old values" in seen[2]["prompt"]


def test_stats_show_each_method_s_trial(store: Store):
    from locks import analysis

    done = finished(store, ("opus/high", 1), kind="debug-unclear")
    store.add_event(done, "briefed", method_arm="diagnosis:with")
    report = analysis.compute(store, days=0, repo="r")
    assert report["methods"]["diagnosis"]["with"] == {"tasks": 1, "tries": 1, "passed": 1, "rate": 1.0,
                                                       "pass_p50_s": None}
    assert "the diagnosis method (its trial" in analysis.text(report)


# --- a loop's round that moved nothing --------------------------------------------------------------


def test_a_round_that_changed_and_moved_nothing_is_an_empty_round_and_no_base(tmp_path, store: Store, kicks):
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "count").write_text("1\n")
    _save(ROUND, folder)
    _loop(folder, metric="cat count", stale=3)
    loops.set_state("leads", status="running")
    now = datetime(2026, 10, 25, 23, 0).astimezone()
    assert loops.tick(store, "leads", now=now) == "started"
    flow.work(store, parallel=False, runner=_runner([ok("nothing would move it now")], []))
    loops.tick(store, "leads", now=now)
    first = loops.diary("leads")[0]
    assert first["empty"] is True and first["changed_files"] == 0
    told = store.run_tasks(loops.diary("leads")[1]["run"])[0]["body"]
    assert "A round that changes nothing is a valid outcome" in told and "changed nothing" in told
    assert "empty rounds (passed, changed nothing): 1" in loops.summary("leads")
    assert Options  # kept for the module's imports


def test_a_run_s_status_says_what_each_step_is_for_its_graph(git_repo, store: Store, kicks, capsys):
    from locks import workflows

    from .test_workflow_steps import ASKING

    _save(ASKING, git_repo)
    started = workflows.start(store, "asking", git_repo, {"goal": "the export"}, session_id="s1")
    flow.work(store, runner=_runner([ok("built")], []))
    steps = {s["id"]: s for s in workflows.status(store, started["id"])["steps"]}
    assert steps["how"]["asks"] == ["flag", "direct"] and steps["flagged"]["when"] == {"how": "flag"}
    assert "asks" not in steps["build"] and "runs" not in steps["build"]
    from locks import workflow_cli

    workflow_cli._print_run(workflows.status(store, started["id"]))
    out = capsys.readouterr().out
    assert f"asks the person: how — `locks workflow answer {started['id']} how <flag|direct>`" in out
    assert "waits on the orchestrator: " not in out  # a question is answered, never resumed


def test_a_plan_is_told_the_change_its_task_names_is_for_the_work_after_it(tmp_path):
    planned = orchestrate.brief("Plan: add a subtract function", "plan", tmp_path, [], [], None, writes=False)
    assert orchestrate.PLAN_CHANGE in planned and "`fail` with `spec_bug` when the task asks" not in planned
    read = orchestrate.brief("Explore the export", "explore", tmp_path, [], [], None, writes=False)
    assert "`fail` with `spec_bug` when the task asks for a change" in read


# --- the order of a brief's notes, what workers relied on, long files by section ---------------------


def test_reciprocal_rank_fusion_puts_a_note_both_lists_found_first():
    assert notes.fused([[1, 2, 3], [4, 3, 5]]) == [3, 1, 4, 2, 5]
    assert notes.fused([[1], []], rest=[9, 1]) == [1, 9]


def test_recall_fuses_what_the_code_and_the_words_found(store: Store):
    a = store.add_note("r", "code", "Cache", "The totals cache is warmed nightly", author="person", filed_by="person")
    b = store.add_note("r", "code", "Export", "Exports stream rows in batches of 500 rows each", author="person",
                       filed_by="person")
    store.add_anchor(a, "cache.py")
    plain = [n["id"] for n in notes.recall(store, "r", "exports stream rows batches", paths=["cache.py"], limit=1)]
    fused = [n["id"] for n in notes.recall(store, "r", "exports stream rows batches", paths=["cache.py"], limit=2,
                                           fuse=True)]
    assert plain == [a] and set(fused) == {a, b}


def test_the_recall_order_alternates_then_stays_where_it_did_no_worse(store: Store):
    def task(side: str, passed: bool) -> None:
        t = store.create_task("t", status="done", source="locks", repo="r", kind="implement")
        store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=int(passed), input_tokens=10)
        store.add_event(t["id"], "briefed", recall_arm=side)

    ids = {notes.RECALL_ARMS[arms.side("recall order", "r", n, 0, 0)]: n for n in range(1, 30)}
    assert notes.recall_arm(store, "r", ids["fused"])[0] == "fused"
    task("fused", True)
    task("fused", True)
    assert notes.recall_arm(store, "r", ids["fused"])[0] == "ordered"
    for n in range(notes.RECALL_TRIAL):
        task("fused", n < 2)
        task("ordered", True)
    side, why = notes.recall_arm(store, "r", 0)
    assert side == "ordered" and why.startswith("the recall order lost its trial here")
    assert notes.recall_trial(store, "r")["ordered"]["passed"] == notes.RECALL_TRIAL


@pytest.mark.usefixtures("no_verifier")
def test_a_brief_whose_code_and_words_found_different_notes_records_its_order(git_repo, store: Store, monkeypatch):
    from locks import repo

    monkeypatch.setenv("LOCKS_NOTES", "on")
    monkeypatch.setenv("LOCKS_RECALL_FUSION", "on")
    key = repo.key(git_repo)
    for n in range(7):
        anchored = store.add_note(key, "code", f"App {n}", f"app.py detail number {n}", author="person",
                                  filed_by="person")
        store.add_anchor(anchored, "app.py")
    store.add_note(key, "code", "Greeting", "The greeting banner prints hello in the app", author="person",
                   filed_by="person")
    report = run("Change the greeting banner in app.py so it prints hello", git_repo, store, registry={},
                 launcher=Script(ok(), write="app.py"), classifier=kind("implement"))
    briefed = store.events_of(report.task_id, "briefed")[0]["data"]
    assert briefed[notes.RECALL_KEY] in notes.RECALL_ARMS and briefed["recall_why"]


@pytest.mark.usefixtures("no_verifier")
def test_a_worker_s_notes_used_are_kept_only_for_notes_its_brief_carried(git_repo, store: Store, monkeypatch):
    from locks import analysis, launch, repo

    assert launch.notes_used({"notes_used": [12, "#13", "x", True, 12]}) == (12, 13)
    assert launch.notes_used({"notes_used": "12"}) == ()
    monkeypatch.setenv("LOCKS_NOTES", "on")
    key = repo.key(git_repo)
    note = store.add_note(key, "code", "Greeting", "The greeting prints hi", author="person", filed_by="person")
    store.add_anchor(note, "app.py")
    done = dataclasses_replace(ok(), notes_used=(note, 999), notes_said=True)
    report = run("Change the greeting in app.py", git_repo, store, registry={}, launcher=Script(done, write="app.py"),
                 classifier=kind("implement"))
    finished_attempt = store.events_of(report.task_id, "attempt_finished")[0]["data"]
    assert finished_attempt["notes_used"] == [note]
    said = analysis.compute(store, days=0, repo=key)["notes_used"]
    assert said["tasks"] == 1 and said["relied_on"] == 1 and said["pass_first_naming"] == 1.0
    assert "workers said they relied on 1" in analysis.text(analysis.compute(store, days=0, repo=key))


def test_an_outline_names_each_section_with_its_lines():
    from locks.text import outline

    body = "# A\nx\n## B\ny\n```\n# not a heading\n```\n# C\nz\n"
    assert outline("f.md", body) == ["A (f.md:1-7)", "  B (f.md:3-7)", "C (f.md:8-9)"]
    assert outline("f.md", body, after=3) == ["C (f.md:8-9)"]


def test_method_files_that_do_not_fit_are_named_by_section(tmp_path, monkeypatch):
    from locks import tracer

    folder = tmp_path / "docs" / "tracer"
    folder.mkdir(parents=True)
    long = "# Audience\n" + "\n".join(f"line {n} of who uses it" for n in range(400)) + "\n## Edge readers\nmore\n"
    (folder / "audiences.md").write_text(long)
    monkeypatch.setattr(tracer, "BRIEF_CHARS", 600)
    lines = tracer.brief_lines(tmp_path, "anything")
    index = next(line for line in lines if line.startswith("Not shown above, by section"))
    assert "Edge readers (docs/tracer/audiences.md:402-403)" in index


def test_two_trials_over_the_same_tasks_land_their_arms_on_different_tasks():
    # One parity for every trial put the same tasks in every "with" arm; a hash salted with the
    # trial's name does not: over forty tasks the two trials agree on about half.
    primer = [arms.side("primer", "r", n, 0, 0) for n in range(40)]
    trunk = [arms.side("trunk", "r", n, 0, 0) for n in range(40)]
    agreed = sum(a == b for a, b in zip(primer, trunk, strict=True))
    assert 8 <= agreed <= 32 and set(primer) == {0, 1}
    # Deterministic: the same trial, repository and task always land the same side.
    assert arms.side("primer", "r", 7, 0, 0) == arms.side("primer", "r", 7, 1, 1)
    # Once the arms drift `BALANCE` apart, the one with fewer tasks goes next, whatever the hash says.
    assert all(arms.side("primer", "r", n, 0, arms.BALANCE) == 0 for n in range(40))
    assert all(arms.side("primer", "r", n, arms.BALANCE, 0) == 1 for n in range(40))


def test_a_change_that_passes_as_often_but_slower_loses_its_trial():
    def rows(arm: str, took: float, n: int = 4) -> list[dict]:
        return [{"arm": arm, "tries": 1, "passed": 1, "duration_s": took} for _ in range(n)]

    found = arms.tally([*rows("with", 100.0), *rows("without", 60.0)], ("with", "without"))
    assert found["with"]["pass_p50_s"] == 100.0 and found["without"]["pass_p50_s"] == 60.0
    side, why = arms.pick(found, ("with", "without"), 1, 4, "diagnosis")
    assert side == "without" and "passes took 100s at the median with against 60s without" in why
    # Within the margin, or untimed, pass-first alone decides; a failed try's time is nobody's.
    close = arms.tally([*rows("with", 70.0), *rows("without", 60.0)], ("with", "without"))
    assert arms.pick(close, ("with", "without"), 1, 4, "diagnosis")[0] == "with"
    untimed = arms.tally([{"arm": "with", "tries": 1, "passed": 1}] * 4 + rows("without", 60.0), ("with", "without"))
    assert untimed["with"]["pass_p50_s"] is None and arms.pick(untimed, ("with", "without"), 1, 4, "d")[0] == "with"
    failed = arms.tally([{"arm": "with", "tries": 1, "passed": 0, "duration_s": 999.0}] * 4, ("with", "without"))
    assert failed["with"]["pass_p50_s"] is None


# --- livespec handed to a worker, tried against withholding it --------------------------------------


def test_livespec_alternates_hand_and_withhold_then_stays_where_it_did_no_worse_and_no_slower(store: Store):
    from locks import handing

    def task(which: str, side: str, passed: bool, took: float) -> int:
        t = store.create_task("t", status="done", source="locks", repo="r",
                              kind="implement" if which == "writing" else "explore")
        store.add_attempt(t["id"], cell="sonnet/high", max_turns=30, passed=int(passed), input_tokens=10,
                          duration_s=took)
        store.add_event(t["id"], "briefed", livespec_arm=f"{which}:{side}")
        return t["id"]

    assert handing.nature("implement") == "writing" and handing.nature("review-routine") == "reading"
    assert handing.arm(store, None, "implement", 1) == ("hand", "no repository to run a trial in")
    ids = {handing.ARMS[arms.side("livespec-writing", "r", n, 0, 0)]: n for n in range(1, 40)}
    assert handing.arm(store, "r", "implement", ids["hand"])[0] == "hand"
    assert handing.arm(store, "r", "implement", ids["withhold"])[0] == "withhold"
    task("writing", "hand", True, 100)
    task("writing", "hand", True, 100)
    assert handing.arm(store, "r", "implement", ids["hand"])[0] == "withhold"  # two apart: fewer goes next
    for _ in range(handing.TRIAL):
        task("writing", "hand", True, 300)
        task("writing", "withhold", True, 100)
    # as many passes, three times slower: handing lost, and the reading trial is apart
    side, why = handing.arm(store, "r", "implement", 999)
    assert side == "withhold" and "lost its trial here" in why and "300s" in why
    assert handing.arm(store, "r", "explore", 999)[1].startswith("the livespec-reading trial here (0 hand")
    # a resume keeps the arm its first brief drew, whatever the trial says now
    kept = task("writing", "hand", True, 300)
    assert handing.arm(store, "r", "implement", kept) == ("hand", "the arm this task's first brief drew")
    assert handing.trial(store, "r", "writing")["withhold"] == {
        "tasks": 6, "tries": 6, "passed": 6, "rate": 1.0, "pass_p50_s": 100.0}


@pytest.mark.usefixtures("no_verifier")
def test_a_withheld_worker_gets_no_livespec_server_nor_orientation_and_the_map_stays(
        git_repo, store: Store, tmp_path, monkeypatch):
    from locks import handing

    from .livespec_fixture import build
    from .test_orchestrate import _livespec

    build(git_repo)
    monkeypatch.setenv("LOCKS_LIVESPEC_TRIAL", "on")
    monkeypatch.setattr(handing, "arm", lambda store, repo, kind, task_id: ("withhold", "on trial"))
    script = Script(ok("fixed"))
    report = run("charge_card charges twice on retry", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[_livespec(tmp_path)])
    spec = script.specs[0]
    assert "livespec" not in spec.mcp_servers and "mcp__livespec" not in spec.allowed_tools
    assert "Orient by livespec" not in spec.prompt and "livespec" not in spec.append_system_prompt
    assert "Code map from livespec" in spec.prompt  # what locks read from the index itself stays
    briefed = store.events_of(report.task_id, "briefed")[0]["data"]
    assert briefed["livespec_arm"] == "writing:withhold" and "livespec_tools" not in briefed
    assert "livespec" not in store.events_of(report.task_id, "attempt_started")[0]["data"]["capabilities"]
    assert "livespec_calls" not in store.events_of(report.task_id, "attempt_finished")[0]["data"]
    # handed: the server, the orientation and the record of what was offered, as before
    monkeypatch.setattr(handing, "arm", lambda store, repo, kind, task_id: ("hand", "on trial"))
    script = Script(ok("fixed"))
    report = run("charge_card charges twice on retry", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[_livespec(tmp_path)])
    spec = script.specs[0]
    assert "livespec" in spec.mcp_servers and "Orient by livespec" in spec.prompt
    briefed = store.events_of(report.task_id, "briefed")[0]["data"]
    assert briefed["livespec_arm"] == "writing:hand" and briefed["livespec_tools"]
    # the setting off: handed, no arm drawn
    monkeypatch.setenv("LOCKS_LIVESPEC_TRIAL", "off")
    script = Script(ok("fixed"))
    report = run("charge_card charges twice on retry", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[_livespec(tmp_path)])
    assert "livespec" in script.specs[0].mcp_servers
    assert "livespec_arm" not in store.events_of(report.task_id, "briefed")[0]["data"]


def test_stats_show_the_livespec_handing_trial(store: Store):
    from locks import analysis

    done = finished(store, ("sonnet/high", 1), kind="implement")
    store.add_event(done, "briefed", livespec_arm="writing:withhold")
    report = analysis.compute(store, days=0, repo="r")
    assert report["livespec_handing"]["writing"]["withhold"]["tasks"] == 1
    assert report["livespec_handing"]["reading"]["withhold"]["tasks"] == 0
    assert "the livespec-writing trial here" in report["livespec_handing_now"]["writing"]
    text = analysis.text(report)
    assert "livespec handed to writing workers (its trial" in text and "pass p50 -" in text


def test_a_trial_short_here_takes_the_verdict_of_every_repository_once_those_arms_are_full(store: Store):
    """A repository with few tasks would pay for both arms for months: while its own arms are short and
    every repository's are full, the pooled verdict decides, says so, and its own arms decide alone
    once they fill."""
    from locks import handing

    def task(repo: str, side: str, passed: bool, took: float = 100.0) -> int:
        t = store.create_task("t", status="done", source="locks", repo=repo, kind="implement")
        store.add_attempt(t["id"], cell="sonnet/high", max_turns=30, passed=int(passed), input_tokens=10,
                          duration_s=took)
        store.add_event(t["id"], "briefed", livespec_arm=f"writing:{side}")
        return t["id"]

    # Elsewhere, handing lost (as many passes, three times slower); here the trial has one task.
    for _ in range(handing.TRIAL):
        task("other", "hand", True, 300)
        task("other", "withhold", True, 100)
    task("r", "hand", True)
    side, why = handing.arm(store, "r", "implement", 999)
    assert side == "withhold"
    assert "lost its trial everywhere" in why and "pooled from every repository while the trial here has 1 hand" in why
    # Pure: `arms.pick` with no pooled tally alternates as before; with one short of the trial, too.
    here = handing.trial(store, "r", "writing")
    assert "the x trial here (1 hand, 0 withhold" in arms.pick(here, handing.ARMS, 999, handing.TRIAL, "x", "r")[1]
    short = handing.trial(store, "nowhere", "writing")
    assert "trial here" in arms.pick(here, handing.ARMS, 999, handing.TRIAL, "x", "r", pooled=short)[1]
    # Once the repository's own arms are full, they decide alone, whatever the pool says.
    for _ in range(handing.TRIAL):
        task("r", "hand", True, 100)
        task("r", "withhold", True, 100)
    side, why = handing.arm(store, "r", "implement", 999)
    assert side == "hand" and "won its trial here" in why and "pooled" not in why


def test_a_worker_handed_nothing_still_has_its_logged_calls_read_at_the_attempt_s_end(git_repo, store, monkeypatch):
    """The hooks' log is loaded once before the attempt's record is read, whatever the worker was handed:
    read before it, a withheld worker's calls read as none (`doctor`: `tools-logged-off`)."""
    from locks import habits

    def load(store_, env=None):
        running = store_.list_tasks(status=["running"], limit=1)
        if running:
            store_.add_tool_event(task_id=running[0]["id"], attempt=1, tool="read", sig="read", arg_hash="x", ok=1)
        return 1

    monkeypatch.setattr(habits, "load_events", load)
    run("explain how the cart total is rounded", git_repo, store, registry={}, classifier=kind("explore"),
        launcher=Script(ok()))
    done = store.last_event(store.list_tasks(status=["done"], limit=1)[0]["id"], "attempt_finished")["data"]
    assert done["tools_logged"] is True and "capability_calls" not in done
