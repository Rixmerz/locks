"""The Workflows and Map screens: what the API hands them, and the one press they make."""
from __future__ import annotations

import json

import pytest

from locks import flow, project, repo, workflows
from locks.store import Store
from locks.ui import api

from .test_orchestrate import ok
from .test_ui import call, token, ui  # noqa: F401  (the ui fixture)
from .test_workflows import CHAIN, _runner, _save


@pytest.fixture
def mapped(git_repo, store: Store, kicks):
    """A project with notes, a link, an anchor, a problem with fixes, a finished run and a decision."""
    project.enroll(git_repo)
    key = repo.key(git_repo.resolve())
    zone = store.add_note(key, "conventions", "Zones", "Zone ids are UTC offsets.", author="person", filed_by="person")
    rule = store.add_note(key, "decisions", "Money is cents", "All money is integer cents.", author="person",
                          filed_by="person")
    store.link_notes(rule, zone, "depends_on")
    store.add_anchor(zone, "src/zones.py", symbol="parse_zone")
    problem = store.open_problem("Totals off by a cent", repo=key)
    store.add_fix(problem, "round each line", outcome="failed", repo=key, why="the error adds up")
    store.add_note("other/repo", "conventions", "Elsewhere", "Not this project's.", author="person", filed_by="person")
    _save(CHAIN, git_repo)
    run = workflows.start(store, "chain", git_repo, {"goal": "the export"})
    flow.work(store, runner=_runner([ok(), ok(), ok()], []))
    return {"key": key, "zone": zone, "rule": rule, "problem": problem, "run": run["id"], "repo": git_repo}


def test_workflows_view_draws_runs_with_their_progress_and_the_definitions(mapped, store: Store):
    view = api.workflows_view(store, mapped["key"])
    [run] = view["runs"]
    assert run["id"] == mapped["run"] and run["progress"] == {"done": 3, "total": 3} and run["state"] == "done"
    assert [s["needs"] for s in run["steps"]] == [[], ["build"], ["check"]]
    names = {d["name"]: d for d in view["definitions"]}
    assert {"chain", "feature", "bugfix", "review-fix"} <= set(names)
    assert names["chain"]["version"] == 1 and names["chain"]["held_by"] == [] and names["chain"]["pending"] is False
    assert [s["id"] for s in names["chain"]["steps"]] == ["build", "check", "polish"]
    assert names["chain"]["record"]["runs"] == 1 and names["chain"]["record"]["done"] == 1
    assert names["chain"]["record"]["cost_p50"] is not None and names["feature"]["record"] is None
    assert api.workflows_view(store, "nothing/here")["runs"] == []


def test_the_map_is_one_project_s_notes_links_anchors_and_problems(mapped, store: Store):
    view = api.map_view(store, mapped["key"])
    by = {n["id"]: n for n in view["nodes"]}
    assert by["root"]["parent"] is None
    assert by[f"note:{mapped['zone']}"]["parent"] == "topic:conventions"
    assert by[f"anchor:{mapped['zone']}:src/zones.py"]["label"] == "parse_zone (src/zones.py)"
    assert view["edges"] == [{"from": f"note:{mapped['rule']}", "to": f"note:{mapped['zone']}", "kind": "depends_on"}]
    assert by[f"problem:{mapped['problem']}"]["parent"] == "problems"
    fix = next(n for n in view["nodes"] if n["kind"] == "fix")
    assert fix["state"] == "failed" and fix["text"] == "the error adds up"
    assert not any(n["label"] == "Elsewhere" for n in view["nodes"])  # another project's note
    assert view["left_out"] == {"notes": 0, "problems": 0}
    assert api.map_view(store, "nothing/here")["nodes"] == [{"id": "root", "parent": None, "kind": "project",
                                                              "label": "nothing/here"}]


def test_milestones_come_from_what_happened(mapped, store: Store):
    store.create_task("add the CSV export", status="done", source="locks", repo=mapped["key"], cwd="/x",
                      kind="feature", final_cell="sonnet/xhigh")
    store.create_task("fix a typo", status="done", source="locks", repo=mapped["key"], cwd="/x", kind="docs")
    kinds = [(m["kind"], m["title"]) for m in api.milestones(store, mapped["key"])]
    assert ("workflow", "chain — goal: the export") in kinds
    assert ("decision", "Money is cents") in kinds
    assert ("feature", "add the CSV export") in kinds
    assert not any(t == "fix a typo" for _, t in kinds)
    # a step of a workflow run is the run's milestone, not one of its own
    assert sum(1 for k, _ in kinds if k == "workflow") == 1


def test_a_note_is_attached_to_a_step_and_waits_while_the_workflow_is_used(mapped, store: Store):
    saved = api.attach_note(store, mapped["key"], "chain", "build", mapped["zone"])
    assert saved["held_by"] == []
    steps = workflows.load("chain", mapped["repo"])["steps"]
    assert steps[0]["memory"] == [f"note:{mapped['zone']}"]
    api.attach_note(store, mapped["key"], "chain", "build", mapped["zone"])  # twice is once
    assert workflows.load("chain", mapped["repo"])["steps"][0]["memory"] == [f"note:{mapped['zone']}"]
    held = workflows.start(store, "chain", mapped["repo"], {"goal": "again"})
    saved = api.attach_note(store, mapped["key"], "chain", "polish", mapped["rule"])
    assert saved["held_by"] == [held["id"]] and ".pending" in saved["path"]
    elsewhere = store.add_note("other/repo", "zones", "x", "y", author="person", filed_by="person")
    with pytest.raises(workflows.WorkflowError, match="no note"):
        api.attach_note(store, mapped["key"], "chain", "build", elsewhere)
    with pytest.raises(workflows.WorkflowError, match="no step"):
        api.attach_note(store, mapped["key"], "chain", "ghost", mapped["zone"])


def test_the_screens_routes_read_and_the_attach_needs_the_token(mapped, ui):  # noqa: F811
    key = mapped["key"]
    for path in ("/api/workflows", "/api/map", "/api/milestones"):
        res, data = call(ui, "GET", f"{path}?repo={key}")
        assert res.status == 200, path
        assert call(ui, "GET", path)[0].status == 400
    for asset in ("/static/workflows.js", "/static/map.js", "/static/svg.js"):
        assert call(ui, "GET", asset)[0].status == 200
    body = {"repo": key, "workflow": "chain", "step": "check", "note": mapped["zone"]}
    assert call(ui, "POST", "/api/workflows/attach", body)[0].status == 403
    res, data = call(ui, "POST", "/api/workflows/attach", body, headers={"X-Locks-Token": token(ui)})
    assert res.status == 200 and json.loads(data)["held_by"] == []
    res, data = call(ui, "POST", "/api/workflows/attach", {**body, "note": 999},
                     headers={"X-Locks-Token": token(ui)})
    assert res.status == 409 and b"no note" in data
