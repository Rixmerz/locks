"""Claude Code hooks: every prompt becomes a task, and what is said about it stays with it.

- **UserPromptSubmit** — a new prompt is a new task. A message typed while a
  turn is running belongs to that turn's task, not to one of its own. A bare
  "continue" folds into the turn it pushes along. When the prompt matches a
  fix that already failed — here or in another repository — the dead end is
  put in front of the model before it starts; when it matches this project's
  notes, so are they.
- **Stop** — the turn's final message is the task's result. Any other task
  still running in the session belongs to a turn the user interrupted; Claude
  Code fires no Stop for those, so they are marked interrupted now. The scribe
  reads what the transcript gained (`locks.scribe`) and, every few turns, has
  the project's notes filed.
- **StopFailure** — the turn died; its task failed.
- **SessionStart** — on resume or compaction, the unfinished tasks of the
  session; on a new session where a person left a relay (`locks relay`), the
  thread of the session it continues; always, the recent dead ends of this
  repository and the index of its notes. When `locks` does not resolve on PATH, it is put there for the
  session's commands.
- **PreCompact**, **SessionEnd** — a detached Haiku reads what the session
  said since the last time and keeps its durable facts in the project's notes,
  before the conversation is summarized away or closed.
- **Stop**, **PreCompact**, **PostCompact** — whether a turn that ended is a
  good moment to compact, and every compaction seen, for the mod to suggest or
  submit `/compact` and learn whether it ran (`locks.compaction`).

Hooks fail open: an exception never reaches Claude Code, because a hook that
raises takes the user's session down. But failing open is not failing silently
— the error goes to a ledger, and the next SessionStart says it happened.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

from locks import after, config, legacy, orchestrator, project, repo, specs, stops, trace
from locks import findings as findings_mod
from locks.notes import _SECRET
from locks.store import OPEN_FOR_A_RELAY, Store, home
from locks.text import clip, fold, words

_NUDGES = frozenset({
    "sigue", "continua", "continue", "--continue", "go on", "keep going", "dale", "sigue porfa",
    "sigue por favor", "continua por favor", "please continue", "proceed", "procede",
})
_MAX_DEAD_ENDS = 3
QUEUE_PREFIX = "++"
DELEGATION_TOOLS = frozenset({"Agent", "Task"})


def _queue_request(text: str) -> str | None:
    """The task text after the queue prefix, "" for a bare prefix, None otherwise."""
    stripped = text.lstrip()
    if not stripped.startswith(QUEUE_PREFIX):
        return None
    return stripped[len(QUEUE_PREFIX):].strip()
ERROR_LOG = "hook-errors.log"


def _context(event_name: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}


#: How Claude Code hands a background subagent's final report back to the session.
_HANDBACK = re.compile(r'^<agent-message from="([^"]+)">\s*\[Subagent hand-back\]')


#: How the endings notice opens: the mod submits it as a prompt to wake an idle session.
ENDINGS_HEADER = "locks: work this session sent has ended since you last heard."
#: A prompt a plugin submitted in the person's place, as Claude Code words it.
_PLUGIN_SENT = re.compile(r"^The [\w.:@-]+ plugin sent a message")


def _ignored(text: str) -> bool:
    from locks.compact import ASKED

    stripped = text.strip()
    # Harness-generated turns (task notifications, local command output) are not
    # requests anyone made, and neither is a notice locks itself put in the
    # session, nor a `/compact` the mod submitted that reached the model as a
    # message: none of them is a turn to record as a task.
    return (not stripped or stripped.startswith(("<task-notification", "<command-", "<local-command", "<system-",
                                                 ENDINGS_HEADER, ASKED))
            or bool(_PLUGIN_SENT.match(stripped)) or bool(_ECHO.fullmatch(stripped)))


#: A prompt that is only a command line's flags (`--continue`, `--resume <id>`) or a bare
#: slash command (`/clear`): an echo of how a session was started or cleared, no request.
_ECHO = re.compile(r"(?:--?[A-Za-z][\w-]*(?:[= ][^\s-]\S*)?\s*)+|/[A-Za-z][\w:-]*")


def _dead_end_text(dead: list[dict], intro: str) -> str:
    lines = [intro]
    for d in dead:
        line = f"- {d['problem']} ({d['repo'] or 'unknown repo'}): tried {clip(d['tried'], 200, one_line=True)}"
        if d["why"]:
            line += f"; failed: {clip(d['why'], 120, one_line=True)}"
        if d["worked_instead"]:
            line += f"; what worked: {clip(d['worked_instead'], 200, one_line=True)}"
        lines.append(line)
    return "\n".join(lines)


def user_prompt_submit(event: Mapping[str, Any], store: Store) -> dict | None:
    session_id = event.get("session_id")
    text = str(event.get("prompt") or "")
    if session_id and text.lstrip().startswith("<task-notification"):
        _background_child_finished(session_id, text, store)
        return None
    if session_id and (handback := _HANDBACK.match(text.lstrip())):
        _handed_back(store, event, handback.group(1), text)
        return None
    if not session_id or _ignored(text):
        return None
    _record_loaded(store.path.parent, str(session_id))
    cwd = event.get("cwd")
    key = repo.key(cwd) if cwd else None
    prompt_id = event.get("prompt_id")
    store.touch_session(session_id, cwd, key)

    queued = _queue_request(text)
    if queued is not None:
        if not queued:
            return {"decision": "block", "reason": f'locks: "{QUEUE_PREFIX} <task>" queues a task for a worker'}
        task = store.enqueue(queued, repo=key, cwd=cwd, session_id=session_id, author="person")
        if cwd:
            _enroll(cwd)
            orchestrator.sent(store, cwd, {"LOCKS_SESSION_ID": str(session_id)})
        # Blocked: queuing costs no turn. The reason is what the person sees.
        return {"decision": "block", "reason": f"locks: queued #{task['id']} — {task['title']}. "
                + _kick(cwd, key)}

    running = store.running_task(session_id, prompt_id) if prompt_id else None
    if running is not None:
        store.add_message(running["id"], "user", text)
        return None

    if " ".join(fold(text).split()) in _NUDGES:
        previous = store.last_prompt_task(session_id)
        if previous is not None and previous["status"] in ("interrupted", "failed"):
            store.add_message(previous["id"], "user", text)
            store.update_task(previous["id"], status="running", prompt_id=prompt_id)
            return None

    task = store.create_task(text, status="running", source="hook", session_id=session_id, prompt_id=prompt_id,
                             cwd=cwd, repo=key)
    blocks = [notice] if (notice := _endings(store, session_id, via="prompt")) else []
    # The session delegates: it hears only of failures met across areas, never one
    # cell's (`locks.cells`), which goes to the workers of that cell.
    from locks import cells

    dead = cells.dead_ends(store, text, repo=key, shared_only=True, limit=_MAX_DEAD_ENDS) \
        if len(words(text)) >= 3 else []
    if dead:
        store.add_event(task["id"], "dead_ends", shown=dead)
        blocks.append(_dead_end_text(
            dead, "locks memory: fixes already tried against problems like this one, and they did not work. "
            "Before applying one again, say so and check why it failed."))
    if key and cwd and len(words(text)) >= 3 and config.enabled("notes", os.environ) and project.find(cwd):
        from locks import notes

        known = notes.recall(store, key, text, limit=3, hops=0, strict=True)
        if known:
            store.add_event(task["id"], "notes_shown", ids=[n["id"] for n in known])
            blocks.append("locks notes: what this project's notes say about this (kept from earlier work; check "
                          "them against the code, and `locks recall \"<question>\"` for more):\n"
                          + "\n".join(f"- {notes.line(n, text_chars=300)}" for n in known))
    if routed := _route(store, task["id"], text, cwd, event.get("transcript_path"), str(session_id)):
        blocks.append(routed)
    if newer := _newer_plugin(store.path.parent, str(session_id), os.environ):
        blocks.append(newer)
    return _context("UserPromptSubmit", "\n\n".join(blocks)) if blocks else None


#: The sessions told of a newer install, kept in the locks home; the newest ones only.
NEWER_SAID, NEWER_KEPT = "newer-plugin.json", 200


#: Where the older installs SessionStart named are kept, so the same set is said once.
OLDER_SAID = "older-installs.json"


def _older_installs(root: Path, env: Mapping[str, str]) -> str | None:
    """Older locks installs left in Claude Code's plugin cache, named once per set: after a
    compaction a hook may still run from one of them, an older version's code and text. The
    person removes them; locks never deletes from Claude Code's folder. Never costs the turn."""
    try:
        from locks import __version__, link

        mine = link.parse_version(__version__)
        found = link.installed(env)
        # An install of the old name is older whatever its version: it is the old plugin, this is Locks.
        old_name = [(v, launcher.parents[1]) for v, launcher in link.installed(env, old=True)]
        older = [(v, launcher.parents[1]) for v, launcher in found if mine and v < mine] + old_name
        if not older or not mine or (found and max(v for v, _ in found) > mine):
            return None  # none, or this one is not the newest: the newer-plugin notice speaks
        key = sorted(str(folder) for _, folder in older)
        path = root / "work" / OLDER_SAID
        try:
            if json.loads(path.read_text()) == key:
                return None
        except (OSError, ValueError):
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(key))
    except Exception:  # the session starts either way
        note_error(root, "older installs")
        return None
    live = link.live_versions(root)
    held = [folder for v, folder in older if ".".join(map(str, v)) in live]
    names = ", ".join(".".join(map(str, v)) for v, folder in older if folder.parent.name != legacy.PLUGIN)
    old = ", ".join(".".join(map(str, v)) for v, folder in older if folder.parent.name == legacy.PLUGIN)
    lines = []
    if names:
        lines.append(f"older locks installs remain in Claude Code's plugin cache ({names}), beside {__version__}; "
                     "after a compaction a hook can run from one of them, with that version's code and text.")
    if old:
        lines.append(f"the plugin's old name, {legacy.PLUGIN} ({old}), is still installed beside Locks {__version__}: "
                     "while it is enabled its hooks run too. Its data is Locks' now, nothing is lost by removing it: "
                     f"`claude plugin uninstall {legacy.PLUGIN}@rixmerz` (or `/plugin`).")
    free = [f for f in key if Path(f) not in held]
    if held:
        lines.append("still loaded by a session started before the update ("
                     + ", ".join(Path(f).name for f in sorted(map(str, held))) + "): its hooks run from that folder, "
                     "so removing it now fails that session's next hook (after a compaction, the prompt it "
                     "carries is lost); restart those sessions, or `/reload-plugins` in them, before removing it.")
    removal = (" Tell the person once; removing them is theirs: " + " ".join(f"rm -rf {shlex.quote(f)}" for f in free)
               if free else " Tell the person once.")
    return "locks: " + " ".join(lines) + removal


