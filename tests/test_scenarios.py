"""Scenarios: locks run as it runs for a person, with real processes, and only `claude` faked.

The unit tests drive the engine in-process with a Python launcher, the adaptive settings off:
they never saw a worker's process die under the dispatcher, a resume rewrite a workflow
part, a lane stay paused after its task passed, or a worker inherit the host's session.
These run the real `locks` commands as subprocesses (the dispatcher, `run-queued`, the
automatic resume, the workflow engine, git worktrees), with `tests/scenarios/fake_claude.py`
on PATH as `claude`, and end with `locks doctor`: whatever else a scenario asserts, the store
it leaves must hold nothing broken.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from locks import doctor, flow, workflows
from locks.store import Store

from .conftest import git

FAKE = Path(__file__).parent / "scenarios" / "fake_claude.py"
SRC = Path(__file__).resolve().parents[1] / "src"
#: How long a scenario may take before it is stuck: a real stuck state never ends on its own.
DEADLINE_S = 120


@pytest.fixture
def world(tmp_path: Path, git_repo: Path, _isolated_home: Path):
    """A repository, a fake `claude` first on PATH, and the environment the commands run in:
    the dispatcher starts on its own, and the host's session ids are set, as in a cloud session."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    (bin_dir / "claude").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "PYTHONPATH": str(SRC),
           "LOCKS_AUTOWORK": "on", "LOCKS_LIMIT_GRACE_S": "1", "LOCKS_LOOP_POLL_S": "0.5",
           "CLAUDE_CODE_SESSION_ID": "host-session", "CLAUDE_CODE_REMOTE_SESSION_ID": "host-session",
           "FAKE_CLAUDE_SCRIPT": str(tmp_path / "script.json"), "FAKE_CLAUDE_LOG": str(tmp_path / "calls.jsonl")}
    env.pop("LOCKS_SESSION_ID", None)

    class World:
        repo, home, log = git_repo, _isolated_home, tmp_path / "calls.jsonl"

        def setenv(self, name: str, value: str) -> None:
            env[name] = value

        def script(self, rules: list[dict]) -> None:
            Path(env["FAKE_CLAUDE_SCRIPT"]).write_text(json.dumps(rules))

        def locks(self, *args: str) -> str:
            done = subprocess.run([sys.executable, "-m", "locks", *args], cwd=self.repo, env=env,
                                  capture_output=True, text=True, timeout=DEADLINE_S)
            assert done.returncode == 0, done.stdout + done.stderr
            return done.stdout

        def calls(self, *, haiku: bool = False, checks: bool = False) -> list[dict]:
            """The workers' calls of the fake `claude`, oldest first (`haiku`: the questions to Haiku;
            `checks`: the independent check's, whose attempt is marked `-verify`)."""
            said = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
            return [c for c in said if bool(c.get("haiku")) == haiku
                    and str(c.get("attempt") or "").endswith("-verify") == checks]

        def wait(self, ended, what: str):
            """Until `ended(store)` says so; a scenario that never gets there is stuck."""
            deadline = time.monotonic() + DEADLINE_S
            while time.monotonic() < deadline:
                store = Store(self.home / "locks.db")
                try:
                    if found := ended(store):
                        return found
                finally:
                    store.close()
                time.sleep(0.5)
            store = Store(self.home / "locks.db")
            try:
                tasks = [(t["id"], t["status"], t["title"][:50]) for t in store.list_tasks(limit=20)]
                said = doctor.text(doctor.check(store))
                # How each task ended each time, and what resumed it: a stuck scenario says why.
                said += "\n" + "\n".join(
                    f"#{t['id']} {e['kind']} {json.dumps({k: e['data'].get(k) for k in ('status', 'stop', 'after')})}"
                    for t in store.list_tasks(limit=20) for kind in ("dispatched", "auto_resumed", "finished")
                    for e in store.events_of(t["id"], kind))
            finally:
                store.close()
            logs = "\n".join(p.read_text()[-1500:] for p in (self.home / "work").glob("*.log"))
            pytest.fail(f"stuck waiting for {what}:\n{tasks}\n{said}\n{logs}")

        def healthy(self) -> None:
            store = Store(self.home / "locks.db")
            try:
                broken = [f for f in doctor.check(store) if f.severity == doctor.BROKEN
                          or (f.severity == doctor.SUSPECT and f.rule in (
                              "part-lost-its-mark", "join-short-of-parts", "attempts-with-no-session",
                              "format-failure-climbed", "died-after-a-pass"))]
            finally:
                store.close()
            assert broken == [], doctor.text(broken)

    return World()


