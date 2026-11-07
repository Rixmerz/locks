from __future__ import annotations

import http.client
import http.server
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from locks import stops, usage
from locks.store import Store
from locks.ui import api, server

# --- the data each screen shows -------------------------------------------------------

def test_the_board_sorts_work_by_what_it_needs(store: Store):
    failed = store.create_task("broke", status="failed", source="locks", repo="r", kind="implement")
    running = store.create_task("going", status="running", source="locks", repo="r", current_cell="sonnet/high")
    store.add_event(running["id"], "attempt_started", seq=2, cell="sonnet/high")
    done = store.create_task("shipped", status="done", source="locks", repo="r")
    store.add_event(done["id"], "finished", status="done", branch="locks/task-3")
    store.create_task("asked in a session", status="failed", source="hook", session_id="s")
    store.create_task("child", status="running", source="delegation", session_id="s")
    store.enqueue("later", repo="r", cwd="/x")
    store.pause_lane("other", "task #9 ended failed")
    b = api.board(store)
    assert [t["id"] for t in b["needs_you"]] == [done["id"], failed["id"]]
    assert "review branch locks/task-3" in b["needs_you"][0]["asks"]
    assert b["needs_you"][1]["asks"].startswith("every cell")
    assert b["needs_you"][1]["stop"]["recovered"] and b["needs_you"][1]["stop"]["cause"] == "exhausted"
    assert b["running"][0]["attempt"] == 2 and b["running"][0]["current_cell"] == "sonnet/high"
    assert {lane["repo"]: lane["paused"] for lane in b["queued"]} == {"other": True, "r": False}
    # The branch to merge is the person's; a task every cell failed, the orchestrator's to decide.
    assert [t["waits_on"] for t in b["needs_you"]] == ["person", "orchestrator"]
    assert b["counts"] == {"needs_you": 1, "orchestrator": 1, "running": 1, "workers": 0, "queued": 1, "done": 1,
                           "answering": 0}
    assert b["last_event"] == store.last_event_id()
    # a status line counts the waits its own session sent apart from the project's
    store.update_task(done["id"], session_id="mine")
    assert api.board(store, session="mine")["counts"]["mine"] == 1
    assert api.board(store, session="nobody")["counts"]["mine"] == 0


def test_prompts_answered_in_sessions_are_not_cards(store: Store):
    """Every prompt is recorded as a task, but the board is for work locks runs: a
    long session must not bury the done column in turns."""
    for i in range(80):
        store.create_task(f"prompt {i}", status="done", source="hook", session_id="s", repo="r")
    shipped = store.create_task("shipped", status="done", source="locks", repo="r")
    store.create_task("still typing", status="running", source="hook", session_id="s", repo="r")
    store.create_task("elsewhere", status="running", source="hook", session_id="t", repo="other")
    b = api.board(store)
    assert [t["id"] for t in b["done"]] == [shipped["id"]] and b["running"] == []
    assert [t["title"] for t in b["answering"]] == ["elsewhere", "still typing"]
    assert b["counts"]["done"] == 1 and b["counts"]["answering"] == 2
    assert [t["title"] for t in api.board(store, {"r"})["answering"]] == ["still typing"]


def test_the_board_shows_each_task_s_way_and_the_worker_out_now(store: Store):
    """The flow: kind, ladder, start and every attempt with its move. The worker: the claude -p locks
    launched for the attempt in flight, gone from the board once that attempt is recorded."""
    import os

    t = store.create_task("going", status="running", source="locks", repo="r", pid=os.getpid())
    store.add_event(t["id"], "planned", kind="implement", start="sonnet/low",
                    ladder=["sonnet/low", "sonnet/medium", "opus/medium"], reasons=["history raised it"])
    store.add_attempt(t["id"], cell="sonnet/low", max_turns=30, passed=0, failure="code_bug", cost_usd=0.1)
    store.set_move(t["id"], 1, "more_effort", "the work was shallow")
    store.add_event(t["id"], "attempt_started", seq=2, cell="sonnet/medium", max_turns=30, budget_usd=1.9,
                    capabilities=["livespec"])
    store.create_task("in a session", status="running", source="hook", session_id="s")
    b = api.board(store)
    going = next(r for r in b["running"] if r["id"] == t["id"])
    assert going["flow"]["ladder"] == ["sonnet/low", "sonnet/medium", "opus/medium"]
    assert going["flow"]["steps"][0]["move"] == "more_effort" and going["flow"]["steps"][0]["failure"] == "code_bug"
    assert going["worker"]["seq"] == 2 and going["worker"]["cell"] == "sonnet/medium"
    assert going["worker"]["capabilities"] == ["livespec"] and going["worker"]["alive"] is True
    assert b["answering"][0]["title"] == "in a session" and all(r["source"] != "hook" for r in b["running"])
    assert b["counts"]["workers"] == 1
    assert going["body"] == "going" and "session_id" in going
    long = store.create_task("x" * 2000, status="queued", source="queue", repo="r", cwd="/x")
    lane = next(q for q in api.board(store)["queued"] if q["repo"] == "r")
    assert len(next(x for x in lane["tasks"] if x["id"] == long["id"])["body"]) == api.BODY_CHARS
    assert store.sessions(repos=[]) == [] and store.recent_problems(repos=[]) == []

    store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=0, failure="approach")
    assert next(r for r in api.board(store)["running"] if r["id"] == t["id"])["worker"] is None  # between attempts
    assert api.flow_of(store, 999) is None


