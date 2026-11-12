"""locks doctor: every rule is a bug locks shipped, turned into a query over the store."""
import json
from datetime import UTC, datetime, timedelta

from locks import cli, doctor
from locks.store import Store

LATER = datetime.now(UTC) + timedelta(hours=1)


def rules(store: Store, **kw) -> dict[str, list[doctor.Finding]]:
    out: dict[str, list[doctor.Finding]] = {}
    for found in doctor.check(store, days=0, **kw):
        out.setdefault(found.rule, []).append(found)
    return out


def test_a_clean_store_has_nothing_to_say(store: Store):
    assert doctor.check(store) == []
    assert doctor.text([]) == "locks doctor: nothing broken, nothing suspect."


def test_a_lane_paused_on_a_task_that_passed_is_broken_and_one_paused_on_a_live_stop_is_not(store: Store):
    passed = store.create_task("x", status="done", source="locks", repo="r")
    store.pause_lane("r", f"task #{passed['id']}: its process exited unexpectedly")
    store.enqueue("next", repo="r", cwd="/")
    found = rules(store, now=LATER, alive=lambda pid: True)["lane-paused-on-ended"][0]
    assert found.severity == doctor.BROKEN and found.task_id == passed["id"]
    assert "1 queued task(s) wait on nothing" in found.what and found.fix == "locks lanes --unpause r"
    stopped = store.create_task("y", status="blocked", source="locks", repo="q")
    store.pause_lane("q", f"task #{stopped['id']} ended blocked: a rule")
    assert [f.repo for f in rules(store, now=LATER, alive=lambda pid: True)["lane-paused-on-ended"]] == ["r"]


def test_running_with_its_process_gone_and_queued_work_nobody_started_are_broken(store: Store):
    dead = store.create_task("x", status="running", source="locks", repo="r", pid=999999)
    store.create_task("y", status="running", source="locks", repo="r", pid=1)
    waiting = store.enqueue("z", repo="q", cwd="/")
    found = rules(store, now=LATER, alive=lambda pid: pid == 1)
    assert [f.task_id for f in found["running-with-no-process"]] == [dead["id"]]
    # The lane with running work is busy, not stuck; the other one is.
    assert [f.task_id for f in found["queued-with-no-dispatcher"]] == [waiting["id"]]
    assert "queued-with-no-dispatcher" not in rules(store, now=datetime.now(UTC), alive=lambda pid: True)


def test_work_held_behind_work_that_left_the_board_is_broken(store: Store):
    gone = store.create_task("x", status="dismissed", source="locks", repo="r")
    live = store.create_task("y", status="blocked", source="locks", repo="r")
    stuck = store.enqueue("after x", repo="r", cwd="/", options={"after": {"task": gone["id"]}})
    store.enqueue("after y", repo="r", cwd="/", options={"after": {"task": live["id"]}})
    lost = store.enqueue("after a run", repo="r", cwd="/", options={"after": {"run": 99}})
    found = rules(store, now=LATER, alive=lambda pid: True)["held-behind-gone"]
    assert sorted(f.task_id for f in found) == [stuck["id"], lost["id"]]
    assert all(f.severity == doctor.BROKEN and f.fix.startswith(f"locks dismiss {f.task_id}") for f in found)
    assert "left the board" in next(f for f in found if f.task_id == stuck["id"]).what


def test_an_automatic_resume_that_started_no_attempt_is_broken(store: Store):
    task = store.create_task("x", status="interrupted", source="locks", repo="r")
    store.add_event(task["id"], "auto_resumed", cause="died", after="killed")
    assert rules(store, now=LATER, alive=lambda pid: True)["resume-never-ran"][0].task_id == task["id"]
    store.add_event(task["id"], "attempt_started", seq=2, cell="sonnet/high", max_turns=80)
    assert "resume-never-ran" not in rules(store, now=LATER, alive=lambda pid: True)


def test_a_part_that_lost_its_mark_and_a_join_that_skipped_kept_branches_are_suspect(store: Store):
    part = {"id": "api", "n": 1, "of": 2, "plan": 1}
    a = store.create_task("a", status="done", source="queue", repo="r", workflow_run=7, workflow_step="build",
                          options=json.dumps({"part": part}))
    b = store.create_task("b", status="done", source="queue", repo="r", workflow_run=7, workflow_step="build",
                          options=json.dumps({"kind": "feature"}))
    for t in (a, b):
        store.add_event(t["id"], "finished", status="done", branch=f"locks/task-{t['id']}")
    join = store.create_task("join", status="done", source="queue", repo="r", workflow_run=7,
                             workflow_step="integrate")
    store.add_event(join["id"], "finished", status="done", branch=None, skipped=True)
    found = rules(store, now=LATER, alive=lambda pid: True)
    assert [f.task_id for f in found["part-lost-its-mark"]] == [b["id"]]
    # Only one part still says it is one, so the join reads one branch: the rule sees the lost one too
    # once its mark is back, as the store held it before the fix.
    store.update_task(b["id"], options=json.dumps({"part": {**part, "id": "web", "n": 2}}))
    assert rules(store, now=LATER, alive=lambda pid: True)["join-short-of-parts"][0].task_id == join["id"]


