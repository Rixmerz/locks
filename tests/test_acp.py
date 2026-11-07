from __future__ import annotations

import io
import json
import os
import queue
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import locks
from locks import acp

WAIT_S = 5.0
TASK_LINE = "locks: #7 attempt 1 at opus/low"


class FakeProc:
    """A `locks` process: its stderr lines, its stdout, and an exit that waits for `release()` when held."""

    def __init__(self, argv: list[str], kwargs: dict[str, Any], *, stderr: list[str] | None = None,
                 stdout: str = "", code: int = 0, hold: bool = False) -> None:
        self.argv, self.kwargs, self.pid, self.code = argv, kwargs, 4242, code
        self.stdout = io.StringIO(stdout)
        self._stderr = list(stderr or [])
        self._later: queue.Queue[str] = queue.Queue()
        self._released = threading.Event()
        if not hold:
            self._released.set()

    @property
    def stderr(self) -> Iterator[str]:
        yield from (line + "\n" for line in self._stderr)
        # Lines said later, until the pipe closes when the process ends.
        while True:
            try:
                line = self._later.get(timeout=0.01)
            except queue.Empty:
                if self._released.is_set() and self._later.empty():
                    return
                continue
            yield line + "\n"

    @property
    def running(self) -> bool:
        return not self._released.is_set()

    def say(self, line: str) -> None:
        self._later.put(line)

    def release(self) -> None:
        self._released.set()

    def wait(self, timeout: float | None = None) -> int:
        if not self._released.wait(WAIT_S if timeout is None else timeout):
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.code


class Spawner:
    """The injected popen: each prompt is a run; the `locks cancel` the agent asks for on its own (its
    output discarded) is answered at once, and ends the first run unless told not to."""

    def __init__(self, release_on_cancel: bool = True, **run: Any) -> None:
        self.run, self.release_on_cancel = run, release_on_cancel
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.procs: list[FakeProc] = []
        self.killed: list[tuple[int, int]] = []
        self.cancelled = threading.Event()
        self.was_killed = threading.Event()

    def asked_to_cancel(self) -> list[list[str]]:
        return [argv for argv, kwargs in self.calls if argv[3] == "cancel" and kwargs["stdout"] == subprocess.DEVNULL]

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProc:
        self.calls.append((argv, kwargs))
        if argv[3] == "cancel" and kwargs["stdout"] == subprocess.DEVNULL:
            self.cancelled.set()
            if self.release_on_cancel:
                self.procs[0].release()
            return FakeProc(argv, kwargs)
        proc = FakeProc(argv, kwargs, **self.run)
        self.procs.append(proc)
        return proc

    def killpg(self, pid: int, sig: int) -> None:
        self.killed.append((pid, sig))
        self.was_killed.set()
        self.procs[0].release()


class Sink:
    """What the agent wrote to stdout, one JSON message per line."""

    def __init__(self) -> None:
        self._text = ""
        self._changed = threading.Condition()

    def write(self, text: str) -> int:
        with self._changed:
            self._text += text
            self._changed.notify_all()
        return len(text)

    def flush(self) -> None:
        pass

    def messages(self) -> list[dict[str, Any]]:
        with self._changed:
            text = self._text
        assert text == "" or text.endswith("\n")
        return [json.loads(line) for line in text.splitlines()]

    def wait_for(self, match) -> dict[str, Any]:
        found: list[dict[str, Any]] = []

        def seen() -> bool:
            found[:] = [m for m in self.messages() if match(m)]
            return bool(found)

        with self._changed:
            assert self._changed.wait_for(seen, WAIT_S), f"never written: {self.messages()}"
        return found[0]