def _keep_old_command(root: Path) -> None:
    """The shim the old name's link command wrote runs the newest Locks from now on (`link.write_old`)."""
    try:
        from locks import habits, link

        link.write_old(link.default_dir(os.environ), habits.launcher())
    except Exception:  # the session starts either way
        note_error(root, "old command")


def _record_loaded(root: Path, session_id: str) -> None:
    """Keep which locks this session's hooks run, so a `locks run` typed in it can tell whether the
    session has reloaded its plugins (`link.session_loaded`). Never costs the turn."""
    try:
        from locks import __version__, link

        link.record_loaded(root, session_id, __version__)
    except Exception:  # the turn goes on
        note_error(root, "loaded plugin")


def _newer_plugin(root: Path, session_id: str, env: Mapping[str, str]) -> str | None:
    """Once per session and installed version: a session keeps the plugin it loaded, so after
    an update the commands it types run the newest locks (`link.forward_to`) while its skills,
    hooks and /locks:* text stay the old one's. Said, never acted on; it never costs the turn."""
    try:
        from locks import __version__, link

        mine = link.parse_version(__version__)
        newest = max((version for version, _ in link.installed(env)), default=None)
        if not mine or not newest or newest <= mine:
            return None
        said = ".".join(map(str, newest))
        path = root / "work" / NEWER_SAID
        try:
            seen = json.loads(path.read_text())
        except (OSError, ValueError):
            seen = {}
        seen = seen if isinstance(seen, dict) else {}
        if seen.get(session_id) == said:
            return None
        seen.pop(session_id, None)
        seen[session_id] = said
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(list(seen.items())[-NEWER_KEPT:])))
    except Exception:  # the turn goes on
        note_error(root, "newer plugin")
        return None
    return (f"locks: locks {said} is installed, and this session loaded {__version__}: the commands it types run "
            f"{said}, but its skills, hooks and /locks:* text stay {__version__}'s until it reloads. Tell the person "
            "once: /reload-plugins --force (a plain one can hold the load back), or `/locks:relay` then /clear, "
            "picks up the new one.")


def _route(store: Store, task_id: int, text: str, cwd: str | None, transcript: str | None,
           session_id: str = "") -> str | None:
    """Which way the request is best done (`locks.route`): said only when it is not the
    session itself, the default, and kept on the turn either way, to be measured. With it,
    what the last turn read with the session's own tools when that was heavy (the scribe's
    count, `route.read_advice`), kept on the same event (`read_chars`, `read_said`). Only in
    an enrolled project, with the `route` setting on; it never costs the turn."""
    if not cwd or not config.enabled("route", os.environ) or project.find(cwd) is None:
        return None
    try:
        from locks import compaction, route, scribe

        share, tokens = None, None
        if transcript:
            context = compaction.read_context(Path(transcript))
            tokens = context.tokens
            share = context.tokens / context.window if context.tokens else None
        key = repo.key(cwd)
        footprint = store.worker_footprint(key)
        chosen = route.choose(text, context_tokens=tokens, context_share=share, footprint=footprint)
        # The scribe absorbs the transcript at each Stop: at this prompt its count is the last turn's.
        read = ((scribe.load(home(), session_id) if session_id else None) or {}).get("read") or {}
        chars, calls = int(read.get("chars") or 0), int(read.get("calls") or 0)
        heavy = route.read_advice(chars, calls, store.kind_record("explore", key) if chars else None)
        store.add_event(task_id, "routed", **chosen.data(), context_tokens=tokens,
                        worker_tokens=footprint[0] if footprint else None,
                        read_chars=chars, read_calls=calls, read_said=bool(heavy))
        return "\n".join(s for s in (chosen.advice(), heavy) if s) or None
    except Exception:  # the turn goes on
        note_error(home(), "route")
        return None


def _handed_back(store: Store, event: Mapping[str, Any], agent_id: str, text: str) -> None:
    """A subagent's report is no request: it joins the task that delegated the work,
    which records the answer the session gives on it."""
    session_id = str(event["session_id"])
    tool_use = None
    if event.get("transcript_path"):
        from locks import usage

        tool_use = usage.subagent_tool_use(event["transcript_path"], agent_id)
    child = store.delegation(session_id, tool_use) if tool_use else None
    task = store.get_task(child["parent_id"]) if child and child["parent_id"] else store.last_prompt_task(session_id)
    if task is None:
        return
    store.add_message(task["id"], "subagent", text)
    store.update_task(task["id"], status="running", prompt_id=event.get("prompt_id"))


def _kick(cwd: str | None, key: str | None) -> str:
    from locks import dispatch

    return dispatch.kick(cwd, key, os.environ)


def stop(event: Mapping[str, Any], store: Store) -> dict | None:
    session_id = event.get("session_id")
    if not session_id:
        return None
    prompt_id = event.get("prompt_id")
    target = store.running_task(session_id, prompt_id) if prompt_id else store.running_task(session_id)
    message = str(event.get("last_assistant_message") or "")
    if target is not None:
        # A subagent this turn started in the background is still working: the
        # turn's answer is in, but the task is not done until its children are.
        waiting = store.children(target["id"], status=["running"])
        store.update_task(target["id"], status="running" if waiting else "done", result=message[:20000])
        if message:
            store.add_message(target["id"], "assistant", message)
    store.interrupt_running(session_id, exclude=[target["id"]] if target else [])
    if event.get("transcript_path"):
        from locks import usage

        # What this session's turns cost, by the model that served them.
        counted = {"session_id": session_id, "task_id": target["id"] if target else None,
                   "repo": target["repo"] if target else None}
        usage.ingest(store, event["transcript_path"], **counted)
        usage.ingest_subagents(store, event["transcript_path"], **counted)
    _scribe(event, home(), os.environ)
    _evolve(store, home())
    cwd = event.get("cwd")
    _landed(store, repo.toplevel(Path(cwd)) if cwd and Path(cwd).is_dir() else None, home())
    _revive(store, home(), os.environ)
    from locks import naming

    naming.maybe_start(store, session_id, home(), os.environ)
    _adopt(store, str(session_id), event.get("cwd"))
    notice = _endings(store, session_id, via="stop")
    if notice:
        # The turn would end without the session knowing its work stopped:
        # it goes on, once per ending, to tell the person and continue.
        return {"decision": "block", "reason": notice}
    _consider_compaction(event, store, target, home(), os.environ)
    return None


def _adopt(store: Store, session_id: str, cwd: str | None) -> int:
    """The orchestrator takes what sessions gone quiet left in its project, so their
    endings reach it with its own."""
    if not cwd:
        return 0
    try:
        if not orchestrator.is_it(project.find(cwd), session_id):
            return 0
        key = repo.key(cwd)
        return orchestrator.adopt(store, [key], session_id) if key else 0
    except OSError:
        return 0


