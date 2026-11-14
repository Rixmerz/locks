"""TRACER as the way a project is built: its phase read from docs/tracer/, a slice's Definition of Ready
checked before any worker, the person's switch, and the line a build request hears."""
from __future__ import annotations

import json

import pytest

from locks import hooks, method, project, repo, stops, workflows
from locks.cli import main
from locks.orchestrate import Options, run

from .conftest import git
from .test_orchestrate import Script, kind
from .test_tracer import _tracer


def _write(folder, name, text="written\n"):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _complete(repo_dir):
    """A project whose every phase wrote its files, with a slice plan of three slices."""
    _tracer(repo_dir)
    folder = repo_dir / "docs" / "tracer"
    for name in ("contract.md", "identity.md", "design-system.md", "empathy/find-material.md"):
        _write(folder, name)
    _write(folder, "capabilities.md", "# Capabilities\n- CAP-1: find material — search by title\n")
    _write(folder, "slices.md", "| Slice | Capability | Status |\n|---|---|---|\n"
                                "| skeleton | find material | done |\n| find material by title | find material |"
                                " in progress |\n| progress bars | track progress | planned |\n")
    _write(repo_dir, "openspec/specs/find/spec.md", "### Requirement: find\n#### Scenario: by title\n")
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-q", "-m", "method")


def test_a_project_without_the_folder_is_before_the_contract(tmp_path):
    found = method.state(tmp_path)
    assert not found.present and found.phase.id == "contract"
    assert "tracer-contract" in found.advice() and "person decides" in found.advice()


def test_the_phase_is_the_first_whose_files_are_not_all_there(git_repo):
    _tracer(git_repo)  # flows, floors, anti-patterns and audiences: no contract yet
    found = method.state(git_repo)
    assert found.phase.id == "contract" and found.missing["contract"] == ["contract.md"]
    assert found.missing["flows"] == []
    _write(git_repo / "docs" / "tracer", "contract.md")
    found = method.state(git_repo)
    assert found.phase.id == "spec" and "tracer-spec" in found.advice()
    lines = "\n".join(found.lines())
    assert "done   contract" in lines and "now    spec" in lines and "missing: capabilities.md" in lines


def test_an_empty_file_or_folder_is_not_a_phase_done(git_repo):
    folder = git_repo / "docs" / "tracer"
    _write(folder, "contract.md", "\n  \n")
    (folder / "empathy").mkdir()
    found = method.state(git_repo)
    assert "contract.md" in found.missing["contract"] and found.missing["empathy"] == ["empathy/"]


def test_a_complete_project_points_at_its_next_slice(git_repo):
    _complete(git_repo)
    found = method.state(git_repo)
    assert found.phase is None
    assert [(s.name, s.status) for s in found.slices] == [
        ("skeleton", "done"), ("find material by title", "in progress"), ("progress bars", "planned")]
    assert 'next is "find material by title"' in found.advice()
    assert "slices: 1 done, 1 in progress, 1 planned" in found.lines()[-1]


def test_checkbox_slices_and_a_finished_plan(git_repo):
    _write(git_repo / "docs" / "tracer", "slices.md", "- [x] skeleton\n- [X] search\n")
    assert [s.status for s in method.slices(git_repo)] == ["done", "done"]
    _complete(git_repo)
    _write(git_repo / "docs" / "tracer", "slices.md", "- [x] skeleton\n")
    assert "every planned slice is done" in method.state(git_repo).advice()


def test_a_slice_is_ready_only_when_its_files_meet_the_definition(git_repo):
    _complete(git_repo)
    assert method.unready(git_repo, "find material by title") == []
    missing = method.unready(git_repo, "export the reports as PDF")
    assert any("capability" in m for m in missing) and any("critical flow" in m for m in missing)


def test_what_a_slice_lacks_is_named_file_by_file(git_repo):
    _tracer(git_repo)  # no specs, no empathy round, no capability map
    missing = method.unready(git_repo, "find material by title")
    assert len(missing) == 3
    assert "openspec/" in missing[0] and "empathy" in missing[1] and "capabilities.md" in missing[2]