def _task(status: tuple[str, ...]):
    """A task in one of `status` whose ending is on record: the status is set a moment before its
    `finished` event (`Store.final_event`), and a scenario that reads the ending must not run between."""
    return lambda store: next((t for t in store.list_tasks(status=list(status), limit=5)
                               if t["status"] in ("queued", "running") or store.final_event(t["id"]) is not None), None)


def test_a_worker_killed_mid_attempt_is_resumed_by_the_dispatcher_and_the_lane_goes_on(world):
    world.script([{"match": "make the greeting", "do": [{"die": True}, {"pass": True, "write": {"hi.txt": "hi\n"}}]}])
    world.locks("queue", "add", "--kind", "implement", "make the greeting file for the app")
    ended = world.wait(_task(("done", "failed", "blocked", "review")), "the killed task to end")
    assert ended["status"] == "done"
    calls = world.calls()
    assert [c["action"].get("die", False) for c in calls] == [True, False]
    # A worker never sees the host's session, nor another task's identity.
    assert all(c["host"] == {} for c in calls) and all(c["worker_task"] == str(ended["id"]) for c in calls)
    world.wait(lambda store: not any(lane["paused"] for lane in store.lanes()), "the lane to open")
    world.healthy()


def test_a_worker_that_committed_its_own_work_and_left_only_artifacts_passes_on_its_commit(world):
    # A real docs worker did this and its run died twice at the ending (git commit of nothing).
    world.script([{"match": "document the greeting", "do": [{
        "pass": True, "write": {"README.md": "# Greeting\n"}, "commit": "Document the greeting",
        "litter": {"__pycache__/app.cpython-313.pyc": "x"}}]}])
    world.locks("queue", "add", "--kind", "docs", "document the greeting in the README")
    ended = world.wait(_task(("done", "failed", "blocked", "review", "interrupted")), "the docs task to end")
    assert ended["status"] == "done"
    log = subprocess.run(["git", "log", "-1", "--format=%s", f"locks/task-{ended['id']}"], cwd=world.repo,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert log == "Document the greeting"
    world.healthy()


def test_a_worker_that_dies_of_the_same_crash_twice_is_resumed_once_and_then_left_to_the_orchestrator(world):
    world.script([{"match": "make the badge", "do": [{"crash": "locks.isolate.IsolationError: git commit failed"}]}])
    world.locks("queue", "add", "--kind", "implement", "make the badge file for the app")

    def died_twice(store):
        # The automatic resume is a process of its own: what notices its death is a turn's end, whose
        # hooks sweep (`flow.sweep`), not the dispatcher that started it. Nothing here ends a turn.
        flow.sweep(store)
        task = next(iter(store.list_tasks(status=["interrupted"], limit=5)), None)
        stop = (store.final_event(task["id"]) or {}).get("data", {}).get("stop") if task else None
        return task if stop and stop.get("repeated") else None

    ended = world.wait(died_twice, "the second death of the same crash")
    store = Store(world.home / "locks.db")
    try:
        stop = store.final_event(ended["id"])["data"]["stop"]
        resumed = store.events_of(ended["id"], "auto_resumed")
    finally:
        store.close()
    assert stop["crash"] == "locks.isolate.IsolationError: git commit failed"
    assert "the same crash as its last death" in stop["reason"] and len(resumed) == 1
    assert len(world.calls()) == 2  # the first try and the one automatic resume; never a third


def test_a_folder_a_cancel_left_where_a_task_s_worktree_goes_does_not_stop_it(world):
    """A cancel left a task's folder with a build cache, and its resume failed until the
    person deleted it by hand."""
    stray = world.home / "worktrees" / "task-1" / ".angular"
    stray.mkdir(parents=True)
    (stray / "cache").write_text("x")
    world.script([{"match": "make the greeting", "do": [{"pass": True, "write": {"hi.txt": "hi\n"}}]}])
    world.locks("queue", "add", "--kind", "implement", "make the greeting file for the app")
    ended = world.wait(_task(("done", "failed", "blocked", "review", "replan", "interrupted")), "the task")
    assert ended["id"] == 1 and ended["status"] == "done"
    [moved] = (world.home / "worktrees" / "stray").iterdir()
    assert (moved / ".angular" / "cache").read_text() == "x"
    store = Store(world.home / "locks.db")
    try:
        assert store.events_of(1, "stray_moved")
    finally:
        store.close()
    world.healthy()


def test_more_turns_continue_the_worker_s_own_session(world):
    world.script([{"match": "write the long report", "do": [
        {"turns": True, "write": {"report.md": "part one\n"}},
        {"pass": True, "write": {"report.md": "part one\npart two\n"}}]}])
    world.locks("queue", "add", "--kind", "implement", "write the long report for the release")
    ended = world.wait(_task(("done", "failed", "blocked", "review", "replan", "interrupted")), "the report task")
    assert ended["status"] == "done"
    first, second = world.calls()
    assert "--resume" not in first["argv"]
    # The session the first attempt kept is the one the second continues, in the same worktree.
    store = Store(world.home / "locks.db")
    try:
        kept = store.attempt(ended["id"], 1)["session_id"]
    finally:
        store.close()
    assert kept and kept != "host-session"
    assert second["argv"][second["argv"].index("--resume") + 1] == kept and second["cwd"] == first["cwd"]
    world.healthy()


def test_a_workflow_part_that_died_is_resumed_and_the_join_holds_every_part(world):
    parts = [{"id": "api", "prompt": "PART-API: write api.txt", "surfaces": ["api.txt"]},
             {"id": "web", "prompt": "PART-WEB: write web.txt", "surfaces": ["web.txt"]}]
    world.script([
        # The plan's first answer cannot run in parts: the same cell is asked again, no climb.
        {"match": "Plan, without changing any file|Your last answer to this task could not be read", "do": [
            {"pass": True, "block": {"parts": [{"id": "api"}], "contract": "c"}},
            {"pass": True, "block": {"parts": parts, "contract": "api.txt and web.txt each say their name"}}]},
        {"match": "PART-API", "do": [{"pass": True, "write": {"api.txt": "api\n"}}]},
        {"match": "PART-WEB", "do": [{"die": True}, {"pass": True, "write": {"web.txt": "web\n"}}]},
        {"match": "Review, without changing any file", "do": [{"pass": True, "block": {"findings": []}}]},
    ])
    world.locks("workflow", "run", "feature-parallel", "add the two greeting files for the app")

    def run_ended(store):
        run = store.get_run(1)
        return run if run and run["status"] not in ("active",) else None

    run = world.wait(run_ended, "the workflow run to end")
    assert run["status"] == "done", run
    store = Store(world.home / "locks.db")
    try:
        last = max((t for t in store.run_tasks(1) if t["status"] == "done"), key=lambda t: t["id"])
        branch = next(b for t in reversed(store.run_tasks(1))
                      if (b := ((store.final_event(t["id"]) or {}).get("data") or {}).get("branch")))
    finally:
        store.close()
    assert last["workflow_step"] in ("review", "recheck")
    # The join holds both parts' work, the one that died and was resumed included.
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", branch], cwd=world.repo,
                           capture_output=True, text=True, check=True).stdout.split()
    assert {"api.txt", "web.txt"} <= set(files), (branch, files)
    plans = [c["argv"] for c in world.calls() if c["rule"] == 0]
    assert len(plans) == 2 and plans[0][plans[0].index("--effort") + 1] == plans[1][plans[1].index("--effort") + 1]
    world.healthy()