def test_task_detail_spend_routing_memory_habits(store: Store):
    t = store.create_task("fix it", status="done", source="locks", repo="r", kind="docs", start_cell="haiku",
                          final_cell="sonnet/low", cost_usd=0.3)
    store.add_attempt(t["id"], cell="haiku", max_turns=30, passed=0, failure="code_bug", changed_paths=["a.py"],
                      capabilities=["livespec"], cost_usd=0.1)
    store.add_attempt(t["id"], cell="sonnet/low", max_turns=30, passed=1, cost_usd=0.2)
    store.add_event(t["id"], "planned", ladder=["haiku"], start="haiku", reasons=[])
    store.add_event(t["id"], "finished", status="done", branch="b", impact=["livespec: x"])
    store.add_event(t["id"], "attempt_finished", seq=1, denied=["Bash(make)"])
    d = api.task_detail(store, t["id"])
    assert d["attempts"][0]["changed_paths"] == ["a.py"] and d["branch"] == "b" and d["impact"] == ["livespec: x"]
    assert d["attempts"][0]["denied"] == ["Bash(make)"] and d["attempts"][1]["denied"] == [] and d["stop"] is None
    assert d["plan"]["start"] == "haiku" and api.task_detail(store, 999) is None

    s = api.spend(store, days=7)
    assert s["workers"][0]["cell"] in ("haiku", "sonnet/low") and s["by_day"][0]["workers"] == pytest.approx(0.3)

    docs = next(r for r in api.routing(store) if r["kind"] == "docs")
    assert docs["tasks"] == 1 and docs["climbed"] == 1 and docs["passes"] == {"sonnet/low": 1}
    assert next(r for r in api.routing(store) if r["kind"] == "plan")["tasks"] == 0

    p = store.open_problem("slow import", repo="r")
    store.add_fix(p, "lazy load", "failed", repo="r")
    assert api.memory(store)[0]["title"] == "slow import"
    # a dead end recorded after the task ran was never shown to it
    assert api.task_detail(store, t["id"])["dead_ends"] == []
    store.add_event(t["id"], "dead_ends", shown=[{"problem": "fix it", "tried": "x"}])
    assert api.task_detail(store, t["id"])["dead_ends"][0]["tried"] == "x"
    assert api.memory(store, "slow import")[0]["id"] == p

    for s_ in range(3):
        for sig in ("edit:.py", "bash:pytest"):
            store.add_tool_event(session_id=f"s{s_}", tool="x", sig=sig, arg_hash="h", ok=1)
    view = api.habit_view(store)
    assert view["candidates"][0]["steps"] == ["edit:.py", "bash:pytest"] and view["installed"] == []


def test_a_session_s_own_task_list_its_work_and_its_turns(store: Store, tmp_path, monkeypatch):
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    plan = config / "tasks" / "s1"
    plan.mkdir(parents=True)
    for i, (subject, status) in enumerate((("write the plan", "completed"), ("build it", "in_progress"),
                                           ("ship it", "pending")), start=1):
        (plan / f"{i}.json").write_text(json.dumps({"id": str(i), "subject": subject, "status": status,
                                                    "activeForm": "building", "description": "d"}))
    (plan / "10.json").write_text(json.dumps({"id": "10", "subject": "later", "status": "pending"}))
    (plan / ".lock").write_text("")
    work = tmp_path / "work"
    (work / ".locks").mkdir(parents=True)
    store.touch_session("s1", str(work), "r")
    store.create_task("make the importer faster", status="done", source="hook", session_id="s1", repo="r")
    sent = store.enqueue("rewrite the importer", repo="r", cwd="/x", session_id="s1")
    store.set_parallel(sent["id"], False, "touches the importer")

    got = api.claude_tasks("s1")
    assert got["state"] == "read" and [t["id"] for t in got["tasks"]] == ["1", "2", "3", "10"]
    listed = api.session_list(store, {"r"})
    assert listed[0]["plan"] == {"done": 1, "total": 4}
    detail = api.session_detail(store, "s1")
    assert [t["title"] for t in detail["tasks"]] == ["rewrite the importer"]
    assert detail["tasks"][0]["parallel_reason"] == "touches the importer"
    assert [t["title"] for t in detail["turns"]] == ["make the importer faster"]
    assert api.session_detail(store, "nobody") is None

    # Older Claude Code: one todo file per session.
    todos = config / "todos"
    todos.mkdir()
    (todos / "s2-agent-s2.json").write_text(json.dumps([{"content": "a", "status": "completed"},
                                                        {"content": "b", "status": "pending"}]))
    assert [t["subject"] for t in api.claude_tasks("s2")["tasks"]] == ["a", "b"]
    assert api.claude_tasks("none") == {"state": "absent", "tasks": []}
    assert api.claude_tasks("../etc") == {"state": "absent", "tasks": []}  # never a path out of the folder
    (plan / "4.json").write_text("{broken")
    assert api.claude_tasks("s1")["state"] == "unreadable"
    assert api.session_list(store, {"r"})[0]["plan"] is None


def test_a_lane_says_whether_its_dispatcher_runs(store: Store):
    from locks import dispatch

    store.enqueue("x", repo="r", cwd="/x")
    assert api.board(store, {"r"})["queued"][0]["dispatching"] is False
    with dispatch.hold(store.path.parent, "r"):
        assert api.board(store, {"r"})["queued"][0]["dispatching"] is True


# --- the server and its envelope --------------------------------------------------------

@pytest.fixture
def ui(_isolated_home):
    srv = server.serve(0, _isolated_home)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.stop_event.set()
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, headers=None, host=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode() if not isinstance(body, bytes) else body
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=payload, headers=hdrs)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res, data


def token(srv):
    _, data = call(srv, "GET", "/api/token", headers={"Sec-Fetch-Site": "same-origin"})
    return json.loads(data)["token"]


def test_pages_and_reads(ui):
    res, data = call(ui, "GET", "/")
    assert res.status == 200 and b"/static/main.js" in data
    assert "script-src 'self'" in res.getheader("Content-Security-Policy")
    assert res.getheader("X-Content-Type-Options") == "nosniff"
    res, data = call(ui, "GET", "/static/board.js")
    assert res.status == 200 and res.getheader("Content-Type").startswith("text/javascript")
    for path in ("/api/board?repo=r", "/api/sessions?repo=r", "/api/projects", "/api/spend?days=3",
                 "/api/routing", "/api/memory?q=x", "/api/habits", "/api/events?after=0", "/static/sessions.js",
                 "/api/analysis", "/api/analysis?days=0&repo=r", "/static/analysis.js",
                 "/api/evolution", "/api/evolution?days=0&repo=r", "/static/evolution.js"):
        res, data = call(ui, "GET", path)
        assert res.status == 200, path
        if path.startswith("/api/"):
            json.loads(data)
    # The analysis screen reads both livespec measures the report carries.
    report = json.loads(call(ui, "GET", "/api/analysis")[1])
    screen = call(ui, "GET", "/static/analysis.js")[1].decode()
    for key in ("livespec_use", "livespec_tokens"):
        assert key in report and f"r.{key}" in screen
    # One project at a time: there is no board of every repository.
    for path in ("/api/board", "/api/sessions"):
        res, data = call(ui, "GET", path)
        assert res.status == 400 and b"pick a project" in data
    assert call(ui, "GET", "/api/tasks/99")[0].status == 404
    assert call(ui, "GET", "/api/sessions/nope")[0].status == 404
    assert call(ui, "GET", "/api/sessions/../../etc")[0].status == 404
    assert call(ui, "GET", "/api/nope")[0].status == 404
    assert call(ui, "HEAD", "/")[0].status == 200