class Client:
    def __init__(self, spawner: Spawner | None = None, **serve_args: Any) -> None:
        self.spawner = spawner or Spawner()
        self.out, self.err = Sink(), io.StringIO()
        self._lines: queue.Queue[str | None] = queue.Queue()
        serve_args.setdefault("grace", 30.0)
        serve_args.setdefault("diffs", lambda task, live: [])
        serve_args.setdefault("look", lambda task, ended: acp.Look())
        serve_args.setdefault("look_every", 0.01)
        self.thread = threading.Thread(
            target=acp.serve, args=(iter(self._lines.get, None), self.out, self.err),
            kwargs={"popen": self.spawner, "killpg": self.spawner.killpg, **serve_args})
        self.thread.start()
        self._ids = 0

    def request(self, method: str, params: dict[str, Any] | None = None) -> int:
        self._ids += 1
        self.raw(json.dumps({"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params or {}}))
        return self._ids

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.raw(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    def raw(self, line: str) -> None:
        self._lines.put(line)

    def answer(self, request_id: int) -> dict[str, Any]:
        return self.out.wait_for(lambda m: m.get("id") == request_id)

    def new_session(self, cwd: str = "/work/app", mcp: list[Any] | None = None) -> str:
        reply = self.answer(self.request("session/new", {"cwd": cwd, "mcpServers": mcp or []}))
        return reply["result"]["sessionId"]

    def prompt(self, session: str, *text: str) -> int:
        return self.request("session/prompt", {
            "sessionId": session, "prompt": [{"type": "text", "text": t} for t in text]})

    def updates(self, session: str) -> list[dict[str, Any]]:
        return [m["params"]["update"] for m in self.out.messages()
                if m.get("method") == "session/update" and m["params"]["sessionId"] == session]

    def close(self) -> None:
        self._lines.put(None)
        self.thread.join(WAIT_S)
        assert not self.thread.is_alive()


@pytest.fixture
def client() -> Iterator[Client]:
    made: list[Client] = []

    def make(spawner: Spawner | None = None, **serve_args: Any) -> Client:
        made.append(Client(spawner, **serve_args))
        return made[-1]

    yield make
    for c in made:
        for proc in c.spawner.procs:
            proc.release()
        c.close()


def test_initialize_says_what_locks_is_and_can_do(client):
    c = client()
    reply = c.answer(c.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}}))
    assert reply == {"jsonrpc": "2.0", "id": 1, "result": {
        "protocolVersion": 1,
        "agentCapabilities": {"loadSession": False,
                              "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False}},
        "agentInfo": {"name": "locks", "title": "locks", "version": locks.__version__},
        "authMethods": []}}
    assert c.answer(c.request("authenticate", {"methodId": "x"}))["result"] == {}


def test_a_new_session_is_answered_before_its_commands_are_announced(client):
    c = client()
    reply = c.answer(c.request("session/new", {"cwd": "/work/app", "mcpServers": []}))
    session = reply["result"]["sessionId"]
    commands = c.out.wait_for(lambda m: m.get("method") == "session/update")
    assert c.out.messages().index(reply) < c.out.messages().index(commands)
    assert commands["params"] == {"sessionId": session, "update": {
        "sessionUpdate": "available_commands_update",
        "availableCommands": [{"name": "tasks", "description": acp.COMMANDS["tasks"]},
                              {"name": "stats", "description": acp.COMMANDS["stats"]},
                              {"name": "cancel", "description": acp.COMMANDS["cancel"]}]}}
    assert c.new_session() != session


def test_a_session_needs_an_absolute_folder(client):
    c = client()
    for params in ({}, {"cwd": "relative/dir"}, {"cwd": 5}):
        assert c.answer(c.request("session/new", params))["error"]["code"] == -32602


def test_a_prompt_is_one_locks_run_shown_as_a_tool_call_and_answered_with_its_report(client):
    spawner = Spawner(stderr=[TASK_LINE, "locks: #7 passed"], stdout="the report\n")
    c = client(spawner)
    session = c.new_session("/work/app")
    request = c.request("session/prompt", {"sessionId": session, "prompt": [
        {"type": "text", "text": "fix the login"},
        {"type": "resource_link", "uri": "file:///work/app/auth.py", "name": "auth.py"},
        {"type": "image", "data": "x", "mimeType": "image/png"},
        {"type": "text", "text": "and its tests"}]})
    reply = c.answer(request)
    assert reply["result"] == {"stopReason": "end_turn"}
    argv, kwargs = spawner.calls[0]
    assert argv == [sys.executable, "-m", "locks", "run", "fix the login\nfile:///work/app/auth.py\nand its tests"]
    assert kwargs["cwd"] == "/work/app"
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE

    shown = [u for u in c.updates(session)[1:] if u["sessionUpdate"] != "plan"]  # the first announced the commands
    call_id = shown[0]["toolCallId"]
    assert shown == [
        {"sessionUpdate": "tool_call", "toolCallId": call_id, "title": "locks run", "kind": "execute",
         "status": "in_progress"},
        {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "title": TASK_LINE,
         "content": [{"type": "content", "content": {"type": "text", "text": TASK_LINE}}]},
        {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "title": "locks: #7 passed",
         "content": [{"type": "content", "content": {"type": "text", "text": f"{TASK_LINE}\nlocks: #7 passed"}}]},
        {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "completed"},
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "the report\n"}},
    ]
    # The answer to the prompt comes after everything it showed.
    assert c.out.messages()[-1] == reply