def test_a_part_that_builds_on_another_starts_from_its_work_after_it_passes(world):
    parts = [{"id": "api", "prompt": "PART-API: write api.txt", "surfaces": ["api.txt"]},
             {"id": "web", "prompt": "PART-WEB: write web.txt from api.txt", "surfaces": ["web.txt"], "after": "api"}]
    world.script([
        {"match": "Plan, without changing any file|Your last answer to this task could not be read", "do": [
            {"pass": True, "block": {"parts": parts, "contract": "web.txt repeats api.txt"}}]},
        {"match": "PART-API", "do": [{"pass": True, "write": {"api.txt": "api\n"}}]},
        {"match": "PART-WEB", "do": [{"pass": True, "write": {"web.txt": "web on api\n"}}]},
        {"match": "Review, without changing any file", "do": [{"pass": True, "block": {"findings": []}}]},
    ])
    world.locks("workflow", "run", "feature-parallel", "add the api file and the web file built on it")

    def run_ended(store):
        run = store.get_run(1)
        return run if run and run["status"] != "active" else None

    run = world.wait(run_ended, "the workflow run to end")
    assert run["status"] == "done", run
    calls = world.calls()
    api = next(i for i, c in enumerate(calls) if c["rule"] == 1)
    web = next(i for i, c in enumerate(calls) if c["rule"] == 2)
    # The web part ran only after the api part passed, from the commit the api part kept.
    assert api < web
    store = Store(world.home / "locks.db")
    try:
        part = {json.loads(t["options"])["part"]["id"]: t for t in store.run_tasks(1)
                if (json.loads(t["options"] or "{}").get("part") or {}).get("id")}
        released = store.last_event(part["web"]["id"], "released")
        tip = store.final_event(part["api"]["id"])["data"]["tip"]
    finally:
        store.close()
    assert released is not None and released["data"]["base"] == tip
    world.healthy()


