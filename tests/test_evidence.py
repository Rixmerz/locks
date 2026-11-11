"""Workers made to use what they were given: notes found by the code a task touches, the code
index asked for them before they start, a neighbour's measurement required where it is owed, and
each CORRECT line of a TRACER flow a criterion the independent check answers."""
from __future__ import annotations

import pytest

from locks import analysis, arms, evidence, notes, orchestrate
from locks import capabilities as caps
from locks.launch import WorkerResult
from locks.orchestrate import Options, run
from locks.store import Store

from .conftest import git
from .test_checking import check, passing
from .test_orchestrate import Script, kind
from .test_tracer import _tracer

LAYOUT = {"layout-inspector": caps.Capability("layout-inspector", {"command": "li"}, hint="measure the page")}


# --- notes by the code a task touches -----------------------------------------------------------


def test_a_note_anchored_to_a_named_file_reaches_the_brief_and_a_loose_word_match_does_not(git_repo, store: Store,
                                                                                         monkeypatch):
    from locks import repo

    monkeypatch.setenv("LOCKS_NOTES", "on")
    key = repo.key(git_repo)
    anchored = store.add_note(key, "pricing", "Totals round half-even", "Every total keeps banker's rounding",
                              author="person", filed_by="person")
    store.add_anchor(anchored, "src/pricing.py")
    store.add_note(key, "ops", "Fix deploys on Fridays", "Deploys freeze on Fridays", author="person",
                   filed_by="person")
    dry = run("Fix the discount bug in src/pricing.py", git_repo, store, Options(dry_run=True), registry={},
              classifier=kind("implement"), adapters=[])
    ids = [int(n["id"]) for n in dry.plan.notes]
    assert anchored in ids
    assert len(ids) == 1  # "fix" alone is no reason to brief the deploy note


# --- the code index asked for the worker ---------------------------------------------------------


def test_the_index_says_what_named_files_reach_before_the_worker_starts(git_repo, tmp_path, store: Store):
    from .livespec_fixture import build
    from .test_livespec_adapter import adapter

    build(git_repo)
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    lines = a.reach(["src/billing.py"], git_repo, status)
    assert lines[0].startswith("livespec: these files implement specs") and "SPEC-7" in lines[0]
    assert "outside them call into them" in lines[1] and "src/api.py" in lines[1]
    assert a.reach(["README.md"], git_repo, status) == ()
    assert a.reach(["src/billing.py"], git_repo, type(status)(False, "", {})) == ()
    dry = run("Fix the charge in src/billing.py", git_repo, store, Options(dry_run=True), registry={},
              classifier=kind("implement"), adapters=[a])
    told = next(line for line in dry.plan.context if line.startswith("What the code index says the files"))
    assert "SPEC-7" in told and "src/api.py" in told
    assert any("named file(s) reach is in the brief" in r for r in dry.plan.reasons)
    reading = run("Explain the charge in src/billing.py", git_repo, store, Options(dry_run=True), registry={},
                  classifier=kind("explore"), adapters=[a])
    assert not any(line.startswith("What the code index says the files") for line in reading.plan.context)


# --- evidence a neighbour owes -------------------------------------------------------------------


def _call(store: Store, task_id: int, attempt: int, tool: str, ok: int = 1) -> None:
    store.add_tool_event(task_id=task_id, attempt=attempt, tool=tool, sig=tool, arg_hash="h", ok=ok)


def test_what_is_owed_is_read_from_the_calls_that_went_through(store: Store):
    task = store.create_task("t", status="running", source="locks")["id"]
    names = ["layout-inspector", "rastro", "other"]
    ui, plain = ["components/Card.tsx"], ["src/db.py"]
    assert [r.capability for r in evidence.owed(store, task, 1, names, ui, "implement")] == [
        "layout-inspector"]
    assert evidence.owed(store, task, 1, names, plain, "implement") == []  # rastro browses, it owes nothing
    assert evidence.owed(store, task, 1, names, ui, "explore") == []
    _call(store, task, 1, "mcp__layout-inspector__detect_issues", ok=0)  # refused: it showed nothing
    assert "layout-inspector" in [r.capability for r in evidence.owed(store, task, 1, names, ui, "implement")]
    _call(store, task, 1, "mcp__layout-inspector__detect_issues")
    _call(store, task, 2, "mcp__rastro__rastro_open")
    assert evidence.owed(store, task, 2, names, ui, "implement") == []
    assert evidence.calls(store, task, 1, names) == {
        "layout-inspector": {"calls": 2, "ok": 1}, "rastro": {"calls": 0, "ok": 0}, "other": {"calls": 0, "ok": 0}}
    assert "detect_issues" in evidence.brief_line(evidence.handed(names))
    assert evidence.shape(evidence.handed(["layout-inspector"])).startswith(
        "evidence: no call of layout-inspector went through")
    assert evidence.handed(["rastro"]) == []


