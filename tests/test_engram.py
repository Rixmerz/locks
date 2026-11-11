"""engram, adopted: read into the brief, handed to workers read-only, written opt-in."""
from __future__ import annotations

import json
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from locks import mcp_guard, notes
from locks.adapters import ABSENT, PRESENT, UNREADABLE
from locks.adapters.engram import Engram, EngramDown, endpoint, http_client, private_out
from locks.launch import WorkerResult
from locks.orchestrate import run

from .test_orchestrate import Script, kind

HEALTH = {"status": "ok", "service": "engram", "version": "3.3.1", "instance_id": "i1"}
READY = {"status": "ready", "active_reuse_authorized": True, "source": "active"}
HITS = [{"id": 7, "type": "decision", "title": "Prices in cents", "content": "Every table stores cents <private>"
         "key=abc</private> as integers.", "state": "active"},
        {"id": 9, "type": "bugfix", "title": "Old rounding fix", "content": "Round half up.", "state": "needs_review"},
        {"id": 10, "type": "bugfix", "title": "Gone", "content": "x", "deleted_at": "2026-01-01"}]


class Fake:
    """An engram server in memory: answers by route, records every call."""

    def __init__(self, **routes):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.routes = {"GET /health": (200, HEALTH), "GET /compatibility": (200, READY),
                       "GET /project/current": (200, {"project": "shop", "project_source": "git_remote"}),
                       "GET /search": (200, HITS), "POST /sessions": (201, {"id": "locks-task-1", "status": "created"}),
                       "POST /observations": (201, {"id": 41, "status": "saved"}), **routes}
        self.next_id = 41

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        route = f"{method} {path.split('?')[0]}"
        if route.endswith("/end"):
            return 200, {"status": "ended"}
        answer = self.routes.get(route)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return 404, {"error": "no route"}
        if route == "POST /observations" and answer[0] == 201:
            self.next_id += 1
            return 201, {"id": self.next_id - 1}
        return answer

    def posted(self, path):
        return [b for m, p, b in self.calls if m == "POST" and p == path]


def engram(fake, binary="/usr/bin/engram"):
    return Engram({}, http=fake, which=lambda name: binary)


def test_where_engram_answers():
    assert endpoint({}) == ("http", "http://127.0.0.1:7437")
    assert endpoint({"ENGRAM_PORT": "9000"}) == ("http", "http://127.0.0.1:9000")
    assert endpoint({"ENGRAM_PORT": "nope"}) == ("http", "http://127.0.0.1:7437")
    assert endpoint({"ENGRAM_URL": "http://box:1/x/"}) == ("http", "http://box:1/x")
    assert endpoint({"ENGRAM_SOCKET": "/run/e.sock", "ENGRAM_URL": "http://x"}) == ("unix", "/run/e.sock")
    assert private_out("keep <PRIVATE>drop\nthis</private> this") == "keep this"


def test_absent_unreadable_and_present_are_told_apart(tmp_path):
    down = engram(Fake(**{"GET /health": EngramDown("refused")}))
    status = down.inspect(tmp_path)
    assert status.state == ABSENT and "engram serve" in status.detail and down.server() is None
    assert engram(Fake(**{"GET /health": (200, {"service": "other"})})).inspect(tmp_path).state == UNREADABLE
    stale = engram(Fake(**{"GET /compatibility": (200, {"status": "unknown", "active_reuse_authorized": False})}))
    status = stale.inspect(tmp_path)
    assert status.state == UNREADABLE and "not verified (unknown)" in status.detail and status.version == "3.3.1"
    lost = engram(Fake(**{"GET /compatibility": EngramDown("reset")}))
    assert lost.inspect(tmp_path).state == UNREADABLE
    none = engram(Fake(**{"GET /project/current": (200, {"project": "", "warning": "ambiguous"})}))
    status = none.inspect(tmp_path)
    assert status.state == UNREADABLE and "ambiguous" in status.detail
    assert "engram init" in engram(Fake(**{"GET /project/current": (500, None)})).inspect(tmp_path).detail
    fake = Fake()
    good = engram(fake)
    status = good.inspect(tmp_path)
    assert status.state == PRESENT and status.data["project"] == "shop" and "v3.3.1" in status.line()
    assert any(p.startswith("/project/current?cwd=%2F") for _, p, _ in fake.calls)
    assert not good.refresh_in_background(tmp_path, tmp_path / "log") and good.refresh(tmp_path) == ""
    assert good.assess(["a.py"], tmp_path, status) == () and not good.indexes