def test_a_check_that_fails_at_the_base_the_same_way_does_not_fail_the_work(world):
    """A real run: the check's coverage thresholds of files the task never touched failed at
    the base too, and the red check was read as the work's bug. It is compared with the base."""
    (world.repo / "check.sh").write_text(
        "echo 'FAIL legacy/old.test.js'\n"
        "echo 'Jest: \"legacy/old.js\" coverage threshold for lines (80%) not met: 41.2%'\n"
        "if [ -f feature.txt ] && ! grep -q ok feature.txt; then echo 'FAIL src/feature.test.js'; fi\n"
        "exit 1\n")
    subprocess.run(["git", "add", "-A"], cwd=world.repo, check=True)
    subprocess.run(["git", "commit", "-qm", "a check that already fails"], cwd=world.repo, check=True)
    world.script([{"match": "add the feature file", "do": [{"pass": True, "write": {"feature.txt": "ok\n"}}]}])
    world.locks("queue", "add", "--kind", "implement", "--verify", "sh check.sh", "add the feature file to the app")
    ended = world.wait(_task(("done", "failed", "blocked", "review", "replan")), "the task to end")
    assert ended["status"] == "done", ended
    store = Store(world.home / "locks.db")
    try:
        kept = store.last_event(ended["id"], "check_preexisting")
        verdict = store.final_event(ended["id"])["data"]["verdict"]
    finally:
        store.close()
    assert kept is not None and kept["data"]["n"] == 2 and kept["data"]["added"] == 0
    assert verdict["check"]["state"] == "failing at base, unchanged"
    assert len(world.calls()) == 1  # no climb: the pass stood on its first try
    world.healthy()