def test_worktree_attempts_that_keep_no_session_and_low_turns_and_format_climbs_are_suspect(store: Store):
    for n in range(3):
        t = store.create_task(f"t{n}", status="done", source="locks", repo="r", kind="implement")
        store.add_event(t["id"], "attempt_started", seq=1, base="abc", max_turns=40)
        store.add_attempt(t["id"], cell="sonnet/high", max_turns=40, passed=1, session_id="s" if n == 0 else None)
    asked = store.create_task("asked", status="done", source="locks", repo="r", kind="implement",
                              options=json.dumps({"max_turns": 12}))
    store.add_event(asked["id"], "attempt_started", seq=1, base="abc", max_turns=12)
    plan = store.create_task("plan", status="done", source="locks", repo="r", kind="plan")
    store.add_attempt(plan["id"], cell="opus/xhigh", max_turns=80, passed=0, failure="inconclusive",
                      summary="claimed a pass, but its plan cannot run in parts: part 'x' has no prompt")
    store.add_event(plan["id"], "moved", seq=1, axis="effort", trigger="inconclusive", from_cell="opus/xhigh",
                    to_cell="opus/max")
    found = rules(store, now=LATER, alive=lambda pid: True)
    assert "2 of 3 worktree attempts kept no session id" in found["attempts-with-no-session"][0].what
    assert found["turns-under-the-kind"][0].what == "3 implement task(s) started under the kind's 80 turns (latest #3)"
    assert found["format-failure-climbed"][0].task_id == plan["id"]
    assert "opus/xhigh → opus/max" in doctor.text(doctor.check(store, days=0))


def test_the_command_exits_1_only_when_something_is_broken(store: Store, capsys):
    assert cli.main(["doctor"]) == 0
    passed = store.create_task("x", status="done", source="locks", repo="r")
    store.pause_lane("r", f"task #{passed['id']} ended done")
    assert cli.main(["doctor", "--json"]) == 1
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])[0]["rule"] == "lane-paused-on-ended"


def test_records_from_before_the_release_that_fixes_a_rule_are_history_never_a_sign(store: Store):
    for n, version in enumerate(("0.29.1", "0.29.1", "0.29.1", None)):
        t = store.create_task(f"t{n}", status="done", source="locks", repo="r", kind="implement")
        store.add_event(t["id"], "attempt_started", seq=1, base="abc", max_turns=40, version=version)
        store.add_attempt(t["id"], cell="sonnet/high", max_turns=40, passed=1)
    found = rules(store, now=LATER, alive=lambda pid: True)
    assert {f.severity for fs in found.values() for f in fs} == {doctor.FIXED}
    assert found["attempts-with-no-session"][0].what == (
        "4 record(s) from before 0.29.2, the release that fixes it (latest #4)")
    said = doctor.text(doctor.check(store, days=0))
    assert said.startswith("locks doctor: nothing broken, nothing suspect.\nfixed since, history only:")
    # The same bug written by a release that fixes it is the bug come back.
    t = store.create_task("t", status="done", source="locks", repo="r", kind="implement")
    store.add_event(t["id"], "attempt_started", seq=1, base="abc", max_turns=40, version="0.29.2")
    store.add_attempt(t["id"], cell="sonnet/high", max_turns=40, passed=1)
    back = rules(store, now=LATER, alive=lambda pid: True)["turns-under-the-kind"]
    assert [(f.severity, f.task_id) for f in back] == [(doctor.SUSPECT, t["id"]), (doctor.FIXED, 4)]


def test_the_events_doctor_reads_keep_the_version_that_wrote_them(store: Store):
    from locks import __version__

    t = store.create_task("t", status="done", source="locks", repo="r")
    for kind in ("planned", "attempt_started", "moved", "finished", "report"):
        store.add_event(t["id"], kind, seq=1)
    kept = {e["kind"]: e["data"].get("version") for e in store.events(task_id=t["id"])}
    assert kept == {"planned": __version__, "attempt_started": __version__, "moved": __version__,
                    "finished": __version__, "report": None}


