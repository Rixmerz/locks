from __future__ import annotations

import os

import pytest

from locks.store import Store, home


def test_home_honours_locks_home_then_absolute_xdg(tmp_path):
    assert home({"LOCKS_HOME": str(tmp_path)}) == tmp_path
    assert home({"XDG_DATA_HOME": "/x"}).as_posix() == "/x/locks"
    assert home({"XDG_DATA_HOME": "relative"}).name == "locks"


def test_a_task_keeps_its_messages_in_order(store: Store):
    task = store.create_task("fix the parser", status="running", source="hook", session_id="s", prompt_id="p1")
    store.add_message(task["id"], "user", "also the tests")
    store.add_message(task["id"], "assistant", "done")
    assert [m["role"] for m in store.messages(task["id"])] == ["user", "user", "assistant"]
    assert store.running_task("s", "p1")["id"] == task["id"]
    assert store.running_task("s")["id"] == task["id"]
    assert store.running_task("s", "other") is None
    with pytest.raises(ValueError):
        store.create_task("x", status="bogus", source="hook")
    with pytest.raises(ValueError):
        store.update_task(task["id"], status="bogus")
    store.update_task(task["id"])  # nothing to change is not an error


def test_interrupt_sweeps_only_other_hook_tasks(store: Store):
    a = store.create_task("a", status="running", source="hook", session_id="s")
    b = store.create_task("b", status="running", source="hook", session_id="s")
    c = store.create_task("c", status="running", source="locks", session_id="s")
    assert store.interrupt_running("s", exclude=[b["id"]]) == 1
    assert store.get_task(a["id"])["status"] == "interrupted"
    assert store.get_task(b["id"])["status"] == "running"
    assert store.get_task(c["id"])["status"] == "running"
    assert store.last_prompt_task("s")["id"] == b["id"]
    assert [t["id"] for t in store.list_tasks(session_id="s", status=["interrupted"])] == [a["id"]]


def test_attempts_add_up_and_landings_only_count_finished_core_tasks(store: Store):
    for cell in ("sonnet/high", "sonnet/xhigh", "sonnet/high"):
        t = store.create_task("t", status="running", source="locks", kind="ui", repo="r", start_cell="sonnet/medium")
        store.add_attempt(t["id"], cell="sonnet/high", max_turns=30, passed=0, cost_usd=0.5, changed_paths=["a"])
        seq = store.add_attempt(t["id"], cell=cell, max_turns=30, passed=1, cost_usd=0.25)
        store.set_move(t["id"], 1, "more_effort", "shallow")
        store.update_task(t["id"], status="done", final_cell=cell)
    assert seq == 2
    assert store.get_task(t["id"])["cost_usd"] == pytest.approx(0.75)
    assert store.attempts(t["id"])[0]["move"] == "more_effort"
    assert store.attempts(t["id"])[0]["changed_paths"] == '["a"]'
    assert store.landings("ui", repo="r") == ["sonnet/high", "sonnet/xhigh", "sonnet/high"]
    assert store.landings("ui", repo="elsewhere") == []
    hooked = store.create_task("t", status="done", source="hook", kind="ui", repo="r", final_cell="opus/max")
    assert "opus/max" not in store.landings("ui")
    assert hooked["title"] == "t"
    # A pass where it started is no climb, wherever it started: history had raised later
    # starts to such passes, and every task after them started as high. A task with no
    # start recorded says neither.
    store.create_task("t", status="done", source="locks", kind="ui", repo="r", start_cell="opus/high",
                      final_cell="opus/high")
    store.create_task("t", status="done", source="locks", kind="ui", repo="r", final_cell="opus/xhigh")
    assert store.landings("ui", repo="r") == ["", "sonnet/high", "sonnet/xhigh", "sonnet/high"]
    assert store.landings("ui", repo="r", climbs=False) == ["opus/high", "sonnet/high", "sonnet/xhigh", "sonnet/high"]


