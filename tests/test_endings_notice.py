"""The endings notice a session hears: the rules in full once a day, the short line after, a pass in
one verdict line, the criteria not met still shown, and an overlap that names only the files that differ."""

from __future__ import annotations

from locks import hooks, isolate, stops
from locks.store import Store

from .conftest import git

VERDICT = {"check": {"command": "pytest -q", "passed": True},
           "criteria": [{"id": "S1", "met": True, "evidence": "test_export passes"}],
           "independent": {"ran": True, "passed": True}}


def _passed(store: Store, repo, text: str, *, changed=("export.py",), verdict=VERDICT, result="Added the export."):
    """A writing task that passed and is kept on its branch, as its ending records it."""
    task = store.create_task(text, status="running", source="locks", session_id="s", repo="r", cwd=str(repo),
                             final_cell="sonnet/high")
    store.update_task(task["id"], status="done", result=result, final_cell="sonnet/high")
    store.add_event(task["id"], "finished", status="done", final_cell="sonnet/high",
                    branch=f"locks/task-{task['id']}", commit="0" * 40, changed=list(changed),
                    changed_n=len(changed), verdict=verdict)
    return task


def test_a_passed_writing_task_after_the_rules_were_said_is_a_short_notice(store: Store, git_repo):
    _passed(store, git_repo, "add the csv export")
    hooks.endings(store, "s", claim="stop")  # the day's first notice says the rules in full
    _passed(store, git_repo, "add the json export")
    notice = hooks.endings(store, "s", claim="stop")[0]
    assert len(notice) < 700, notice
    assert notice.startswith(hooks.ENDINGS_HEADER + " " + hooks.ENDINGS_SHORT)
    assert "locks guide endings" in notice and hooks.ENDINGS_RULES not in notice
    assert "passed at sonnet/high on locks/task-" in notice
    assert "locks' check `pytest -q` passed, 1/1 criteria met, independent check passed" in notice
    assert "changed 1 file(s): export.py" in notice
    # The record line was said with the day's first pass: it is not said again.
    assert "do not re-read" not in notice


def test_the_first_notice_of_a_day_carries_the_rules_and_the_record_line_in_full(store: Store, git_repo):
    _passed(store, git_repo, "add the csv export")
    notice = hooks.endings(store, "s", claim="stop")[0]
    assert notice.startswith(hooks.ENDINGS_HEADER + " " + hooks.ENDINGS_RULES)
    assert "do not re-read the code or re-run the check to confirm it" in notice
    # A day later the rules are said again, in full, to the same session.
    store.set_meta("endings-said:rules:s", "2000-01-01")
    _passed(store, git_repo, "add the json export")
    assert hooks.endings(store, "s", claim="stop")[0].startswith(hooks.ENDINGS_HEADER + " " + hooks.ENDINGS_RULES)


def test_a_peek_says_the_rules_in_full_and_marks_nothing_heard(store: Store, git_repo):
    _passed(store, git_repo, "add the csv export")
    assert hooks.endings(store, "s")[0].startswith(hooks.ENDINGS_HEADER + " " + hooks.ENDINGS_RULES)
    assert store.meta("endings-said:rules:s") is None


def test_a_criterion_not_met_is_still_shown_in_a_passed_notice(store: Store, git_repo):
    verdict = {"check": {"command": "pytest -q", "passed": True},
               "criteria": [{"id": "S1", "met": True, "evidence": "test_export passes"},
                            {"id": "S2", "met": False, "evidence": "empty lists are not exported"}],
               "independent": {"ran": True, "passed": True}}
    _passed(store, git_repo, "add the csv export", verdict=verdict)
    notice = hooks.endings(store, "s", claim="stop")[0]
    assert "1/2 criteria met" in notice
    assert "  S2 NOT met: empty lists are not exported" in notice
    assert "S1 met" not in notice  # a criterion met is counted, not repeated
    # Unverified: the notice says what lets the diff be opened, in its own words.
    assert "open the diff only where it says something was not checked or not met" in notice


def test_a_stop_only_the_person_can_clear_carries_its_instruction_after_the_rules_were_said(store: Store, git_repo):
    hooks.endings(store, "s", claim="stop")
    task = store.create_task("add the csv export", status="running", source="locks", session_id="s", repo="r",
                             cwd=str(git_repo))
    stop = stops.Stop("approval", "the next cell needs a person (fable)")
    stops.record(store, task["id"], stop)
    store.add_event(task["id"], "finished", status="needs_approval", changed=[], stop=stop.data())
    store.update_task(task["id"], status="needs_approval")
    notice = hooks.endings(store, "s", claim="stop")[0]
    assert "ask the person for it here; on their yes run its line" in notice


def test_a_file_landed_on_head_and_edited_since_names_no_overlap(store: Store, git_repo):
    base = git(git_repo, "rev-parse", "HEAD")
    git(git_repo, "checkout", "-q", "-b", "side")
    (git_repo / "a.txt").write_text("new\n")
    git(git_repo, "add", "a.txt")
    git(git_repo, "commit", "-q", "-m", "side")
    tip = git(git_repo, "rev-parse", "HEAD")
    git(git_repo, "checkout", "-q", "main")
    # Squash-landed on main, then edited again: HEAD's history holds the branch's version of the file.
    (git_repo / "a.txt").write_text("new\n")
    git(git_repo, "add", "a.txt")
    git(git_repo, "commit", "-q", "-m", "squash of side")
    (git_repo / "a.txt").write_text("newer\n")
    git(git_repo, "commit", "-q", "-am", "edit after the squash")
    assert isolate.path_landed(git_repo, "a.txt", tip)
    assert isolate.live_paths(git_repo, tip, ["a.txt"]) == []

    other = store.create_task("the squashed work", status="done", source="queue", session_id="s", repo="r",
                              cwd=str(git_repo))
    git(git_repo, "branch", "-f", f"locks/task-{other['id']}", tip)
    store.add_event(other["id"], "finished", status="done", changed=["a.txt"], base=base,
                    branch=f"locks/task-{other['id']}")
    mine = store.create_task("touch a.txt again", status="done", source="queue", session_id="s", repo="r",
                             cwd=str(git_repo))
    store.add_event(mine["id"], "finished", status="done", changed=["a.txt"], base=base)
    ended = {**store.get_task(mine["id"]), "finished_data": store.final_event(mine["id"])["data"]}
    assert hooks._overlaps(store, ended) == []


def test_a_writing_workers_answer_is_cut_in_the_notice_and_kept_whole_on_the_task(store: Store, git_repo):
    answer = "Built the export, and checked each row against the spec. " * 12  # well past the notice's 300
    task = _passed(store, git_repo, "add the csv export", result=answer)
    notice = hooks.endings(store, "s", claim="stop")[0]
    said = next(line for line in notice.splitlines() if line.startswith("  the worker says: "))
    assert len(said) <= len("  the worker says: ") + hooks.ANSWER_SHOWN
    assert f"`locks show {task['id']}` prints it whole" in notice