def _consider_compaction(event: Mapping[str, Any], store: Store, target: dict | None, root: Path,
                         env: Mapping[str, str]) -> None:
    """The turn ended for good: is now the moment to compact (`locks.compaction`)?
    The mod reads the decision; its failure is noted and costs the turn nothing."""
    turn = target or store.last_prompt_task(str(event["session_id"]))
    if not event.get("transcript_path") or turn is None:
        return
    try:
        from locks import compaction

        compaction.consider(store, root, session_id=str(event["session_id"]),
                            transcript=Path(str(event["transcript_path"])), cwd=event.get("cwd"), task_id=turn["id"],
                            env=env)
    except Exception:  # the turn ends as it would have
        note_error(root, "Stop (compaction)")


def _compaction_seen(store: Store, root: Path, session_id: str, source: str, *, trigger: Any = None,
                     instructions: Any = None) -> None:
    """Keep a compaction the hooks saw (`locks.compaction.seen`), after the digest
    is kept or put back: its failure is noted and costs the hook nothing else."""
    try:
        from locks import compaction

        compaction.seen(store, session_id, source, trigger=trigger, instructions=instructions)
    except Exception:  # the digest and its restore never depend on this record
        note_error(root, f"{source} (compaction)")


def _scribe(event: Mapping[str, Any], root: Path, env: Mapping[str, str]) -> None:
    """The scribe keeps the session's facts as it goes (`locks.scribe`), and every few
    turns has the project's notes filed: a session that never compacts still feeds
    them. Its own failure is noted, and never costs the turn its endings."""
    if not event.get("transcript_path") or not config.enabled("scribe", env):
        return
    try:
        from locks import scribe

        state = scribe.update(root, str(event["session_id"]), str(event["transcript_path"]),
                              max_bytes=scribe.HOOK_BYTES)
        if state is not None and scribe.notes_due(state) and keep_notes(event, root, env):
            scribe.notes_started(root, state)
    except Exception:  # the turn's endings still go out
        note_error(root, "Stop (scribe)")


def _review_habits(store: Store, root: Path, env: Mapping[str, str], session_id: str, cwd: str,
                   key: str) -> None:
    """A project's new habits go to a read-only worker once (`habits.queue_review`), mined at
    most once a day per repository; its ending brings the session the install lines. Its
    failure is noted, and never costs the session its start."""
    if not (config.enabled("habit_review", env) and config.enabled("autowork", env)):
        return
    try:
        from locks import dispatch, habits

        today = datetime.now(UTC).date().isoformat()
        if store.meta(f"habits.mined:{key}") == today:
            return
        store.set_meta(f"habits.mined:{key}", today)
        habits.queue_review(store, repo=key, cwd=cwd, session_id=session_id,
                            kick=lambda d, k: dispatch.start(Path(d), root, k) or dispatch.held(root, k))
    except Exception:  # the session starts as it would have
        note_error(root, "SessionStart (habits)")


def _revive(store: Store, root: Path, env: Mapping[str, str]) -> None:
    """A worker that died is noticed and resumed (`dispatch.recover`), and a lane with queued
    work and no dispatcher (one died on a full disk, or was killed) gets one again
    (`dispatch.revive`). Its failure is noted, and never costs the turn."""
    try:
        from locks import dispatch

        dispatch.recover(store, root, env)
        dispatch.revive(store, root, env)
    except Exception:  # the turn goes on
        note_error(root, "revive")


def _landed(store: Store, top: Path | None, root: Path) -> None:
    """Stopped tasks of this checkout whose kept work is already on its HEAD leave the
    board (`stops.landed`). Its failure is noted, and never costs the turn."""
    if top is None:
        return
    try:
        stops.landed(store, top)
    except Exception:  # the turn goes on
        note_error(root, "landed")


def _evolve(store: Store, root: Path) -> None:
    """What ended settles the standards and, on a new day, the evolution history
    (`locks.evolution`). Its failure is noted, and never costs the turn its endings."""
    try:
        from locks import evolution

        evolution.settle(store)
    except Exception:  # the turn's endings still go out
        note_error(root, "Stop (evolution)")


def _relay(store: Store, root: Path, session_id: str, cwd: str, transcript: str | None) -> str | None:
    """A session started where a person handed one over (`locks relay`): it starts
    from that session's thread, and the work that session sent reports here now."""
    from locks import compact, scribe

    source = scribe.take_relay(root, session_id, cwd)
    state = scribe.load(root, source) if source else None
    if source is None or state is None:
        return None
    if state.get("transcript"):
        state = scribe.update(root, source, state["transcript"]) or state
    moved = store.hand_over(source, session_id)
    folder = project.find(cwd)
    if orchestrator.is_it(folder, source):  # the role goes with the thread
        with contextlib.suppress(OSError):
            orchestrator.take(store, folder, session_id, cwd, via="relay", force=True)
    tasks = store.list_tasks(session_id=session_id, status=OPEN_FOR_A_RELAY, source=("locks", "queue"),
                             limit=15)
    text = scribe.relay_text(state, tasks=tasks, changed=compact._changed(Path(cwd)),
                             commits=scribe.commits_since(state.get("cwd") or cwd, state.get("started")))
    if transcript:
        scribe.save(root, scribe.fresh(session_id, Path(transcript)) | {"relayed_from": source})
    if moved:
        text += (f"\n\n{moved} locks task(s) that session sent (open, waiting on the person, or ended unheard) "
                 "report to this session now.")
    return text or None


def _orchestrates(store: Store, session_id: str, cwd: str) -> str | None:
    """At a session's start: whether it orchestrates this project (it is the one the
    project names, resumed; or it starts where that one ended), the work it takes
    from sessions gone quiet, and what the role asks of it."""
    folder = project.find(cwd)
    try:
        holds, moved = orchestrator.started(store, folder, session_id, cwd)
        if not holds and not orchestrator.is_it(folder, session_id):
            return None
        key = repo.key(cwd)
        moved += orchestrator.adopt(store, [key], session_id) if key else 0
    except OSError:
        return None
    text = ("locks: this session orchestrates this project, and every stop is yours to carry. What only the "
            "person can grant (a refused command's rule, a cell's approval, a merge) you ask them for in this "
            "conversation and, on their yes, run its line; every other stop you decide: resume it, rewrite it, "
            "or dismiss it with why (`locks dismiss N --why '<why>'`). Nothing is left waiting on the person unasked.")
    if moved:
        text += f" {moved} task(s) sent by a session that ended or went quiet report here now."
    return text


def _enroll(cwd: str) -> None:
    """Using locks in a project enrolls it: it gets its `.locks/` folder."""
    with contextlib.suppress(OSError, project.NotAProject):
        project.enroll(cwd)


def pre_tool_use(event: Mapping[str, Any], store: Store) -> dict | None:
    """A delegation to a subagent becomes a child of the turn that made it."""
    if event.get("tool_name") not in DELEGATION_TOOLS or not event.get("session_id"):
        return None
    parent = store.running_task(event["session_id"])
    tool_input = event.get("tool_input") or {}
    body = str(tool_input.get("prompt") or tool_input.get("description") or "delegated work")
    store.create_task(body, status="running", source="delegation", session_id=event["session_id"],
                      author="orchestrator", parent_id=parent["id"] if parent else None, cwd=event.get("cwd"),
                      title=str(tool_input.get("description") or "") or None,
                      repo=parent["repo"] if parent else None, prompt_id=event.get("tool_use_id"))
    return None


def post_tool_use(event: Mapping[str, Any], store: Store) -> dict | None:
    """A delegation that returned closes its child task, and the turn too when it
    was the last child. (Every tool call's signature is logged before this, by the
    fast path in `locks.__main__`.)"""
    if not event.get("session_id") or event.get("tool_name") not in DELEGATION_TOOLS:
        return None
    child = store.delegation(event["session_id"], event.get("tool_use_id"))
    if child is None:
        return None
    if (event.get("tool_input") or {}).get("run_in_background"):
        # The call returned at launch; the work ends when its notification arrives.
        return None
    response = event.get("tool_response")
    text = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)[:20000]
    _finish_child(store, child, "done", text)
    return None


def _finish_child(store: Store, child: dict, status: str, result: str) -> None:
    store.update_task(child["id"], status=status, result=result)
    parent = store.get_task(child["parent_id"]) if child["parent_id"] else None
    if (parent and parent["status"] == "running" and parent["result"]
            and not store.children(parent["id"], status=["running"])):
        store.update_task(parent["id"], status="done")


_NOTE_TAG = re.compile(r"<(tool-use-id|status|summary|result)>(.*?)</\1>", re.DOTALL)


def _background_child_finished(session_id: str, text: str, store: Store) -> None:
    fields = {k: v.strip() for k, v in _NOTE_TAG.findall(text)}
    child = store.delegation(session_id, fields.get("tool-use-id")) if fields.get("tool-use-id") else None
    if child is None or child["status"] != "running":
        return
    status = "done" if fields.get("status", "completed") == "completed" else "failed"
    _finish_child(store, child, status, fields.get("result") or fields.get("summary") or "")