def test_the_request_s_items_are_the_plan_and_its_files_are_diffs(client):
    """The editor sees the request's items as the plan (in progress once the task started, completed
    on a pass) and the worker's files as diffs: its worktree while it runs, the kept branch after."""
    asked: list[tuple[int, bool]] = []
    live = {"type": "diff", "path": "/work/app/a.py", "oldText": None, "newText": "x = 1\n"}
    kept = {"type": "diff", "path": "/work/app/a.py", "oldText": None, "newText": "x = 2\n"}

    def diffs(task: int, running: bool) -> list[dict[str, Any]]:
        asked.append((task, running))
        return [live] if running else [kept]

    spawner = Spawner(stderr=[TASK_LINE], stdout="the report\n", hold=True)
    c = client(spawner, diffs=diffs, diff_every=0.01)
    session = c.new_session()
    request = c.prompt(session, "Add two things:\n- a sub function\n- a mul function")
    c.out.wait_for(lambda m: m.get("params", {}).get("update", {}).get("content", [None])[-1] == live)
    spawner.procs[0].release()
    c.answer(request)
    plans = [u["entries"] for u in c.updates(session) if u["sessionUpdate"] == "plan"]
    items = [[e["status"] for e in p[:2]] for p in plans]
    assert items == [["pending"] * 2, ["in_progress"] * 2, ["completed"] * 2]
    assert [e["content"] for e in plans[0]] == ["a sub function", "a mul function", *acp.STAGE_ENTRIES]
    final = next(u for u in c.updates(session) if u.get("status") == "completed")
    assert final["content"][-1] == kept and (7, True) in asked and (7, False) in asked


def test_a_run_short_of_a_pass_leaves_its_plan_unfinished(client):
    c = client(Spawner(stderr=[TASK_LINE], code=1))
    session = c.new_session()
    c.answer(c.prompt(session, "Do:\n- one\n- two"))
    plans = [u["entries"] for u in c.updates(session) if u["sessionUpdate"] == "plan"]
    assert [e["status"] for e in plans[-1]] == ["in_progress"] * 2 + ["in_progress", "pending", "pending"]


def test_a_tool_call_shows_the_last_forty_stderr_lines(client):
    lines = [f"line {n}" for n in range(45)]
    c = client(Spawner(stderr=lines))
    session = c.new_session()
    c.answer(c.prompt(session, "go"))
    last = next(u for u in c.updates(session) if u.get("title") == "line 44")
    shown = last["content"][0]["content"]["text"].split("\n")
    assert shown == lines[5:]


def test_a_run_that_exits_nonzero_is_a_failed_tool_call_that_still_reports(client):
    c = client(Spawner(stderr=["locks: #3 failed"], stdout="it failed\n", code=1))
    session = c.new_session()
    reply = c.answer(c.prompt(session, "go"))
    assert reply["result"] == {"stopReason": "end_turn"}
    updates = c.updates(session)
    assert [u["status"] for u in updates if "status" in u] == ["in_progress", "failed"]
    assert updates[-1] == {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "it failed\n"}}


@pytest.mark.parametrize("command", ["tasks", "stats"])
def test_slash_commands_run_the_reading_commands_instead(client, command):
    spawner = Spawner(stdout="the output\n")
    c = client(spawner)
    session = c.new_session()
    c.answer(c.prompt(session, f"/{command}"))
    assert spawner.calls[0][0] == [sys.executable, "-m", "locks", command]
    updates = c.updates(session)
    assert updates[1]["title"] == f"locks {command}"
    assert updates[-1]["content"]["text"] == "the output\n"


def test_a_text_that_only_starts_with_a_slash_command_is_a_request(client):
    spawner = Spawner()
    c = client(spawner)
    session = c.new_session()
    c.answer(c.prompt(session, "/tasks now"))
    assert spawner.calls[0][0][3:] == ["run", "/tasks now"]


class Looks:
    """A fake `look`: what locks would say of the task now, which a test changes; every read is counted and the
    ones past `enough` set `plenty`."""

    def __init__(self, running: acp.Look | None = None, ended: acp.Look | None = None, enough: int = 8) -> None:
        self.running, self.ended = running or acp.Look(), ended or acp.Look()
        self.reads: list[tuple[int, bool]] = []
        self.plenty = threading.Event()
        self._enough = enough

    def __call__(self, task: int, ended: bool) -> acp.Look:
        self.reads.append((task, ended))
        if len(self.reads) >= self._enough:
            self.plenty.set()
        return self.ended if ended else self.running


