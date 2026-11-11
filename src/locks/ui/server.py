"""The UI server: a local page that can launch paid work, so a web page must never reach it.

The envelope, kept from tasky's dashboard because it held:

- it binds 127.0.0.1 only, and refuses any request whose `Host` is not this
  server — a DNS-rebinding page arrives with its own host name;
- mutations need a token from a 0600 file, compared in constant time and
  re-read on every request, so deleting the file revokes every open tab;
- mutations are `POST` with a JSON body; every `OPTIONS` is refused, so no
  cross-origin page can get past a preflight;
- a strict content security policy, a 1 MiB body cap, socket timeouts, and
  bare 500s with the trace in a local log.

A server outlives plugin updates: it keeps the code it started with. So it
checks, with its sweep, whether Claude Code has installed a newer locks, and
when it has, it runs that version's UI in its own place, on its own port.

Two of tasky's weak spots are designed out. The token never travels in a URL:
the page asks `/api/token`, which answers only a same-origin request (and a
cross-origin page could not read the answer anyway). And slow work never runs
in a request thread: a request records intent, a background thread or a
`locks` process does the work. A GET never starts anything.
"""
from __future__ import annotations

import contextlib
import hmac
import http.client
import json
import mimetypes
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from locks import __version__, config, dispatch, flow, legacy, link, stops
from locks.store import Store, home
from locks.ui import api

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 1024 * 1024
SOCKET_TIMEOUT = 10
SWEEP_EVERY_S = 30
#: Pruning reads every checkout locks worked in: seconds, not milliseconds.
PRUNE_EVERY_S = 600
STREAM_SECONDS = 25
MAX_STREAMS = 8
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
_STATIC_NAME = re.compile(r"^[a-z0-9_-]+\.(js|css|html|svg)$")
_TASK_ROUTE = re.compile(r"^/api/tasks/(\d+)$")
_DIFF_ROUTE = re.compile(r"^/api/tasks/(\d+)/diff$")
_LIVE_ROUTE = re.compile(r"^/api/tasks/(\d+)/live$")
_CANCEL_ROUTE = re.compile(r"^/api/tasks/(\d+)/cancel$")
_DISMISS_ROUTE = re.compile(r"^/api/tasks/(\d+)/dismiss$")
_RESUME_ROUTE = re.compile(r"^/api/tasks/(\d+)/resume$")
_FORGET_ROUTE = re.compile(r"^/api/problems/(\d+)/forget$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SESSION_ROUTE = re.compile(r"^/api/sessions/([A-Za-z0-9_-]{1,128})$")
_NOTE_ROUTE = re.compile(r"^/api/notes/(\d+)/(ok|drop)$")


def token_path(root: Path) -> Path:
    return root / "ui-token"


def ensure_token(root: Path) -> str:
    path = token_path(root)
    if path.is_file():
        with contextlib.suppress(OSError):  # a token file made loose (0644) is tightened before it is read
            os.chmod(path, 0o600)
        return path.read_text(encoding="utf-8").strip()
    root.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)
    return value


class UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, root: Path) -> None:
        self.root = root
        #: The sessions that have their one tab (`locks ui --tabs`): a second ask for the same
        #: session is told it has one, so the browser never gets another for it.
        self.tabs: set[str] = set()
        self._tabs_lock = threading.Lock()
        self.streams = threading.BoundedSemaphore(MAX_STREAMS)
        self.local = threading.local()
        self.stop_event = threading.Event()
        #: When a viewer was last heard (a request began or a stream ended), and the streams open
        #: now: the board is idle when none is open and none was heard for `idle_exit_s`.
        self.last_seen: float | None = None
        self.viewers = 0
        self._seen_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)
        self.allowed_hosts = {f"127.0.0.1:{self.server_address[1]}", f"localhost:{self.server_address[1]}"}
        ensure_token(root)

    def seen(self, opened: int = 0) -> None:
        """A viewer was heard: a request began (`opened` 0), a stream opened (1) or ended (-1)."""
        with self._seen_lock:
            self.viewers += opened
            self.last_seen = time.monotonic()

    def idle_for(self) -> float | None:
        """How long no viewer has been here: None while a stream is open, or before any viewer came
        (a browser that never opened the page leaves the board up)."""
        with self._seen_lock:
            if self.viewers > 0 or self.last_seen is None:
                return None
            return time.monotonic() - self.last_seen

    def exit_when_idle(self, seconds: float, *, every: float = 1.0) -> None:
        """Stop serving once no viewer was here for `seconds` (`locks ui --idle-exit`): a board
        opened for one look does not stay up after its last tab closes."""
        while not self.stop_event.wait(every):
            idle = self.idle_for()
            if idle is not None and idle >= seconds:
                self.stop_event.set()
                self.shutdown()
                return

    def store(self) -> Store:
        """One connection per thread: sqlite3 objects never cross threads."""
        if getattr(self.local, "store", None) is None:
            self.local.store = Store(legacy.database(self.root))
        return self.local.store

    def housekeeping(self) -> None:
        """The sweep, in the server, on a timer — never because a page is open.
        And the check for a newer locks, which takes this server's place."""
        store = Store(legacy.database(self.root))
        # Never pruned yet: the first pass prunes. `time.monotonic()` counts from the machine's boot,
        # so a 0.0 start skipped pruning for the first hour after a boot (a fresh CI runner).
        pruned_at: float | None = None
        try:
            while not self.stop_event.wait(SWEEP_EVERY_S):
                try:
                    flow.sweep(store)
                    if pruned_at is None or time.monotonic() - pruned_at >= PRUNE_EVERY_S:
                        pruned_at = time.monotonic()
                        flow.prune(store)
                    newer = newer_install(os.environ)
                    if newer is not None:
                        self.hand_over(*newer)
                        return
                except Exception:  # logged; the server keeps serving
                    self.log_error_trace("housekeeping")
        finally:
            store.close()

    def claim_tab(self, session_id: str) -> bool:
        """Whether the session has no tab yet: the one asked takes it, so a session gets one
        tab however many times it asks (a later `locks ui --tabs` finds the server and asks)."""
        with self._tabs_lock:
            if session_id in self.tabs:
                return False
            self.tabs.add(session_id)
            return True

    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/"

    def hand_over(self, version: str, launcher: Path) -> None:
        """Become the newer locks' UI, in this process and on this port: an open
        page keeps its address, and reloads when it sees the version change."""
        self.log_note(f"locks {version} is installed; this {__version__} server runs it in its place "
                      f"on port {self.server_address[1]}")
        self.stop_event.set()
        self.socket.close()
        # The newer server holds the same claims, so a session that has its tab never gets a second.
        follow = ["--tabs", *(arg for session in sorted(self.tabs) for arg in ("--session", session))]
        follow = follow if self.tabs else []
        os.execv(str(launcher), [str(launcher), "ui", "--port", str(self.server_address[1]), *follow])  # noqa: S606

    def log_note(self, text: str) -> None:
        try:
            with (self.root / "ui-errors.log").open("a", encoding="utf-8") as fh:
                fh.write(f"--- {datetime.now(UTC).isoformat(timespec='seconds')} {text}\n")
        except OSError:
            pass

    def log_error_trace(self, where: str) -> None:
        try:
            with (self.root / "ui-errors.log").open("a", encoding="utf-8") as fh:
                fh.write(f"--- {datetime.now(UTC).isoformat(timespec='seconds')} {where}\n{traceback.format_exc()}\n")
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    server: UIServer
    server_version = "locks"
    sys_version = ""
    timeout = SOCKET_TIMEOUT

    def log_message(self, format: str, *args: Any) -> None:
        return  # quiet: the page polls

    # --- the envelope -----------------------------------------------------------

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.server.allowed_hosts

    def _token_ok(self) -> bool:
        try:
            expected = token_path(self.server.root).read_text(encoding="utf-8").strip()
        except OSError:
            return False
        given = self.headers.get("X-Locks-Token") or self.headers.get(legacy.UI_HEADER, "")  # an older page
        return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, default=str).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _guard(self) -> bool:
        if not self._host_ok():
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "unknown host")
            return False
        return True

    def _body(self) -> dict[str, Any] | None:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON only")
            return None
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw) if raw is not None else -1
        except ValueError:
            length = -1
        if length < 0:  # missing, not a number or negative: answered before the socket is read
            self._error(HTTPStatus.BAD_REQUEST, "a valid Content-Length is required")
            return None
        if length > MAX_BODY:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid JSON")
            return None
        if not isinstance(data, dict):
            self._error(HTTPStatus.BAD_REQUEST, "expected an object")
            return None
        return data

    # --- verbs -----------------------------------------------------------------------

    def do_OPTIONS(self) -> None:
        self._error(HTTPStatus.FORBIDDEN, "no cross-origin requests")

    def do_GET(self) -> None:
        self._handle(self._get)

    def do_HEAD(self) -> None:
        self._handle(self._get)

    def do_POST(self) -> None:
        self._handle(self._post)

    def _handle(self, method) -> None:
        self.server.seen()
        try:
            if self._guard():
                method()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # a bare 500; the trace goes to the local log
            self.server.log_error_trace(f"{self.command} {self.path}")
            with contextlib.suppress(OSError):
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    # --- reads -----------------------------------------------------------------------

    def _get(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path
        store = self.server.store()
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path.removeprefix("/static/"))
        if path == "/api/version":
            # Which locks answers here: `locks ui` asks when the port is taken, and
            # the page reloads when it changes.
            return self._json(HTTPStatus.OK, {"version": __version__})
        if path == "/api/token":
            # Only the page itself: a browser marks its own fetches same-origin, and
            # a cross-origin page could not read this answer even if it asked.
            origin = self.headers.get("Origin")
            own = {f"http://{host}" for host in self.server.allowed_hosts}
            if (
                self.headers.get("Sec-Fetch-Site") != "same-origin"
                or self.headers.get("Sec-Fetch-Mode") is None
                or (origin is not None and origin not in own)
            ):
                return self._error(HTTPStatus.FORBIDDEN, "same-origin only")
            return self._json(HTTPStatus.OK, {"token": ensure_token(self.server.root)})
        if path == "/api/projects":
            return self._json(HTTPStatus.OK, api.projects(store))
        if path == "/api/board":
            # One project at a time, always: every repository's cards at once is
            # a wall nobody reads.
            repo_key = (query.get("repo") or [""])[0]
            if not repo_key:
                return self._error(HTTPStatus.BAD_REQUEST, "pick a project: /api/board?repo=<key>")
            return self._json(HTTPStatus.OK, api.board(store, {repo_key}))
        if path == "/api/sessions":
            repo_key = (query.get("repo") or [""])[0]
            if not repo_key:
                return self._error(HTTPStatus.BAD_REQUEST, "pick a project: /api/sessions?repo=<key>")
            return self._json(HTTPStatus.OK, api.session_list(store, {repo_key}))
        if match := _SESSION_ROUTE.match(path):
            detail = api.session_detail(store, match.group(1))
            return self._json(HTTPStatus.OK, detail) if detail else self._error(HTTPStatus.NOT_FOUND, "no such session")
        if match := _DIFF_ROUTE.match(path):
            found = api.task_diff(store, int(match.group(1)))
            return self._json(HTTPStatus.OK, found) if found else self._error(HTTPStatus.NOT_FOUND, "no such task")
        if match := _LIVE_ROUTE.match(path):
            # What a running task's worker is doing, polled by its page: tool calls write no events.
            live = api.task_live(store, int(match.group(1)))
            return self._json(HTTPStatus.OK, live) if live else self._error(HTTPStatus.NOT_FOUND, "no such task")
        if match := _TASK_ROUTE.match(path):
            detail = api.task_detail(store, int(match.group(1)))
            return self._json(HTTPStatus.OK, detail) if detail else self._error(HTTPStatus.NOT_FOUND, "no such task")
        if path == "/api/spend":
            return self._json(HTTPStatus.OK, api.spend(store, days=_int(query, "days", 7, 1, 365)))
        if path == "/api/analysis":
            return self._json(HTTPStatus.OK, api.analysis_view(store, days=_int(query, "days", 30, 0, 3650),
                                                               repo_key=(query.get("repo") or [""])[0] or None))
        if path == "/api/evolution":
            return self._json(HTTPStatus.OK, api.evolution_view(store, days=_int(query, "days", 30, 0, 3650),
                                                                repo_key=(query.get("repo") or [""])[0] or None))
        if path == "/api/routing":
            return self._json(HTTPStatus.OK, api.routing(store, days=_int(query, "days", 0, 0, 3650)))
        if path == "/api/notes":
            repo_key = (query.get("repo") or [""])[0]
            if not repo_key:
                return self._error(HTTPStatus.BAD_REQUEST, "pick a project: /api/notes?repo=<key>")
            return self._json(HTTPStatus.OK, api.notes_view(
                store, repo_key, topic=(query.get("topic") or [""])[0], query=(query.get("q") or [""])[0],
                state=(query.get("state") or [""])[0]))
        if path in ("/api/workflows", "/api/map", "/api/milestones"):
            repo_key = (query.get("repo") or [""])[0]
            if not repo_key:
                return self._error(HTTPStatus.BAD_REQUEST, f"pick a project: {path}?repo=<key>")
            view = {"/api/workflows": api.workflows_view, "/api/map": api.map_view,
                    "/api/milestones": api.milestones}[path]
            return self._json(HTTPStatus.OK, view(store, repo_key))
        if path == "/api/memory":
            return self._json(HTTPStatus.OK, api.memory(store, (query.get("q") or [""])[0]))
        if path == "/api/teams":
            return self._json(HTTPStatus.OK, api.teams_view((query.get("repo") or [""])[0] or None))
        if path == "/api/habits":
            return self._json(HTTPStatus.OK, api.habit_view(store, (query.get("repo") or [""])[0] or None))
        if path == "/api/events":
            return self._json(HTTPStatus.OK, store.events(after=_int(query, "after", 0, 0, 10**12), limit=500))
        if path == "/api/stream":
            return self._stream(_int(query, "after", 0, 0, 10**12))
        return self._error(HTTPStatus.NOT_FOUND, "not found")

    def _static(self, name: str) -> None:
        if not _STATIC_NAME.match(name) or not (STATIC / name).is_file():
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind.endswith("javascript"):
            kind += "; charset=utf-8"
        self._send(HTTPStatus.OK, (STATIC / name).read_bytes(), kind)

    def _stream(self, after: int) -> None:
        """Server-sent events for `STREAM_SECONDS`; the browser reconnects with the
        last id it saw. Bounded, so open tabs cannot exhaust the threads."""
        if not self.server.streams.acquire(blocking=False):
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, "too many streams; poll /api/events")
        self.server.seen(1)
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            store = self.server.store()
            deadline = time.monotonic() + STREAM_SECONDS
            while time.monotonic() < deadline and not self.server.stop_event.is_set():
                for event in store.events(after=after, limit=200):
                    after = event["id"]
                    self.wfile.write(f"id: {after}\ndata: {json.dumps(event, default=str)}\n\n".encode())
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                self.server.stop_event.wait(1)
        finally:
            self.server.seen(-1)
            self.server.streams.release()

    # --- writes ----------------------------------------------------------------------

    def _post(self) -> None:
        if not self._token_ok():
            return self._error(HTTPStatus.FORBIDDEN, "missing or wrong token")
        data = self._body()
        if data is None:
            return None
        store = self.server.store()
        path = urlparse(self.path).path
        if match := _CANCEL_ROUTE.match(path):
            task = store.get_task(int(match.group(1)))
            if task is None:
                return self._error(HTTPStatus.NOT_FOUND, "no such task")
            if task["status"] == "queued":
                stops.record(store, task["id"], stops.Stop("cancelled", "you cancelled it from the UI before it ran",
                                                           by="you", extra={"via": "ui"}))
            elif task["status"] == "running":
                store.request_cancel(task["id"], via="ui")
                if task["pid"]:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(int(task["pid"]), signal.SIGTERM)
            return self._json(HTTPStatus.OK, {"id": task["id"], "cancel": True})
        if match := _DISMISS_ROUTE.match(path):
            task = store.get_task(int(match.group(1)))
            if task is None:
                return self._error(HTTPStatus.NOT_FOUND, "no such task")
            why = data.get("why") if isinstance(data.get("why"), str) else ""
            if stops.dismiss(store, task, via="ui", why=why[:500]) is None:
                return self._error(HTTPStatus.CONFLICT, f"task #{task['id']} is {task['status']}: it waits on no one")
            dispatch.reopened(task, os.environ)
            return self._json(HTTPStatus.OK, {"id": task["id"], "dismissed": True})
        if match := _RESUME_ROUTE.match(path):
            task = store.get_task(int(match.group(1)))
            if task is None:
                return self._error(HTTPStatus.NOT_FOUND, "no such task")
            # The rules come from locks' account of the stop, never from the request.
            args = stops.resume_args(task["id"], stops.view(store, task), keep=bool(data.get("keep")))
            if args is None:
                return self._error(HTTPStatus.CONFLICT, f"task #{task['id']} has no resume to press")
            ran = resume(args, task["cwd"])
            if ran.returncode != 0:
                return self._error(HTTPStatus.CONFLICT, (ran.stderr or ran.stdout or "resume failed").strip()[-400:])
            return self._json(HTTPStatus.ACCEPTED, {"id": task["id"], "resumed": True})
        if match := _FORGET_ROUTE.match(path):
            problem = store.forget_problem(int(match.group(1)))
            if problem is None:
                return self._error(HTTPStatus.NOT_FOUND, "no such problem")
            return self._json(HTTPStatus.OK, {"id": problem["id"], "forgotten": True})
        if match := _NOTE_ROUTE.match(path):
            from locks import notes

            note = store.get_note(int(match.group(1)))
            if note is None:
                return self._error(HTTPStatus.NOT_FOUND, "no such note")
            if match.group(2) == "drop":
                store.update_note(note["id"], state="dropped", state_reason="dropped from the UI")
            else:
                place = next((p["dir"] for p in api.projects(store, enrolled_only=False)
                              if p["repo"] == note["project"] and p["exists"]), "")
                notes.confirm(store, note["id"], Path(place) if place else None)
            return self._json(HTTPStatus.OK, {"id": note["id"], "state": store.get_note(note["id"])["state"]})
        if path == "/api/tab":
            # The one tab of a session (`locks ui --tabs` finds this server on the port and asks):
            # `claimed` says whether it may open it, or the session already has it.
            session = data.get("session")
            if not isinstance(session, str) or not _SESSION_ID.match(session):
                return self._error(HTTPStatus.BAD_REQUEST, "session: a session id")
            return self._json(HTTPStatus.OK, {"claimed": self.server.claim_tab(session)})
        if path == "/api/workflows/attach":
            # A person's press: one note of this project into a step's brief. A
            # workflow in use keeps it as a pending revision.
            from locks import workflows

            try:
                saved = api.attach_note(store, str(data.get("repo") or ""), str(data.get("workflow") or ""),
                                        str(data.get("step") or ""), int(data.get("note") or 0),
                                        str(data.get("scope") or "") or None)
            except (workflows.WorkflowError, ValueError, TypeError) as err:
                return self._error(HTTPStatus.CONFLICT, str(err))
            return self._json(HTTPStatus.OK, saved)
        if path == "/api/lanes/unpause":
            repo_key = str(data.get("repo") or "")
            store.unpause_lane(repo_key)
            # Unpausing is the person saying "go on": the lane's dispatcher resumes.
            started = config.enabled("autowork", os.environ) and dispatch.start_lane(store, self.server.root, repo_key)
            return self._json(HTTPStatus.OK, {"unpaused": repo_key, "started": bool(started)})
        if path == "/api/work":
            # Intent only: a detached `locks work` does the work, not this thread.
            if not dispatch.start_lane(store, self.server.root, str(data.get("repo") or "")):
                return self._error(HTTPStatus.CONFLICT, "nothing to start: no directory for that lane, or it runs")
            return self._json(HTTPStatus.ACCEPTED, {"started": True})
        return self._error(HTTPStatus.NOT_FOUND, "not found")


