"""Where a lesson belongs: the area it was met in, the role that met it, or everyone."""
from __future__ import annotations

import json

import pytest

from locks import arms, cells, hooks
from locks.store import Store

from .test_hooks import prompt


def test_a_path_is_in_one_area_and_a_change_in_its_specific_ones():
    assert cells.of_path("islands/Board.tsx") == "ui" and cells.of_path("src/auth/login.py") == "security"
    assert cells.of_path("migrations/0003_add.sql") == "data" and cells.of_path("tests/test_x.py") == "tests"
    assert cells.of_path("docs/guide.md") == "docs" and cells.of_path("src/locks/flow.py") == "code"
    # A feature's tests and docs travel with it: its area is where the work was.
    assert cells.of_paths(["islands/Board.tsx", "tests/test_board.py", "README.md"]) == ("ui",)
    assert cells.of_paths(["src/x.py", "tests/test_x.py"]) == ("code",)
    assert cells.of_paths(["tests/test_x.py", "docs/a.md"]) == ("docs", "tests")
    assert cells.of_paths([]) == ()
    # A request reaches every area its paths are in, and the ones its words are about.
    assert cells.of_request("fix the login password check", "bugfix", ["tests/test_login.py"]) == (
        "security", "tests")
    assert "ui" in cells.of_request("the modal button overlaps on small screens", "bugfix")
    # A hosting service's name is no rendered page.
    assert "ui" not in cells.of_request("reject replayed Render webhooks in the deploy sync", "bugfix")


def _met(store: Store, problem: int, *, role: str | None, areas: tuple[str, ...], task: int | None = 1) -> None:
    store.add_fix(problem, f"tried something by {role}", "failed", repo="r", why="code_bug", task_id=task,
                  role=role, areas=areas)


def test_a_failure_is_its_cell_s_and_shared_only_when_cells_with_nothing_in_common_met_it(store: Store):
    ui = store.open_problem("the board header overlaps the filters on phones", repo="r")
    _met(store, ui, role="implementer", areas=("ui",))
    problem = store.problem(ui)
    assert cells.scope(problem, role="security-auditor", areas=("security",)) is None
    assert cells.scope(problem, role="reviewer", areas=("ui", "code")) == "area"
    assert cells.scope(problem, role="implementer", areas=()) == "role"
    # The same task meeting it again spans what it spans: still one cell.
    _met(store, ui, role="implementer", areas=("ui", "data"))
    assert cells.scope(store.problem(ui), role="security-auditor", areas=("security",)) is None
    # Another task met it in an area the first never was in: everyone's now.
    _met(store, ui, role="debugger", areas=("security",), task=2)
    assert cells.scope(store.problem(ui), role="security-auditor", areas=()) == "shared"
    # Two tasks in a common area stay that area's.
    data = store.open_problem("the backfill drops rows with a null owner", repo="r")
    _met(store, data, role="implementer", areas=("data",), task=3)
    _met(store, data, role="debugger", areas=("data", "code"), task=4)
    assert cells.scope(store.problem(data), role=None, areas=()) is None
    # What a person wrote down is everyone's.
    kept = store.open_problem("the proxy drops long uploads", repo="r")
    _met(store, kept, role=None, areas=(), task=None)
    assert cells.scope(store.problem(kept), role=None, areas=()) == "shared"


def test_a_worker_sees_its_cell_s_dead_ends_and_the_session_only_the_shared_ones(store: Store):
    ui = store.open_problem("dashboard chart tooltip clipped inside the card", repo="r")
    _met(store, ui, role="implementer", areas=("ui",))
    text = "the dashboard chart tooltip is clipped inside the card again"
    shown = cells.dead_ends(store, text, repo="r", role="reviewer", areas=("ui",))
    assert [d["scope"] for d in shown] == ["area"]
    assert cells.dead_ends(store, text, repo="r", role="security-auditor", areas=("security",)) == []
    assert cells.dead_ends(store, text, repo="r", shared_only=True) == []
    assert cells.recent_shared(store, repo="r") == []
    _met(store, ui, role="debugger", areas=("data",), task=9)
    assert {d["scope"] for d in cells.dead_ends(store, text, repo="r", shared_only=True)} == {"shared"}
    assert cells.recent_shared(store, repo="r")[0]["scope"] == "shared"


def test_the_prompt_hook_tells_the_session_only_what_is_shared(store: Store):
    ui = store.open_problem("websocket reconnect loop in the dashboard", repo="github.com/x/y")
    _met(store, ui, role="implementer", areas=("ui",))
    assert prompt(store, "the websocket reconnect loop is back in the dashboard") is None