def test_work_queued_after_a_task_that_is_dismissed_leaves_the_queue_with_it(world):
    world.script([{"match": "write the schema file", "do": [
        {"block": {"verdict": "fail", "failure": "spec_bug", "summary": "the schema it names does not exist"}}]}])
    world.locks("queue", "add", "--kind", "implement", "write the schema file for the totals")
    first = world.wait(_task(("queued", "running")), "the first task to be queued")
    world.locks("queue", "add", "--kind", "implement", "--after", str(first["id"]), "use the schema in the report")
    world.wait(lambda store: store.get_task(first["id"])["status"] not in ("queued", "running"), "the first to stop")
    said = world.locks("dismiss", str(first["id"]), "--why", "the schema is not needed")
    assert "off the queue" in said
    store = Store(world.home / "locks.db")
    try:
        held = next(t for t in store.list_tasks(limit=5) if t["id"] != first["id"])
        assert held["status"] == "cancelled" and json.loads(held["options"]).get("after") is None
    finally:
        store.close()
    world.healthy()


def test_a_task_the_usage_limit_stopped_resumes_on_its_own_after_the_reset(world):
    resets = int(time.time()) + 3
    world.script([{"match": "make the greeting", "do": [
        {"limit": f"Claude AI usage limit reached|{resets}", "write": {"hi.txt": "h\n"}},
        {"pass": True, "write": {"hi.txt": "hi\n"}}]}])
    world.locks("queue", "add", "--kind", "implement", "make the greeting file for the app")
    stopped = world.wait(_task(("blocked", "done", "failed")), "the limit to stop it")
    assert stopped["status"] == "blocked"
    # A turn's end (here, the endings command) sees the limit has not reset: a waiter resumes it then.
    world.locks("endings", "--session", "nobody")
    ended = world.wait(_task(("done",)), "the resume after the reset")
    assert ended["id"] == stopped["id"] and [c["action"].get("limit") is None for c in world.calls()] == [False, True]
    world.healthy()


#: How many tasks the long scenario runs one after another in one repository.
LONG_RUN = 30


def test_a_loop_runs_rounds_by_itself_until_its_metric_stops_moving(world, tmp_path):
    """The loop's own runner, detached, starts each round as a workflow run, measures the
    metric between rounds, and stops when it no longer moves; the session hears once."""
    folder = tmp_path / "leads"
    folder.mkdir()
    (folder / "count.txt").write_text("0\n")
    (tmp_path / "round.json").write_text(json.dumps({"name": "probe", "inputs": ["goal"], "steps": [
        {"id": "work", "prompt": "LOOP-ROUND: work toward {goal}", "kind": "explore"}]}))
    world.locks("workflow", "save", str(tmp_path / "round.json"), "--scope", "user")
    world.script([{"match": "LOOP-ROUND", "do": [
        {"pass": True, "write": {"count.txt": "2\n"}}, {"pass": True, "write": {"count.txt": "3\n"}},
        {"pass": True, "write": {"count.txt": "3\n"}}]}])
    world.locks("loop", "add", "leads", "--goal", "count the leads", "--workflow", "probe", "--dir",
                str(folder), "--metric", "cat count.txt", "--stale", "1", "--policy", "offer only the audit")
    world.locks("loop", "start", "leads")
    from locks import loops

    deadline = time.monotonic() + DEADLINE_S
    while loops.state("leads").get("status") != "stopped" and time.monotonic() < deadline:
        time.sleep(0.5)
    st = loops.state("leads")
    assert st["status"] == "stopped" and "did not improve" in st["why"], (st, loops.diary("leads"))
    rounds = loops.diary("leads")
    assert [(r["before"], r["after"]) for r in rounds] == [(0.0, 2.0), (2.0, 3.0), (3.0, 3.0)]
    briefs = [c for c in world.calls() if c["rule"] == 0]
    assert len(briefs) == 3
    shown = world.locks("loop", "show", "leads")
    assert "metric: 0.0 → 3.0" in shown and "rounds: 3 (3 ended)" in shown
    world.healthy()