def test_a_change_with_nothing_to_render_owes_no_measurement(store: Store):
    # Issue #115: a validator's `.tsx` fixtures and a coverage report's HTML matched the UI patterns,
    # so work in a repository with nothing to render was held to layout-inspector.
    task = store.create_task("t", status="running", source="locks")["id"]
    names = ["layout-inspector"]
    nothing_rendered = ["src/rules.ts", "tests/fixtures/page.tsx", "test/fixtures/bad.jsx", "coverage/index.html",
                        "src/rules.test.tsx", "examples/demo.html", "docs/guide.html", "dist/app.css",
                        "src/__snapshots__/view.tsx", "node_modules/x/y.css"]
    assert evidence.owed(store, task, 1, names, nothing_rendered, "feature") == []
    for rendered in ("components/Card.tsx", "src/App.tsx", "styles/site.css", "index.html", "routes/home.tsx"):
        assert caps.renders(rendered), rendered
        assert [r.capability for r in evidence.owed(store, task, 1, names, [rendered], "feature")] == [
            "layout-inspector"]


@pytest.mark.usefixtures("no_verifier")
def test_a_feature_in_a_repo_with_no_ui_is_judged_on_its_check(git_repo, store: Store, monkeypatch):
    # The issue's shape: layout-inspector is handed to a feature, the repository renders nothing, and
    # the pass that wrote code and a test fixture of type `.tsx` stands on its check, at once.
    _held(monkeypatch)
    handed = {"layout-inspector": caps.Capability("layout-inspector", {"command": "li"}, when=("feature",),
                                                  hint="measure the page")}

    def worker(spec):
        calls.append(spec)
        (spec.target_dir / "src").mkdir(exist_ok=True)
        (spec.target_dir / "tests" / "fixtures").mkdir(parents=True, exist_ok=True)
        (spec.target_dir / "src" / "rules.ts").write_text("rules\n")
        (spec.target_dir / "tests" / "fixtures" / "page.tsx").write_text("fixture\n")
        return passing()

    calls: list = []
    report = run("Add the validator rules", git_repo, store, registry=handed, launcher=worker,
                 classifier=kind("feature"))
    assert "layout-inspector" in calls[0].mcp_servers
    assert report.status == "done" and len(calls) == 1
    assert not store.events_of(report.task_id, "evidence_owed")


def _held(monkeypatch):
    monkeypatch.setenv("LOCKS_EVIDENCE", "on")
    monkeypatch.setattr(evidence, "arm", lambda store, repo, task_id: ("gate", "held for the test"))


def _ui_repo(git_repo):
    (git_repo / "components").mkdir()
    (git_repo / "components" / ".keep").write_text("")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "components")


def test_a_ui_pass_with_no_measurement_goes_back_once_then_to_the_orchestrator(git_repo, store: Store, monkeypatch):
    _ui_repo(git_repo)
    _held(monkeypatch)
    worked = WorkerResult(True, None, "done", "1 passed", cost_usd=0.1, session_id="s1")
    script = Script(worked, worked, write="components/Card.tsx")
    report = run("Restyle the card", git_repo, store, registry=LAYOUT, launcher=script, classifier=kind("implement"))
    assert report.status == "blocked" and report.stop.cause == "decision"
    first, again = script.specs
    assert "This work is held to evidence" in first.prompt and "detect_issues" in first.prompt
    assert again.cell == first.cell and again.resume_session == "s1" and again.resume_prompt == evidence.PROMPT
    assert "evidence: no call of layout-inspector went through" in again.resume_note
    owed = store.events_of(report.task_id, "evidence_owed")
    assert [e["data"]["again"] for e in owed] == [False, True]
    briefed = store.events_of(report.task_id, "briefed")[0]["data"]
    assert briefed["evidence_arm"] == "gate" and briefed["evidence_of"] == ["layout-inspector"]
    counted = store.events_of(report.task_id, "attempt_finished")[0]["data"]["capability_calls"]
    assert counted == {"layout-inspector": {"calls": 0, "ok": 0}}