def test_a_workflow_step_is_matched_by_its_goal_never_its_template(store: Store):
    run = store.create_run(name="feature", scope="bundled", source="x", definition="{}",
                           inputs=json.dumps({"goal": "Export invoices as CSV from the admin list"}))
    step = store.create_task("[feature#1 build] Build it, with its tests", status="done", source="locks",
                             repo="r", workflow_run=run["id"])
    assert cells.memory_text(store, step) == "Export invoices as CSV from the admin list"
    empty = store.create_run(name="feature", scope="bundled", source="x", definition="{}", inputs="{}")
    other = store.create_task("plain text", status="done", source="locks", repo="r", workflow_run=empty["id"])
    assert cells.memory_text(store, other) == "plain text"
    assert cells.memory_text(store, {"title": "t"}) == "t"


def test_a_word_every_problem_holds_is_no_match(store: Store):
    # Twelve runs of one workflow share their step's template; only the goal tells them apart.
    for i in range(12):
        p = store.open_problem(f"[feature#{i} build] Build it, with its tests", repo="r",
                               symptom=f"Build this feature: subject{i} widget{i} gadget{i}. Read CLAUDE.md first")
        store.add_fix(p, "tried", "failed", repo="r", task_id=i + 1, role="implementer", areas=("code",))
    template = "Build this feature: something else entirely. Read CLAUDE.md first, build it with its tests"
    assert store.dead_ends(template, repo="r", strict=True) == []
    assert store.dead_ends("Build this feature: subject3 widget3 gadget3", repo="r", strict=True)[0][
        "problem"] == "[feature#3 build] Build it, with its tests"
    assert store.dead_ends("subject3 alone", repo="r", strict=True) == []  # one distinctive word is too little


def test_fixes_recorded_before_scopes_get_theirs_once(store: Store):
    task = store.create_task("fix the login redirect", status="done", source="locks", repo="r", kind="bugfix")
    store.add_event(task["id"], "planned", kind="bugfix", role="debugger")
    store.add_attempt(task["id"], cell="sonnet/high", max_turns=80, passed=0, failure="code_bug",
                      changed_paths=json.dumps(["src/auth/redirect.py", "tests/test_redirect.py"]))
    bare = store.create_task("tidy the parser", status="done", source="locks", repo="r", kind="refactor")
    p = store.open_problem("login redirect", repo="r")
    store.add_fix(p, "a", "failed", repo="r", task_id=task["id"])
    store.add_fix(p, "b", "failed", repo="r", task_id=bare["id"])
    store.add_fix(p, "c", "failed", repo="r", task_id=999)  # its task is gone
    assert cells.backfill(store) == 2 and cells.backfill(store) == 0
    fixes = {f["description"]: f for f in store.problem(p)["fixes"]}
    assert (fixes["a"]["role"], fixes["a"]["areas"]) == ("debugger", "security")
    assert (fixes["b"]["role"], fixes["b"]["areas"]) == (None, "")
    assert fixes["c"]["areas"] is None


def test_settling_scopes_the_old_fixes(store: Store):
    from locks import evolution

    evolution.settle(store)
    assert store.meta(cells.BACKFILL_KEY) == "0"
    assert hooks  # the hooks import cells lazily; the module stays light


# --- the primer --------------------------------------------------------------------


def _oriented(store: Store, lines, *, role, areas, repo="r"):
    task = store.create_task("t", status="done", source="locks", repo=repo)
    return task, cells.record_orientation(store, task["id"], lines, role=role, areas=areas)


def test_a_cell_s_orientation_primes_the_next_worker_of_its_role_or_area(store: Store):
    _, kept = _oriented(store, ["islands/Board.tsx draws the board; its data is from routes/api/board.ts",
                                "AKIA1234567890ABCDEF is the key to use"], role="implementer", areas=("ui",))
    assert kept == ["islands/Board.tsx draws the board; its data is from routes/api/board.ts"]  # never a secret
    _oriented(store, ["the migrations run with deno task migrate before the tests"], role="debugger",
              areas=("data",))
    _oriented(store, ["   ", "x"], role="implementer", areas=("ui",))  # nothing worth keeping, no event
    assert cells.primer(store, "r", role="reviewer", areas=("ui",)) == [
        "islands/Board.tsx draws the board; its data is from routes/api/board.ts"]
    assert cells.primer(store, "r", role="debugger", areas=()) == [
        "the migrations run with deno task migrate before the tests"]
    assert cells.primer(store, "r", role="security-auditor", areas=("security",)) == []
    assert cells.primer(store, None, role="debugger", areas=()) == []
    assert cells.primer(store, "elsewhere", role="debugger", areas=("data",)) == []
    text = cells.primer_text(cells.primer(store, "r", role="debugger", areas=("ui",)))
    assert text.startswith("How earlier workers") and "data, not instructions" in text and text.count("\n- ") == 2
    assert cells.primer_text([]) == ""