def test_a_fix_round_that_did_not_start_on_the_round_before_is_suspect(store: Store):
    run = store.create_run(name="feature", scope="bundled", source="x", repo="r",
                           definition=json.dumps({"name": "feature", "steps": []}))
    first = store.create_task("fix", status="done", source="queue", repo="r", workflow_run=run["id"],
                              workflow_step="fix", options=json.dumps({"ledger": {"role": "fix", "round": 1,
                                                                                  "base": "b0"}}))
    store.add_event(first["id"], "finished", status="done", branch=f"locks/task-{first['id']}", tip="t1")
    for base, version in (("b0", "0.31.0"), ("t1", "0.31.0")):
        again = store.create_task("fix", status="done", source="queue", repo="r", workflow_run=run["id"],
                                  workflow_step="fix", options=json.dumps({"ledger": {"role": "fix", "round": 2,
                                                                                      "base": base}}))
        store.add_event(again["id"], "planned", version=version)
    found = rules(store, now=LATER, alive=lambda pid: True).get("fix-round-off-base", [])
    # Only the round that started where round 1 started is off its base.
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, first["id"] + 1)]


def test_a_note_made_current_as_proven_with_too_few_briefs_that_carried_it_is_suspect(store: Store):
    from locks import notes

    writer = store.create_task("x", status="done", source="locks", repo="r")
    note = store.add_note("r", "code", "t", "Totals are rounded half up", author="worker", filed_by="haiku",
                          task_id=writer["id"], state=notes.CANDIDATE)
    reader = store.create_task("y", status="done", source="locks", repo="r")
    store.add_event(reader["id"], "briefed", **{notes.BRIEFED: [note]})
    store.add_event(writer["id"], "briefed", **{notes.BRIEFED: [note]})  # its own brief proves nothing
    store.update_note(note, state="current", state_reason=notes.PROVEN + "in the brief of a guess")
    found = rules(store, now=LATER, alive=lambda pid: True)["note-proven-unbriefed"]
    assert [(f.severity, f.repo) for f in found] == [(doctor.SUSPECT, "r")] and f"note #{note}" in found[0].what
    assert doctor.FIXED_IN["note-proven-unbriefed"] == "0.32.0"
    # Proven by the tasks that read it, or a note a person confirmed: nothing to say.
    more = store.create_task("z", status="done", source="locks", repo="r")
    store.add_event(more["id"], "briefed", **{notes.BRIEFED: [note]})
    person = store.add_note("r", "code", "u", "Invoices are numbered per tenant", author="person", filed_by="person")
    store.update_note(person, state="current", state_reason=None)
    assert "note-proven-unbriefed" not in rules(store, now=LATER, alive=lambda pid: True)


def test_a_wait_on_a_decision_about_work_that_all_ended_is_broken(store: Store):
    gone = store.create_task("rewrite the importer", status="done", source="queue", repo="r")
    long = store.create_task("build the long feature", status="running", source="queue", repo="r", pid=1)
    held = store.create_task("add a flag", status="queued", source="queue", repo="r")
    store.set_parallel(held["id"], False, "it changes the importer")
    store.add_event(held["id"], "dispatch_decided", parallel=False, reason="x", ahead=[gone["id"]])
    found = rules(store, now=LATER, alive=lambda pid: True)["wait-judged-on-gone"]
    assert [f.task_id for f in found] == [held["id"]] and found[0].fix.startswith(f"locks lanes --beside {held['id']}")
    store.add_event(held["id"], "dispatch_decided", parallel=False, reason="x", ahead=[long["id"]])
    assert "wait-judged-on-gone" not in rules(store, now=LATER, alive=lambda pid: True)


def test_a_problem_left_open_after_its_task_passed_is_suspect(store: Store):
    task = store.create_task("fix the ble race", status="done", source="locks", repo="r")
    problem = store.open_problem("fix the ble race", repo="r")
    store.add_fix(problem, "sonnet/high: retried", "failed", repo="r", task_id=task["id"])
    store.add_event(task["id"], "finished", status="done", version="0.40.0")
    old = store.create_task("fix the scanner", status="done", source="locks", repo="r")
    other = store.open_problem("fix the scanner", repo="r")
    store.add_fix(other, "x", "failed", repo="r", task_id=old["id"])
    store.add_event(old["id"], "finished", status="done", version="0.39.0")
    found = rules(store, now=LATER, alive=lambda pid: True)["problem-open-after-pass"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, task["id"]), (doctor.FIXED, old["id"])]
    store.add_fix(problem, "opus/high: serialized it", "worked", repo="r", task_id=task["id"])
    assert [f.severity for f in rules(store, now=LATER, alive=lambda pid: True)["problem-open-after-pass"]] == [
        doctor.FIXED]


