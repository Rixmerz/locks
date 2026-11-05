"""What one place writes and another reads back: each pair round-trips, or the bug is that one
of them drifted (a pause reason written `task #N:` and released as `task #N `, a resume that
rewrote a task's options without its workflow part)."""
import dataclasses
import json
import re
from pathlib import Path

import pytest

from locks import flow
from locks.matrix import Cell
from locks.orchestrate import Options

SRC = Path(__file__).resolve().parents[1] / "src" / "locks"


def test_every_option_a_resume_stores_reads_back_the_same():
    full = Options(max_attempts=3, verify="make check", kind="feature", start=Cell("opus", "high"),
                   allow_approval=True, livespec=False, allow_tools=("Bash(x:*)",), isolate=False, max_turns=99,
                   base="abc123", role="architect", step_role="builder", surfaces=("api/**",),
                   ledger={"role": "fix", "round": 1, "ids": ["F1"]}, keep_learned=False, carry=True,
                   add_dirs=("/other",), services=("serve@8000",), run_verify="make test", parts=True,
                   integrate={"branches": [{"part": "api", "task": 2, "branch": "b"}], "contract": "c"},
                   touches=("ui",), reviewed_next=True, loop="leads", trunk_for=True,
                   trunk={"step": "plan", "task": 4, "session": "s", "model": "m", "cell": "opus/xhigh", "base": "b"})
    back = flow.queued_options({"options": json.dumps(full.stored())}, Options())
    lost = [f.name for f in dataclasses.fields(Options)
            if f.name != "use_model_classifier" and getattr(back, f.name) != getattr(full, f.name)
            and getattr(full, f.name) != getattr(Options(), f.name)]
    # Run-time fields no queued task carries are not stored; everything a task is queued with is.
    assert set(lost) <= RUN_TIME, lost


def test_the_notes_a_brief_carried_are_the_ones_weighing_reads_back(git_repo, store, monkeypatch):
    """`orchestrate` keeps on `briefed` the notes the worker was shown; `notes.weigh` proves or
    drops a candidate from that field alone."""
    from locks import notes, repo
    from locks.orchestrate import run

    from .test_orchestrate import Script, kind, ok

    monkeypatch.setenv("LOCKS_NOTES", "on")
    key = repo.key(git_repo)
    note = store.add_note(key, "business", "Prices in cents", "Prices are stored in cents in every table",
                          author="worker", filed_by="haiku", state=notes.CANDIDATE)
    script = Script(ok())
    report = run("change how prices are stored in cents", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), notes_ask=lambda *a, **kw: (None, 0.0, ""))
    assert f"#{note} [business] Prices in cents" in script.specs[0].prompt and "(unproven:" in script.specs[0].prompt
    assert store.last_event(report.task_id, "briefed")["data"][notes.BRIEFED] == [note]
    assert notes.carried(store) == {report.task_id: {note}}


#: Options a run is given by whoever starts it, never kept on the task.
RUN_TIME: set[str] = set()


def test_every_reason_a_lane_is_paused_with_is_one_its_task_releases(store):
    """Each `pause_lane` reason in the code starts with `task #N` followed by a space or a
    colon, and a release of N lifts both: a new format is checked here, not found in use."""
    reasons = re.findall(r'pause_lane\([^,]+,\s*f"(task #\{[^}]+\}[^"]{0,2})', "".join(
        p.read_text() for p in SRC.rglob("*.py")))
    assert len(reasons) >= 4
    for n, reason in enumerate(reasons, 1):
        written = re.sub(r"\{[^}]+\}", str(n), reason, count=1) + " the rest"
        assert re.match(rf"task #{n}[ :]", written), reason
        store.pause_lane(f"r{n}", written)
        assert store.release_lane(f"r{n}", n), reason