def test_memory_finds_dead_ends_across_repositories_this_one_first(store: Store):
    other = store.open_problem("login token expires early", repo="github.com/a/other", symptom="401 after refresh")
    store.add_fix(other, "extend the token ttl", "failed", repo="github.com/a/other", why="clock skew, not ttl")
    store.add_fix(other, "sync the server clock", "worked", repo="github.com/a/other")
    here = store.open_problem("token refresh returns 401", repo="github.com/a/here")
    store.add_fix(here, "retry the refresh call", "failed", repo="github.com/a/here", why="same 401")
    assert store.open_problem("token refresh returns 401", repo="github.com/a/here") == here

    found = store.search("token 401 refresh", repo="github.com/a/here")
    assert [p["id"] for p in found] == [here, other]
    assert store.problem(other)["state"] == "solved"
    assert store.problem(999) is None

    dead = store.dead_ends("token refresh 401", repo="github.com/a/here")
    assert dead[0]["tried"] == "retry the refresh call"
    assert dead[1]["worked_instead"] == "sync the server clock"
    assert store.dead_ends(repo="github.com/a/other")[0]["tried"] == "extend the token ttl"
    assert store.dead_ends("token", limit=1) and len(store.dead_ends("token", limit=1)) == 1

    # a strict match needs several shared words; one loose word is not enough
    assert store.dead_ends("the token is fine", strict=True) == []
    assert store.dead_ends("refresh token returns 401 again", strict=True)
    assert store.search("") == [] and store.search("zzz qqq") == []
    with pytest.raises(ValueError):
        store.add_fix(here, "x", "maybe", repo=None)


def test_accents_do_not_split_words(store: Store):
    p = store.open_problem("migración de usuarios falla", repo="r")
    store.add_fix(p, "reintentar la migracion", "failed", repo="r")
    assert store.search("migracion usuarios")[0]["id"] == p


def test_two_stores_share_one_file(_isolated_home):
    a = Store.open()
    a.create_task("x", status="queued", source="locks")
    b = Store.open()
    assert len(b.list_tasks()) == 1
    a.close()
    b.close()


def test_an_older_database_gains_the_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, parent_id INTEGER, "
                 "repo TEXT, cwd TEXT, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', kind TEXT, "
                 "complexity TEXT, class_source TEXT, class_reason TEXT, status TEXT NOT NULL, source TEXT NOT NULL, "
                 "prompt_id TEXT, result TEXT, start_cell TEXT, final_cell TEXT, cost_usd REAL NOT NULL DEFAULT 0, "
                 "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO tasks (title, status, source, created_at, updated_at) VALUES ('old', 'done', 'hook', "
                 "'t', 't')")
    conn.commit()
    conn.close()
    store = Store(path)
    task = store.get_task(1)
    assert task["pid"] is None and task["cancel_requested"] == 0
    assert store.request_cancel(1)["cancel_requested"] == 1 and store.cancel_requested(1)
    assert not store.cancel_requested(999)
    store.close()
    Store(path).close()  # running the migration twice changes nothing


def test_a_solved_problem_that_comes_back_is_recurring(store: Store):
    p = store.open_problem("cache stampede", repo="r", symptom="p99 spikes")
    worked = store.add_fix(p, "add jitter to the ttl", "worked", repo="r", commit_sha="abc123")
    fixed = store.problem(p)
    assert fixed["state"] == "solved" and fixed["fixes"][0]["believed_from"] and fixed["first_seen"]
    assert fixed["fixes"][0]["commit_sha"] == "abc123"
    assert store.open_problem("cache stampede", repo="r") == p
    back = store.problem(p)
    assert back["state"] == "recurring" and back["fixes"][0]["invalidated_on"]
    dead = store.dead_ends("cache stampede", repo="r")
    assert dead[0]["tried"] == "add jitter to the ttl" and dead[0]["disproved_on"]
    assert "came back" in dead[0]["why"]
    assert worked


def test_invalidating_a_fix_by_hand(store: Store):
    p = store.open_problem("flaky upload", repo="r")
    fix = store.add_fix(p, "retry twice", "worked", repo="r")
    assert store.invalidate_fix(fix, "the retry hid a real timeout")["state"] == "recurring"
    assert store.invalidate_fix(9999, "x") is None
    assert store.problem(p)["fixes"][0]["why"] == "the retry hid a real timeout"
    assert store.search("real timeout")[0]["id"] == p
    store.add_fix(p, "raise the server timeout", "worked", repo="r")
    assert store.dead_ends("flaky upload")[0]["worked_instead"] == "raise the server timeout"
    store.set_fix_commit(999, "x")  # no fix for that task: nothing happens