def test_a_flake_fix_that_passed_on_one_run_is_suspect(store: Store):
    task = store.create_task("fix the flaky esp-now test", status="done", source="locks", repo="r", kind="bugfix")
    store.add_event(task["id"], "finished", status="done", version="0.40.0",
                    verdict={"check": {"command": "deno task test", "passed": True}})
    held = store.create_task("fix the flaky ble test", status="done", source="locks", repo="r", kind="bugfix")
    store.add_event(held["id"], "finished", status="done", version="0.40.0",
                    verdict={"check": {"command": "deno task test", "passed": True, "runs": 3}})
    found = rules(store, now=LATER, alive=lambda pid: True)["flake-fix-checked-once"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, task["id"])]


def test_a_long_attempt_that_kept_few_turns_is_suspect(store: Store):
    task = store.create_task("ui", status="done", source="locks", repo="r")
    store.add_event(task["id"], "attempt_finished", seq=1, wall_s=48 * 60, turns=2, version="0.40.0")
    store.add_event(task["id"], "attempt_finished", seq=2, wall_s=48 * 60, turns=40, version="0.40.0")
    store.add_event(task["id"], "attempt_finished", seq=3, wall_s=5 * 60, turns=1, version="0.40.0")
    found = rules(store, now=LATER, alive=lambda pid: True)["turns-under-the-wall"]
    assert [(f.severity, f.what) for f in found] == [(doctor.SUSPECT, f"#{task['id']} attempt 1 ran 48m and kept 2 "
                                                                      "turn(s)")]


def test_work_picked_up_late_while_its_lane_was_idle_is_suspect(store: Store):
    def at(event_id: int, minutes: float) -> None:
        store._conn.execute("UPDATE events SET ts = ? WHERE id = ?",
                            ((datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds"),
                             event_id))

    def waited(repo: str, version: str = "0.45.0", **pickup) -> dict:
        task = store.create_task("review it", status="done", source="locks", repo=repo)
        at(store.add_event(task["id"], "dispatched", at=0), 0)
        store.add_event(task["id"], "finished", status="done", version=version, pickup_s=20 * 60, **pickup)
        return task

    lost = waited("r")
    held = waited("q", pickup_conflict_s=19 * 60)  # a dispatch decision held it: the wait was its
    busy = waited("b")
    other = store.create_task("build", status="done", source="locks", repo="b")
    at(store.add_event(other["id"], "attempt_started", seq=1), 25)
    at(store.add_event(other["id"], "attempt_finished", seq=1), 5)  # a worker ran in its lane meanwhile
    old = waited("r", version="0.44.0")
    found = rules(store, now=LATER, alive=lambda pid: True)["pickup-nobody-saw"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, lost["id"]), (doctor.FIXED, old["id"])]
    assert "waited 20 min" in found[0].what and held["id"] != busy["id"]


# --- livespec's index ----------------------------------------------------------------


def _ran_in(store: Store, where, *, status="done"):
    task = store.create_task("work", status=status, source="locks", cwd=str(where))
    store.add_event(task["id"], "planned", kind="implement")
    return task


def _livespec(tmp_path, *, runnable=True):
    from .test_livespec_adapter import adapter, tool

    return adapter(tmp_path, tool(tmp_path, "uvx") if runnable else "")


def test_a_repository_with_no_livespec_index_is_suspect_and_the_fix_is_the_command_that_builds_it(
        store: Store, git_repo, tmp_path):
    from locks import repo

    _ran_in(store, git_repo)
    found = rules(store, livespec=_livespec(tmp_path))["index-missing"]
    assert len(found) == 1
    assert found[0].severity == doctor.SUSPECT and found[0].repo == repo.key(git_repo)
    assert found[0].fix == f"locks index-livespec {git_repo.resolve()}"
    assert "repo: no livespec index" in found[0].what and "not indexed yet" in found[0].what
    cannot = rules(store, livespec=_livespec(tmp_path, runnable=False))["index-missing"][0]
    assert "needs livespec, or `uv` to run the pinned release" in cannot.fix


def test_the_folders_runs_were_in_are_read_once_each_however_many_runs(store: Store, git_repo, tmp_path):
    """A store of months holds thousands of runs in a handful of folders: each folder's
    repositories are read with git once, not once per run (`locks doctor` took 33 s on 3000)."""
    livespec = _livespec(tmp_path)
    asked: list = []
    roots = livespec.roots
    livespec.roots = lambda where: asked.append(where) or roots(where)
    for _ in range(30):
        _ran_in(store, git_repo)
    assert len(doctor._checkouts(store, None, livespec)) == 1
    assert asked == [git_repo]