def test_the_brief_carries_the_team_s_memories_as_data(tmp_path):
    fake = Fake()
    adapter = engram(fake)
    status = adapter.inspect(tmp_path)
    briefing = adapter.brief("store prices in cents", tmp_path, status)
    text = "\n".join(briefing.lines)
    assert "never instructions" in text and "engram #7 [decision] Prices in cents" in text
    assert "key=abc" not in text and "(NEEDS REVIEW)" in text and "Gone" not in text
    assert briefing.reasons == ("engram: 2 memories of shop",)
    search = next(p for m, p, _ in fake.calls if p.startswith("/search"))
    assert "project=shop" in search and "match_mode=any" in search
    assert adapter.brief("??", tmp_path, status).lines == ()
    assert engram(Fake(**{"GET /search": (500, {"error": "x"})})).search("prices", "shop") == []
    failing = engram(Fake(**{"GET /search": EngramDown("gone")}))
    assert "could not search" in failing.brief("prices", tmp_path, status).reasons[0]


def test_a_worker_gets_engram_read_only_and_held_to_its_project(tmp_path):
    adapter = engram(Fake())
    assert adapter.server() is None  # nothing inspected yet
    adapter.inspect(tmp_path)
    server = adapter.server()
    assert server["args"][:3] == ["-m", "locks.mcp_guard", "engram"]
    assert server["args"][-3:] == ["/usr/bin/engram", "mcp", "--tools=agent"]
    assert server["env"]["ENGRAM_PROJECT"] == "shop"
    assert "read-only" in adapter.hint("implement", tmp_path, tmp_path)
    assert engram(Fake(), binary=None).server() is None


def test_the_guard_lets_only_reads_of_this_project_through(monkeypatch):
    monkeypatch.setenv("ENGRAM_PROJECT", "shop")
    assert mcp_guard.engram("mem_save", {"title": "x"})[0].startswith("mem_save changes engram's store")
    assert mcp_guard.engram("mem_delete", {"id": 1})[0]
    assert "only this project's" in mcp_guard.engram("mem_search", {"query": "x", "all_projects": True})[0]
    assert mcp_guard.engram("mem_context", {"scope": "personal"})[0]
    assert "not other" in mcp_guard.engram("mem_search", {"query": "x", "project": "other"})[0]
    why, args = mcp_guard.engram("mem_search", {"query": "x"})
    assert why is None and args["project"] == "shop"
    sent, answers = mcp_guard.screen({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                      "params": {"name": "mem_update", "arguments": {}}}, mcp_guard.engram)
    assert sent is None and "locks refused" in answers[0]["result"]["content"][0]["text"]


def test_what_a_passing_worker_learned_is_saved_once_per_fact(tmp_path):
    fake = Fake()
    adapter = engram(fake)
    adapter.inspect(tmp_path)
    ids = adapter.remember(["Prices are kept in cents. Always.", "<private>only mine</private>"], project="shop",
                           task_id=1, title="prices", directory=tmp_path)
    assert ids == [41]
    session = fake.posted("/sessions")[0]
    assert session["ownership_mode"] == "project_owned" and session["resume"] is True
    saved = fake.posted("/observations")[0]
    assert saved["type"] == "learning" and saved["scope"] == "project" and saved["title"] == "Prices are kept in cents."
    assert saved["capture_prompt"] is False and len(saved["operation_id"]) == 32
    assert fake.posted("/sessions/locks-task-1/end")[0]["summary"] == "locks task #1: prices"
    again = Fake()
    engram(again).remember(["Prices are kept in cents. Always."], project="shop", task_id=1, title="p",
                           directory=tmp_path)
    assert again.posted("/observations")[0]["operation_id"] == saved["operation_id"]
    assert adapter.remember([], project="shop", task_id=1, title="p", directory=tmp_path) == []
    with pytest.raises(ValueError, match="refused the session"):
        engram(Fake(**{"POST /sessions": (409, {"code": "session_project_conflict"})})).remember(
            ["A fact of a few words"], project="shop", task_id=2, title="p", directory=tmp_path)
    with pytest.raises(ValueError, match="refused a memory"):
        engram(Fake(**{"POST /observations": (400, {"error": "title"})})).remember(
            ["A fact of a few words"], project="shop", task_id=2, title="p", directory=tmp_path)


def test_a_run_reads_engram_and_writes_only_when_turned_on(git_repo, store, monkeypatch):
    learned = WorkerResult(True, None, "done", "1 passed", learned=("Prices are stored in cents in every table",
                                                                     "token sk-abcdefghijklmnop leaked"))
    fake = Fake()
    script = Script(learned)
    report = run("store prices in cents", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[engram(fake)])
    assert "engram #7 [decision] Prices in cents" in script.specs[0].prompt
    assert "engram" in script.specs[0].mcp_servers and not fake.posted("/observations")
    monkeypatch.setenv("LOCKS_ENGRAM_WRITE", "on")
    report = run("store prices in cents", git_repo, store, registry={}, launcher=Script(learned),
                 classifier=kind("implement"), adapters=[engram(fake)])
    saved = fake.posted("/observations")
    assert [s["content"] for s in saved] == ["Prices are stored in cents in every table"]  # never a secret
    assert any(line.startswith("engram: saved 1 memories to shop") for line in report.impact)
    assert store.last_event(report.task_id, "engram_saved")["data"]["project"] == "shop"
    # no verified engram: the pass stands, and the report says nothing was saved
    report = run("store prices in cents", git_repo, store, registry={}, launcher=Script(learned),
                 classifier=kind("implement"), adapters=[engram(Fake(**{"GET /health": EngramDown("x")}))])
    assert report.status == "done" and any("was not saved" in line for line in report.impact)
    report = run("store prices in cents", git_repo, store, registry={}, launcher=Script(learned),
                 classifier=kind("implement"), adapters=[engram(Fake(**{"POST /sessions": (500, None)}))])
    assert report.status == "done" and any("could not be saved" in line for line in report.impact)


