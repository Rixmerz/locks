"""A guard between a worker and an MCP server: the calls a worker nobody watches
must not make are refused here, not only asked of it in its prompt.

    python -m locks.mcp_guard rastro -- rastro mcp --routines=off

It runs the server as a child and passes JSON-RPC lines both ways (MCP over
stdio is one JSON message per line). A `tools/call` the policy refuses never
reaches the server: the worker gets a tool error saying why. Everything else
passes unchanged. Standard library only, and no import of the rest of locks:
it starts inside every worker that is handed the server.

The `rastro` policy keeps a worker to the app its task runs:

- a browser profile is refused: it holds the person's logins;
- writes and uploads are let through only to loopback hosts (localhost,
  127.x, ::1): the app under test, never a real site;
- saved routines and the profile list are refused: a routine can write with
  the person's logins;
- every call runs in rastro's session `locks-worker`, so a worker never picks
  up a session of the person's, with what it was bound to.

The `engram` policy keeps a worker to reading its own project's memory:

- only engram's reading tools pass: a save, an update, a delete, a judgment or
  a session call never reaches the store (what a worker learned reaches engram
  through locks, after a pass);
- every call is held to the project locks resolved (`ENGRAM_PROJECT`): another
  project, every project, and personal scope (which reads across projects) are
  refused.

The `livespec` policy keeps a worker to reading the code index:

- only livespec's reading tools pass: indexing, linking, syncing or exporting specs, creating,
  updating or deleting one never reaches the index (a tool's own `readOnlyHint` is not trusted:
  `export_openspec` carries it and writes files), so the independent check that is handed the
  index can look at the change and touch nothing.

The `outbound` policy holds the tools that act on the world (a capability lists
them under `outbound`) to the loop the worker's task is a round of
(`locks.loops`), read from the person's locks home at each call:

- outside a loop, or in a loop that is a draft, an acting call is refused: the
  worker writes what it would send instead;
- in a loop the person set to act, a call is refused outside the loop's hours,
  past its cap of acting calls an hour (across all its workers, counted in the
  loop's own log, which keeps when and which tool, never the arguments), or
  when its arguments name something the loop excludes;
- every other tool of the server passes unchanged.
"""
from __future__ import annotations

import contextlib
import fcntl
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

SESSION = "locks-worker"
#: rastro's own tools; any other name is a saved routine exposed as a tool.
RASTRO_TOOLS = frozenset({"rastro_open", "rastro_view", "rastro_read", "rastro_act", "rastro_navigate",
                          "rastro_effects", "rastro_trace", "rastro_request", "rastro_history", "rastro_detail",
                          "rastro_export"})
RASTRO_REFUSED = frozenset({"rastro_profiles", "rastro_routines", "rastro_routine_run"})