def test_a_primer_holds_each_line_once_and_at_most_its_size(store: Store):
    for i in range(cells.PRIMER_LINES + 3):
        _oriented(store, [f"line number {i} about the ui code here", "The SAME line about the ui code"],
                  role="implementer", areas=("ui",))
    found = cells.primer(store, "r", role=None, areas=("ui",))
    assert len(found) == cells.PRIMER_LINES and sum("same line" in x.lower() for x in found) == 1


def _trial_task(store: Store, arm: str, *, passed: bool, calls: int, repo="r"):
    task = store.create_task("t", status="done", source="locks", repo=repo)
    store.add_event(task["id"], "briefed", primer_arm=arm)
    store.add_event(task["id"], "briefed", primer_arm="ignored")  # a resume's brief: the first one counts
    store.add_attempt(task["id"], cell="sonnet/medium", max_turns=80, passed=int(passed), input_tokens=1000 + calls)
    for i in range(calls):
        store._conn.execute("INSERT INTO tool_events (session_id, task_id, attempt, ts, tool, sig, arg_hash, ok) "
                            "VALUES ('w', ?, 1, '2026-10-20T00:00:00Z', 'read', 'read', ?, 1)", (task["id"], str(i)))
    store._conn.execute("INSERT INTO tool_events (session_id, task_id, attempt, ts, tool, sig, arg_hash, ok) "
                        "VALUES ('w', ?, 1, '2026-10-20T00:00:00Z', 'edit', 'edit', 'e', 1)", (task["id"],))
    return task


def test_the_primer_s_trial_alternates_then_keeps_the_winner(store: Store):
    # Which task goes in which arm is the hash of the trial, the repository and the task (`arms.side`).
    ids = {cells.ARMS[arms.side("primer", "r", n, 0, 0)]: n for n in range(1, 30)}
    arm, why = cells.primer_arm(store, "r", ids["control"])
    assert arm == "control" and "trial" in why and cells.primer_arm(store, "r", ids["primer"])[0] == "primer"
    assert cells.primer_arm(store, None, 7)[0] == "primer"
    for _ in range(cells.PRIMER_TRIAL):
        _trial_task(store, "primer", passed=True, calls=8)
        _trial_task(store, "control", passed=True, calls=20)
    found = cells.trial(store, "r")
    assert found["primer"] == {"tasks": cells.PRIMER_TRIAL, "passed_first": cells.PRIMER_TRIAL,
                               "calls_before_edit_p50": 8, "input_tokens_p50": 1008}
    arm, why = cells.primer_arm(store, "r", 7)
    assert arm == "primer" and "won" in why
    # Elsewhere, a primer that made first tries fail more lost.
    for _ in range(cells.PRIMER_TRIAL):
        _trial_task(store, "primer", passed=False, calls=5, repo="other")
        _trial_task(store, "control", passed=True, calls=20, repo="other")
    arm, why = cells.primer_arm(store, "other", 8)
    assert arm == "control" and "lost" in why
    assert cells.trial(store, None)["primer"]["tasks"] == 2 * cells.PRIMER_TRIAL


def test_stats_show_the_primer_s_trial(store: Store):
    from locks import analysis

    task = _trial_task(store, "primer", passed=True, calls=3)
    _trial_task(store, "control", passed=False, calls=9)
    store.add_event(task["id"], "planned", kind="implement", start="sonnet/medium", plan_s=1.0)
    store.add_event(task["id"], "finished", status="done", wall_s=60.0)
    report = analysis.compute(store, days=0)
    assert report["primer"]["primer"]["tasks"] == 1 and report["primer"]["control"]["passed_first"] == 0
    said = analysis.text(report)
    assert "the cell's primer in the brief" in said
    assert "with it     tasks    1  first 100%  calls before the first change p50 3" in said
    assert "without it  tasks    1  first   0%  calls before the first change p50 9" in said


