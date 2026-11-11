from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest

from locks.ui import server


@pytest.fixture
def ui(_isolated_home):
    srv = server.serve(0, _isolated_home)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.stop_event.set()
    srv.shutdown()
    srv.server_close()


def raw(srv, lines: list[str]) -> tuple[int, dict, float]:
    """Send header lines as they are (no body) and return (status, JSON body, seconds)."""
    port = srv.server_address[1]
    start = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data:
            data += sock.recv(4096)
        head, _, rest = data.partition(b"\r\n\r\n")
        length = next(int(x.split(b":")[1]) for x in head.split(b"\r\n") if x.lower().startswith(b"content-length"))
        while len(rest) < length:
            rest += sock.recv(4096)
    return int(head.split()[1]), json.loads(rest), time.monotonic() - start


def post(srv, *extra: str):
    port = srv.server_address[1]
    return raw(srv, ["POST /api/dismiss HTTP/1.1", f"Host: 127.0.0.1:{port}",
                     "Content-Type: application/json",
                     f"X-Locks-Token: {server.ensure_token(srv.root)}", *extra])


@pytest.mark.parametrize("header", [None, "abc", "-5", str(server.MAX_BODY + 1)])
def test_a_bad_content_length_is_answered_at_once(ui, header):
    extra = [f"Content-Length: {header}"] if header is not None else []
    status, body, took = post(ui, *extra)
    # over the limit keeps its own 413; the rest are 400 with a JSON error
    assert status == (413 if header and header.isdigit() else 400)
    assert "error" in body
    assert took < 2


def test_an_existing_token_file_is_tightened_to_0600(tmp_path):
    path = server.token_path(tmp_path)
    path.write_text("abc\n")
    os.chmod(path, 0o644)
    assert server.ensure_token(tmp_path) == "abc"
    assert path.stat().st_mode & 0o777 == 0o600


def token_get(srv, headers):
    port = srv.server_address[1]
    hdrs = [f"{k}: {v}" for k, v in headers.items()]
    return raw(srv, ["GET /api/token HTTP/1.1", f"Host: 127.0.0.1:{port}", *hdrs])[:2]


def test_the_token_needs_sec_fetch_mode_and_an_own_origin(ui):
    port = ui.server_address[1]
    page = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}
    status, body = token_get(ui, page)
    assert status == 200 and body["token"]
    assert token_get(ui, {"Sec-Fetch-Site": "same-origin"})[0] == 403
    assert token_get(ui, {**page, "Origin": "http://evil.example"})[0] == 403
    assert token_get(ui, {**page, "Origin": f"http://127.0.0.1:{port}"})[0] == 200
    assert token_get(ui, {**page, "Origin": f"http://localhost:{port}"})[0] == 200