def test_many_openers_at_once_migrate_without_tripping(tmp_path):
    import sqlite3
    import threading

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL "
                 "DEFAULT '', status TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL, "
                 "updated_at TEXT NOT NULL, session_id TEXT, repo TEXT, kind TEXT, final_cell TEXT, "
                 "cost_usd REAL NOT NULL DEFAULT 0)")
    conn.commit()
    conn.close()
    errors = []

    def open_it():
        try:
            Store(path).close()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_notes_never_reach_the_problem_search_and_an_edit_is_searchable(store):
    p = store.open_problem("websocket reconnect loop", repo="r")
    store.add_fix(p, "raise the backoff", "failed", repo="r")
    note = store.add_note("r", "code", "websocket", "the websocket reconnect loop lives in the client",
                          author="person", filed_by="person")
    assert [x["id"] for x in store.search("websocket reconnect")] == [p]
    assert store.search_notes("r", "websocket client") == [note]
    store.update_note(note, text="the socket retries with jitter", title="retries")
    assert store.search_notes("r", "websocket client") == [] and store.search_notes("r", "jitter") == [note]
    assert store.update_note(note, unknown="x")["title"] == "retries"
    assert store.notes("r", ids=[]) == [] and store.note_links([]) == [] and store.note_anchors([]) == []
    assert store.notes_at("r", []) == [] and store.search_notes("r", "") == []
    assert store.note_projects() == ["r"]


def test_two_deliverers_at_once_never_hand_over_the_same_ending(store):
    """The Stop hook and a mod's poll can run at the same moment at a turn's end."""
    import threading

    for i in range(5):
        t = store.create_task(f"t{i}", status="running", source="locks", session_id="s")
        store.add_event(t["id"], "finished", status="done")
        store.update_task(t["id"], status="done")
    got: list[int] = []
    lock = threading.Lock()

    def deliver(via):
        mine = Store(store.path)
        try:
            ids = [t["id"] for t in mine.claim_unreported("s", via=via)]
        finally:
            mine.close()
        with lock:
            got.extend(ids)

    threads = [threading.Thread(target=deliver, args=(f"v{i}",)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(got) == [1, 2, 3, 4, 5]  # each once, whoever won
    assert store.claim_unreported("s", via="late") == []


def test_a_lane_paused_by_the_dispatcher_is_released_by_its_task(store):
    """The dispatcher writes `task #N: …` when a process dies; a resume of N that passed
    left that pause in place, and the queue behind it never started."""
    store.pause_lane("r", "task #24: its process exited unexpectedly")
    assert not store.release_lane("r", 2) and not store.release_lane("r", 240)
    assert store.release_lane("r", 24)
    store.pause_lane("r", "task #24 ended blocked: a refused command")
    assert store.release_lane("r", 24)


def test_two_requests_whose_clipped_titles_match_are_two_problems(store):
    title = "fix the radio dri… that drops the last frame of a burst"
    one = store.open_problem(title, repo="r", symptom="fix the radio driver 1 that drops the last frame of a burst")
    two = store.open_problem(title, repo="r", symptom="fix the radio driver 3 that drops the last frame of a burst")
    assert one != two
    assert store.open_problem(title, repo="r", symptom="fix the radio driver 1 that drops the last frame of a burst") \
        == one
    assert store.open_problem(title, repo="r") == two  # no text to tell them apart: the latest


def test_an_ending_touches_the_pulse_the_mod_looks_at(store: Store):
    t = store.create_task("t", status="running", source="locks", kind="ui", repo="r")
    assert not store.pulse_path.exists()
    store.add_event(t["id"], "attempt_started")
    assert not store.pulse_path.exists()  # only an ending wakes a session
    store.add_event(t["id"], "finished", status="done")
    first = store.pulse_path.stat().st_mtime_ns
    assert store.pulse_path.parent == store.path.parent
    os.utime(store.pulse_path, ns=(first - 10**9, first - 10**9))
    store.add_event(t["id"], "finished", status="done")
    assert store.pulse_path.stat().st_mtime_ns > first - 10**9


def test_events_by_kind_use_their_index(store):
    plan = " ".join(str(r[3]) for r in store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM events WHERE kind = 'finished' ORDER BY id"))
    assert "events_kind" in plan


def test_releasing_a_lane_inside_a_transaction_does_not_end_it(store):
    store.pause_lane("r", "task #24: its process exited unexpectedly")
    with store.immediate():
        assert store.release_lane("r", 24)
        store.pause_lane("r", "task #5: x")  # still inside the transaction
    assert store._conn.in_transaction is False