def said(m: dict[str, Any]) -> dict[str, Any]:
    return m.get("params", {}).get("update", {})


def statuses(m: dict[str, Any]) -> list[str] | None:
    update = said(m)
    return [e["status"] for e in update["entries"]] if update.get("sessionUpdate") == "plan" else None


NOW = acp.Look(line="implementing · 3m of ≈5m · 5 files +126 −2", doing="running npm test", wrote="wrote a.py, b.py",
               stage="worker", warnings=("no tool call for 5m (a long command, or stuck)",))


def test_the_title_is_the_line_locks_tasks_says_and_the_content_says_what_the_worker_does(client):
    looks = Looks(NOW)
    spawner = Spawner(stderr=[TASK_LINE], hold=True)
    c = client(spawner, look=looks)
    session = c.new_session()
    request = c.prompt(session, "go")
    live = c.out.wait_for(lambda m: said(m).get("title") == NOW.line)
    assert said(live)["content"] == [
        {"type": "content", "content": {"type": "text", "text": "now: running npm test\nwrote a.py, b.py\n"
                                        "⚠ no tool call for 5m (a long command, or stuck)"}},
        {"type": "content", "content": {"type": "text", "text": TASK_LINE}}]
    # What the worker does next replaces the activity, and says nothing of the title that did not change.
    looks.running = acp.Look(line=NOW.line, doing="editing a .py file", stage="worker")
    again = c.out.wait_for(lambda m: "now: editing a .py file" in str(said(m).get("content")))
    assert "title" not in said(again)
    spawner.procs[0].release()
    c.answer(request)
    # Over, the progress line and the activity are gone: the last thing locks said stands.
    final = next(u for u in c.updates(session) if u.get("status") == "completed")
    assert final["title"] == TASK_LINE
    assert final["content"] == [{"type": "content", "content": {"type": "text", "text": TASK_LINE}}]


def test_locks_s_stages_follow_the_request_s_items_in_the_plan(client):
    looks = Looks(acp.Look(stage="worker"), acp.Look(skipped=(acp.STAGE_ENTRIES[2],)))
    spawner = Spawner(stderr=[TASK_LINE], hold=True)
    c = client(spawner, look=looks)
    session = c.new_session()
    request = c.prompt(session, "Add two things:\n- one\n- two")
    assert c.out.wait_for(lambda m: statuses(m) is not None)
    first = c.out.wait_for(lambda m: statuses(m) == ["in_progress"] * 3 + ["pending"] * 2)
    assert [e["content"] for e in said(first)["entries"]] == ["one", "two", *acp.STAGE_ENTRIES]
    looks.running = acp.Look(stage=acp.STAGE_ENTRIES[1])
    c.out.wait_for(lambda m: statuses(m) == ["in_progress"] * 2 + ["completed", "in_progress", "pending"])
    looks.running = acp.Look(stage=acp.STAGE_ENTRIES[2])
    c.out.wait_for(lambda m: statuses(m) == ["in_progress"] * 2 + ["completed", "completed", "in_progress"])
    spawner.procs[0].release()
    c.answer(request)
    last = [u for u in c.updates(session) if u["sessionUpdate"] == "plan"][-1]["entries"]
    assert [e["status"] for e in last] == ["completed"] * 5
    assert last[-1]["content"] == "independent check (not run)"


def test_a_stage_with_no_item_still_shows_where_the_task_is(client):
    c = client(Spawner(stderr=[TASK_LINE]), look=Looks(acp.Look(stage=acp.STAGE_ENTRIES[1])))
    session = c.new_session()
    c.answer(c.prompt(session, "just do it"))
    plans = [[e["content"] for e in u["entries"]] for u in c.updates(session) if u["sessionUpdate"] == "plan"]
    assert plans[0] == list(acp.STAGE_ENTRIES)


def test_a_task_that_shows_nothing_new_is_said_once_however_often_it_is_read(client):
    looks = Looks(NOW, enough=12)
    spawner = Spawner(stderr=[TASK_LINE], hold=True)
    c = client(spawner, look=looks, diffs=lambda task, live: [{"type": "diff", "path": "/a", "oldText": None,
                                                              "newText": "x"}])
    session = c.new_session()
    request = c.prompt(session, "go")
    assert looks.plenty.wait(acp.PROGRESS_EVERY_S)
    spawner.procs[0].release()
    c.answer(request)
    updates = c.updates(session)
    assert len([u for u in updates if u.get("title") == NOW.line]) == 1
    assert len([u for u in updates if "now: running npm test" in str(u.get("content"))]) == 1
    assert len([u for u in updates if u["sessionUpdate"] == "plan"]) == 3  # pending, started, completed
    assert len(updates) < 12