def test_a_brief_s_revision_arm_is_what_the_trial_reads_back(store):
    """`revisions.for_brief` writes the arm on the `briefed` event; `revisions.arms` counts it,
    reading a task's first brief (a resume's is not the one its first try ran under)."""
    from locks import revisions

    cand = store.add_role_revision(at="2026-10-01T00:00:00+00:00", repo="r", role="implementer",
                                   record="proposed", hash="h", lessons=["Run the suite before a pass."],
                                   version="0.32.0")
    task = store.create_task("t", status="done", source="locks", repo="r")
    _, data = revisions.for_brief(store, "r", "implementer", task["id"])
    store.add_event(task["id"], "briefed", role="implementer", **data)
    store.add_event(task["id"], "briefed", role="implementer", revision_trial=cand, revision_arm="nonsense")
    store.add_attempt(task["id"], cell="sonnet/medium", max_turns=80, passed=1)
    found = revisions.arms(store, "r", cand)
    assert found[data["revision_arm"]]["tasks"] == 1 and found[data["revision_arm"]]["briefed"] == 1
    assert sum(arm["briefed"] for arm in found.values()) == 1


def test_a_check_that_failed_only_as_at_the_base_reads_back_in_the_ending(git_repo, store):
    """`orchestrate` writes `check_preexisting` and the verdict card's `check.state`; the ending
    notice reads both back (`hooks.verdict_lines`, `hooks._found_before`) from those fields alone."""
    from locks import hooks
    from locks.orchestrate import FAILING_AT_BASE, run

    from .test_orchestrate import Script, kind, ok

    report = run("add the export", git_repo, store, Options(verify="echo 'FAIL old.test.js'; exit 1"), registry={},
                 launcher=Script(ok(), write="f.py"), classifier=kind("implement"))
    check = store.final_event(report.task_id)["data"]["verdict"]["check"]
    kept = store.last_event(report.task_id, "check_preexisting")["data"]
    assert check["state"] == FAILING_AT_BASE and check["base"] == kept["base"] and check["failures"] == kept["n"]
    assert any(FAILING_AT_BASE in line and kept["base"][:12] in line for line in hooks.verdict_lines(
        {"check": check}))
    offered = hooks._found_before(store, store.get_task(report.task_id))
    assert kept["failures"][0] in offered[1] and f"base {kept['base'][:12]}" in offered[1]


def test_the_reset_a_limit_stop_keeps_is_the_one_its_resume_waits_for(git_repo, store):
    """`orchestrate` keeps the usage limit's reset on the stop (`resets_at`); `dispatch.recover`
    waits for it from that field alone, and `flow.pick` from the store's meta."""
    from datetime import datetime

    from locks import dispatch, stops
    from locks.escalate import Failure
    from locks.launch import WorkerResult
    from locks.orchestrate import run

    from .test_orchestrate import Script, kind

    limited = WorkerResult(False, Failure.ENVIRONMENT, "the session errored: You've hit your limit · resets 5pm",
                           transient=True, limited="usage", retry_after_s=600.0)
    report = run("fix the total", git_repo, store, Options(), registry={}, launcher=Script(limited),
                 classifier=kind("implement"))
    stop = stops.of(store, store.get_task(report.task_id))
    resets = datetime.fromisoformat(stop.extra["resets_at"]).timestamp()
    assert dispatch._due(stop, 0.0) == resets + dispatch.LIMIT_GRACE_S
    assert flow.limited_until(store) == stop.extra["resets_at"]


