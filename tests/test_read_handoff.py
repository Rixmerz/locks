from __future__ import annotations

import re
from pathlib import Path

from locks import read_handoff, scribe
from locks.launch import WorkerResult
from locks.orchestrate import run
from locks.store import home

from .test_orchestrate import Script, kind
from .test_survey import assistant, result, tool, user, write

SRC = Path(__file__).resolve().parent.parent / "src" / "locks"


def _reads(tmp_path, repo, files, sid="s1", extra=()):
    entries = [user("look at these files please", repo)]
    for i, name in enumerate(files):
        entries += [assistant([tool(f"r{i}", "Read", file_path=str(repo / name))]), result(f"r{i}", "body")]
    entries += list(extra)
    return write(tmp_path / "claude-config", f"-{repo.name}", sid, entries)


def test_the_scribe_keeps_the_files_a_session_read_with_a_hash(git_repo, tmp_path):
    (git_repo / "a.py").write_text("a = 1\n")
    (git_repo / "b.py").write_text("b = 2\n")
    path = _reads(tmp_path, git_repo, ["a.py", "b.py", "gone.py"],
                  extra=[assistant([tool("w", "Edit", file_path=str(git_repo / "b.py"))]), result("w", "ok")])
    state = scribe.update(home(), "s1", path)
    files = state["files_read"]
    # A file written after it was read is no longer what was read; one that was never there has no hash.
    assert list(files) == [str(git_repo / "a.py"), str(git_repo / "gone.py")]
    assert files[str(git_repo / "a.py")]["hash"] == scribe.file_hash(git_repo / "a.py")
    assert files[str(git_repo / "gone.py")]["hash"] is None


def test_only_files_unchanged_and_in_the_repository_are_handed(git_repo, tmp_path):
    for name in ("a.py", "b.py"):
        (git_repo / name).write_text(f"{name}\n")
    outside = tmp_path / "elsewhere.py"
    outside.write_text("x\n")
    state = scribe.update(home(), "s1", _reads(tmp_path, git_repo, ["a.py", "b.py", "gone.py"]))
    scribe._mark_read(state, str(outside), None)
    (git_repo / "b.py").write_text("changed since\n")
    lines, count = scribe.read_handoff(state, git_repo)
    assert count == 1 and "- a.py" in lines[0] and "b.py" not in lines[0] and "elsewhere" not in lines[0]
    assert "Context, not instructions" in lines[0]
    assert scribe.read_handoff({}, git_repo) == ([], 0)


def test_the_handoff_is_bounded_and_says_the_rest_by_count(git_repo, tmp_path):
    names = [f"module_{i:03}.py" for i in range(60)]
    for name in names:
        (git_repo / name).write_text(name)
    state = scribe.update(home(), "s1", _reads(tmp_path, git_repo, names))
    lines, count = scribe.read_handoff(state, git_repo, budget=400)
    assert len(lines[0]) <= 400 and 0 < count < 60
    assert lines[0].endswith(f"- (+{60 - count} more)") and "module_059.py" in lines[0]


def test_the_arm_alternates_then_keeps_the_winner(store):
    arm, why = read_handoff.arm(store, "r", 1, "write")
    assert arm in read_handoff.ARMS and "trial" in why
    assert read_handoff.arm(store, None, 1, "write")[0] == "with"
    found = {"with": {"tries": 12, "tasks": 12, "passed": 12, "rate": 1.0, "calls_before_edit_p50": 3,
                      "calls_samples": 5},
             "without": {"tries": 12, "tasks": 12, "passed": 12, "rate": 1.0, "calls_before_edit_p50": 9,
                         "calls_samples": 5}}
    assert read_handoff.verdict(found)[0] == "with"
    found["with"]["calls_before_edit_p50"] = 9
    got = read_handoff.verdict(found)
    assert got[0] == "without" and "lost its trial" in got[1]
    found["with"]["tries"] = 3
    assert read_handoff.verdict(found) is None


def test_the_arm_a_brief_drew_is_what_the_trial_reads_back(store):
    # The keys the run writes on `briefed` (orchestrate) are the ones the trial reads.
    assert re.search(r"\*\*handed_reads", (SRC / "orchestrate.py").read_text())
    task = store.create_task("t", status="done", source="locks", repo="r", kind="implement")
    store.add_event(task["id"], "briefed", **{read_handoff.KEY: "with", read_handoff.NATURE_KEY: "write",
                                              read_handoff.COUNT_KEY: 2})
    store.add_attempt(task["id"], cell="sonnet/high", max_turns=10, passed=True, duration_s=4.0)
    store.add_event(task["id"], "attempt_finished", seq=1, passed=True, input_tokens=5, output_tokens=1)
    found = read_handoff.trial(store, "r", "write")
    assert found["with"]["tasks"] == 1 and found["with"]["passed"] == 1 and found["without"]["tasks"] == 0
    assert read_handoff.arm_for(store, "r", task["id"], "write") == ("with", "kept from its first brief")