def test_a_slice_that_is_not_ready_stops_before_any_worker(git_repo, store):
    _tracer(git_repo)
    script = Script()
    report = run("Build this TRACER slice: find material by title", git_repo, store, Options(ready="tracer"),
                 registry={}, launcher=script, classifier=kind("feature"))
    assert script.specs == [] and report.status == "blocked"
    assert report.stop.cause == "unready" and "empathy round" in report.stop.reason
    assert store.last_event(report.task_id, "unready")["data"]["missing"] == report.stop.extra["missing"]
    assert "Definition of Ready" in stops.what_to_do(report.stop)


def test_the_bundled_slice_step_carries_the_ready_check_to_its_task(git_repo, store):
    _complete(git_repo)
    defn = workflows.load("tracer-slice", git_repo)
    assert next(s for s in defn["steps"] if s["id"] == "build")["ready"] == "tracer"
    started = workflows.start(store, "tracer-slice", git_repo, {"slice": "find material by title"})
    task = store.get_task(started["queued"][0])
    assert json.loads(task["options"])["ready"] == "tracer"


def test_a_step_ready_holds_only_the_method_name():
    with pytest.raises(workflows.WorkflowError, match="ready is"):
        workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "p", "ready": "yes"}]})


def test_the_person_puts_a_project_on_the_method_and_reads_its_phase(git_repo, store, capsys):
    _complete(git_repo)
    assert main(["method", "--repo", str(git_repo)]) == 0
    assert "method: none" in capsys.readouterr().out
    assert main(["method", "tracer", "on", "--repo", str(git_repo)]) == 0
    out = capsys.readouterr().out
    assert "method: TRACER" in out and "done   slices" in out and "next is" in out
    assert method.on(store, repo.key(git_repo))
    assert main(["method", "tracer", "off", "--repo", str(git_repo)]) == 0
    assert not method.on(store, repo.key(git_repo))


def test_a_build_request_on_the_method_hears_the_phase_it_skips(git_repo, store, monkeypatch):
    project.enroll(git_repo)
    _tracer(git_repo)
    key = repo.key(git_repo)
    assert hooks._method_line(store, key, str(git_repo), "Add a search box to the material screen") is None
    method.set_on(store, key, True)
    line = hooks._method_line(store, key, str(git_repo), "Add a search box to the material screen")
    assert line.startswith("TRACER: this project is in its contract phase") and "tracer-contract" in line
    # A question, or work that is no build, hears nothing.
    assert hooks._method_line(store, key, str(git_repo), "why is the search slow?") is None
    assert hooks._method_line(store, key, str(git_repo), "Write tests for the search box") is None


def test_every_phase_names_a_bundled_workflow_whose_writing_steps_stay_in_the_method_files(tmp_path):
    """The spec, empathy, design and slice phases write documents only: a step that writes is held to
    docs/tracer/ and openspec/ by its surfaces, so no worker of those phases writes code."""
    for name in {p.workflow for p in method.PHASES} | {"tracer-slice"}:
        defn = workflows.load(name, tmp_path)
        if name == "tracer-slice":
            continue
        for step in defn["steps"]:
            if step.get("kind") in ("docs", "implement", "feature"):
                assert step.get("surfaces"), (name, step["id"])
                assert all(s.startswith(("docs/tracer/", "openspec/")) for s in step["surfaces"]), (name, step["id"])
    assert any("ask" in s for s in workflows.load("tracer-contract", tmp_path)["steps"])


def test_the_record_counts_the_expectations_a_slice_s_verifier_found_no_spec_covered(git_repo, store, capsys):
    key = repo.key(git_repo)
    findings = [{"where": "material screen", "what": "an escaped expectation: the list shows no thumbnails",
                 "cause": "change"}]
    for n, (title, found) in enumerate((("[tracer-slice#1 verify] find", findings),
                                        ("[tracer-slice#2 verify] progress", []),
                                        ("[feature#3 verify] other", findings)), start=1):
        task = store.create_task("verify", status="done", source="queue", author="workflow", repo=key,
                                 cwd=str(git_repo), title=title, workflow_run=n, workflow_step="verify")
        store.add_event(task["id"], "finished", status="done", findings=found)
    kept = method.record(store, key)
    assert (kept.verified, kept.clean, kept.escaped) == (2, 1, 1) and "thumbnails" in kept.examples[0]
    assert "escaped expectations: 1" in kept.line()
    assert main(["method", "--repo", str(git_repo)]) == 0
    assert "slices verified: 2 (1 clean)" in capsys.readouterr().out
    assert method.Record().line() is None