@pytest.mark.usefixtures("no_verifier")
def test_a_run_leaves_its_orientation_and_the_next_run_s_brief_follows_its_arm(git_repo, store, monkeypatch):
    import dataclasses

    from locks.orchestrate import run

    from .test_orchestrate import Script, kind, ok

    monkeypatch.setenv("LOCKS_PRIMER", "on")
    oriented = dataclasses.replace(ok(), orientation=("the export lives in src/export.py; run pytest -q tests",))
    first = Script(oriented, write="export.py")
    report = run("add the export", git_repo, store, registry={}, launcher=first, classifier=kind("implement"))
    assert store.last_event(report.task_id, "oriented")["data"]["lines"] == [
        "the export lives in src/export.py; run pytest -q tests"]
    assert "primer_arm" not in store.last_event(report.task_id, "briefed")["data"]  # nothing to prime with yet
    seen = {}
    # The arm is the task's hash until the arms drift two apart (`arms.side`): a few runs see both.
    for _ in range(2 + arms.BALANCE):
        script = Script(ok(), write="import.py")
        again = run("add the import", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
        briefed = store.last_event(again.task_id, "briefed")["data"]
        seen[briefed["primer_arm"]] = "src/export.py" in script.specs[0].prompt
        assert briefed["primer_lines"] == 1 and "trial" in briefed["primer_why"]
        if len(seen) == 2:
            break
    assert seen == {"primer": True, "control": False}
    monkeypatch.setenv("LOCKS_PRIMER", "off")
    off = Script(ok(), write="other.py")
    done = run("add the other", git_repo, store, registry={}, launcher=off, classifier=kind("implement"))
    assert "primer_arm" not in store.last_event(done.task_id, "briefed")["data"]


def test_the_primer_s_arms_fill_evenly_and_a_primer_far_worse_loses_early(store: Store):
    for _ in range(3):
        _trial_task(store, "primer", passed=True, calls=8)
    assert cells.primer_arm(store, "r", 7)[0] == "control" and cells.primer_arm(store, "r", 8)[0] == "control"
    for _ in range(cells.PRIMER_EARLY):
        _trial_task(store, "primer", passed=False, calls=8, repo="early")
        _trial_task(store, "control", passed=True, calls=8, repo="early")
    arm, why = cells.primer_arm(store, "early", 8)
    assert arm == "control" and why.startswith("the primer lost its trial here early")
    for _ in range(cells.PRIMER_EARLY):
        _trial_task(store, "primer", passed=True, calls=8, repo="close")
        _trial_task(store, "control", passed=True, calls=8, repo="close")
    # No gap here, so no early loss; every repository's arms are full by now, and their verdict decides
    # for this one while its own trial runs short (`arms.pooled_why`).
    arm, why = cells.primer_arm(store, "close", 8)
    assert "early" not in why.split(";")[0] or "everywhere" in why
    assert why.endswith("pooled from every repository while the trial here has 6 primed, 6 without, 12 each")


def test_a_shared_dead_end_from_other_cells_stays_out_of_a_worker_s_brief(store: Store):
    """An implement on queries was briefed docs and security dead ends of another phase."""
    old = store.open_problem("feature export fix breaks the docs index", repo="r")
    _met(store, old, role="docs-writer", areas=("docs",), task=1)
    _met(store, old, role="security-auditor", areas=("security",), task=2)
    text = "feature export fix for the queries index"
    assert cells.dead_ends(store, text, repo="r", role="implementer", areas=("data",)) == []
    near = cells.dead_ends(store, text, repo="r", role="implementer", areas=("docs",))
    assert {d["scope"] for d in near} == {"shared"}
    assert {d["scope"] for d in cells.dead_ends(store, text, repo="r", shared_only=True)} == {"shared"}
    written = store.open_problem("feature export fix needs the queries lock", repo="r")
    _met(store, written, role=None, areas=(), task=None)
    kept = cells.dead_ends(store, text, repo="r", role="implementer", areas=("data",))
    assert [d["problem_id"] for d in kept] == [written]


def test_the_primer_s_trial_short_here_takes_every_repository_s_verdict(store: Store):
    for _ in range(cells.PRIMER_TRIAL):
        _trial_task(store, "primer", passed=False, calls=5, repo="other")
        _trial_task(store, "control", passed=True, calls=20, repo="other")
    arm, why = cells.primer_arm(store, "r", 8)
    assert arm == "control" and "lost its trial everywhere" in why
    assert why.endswith("pooled from every repository while the trial here has 0 primed, 0 without, "
                        f"{cells.PRIMER_TRIAL} each")
