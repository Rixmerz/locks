"""The TRACER method carried into the work: the worker's brief and the independent check's."""
from __future__ import annotations

import re

import pytest

from locks import config, tracer, workflows
from locks.orchestrate import run

from .conftest import git
from .test_checking import check, passing
from .test_orchestrate import Script, kind
from .test_plugins import LAYOUT, install

#: The shape of TRACER's own `plugin/templates/critical-flow.md`, filled in: the success path as `-` items,
#: the failure paths as a table whose header row is the only one that says CORRECT.
FLOW = """Flow: find training material by its title
Capability: find material
Audience: learners
Starts at: the material screen
Steps the person takes:
  1. types a title
  2. reads the list

Success path
- WORKS: the search returns the file
- CORRECT: the file shows under its real name with a thumbnail

Failure paths
| Failure | WORKS (system behaviour) | CORRECT (what the person sees and how they recover) |
| Invalid input | 400 | the field says which character is not allowed |
| Network or server error | 503 | the screen says it could not search and offers to try again |
| No permission | 403 | the screen says who can give access |
| Empty data | 200 | the screen says what it is for and the first thing to do |

Runs: end to end through the interface, against a production-like system
Verified by: someone who did not build it
"""


def _tracer(repo):
    folder = repo / "docs" / "tracer"
    (folder / "flows").mkdir(parents=True)
    (folder / "audiences.md").write_text("Learners: no technical words, never a UUID or a path.\n")
    (folder / "anti-patterns.md").write_text("- A storage key or UUID shown to a person.\n")
    (folder / "floors.md").write_text("File list: search by title, real names, empty state.\n")
    (folder / "flows" / "find-material.md").write_text(FLOW)
    (folder / "flows" / "track-progress.md").write_text("Flow: progress\n- CORRECT: a bar per course\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tracer")


def test_flows_are_named_strictly_by_the_request(git_repo):
    _tracer(git_repo)
    assert [p.stem for p in tracer.flows_for(git_repo, "Add search to find training material by title")] == [
        "find-material"]
    assert tracer.flows_for(git_repo, "find the bug in the login") == []
    assert [p.stem for p in tracer.flows_for(git_repo, "learners finding the materials")] == ["find-material"]


def test_a_writing_worker_reads_the_method_and_the_check_holds_the_change_to_its_correct_lines(
        git_repo, store, monkeypatch):
    _tracer(git_repo)
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    script = Script(passing(), check("pass"), check("pass"), write="app.py")
    report = run("Fix the payment of the course so learners can find material", git_repo, store, registry={},
                 launcher=script, classifier=kind("implement"))
    assert report.status == "done"
    worker, verifier, again = (s.prompt for s in script.specs)
    # It answered none of the CORRECT lines: asked once more for them, then said as unanswered.
    assert "Your answer left these CORRECT lines unanswered" in again and "C1:" in again
    checked = store.events_of(report.task_id, "verified")[0]["data"]
    assert checked["tracer_reasked"] == checked["tracer_unanswered"] == ["C1", "C2", "C3", "C4", "C5"]
    assert "follows the TRACER method" in worker and "never a UUID" in worker and "real name" in worker
    assert "track-progress" not in worker  # a flow the request does not name stays out
    assert "CORRECT lines of the flows this request names" in verifier
    assert "real name with a thumbnail" in verifier and "storage key or UUID" in verifier
    assert "first thing to do" in verifier  # a failure path's CORRECT cell
    assert "WORKS (system behaviour)" not in verifier  # the table's header row is no criterion
    # As a word, outside paths: a commit hash may hold the digits "400", and so may a temporary
    # folder (`pytest-400`), which failed this once.
    assert not re.search(r"\b400\b", re.sub(r"/\S+", "", verifier))
    planned = store.last_event(report.task_id, "planned")["data"]
    assert any(r.startswith("tracer: ") for r in planned["reasons"])