def stop_failure(event: Mapping[str, Any], store: Store) -> dict | None:
    session_id = event.get("session_id")
    if not session_id:
        return None
    target = store.running_task(session_id, event.get("prompt_id")) or store.running_task(session_id)
    if target is not None:
        reason = str(event.get("error") or event.get("reason") or "the turn failed")
        store.update_task(target["id"], status="failed", result=reason[:2000])
    return None


def _shed_when_low(store: Store, root: Path) -> str | None:
    """On a disk under `capacity.LOW_DISK_MB` free, the rebuilt folders of idle worktrees go
    (`flow.shed`) and the session hears how much came back: a full disk kills workers mid-task."""
    from locks import capacity, flow

    m = capacity.machine(root)
    if m.disk_mb is None or m.disk_mb >= capacity.LOW_DISK_MB:
        return None
    freed = flow.shed(store)
    said = f"locks: free disk is {m.disk_mb} MB"
    if freed:
        return f"{said}; removed {freed // capacity.MB} MB of build output from worktrees of tasks no run holds."
    return f"{said} and nothing locks keeps could go: free space before sending work."


def session_start(
    event: Mapping[str, Any],
    store: Store,
    root: Path,
    env: Mapping[str, str] | None = None,
    adapters: list | None = None,
) -> dict | None:
    session_id = event.get("session_id")
    if not session_id:
        return None
    env = env or {}
    config.sync_plugin_options(env)
    from locks import flow  # imported here: it pulls in the orchestrator

    flow.sweep(store)
    cwd = event.get("cwd")
    # The branches of this checkout whose work landed since: merged ones go, before anything
    # is resumed on its own: a death whose work is already in HEAD is no work to resume.
    top = repo.toplevel(Path(cwd)) if cwd and Path(cwd).is_dir() else None
    if top is not None:
        _landed(store, top, root)
        flow.prune(store, [top])
    _revive(store, root, {**os.environ, **env})
    key = repo.key(cwd) if cwd else None
    shed_line = _shed_when_low(store, root)
    store.touch_session(session_id, cwd, key)
    blocks: list[str] = []
    _export(env, "LOCKS_SESSION_ID", str(session_id))
    _record_loaded(root, str(session_id))
    reach = _reach(env)
    if reach:
        blocks.append(reach)
    errors = _drain_errors(root)
    if errors:
        blocks.append(f"locks: {errors} hook error(s) since the last session were swallowed so the session "
                      f"could go on; details in {root / ERROR_LOG}.")
    if shed_line:
        blocks.append(shed_line)
    registry = _registry_notices(root)
    if registry:
        blocks.append(registry)
    if older := _older_installs(root, os.environ):
        blocks.append(older)
    _keep_old_command(root)
    if event.get("source") in ("resume", "compact"):
        # A turn or a subagent whose ending never arrived has nothing to resume: not unfinished work.
        unfinished = [t for t in store.list_tasks(session_id=session_id, status=("running", "interrupted"), limit=30)
                      if not (t.get("source") == "hook" and _ignored(str(t.get("body") or "")))
                      and (t["status"] == "running" or getattr(stops.of(store, t), "cause", None) != "turn")][:10]
        if unfinished:
            blocks.append("locks: unfinished tasks in this session (context only, do not restart them unasked):\n"
                          + "\n".join(f"#{t['id']} [{t['status']}] {t['title']}" for t in unfinished))
        stopped = store.list_tasks(session_id=session_id, status=STOPPED_FOR_A_PERSON, limit=10,
                                   source=("locks",))
        if stopped:
            blocks.append(_stopped_text(store, stopped))
    if event.get("source") == "compact":
        from locks import compact

        kept = compact.recall(root, str(session_id))
        if kept:
            blocks.append(kept)
        _compaction_seen(store, root, str(session_id), "SessionStart")
    if event.get("source") in ("startup", "clear") and cwd and config.enabled("scribe", {**os.environ, **env}):
        relayed = _relay(store, root, str(session_id), str(cwd), event.get("transcript_path"))
        if relayed:
            blocks.append(relayed)
    if cwd:
        role = _orchestrates(store, str(session_id), str(cwd))
        if role:
            blocks.append(role)
    if event.get("source") == "startup":
        from locks import scribe

        scribe.prune(root)
    # Only a project that uses locks: indexing writes `.mcp-docs/` into the
    # checkout, and a session opened anywhere else is none of locks' business.
    if cwd and repo.toplevel(Path(cwd)) is not None and project.find(cwd) is not None:
        blocks += _freshen(Path(cwd), root, env, adapters)
        if key:
            _review_habits(store, root, {**os.environ, **env}, str(session_id), str(cwd), key)
    if key:
        from locks import cells

        dead = cells.recent_shared(store, repo=key, limit=5)
        if dead:
            blocks.append(_dead_end_text(dead, "locks memory: recent fixes in this repository that did not work, "
                                               "met across areas."))
    if key:
        # What the store says is stuck now in this repository: a state locks should never reach.
        from locks import doctor

        broken = [f for f in doctor.check(store, repo=key, days=1) if f.severity == doctor.BROKEN]
        if broken:
            blocks.append("locks doctor: work in this repository is stuck in a state locks should never reach; "
                          "tell the person, and run each line only on their word:\n"
                          + "\n".join(f"- {f.what} → {f.fix}" for f in broken[:5]))
    if key and cwd and project.find(cwd) is not None and config.enabled("notes", {**os.environ, **env}):
        # After a compaction above all: what the project knows is read back from
        # its notes, not rebuilt from the code. The index, not the notes: a
        # session asks for what it needs with `locks recall`.
        from locks import notes

        notes.review(store, key, Path(cwd))
        index = notes.index_text(store, key)
        if index:
            blocks.append(index)
    return _context("SessionStart", "\n\n".join(blocks)) if blocks else None


def keep_digest(event: Mapping[str, Any], store: Store, root: Path) -> Path | None:
    """PreCompact: write the facts of the session (`locks.compact`) that the
    SessionStart after the compaction puts back. Anywhere, notes setting or not:
    it is this session's own memory, not a project's."""
    session_id, transcript, cwd = event.get("session_id"), event.get("transcript_path"), event.get("cwd")
    if not (session_id and transcript):
        return None
    from locks import compact

    tasks = store.list_tasks(session_id=str(session_id), limit=15)
    return compact.keep(root, str(session_id), Path(str(transcript)), Path(str(cwd)) if cwd else None, tasks)


def keep_notes(event: Mapping[str, Any], root: Path, env: Mapping[str, str], *,
               popen: Callable[..., Any] = subprocess.Popen) -> bool:
    """PreCompact and SessionEnd: start a detached `locks extract-notes`, so what the
    conversation established is in the project's notes before it is summarized
    away or closed. A model call takes longer than a hook may; one at a time per
    session. Only in an enrolled project, with the `notes` setting on."""
    session_id, transcript, cwd = event.get("session_id"), event.get("transcript_path"), event.get("cwd")
    if not (session_id and transcript and cwd) or not config.enabled("notes", env):
        return False
    if project.find(cwd) is None or not Path(str(transcript)).is_file():
        return False
    from locks import dispatch

    if dispatch.held(root, f"notes:{session_id}"):
        return False
    try:
        (root / "work").mkdir(parents=True, exist_ok=True)
        with open(root / "work" / "notes.log", "a") as out:
            popen([sys.executable, "-m", "locks", "extract-notes", str(session_id), str(transcript), str(cwd)],
                  cwd=str(cwd), env=dispatch.child_env(env), stdin=subprocess.DEVNULL, stdout=out,
                  stderr=subprocess.STDOUT, start_new_session=True)
    except OSError:
        return False
    return True


#: Endings of locks runs that wait on a person to clear something.
STOPPED_FOR_A_PERSON = ("blocked", "needs_approval")


def _endings(store: Store, session_id: str, *, via: str) -> str | None:
    """Work this session sent that ended since it last heard, each with what to do
    next, marked as reported: a queued task finishes while nobody waits on it,
    and the session that sent it would otherwise never learn it should act. A loop
    this session started that stopped is told here too, once (`locks.loops`)."""
    told = endings(store, session_id, claim=via)[0]
    try:
        copied = copies(store, session_id, claim=via)[0]
    except Exception:  # a copy that cannot be read never costs the session its own endings
        note_error(home(), "copies")
        copied = None
    if copied:
        told = f"{told}\n\n{copied}" if told else copied
    try:
        from locks import loops

        stopped = loops.endings(session_id)
    except Exception:  # a loop that cannot be read never costs the session its endings
        stopped = []
    if not stopped:
        return told
    loop_text = ("A loop you started stopped. Tell the person what it did and what waits on them "
                 "(`locks loop show <name>`):\n" + "\n".join(stopped))
    return f"{told}\n\n{loop_text}" if told else loop_text


#: The pauses a session was told of, kept in the store's meta per session and lane.
PAUSED_TOLD = "paused-told:"