def test_an_index_older_than_the_last_commit_or_one_that_cannot_be_read_is_suspect(store: Store, git_repo, tmp_path):
    from .livespec_fixture import build

    _ran_in(store, git_repo)
    db = build(git_repo, indexed_at="2000-01-01 00:00:00")
    found = rules(store, livespec=_livespec(tmp_path))
    assert [f.severity for f in found["index-stale"]] == [doctor.SUSPECT]
    assert "older than the last commit" in found["index-stale"][0].what and "index-missing" not in found
    db.write_bytes(b"not a database")
    assert "cannot be read" in rules(store, livespec=_livespec(tmp_path))["index-unreadable"][0].what


def test_a_current_index_is_clean_and_specs_it_holds_with_no_code_are_named(store: Store, git_repo, tmp_path):
    import sqlite3

    from .livespec_fixture import build

    _ran_in(store, git_repo)
    db = build(git_repo)
    assert rules(store, livespec=_livespec(tmp_path)) == {}  # SPEC-7 is linked to charge_card
    with sqlite3.connect(db) as conn:
        for n in range(8):
            conn.execute("INSERT INTO spec (project_id, spec_id, title) VALUES (1, ?, ?)", (f"loose-{n}", f"Loose {n}"))
        conn.execute("INSERT INTO spec (project_id, spec_id, title, status) VALUES (1, 'gone', 'Gone', 'deprecated')")
        conn.execute("INSERT INTO spec (id, project_id, spec_id, title) VALUES (99, 1, 'related', 'Related')")
        conn.execute("INSERT INTO spec_symbol (spec_id, symbol_id, relation) VALUES (99, 2, 'references')")
    found = rules(store, livespec=_livespec(tmp_path))["specs-with-no-code"]
    assert len(found) == 1 and found[0].severity == doctor.SUSPECT
    # A deprecated spec owes no code; a spec that code only references is still unimplemented.
    assert "9 spec(s)" in found[0].what and "gone" not in found[0].what
    assert "loose-0, loose-1, loose-2, loose-3, loose-4 and 4 more" in found[0].what
    assert "`@spec:<id>`" in found[0].fix and f"locks index-livespec {git_repo.resolve()}" in found[0].fix


def test_a_folder_of_repositories_is_read_through_each_one_and_never_as_the_folder(store: Store, tmp_path):
    from .test_livespec_adapter import make_repo

    folder = tmp_path / "folder"
    make_repo(folder, "api")
    make_repo(folder, "web")
    _ran_in(store, folder)
    _ran_in(store, folder)
    found = rules(store, livespec=_livespec(tmp_path))["index-missing"]
    assert sorted(f.what.split(":")[0] for f in found) == ["api", "web"]
    assert {f.fix for f in found} == {f"locks index-livespec {folder.resolve() / name}" for name in ("api", "web")}


def test_the_index_rules_say_nothing_with_livespec_off_or_for_a_repository_no_run_was_in(
        store: Store, git_repo, tmp_path, monkeypatch):
    _ran_in(store, git_repo)
    monkeypatch.setenv("LOCKS_LIVESPEC", "off")
    assert doctor.check(store, days=0) == []  # the setting, with no adapter handed in
    monkeypatch.setenv("LOCKS_LIVESPEC", "on")
    quiet = Store(tmp_path / "other.db")
    try:
        assert doctor.check(quiet, days=0, livespec=_livespec(tmp_path)) == []
        _ran_in(quiet, tmp_path / "gone")  # a folder that is no longer there
        assert doctor.check(quiet, days=0, livespec=_livespec(tmp_path)) == []
    finally:
        quiet.close()


def test_the_doctor_filters_the_index_findings_to_one_repository(store: Store, git_repo, tmp_path):
    from locks import repo

    _ran_in(store, git_repo)
    mine = doctor.check(store, days=0, repo=repo.key(git_repo), livespec=_livespec(tmp_path))
    elsewhere = doctor.check(store, days=0, repo="github.com/x/y", livespec=_livespec(tmp_path))
    assert [f.rule for f in mine] == ["index-missing"] and elsewhere == []


def test_a_run_that_died_after_its_attempt_passed_is_suspect(store: Store):
    task = store.create_task("docs", status="interrupted", source="locks", repo="r")
    store.add_event(task["id"], "attempt_finished", seq=1, passed=True, version="0.63.0")
    store.add_event(task["id"], "finished", status="interrupted", version="0.63.0",
                    stop={"cause": "died", "by": "locks", "reason": "its process exited with 1"})
    failed = store.create_task("build", status="interrupted", source="locks", repo="r")
    store.add_event(failed["id"], "attempt_finished", seq=1, passed=False, version="0.63.0")
    store.add_event(failed["id"], "finished", status="interrupted", version="0.63.0", stop={"cause": "died"})
    old = store.create_task("old", status="interrupted", source="locks", repo="r")
    store.add_event(old["id"], "attempt_finished", seq=1, passed=True, version="0.62.0")
    store.add_event(old["id"], "finished", status="interrupted", version="0.62.0", stop={"cause": "died"})
    found = rules(store, now=LATER, alive=lambda pid: True)["died-after-a-pass"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, task["id"]), (doctor.FIXED, old["id"])]
    assert "died after its attempt 1 passed" in found[0].what