@pytest.mark.usefixtures("no_verifier")
def test_a_reading_task_or_the_setting_off_leaves_the_method_out(git_repo, store, monkeypatch):
    _tracer(git_repo)
    script = Script(passing("found it"))
    run("where do learners find material", git_repo, store, registry={}, launcher=script, classifier=kind("explore"))
    assert "TRACER" not in script.specs[0].prompt
    monkeypatch.setenv("LOCKS_TRACER", "off")
    script = Script(passing(), write="app.py")
    run("let learners find material", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert "TRACER" not in script.specs[0].prompt


FLOORS = """- Every control is a 44 px target on a phone.
- Text on the card never overflows or is cut off.
- Layout holds at every breakpoint the audience uses.
- Body text keeps WCAG contrast.
- A storage key never shows.
"""


def _floors(repo):
    _tracer(repo)
    (repo / "docs" / "tracer" / "floors.md").write_text(FLOORS)
    git(repo, "commit", "-q", "-am", "floors")  # a worktree reads what is committed


def _beside(told, text):
    """The tool text on the line of `told` that holds `text`."""
    return next(line for line in told.splitlines() if text in line)


def test_a_check_that_has_layout_inspector_is_told_which_tool_measures_each_floor_and_anti_pattern(git_repo):
    _floors(git_repo)
    told = "\n".join(tracer.verifier_lines(git_repo, "find material", ("layout-inspector",)))
    assert "accessibility_spatial" in _beside(told, "44 px target")
    assert "detect_issues" in _beside(told, "never overflows")
    assert "compare_viewports" in _beside(told, "every breakpoint")
    assert "check_design_system" in _beside(told, "WCAG contrast")
    assert "accessibility_spatial" not in _beside(told, "WCAG contrast")
    assert "2 of the floor and anti-pattern lines name nothing layout-inspector measures" in told
    assert "storage key or UUID" in told  # the catalogue's own line is still the check's to hold
    assert "accessibility_spatial" not in "\n".join(tracer.verifier_lines(git_repo, "find material"))


def test_floors_alone_are_measured_but_say_nothing_without_a_measuring_neighbour(git_repo):
    folder = git_repo / "docs" / "tracer"
    folder.mkdir(parents=True)
    (folder / "floors.md").write_text(FLOORS)
    assert tracer.verifier_lines(git_repo, "x") == []
    assert "compare_viewports" in "\n".join(tracer.verifier_lines(git_repo, "x", ("layout-inspector",)))


def test_a_ui_change_gets_layout_inspector_in_its_check_with_the_method_mapped(git_repo, store, monkeypatch):
    _floors(git_repo)
    install("layout-inspector", "0.4.0", manifest={"mcpServers": "./.claude-plugin/mcp.json"},
            files={".claude-plugin/mcp.json": LAYOUT}, skill="layout-inspection")
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    script = Script(passing(), check("pass"), write="Cart.tsx")
    report = run("fix the cart colours", git_repo, store, launcher=script, classifier=kind("implement"))
    assert report.status == "done"
    verifier = script.specs[1]
    assert "layout-inspector" in verifier.mcp_servers
    assert "layout-inspector: measure the rendered page" in verifier.append_system_prompt
    assert "Floors and anti-patterns, with the tool that measures each" in verifier.prompt
    assert "compare_viewports" in _beside(verifier.prompt, "every breakpoint")


def test_a_ui_change_gets_layout_inspector_in_its_check_without_the_method(git_repo, store, monkeypatch):
    install("layout-inspector", "0.4.0", manifest={"mcpServers": "./.claude-plugin/mcp.json"},
            files={".claude-plugin/mcp.json": LAYOUT}, skill="layout-inspection")
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    script = Script(passing(), check("pass"), write="Cart.tsx")
    report = run("fix the cart colours", git_repo, store, launcher=script, classifier=kind("implement"))
    assert report.status == "done"
    verifier = script.specs[1]
    assert "layout-inspector" in verifier.mcp_servers and "mcp__layout-inspector" in verifier.allowed_tools
    assert "layout-inspector: measure the rendered page" in verifier.append_system_prompt
    assert "TRACER" not in verifier.prompt and "Floors and anti-patterns" not in verifier.prompt
    planned = store.last_event(report.task_id, "planned")["data"]["reasons"]
    assert not any(r.startswith("layout-inspector: ") for r in planned)


@pytest.mark.usefixtures("no_verifier")
def test_the_plan_says_once_why_a_ui_change_is_checked_without_layout_inspector(git_repo, store):
    # An empty registry is the plugin absent, whatever this machine has installed.
    first = run("fix the cart colours in Cart.tsx", git_repo, store, registry={},
                launcher=Script(passing(), write="Cart.tsx"), classifier=kind("implement"))
    said = [r for r in store.last_event(first.task_id, "planned")["data"]["reasons"]
            if r.startswith("layout-inspector: ")]
    assert len(said) == 1 and "not installed" in said[0]
    again = run("fix the cart colours in Cart.tsx once more", git_repo, store, registry={},
                launcher=Script(passing(), write="Cart.tsx"), classifier=kind("implement"))
    planned = store.last_event(again.task_id, "planned")["data"]
    assert said[0] in planned["reasons"] and said[0] in planned["quiet"]  # kept on the event, not said again
    config.save({"plugins": False})
    off = run("fix the layout of Cart.tsx", git_repo, store, registry={}, launcher=Script(passing(), write="Cart.tsx"),
              classifier=kind("implement"))
    assert any("the `plugins` setting is off" in r for r in store.last_event(off.task_id, "planned")["data"]["reasons"])


@pytest.mark.usefixtures("no_verifier")
def test_the_plan_says_nothing_of_layout_inspector_for_work_that_is_no_ui(git_repo, store):
    report = run("fix the cart total in app.py", git_repo, store, registry={},
                 launcher=Script(passing(), write="app.py"), classifier=kind("implement"))
    assert not any(r.startswith("layout-inspector: ")
                   for r in store.last_event(report.task_id, "planned")["data"]["reasons"])


def test_without_the_folder_nothing_is_said(git_repo):
    assert tracer.brief_lines(git_repo, "find material") == [] and tracer.verifier_lines(git_repo, "x") == []


def test_the_bundled_tracer_slice_workflow_is_a_valid_definition(tmp_path):
    found = workflows.load("tracer-slice", tmp_path)
    assert found["scope"] == "bundled" and found["inputs"] == ["slice"]
    assert [s["id"] for s in found["steps"]] == ["build", "verify", "refute", "fix", "recheck"]


def test_the_correct_cells_of_the_failure_table_are_read_by_the_header_and_the_header_is_not_one():
    found = tracer.correct_lines(FLOW)
    assert "the file shows under its real name with a thumbnail" in found
    assert "Empty data: the screen says what it is for and the first thing to do" in found
    assert "Invalid input: the field says which character is not allowed" in found
    assert len(found) == 5
    assert not any("CORRECT" in line or "WORKS" in line or "system behaviour" in line for line in found)


def test_a_table_is_read_by_its_header_wherever_the_column_is_and_with_a_separator_row():
    body = ("| CORRECT | Failure | WORKS |\n| :--- | --- | ---: |\n| the page says it is offline | Network | 503 |\n"
            "| | Empty | 200 |\n")
    assert tracer.correct_lines(body) == ["the page says it is offline"]
    assert tracer.correct_lines("| Failure | WORKS |\n| Empty | CORRECT: said |\n") == []


def test_a_correct_bullet_keeps_the_lines_under_it():
    body = """Success path
- WORKS: stored
- CORRECT: the list shows the file
  under its real name
  - with a thumbnail
  - and a size in MB
- CORRECT:
  - the first row is the newest
  - the empty list says what to do
- WORKS: the status is 200
**CORRECT:** the bold one
CORRECT:
- a bare head
- with items at its own level

Failure paths
"""
    assert tracer.correct_lines(body) == [
        "the list shows the file under its real name", "with a thumbnail", "and a size in MB",
        "the first row is the newest", "the empty list says what to do", "the bold one",
        "a bare head", "with items at its own level"]


def _flow(repo, name, body):
    folder = repo / "docs" / "tracer"
    (folder / "flows").mkdir(parents=True, exist_ok=True)
    (folder / "flows" / f"{name}.md").write_text(body)
    return folder


def test_a_flow_is_named_by_its_own_title_when_the_request_does_not_repeat_the_file_name(tmp_path):
    _flow(tmp_path, "browse-files", FLOW)
    assert tracer.flows_for(tmp_path, "let learners search by title") == []
    assert [p.stem for p in tracer.flows_for(tmp_path, "the way to find the training material by title")] == [
        "browse-files"]
    # the `Capability:` line, and a heading, are titles as well
    assert [p.stem for p in tracer.flows_for(tmp_path, "learners find material faster")] == ["browse-files"]
    _flow(tmp_path, "other", "# Flow: close the account\n- CORRECT: said\n")
    assert [p.stem for p in tracer.flows_for(tmp_path, "the close account button")] == ["other"]


def test_a_title_of_one_word_or_a_generic_one_names_nothing(tmp_path):
    _flow(tmp_path, "pay", "Flow: payment\nCapability: pay\n# Critical flow\n- CORRECT: said\n")
    assert tracer.flows_for(tmp_path, "the payment page and the critical flow of it") == []


def test_a_flow_is_reached_by_the_capability_of_the_map_that_the_request_names(tmp_path):
    folder = _flow(tmp_path, "find-material-by-title", "Flow: search\n- CORRECT: said\n")
    _flow(tmp_path, "track-progress", "Flow: progress\n- CORRECT: a bar\n")
    (folder / "capabilities.md").write_text(
        "# Capabilities\n\n- CAP-1: Find material (REQ-1, REQ-2)\n- **Track progress** — what the learner did\n"
        "\n| Capability | Requirements |\n| --- | --- |\n| Close account | REQ-9 |\n")
    assert tracer.capability_names(tmp_path) == ["Find material", "Track progress", "Close account"]
    # the request has neither the whole file name nor a title, only the capability's name
    assert [p.stem for p in tracer.flows_for(tmp_path, "learners must find material")] == ["find-material-by-title"]
    # a capability the request names with no flow written, or a word of one, reaches nothing
    assert tracer.flows_for(tmp_path, "close account page") == []
    assert tracer.flows_for(tmp_path, "the material list") == []