def test_a_step_forks_the_session_of_the_step_that_read_for_it(world, tmp_path):
    """A step whose `trunk` is the exploration before it starts as a fork of that session, from
    its own worktree, told it is a new worker; its brief follows, so its own rule answers."""
    (tmp_path / "scout.json").write_text(json.dumps({"name": "scout", "inputs": ["goal"], "steps": [
        {"id": "look", "prompt": "LOOK-STEP: read the code for {goal}, changing nothing", "kind": "explore"},
        {"id": "make", "prompt": "MAKE-STEP: build {goal}", "kind": "implement", "trunk": "look",
         "needs": ["look"]}]}))
    world.locks("workflow", "save", str(tmp_path / "scout.json"), "--scope", "user")
    world.setenv("LOCKS_TRUNK", "on")
    world.script([
        {"match": "(?s)everything above is the session of the workflow step 'look'.*MAKE-STEP",
         "do": [{"pass": True, "write": {"greeting.txt": "hi\n"}}]},
        {"match": "LOOK-STEP", "do": [{"pass": True}]}])
    # Two fresh first tries here already: the trial's arms are `arms.BALANCE` apart, so this step forks.
    from locks import repo as repo_mod

    seeded = Store(world.home / "locks.db")
    try:
        for _ in range(2):
            task = seeded.create_task("t", status="done", source="queue", repo=repo_mod.key(world.repo))
            seeded.add_event(task["id"], "briefed", trunk_arm="fresh")
            seeded.add_attempt(task["id"], cell="sonnet/high", max_turns=80, passed=1, input_tokens=1000)
    finally:
        seeded.close()
    world.locks("workflow", "run", "scout", "the greeting")
    def run_ended(store):
        return next((r for r in store.list_runs(limit=5) if r["status"] not in ("running", "active")), None)

    ended = world.wait(run_ended, "the run to end")
    assert ended["status"] == "done", ended
    look, make = world.calls()
    assert look["rule"] == 1 and "--resume" not in look["argv"]
    store = Store(world.home / "locks.db")
    try:
        tasks = {t["workflow_step"]: t for t in store.run_tasks(ended["id"])}
        kept = store.attempt(tasks["look"]["id"], 1)["session_id"]
        briefed = store.events_of(tasks["make"]["id"], "briefed")[0]["data"]
    finally:
        store.close()
    assert kept and make["rule"] == 0
    assert make["argv"][make["argv"].index("--resume") + 1] == kept and "--fork-session" in make["argv"]
    assert make["cwd"] != look["cwd"] and briefed["trunk_arm"] == "fork"
    world.healthy()