@pytest.mark.parametrize("path", ["/static/../store.py", "/static/x.py", "/static/.hidden.js", "/static/missing.js"])
def test_static_files_are_a_closed_list(ui, path):
    assert call(ui, "GET", path)[0].status == 404


def test_a_foreign_host_is_refused(ui):
    # A DNS-rebinding page reaches 127.0.0.1 under its own name.
    assert call(ui, "GET", "/api/board?repo=r", host="evil.example:80")[0].status == 421
    assert call(ui, "GET", "/", host="localhost:1")[0].status == 421
    port = ui.server_address[1]
    assert call(ui, "GET", "/api/board?repo=r", host=f"localhost:{port}")[0].status == 200


def test_the_token_is_only_for_the_page_and_every_write_needs_it(ui):
    assert call(ui, "GET", "/api/token")[0].status == 403
    assert call(ui, "GET", "/api/token", headers={"Sec-Fetch-Site": "cross-site"})[0].status == 403
    key = token(ui)
    assert len(key) == 64
    assert call(ui, "OPTIONS", "/api/lanes/unpause")[0].status == 403
    body = {"repo": "r"}
    assert call(ui, "POST", "/api/lanes/unpause", body)[0].status == 403
    assert call(ui, "POST", "/api/lanes/unpause", body, headers={"X-Locks-Token": "wrong"})[0].status == 403
    assert call(ui, "POST", "/api/lanes/unpause", body, headers={"X-Locks-Token": key})[0].status == 200
    form = call(ui, "POST", "/api/lanes/unpause", b"repo=r",
                headers={"X-Locks-Token": key, "Content-Type": "application/x-www-form-urlencoded"})
    assert form[0].status == 415
    assert call(ui, "POST", "/api/lanes/unpause", b"{bad", headers={"X-Locks-Token": key})[0].status == 400
    assert call(ui, "POST", "/api/lanes/unpause", b"[1]", headers={"X-Locks-Token": key})[0].status == 400
    # Refused on the declared length, before a byte of it is read. Sending the
    # whole body would race the refusal: the server closes while the client writes.
    big = {"X-Locks-Token": key, "Content-Length": str(server.MAX_BODY + 1)}
    assert call(ui, "POST", "/api/lanes/unpause", b"{}", headers=big)[0].status == 413
    # Work is asked for in a session, never typed into the page.
    assert call(ui, "POST", "/api/queue", {"text": "x", "repo_dir": "/tmp"},
                headers={"X-Locks-Token": key})[0].status == 404

    # deleting the token file revokes every open tab
    server.token_path(ui.root).unlink()
    assert call(ui, "POST", "/api/lanes/unpause", body, headers={"X-Locks-Token": key})[0].status == 403


def test_cancel_unpause_and_start(ui, git_repo, monkeypatch):
    monkeypatch.setenv("LOCKS_AUTOWORK", "off")  # what follows reads its absence, then turns it on
    key = {"X-Locks-Token": token(ui)}
    store = Store(ui.root / "locks.db")
    queued = store.enqueue("x", repo="r", cwd=str(git_repo))
    running = store.create_task("y", status="running", source="locks", pid=None)
    store.pause_lane("r", "failed")
    assert call(ui, "POST", f"/api/tasks/{queued['id']}/cancel", {}, headers=key)[0].status == 200
    assert call(ui, "POST", f"/api/tasks/{running['id']}/cancel", {}, headers=key)[0].status == 200
    assert call(ui, "POST", "/api/tasks/999/cancel", {}, headers=key)[0].status == 404
    assert store.get_task(queued["id"])["status"] == "cancelled" and store.cancel_requested(running["id"])
    assert "from the UI" in store.last_event(queued["id"], "finished")["data"]["stop"]["reason"]
    assert store.last_event(running["id"], "cancel_requested")["data"] == {"via": "ui"}
    started = []
    monkeypatch.setattr(server.dispatch, "start", lambda where, root, scope: started.append((where, scope)) or True)
    store.enqueue("z", repo="r", cwd=str(git_repo))
    res, data = call(ui, "POST", "/api/lanes/unpause", {"repo": "r"}, headers=key)
    assert res.status == 200 and json.loads(data)["started"] is False and not started  # autowork is off here
    assert not store.lanes()[0]["paused"]
    monkeypatch.setenv("LOCKS_AUTOWORK", "on")
    assert json.loads(call(ui, "POST", "/api/lanes/unpause", {"repo": "r"}, headers=key)[1])["started"] is True
    assert call(ui, "POST", "/api/work", {"repo": "r"}, headers=key)[0].status == 202
    assert started == [(git_repo, "r"), (git_repo, "r")]
    assert call(ui, "POST", "/api/work", {"repo": "empty"}, headers=key)[0].status == 409
    assert call(ui, "POST", "/api/nope", {}, headers=key)[0].status == 404
    store.close()


def test_the_stream_sends_events_and_is_bounded(ui, monkeypatch):
    store = Store(ui.root / "locks.db")
    t = store.create_task("x", status="running", source="locks")
    store.add_event(t["id"], "planned", kind="docs")
    store.close()
    monkeypatch.setattr(server, "STREAM_SECONDS", 1)
    res, data = call(ui, "GET", "/api/stream?after=0")
    assert res.getheader("Content-Type") == "text/event-stream"
    assert b'"kind": "planned"' in data and b"id: 1" in data
    for _ in range(server.MAX_STREAMS):
        ui.streams.acquire()
    try:
        assert call(ui, "GET", "/api/stream")[0].status == 503
    finally:
        for _ in range(server.MAX_STREAMS):
            ui.streams.release()


def test_a_crash_is_a_bare_500_and_a_logged_trace(ui, monkeypatch):
    def boom(store, repos):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(api, "board", boom)
    res, data = call(ui, "GET", "/api/board?repo=r")
    assert res.status == 500 and b"secret detail" not in data
    assert "secret detail" in (ui.root / "ui-errors.log").read_text()