def _paused_lanes(store: Store, session_id: str, *, said: Sequence[int] = ()) -> str | None:
    """The lanes paused with work this session queued, each told once per pause: a
    pause outlives the ending that caused it, and work queued into a paused lane
    later (a workflow's next step, another run) would otherwise wait unseen. A pause
    on a task whose ending (`said`) is told with it counts as told: that ending says it."""
    lines = []
    for lane in store.paused_with_work(session_id):
        key = f"{PAUSED_TOLD}{session_id}:{lane['repo']}"
        # Keyed on the reason, never the time: a sweep that pauses the lane again on the
        # same stop is no new pause, and a turn blocked on each would be noise.
        if store.meta(key) == lane["reason"]:
            continue
        store.set_meta(key, str(lane["reason"]))
        if any(re.match(rf"task #{n}\b", str(lane["reason"] or "")) for n in said):
            continue
        lines.append(f"  {lane['repo']}: {lane['queued']} queued task(s) of yours wait, paused since "
                     f"{lane['updated_at']}: {clip(str(lane['reason'] or ''), 200, one_line=True)}")
    if not lines:
        return None
    return ("locks: a lane holding work you queued is paused, so nothing in it starts. Tell the person; settle "
            "the task it paused on (resume or dismiss it), or, when the queued work does not depend on it, run "
            "`locks lanes --unpause <repo>`:\n" + "\n".join(lines))


def habit_lines(task: Mapping[str, Any], store: Store | None = None) -> list[str]:
    """A habits review's outcome (`habits.queue_review`): what locks installed after its own
    check (`habits.apply_reviews`) and what it refused, each with why; else the install lines,
    built by locks with the command quoted, for the person (with `habit_auto` off)."""
    from locks import habits

    try:
        reviewed = json.loads(task.get("options") or "{}").get("habit_review")
    except (TypeError, ValueError):
        return []
    if not reviewed or task.get("status") != "done":
        return []
    data = task.get("finished_data") or {}
    if isinstance(data, str):
        data = json.loads(data or "{}")
    texts = ([str(c.get("evidence") or "") for c in (data.get("verdict") or {}).get("criteria") or ()]
             + [str(task.get("result") or "")])
    if store is not None and task.get("id"):
        texts = habits.review_texts(store, task, data)
    installs, skips = habits.review_verdicts(texts, reviewed)
    skipped = [f"skip {cid}: {why}" for cid, why in skips.items()]
    unanswered = [cid for cid in reviewed if cid not in installs and cid not in skips]
    tail = [f"    not answered: {', '.join(unanswered)} (`locks habits` lists them)"] if unanswered else []
    if store is not None and task.get("id"):
        try:  # the review is applied before it is told, whichever way its ending is delivered
            habits.apply_reviews(store, home(), task_ids=[int(task["id"])])
        except Exception:  # its ending still goes out, with the lines for the person
            note_error(home(), "endings (habits)")
    applied = [e for e in (store.events(task_id=int(task["id"]), limit=500) if store and task.get("id") else ())
               if e["kind"] in ("habit_installed", "habit_refused")]
    if applied:
        lines = ["  habits review: locks checked each proposal itself (one simple command, passing at HEAD, "
                 "fast, writing nothing). Tell the person:"]
        for e in applied:
            d = e["data"]
            took = f", {d['seconds']} s" if d.get("seconds") is not None else ""
            lines.append(f"    installed habit #{d['habit']} ({d['candidate']}): `{d['command']}` after each edit"
                         f"{took}; `locks habits uninstall {d['habit']}` removes it" if e["kind"] == "habit_installed"
                         else f"    not installed {d['candidate']}: {d['why']}"
                         + (f" (proposed `{d['command']}`)" if d.get("command") else ""))
        return [*lines, *(f"    {line}" for line in skipped), *tail]
    if not installs:
        return [f"  habits review: nothing worth a hook ({len(skips)} skipped)"
                + (": " + "; ".join(skipped) if skipped else ""), *tail]
    return ["  habits review: ask the person whether to install each (it writes this repository's "
            ".claude/settings.local.json); on a yes run its line:",
            *(f"    locks habits install {cid} --command {shlex.quote(cmd)}" for cid, cmd in installs.items()),
            *(f"    {line}" for line in skipped), *tail]


def endings(store: Store, session_id: str, *, claim: str | None = None) -> tuple[str | None, list[int]]:
    """The notice of what ended since the session last heard, and the tasks it
    covers. With `claim`, they are marked delivered by that route as they are
    read, atomically; without, only read."""
    ended = store.claim_unreported(session_id, via=claim) if claim else store.unreported(session_id)
    paused = _paused_lanes(store, str(session_id), said=[t["id"] for t in ended]) if claim else None
    if not ended:
        return paused, []
    lines = [ENDINGS_HEADER + " Act on each now: tell the person what happened. Every stop is yours to carry: "
             "one only the person can grant (a refused command's rule, a cell's approval) you ask them for here, "
             "and on their yes you run its line; any other you decide as its line says: resume it, rewrite it, "
             "or dismiss it with why (`locks dismiss N --why '<why>'`). Nothing waits on the person unasked "
             "(never redo a worker's task by hand)."]
    # Many endings at once: each pass in one line, so the notice costs the session little.
    grouped = len(ended) >= ENDINGS_GROUPED
    if grouped:
        passed = sum(1 for t in ended if t["status"] == "done")
        lines.append(f"{len(ended)} ended: {passed} passed, {len(ended) - passed} stopped; each pass in one line "
                     "(`locks show N` has its verdict in full).")
    for t in ended:
        data = t["finished_data"]
        changed = data.get("changed") or []
        total = data.get("changed_n") or len(changed)
        stop = stops.view(store, t) if t["status"] != "done" else None
        decided = bool(stop) and stop["cause"] == "decision"
        # Work done that waits on one decision is no failure, and its header never reads as one.
        lines.append(f"#{t['id']} [{'work done, one decision open' if decided else t['status']}] {t['title']}")
        # A security problem the worker named goes first, on a line of its own, secrets masked.
        lines += [f"  ⚠ security: {alert}" for alert in data.get("alerts") or ()]
        # Said when it started, to a caller that may be gone by now: a queued step's above all.
        lines += [f"  it ran with a warning: {warning}" for warning in _warned(store, t["id"])]
        lines += _went_past(store, t)
        lines += _behind(store, t)
        lines += _found_before(store, t)
        lines += specs.ending_lines(t, changed, data.get("specs"), touched_elsewhere=_run_touched_specs(store, t),
                                    after=f"run:{t['workflow_run']}" if t.get("workflow_run") else f"task:{t['id']}")
        lines += _overlaps(store, t)
        lines += habit_lines(t, store)
        if t["status"] == "done" and grouped:
            branch = data.get("branch")
            lines.append(f"  passed at {data.get('final_cell') or t['final_cell']}"
                         + (f", on branch {branch}" if branch else "") + f"; {verdict_summary(data.get('verdict'))}"
                         + (f"; changed {total} file(s)" if total else ""))
            continue
        if t["status"] == "done":
            branch = data.get("branch")
            commit = f" (commit {str(data['commit'])[:12]})" if data.get("commit") else ""
            # In the checkout only when the run had no worktree; an older ending did not say.
            in_place = data.get("in_place")
            where = (f"; its work is on branch {branch}{commit}, merged only on the person's word"
                     if branch else f"; its changes are in your checkouts of {', '.join(in_place)}, none of them in a "
                     "worktree (the run started in a folder that holds several repositories)"
                     if changed and not data.get("isolated") and in_place
                     else "; its changes are in the checkout (the run had no worktree)"
                     if changed and not data.get("isolated") else "; no branch kept its changes"
                     if changed else "; it changed nothing")
            lines.append(f"  passed at {data.get('final_cell') or t['final_cell']}{where}")
            if t.get("result"):
                lines.append(f"  the worker says: {clip(str(t['result']), 800)}")
            if store.last_event(t["id"], "report") is not None:
                lines.append(f"  its full answer is kept on the task: `locks show {t['id']}` prints it whole")
            if changed:
                lines.append("  changed: " + ", ".join(changed[:8])
                             + (f" and {total - 8} more" if total > 8 else ""))
            lines += verdict_lines(data.get("verdict"))
            if flaky := store.last_event(t["id"], "check_flaky"):
                found = flaky["data"].get("failures") or []
                lines.append(f"  flaky: `{flaky['data'].get('command')}` failed {len(found)} test(s) once, and they "
                             "passed when it ran again: " + clip("; ".join(found), 300, one_line=True)
                             + " (not this work's; kept per repository)")
            continue
        if decided:
            branch = data.get("branch")
            commit = f" (commit {str(data['commit'])[:12]})" if data.get("commit") else ""
            checked = {"locks": "its check passed, run by locks",
                       "worker": "its own check passed, not re-run by locks"}.get(str(stop.get("checked") or ""),
                                                                                 "no check locks ran passed")
            lines.append(f"  the work is done and kept on branch {branch}{commit}, merged only on the person's word "
                         f"({checked})" if branch else f"  the work is done and in the checkout ({checked})")
            said = stop.get("account") or stop["reason"]
            lines.append(clip(f"  one point needs a decision; the worker says: {said}", 900))
        elif stop:
            lines.append(f"  stopped by {stop['who']}: {clip(stop['reason'], 300)}")
            if stop.get("account"):
                lines.append(f"  the worker found: {clip(stop['account'], 800)}")
        if changed:
            shown = ", ".join(changed[:5]) + (f" and {total - 5} more" if total > 5 else "")
            lines.append(f"  changed so far: {shown}")
        if not decided:
            lines += _failed_check(store, t)
        if stop:
            whose = "ask the person for it here" if stops.waits_on(store, t) == "person" else "yours to decide"
            lines.append(f"  next ({whose}): {stop['todo']}" + (f": {stop['next']}" if stop["next"] else ""))
        if held := after.behind(store, {"task": int(t["id"])}):
            # Queued after this work: it starts only when a resume of this passes.
            lines.append(f"  {len(held)} queued task(s) wait for it to pass: "
                         + ", ".join(f"#{h['id']} {clip(str(h['title'] or ''), 60, one_line=True)}"
                                     for h in held[:4])
                         + "; a resume of it to a pass releases them, and dismissing it takes them off the queue")
        waiting = store.lane_paused_on(t.get("repo"), t["id"])
        if waiting:
            lines.append(f"  its lane is paused on it: {waiting} queued task(s) wait. Dismissing or resuming it "
                         "opens the lane; when the queued work does not depend on it, tell the person and "
                         f"run `locks lanes --unpause {t.get('repo')}` to let them start now")
    if (split := session_split(store, str(session_id))) is not None:
        lines.append(split)
    if paused:
        lines += ["", paused]
    return "\n".join(lines), [t["id"] for t in ended]