def test_a_task_resumed_on_its_own_after_its_work_landed_is_suspect(store: Store):
    def died_resumed_landed(version: str, *, resumed: bool = True) -> int:
        task = store.create_task("work", status="dismissed", source="locks", repo="r")["id"]
        store.add_event(task, "finished", status="interrupted", version=version, stop={"cause": "died"})
        if resumed:
            store.add_event(task, "auto_resumed", after="its process is gone", cause="died")
        store.add_event(task, "finished", status="dismissed", version=version,
                        stop={"cause": "landed", "by": "locks", "reason": "its change is already in main"})
        return task

    now = died_resumed_landed("0.66.0")
    died_resumed_landed("0.66.0", resumed=False)
    old = died_resumed_landed("0.65.0")
    found = rules(store, now=LATER, alive=lambda pid: True)["resumed-after-it-landed"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, now), (doctor.FIXED, old)]


def test_compaction_advice_given_twice_in_one_prompt_is_suspect(store: Store):
    prompt = store.create_task("a turn", status="done", source="hook", session_id="s", repo="r")
    for decision in (1, 2):
        store.add_event(prompt["id"], "compaction_delivered", decision=decision, how="advise", locks="0.82.0")
    once = store.create_task("another turn", status="done", source="hook", session_id="s", repo="r")
    store.add_event(once["id"], "compaction_delivered", decision=3, how="advise", locks="0.82.0")
    old = store.create_task("an old turn", status="done", source="hook", session_id="s", repo="r")
    for decision in (4, 5):
        store.add_event(old["id"], "compaction_delivered", decision=decision, how="advise", locks="0.81.0")
    found = rules(store, now=LATER, alive=lambda pid: True)["advised-again-in-a-prompt"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, prompt["id"]), (doctor.FIXED, old["id"])]


def test_a_habits_review_that_passed_and_nothing_applied_is_suspect(store: Store):
    def review(version: str, *kinds: str) -> int:
        task = store.create_task("[habits review]", status="done", source="locks", repo="r",
                                 options=json.dumps({"kind": "explore", "habit_review": ["913c3597"]}))
        store.add_event(task["id"], "finished", status="done", version=version)
        for kind in kinds:
            store.add_event(task["id"], kind, candidate="913c3597", command="x", why="y")
        return task["id"]

    unapplied = review("0.85.0")
    applied = review("0.85.0", "habit_installed")
    refused = review("0.85.0", "habit_refused")
    skipped = review("0.85.0", "habit_skipped")  # it skipped every habit: applied, nothing to install
    marked = review("0.85.0")  # skipped before `habit_skipped`: only apply_reviews' mark says so
    store.set_meta(f"habits.applied:{marked}", "1")
    old = review("0.84.0")
    plain = store.create_task("not a review", status="done", source="locks", repo="r")
    store.add_event(plain["id"], "finished", status="done", version="0.85.0")
    found = rules(store, now=LATER, alive=lambda pid: True)["habit-review-unapplied"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, unapplied), (doctor.FIXED, old)]
    assert "never applied it" in found[0].what and str(unapplied) in found[0].fix
    assert {applied, refused, skipped, marked} & {f.task_id for f in found} == set()


def test_an_attempt_that_says_nothing_was_logged_while_the_store_holds_its_calls_is_suspect(store: Store):
    t = store.create_task("t", status="done", source="locks", repo="r", kind="explore")
    store.add_attempt(t["id"], cell="haiku/medium", max_turns=30, passed=1)
    store.add_tool_event(task_id=t["id"], attempt=1, tool="read", sig="read", arg_hash="x", ok=1)
    store.add_event(t["id"], "attempt_finished", seq=1, cell="haiku/medium", passed=True, tools_logged=False,
                    version="0.89.0")
    found = rules(store, now=LATER, alive=lambda pid: True)["tools-logged-off"]
    assert [(f.severity, f.what) for f in found] == [
        (doctor.SUSPECT, f"#{t['id']} attempt 1 says no tool call was logged, and the store holds 1 of its worker's")]
    # One honestly unlogged (no call in the store) is no finding; one from before 0.89.0 is history.
    u = store.create_task("u", status="done", source="locks", repo="r", kind="explore")
    store.add_event(u["id"], "attempt_finished", seq=1, cell="haiku/medium", passed=True, tools_logged=False,
                    version="0.89.0")
    v = store.create_task("v", status="done", source="locks", repo="r", kind="explore")
    store.add_tool_event(task_id=v["id"], attempt=1, tool="read", sig="read", arg_hash="x", ok=1)
    store.add_event(v["id"], "attempt_finished", seq=1, cell="haiku/medium", passed=True, tools_logged=False,
                    version="0.88.0")
    found = rules(store, now=LATER, alive=lambda pid: True)["tools-logged-off"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, t["id"]), (doctor.FIXED, v["id"])]