def test_housekeeping_sweeps_without_a_page(_isolated_home, monkeypatch):
    srv = server.UIServer(0, _isolated_home)
    store = Store(_isolated_home / "locks.db")
    dead = store.create_task("dead", status="running", source="locks", pid=999999)
    monkeypatch.setattr(server, "SWEEP_EVERY_S", 0.05)
    thread = threading.Thread(target=srv.housekeeping, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while store.get_task(dead["id"])["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    srv.stop_event.set()
    thread.join(timeout=5)
    srv.server_close()
    assert store.get_task(dead["id"])["status"] == "interrupted"
    store.close()


def test_the_int_parser():
    assert server._int({"days": ["x"]}, "days", 7, 1, 30) == 7
    assert server._int({"days": ["999"]}, "days", 7, 1, 30) == 30
    assert server._int({}, "days", 7, 1, 30) == 7


# --- a server that outlives an update ------------------------------------------------------


def test_the_server_says_which_locks_it_is(ui):
    from locks import __version__

    res, data = call(ui, "GET", "/api/version")
    assert res.status == 200 and json.loads(data) == {"version": __version__}
    assert server.occupant(ui.server_address[1]) == {"locks": True, "version": __version__}


def test_only_a_newer_install_takes_the_servers_place(monkeypatch, tmp_path):
    launcher = tmp_path / "bin" / "locks"
    monkeypatch.setattr(server.link, "installed", lambda env: [((0, 0, 1), launcher), ((99, 0), launcher)])
    assert server.newer_install({}) == ("99.0", launcher)
    monkeypatch.setattr(server.link, "installed", lambda env: [((0, 0, 1), launcher)])
    assert server.newer_install({}) is None
    monkeypatch.setattr(server.link, "installed", lambda env: [])
    assert server.newer_install({}) is None


def test_housekeeping_hands_the_port_to_a_newer_locks(_isolated_home, monkeypatch, tmp_path):
    srv = server.UIServer(0, _isolated_home)
    port = srv.server_address[1]
    launcher = tmp_path / "locks"
    ran = []
    monkeypatch.setattr(server, "SWEEP_EVERY_S", 0.01)
    monkeypatch.setattr(server, "newer_install", lambda env: ("9.9.9", launcher))
    monkeypatch.setattr(server.os, "execv", lambda path, argv: ran.append((path, argv)))
    srv.housekeeping()
    assert ran == [(str(launcher), [str(launcher), "ui", "--port", str(port)])]
    assert srv.stop_event.is_set() and srv.socket.fileno() == -1
    assert "this" in (srv.root / "ui-errors.log").read_text() and "9.9.9" in (srv.root / "ui-errors.log").read_text()
    srv.server_close()


class _Other(http.server.BaseHTTPRequestHandler):
    server_version = "other"

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


class _OldLocks(_Other):
    server_version = "locks"


@pytest.mark.parametrize(("handler", "expected"), [
    (_Other, {"locks": False, "version": None}),
    (_OldLocks, {"locks": True, "version": None}),
])
def test_who_holds_a_taken_port(handler, expected):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.occupant(srv.server_address[1]) == expected
    finally:
        srv.shutdown()
        srv.server_close()
    assert server.occupant(srv.server_address[1]) is None  # nobody answers now


def test_each_move_says_where_it_went_and_why_and_routing_adds_them_up(store: Store, git_repo):
    from locks.escalate import Failure
    from locks.launch import WorkerResult
    from locks.orchestrate import run

    from .test_orchestrate import Script, bad, kind

    for _ in range(2):
        report = run("fix the parser", git_repo, store, registry={}, classifier=kind("implement"),
                     launcher=Script(bad(Failure.CODE_BUG, "missed it"), WorkerResult(True, None, "ok", "1 passed")))
    d = api.task_detail(store, report.task_id)
    c = d["attempts"][0]["climb"]
    assert (c["from"], c["to"], c["axis"], c["trigger"]) == ("sonnet/medium", "sonnet/high", "effort", "code_bug")
    assert c["because"][0].startswith("attempt 1 at sonnet/medium ended code_bug") and c["turns_to"] == 80
    assert d["attempts"][1]["climb"] is None
    assert api.flow_of(store, report.task_id)["steps"][0]["climb"]["to"] == "sonnet/high"

    # a run from before moves kept their evidence: what its attempts say
    old = store.create_task("old", status="failed", source="locks", kind="implement")
    store.add_event(old["id"], "planned", kind="implement", ladder=["sonnet/medium"], start="sonnet/medium")
    store.add_attempt(old["id"], cell="sonnet/medium", max_turns=30, passed=0, failure="approach")
    store.set_move(old["id"], 1, "next_model", "the approach was wrong")
    store.add_attempt(old["id"], cell="opus/high", max_turns=30, passed=0, failure="code_bug")
    store.set_move(old["id"], 2, "exhausted", "the top of the ladder failed too")
    steps = api.flow_of(store, old["id"])["steps"]
    assert steps[0]["climb"] == {"from": "sonnet/medium", "to": "opus/high", "axis": "model", "trigger": "approach",
                                 "because": ["the approach was wrong"], "skipped": [], "recovered": True}
    assert steps[1]["climb"]["to"] is None and steps[1]["climb"]["axis"] == "stop"

    implement = next(r for r in api.routing(store) if r["kind"] == "implement")
    assert implement["climbs"][0] == {"from": "sonnet/medium", "move": "more_effort", "to": "sonnet/high",
                                      "after": "code_bug", "times": 2, "then_passed": 2}
    stopped = next(c for c in implement["climbs"] if c["move"] == "exhausted")
    assert stopped["to"] is None and stopped["then_passed"] is None


def test_each_message_says_who_wrote_it_and_older_ones_say_it_is_inferred(store: Store):
    sent = store.create_task("reduce the app", status="blocked", source="locks", session_id="s",
                             author="orchestrator")
    typed = store.create_task("write the changelog", status="done", source="locks", author="person")
    old = store.create_task("remove the role", status="blocked", source="locks", session_id="s")  # before authors
    terminal = store.create_task("old, from a terminal", status="done", source="locks")
    prompt = store.create_task("why is it slow?", status="done", source="hook", session_id="s")
    store.add_message(sent["id"], "worker", "task #1: blocked")
    label = lambda t: [m["author"] for m in api.task_detail(store, t["id"])["messages"]]  # noqa: E731
    assert label(sent) == ["main session (orchestrator)", "locks report"]
    assert label(typed) == ["you"]
    assert label(old)[0].startswith("main session (orchestrator, inferred")
    assert label(terminal) == ["you"] and label(prompt) == ["you"]
    assert api.author("system", {}) == "system"


def test_a_project_s_notes_by_topic_with_their_links(store: Store, ui):
    from locks import notes

    rule, _ = notes.add(store, "r", "Orders over 100 USD need approval", topic="business", author="person",
                        filed_by="person")
    code, _ = notes.add(store, "r", "approve_order checks the threshold", topic="code", author="worker",
                        filed_by="haiku", links=[(rule, "explains")])
    store.update_note(code, state="review", state_reason="app.py changed")
    notes.add(store, "r", "A rule Haiku wanted its own topic for", topic="business", author="worker",
              filed_by="haiku", proposed_topic="approvals")
    view = api.notes_view(store, "r")
    assert {t["name"]: (t["live"], t["review"]) for t in view["topics"]}["code"] == (1, 1)
    linked = next(n for n in view["notes"] if n["id"] == code)
    assert linked["links"] == [{"to": rule, "kind": "explains", "out": True, "title": rule_title(store, rule)}]
    assert view["proposals"] == [{"id": 3, "title": "A rule Haiku wanted its own topic for", "topic": "approvals"}]
    assert [n["id"] for n in api.notes_view(store, "r", topic="code")["notes"]] == [code]
    assert [n["id"] for n in api.notes_view(store, "r", state="review")["notes"]] == [code]
    assert api.notes_view(store, "r", query="threshold")["notes"][0]["id"] == code
    assert api.notes_view(store, "other")["notes"] == []

    res, data = call(ui, "GET", "/api/notes?repo=r&topic=business")
    assert res.status == 200 and {n["topic"] for n in json.loads(data)["notes"]} == {"business"}
    assert call(ui, "GET", "/api/notes")[0].status == 400
    assert call(ui, "GET", "/static/notes.js")[0].status == 200
    key = token(ui)
    assert call(ui, "POST", f"/api/notes/{code}/ok", {})[0].status == 403
    res, data = call(ui, "POST", f"/api/notes/{code}/ok", {}, headers={"X-Locks-Token": key})
    assert res.status == 200 and json.loads(data)["state"] == "current"
    res, data = call(ui, "POST", f"/api/notes/{rule}/drop", {}, headers={"X-Locks-Token": key})
    assert json.loads(data)["state"] == "dropped"
    assert call(ui, "POST", "/api/notes/999/ok", {}, headers={"X-Locks-Token": key})[0].status == 404


def rule_title(store, note_id):
    return store.get_note(note_id)["title"]


def test_a_branch_that_landed_waits_on_nobody(store: Store, git_repo):
    """Merged, squash-merged or deleted: no "review branch" card for it."""
    from .conftest import git

    def done(task_id, branch, changed):
        t = store.create_task(f"task {task_id}", status="running", source="locks", cwd=str(git_repo), repo="r")
        store.add_event(t["id"], "finished", status="done", branch=branch, changed=changed)
        store.update_task(t["id"], status="done")
        return t["id"]

    for name, content in (("merged", "a"), ("squashed", "b"), ("pending", "c")):
        git(git_repo, "checkout", "-qb", f"locks/{name}")
        (git_repo / f"{name}.txt").write_text(content)
        git(git_repo, "add", "-A")
        git(git_repo, "commit", "-qm", name)
        git(git_repo, "checkout", "-q", "main")
    git(git_repo, "merge", "-q", "--ff-only", "locks/merged")
    (git_repo / "squashed.txt").write_text("b")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "squash")
    ids = {name: done(i, f"locks/{name}", [f"{name}.txt"]) for i, name in enumerate(("merged", "squashed", "pending"))}
    ids["gone"] = done(9, "locks/gone", ["x.txt"])
    asks = {c["id"]: c["asks"] for c in api.board(store, {"r"})["needs_you"]}
    assert asks == {ids["pending"]: "review branch locks/pending, then merge it"}
    # a squash-merged file changed again on HEAD is asked for again: unknown is never "landed"
    (git_repo / "squashed.txt").write_text("changed later")
    git(git_repo, "commit", "-qam", "later")
    assert ids["squashed"] in {c["id"] for c in api.board(store, {"r"})["needs_you"]}


def test_done_is_ordered_by_when_each_ended_not_by_id(store: Store):
    old = store.create_task("resumed today", status="done", source="locks", repo="r")
    new = store.create_task("ended yesterday", status="done", source="locks", repo="r")
    store.update_task(new["id"], status="done")
    store._conn.execute("UPDATE tasks SET updated_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (new["id"],))
    store._conn.execute("UPDATE tasks SET updated_at = '2026-01-02T00:00:00+00:00' WHERE id = ?", (old["id"],))
    assert [t["id"] for t in api.board(store, {"r"})["done"]] == [old["id"], new["id"]]


def test_dismiss_and_forget_from_the_ui(ui):
    key = {"X-Locks-Token": token(ui)}
    store = Store(ui.root / "locks.db")
    blocked = store.create_task("x", status="blocked", source="locks", repo="r")
    done = store.create_task("y", status="done", source="locks", repo="r")
    problem = store.open_problem("a clash, not the work", repo="r")
    store.add_fix(problem, "reinstalled node_modules", "worked", repo="r")
    assert call(ui, "POST", f"/api/tasks/{blocked['id']}/dismiss", {})[0].status == 403
    assert call(ui, "POST", f"/api/tasks/{blocked['id']}/dismiss", {}, headers=key)[0].status == 200
    assert call(ui, "POST", f"/api/tasks/{done['id']}/dismiss", {}, headers=key)[0].status == 409
    assert call(ui, "POST", "/api/tasks/999/dismiss", {}, headers=key)[0].status == 404
    assert store.get_task(blocked["id"])["status"] == "dismissed"
    assert store.last_event(blocked["id"], "finished")["data"]["stop"]["via"] == "ui"
    assert call(ui, "POST", f"/api/problems/{problem}/forget", {}, headers=key)[0].status == 200
    assert call(ui, "POST", f"/api/problems/{problem}/forget", {}, headers=key)[0].status == 404
    assert store.problem(problem) is None
    assert not store.search("clash node_modules")
    store.close()


def test_routing_keeps_a_period_and_says_why_a_move_has_no_next_attempt(store: Store):
    old = store.create_task("old", status="blocked", source="locks", repo="r", kind="test")
    store.add_attempt(old["id"], cell="sonnet/medium", max_turns=30, passed=0, failure="permission")
    store.set_move(old["id"], 1, "blocked", "refused")
    store._conn.execute("UPDATE tasks SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old["id"],))
    store._conn.execute("UPDATE attempts SET created_at = '2020-01-01T00:00:00+00:00' WHERE task_id = ?", (old["id"],))
    going = store.create_task("going", status="running", source="locks", repo="r", kind="test")
    store.add_attempt(going["id"], cell="sonnet/medium", max_turns=30, passed=0, failure="turns_exhausted")
    store.set_move(going["id"], 1, "more_turns", "raised")
    test = next(r for r in api.routing(store, days=7) if r["kind"] == "test")
    assert test["tasks"] == 1
    assert [(c["move"], c["to"], c["then_passed"]) for c in test["climbs"]] == [
        ("more_turns", "— running, no next attempt", None)]
    everything = next(r for r in api.routing(store) if r["kind"] == "test")
    assert everything["tasks"] == 2


def test_routing_says_a_pass_with_no_worker_ran_instead_of_a_null_cell(store: Store):
    joined = store.create_task("join the parts", status="done", source="locks", repo="r", kind="implement")
    store.update_task(joined["id"], start_cell="sonnet/medium", final_cell=None)
    built = store.create_task("build it", status="done", source="locks", repo="r", kind="implement")
    store.add_attempt(built["id"], cell="sonnet/medium", max_turns=30, passed=1)
    store.update_task(built["id"], start_cell="sonnet/medium", final_cell="sonnet/medium")
    implement = next(r for r in api.routing(store) if r["kind"] == "implement")
    assert None not in implement["passes"]
    assert implement["passes"].get("sonnet/medium") == 1
    assert implement["passes"][api.NO_WORKER] == 1


def test_resume_from_the_ui_runs_the_rules_locks_suggested_never_the_requests(ui, monkeypatch):
    import subprocess

    ran: list[list[str]] = []
    monkeypatch.setattr(server, "resume", lambda args, cwd: ran.append(args) or subprocess.CompletedProcess(
        args, 0, "resuming", ""))
    key = {"X-Locks-Token": token(ui)}
    store = Store(ui.root / "locks.db")
    refused = store.create_task("x", status="blocked", source="locks", repo="r", cwd="/x")
    store.add_event(refused["id"], "finished", status="blocked", stop=stops.Stop(
        "permission", "refused", allow=("Bash(npm:*)",)).data())
    done = store.create_task("y", status="done", source="locks", repo="r", cwd="/x")
    path = f"/api/tasks/{refused['id']}/resume"
    assert call(ui, "POST", path, {})[0].status == 403
    sent = {"keep": True, "allow": ["Bash(rm:*)"]}
    assert call(ui, "POST", path, sent, headers=key)[0].status == 202
    assert ran[-1] == ["resume", str(refused["id"]), "--detach", "--allow", "Bash(npm:*)", "--keep"]
    assert call(ui, "POST", f"/api/tasks/{done['id']}/resume", {}, headers=key)[0].status == 409
    assert call(ui, "POST", "/api/tasks/999/resume", {}, headers=key)[0].status == 404
    monkeypatch.setattr(server, "resume", lambda args, cwd: subprocess.CompletedProcess(args, 1, "", "it waits"))
    response, body = call(ui, "POST", path, {}, headers=key)
    assert response.status == 409
    assert "it waits" in str(body)
    store.close()


@pytest.mark.parametrize(("cause", "extra", "keep", "expected"), [
    ("approval", {}, False, ["--allow-approval"]),
    ("budget", {"budget_usd": 5.0}, False, []),  # an older locks' stop: a plain resume
    ("budget", {"budget_usd": 5.0}, True, None),
    ("turns", {}, False, []),
    ("turns", {}, True, None),
    ("permission", {}, False, None),
])
def test_resume_args_follow_the_stop(cause, extra, keep, expected):
    shown = {"cause": cause, "next": "locks resume 3", "allow": [], **extra}
    args = stops.resume_args(3, shown, keep=keep)
    assert args == (None if expected is None else ["resume", "3", "--detach", *expected])
    assert stops.resume_args(3, {**shown, "next": None}) is None


def test_a_branch_of_tool_artifacts_only_waits_on_nobody(store: Store):
    """Validations committed before artifacts were left out kept branches of a
    browser's logs: nine "review branch" cards with nothing to merge."""
    def done(changed):
        t = store.create_task("validate", status="running", source="locks", repo="r")
        store.add_event(t["id"], "finished", status="done", branch=f"locks/task-{t['id']}", changed=changed)
        store.update_task(t["id"], status="done")
        return t["id"]

    done([".playwright-mcp/console.log", "e2e/__pycache__/x.pyc"])
    work = done([".playwright-mcp/console.log", "src/app.ts"])
    unknown = done([])
    assert {c["id"] for c in api.board(store, {"r"})["needs_you"]} == {work, unknown}


def test_overview_is_every_recent_session_and_how_its_work_stands(store: Store, tmp_path, capsys):
    """One watcher, several projects: per session what waits, runs, is queued and ended."""
    from locks import cli

    for name in ("shop", "api", "old"):
        (tmp_path / name / ".locks").mkdir(parents=True)
        store.touch_session(name, str(tmp_path / name), name)
    store._conn.execute("UPDATE sessions SET last_seen_at = '2020-01-01T00:00:00+00:00' WHERE id = 'old'")
    waits = store.create_task("add the export", status="running", source="locks", repo="shop", session_id="shop")
    stops.record(store, waits["id"], stops.Stop("spec", "the API has no such field", by="worker"))
    runs = store.create_task("write stats", status="running", source="locks", repo="api", session_id="api")
    store.add_event(runs["id"], "planned", kind="implement", start="sonnet/medium", ladder=["sonnet/medium"])
    store.add_event(runs["id"], "attempt_started", seq=1, cell="sonnet/medium", model="sonnet")
    ended = store.create_task("fix the date", status="done", source="locks", repo="api", session_id="api")
    long_ago = store.create_task("rename it", status="done", source="locks", repo="api", session_id="api")
    store._conn.execute("UPDATE tasks SET updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (long_ago["id"],))
    old_open = store.create_task("old work", status="failed", source="locks", repo="old", session_id="old")
    store._conn.execute("UPDATE tasks SET updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old_open["id"],))
    queued = store.enqueue("tidy the lane", repo="api", cwd=str(tmp_path / "api"))
    store._conn.commit()

    seen = api.overview(store, hours=24)
    by = {s["id"]: s for s in seen["sessions"]}
    assert set(by) == {"shop", "api", "old"}  # "old" stays: it still has work waiting on the person
    assert [(t["id"], t["state"], t["cause"]) for t in by["shop"]["tasks"]] == [(waits["id"], "needs_you", "spec")]
    states = {t["id"]: t["state"] for t in by["api"]["tasks"]}
    assert states == {runs["id"]: "running", ended["id"]: "ended"}
    assert by["api"]["counts"] == {"running": 1, "ended": 1}
    assert [(t["id"], t["state"]) for t in seen["other"]] == [(queued["id"], "queued")]
    assert [t["id"] for t in store.unreported("shop")] == [waits["id"]]  # a read: nothing claimed

    assert cli.main(["overview"]) == 0
    out = capsys.readouterr().out
    assert f"#{waits['id']} [replan] (spec) add the export" in out
    assert "waits on the person:" in out
    assert "attempt 1 on sonnet/medium" in out
    assert "Work no listed session sent" in out
    assert cli.main(["overview", "--json", "--hours", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["sessions"]


def test_a_session_s_own_turns_are_priced_apart_from_its_workers(store: Store, tmp_path):
    work = tmp_path / "work"
    (work / ".locks").mkdir(parents=True)
    store.touch_session("s1", str(work), "r")
    asked = store.create_task("build it", status="done", source="hook", session_id="s1", repo="r")
    child = store.create_task("write the server", status="done", source="delegation", session_id="s1",
                              parent_id=asked["id"], repo="r")
    for mid, task, side in (("m1", asked["id"], 0), ("m2", child["id"], 1)):
        store.add_usage(message_id=mid, session_id="s1", task_id=task, repo="r", model="claude-opus-5-5",
                        input_tokens=0, output_tokens=40_000, cache_read=0, cache_write=0, sidechain=side,
                        ts="2099-01-01T00:00:00Z")
    one = round(usage.dollars("claude-opus-5-5", 0, 40_000), 4)
    detail = api.task_detail(store, asked["id"])
    assert detail["task"]["turns_usd"] == 2 * one and detail["task"]["cost_usd"] == 0
    assert detail["children"][0]["turns_usd"] == one
    assert api.session_list(store, {"r"})[0]["turns_usd"] == 2 * one
    assert api.session_detail(store, "s1")["session"]["turns_usd"] == 2 * one


def test_a_card_carries_its_points_and_why(store: Store):
    t = store.create_task("schema work", status="running", source="locks", repo="r", points=5,
                          points_why="4 criteria, touches schema, risk high")
    card = api.board(store, {"r"})["running"][0]
    assert card["id"] == t["id"] and card["points"] == 5 and card["points_why"].startswith("4 criteria")
    assert api.task_detail(store, t["id"])["task"]["points"] == 5


# --- one tab for a session, never one per piece of its work -----------------------------

def test_a_session_has_one_tab_that_opens_its_page():
    assert api.session_tab("abc12345-x") == {"key": "session:abc12345-x", "title": "session abc12345",
                                             "fragment": "session=abc12345-x"}


def test_a_server_gives_a_session_its_tab_once(ui):
    """The token holder (a second `locks ui --tabs` that found this server) asks for the session's
    tab; only the first ask is told it may open it, and nothing opens from the server itself."""
    key = {"X-Locks-Token": token(ui)}
    assert call(ui, "POST", "/api/tab", {"session": "s1"})[0].status == 403
    assert call(ui, "POST", "/api/tab", {"session": "../x"}, headers=key)[0].status == 400
    port = ui.server_address[1]
    assert server.claim_tab_there(port, "s1", root=ui.root) is True
    assert server.claim_tab_there(port, "s1", root=ui.root) is False
    assert server.claim_tab_there(port, "s2", root=ui.root) is True
    assert ui.tabs == {"s1", "s2"}
    assert server.claim_tab_there(1, "s1", root=ui.root) is None  # nothing listens there


def test_a_server_handed_over_keeps_the_claims(ui, monkeypatch):
    ran = []
    monkeypatch.setattr(server.os, "execv", lambda path, argv: ran.append(argv))
    ui.tabs = {"s2", "s1"}
    ui.hand_over("9.9.9", server.Path("/bin/locks"))
    assert ran[0][1:] == ["ui", "--port", str(ui.server_address[1]), "--tabs", "--session", "s1", "--session", "s2"]
    ran.clear()
    ui.tabs = set()
    ui.hand_over("9.9.9", server.Path("/bin/locks"))
    assert ran[0][1:] == ["ui", "--port", str(ui.server_address[1])]


def test_tabs_are_for_the_session_the_command_runs_in(monkeypatch, capsys):
    import argparse

    from locks import cli

    monkeypatch.setenv("LOCKS_SESSION_ID", "here")
    assert cli._tabs_sessions(argparse.Namespace(tabs=False, session=None)) == []
    assert cli._tabs_sessions(argparse.Namespace(tabs=True, session=None)) == ["here"]
    assert cli._tabs_sessions(argparse.Namespace(tabs=True, session=["a"])) == ["a"]
    monkeypatch.delenv("LOCKS_SESSION_ID")
    assert cli._tabs_sessions(argparse.Namespace(tabs=True, session=None)) == []
    assert "--tabs needs a session" in capsys.readouterr().err


def test_the_skill_starts_the_board_with_no_tab():
    """The complaint was a tab per task: the skill's command opens none and never passes `--tabs` itself."""
    skill = (Path(__file__).resolve().parent.parent / "commands" / "ui.md").read_text(encoding="utf-8")
    started = skill.split("```bash\n")[1].split("```")[0]
    assert started.strip() == "locks ui"
    assert "Do not pass it on your own" in skill


# --- a tab's work, live: the worker's last calls, the attempts compared --------------------

def _log_calls(rows):
    import os
    from datetime import UTC, datetime

    from locks import signature

    directory = signature.log_dir(os.environ)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{datetime.now(UTC):%Y-%m-%d}.ndjson").open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_a_running_task_shows_what_its_worker_does_now(store: Store):
    """The attempt in flight, its phase and its last calls, newest last: a signature and no more,
    never an argument or its hash."""
    t = store.create_task("going", status="running", source="locks", repo="r")
    store.add_attempt(t["id"], cell="sonnet/low", max_turns=30, passed=0, failure="code_bug")
    store.add_event(t["id"], "attempt_started", seq=2, cell="sonnet/medium")
    base = {"task_id": t["id"], "attempt": 2, "session_id": "w", "arg_hash": "abc123", "ok": 1}
    _log_calls([{**base, "attempt": 1, "ts": "a", "tool": "read", "sig": "read:py"},
                {**base, "task_id": 999, "ts": "b", "tool": "bash", "sig": "bash:rm"},
                *({**base, "ts": f"2026-10-21T10:00:{i:02d}", "tool": "read", "sig": "read:py"} for i in range(6)),
                {**base, "ts": "2026-10-21T10:01:00", "tool": "edit", "sig": "edit:py"},
                {**base, "ts": "2026-10-21T10:01:01", "tool": "bash", "sig": "bash:pytest", "ok": 0},
                {**base, "ts": "2026-10-21T10:01:02", "tool": "bash", "sig": "bash:git-status"}])
    detail = api.task_detail(store, t["id"])
    calls = detail["recent_calls"]
    assert detail["attempt_now"] == 2 and detail["phase"] == "checking"
    assert len(calls) == 8 and [c["sig"] for c in calls[-3:]] == ["edit:py", "bash:pytest", "bash:git-status"]
    assert all(set(c) == {"ts", "tool", "sig", "ok", "kind"} for c in calls)
    assert calls[-2]["ok"] is False and calls[-2]["kind"] == "checking" and calls[-3]["kind"] == "implementing"
    live = api.task_live(store, t["id"])
    assert live["status"] == "running" and live["recent_calls"] == calls and live["phase"] == "checking"
    assert "abc123" not in json.dumps(live)
    # Before its attempt says it started, the attempt after the last recorded one is in flight.
    other = store.create_task("fresh", status="running", source="locks", repo="r")
    fresh = api.task_live(store, other["id"])
    assert fresh["attempt_now"] == 1 and fresh["recent_calls"] == [] and fresh["phase"] is None
    # Work that is over has no calls going on.
    store.update_task(t["id"], status="done")
    ended = api.task_live(store, t["id"])
    assert ended["status"] == "done" and ended["recent_calls"] == [] and ended["phase"] is None
    assert api.task_detail(store, t["id"])["recent_calls"] == []
    assert api.task_live(store, 999) is None


def test_attempts_carry_what_the_comparison_shows(store: Store):
    """Two attempts side by side: cell, served model, turns, cost, wall time, how each ended, the move
    after it, the files it changed, its summary and its evidence."""
    t = store.create_task("fix", status="done", source="locks", repo="r")
    store.add_attempt(t["id"], cell="sonnet/low", max_turns=30, passed=0, failure="code_bug", turns=12,
                      cost_usd=0.2, duration_s=95.0, changed_paths=["a.py"], summary="tried", evidence="1 failed")
    store.set_move(t["id"], 1, "more_effort", "the work was shallow")
    store.add_attempt(t["id"], cell="sonnet/high", max_turns=30, passed=1, turns=20, cost_usd=0.5,
                      duration_s=300.0, changed_paths=["a.py", "b.py"], summary="fixed")
    first, second = api.task_detail(store, t["id"])["attempts"]
    for key in ("cell", "served_model", "turns", "max_turns", "cost_usd", "duration_s", "passed", "failure", "move",
                "changed_paths", "summary", "evidence", "climb"):
        assert key in first and key in second, key
    assert first["changed_paths"] == ["a.py"] and second["changed_paths"] == ["a.py", "b.py"]
    assert first["duration_s"] == 95.0 and first["move"] == "more_effort" and second["passed"] == 1


def test_the_live_read_is_served_and_the_favicon_may_be_drawn(ui):
    srv_store = Store(ui.root / "locks.db")
    try:
        t = srv_store.create_task("going", status="running", source="locks", repo="r")
    finally:
        srv_store.close()
    res, data = call(ui, "GET", f"/api/tasks/{t['id']}/live")
    assert res.status == 200 and json.loads(data)["status"] == "running"
    assert call(ui, "GET", "/api/tasks/999/live")[0].status == 404
    # A tab's favicon is an SVG data: URL drawn in the page: the policy lets images come from data:.
    res, _ = call(ui, "GET", "/")
    assert "img-src 'self' data:" in res.getheader("Content-Security-Policy")
    assert call(ui, "GET", "/static/notify.js")[0].status == 200


def test_the_page_never_parses_html_nor_sets_inline_styles():
    from pathlib import Path

    static = Path(server.__file__).parent / "static"
    for path in static.glob("*.js"):
        text = path.read_text()
        assert "innerHTML" not in text and "outerHTML" not in text and "insertAdjacentHTML" not in text, path.name
        assert '"style"' not in text and "style:" not in text, path.name


# --- the page's own scripts ------------------------------------------------------------

STATIC = Path(server.__file__).parent / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("script", sorted(STATIC.glob("*.js")), ids=lambda p: p.name)
def test_every_script_of_the_page_parses(script: Path, tmp_path: Path):
    # A syntax error in one module blanks the whole board; node checks it as the browser would.
    module = tmp_path / (script.stem + ".mjs")
    module.write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
    checked = subprocess.run(["node", "--check", str(module)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_task_detail_carries_a_reading_worker_s_whole_answer(store: Store):
    t = store.create_task("where is x?", status="done", source="locks", repo="r", kind="explore")
    assert api.task_detail(store, t["id"])["report"] is None
    store.add_event(t["id"], "report", seq=1, passed=True, report={"unparsed": "# where\n\nx lives in a.py"})
    assert api.task_detail(store, t["id"])["report"] == {"unparsed": "# where\n\nx lives in a.py"}