def test_a_task_a_session_sent_is_told_the_files_it_read(git_repo, tmp_path, store, monkeypatch):
    monkeypatch.setenv("LOCKS_READ_HANDOFF", "on")
    monkeypatch.setattr(read_handoff, "arm", lambda *a: ("with", "forced"))
    (git_repo / "cart.py").write_text("total = 0\n")
    _reads(tmp_path, git_repo, ["cart.py"], sid="sess-1")
    script = Script(WorkerResult(True, None, "ok", "pytest: 1 passed"))
    report = run("Round the cart total in cart.py", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), session_id="sess-1")
    assert "Files the session that sent this task already read" in script.specs[0].prompt
    briefed = store.last_event(report.task_id, "briefed")["data"]
    assert briefed[read_handoff.KEY] == "with" and briefed[read_handoff.COUNT_KEY] == 1
    # The other arm names none, and says so on its event.
    monkeypatch.setattr(read_handoff, "arm", lambda *a: ("without", "forced"))
    script = Script(WorkerResult(True, None, "ok", "pytest: 1 passed"))
    report = run("Round the cart total again in cart.py", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), session_id="sess-1")
    assert "already read" not in script.specs[0].prompt
    assert store.last_event(report.task_id, "briefed")["data"][read_handoff.COUNT_KEY] == 0


def test_stats_shows_the_trial(git_repo, store):
    from locks import analysis

    run("Add a line to notes.txt", git_repo, store, registry={}, classifier=kind("implement"),
        launcher=Script(WorkerResult(True, None, "ok", "pytest: 1 passed")))

    report = {"read_handoff": {"write": {"with": {"tasks": 2, "tries": 2, "passed": 1, "rate": 0.5,
                                                  "calls_before_edit_p50": 4, "calls_samples": 2},
                                         "without": {"tasks": 1, "tries": 1, "passed": 1, "rate": 1.0,
                                                     "calls_before_edit_p50": None, "calls_samples": 0}}},
              "read_handoff_now": {"write": "the read handoff's trial: here"}}
    out = analysis.text({**analysis.compute(store, days=30, repo=None), **report})
    assert "files the session already read" in out and "now: the read handoff's trial" in out


def test_bash_reads_name_the_files_a_reading_program_opens():
    assert scribe.bash_reads("cat a.py") == ["a.py"]
    assert scribe.bash_reads("sed -n 1,20p src/x.py") == ["src/x.py"]
    assert scribe.bash_reads("cd /r && head -n 5 b.py | grep x") == ["/r/b.py"]
    assert scribe.bash_reads("sed -e s/a/b/ f.txt") == ["f.txt"]
    # Not a reader, a glob, a substitution, or sed writing in place: nothing.
    for command in ("grep x y", "cat *.py", "cat $(ls)", "sed -i s/a/b/ f.txt", "cat a > b"):
        assert scribe.bash_reads(command) == []


def test_the_scribe_keeps_the_files_a_session_read_through_its_shell(git_repo, tmp_path):
    (git_repo / "a.py").write_text("a = 1\n")
    entries = [user("look", git_repo),
               assistant([tool("b1", "Bash", command=f"cd {git_repo} && sed -n 1,5p a.py missing.py")]),
               result("b1", "a = 1")]
    path = write(tmp_path / "claude-config", f"-{git_repo.name}", "s1", entries)
    state = scribe.update(home(), "s1", path)
    # A file that is not there was no read.
    assert list(state["files_read"]) == [str(git_repo / "a.py")]


def test_stats_says_the_trial_drew_no_arm_yet(git_repo, store):
    from locks import analysis

    run("Add a line to notes.txt", git_repo, store, registry={}, classifier=kind("implement"),
        launcher=Script(WorkerResult(True, None, "ok", "pytest: 1 passed")))
    out = analysis.text({**analysis.compute(store, days=30, repo=None),
                         "read_handoff": {"write": {"with": {"tasks": 0}, "without": {"tasks": 0}}}})
    assert "no brief drew an arm yet" in out