def test_the_progress_of_a_run_is_read_only_while_it_is_a_request_and_has_a_task(client):
    looks = Looks(NOW, enough=1)
    c = client(Spawner(stderr=["locks: #7 keeps its number here"], stdout="out\n"), look=looks)
    session = c.new_session()
    c.answer(c.prompt(session, "/tasks"))
    assert looks.reads == []
    c2 = client(Spawner(stdout="out\n"), look=looks)  # a request whose run never named a task
    c2.answer(c2.prompt(c2.new_session(), "go"))
    assert looks.reads == []


def test_the_end_of_a_failed_check_stays_in_view(client):
    failed = acp.Look(check="locks' check failed: `npm test` exited 1\nthe end of its output:\nboom")
    spawner = Spawner(stderr=[TASK_LINE], hold=True, code=1)
    c = client(spawner, look=Looks(failed, failed))
    session = c.new_session()
    request = c.prompt(session, "go")
    shown = c.out.wait_for(lambda m: "boom" in str(said(m).get("content")))
    assert said(shown)["content"][-1] == {"type": "content", "content": {"type": "text", "text": failed.check}}
    spawner.procs[0].release()
    c.answer(request)
    assert [u for u in c.updates(session) if "boom" in str(u.get("content"))]
    assert next(u for u in c.updates(session) if u.get("status") == "failed")["sessionUpdate"] == "tool_call_update"


def test_the_agent_message_leads_with_the_verdict_and_the_next_step_then_the_report(client):
    ending = "locks: task #7 passed.\nThe work is on branch locks/task-7. Next: `locks merge 7`."
    c = client(Spawner(stderr=[TASK_LINE], stdout="the report\n"), look=Looks(ended=acp.Look(ending=ending)),
               )
    session = c.new_session(mcp=[{"name": "x"}])
    c.answer(c.prompt(session, "go"))
    message = c.updates(session)[-1]
    assert message["sessionUpdate"] == "agent_message_chunk"
    assert message["content"]["text"] == f"{acp.MCP_NOTICE}\n\n{ending}\n\nthe report\n"


def test_a_look_that_cannot_be_read_costs_the_view_never_the_answer(client):
    def broken(task: int, ended: bool) -> acp.Look:
        raise RuntimeError("the store is locked")

    c = client(Spawner(stderr=[TASK_LINE], stdout="the report\n"), look=broken)
    session = c.new_session()
    assert c.answer(c.prompt(session, "go"))["result"] == {"stopReason": "end_turn"}
    assert c.updates(session)[-1]["content"]["text"] == "the report\n"
    assert "the store is locked" in c.err.getvalue()


def first_title(update_title: str):
    return lambda m: m.get("params", {}).get("update", {}).get("title") == update_title


def test_a_cancel_nothing_follows_asks_locks_to_cancel_the_task_the_run_named(client, monkeypatch):
    monkeypatch.setattr(acp, "CANCEL_WINDOW_S", 0.05)
    spawner = Spawner(stderr=[TASK_LINE, "locks: #8 other"], hold=True, code=-15)
    c = client(spawner)
    session = c.new_session("/work/app")
    request = c.prompt(session, "go")
    c.out.wait_for(first_title(TASK_LINE))
    c.notify("session/cancel", {"sessionId": session})
    reply = c.answer(request)
    assert reply["result"] == {"stopReason": "cancelled"}
    assert spawner.cancelled.wait(WAIT_S)
    c.close()
    argv, kwargs = spawner.calls[1]
    assert argv == [sys.executable, "-m", "locks", "cancel", "7"]
    assert kwargs["cwd"] == "/work/app"
    assert spawner.killed == []
    # The turn ended with its answer: the run's own ending is not said after it.
    assert [u["status"] for u in c.updates(session) if "status" in u] == ["in_progress"]
    assert c.out.messages()[-1] == reply


def test_a_run_that_outlasts_its_cancel_is_killed_with_its_process_group(client, monkeypatch):
    monkeypatch.setattr(acp, "CANCEL_WINDOW_S", 0.05)
    spawner = Spawner(release_on_cancel=False, stderr=[TASK_LINE], hold=True, code=-9)
    c = client(spawner, grace=0.2)
    session = c.new_session()
    request = c.prompt(session, "go")
    c.notify("session/cancel", {"sessionId": session})
    assert c.answer(request)["result"] == {"stopReason": "cancelled"}
    assert spawner.was_killed.wait(WAIT_S)
    assert spawner.killed == [(4242, signal.SIGKILL)]