def test_a_problem_a_pass_opened_beside_a_race_s_other_try_is_suspect(store: Store):
    t = store.create_task("t", status="done", source="locks", repo="r", kind="implement")
    store.add_event(t["id"], "attempt_finished", seq=1, cell="sonnet/high", passed=False, raced=True, stopped=True)
    store.add_event(t["id"], "attempt_finished", seq=2, cell="sonnet/xhigh", passed=True)
    store.add_event(t["id"], "finished", status="done", version="0.90.0")
    problem = store.open_problem("t", repo="r", symptom="t")
    store.add_fix(problem, "sonnet/xhigh: done", "worked", repo="r", task_id=t["id"])
    found = rules(store, now=LATER, alive=lambda pid: True)["problem-opened-by-a-race"]
    assert [(f.severity, f.what) for f in found] == [
        (doctor.SUSPECT, f"#{t['id']} passed beside a race's other try and opened problem #{problem} with no "
                         "failure of its own")]
    # A task whose race's other try failed its own check before the pass opened its problem honestly.
    u = store.create_task("u", status="done", source="locks", repo="r", kind="implement")
    store.add_event(u["id"], "attempt_finished", seq=1, cell="sonnet/high", passed=False, raced=True, stopped=False)
    store.add_event(u["id"], "attempt_finished", seq=2, cell="sonnet/xhigh", passed=True)
    store.add_event(u["id"], "finished", status="done", version="0.90.0")
    p2 = store.open_problem("u", repo="r", symptom="u")
    store.add_fix(p2, "sonnet/high: no", "failed", repo="r", task_id=u["id"])
    store.add_fix(p2, "sonnet/xhigh: done", "worked", repo="r", task_id=u["id"])
    assert [f.task_id for f in rules(store, now=LATER, alive=lambda pid: True)["problem-opened-by-a-race"]] == [t["id"]]


def _clean_review(store: Store, *, kind: str = "review-routine", read_calls: int | None = 0, seq: int = 1,
                  tools_logged: bool = True, passed: bool = True, version: str = "0.91.0", status: str = "done",
                  findings: tuple = ()) -> int:
    """A review task whose attempt `seq` ended as `behavior` says and whose run ended `status`."""
    t = store.create_task("review the change", status="done", source="locks", repo="r", kind=kind)
    worked = {"calls": 4} if read_calls is None else {"calls": 4, "read_calls": read_calls}
    store.add_event(t["id"], "attempt_finished", seq=seq, cell="sonnet/high", passed=passed,
                    tools_logged=tools_logged, behavior=worked, version=version)
    store.add_event(t["id"], "finished", status=status, findings=list(findings))
    return t["id"]


def test_a_review_that_passed_clean_while_its_calls_show_it_read_nothing_is_suspect(store: Store):
    t = _clean_review(store)
    other = _clean_review(store, kind="review-critical")
    found = rules(store, now=LATER, alive=lambda pid: True)["clean-review-read-nothing"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, t), (doctor.SUSPECT, other)]
    assert found[0].what.startswith(f"#{t} attempt 1 passed a review with no finding") and found[0].repo == "r"
    assert "read nothing" in found[0].what and doctor.FIXED_IN["clean-review-read-nothing"] == "0.91.0"
    assert doctor.clean_reviews_read_nothing in doctor.RULES
    # The window reads the same: an attempt finished now is inside the default two days.
    assert [f.rule for f in doctor.check(store, now=LATER, alive=lambda pid: True)].count(
        "clean-review-read-nothing") == 2


def test_a_clean_review_from_before_the_release_that_holds_it_back_is_history(store: Store):
    _clean_review(store, version="0.90.0")
    unversioned = _clean_review(store, version=None)
    found = rules(store, now=LATER, alive=lambda pid: True)["clean-review-read-nothing"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.FIXED, unversioned)]
    assert found[0].what == f"2 record(s) from before 0.91.0, the release that fixes it (latest #{unversioned})"
    now = _clean_review(store)
    assert [(f.severity, f.task_id) for f in rules(store, now=LATER, alive=lambda pid: True)[
        "clean-review-read-nothing"]] == [(doctor.SUSPECT, now), (doctor.FIXED, unversioned)]