def resume(args: list[str], cwd: str | None) -> subprocess.CompletedProcess:
    """Run `locks resume … --detach`: it returns once the run is started on its own."""
    try:
        return subprocess.run([sys.executable, "-m", "locks", *args], cwd=cwd or None, env=dispatch.child_env(),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as err:
        return subprocess.CompletedProcess(args, 1, "", str(err))


def _int(query: dict[str, list[str]], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int((query.get(key) or [default])[0])
    except ValueError:
        return default
    return max(low, min(high, value))


def newer_install(env: Mapping[str, str]) -> tuple[str, Path] | None:
    """The newest locks Claude Code has installed, when it is newer than this one."""
    found = link.installed(env)
    own = link.parse_version(__version__)
    if not found or own is None or found[-1][0] <= own:
        return None
    version, launcher = found[-1]
    return ".".join(map(str, version)), launcher


def occupant(port: int) -> dict[str, Any] | None:
    """Who answers on a taken port: {"locks": bool, "version": str | None}, or None.
    A locks UI from before 0.4.1 answers as locks, without a version."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", "/api/version", headers={"Host": f"127.0.0.1:{port}"})
        res = conn.getresponse()
        body = res.read()
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    if not (res.getheader("Server") or "").startswith(("locks", legacy.OLD)):  # an older UI of the old name
        return {"locks": False, "version": None}
    try:
        data = json.loads(body) if res.status == HTTPStatus.OK else {}
    except ValueError:
        data = {}
    version = data.get("version") if isinstance(data, dict) else None
    return {"locks": True, "version": version if isinstance(version, str) else None}


def older(found: Mapping[str, Any] | None) -> bool:
    """An occupant that is a locks UI older than this one (one from before 0.4.1 says no version)."""
    if not found or not found.get("locks"):
        return False
    theirs = link.parse_version(str(found.get("version") or "0"))
    return theirs is not None and theirs < (link.parse_version(__version__) or ())


def listener(port: int, run: Callable[..., Any] = subprocess.run) -> int | None:
    """The process listening on a loopback port, when `lsof` can say and it is a locks UI by
    its command line; None otherwise. Never a guess: a process that is not locks' is left alone."""
    try:
        found = run(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True,
                    timeout=5, check=False).stdout.split()
        pid = int(found[0]) if len(found) == 1 else None
        if pid is None or pid == os.getpid():
            return None
        command = run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5,
                      check=False).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return pid if re.search(rf"\b(?:locks|{legacy.OLD})\b", command) and re.search(r"\bui\b", command) else None


def take_over(port: int, *, run: Callable[..., Any] = subprocess.run,
              kill: Callable[[int, int], None] = os.kill, wait_s: float = 5.0) -> int | None:
    """Stop the older locks UI that holds `port`, so this one serves it: the pid stopped once the
    port is free, else None (it could not be found as locks', or did not let go)."""
    pid = listener(port, run)
    if pid is None:
        return None
    try:
        kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return None
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if occupant(port) is None:
            return pid
        time.sleep(0.1)
    return None


def claim_tab_there(port: int, session_id: str, root: Path | None = None) -> bool | None:
    """Ask the locks UI that holds `port` for the session's tab (the token file is the key):
    True when it may be opened, False when the session has it already, None when it could not be asked."""
    token = ensure_token(root or home())
    body = json.dumps({"session": session_id}).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("POST", "/api/tab", body=body, headers={
            "Host": f"127.0.0.1:{port}", "Content-Type": "application/json", "X-Locks-Token": token,
            legacy.UI_HEADER: token})  # an older UI on the port reads only its own name
        res = conn.getresponse()
        if res.status != HTTPStatus.OK:
            return None
        return bool(json.loads(res.read()).get("claimed"))
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        conn.close()


def serve(port: int = 8790, root: Path | None = None, *, ready: threading.Event | None = None) -> UIServer:
    """Build the server and start its housekeeping; the caller runs `serve_forever`."""
    server = UIServer(port, root or home())
    threading.Thread(target=server.housekeeping, daemon=True).start()
    if ready is not None:
        ready.set()
    return server