def loopback(host: str) -> bool:
    """A host a write may go to: this machine only."""
    value = host.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split("@")[-1]
    if value.startswith("["):
        value = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    if value == "localhost" or value.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def rastro(name: str, arguments: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Why a call is refused (None when it is not), and its arguments as sent on."""
    args = dict(arguments)
    if name in RASTRO_REFUSED or name not in RASTRO_TOOLS:
        return (f"{name} is not for a worker: saved routines and browser profiles can act with the person's "
                "logins"), args
    if name == "rastro_open":
        if args.get("profile"):
            return "a browser profile holds the person's logins: open without one", args
        for key in ("allowWrite", "allowUpload"):
            hosts = args.get(key) or []
            if not isinstance(hosts, list):
                return f"{key} is a list of hosts", args
            others = [h for h in hosts if not isinstance(h, str) or not loopback(h)]
            if others:
                return (f"{key} reaches only the app on this machine (localhost); refused for "
                        f"{', '.join(map(str, others))}"), args
    args["session"] = SESSION
    return None, args


#: engram's tools that only read.
ENGRAM_READ = frozenset({"mem_search", "mem_context", "mem_timeline", "mem_get_observation", "mem_current_project",
                         "mem_stats"})


def engram(name: str, arguments: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
    args = dict(arguments)
    if name not in ENGRAM_READ:
        return f"{name} changes engram's store; a worker only reads it, and locks saves what it learned", args
    project = os.environ.get("ENGRAM_PROJECT", "")
    if args.get("all_projects") or args.get("scope") == "personal":
        return f"a worker reads only this project's memory ({project or 'its own'})", args
    if args.get("project") not in (None, "", project):
        return f"a worker reads only this project's memory ({project}), not {args['project']}", args
    if project:
        args["project"] = project
    return None, args


#: livespec's tools that only read the index (and the repository's files), by name: the list is
#: the policy, so a tool livespec adds later is refused until it is read and added here.
LIVESPEC_READ = frozenset({
    "quick_orient", "find_symbol", "who_calls", "who_does_this_call", "analyze_impact", "git_diff_impact",
    "get_symbol_source", "get_spec_implementation", "list_specs", "audit_coverage", "find_orphan_tests",
    "find_dead_code", "search", "get_project_overview", "find_endpoints", "find_legacy_flows",
    "grep_in_indexed_files", "validate_openspec", "list_spec_changes", "get_spec_change", "read_unit",
    "resolve_location", "search_similar"})


def livespec(name: str, arguments: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
    if name not in LIVESPEC_READ:
        return f"{name} may change the index or the specs; this worker only reads them", dict(arguments)
    return None, dict(arguments)


def locks_home(env: Mapping[str, str]) -> Path:
    """locks' home as `locks.store.home` reads it (this module imports nothing of locks but
    `locks.legacy`, the old name's stdlib-only reader)."""
    from locks import legacy

    if given := legacy.getenv(env, "LOCKS_HOME"):
        return Path(given).expanduser()
    xdg = env.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg.startswith("/") else Path(env.get("HOME") or Path.home()) / ".local" / "share"
    return legacy.home(base)


def outbound(name: str, arguments: Mapping[str, Any], env: Mapping[str, str] | None = None,
             now: float | None = None) -> tuple[str | None, dict[str, Any]]:
    env = os.environ if env is None else env
    args = dict(arguments)
    if name not in {a for a in env.get("LOCKS_OUTBOUND_ACTS", "").split(",") if a}:
        return None, args
    loop_name = env.get("LOCKS_LOOP", "")
    if not loop_name or "/" in loop_name:
        return (f"{name} acts on the world: it runs only in a round of a loop the person set to act "
                "(`locks loop act`); write what you would send in your result instead"), args
    folder = locks_home(env) / "loops"
    try:
        loop = json.loads((folder / f"{loop_name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"loop {loop_name!r} cannot be read: nothing acts", args
    if loop.get("mode") != "act":
        return (f"loop {loop_name!r} is a draft: nothing leaves; write what you would send, to whom and why, "
                "in your result"), args
    now = time.time() if now is None else now
    hours = str(loop.get("hours") or "")
    if hours:
        start, end = (int(h) for h in hours.split("-", 1))
        if not start <= datetime.fromtimestamp(now).hour < end:
            return f"loop {loop_name!r} acts only between {start}:00 and {end}:00; list it for later", args
    blob = json.dumps(args, ensure_ascii=False).lower()
    if any(str(x).lower() in blob for x in loop.get("exclude") or () if str(x).strip()):
        return f"this call names someone or something loop {loop_name!r} excludes", args
    cap = int(loop.get("per_hour") or 0)
    log = folder / f"{loop_name}.sent.jsonl"
    with open(folder / f"{loop_name}.sent.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if cap:
            recent = 0
            with contextlib.suppress(FileNotFoundError):
                for line in log.read_text(encoding="utf-8").splitlines():
                    with contextlib.suppress(ValueError, TypeError):
                        recent += float(json.loads(line)["at"]) > now - 3600
            if recent >= cap:
                return f"loop {loop_name!r} acts at most {cap} times an hour; list the rest for later", args
        with log.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"at": now, "tool": name}) + "\n")
    return None, args


POLICIES: Mapping[str, Callable[[str, Mapping[str, Any]], tuple[str | None, dict[str, Any]]]] = {
    "rastro": rastro, "engram": engram, "livespec": livespec, "outbound": outbound}


def screen(message: Any, policy: Callable[[str, Mapping[str, Any]], tuple[str | None, dict[str, Any]]]
           ) -> tuple[Any, list[dict[str, Any]]]:
    """A message from the worker: what goes on to the server (None for nothing),
    and the answers to send back for the calls refused."""
    if isinstance(message, list):
        onward, answers = [], []
        for item in message:
            sent, refused = screen(item, policy)
            if sent is not None:
                onward.append(sent)
            answers += refused
        return (onward or None), answers
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return message, []
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    name = params.get("name") if isinstance(params.get("name"), str) else ""
    arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    why, args = policy(name, arguments)
    if why is None:
        return {**message, "params": {**params, "arguments": args}}, []
    answer = {"jsonrpc": "2.0", "id": message.get("id"),
              "result": {"content": [{"type": "text", "text": f"locks refused this call: {why}"}], "isError": True}}
    return None, [answer]


#: The environment variable that holds a guarded server to some of its tools (`plugins.guarded`'s
#: `tools`): comma-separated names. Its `tools/list` shows only those, and a call to another is refused.
ONLY = "LOCKS_GUARD_ONLY"


def only_from(env: Mapping[str, str]) -> frozenset[str] | None:
    """The tools a guarded server is held to, or None when it is not held."""
    if ONLY not in env:
        return None
    return frozenset(name for name in env[ONLY].split(",") if name)


def held_to(policy: Callable[[str, Mapping[str, Any]], tuple[str | None, dict[str, Any]]], only: frozenset[str]
            ) -> Callable[[str, Mapping[str, Any]], tuple[str | None, dict[str, Any]]]:
    """The policy, with every tool outside `only` refused first."""
    def held(name: str, arguments: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
        if name not in only:
            return f"{name} is not among the tools this worker was handed", dict(arguments)
        return policy(name, arguments)
    return held


def listed(message: Any, asked: set[Any], only: frozenset[str] | None) -> Any:
    """A server's answer, with the tools of a `tools/list` it answers cut to `only`."""
    if only is None or not isinstance(message, dict) or message.get("id") not in asked:
        return message
    asked.discard(message.get("id"))
    result = message.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        return message
    tools = [t for t in result["tools"] if isinstance(t, dict) and t.get("name") in only]
    return {**message, "result": {**result, "tools": tools}}


def relay(source: BinaryIO, sink: BinaryIO, lock: threading.Lock, asked: set[Any] | None = None,
          only: frozenset[str] | None = None) -> None:
    for line in iter(source.readline, b""):
        if only is not None:
            with contextlib.suppress(ValueError):
                line = json.dumps(listed(json.loads(line), asked if asked is not None else set(), only)
                                  ).encode() + b"\n"
        with lock:
            sink.write(line)
            sink.flush()


def serve(policy_name: str, command: list[str], stdin: BinaryIO, stdout: BinaryIO,
          env: Mapping[str, str] | None = None) -> int:
    only = only_from(os.environ if env is None else env)
    policy = POLICIES[policy_name] if only is None else held_to(POLICIES[policy_name], only)
    asked: set[Any] = set()
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert child.stdin is not None and child.stdout is not None
    lock = threading.Lock()
    back = threading.Thread(target=relay, args=(child.stdout, stdout, lock, asked, only), daemon=True)
    back.start()
    try:
        for line in iter(stdin.readline, b""):
            try:
                message = json.loads(line)
            except ValueError:
                child.stdin.write(line)
                child.stdin.flush()
                continue
            for item in message if isinstance(message, list) else [message]:
                if isinstance(item, dict) and item.get("method") == "tools/list":
                    asked.add(item.get("id"))
            onward, answers = screen(message, policy)
            for answer in answers:
                with lock:
                    stdout.write(json.dumps(answer).encode() + b"\n")
                    stdout.flush()
            if onward is not None:
                child.stdin.write(json.dumps(onward).encode() + b"\n")
                child.stdin.flush()
    except BrokenPipeError:
        pass
    finally:
        with contextlib.suppress(OSError):
            child.stdin.close()
        code = child.wait()
        back.join(timeout=2)
    return code


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] not in POLICIES or argv[1] != "--":
        print(f"usage: python -m locks.mcp_guard {{{','.join(POLICIES)}}} -- <server command>", file=sys.stderr)
        return 2
    return serve(argv[0], argv[2:], sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":  # pragma: no cover - the module is run as a server
    sys.exit(main(sys.argv[1:]))