def test_a_question_answered_chooses_a_branch_that_runs_another_workflow_to_its_end(world, tmp_path):
    """A run asks the person, waits with no worker on the question, and their answer chooses the
    step that runs another workflow; that run's end passes the step, the run goes on from its
    branch, and the step the answer did not choose is skipped."""
    (tmp_path / "inner.json").write_text(json.dumps({"name": "inner", "inputs": ["bug"], "steps": [
        {"id": "repair", "prompt": "INNER-STEP: fix {bug}", "kind": "implement"}]}))
    (tmp_path / "outer.json").write_text(json.dumps({"name": "outer", "inputs": ["goal"], "steps": [
        {"id": "how", "ask": "Repair {goal} with the inner workflow?", "options": ["inner", "skip"]},
        {"id": "repair", "workflow": "inner", "with": {"bug": "{goal}"}, "when": {"how": "inner"}},
        {"id": "other", "prompt": "OTHER-STEP: {goal}", "kind": "implement", "needs": ["how"],
         "when": {"how": "skip"}},
        {"id": "ship", "prompt": "SHIP-STEP: ship {goal}", "kind": "implement", "needs": ["repair", "other"]}]}))
    for name in ("inner", "outer"):
        world.locks("workflow", "save", str(tmp_path / f"{name}.json"), "--scope", "user")
    world.script([{"match": "INNER-STEP", "do": [{"pass": True, "write": {"fixed.txt": "fixed\n"}}]},
                  {"match": "SHIP-STEP", "do": [{"pass": True, "write": {"shipped.txt": "shipped\n"}}]}])
    world.locks("workflow", "run", "outer", "the greeting")
    asked = world.wait(_task(("blocked",)), "the question to wait on the person")
    assert world.calls() == []  # no worker ran the question
    run_id = asked["workflow_run"]
    world.locks("workflow", "answer", str(run_id), "how", "inner")

    def outer_ended(store):
        run = store.get_run(run_id)
        return run if run["status"] not in ("running", "active") else None

    ended = world.wait(outer_ended, "the outer run to end")
    assert ended["status"] == "done", ended
    assert [c["rule"] for c in world.calls()] == [0, 1]
    store = Store(world.home / "locks.db")
    try:
        steps = {s["id"]: s for s in workflows.status(store, run_id)["steps"]}
        inner = workflows.status(store, steps["repair"]["subrun"])
    finally:
        store.close()
    assert inner["state"] == "done" and steps["other"].get("skipped") and steps["how"]["answer"] == "inner"
    # The last step started from the inner run's branch: its worker saw the fix.
    assert git(world.repo, "show", f"{steps['ship']['branch']}:fixed.txt") == "fixed"
    world.healthy()


def test_thirty_tasks_in_one_repository_keep_their_reports_short_and_their_memory_true(world):
    """A day of work in one repository: what a person's session reads after each task stays
    short (a line the repository already heard today is not said again), and every problem a
    failing attempt opened is solved once its task passes."""
    from locks import repo as repo_mod

    from .conftest import git

    workflows = world.repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("jobs:\n  e2e:\n    runs-on: ubuntu-latest\n    steps:\n"
                                      "      - run: npx playwright test\n")
    git(world.repo, "add", "-A")
    git(world.repo, "commit", "-qm", "ci")
    # Every other task fails its first attempt whatever its cell: a start the record predicts (`locks.predict`)
    # is wrong here by construction, and a raised start then races (`race_cell`), whose losers open no problem.
    world.setenv("LOCKS_PREDICT", "off")
    fail = {"pass": True, "block": {"verdict": "fail", "failure": "code_bug", "summary": "the driver still drops it"}}
    world.script([{"match": f"radio driver {n} ", "do": [fail, {"pass": True, "write": {f"d{n}.py": "ok\n"}}]
                   if n % 2 else [{"pass": True, "write": {f"d{n}.py": "ok\n"}}]}
                  # A brief carries earlier tasks' dead ends, never later ones': the latest task's rule first.
                  for n in reversed(range(LONG_RUN))])
    reports = [world.locks("run", "--kind", "bugfix", "--verify", "true", "--max-attempts", "3",
                           f"fix the radio driver {n} that drops the last frame of a burst")
               for n in range(LONG_RUN)]
    assert sum("CI also runs" in r for r in reports) == 1
    longest = max(len(r) for r in reports[1:])
    assert longest < 2500, max(reports[1:], key=len)
    store = Store(world.home / "locks.db")
    try:
        assert {t["status"] for t in store.list_tasks(limit=LONG_RUN + 5)} == {"done"}
        key = repo_mod.key(world.repo)
        problems = store.recent_problems(repos=[key], limit=50)
        assert len(problems) == LONG_RUN // 2 and {p["state"] for p in problems} == {"solved"}
        assert not [f for f in doctor.check(store) if f.rule in ("problem-open-after-pass", "turns-under-the-wall")]
    finally:
        store.close()
    world.healthy()