def test_a_cancel_is_answered_at_once_and_holds_the_task_for_a_prompt_that_may_follow(client, monkeypatch):
    monkeypatch.setattr(acp, "CANCEL_WINDOW_S", 60.0)  # longer than the test may wait: only a closed input ends it
    spawner = Spawner(stderr=[TASK_LINE], hold=True)
    c = client(spawner)
    session = c.new_session()
    request = c.prompt(session, "go")
    c.out.wait_for(first_title(TASK_LINE))
    c.notify("session/cancel", {"sessionId": session})
    assert c.answer(request)["result"] == {"stopReason": "cancelled"}
    assert spawner.procs[0].running
    assert spawner.asked_to_cancel() == []
    # Nothing can follow a closed input: the cancel stands without waiting out the window.
    c.close()
    assert spawner.asked_to_cancel() == [[sys.executable, "-m", "locks", "cancel", "7"]]


def test_a_prompt_after_a_cancel_leaves_the_task_running_and_starts_its_own(client, monkeypatch):
    """What Zed does when a person sends a request while one runs: `session/cancel`, the answer to the
    turn, then the new `session/prompt`. The first task is delegated work, not that turn."""
    monkeypatch.setattr(acp, "CANCEL_WINDOW_S", 60.0)  # the window must end by the prompt, not by its time
    spawner = Spawner(stderr=[TASK_LINE], stdout="old report\n", hold=True, code=-15)
    c = client(spawner)
    session = c.new_session("/work/app")
    first = c.prompt(session, "one")
    c.out.wait_for(first_title(TASK_LINE))
    c.notify("session/cancel", {"sessionId": session})
    assert c.answer(first)["result"] == {"stopReason": "cancelled"}
    spawner.run.update(hold=False, stdout="new report\n", stderr=[])
    second = c.prompt(session, "two")
    assert c.answer(second)["result"] == {"stopReason": "end_turn"}

    assert spawner.asked_to_cancel() == []
    assert spawner.killed == []
    assert spawner.procs[0].running
    assert spawner.calls[1][0][3:] == ["run", "two"]
    assert spawner.calls[1][1]["cwd"] == "/work/app"

    # The first turn is over: what its run does from here is not said to the editor, nor answered again.
    first_call = c.updates(session)[1]["toolCallId"]
    spawner.procs[0].say("locks: #7 attempt 2 at opus/high")
    spawner.procs[0].release()
    c.close()
    updates = c.updates(session)
    assert [u.get("title") for u in updates if u.get("toolCallId") == first_call] == ["locks run", TASK_LINE]
    assert len([m for m in c.out.messages() if m.get("id") == first]) == 1
    # The notice comes before the new prompt's own tool call, and the old report is never said.
    assert [u.get("content", {}).get("text", u["sessionUpdate"]) for u in updates
            if u["sessionUpdate"] in ("tool_call", "agent_message_chunk")] == [
        "tool_call", acp.kept_notice(7), "tool_call", "new report\n"]
    assert spawner.asked_to_cancel() == []


def test_the_notice_of_a_task_left_running_names_it_when_it_is_known():
    assert "#7" in acp.kept_notice(7)
    assert "/cancel 7" in acp.kept_notice(7)
    assert "#" not in acp.kept_notice(None)
    assert "/tasks" in acp.kept_notice(None)


def test_a_prompt_after_a_cancel_does_not_say_a_task_that_already_ended_keeps_running():
    sink, spawner = Sink(), Spawner()
    agent = acp.Agent(sink, io.StringIO(), spawner, spawner.killpg, 30.0, lambda task, live: [])
    ended = acp.Run(proc=None, call_id="locks-old", request_id=1, task=7, released=True, answered=True)
    ended.exited.set()
    session = acp.Session(cwd="/work/app", mcp_ignored=False, waiting=ended)
    agent._sessions["sess_x"] = session
    agent._handle(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {
        "sessionId": "sess_x", "prompt": [{"type": "text", "text": "two"}]}}))
    agent.serve([])
    assert ended.decided.is_set()
    assert session.waiting is None
    assert [m for m in sink.messages() if "keeps running" in json.dumps(m)] == []
    assert [m["result"] for m in sink.messages() if m.get("id") == 2] == [{"stopReason": "end_turn"}]