COPIES_HEADER = ("locks: work another session sent for you has ended (a copy: that session heard it too and acts "
                 "on it; decide with it, through SendMessage, never with a resume or dismissal of your own unless "
                 "the person asked).")


def copies(store: Store, session_id: str, *, claim: str | None = None) -> tuple[str | None, list[int]]:
    """Endings of work other sessions sent with `--notify` naming this one, in a notice of
    their own: one line each, its verdict or stop, and which session owns it."""
    got = store.claim_copies(session_id, via=claim) if claim else store.unreported_copies(session_id)
    if not got:
        return None, []
    lines = [COPIES_HEADER]
    for t in got:
        stop = stops.view(store, t) if t["status"] != "done" else None
        why = f": {stop['reason']}" if stop and stop.get("reason") else ""
        lines.append(f"#{t['id']} [{t['status']}] {t['title']} — sent by session {t.get('session_id') or '?'}"
                     f" in {t.get('repo') or '?'}{why}")
    return "\n".join(lines), [t["id"] for t in got]


def session_split(store: Store, session_id: str) -> str | None:
    """What this session cost so far, its own turns apart from the work it sent locks (issue #118):
    so it can tell when delegating paid. Observed from the usage it ingested and the tasks' costs;
    None when neither is known. Never fails an ending."""
    try:
        from locks import usage

        own = 0.0
        for model, inp, out, read, write in store._conn.execute(
                "SELECT model, SUM(input_tokens), SUM(output_tokens), SUM(cache_read), SUM(cache_write) "
                "FROM usage WHERE session_id = ? AND sidechain = 0 GROUP BY model", (session_id,)):
            own += usage.dollars(model or "", inp or 0, out or 0, cache_read=read or 0, cache_write=write or 0)
        (sent,) = store._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM tasks WHERE session_id = ? AND source IN ('locks', 'queue')",
            (session_id,)).fetchone()
    except Exception:
        return None
    if not own and not sent:
        return None
    whole = own + float(sent or 0)
    return (f"This session so far: ${own:.2f} its own turns, ${float(sent or 0):.2f} the work it sent locks "
            f"({own / whole:.0%} the session's own).")


#: This many endings at once are told in short: each pass in one line.
ENDINGS_GROUPED = 4
#: How much of a failed check's own output an ending shows (`locks show N` has all that was kept).
CHECK_TAIL_SHOWN = 1500


def _failed_check(store: Store, task: Mapping) -> list[str]:
    """What locks' check said on the last attempt of a task that ended short of a pass: the
    failing tests and the last lines of its output, so nobody re-runs the whole check by hand
    to learn which test failed. Only an attempt of the run that just ended: an earlier run's
    is not why this one stopped."""
    last = store.last_event(task["id"], "attempt_finished")
    planned = store.last_event(task["id"], "planned")
    tail = str(((last or {}).get("data") or {}).get("check_tail") or "").strip()
    if not tail or (planned and planned["id"] > last["id"]):
        return []
    return ["  the check's own output on its last attempt (`locks show "
            f"{task['id']}` has it under the attempt):", *(f"    {line}".rstrip() for line in
                                                           clip(tail, CHECK_TAIL_SHOWN).splitlines())]


def verdict_summary(verdict: Mapping | None) -> str:
    """A verdict in a few words: the check, the criteria met, the independent check."""
    if not isinstance(verdict, Mapping):
        return "no verdict kept"
    check = verdict.get("check")
    said = [f"`{check['command']}` " + ("failing at base, unchanged" if check.get("state") else "passed")
            if isinstance(check, Mapping) else "no check ran"]
    found = list(verdict.get("criteria") or ())
    if found:
        said.append(f"{sum(1 for c in found if c.get('met'))}/{len(found)} criteria met")
    ind = verdict.get("independent") or {}
    if ind.get("ran"):
        said.append("independent check "
                    + ("passed" if ind.get("passed") else f"left {ind.get('blocking')} blocker(s)"))
    elif ind.get("sampled"):
        said.append(f"independent check not drawn (1 pass in {ind.get('every')} here)")
    return ", ".join(said)


def verdict_lines(verdict: Mapping | None) -> list[str]:
    """The verdict a passing ending carries (`orchestrate.verdict_card`), for the session that
    sent the work: what it was held to and how that was checked, so it reports the result
    instead of re-reading the code or running the tests again. A verdict locks did not keep
    (an older ending) says nothing."""
    if not isinstance(verdict, Mapping):
        return []
    lines = []
    check = verdict.get("check")
    if isinstance(check, Mapping) and check.get("state"):
        from locks import isolate

        lines.append(f"  check: `{check['command']}` {check['state']}: it fails at the base "
                     f"{isolate.base_label(str(check.get('base') or ''))} too, with the same {check.get('failures')} "
                     "failure(s), so the work stood; they were there before it")
    elif isinstance(check, Mapping):
        runs = int(check.get("runs") or 1)
        lines.append(f"  check: `{check['command']}` passed, run by locks in its worktree"
                     + (f" {runs} times, every run, since this work fixes a flake" if runs > 1 else ""))
    else:
        lines.append("  check: none was given, so nothing ran it: say so to the person")
    for c in verdict.get("criteria") or ():
        mark = "met" if c.get("met") else "NOT met"
        lines.append(f"  {c.get('id')} {mark}: {c.get('evidence') or 'no evidence given'}")
    ind = verdict.get("independent") or {}
    if ind.get("ran"):
        lines.append("  independent check: " + ("passed" if ind.get("passed") else
                                                 f"{ind.get('blocking')} blocker(s) left, recorded"))
        if ind.get("unchecked"):
            lines.append(f"  TRACER CORRECT lines nobody checked: {', '.join(ind['unchecked'])}")
    elif ind.get("late") or ind.get("sampled"):
        lines.append(f"  independent check: {ind['why']}")
    elif ind.get("why"):
        lines.append(f"  independent check: not run ({ind['why']})")
    if verdict.get("unchecked"):
        lines.append(f"  not self-checked: {verdict['unchecked']}")
    unverified = not isinstance(check, Mapping) or bool(verdict.get("unchecked")) or bool(ind.get("unchecked")) or any(
        not c.get("met") for c in verdict.get("criteria") or ())
    lines.append("  this verdict is locks' own record: do not re-read the code or re-run the check to confirm it"
                 + ("; open the diff only where it says something was not checked or not met" if unverified
                    else "; open the diff only if the person asks"))
    return lines


#: The most problems that were already there an ending lists.
FOUND_BEFORE_SHOWN = 3


def _failing_at_base(data: Mapping) -> dict[str, str]:
    """A `check_preexisting` event as a problem that was already there: where (the base) and
    what (the command and its first failing lines), for the bugfix an ending offers."""
    from locks import isolate

    failures = [str(x) for x in data.get("failures") or ()]
    more = int(data.get("n") or len(failures)) - 3
    what = f"`{data.get('command')}` fails at the base too: " + "; ".join(failures[:3]) + (
        f"; and {more} more" if more > 0 else "")
    return {"where": f"base {isolate.base_label(str(data.get('base') or ''))}", "what": what}