@pytest.mark.usefixtures("no_verifier")
def test_the_base_a_finished_event_keeps_is_what_the_ending_reads_moved_files_from(git_repo, store):
    """`orchestrate` keeps the work's base on its `finished` event; `hooks._found_before` reads
    what the checkout's branch changed since from that field alone."""
    from locks import hooks
    from locks.orchestrate import run

    from .conftest import git
    from .test_orchestrate import Script, kind, ok

    found = {"where": "landing.md:1", "what": "says no binaries", "evidence_class": "deterministic",
             "cause": "pre-existing", "proof": "git ls-remote --tags"}
    report = run("write the radio tests", git_repo, store, Options(), registry={},
                 launcher=Script(dataclasses.replace(ok(), findings=(found,)), write="t.py"),
                 classifier=kind("test"))
    base = store.final_event(report.task_id)["data"]["base"]
    assert base == git(git_repo, "rev-parse", "HEAD")
    assert "landing.md:1" in "\n".join(hooks._found_before(store, store.get_task(report.task_id)))
    (git_repo / "landing.md").write_text("binaries are published\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "fix the landing")
    assert "landing.md:1" not in "\n".join(hooks._found_before(store, store.get_task(report.task_id)))


def test_the_lines_a_plan_kept_quiet_are_left_out_of_the_ending_s_warnings(git_repo, store):
    """`orchestrate` keeps the lines a repository already heard today on `planned.quiet`;
    `hooks._warned` leaves them out from that field alone."""
    from locks import hooks

    task = store.create_task("t", status="done", source="locks")
    store.add_event(task["id"], "planned", warnings=["CI also runs: e2e", "this task's check exits 0"],
                    quiet=["CI also runs: e2e"])
    assert hooks._warned(store, task["id"]) == ["this task's check exits 0"]


def test_the_pickup_causes_a_run_keeps_are_what_stats_reads(store):
    """`orchestrate.pickup_causes` writes `pickup_*_s` on the `finished` event; `analysis`
    reads them back as each run's `pickup_why` and adds them up by cause."""
    from locks import analysis, orchestrate

    task = store.create_task("t", status="done", source="queue", repo="r")
    store.add_event(task["id"], "planned", kind="implement", start="sonnet/medium", plan_s=0.1)
    causes = {"pickup_conflict_s": 30.0, "pickup_ceiling_s": 70.0}
    assert set(causes) <= set(orchestrate.PICKUP_CAUSES) == set(analysis.PICKUP_CAUSES)
    store.add_event(task["id"], "finished", status="done", wall_s=10, pickup_s=100.0, **causes)
    report = analysis.compute(store, days=0)
    assert report["pickup"]["seconds"]["pickup_conflict_s"] == 30.0
    assert report["pickup"]["share"]["pickup_ceiling_s"] == 0.7


def test_the_rows_a_fix_s_check_answers_are_in_the_words_its_re_check_reads():
    """The fix's independent check answers its rows in the re-check's words, and the engine decides the
    re-check from them only when every row reads `fixed` (`workflows._ledger`)."""
    from locks import ledger

    orchestrate_src = (SRC / "orchestrate.py").read_text()
    workflows_src = (SRC / "workflows.py").read_text()
    assert ledger.WORDS["recheck"][0] == "fixed" and 'ledger_mod.WORDS["recheck"]' in orchestrate_src
    assert '"verdicts": list(answered.values())' in orchestrate_src
    assert 'checked.get("verdicts")' in workflows_src and '== "fixed"' in workflows_src


def test_the_cell_a_check_or_a_sampled_pass_names_is_the_one_trust_reads_back(git_repo, store, monkeypatch):
    """`orchestrate` keeps on `verified` and `verifier_sampled` the worker's cell (`worker`); `trust`
    reads a cell's record, and with it its sampling, from that field."""
    from locks import trust
    from locks.orchestrate import run

    from .test_checking import check, passing
    from .test_orchestrate import Script, kind

    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    haiku = Cell("haiku", "high")
    report = run("rename the helper in notes", git_repo, store, Options(start=haiku), registry={},
                 launcher=Script(passing(), check("pass"), write="notes.txt"), classifier=kind("implement"),
                 adapters=[])
    found, = trust.inspections(store, None)
    assert (found.task, found.cell, found.outcome) == (report.task_id, "haiku/high", "clean")
    store.add_event(report.task_id, "verifier_sampled", seq=1, worker="haiku/high", every=2)
    assert [i.outcome for i in trust.inspections(store, None)] == ["clean", "sampled"]


def test_the_snapshot_a_check_keeps_is_the_one_the_next_round_reads_from(git_repo, store, monkeypatch):
    """`_judged` keeps `read_at` and the worker's cell on `verified`; `_read_before` reads them back."""
    from locks import orchestrate
    from locks.orchestrate import run

    from .test_checking import BUG, check, passing
    from .test_orchestrate import Script, kind

    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    report = run("fix the cart total", git_repo, store, registry={}, classifier=kind("implement"),
                 launcher=Script(passing(), check("fail", [BUG]), passing("fixed"), check("pass"), write="app.py"))
    first = store.events_of(report.task_id, "verified")[0]["data"]
    assert first["worker"] == "sonnet/medium" and first["read_at"]
    task = store.create_task("t", status="running", source="locks", repo="r", kind="implement")
    store.add_event(task["id"], "verified", **first)
    since = orchestrate._read_before(store, task["id"], Cell("sonnet", "high"), 1, git_repo)
    assert since is not None and since[0] == first["read_at"]


def test_the_evidence_arm_a_brief_keeps_is_the_one_its_trial_reads_back(git_repo, store, monkeypatch):
    """`orchestrate` keeps a task's arm on `briefed` (`evidence.KEY`); `evidence.trial` counts arms from it."""
    from locks import capabilities as caps
    from locks import evidence
    from locks.orchestrate import run

    from .test_checking import passing
    from .test_orchestrate import Script, kind

    monkeypatch.setenv("LOCKS_EVIDENCE", "on")
    monkeypatch.setattr(evidence, "arm", lambda store, repo, task_id: ("free", "free for the test"))
    registry = {"rastro": caps.Capability("rastro", {"command": "rastro"}, hint="drive the app")}
    report = run("Rename the helper in notes", git_repo, store, registry=registry,
                 launcher=Script(passing(), write="notes.txt"), classifier=kind("implement"))
    task = store.get_task(report.task_id)
    found = evidence.trial(store, task["repo"])
    assert found["free"]["tasks"] == 1 and found["gate"]["tasks"] == 0


#: Keys read with `.get` from data that comes from outside locks, never from its own events: a
#: neighbour's manifest (`format`, `hints`) and a request's body to the UI (`note`).
OUTSIDE_KEYS = {"format", "hints", "note"}


def test_every_field_read_from_an_event_is_one_some_place_writes():
    """A field read from an event's data (`data.get("x")`) that nothing writes is a contract one side
    dropped: the reader sees None forever and says nothing. Read statically from the source."""
    import ast

    written: set[str] = set()
    read: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                func = node.func
                if (isinstance(func, ast.Attribute) and func.attr in ("add_event", "update", "replace")) or (
                        isinstance(func, ast.Name) and func.id == "dict"):
                    written.update(kw.arg for kw in node.keywords if kw.arg)
                    for kw in node.keywords:
                        if kw.arg is None and isinstance(kw.value, ast.Dict):
                            written.update(k.value for k in kw.value.keys if isinstance(k, ast.Constant))
                if (isinstance(func, ast.Attribute) and func.attr == "get" and node.args
                        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                        and "data" in ast.unparse(func.value)):
                    read.setdefault(node.args[0].value, set()).add(path.name)
            elif isinstance(node, ast.Dict):
                written.update(k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str))
            elif (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
                  and isinstance(node.slice, ast.Constant)):
                written.add(node.slice.value)
    dropped = {key: sorted(where) for key, where in read.items() if key not in written and key not in OUTSIDE_KEYS}
    assert dropped == {}, dropped


def test_the_livespec_tools_a_brief_offered_are_what_stats_read_back(store):
    from locks import analysis

    # The key the run writes on `briefed` (orchestrate) is the one the analysis reads.
    assert re.search(r'"livespec_tools":\s*sorted\(', (SRC / "orchestrate.py").read_text())
    task = store.create_task("t", status="done", source="locks", repo="r", kind="implement")
    store.add_event(task["id"], "planned", plan_s=0.1)
    store.add_event(task["id"], "briefed", livespec_tools=["search_specs", "who_calls"])
    store.add_event(task["id"], "attempt_finished", seq=1, passed=True, livespec_calls={"who_calls": 2})
    store.add_event(task["id"], "finished", status="done", wall_s=1.0)
    events = store.events_of_kinds(["planned", "briefed", "attempt_finished", "finished"])
    offer = analysis.livespec_offer(analysis.runs([store.get_task(task["id"])], events))
    assert offer["tools"] == {"search_specs": {"offered": 1, "called": 0}, "who_calls": {"offered": 1, "called": 1}}