def test_a_clean_review_that_read_something_was_held_back_or_is_no_review_is_not_a_finding(store: Store):
    _clean_review(store, read_calls=2)  # it read
    _clean_review(store, tools_logged=False)  # unknown is not none
    _clean_review(store, read_calls=None)  # recorded before the count existed
    _clean_review(store, passed=False)  # an attempt that did not pass was not taken as clean
    _clean_review(store, kind="implement")  # not a review
    _clean_review(store, kind="explore")
    _clean_review(store, findings=({"where": "a.py:1", "what": "x"},))  # it found something
    _clean_review(store, status="blocked")  # its run did not end as a pass
    held = _clean_review(store)
    store.add_event(held, "review_unread", seq=1, who="worker", again=False, calls=3)
    elsewhere = _clean_review(store, seq=2)
    store.add_event(elsewhere, "review_unread", seq=1, who="worker", again=False, calls=3)  # another attempt's
    found = rules(store, now=LATER, alive=lambda pid: True).get("clean-review-read-nothing", [])
    assert [f.task_id for f in found] == [elsewhere]
    assert "attempt 2" in found[0].what


def test_a_verifier_hold_at_the_same_seq_does_not_excuse_a_worker_that_passed_unread(store: Store):
    t = _clean_review(store)
    store.add_event(t, "review_unread", seq=1, who="verifier", again=False, calls=2)
    found = rules(store, now=LATER, alive=lambda pid: True)["clean-review-read-nothing"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, t)]


def test_a_transcript_far_ahead_of_what_a_stop_counted_is_suspect(store: Store, tmp_path):
    from locks import usage

    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * (usage.BEHIND_BYTES + 10))
    counted_at = datetime.now(UTC) - timedelta(hours=1)
    store.set_meta(usage.COUNTED_KEY + "s", json.dumps({"at": counted_at.isoformat(), "version": "0.99.0",
                                                       "transcript": str(transcript)}))
    [found] = rules(store, now=datetime.now(UTC), alive=lambda pid: True)["transcript-ahead-of-usage"]
    assert found.severity == doctor.SUSPECT and "session s" in found.what
    # Counted up to the end, or counted a moment ago, or by a release before the fix: nothing to say.
    store.set_transcript_offset(str(transcript), transcript.stat().st_size)
    assert "transcript-ahead-of-usage" not in rules(store, now=datetime.now(UTC), alive=lambda pid: True)
    store.set_transcript_offset(str(transcript), 0)
    assert "transcript-ahead-of-usage" not in rules(store, now=counted_at + timedelta(minutes=1),
                                                    alive=lambda pid: True)
    store.set_meta(usage.COUNTED_KEY + "s", json.dumps({"at": counted_at.isoformat(), "version": "0.98.0",
                                                       "transcript": str(transcript)}))
    assert "transcript-ahead-of-usage" not in rules(store, now=datetime.now(UTC), alive=lambda pid: True)


def test_a_trial_whose_verdict_read_both_ways_is_suspect(store: Store):
    def briefed(why: str, version: str) -> None:
        task = store.create_task("x", status="done", source="queue", repo="r")["id"]
        store.add_event(task, "planned", version=version)
        store.add_event(task, "briefed", primer_arm="primer", primer_why=why)

    briefed("the primer won its trial here: primed first tries passed 90%, without 80%", "0.99.0")
    briefed("the primer lost its trial here: primed first tries passed 80%, without 90%", "0.99.0")
    [old] = rules(store, now=datetime.now(UTC), alive=lambda pid: True)["trial-verdict-turned"]
    assert old.severity == doctor.FIXED  # written before the release that fixes it: history
    briefed("the primer won its trial here: primed first tries passed 90%, without 80%", "0.100.1")
    assert rules(store, now=datetime.now(UTC), alive=lambda pid: True)["trial-verdict-turned"][0].severity == \
        doctor.FIXED  # one way only since the fix
    briefed("the primer lost its trial here: primed first tries passed 80%, without 90%", "0.100.1")
    [found] = rules(store, now=datetime.now(UTC), alive=lambda pid: True)["trial-verdict-turned"]
    assert found.severity == doctor.SUSPECT and "won its trial in 2 brief(s) and lost it in 2" in found.what
    # a pooled verdict beside the repository's own is two trials, never one that turned
    task = store.create_task("x", status="done", source="queue", repo="q")["id"]
    store.add_event(task, "briefed", recall_arm="fused",
                    recall_why="the recall order won its trial everywhere: …; pooled from every repository while …")
    turned = rules(store, now=datetime.now(UTC), alive=lambda pid: True)["trial-verdict-turned"]
    assert [f.repo for f in turned] == ["r"]