def _found_before(store: Store, ended: Mapping) -> list[str]:
    """Problems the work found that were already there, shown with proof: never this change's
    to fix, never a blocker, and so never fixed unless somebody starts it. The ending names
    them and the bugfix that would take one on, after this work, for the orchestrator to
    decide; nothing is started."""
    tasks = store.run_tasks(int(ended["workflow_run"])) if ended.get("workflow_run") else [ended]
    found: dict[str, Mapping] = {}
    # What the work changed, every step of a run: a problem in one of those files was most
    # likely fixed by it (a test that caught a bug, then the fix), never work left to offer.
    changed = {str(p).lower() for task in tasks
               for p in (((store.final_event(task["id"]) or {}).get("data") or {}).get("changed") or ())}
    # What changed since the work started, on the checkout's branch or by other passing work
    # here: a problem in one of those files may be fixed already, so it is counted, not offered.
    moved = _moved_since(store, ended, tasks)
    fixed: set[str] = set()
    since: set[str] = set()
    for task in tasks:
        final = store.final_event(task["id"]) if task.get("status") == "done" else None
        for f in ((final or {}).get("data") or {}).get("findings") or ():
            if isinstance(f, Mapping) and f.get("cause") == "pre-existing" and f.get("evidence_class") == \
                    "deterministic":
                place = findings_mod.place(str(f.get("where") or ""))
                if f.get("fixed") or place in changed:
                    fixed.add(findings_mod.key(f))
                elif place in moved:
                    since.add(findings_mod.key(f))
                else:
                    found.setdefault(findings_mod.key(f), f)
        # A check that failed only as it does at the base: its failures are problems that were
        # already there too, shown by locks' own run of the check on both commits.
        # Only when the passing attempt's own check was that one (its verdict says so): an earlier
        # attempt's is not what the work ended with.
        check = (((final or {}).get("data") or {}).get("verdict") or {}).get("check")
        kept = store.last_event(task["id"], "check_preexisting") \
            if isinstance(check, Mapping) and check.get("state") else None
        if kept is not None and not int(kept["data"].get("added") or 0):
            found.setdefault(f"check:{kept['data'].get('command')}:{kept['data'].get('base')}",
                             _failing_at_base(kept["data"]))
    fixed -= set(found)
    since -= set(found) | fixed
    later = [f"    and {len(since)} more in files changed since this work started, on the checkout's branch or by "
             "other passing work here: they may be fixed already, so none is offered"] if since else []
    if not found:
        if not since:
            return [f"  it found {len(fixed)} problem(s) that were already there, each in a file this work changed "
                    "or marked fixed by it: nothing left to offer"] if fixed else []
        return [f"  it found {len(fixed) + len(since)} problem(s) that were already there: nothing left to offer"] + (
            [f"    {len(fixed)} in files this work changed or marked fixed by it"] if fixed else []) + later
    after_ref = f"run:{ended['workflow_run']}" if ended.get("workflow_run") else f"task:{ended['id']}"
    shown = list(found.values())[:FOUND_BEFORE_SHOWN]
    lines = [f"  it found {len(found)} problem(s) that were already there, not this change's (each shown with "
             "its proof); a bugfix for one is yours to decide, nothing was started:"]
    for f in shown:
        # A finding may quote what it found: nothing that looks like a secret is repeated.
        what = clip(_SECRET.sub("[secret]", str(f.get("what") or "")), 200, one_line=True)
        where = str(f.get("where") or "")
        lines.append(f"    {where}: {what} — `locks workflow run bugfix {shlex.quote(f'{what} ({where})')} "
                     f"--after {after_ref}`")
    if len(found) > len(shown):
        lines.append(f"    and {len(found) - len(shown)} more: `locks show {ended['id']}`")
    if fixed:
        lines.append(f"    and {len(fixed)} more in files this work changed or marked fixed by it: not offered")
    return lines + later


def _moved_since(store: Store, ended: Mapping, tasks: Sequence[Mapping]) -> set[str]:
    """Files (lowercased) changed since the ended work started: on the checkout's branch since
    each task's base, and by other work here that passed after it started (within `OVERLAP_DAYS`). Empty when
    git cannot say; an ending never fails on it."""
    moved: set[str] = set()
    cwd = Path(str(ended.get("cwd") or ""))
    if ended.get("cwd") and cwd.is_dir():
        from locks import isolate

        top = repo.toplevel(cwd) or cwd
        for task in tasks:
            base = (((store.final_event(task["id"]) or {}).get("data")) or {}).get("base")
            try:
                moved |= {p.lower() for p in isolate.moved_since(top, str(base or "")) or ()}
            except Exception:  # noqa: S112 - an ending never fails on what git could not say
                continue
    if ended.get("repo"):
        since = max((datetime.now(UTC) - timedelta(days=OVERLAP_DAYS)).isoformat(timespec="seconds"),
                    min(str(t.get("created_at") or "") for t in tasks))
        mine = {t["id"] for t in tasks}
        for other in store.list_tasks(repo=ended["repo"], source=("locks", "queue"), limit=300, status=["done"]):
            if other["id"] in mine or (other.get("updated_at") or "") < since or (
                    ended.get("workflow_run") and other.get("workflow_run") == ended.get("workflow_run")):
                continue
            data = ((store.final_event(other["id"]) or {}).get("data")) or {}
            moved |= {str(p).lower() for p in data.get("changed") or ()}
    return moved


#: How long work that stopped or passed beside an ending still counts as work to merge beside it.
OVERLAP_DAYS = 3
#: The most other tasks, and files of each, an ending names.
OVERLAP_SHOWN = 4


def _overlaps(store: Store, ended: Mapping) -> list[str]:
    """Other work in the same repository that changed files this work changed: a task running
    beside it, or one whose kept branch still waits to be merged. Merging both conflicts there,
    so the ending names each and the files, before the person meets it in a merge. Steps of
    one workflow run build on each other and are never named; nothing is merged or changed."""
    mine = set((ended.get("finished_data") or {}).get("changed") or ())
    if not mine or not ended.get("repo") or not ended.get("cwd") or not Path(str(ended["cwd"])).is_dir():
        return []
    from locks import isolate

    since = (datetime.now(UTC) - timedelta(days=OVERLAP_DAYS)).isoformat(timespec="seconds")
    top = repo.toplevel(Path(str(ended["cwd"]))) or Path(str(ended["cwd"]))
    try:
        kept = isolate.kept_branches(top)
    except Exception:  # an ending never fails on what git could not say
        kept = {}
    hits: list[tuple[dict, list[str]]] = []
    for other in store.list_tasks(repo=ended["repo"], source=("locks", "queue"), limit=300,
                                  status=["running", "done", *stops.DISMISSABLE]):
        if other["id"] == ended["id"] or (other.get("updated_at") or "") < since or (
                ended.get("workflow_run") and other.get("workflow_run") == ended.get("workflow_run")):
            continue
        if other["status"] == "running":
            tries = store.attempts(other["id"])
            paths = json.loads(tries[-1]["changed_paths"] or "[]") if tries else []
        elif other["id"] in kept:
            paths = (((store.final_event(other["id"]) or {}).get("data")) or {}).get("changed") or []
        else:
            continue
        both = sorted(mine & set(paths))
        if both:
            hits.append((other, both))
    if not hits:
        return []
    lines = ["  other work here changes the same files, so merging both conflicts there; merge one first and "
             "reconcile the other with it (nothing was merged):"]
    for other, both in hits[:OVERLAP_SHOWN]:
        state = "running now" if other["status"] == "running" else f"{other['status']}, branch kept"
        lines.append(f"    #{other['id']} ({state}) {clip(str(other['title'] or ''), 60, one_line=True)}: "
                     + ", ".join(both[:4]) + (f" and {len(both) - 4} more" if len(both) > 4 else ""))
    if len(hits) > OVERLAP_SHOWN:
        lines.append(f"    and {len(hits) - OVERLAP_SHOWN} more task(s)")
    return lines


def _behind(store: Store, ended: Mapping) -> list[str]:
    """How far each step of the ended task's workflow run is behind the branch the run
    started from, as last measured (its start, or the run's end)."""
    if not ended.get("workflow_run"):
        return []
    from locks import isolate

    lines = []
    for step in store.run_tasks(int(ended["workflow_run"])):
        kept = store.last_event(step["id"], "behind_base")
        said = isolate.behind_text(kept["data"]) if kept else None
        if said:
            lines.append(f"  step {step['workflow_step']} (#{step['id']}): {said}")
    return lines


def _run_touched_specs(store: Store, ended: Mapping) -> bool:
    """Whether a step of the ended task's workflow run changed a spec: the run caught its
    specs up, so no step of it is told they are behind."""
    from locks import specs

    if not ended.get("workflow_run"):
        return False
    for step in store.run_tasks(int(ended["workflow_run"])):
        finished = store.last_event(step["id"], "finished")
        if finished and specs.touched((finished.get("data") or {}).get("changed") or ()):
            return True
    return False