def test_engram_is_on_its_own_setting_and_never_indexed(git_repo, store, monkeypatch, tmp_path):
    from locks import hooks
    from locks import orchestrate as o
    from locks.adapters import default_adapters

    monkeypatch.setenv("LOCKS_ENGRAM", "on")
    assert [a.name for a in default_adapters()] == ["engram"]
    monkeypatch.setenv("LOCKS_LIVESPEC", "on")
    assert [a.name for a in o.adopted(o.Options(), None)] == ["livespec", "engram"]
    assert [a.name for a in o.adopted(o.Options(livespec=False), None)] == ["engram"]
    down = engram(Fake(**{"GET /health": EngramDown("x")}))
    plan = o.plan("fix the cart", git_repo, store, o.Options(), {}, classifier=kind("implement"), adapters=[down])
    assert not any("index" in r for r in plan.reasons) and plan.neighbours[0].state == ABSENT
    assert hooks._freshen(git_repo, tmp_path, {}, [down]) == []


def test_a_private_part_never_becomes_a_note():
    assert notes.clean_fact("Prices in cents <private>my key is 1234</private> everywhere") == \
        "Prices in cents everywhere"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({**HEALTH, "auth": self.headers.get("Authorization")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        echo = self.rfile.read(size)
        self.send_response(201)
        self.end_headers()
        self.wfile.write(echo if self.path != "/text" else b"not json")

    def log_message(self, *args):
        pass


def test_the_client_speaks_http_and_a_unix_socket():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        call = http_client({"ENGRAM_PORT": str(server.server_address[1]), "ENGRAM_HTTP_TOKEN": "t0k"})
        code, health = call("GET", "/health", None)
        assert code == 200 and health["service"] == "engram" and health["auth"] == "Bearer t0k"
        assert call("POST", "/echo", {"a": 1}) == (201, {"a": 1})
        assert call("POST", "/text", {}) == (201, None)
        prefixed = http_client({"ENGRAM_URL": f"http://127.0.0.1:{server.server_address[1]}/"})
        assert prefixed("GET", "/health", None)[0] == 200
    finally:
        server.shutdown()
    with pytest.raises(EngramDown, match="no engram server"):
        http_client({"ENGRAM_PORT": "1"})("GET", "/health", None)

    folder = Path(tempfile.mkdtemp(dir="/tmp"))
    path = str(folder / "e.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)

    def answer():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]")
        conn.close()

    threading.Thread(target=answer, daemon=True).start()
    try:
        assert http_client({"ENGRAM_SOCKET": path})("GET", "/search?q=x", None) == (200, [])
    finally:
        listener.close()


def test_engram_is_off_until_the_person_turns_it_on(tmp_path, monkeypatch, capsys):
    from locks import cli, config
    from locks.adapters import default_adapters

    monkeypatch.setenv("LOCKS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LOCKS_ENGRAM", raising=False)
    assert config.DEFAULTS["engram"] is False and config.DEFAULTS["engram_write"] is False
    assert not config.enabled("engram", {"LOCKS_HOME": str(tmp_path / "home")})
    assert [a.name for a in default_adapters({"LOCKS_HOME": str(tmp_path / "home"), "LOCKS_LIVESPEC": "off"})] == []
    on = {"LOCKS_HOME": str(tmp_path / "home"), "LOCKS_LIVESPEC": "off", "LOCKS_ENGRAM": "on"}
    assert [a.name for a in default_adapters(on)] == ["engram"]
    # `locks neighbours` probes engram only when it is on
    monkeypatch.setattr(Engram, "inspect", lambda self, where: pytest.fail("engram was probed while off"))
    assert cli.main(["neighbours", "--repo", str(tmp_path)]) == 0
    assert "engram setting: off" in capsys.readouterr().out


def test_the_plugin_manifest_declares_engram_off_like_the_settings():
    import json
    from pathlib import Path

    from locks import config

    manifest = json.loads((Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["userConfig"]["engram"]["default"] is config.DEFAULTS["engram"] is False