def test_a_prompt_is_answered_once_whichever_of_its_end_and_a_cancel_comes_first():
    spawner = Spawner()
    agent = acp.Agent(Sink(), io.StringIO(), spawner, spawner.killpg, 30.0)
    session = acp.Session(cwd="/work/app", mcp_ignored=False)
    run = session.run = acp.Run(proc=None, call_id="locks-x", request_id=1)
    assert agent._claim(session, run, released=True)
    assert session.run is None
    assert session.waiting is run
    assert run.released
    assert not agent._claim(session, run)
    assert run.released  # the late end did not take the answer back


@pytest.mark.parametrize("said", ["/cancel 7", "/cancel #7", "  /cancel   7  "])
def test_cancel_with_a_task_number_runs_locks_cancel_as_a_prompt(client, said):
    spawner = Spawner(stdout="cancelled #7\n")
    c = client(spawner)
    session = c.new_session()
    assert c.answer(c.prompt(session, said))["result"] == {"stopReason": "end_turn"}
    assert spawner.calls[0][0] == [sys.executable, "-m", "locks", "cancel", "7"]
    assert spawner.asked_to_cancel() == []  # it is the prompt's run, not the agent's own cancel
    updates = c.updates(session)
    assert updates[1]["title"] == "locks cancel"
    assert updates[-1]["content"]["text"] == "cancelled #7\n"


@pytest.mark.parametrize("said", ["/cancel", "/cancel soon", "/cancel 7 8", "/cancel #"])
def test_cancel_without_one_task_number_is_refused_and_starts_no_worker(client, said):
    spawner = Spawner()
    c = client(spawner)
    session = c.new_session()
    error = c.answer(c.prompt(session, said))["error"]
    assert error["code"] == -32602
    assert "/cancel 7" in error["message"]
    assert spawner.calls == []
    # The session is not left busy by the refusal.
    assert c.answer(c.prompt(session, "go"))["result"] == {"stopReason": "end_turn"}


def test_a_cancel_command_after_a_cancel_is_what_stops_the_task(client):
    spawner = Spawner(stderr=[TASK_LINE], hold=True)
    c = client(spawner)
    session = c.new_session()
    first = c.prompt(session, "go")
    c.out.wait_for(first_title(TASK_LINE))
    c.notify("session/cancel", {"sessionId": session})
    assert c.answer(first)["result"] == {"stopReason": "cancelled"}
    spawner.run.update(hold=False)
    c.answer(c.prompt(session, "/cancel 7"))
    assert spawner.calls[1][0][3:] == ["cancel", "7"]
    assert spawner.procs[0].running  # the fake `locks cancel` is a prompt's run: nothing stopped it
    assert spawner.asked_to_cancel() == []


def test_loading_a_saved_thread_is_answered_with_what_to_do_instead(client):
    c = client()
    params = {"sessionId": "7a9d7340-ef99-4d9c-8958-08877ef812da", "cwd": "/work/app", "mcpServers": []}
    error = c.answer(c.request("session/load", params))["error"]
    assert error["code"] == acp.SESSION_NOT_FOUND
    assert "open a new thread" in error["message"]
    c.notify("session/load", params)  # as a notification it has nobody to answer: ignored
    assert c.answer(c.request("authenticate"))["result"] == {}
    assert len(c.out.messages()) == 2
    # A thread opened after the refusal works as any.
    assert c.new_session("/work/app").startswith("sess_")


def test_a_second_prompt_while_one_runs_is_refused_and_the_first_goes_on(client):
    spawner = Spawner(hold=True, stdout="done\n")
    c = client(spawner)
    session = c.new_session()
    first = c.prompt(session, "one")
    c.out.wait_for(lambda m: m.get("params", {}).get("update", {}).get("sessionUpdate") == "tool_call")
    second = c.prompt(session, "two")
    assert c.answer(second)["error"]["code"] == -32000
    assert len(spawner.calls) == 1
    spawner.procs[0].release()
    assert c.answer(first)["result"] == {"stopReason": "end_turn"}
    # The session is free again.
    spawner.run["hold"] = False
    other = c.prompt(session, "three")
    assert c.answer(other)["result"] == {"stopReason": "end_turn"}