@pytest.mark.usefixtures("no_verifier")
def test_a_ui_pass_that_measured_stands(git_repo, store: Store, monkeypatch):
    _ui_repo(git_repo)
    _held(monkeypatch)

    def measured(spec):
        (spec.target_dir / "components" / "Card.tsx").write_text("card\n")
        _call(store, int(spec.env["LOCKS_WORKER_TASK"]), int(spec.env["LOCKS_WORKER_ATTEMPT"]),
              "mcp__layout-inspector__detect_issues")
        return passing()

    report = run("Restyle the card", git_repo, store, registry=LAYOUT, launcher=measured,
                 classifier=kind("implement"))
    assert report.status == "done" and not store.events_of(report.task_id, "evidence_owed")


@pytest.mark.usefixtures("no_verifier")
def test_free_work_is_never_held_back_and_its_calls_are_still_counted(git_repo, store: Store, monkeypatch):
    _ui_repo(git_repo)
    monkeypatch.setenv("LOCKS_EVIDENCE", "on")
    monkeypatch.setattr(evidence, "arm", lambda store, repo, task_id: ("free", "free for the test"))
    script = Script(passing(), write="components/Card.tsx")
    report = run("Restyle the card", git_repo, store, registry=LAYOUT, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and "held to evidence" not in script.specs[0].prompt
    assert store.events_of(report.task_id, "briefed")[0]["data"]["evidence_arm"] == "free"


def test_the_trial_alternates_then_keeps_the_gate_only_where_it_drew_fewer_blockers(store: Store):
    def task(arm: str, outcome: str | None) -> None:
        t = store.create_task("t", status="done", source="locks", repo="r", kind="implement")
        store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=1, input_tokens=10)
        store.add_event(t["id"], "briefed", evidence_arm=arm)
        if outcome:
            store.add_event(t["id"], "verified", seq=1, round=0, worker="sonnet/medium", passed=outcome == "clean",
                            blocking=int(outcome == "blocked"))

    ids = {evidence.ARMS[arms.side("evidence", "r", n, 0, 0)]: n for n in range(1, 30)}
    assert evidence.arm(store, "r", ids["gate"])[0] == "gate" and evidence.arm(store, "r", ids["free"])[0] == "free"
    task("gate", "clean")
    task("gate", "clean")
    assert evidence.arm(store, "r", ids["gate"])[0] == "free"  # two apart: the arm with fewer tasks goes next
    for n in range(evidence.TRIAL):
        task("gate", "clean")
        task("free", "blocked" if n < 3 else "clean")
    arm, why = evidence.arm(store, "r", 2)
    assert arm == "gate" and why.startswith("holding work to evidence won its trial here")
    found = evidence.trial(store, "r")
    assert found["gate"]["read"] == evidence.TRIAL + 2 and found["free"]["blocked"] == 3
    for _ in range(6):  # 6 of 16 held blocked against 3 of 8 free: the gate lost
        task("gate", "blocked")
    arm, why = evidence.arm(store, "r", 2)
    assert arm == "free" and why.startswith("holding work to evidence lost its trial here")
    assert evidence.arm(store, None, 1)[0] == "gate"


def test_stats_say_which_capabilities_workers_were_handed_and_never_called(store: Store):
    events = [{"kind": "attempt_finished", "task_id": 1, "data": {"passed": True, "capability_calls": {
                  "layout-inspector": {"calls": 3, "ok": 2}, "rastro": {"calls": 0, "ok": 0}}}},
              {"kind": "attempt_finished", "task_id": 2, "data": {"passed": False, "capability_calls": {
                  "layout-inspector": {"calls": 0, "ok": 0}}}},
              {"kind": "attempt_finished", "task_id": 9, "data": {"capability_calls": {"x": {"calls": 1}}}}]
    rows = {r["capability"]: r for r in analysis.capabilities_used(events, {1, 2})}
    assert set(rows) == {"layout-inspector", "rastro"}
    layout = rows["layout-inspector"]
    assert (layout["handed"], layout["called"], layout["through"], layout["never_called"]) == (2, 1, 1, 1)
    assert layout["pass_when_called"] == 1.0 and layout["pass_when_silent"] == 0.0
    assert rows["rastro"]["called_rate"] == 0.0


# --- each CORRECT line a criterion of the independent check ---------------------------------------


