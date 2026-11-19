"""`locks check`: the session's own change checked by locks with no worker (`locks.self_check`)."""
import json
import subprocess
import sys

import pytest

from locks import analysis, cli, coverage, self_check
from locks.classify import Classification
from locks.launch import WorkerResult
from tests.conftest import git

BUG = {"where": "app.py:1", "what": "it prints the wrong word", "evidence_class": "deterministic",
       "cause": "change", "proof": "python app.py"}


class Fake:
    def __init__(self, result):
        self.result, self.specs = result, []

    def __call__(self, spec):
        self.specs.append(spec)
        return self.result


def implement(_text):
    return Classification("implement")


@pytest.fixture
def verifier_on(monkeypatch):
    monkeypatch.setenv("LOCKS_VERIFIER", "on")


def _change(repo):
    (repo / "app.py").write_text("print('hello')\n")
    (repo / "new.py").write_text("x = 1\n")


def test_a_clean_check_passes_and_records_the_contract(git_repo, store, verifier_on):
    _change(git_repo)
    status_before = git(git_repo, "status", "--porcelain")
    fake = Fake(WorkerResult(True, None, "fine", verdict="pass", cost_usd=0.2,
                             criteria=({"id": "S1", "met": True, "evidence": "ran it"},)))
    out = self_check.run(store, git_repo, "Change the greeting:\n- S1 print hello",
                         verify=f"{sys.executable} app.py", launcher=fake, classifier=implement)
    assert out.passed and out.check["passed"] and out.verifier["passed"]
    # The person's checkout and index are left as they were.
    assert git(git_repo, "status", "--porcelain") == status_before
    assert git(git_repo, "worktree", "list").count("\n") == 0
    spec = fake.specs[0]
    assert "Change the greeting" in spec.prompt and "read-only" in spec.prompt
    assert "app.py" in spec.prompt and "new.py" in spec.prompt
    task = store.get_task(out.task_id)
    assert task["status"] == "done" and json.loads(task["options"])[self_check.OPTION] is True
    assert task["cost_usd"] == pytest.approx(0.2)
    kinds = [e["kind"] for e in store.events(task_id=out.task_id)]
    assert {"planned", "self_checked", "verified", "finished"} <= set(kinds)
    assert "PASS" in self_check.text(out)


def test_a_blocker_stops_it_as_review_and_a_later_check_supersedes_it(git_repo, store, verifier_on):
    _change(git_repo)
    fake = Fake(WorkerResult(False, None, "broken", verdict="fail", findings=(BUG,)))
    out = self_check.run(store, git_repo, "change the greeting", launcher=fake, classifier=implement)
    assert not out.passed and out.verifier["blockers"]
    assert store.get_task(out.task_id)["status"] == "blocked"
    assert "REVIEW" in self_check.text(out) and "blocker" in self_check.text(out)
    again = self_check.run(store, git_repo, "change the greeting", classifier=implement,
                           launcher=Fake(WorkerResult(True, None, "ok", verdict="pass")))
    assert again.passed
    assert store.get_task(out.task_id)["status"] == "dismissed"


def test_a_failing_check_skips_the_verifier(git_repo, store, verifier_on):
    _change(git_repo)
    fake = Fake(WorkerResult(True, None, "ok", verdict="pass"))
    out = self_check.run(store, git_repo, "change the greeting", verify="false", launcher=fake,
                         classifier=implement)
    assert not out.passed and not fake.specs and out.verifier["ran"] is False


def test_with_the_verifier_off_only_the_check_runs(git_repo, store, no_verifier):
    _change(git_repo)
    out = self_check.run(store, git_repo, "change the greeting", verify="true", classifier=implement,
                         launcher=Fake(None))
    assert out.passed and "off" in out.verifier["why"]


def test_nothing_changed_is_nothing_to_check(git_repo, store):
    with pytest.raises(self_check.NothingToCheck):
        self_check.run(store, git_repo, "x", classifier=implement)


def test_a_clean_checkout_is_read_from_where_it_left_the_trunk(git_repo):
    first = git(git_repo, "rev-parse", "HEAD")
    git(git_repo, "checkout", "-q", "-b", "topic")
    (git_repo / "app.py").write_text("print('x')\n")
    git(git_repo, "commit", "-qam", "x")
    assert self_check.resolve_base(git_repo, None) == first
    assert self_check.resolve_base(git_repo, "HEAD") == git(git_repo, "rev-parse", "HEAD")


def test_stats_and_coverage_count_it(git_repo, store, no_verifier):
    _change(git_repo)
    self_check.run(store, git_repo, "change the greeting", verify="true", classifier=implement)
    row = analysis.self_checks(store)
    assert row["n"] == 1 and row["passed"] == 1
    assert "self-checks" in "\n".join(analysis.self_check_lines(row))
    # With no run of locks' in the period, the session's own checked work is still said.
    assert "self-checks" in analysis.text(analysis.compute(store))
    assert any(f.name == "self-check" and f.source == ("self_check",) for f in coverage.FEATURES)


def test_the_command_exits_by_its_verdict(git_repo, monkeypatch, capsys, no_verifier):
    monkeypatch.chdir(git_repo)
    assert cli.main(["check", "--request", "change it", "--kind", "implement"]) == self_check.EXIT_NOTHING
    _change(git_repo)
    assert cli.main(["check", "--request", "change it", "--kind", "implement", "--verify", "true"]) == 0
    assert cli.main(["check", "--request", "change it", "--kind", "implement", "--verify", "false",
                     "--json"]) == self_check.EXIT_BLOCKED
    assert '"self_check": true' in capsys.readouterr().out


def test_a_worker_never_runs_it(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("LOCKS_WORKER_TASK", "3")
    assert cli.main(["check", "--request", "x"]) == 2
    assert subprocess.run(["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True).stdout == ""