def test_a_cancel_is_read_while_a_prompt_of_another_session_runs(client):
    spawner = Spawner(hold=True)
    c = client(spawner)
    busy, idle = c.new_session(), c.new_session()
    c.prompt(busy, "one")
    c.out.wait_for(lambda m: m.get("params", {}).get("update", {}).get("sessionUpdate") == "tool_call")
    c.notify("session/cancel", {"sessionId": idle})  # nothing runs there: ignored
    assert c.answer(c.request("authenticate"))["result"] == {}


def test_a_client_mcp_list_is_ignored_and_said_once_in_the_first_answer(client):
    c = client(Spawner(stdout="the report\n"))
    session = c.new_session(mcp=[{"name": "files", "command": "x", "args": [], "env": []}])
    c.answer(c.prompt(session, "go"))
    c.answer(c.prompt(session, "again"))
    chunks = [u["content"]["text"] for u in c.updates(session) if u["sessionUpdate"] == "agent_message_chunk"]
    assert chunks == [acp.MCP_NOTICE + "\n\nthe report\n", "the report\n"]
    quiet = c.new_session()
    c.answer(c.prompt(quiet, "go"))
    assert [u["content"]["text"] for u in c.updates(quiet) if u["sessionUpdate"] == "agent_message_chunk"] == [
        "the report\n"]


def test_a_process_that_cannot_start_answers_with_an_error(client):
    def broken(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    c = client(popen=broken)
    session = c.new_session("/gone")
    error = c.answer(c.prompt(session, "go"))["error"]
    assert error["code"] == -32603
    assert "/gone" in error["message"]
    assert not [u for u in c.updates(session) if u["sessionUpdate"] == "tool_call"]
    # The session is not left busy by the failed start.
    assert c.answer(c.prompt(session, "again"))["error"]["code"] == -32603


def test_unknown_methods_notifications_and_garbage_are_answered_as_json_rpc_says(client):
    c = client()
    c.notify("session/whatever", {})  # a notification nobody knows: ignored
    c.raw("")  # a blank line is no message
    c.raw("{this is not json")
    c.raw("[1, 2]")
    unknown = c.request("session/whatever")
    prompt = c.request("session/prompt", {"sessionId": "nope", "prompt": [{"type": "text", "text": "x"}]})
    bad_blocks = c.request("session/prompt", {"sessionId": c.new_session(), "prompt": "text"})
    no_text = c.request("session/prompt", {"sessionId": c.new_session(), "prompt": [{"type": "image"}]})
    c.answer(no_text)
    errors = {m["id"]: m["error"] for m in c.out.messages() if "error" in m}
    assert [m["error"] for m in c.out.messages() if "error" in m and m["id"] is None] == [
        {"code": -32700, "message": "Parse error"}, {"code": -32600, "message": "Invalid Request"}]
    assert errors[unknown] == {"code": -32601, "message": "Method not found"}
    assert errors[prompt]["code"] == -32602
    assert errors[bad_blocks]["code"] == -32602
    assert errors[no_text]["code"] == -32602
    # Nothing else was said: not for the unknown notification, nor the blank line.
    assert len(c.out.messages()) == 2 + 4 + 2 * 2  # two garbage, four requests, two sessions' announcements and answers


def test_the_loop_ends_at_eof_only_after_the_running_prompts_finish(client):
    spawner = Spawner(hold=True, stdout="late\n")
    c = client(spawner)
    session = c.new_session()
    request = c.prompt(session, "go")
    c.out.wait_for(lambda m: m.get("params", {}).get("update", {}).get("sessionUpdate") == "tool_call")
    c._lines.put(None)  # the client closed its input
    assert c.thread.is_alive()
    spawner.procs[0].release()
    c.thread.join(WAIT_S)
    assert not c.thread.is_alive()
    assert c.answer(request)["result"] == {"stopReason": "end_turn"}


def test_the_subcommand_serves_the_protocol_on_stdout_alone(tmp_path):
    lines = "\n".join(json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": {}})
                      for n, method in enumerate(["initialize", "authenticate"], 1)) + "\n"
    proc = subprocess.run([sys.executable, "-m", "locks", "acp"], input=lines, capture_output=True, text=True,
                          check=False, timeout=30, cwd=tmp_path,
                          env={**os.environ, "PYTHONPATH": str(Path(locks.__file__).parents[1])})
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[0]["result"]["agentInfo"]["version"] == locks.__version__


def test_an_unread_independent_check_is_not_told_as_zero_blockers():
    from locks import acp

    text = "\n".join(acp.verdict_text({"check": None, "independent": {"ran": True, "passed": False,
                                                                       "blocking": 0, "unread": True}}))
    assert "blocker" not in text and "read nothing twice" in text