def test_the_check_answers_each_correct_line_and_one_not_met_is_a_blocker(git_repo, store: Store, monkeypatch):
    from locks import tracer

    _tracer(git_repo)
    listed, more = tracer.criteria(git_repo, "Fix the course so learners can find material")
    assert listed[0] == ("C1", "find-material: the file shows under its real name with a thumbnail")
    assert len(listed) == 5 and more == 0
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    missed = WorkerResult(True, None, "checked", "probe", cost_usd=0.05, verdict="pass",
                          criteria=({"id": "C1", "met": False, "evidence": "the list shows a UUID"},
                                    *({"id": f"C{n}", "met": True, "evidence": "seen"} for n in range(2, 6))))
    script = Script(passing(), missed, passing("fixed"), check("pass"), check("pass"), write="app.py")
    report = run("Fix the course so learners can find material", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "done"
    verifier = script.specs[1]
    assert {"C1", "C5"} <= set(verifier.criteria) and "C1: find-material: the file shows" in verifier.prompt
    first, second = store.events_of(report.task_id, "verified")
    assert first["data"]["blocking"] == 1 and first["data"]["blockers"][0]["where"] == "C1"
    assert "the list shows a UUID" in script.specs[2].resume_note or "C1 is not met" in script.specs[2].prompt
    # The re-check answered none of the lines, even asked again: said, on its event and the verdict card,
    # never taken as met.
    assert second["data"]["tracer_reasked"] == second["data"]["tracer_unanswered"] == ["C1", "C2", "C3", "C4", "C5"]
    assert "Your answer left these CORRECT lines unanswered" in script.specs[4].prompt
    card = store.last_event(report.task_id, "finished")["data"]["verdict"]["independent"]
    assert card["unchecked"] == ["C1", "C2", "C3", "C4", "C5"]
    from locks import hooks

    lines = hooks.verdict_lines({"check": None, "criteria": [], "independent": card})
    assert "  TRACER CORRECT lines nobody checked: C1, C2, C3, C4, C5" in lines
    assert lines[-1].endswith("open the diff only where it says something was not checked or not met")


def test_correct_lines_that_do_not_fit_are_said_by_count(git_repo, monkeypatch):
    from locks import tracer

    _tracer(git_repo)
    monkeypatch.setattr(tracer, "BRIEF_CHARS", 300)
    listed, more = tracer.criteria(git_repo, "Fix the course so learners can find material")
    assert listed and more == 5 - len(listed)
    lines = tracer.verifier_lines(git_repo, "Fix the course so learners can find material")
    assert any(f"({more} more CORRECT line(s) did not fit here" in line for line in lines)


@pytest.mark.parametrize("setting", ["evidence"])
def test_the_setting_turns_it_off(setting, monkeypatch):
    monkeypatch.setenv("LOCKS_EVIDENCE", "off")
    assert not evidence.enabled()
    monkeypatch.setenv("LOCKS_EVIDENCE", "on")
    assert evidence.enabled()
    assert notes and orchestrate  # imported for the module's own checks above


def test_an_attempt_whose_hooks_logged_no_call_is_not_recorded_as_silent(store: Store):
    silent = {"rastro": {"calls": 0, "ok": 0}}
    events = [{"kind": "attempt_finished", "task_id": 1,
               "data": {"passed": True, "tools_logged": False, "capability_calls": silent}},
              {"kind": "attempt_finished", "task_id": 2,
               "data": {"passed": True, "tools_logged": True, "capability_calls": silent}}]
    rows = {r["capability"]: r for r in analysis.capabilities_used(events, {1, 2})}
    assert rows["rastro"]["handed"] == 1 and rows["rastro"]["never_called"] == 1 and rows["rastro"]["unlogged"] == 1
    assert rows["rastro"]["called_rate"] == 0.0


def test_stats_say_when_a_worker_s_calls_were_never_logged(store: Store):
    t = store.create_task("t", status="done", source="locks", repo="r", kind="implement")
    store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=1)
    store.add_event(t["id"], "planned", kind="implement", start="sonnet/medium", plan_s=1.0)
    store.add_event(t["id"], "attempt_finished", seq=1, cell="sonnet/medium", passed=True, tools_logged=False,
                    capability_calls={"rastro": {"calls": 0, "ok": 0}})
    store.add_event(t["id"], "finished", status="done", wall_s=60.0)
    said = analysis.text(analysis.compute(store, days=0))
    assert "rastro" in said and "1 attempt(s) not recorded: their worker's hooks logged no call" in said


def test_rastro_is_no_requirement_and_layout_inspector_still_is():
    assert [r.capability for r in evidence.REQUIRED] == ["layout-inspector"]