def _warned(store: Store, task_id: int) -> list[str]:
    """What a task's plan warned of when its run started, clipped."""
    planned = (store.last_event(task_id, "planned") or {}).get("data") or {}
    heard = set(planned.get("quiet") or ())
    return [clip(str(warning), 300) for warning in planned.get("warnings") or () if warning not in heard]


def _went_past(store: Store, ended: Mapping) -> list[str]:
    """The warnings of the steps of a workflow run the engine went past since the
    session last heard of that run: their endings are never handed over, so the
    run's next one says them."""
    if not ended.get("workflow_run"):
        return []
    heard, past = 0, []
    for step in store.run_tasks(int(ended["workflow_run"])):
        finished = store.last_event(step["id"], "finished")
        if step["id"] == ended["id"] or not finished or finished["id"] > ended["finished_id"]:
            continue
        via = {e["data"].get("via") for e in store.events_of(step["id"], "reported")}
        if via - {"workflow"}:
            heard = max(heard, finished["id"])
        elif via:
            past.append((finished["id"], step))
    return [f"  step {step['workflow_step']} (#{step['id']}) ran with a warning: {warning}"
            for at, step in past if at > heard for warning in _warned(store, step["id"])]


def _stopped_text(store: Store, tasks: list[dict]) -> str:
    """The session's runs that stopped, with why and what continues each: the session
    carries every one, asking the person only for what only they can grant."""
    lines = ["locks: tasks of this session that stopped; each is yours to carry (decide it, or ask the person "
             "here for what only they can grant):"]
    for t in tasks:
        stop = stops.view(store, t)
        lines.append(f"#{t['id']} [{t['status']}] {t['title']}")
        if stop:
            whose = "ask the person for it here" if stops.waits_on(store, t) == "person" else "yours to decide"
            lines.append(f"  stopped by {stop['who']}: {clip(stop['reason'], 300)}")
            lines.append(f"  next ({whose}): {stop['todo']}" + (f": {stop['next']}" if stop["next"] else ""))
    return "\n".join(lines)


def _export(env: Mapping[str, str], name: str, value: str) -> bool:
    """Set a variable for the session's Bash commands, through `CLAUDE_ENV_FILE`.
    `LOCKS_SESSION_ID` is how a `locks run` or `locks queue add` typed in a
    session is recorded as that session's."""
    env_file = env.get("CLAUDE_ENV_FILE")
    if not env_file:
        return False
    try:
        with open(env_file, "a", encoding="utf-8") as out:
            out.write(f"export {name}={shlex.quote(value)}\n")
        return True
    except OSError:
        return False


def _reach(env: Mapping[str, str]) -> str | None:
    """Make `locks` resolve in the session's Bash commands.

    The commands call it by name. Claude Code puts a plugin's `bin/` on the Bash
    tool's PATH, but older versions do not, and neither does a session opened
    before the plugin was installed. A SessionStart hook can extend the session's
    environment through `CLAUDE_ENV_FILE`; without one, the model is told the full
    path instead of finding out from an exit 127.
    """
    plugin_root = env.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        return None
    bin_dir = Path(plugin_root) / "bin"
    if not (bin_dir / "locks").is_file() or shutil.which("locks", path=env.get("PATH", "")):
        return None
    env_file = env.get("CLAUDE_ENV_FILE")
    if env_file:
        try:
            with open(env_file, "a", encoding="utf-8") as out:
                out.write(f'export PATH={shlex.quote(str(bin_dir))}:"$PATH"\n')
            return None
        except OSError:
            pass
    return (f"locks: the `locks` command is not on PATH in this session; "
            f"run it as {shlex.quote(str(bin_dir / 'locks'))}.")


def _freshen(cwd: Path, root: Path, env: Mapping[str, str], adapters: list | None) -> list[str]:
    """Start a background index for an adopted neighbour whose index is missing
    or older than the last commit, so it is current by the time work starts."""
    from locks.adapters import ABSENT, default_adapters

    if adapters is None:
        adapters = default_adapters(env)
    notes = []
    for adapter in adapters:
        if not getattr(adapter, "indexes", True):
            continue
        status = adapter.inspect(cwd)
        missing = status.state == ABSENT and adapter.server() is not None
        if (missing or status.stale) and adapter.refresh_in_background(cwd, root / f"{adapter.name}-index.log"):
            notes.append(f"locks: {'indexing' if missing else 'refreshing'} this repository with "
                         f"{adapter.name} in the background.")
    return notes


HANDLERS = {
    "UserPromptSubmit": user_prompt_submit,
    "PreToolUse": pre_tool_use,
    "PostToolUse": post_tool_use,
    "Stop": stop,
    "StopFailure": stop_failure,
}


#: Hooks that need the environment and the locks home, not only the store.
WITH_ENV = frozenset({"SessionStart", "PreCompact", "PostCompact", "SessionEnd"})


def handle(
    event_name: str, event: Mapping[str, Any], store: Store, root: Path, env: Mapping[str, str] | None = None
) -> dict | None:
    if event_name == "SessionStart":
        return session_start(event, store, root, env)
    if event_name == "PreCompact":
        keep_digest(event, store, root)
    if event_name in ("PreCompact", "PostCompact") and event.get("session_id"):
        # Whatever asked for it: the mod reads these to learn whether a `/compact`
        # it asked for ran, and every compaction starts the cooldown.
        _compaction_seen(store, root, str(event["session_id"]), event_name, trigger=event.get("trigger"),
                         instructions=event.get("custom_instructions"))
    if event_name == "SessionEnd" and event.get("cwd"):
        with contextlib.suppress(OSError):
            orchestrator.ended(project.find(str(event["cwd"])), event.get("session_id"), str(event.get("reason") or ""))
    if event_name in ("PreCompact", "SessionEnd"):
        keep_notes(event, root, dict(os.environ if env is None else env))
        return None
    handler = HANDLERS.get(event_name)
    return handler(event, store) if handler else None


def main(event_name: str, stdin: TextIO, stdout: TextIO, env: Mapping[str, str]) -> int:
    """Always exits 0 and never writes anything but a hook answer to stdout."""
    root = home(dict(env))
    if env.get("LOCKS_HOOKS_OFF") == "1":
        return 0
    try:
        event = json.load(stdin)
        if not isinstance(event, dict):
            return 0
        traced = trace.enabled(env)
        if traced:
            _trace_hook(event_name, event, env)
        store = Store(legacy.database(root))
        try:
            answer = handle(event_name, event, store, root, env)
        finally:
            store.close()
        if traced:
            with contextlib.suppress(Exception):
                trace.injected(event_name, event, answer, env)
        if answer:
            stdout.write(json.dumps(answer))
    except Exception:  # the contract: a hook never takes the session down
        note_error(root, event_name)
    return 0


def _trace_hook(event_name: str, event: Mapping[str, Any], env: Mapping[str, str]) -> None:
    """The opt-in trace (`locks.trace`): the event whole, and the transcript copied when a turn
    or the session ends. Never fails the hook."""
    with contextlib.suppress(Exception):
        if event_name not in trace.OWN_PATH:
            trace.hook(event_name, event, env)
        if event_name in ("Stop", "SessionEnd", "PreCompact"):
            trace.keep_transcript(event, env)


def note_error(root: Path, event_name: str) -> None:
    """Append the exception being handled to the error log the next SessionStart reports."""
    try:
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with (root / ERROR_LOG).open("a", encoding="utf-8") as fh:
            fh.write(f"--- {stamp} {event_name}\n{traceback.format_exc()}\n")
    except Exception:  # noqa: S110 - losing a note is acceptable, raising from here is not
        pass


#: The registry notices a SessionStart last said, so the same ones are said once.
REGISTRY_SAID = "capabilities.said"


def _registry_notices(root: Path) -> str | None:
    """What the person's capability registry does that they may not mean
    (`capabilities.notices`), said at the first SessionStart that sees it, and again
    only when it changes."""
    from locks import capabilities

    lines = capabilities.notices(root / "capabilities.json")
    said = root / REGISTRY_SAID
    text = "\n".join(lines)
    try:
        if text == (said.read_text(encoding="utf-8") if said.is_file() else ""):
            return None
        said.write_text(text, encoding="utf-8")
    except OSError:
        return None
    return text or None


def _drain_errors(root: Path) -> int:
    """How many errors were noted since the last drain. The log is kept for
    reading; a marker file remembers how much of it was already reported."""
    log, mark = root / ERROR_LOG, root / (ERROR_LOG + ".reported")
    try:
        if not log.is_file():
            return 0
        total = log.read_text(encoding="utf-8").count("\n--- ") + (
            1 if log.read_text(encoding="utf-8").startswith("--- ") else 0)
        seen = int(mark.read_text(encoding="utf-8")) if mark.is_file() else 0
        mark.write_text(str(total), encoding="utf-8")
        return max(total - seen, 0)
    except (OSError, ValueError):
        return 0
