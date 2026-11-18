"""A task whose first attempt forks the orchestrating session itself (`locks run --fork-session`).

Work that points back into the conversation ("fix that one", "as we discussed") was left to the
session, unchecked: a fresh worker does not see what "that" is. A fork of the session does. Its
first attempt is `claude -p --resume <host session> --fork-session` in the task's worktree: a new
session, with a new id, that holds the conversation up to the fork point, then locks' loop as for
any task (its check, the independent verifier, which is never a fork and reads blind, and the
escalation, where another model starts fresh).

Claude Code keeps a session's transcript under the folder of the directory it ran in
(`projects/<cwd with every other character a dash>/<id>.jsonl`), and `--resume` looks for it
there: a worktree is another directory, so the host's transcript is copied into the worktree's
folder for the attempt and removed after it (`place`, `LaunchSpec.session_copy`). Claude Code
records a session's system prompt on its first request and every resume and fork sends that
record, so the worker's role and its capabilities' hints reach it as its first message (`note`),
as a workflow trunk's fork gets them (`locks.trunks`). stdlib; no model.

Whether forking pays is a trial (`KEY`, the `host_fork` setting): a repository's forkable tasks
alternate `fork` and `fresh` first tries (`locks.arms`), and forking stays only where it won.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from locks import arms as arms_mod
from locks import config
from locks.store import Store

KEY = "host_fork_arm"
WHY_KEY = "host_fork_why"
ARMS = ("fork", "fresh")
#: First tries each arm needs before the trial decides.
TRIAL = 10
#: A host session whose context held more tokens than this is not forked: its fork would start with
#: little room before it compacts (the bound a workflow trunk's fork keeps).
MAX_TOKENS = 120_000
#: Where the session that sends the work names itself: locks' own hook first, then the host's.
SESSION_VARS = ("LOCKS_SESSION_ID", "CLAUDE_CODE_SESSION_ID")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return config.enabled("host_fork", os.environ if env is None else env)


def source(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What a `--fork-session` task forks, read when it is sent: the sending session's id, or
    `missing`, why there is none (the task then starts fresh and says so)."""
    env = os.environ if env is None else env
    for name in SESSION_VARS:
        value = str(env.get(name) or "").strip()
        if value and _ID.match(value):
            return {"session": value}
    return {"missing": "no session sent it (no LOCKS_SESSION_ID or CLAUDE_CODE_SESSION_ID)"}


def _config_root(env: Mapping[str, str]) -> Path:
    if env.get("CLAUDE_CONFIG_DIR"):
        return Path(env["CLAUDE_CONFIG_DIR"])
    return Path(env.get("HOME") or Path.home()).expanduser() / ".claude"


def project_folder(cwd: Path | str, env: Mapping[str, str] | None = None) -> Path:
    """Claude Code's folder for the sessions of a directory: its path, every character that is no
    letter or digit a dash."""
    env = os.environ if env is None else env
    return _config_root(env) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd)))


def transcript(session_id: str, env: Mapping[str, str] | None = None) -> Path | None:
    from locks import usage

    return usage.transcript_of(session_id, env)


def context_of(path: Path) -> tuple[int | None, str]:
    """The tokens the session's context held at its last model call, and the model that made it,
    read from its transcript; (None, "") when nothing could be read."""
    tokens: int | None = None
    model = ""
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message") or {}
                if not isinstance(message, dict):
                    continue
                model = str(message.get("model") or model)
                used = message.get("usage") or {}
                if isinstance(used, dict) and used:
                    tokens = sum(int(used.get(k) or 0) for k in (
                        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    except OSError:
        return None, ""
    return tokens, model


def prepare(fork: Mapping[str, Any] | None, *, worktree: bool,
            env: Mapping[str, str] | None = None) -> tuple[dict[str, Any] | None, str]:
    """What the first attempt forks (`session`, `path` of its transcript, `model`, `tokens`), or
    None and why it starts fresh: no session sent it, no worktree to keep a session in, a transcript
    that cannot be found, or a context over `MAX_TOKENS`."""
    if not fork:
        return None, ""
    if fork.get("missing"):
        return None, str(fork["missing"])
    if not worktree:
        return None, "it runs in no worktree of its own, where a session is kept"
    session = str(fork.get("session") or "")
    path = transcript(session, env)
    if path is None:
        return None, f"the transcript of session {session} is not where Claude Code keeps it"
    tokens, model = context_of(path)
    if tokens is not None and tokens > MAX_TOKENS:
        return None, f"the session's context held {tokens} tokens, over {MAX_TOKENS}: little room left"
    return {"session": session, "path": str(path), "model": model,
            **({"tokens": tokens} if tokens is not None else {})}, ""


def session_cost(fork: Mapping[str, Any]) -> float:
    """What the host's session had cost at the fork, read from its transcript: the CLI's figure for a fork is
    the whole session's, so the attempt's own is what it adds to this (`launch.own_cost`). 0 when unread."""
    from locks import usage

    try:
        return usage.transcript_cost(Path(str(fork.get("path") or "")))
    except (OSError, ValueError):
        return 0.0


def same_model(fork: Mapping[str, Any], model: str) -> bool:
    """Whether a cell of `model` (an alias) may fork the host: only on the model the session ran on,
    since another model never inherits an approach (`trunks.same_model`). Unknown is no match."""
    from locks import models

    return bool(fork.get("model")) and models.family(str(fork["model"])) == model


def place(source_path: Path | str, session_id: str, cwd: Path | str,
          env: Mapping[str, str] | None = None) -> Path | None:
    """Copy the host's transcript where `--resume` run in `cwd` finds it; the copy's path, or None
    when it was already there (or could not be copied: the CLI then refuses the resume and the
    attempt starts fresh, saying why)."""
    folder = project_folder(Path(cwd).resolve(), env)
    target = folder / f"{session_id}.jsonl"
    try:
        if target.exists() or Path(source_path).resolve() == target.resolve():
            return None
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target)
    except OSError:
        return None
    return target


def note(*, role: str, capabilities: str, workdir: Path) -> str:
    """The first words the fork reads after the fork point: that it is a new worker of locks now,
    where its files are, its tools, its role (the system prompt is the host's record) and its hints."""
    lines = [
        "locks: everything above is the conversation of the session that sent this task. From here this session "
        "is a new worker run by locks, with the task below: what was said above is the context the task points "
        "back to, and the task below is what to do. You are not that session: you send no work to locks, and "
        "you answer only through the result block your task asks for.",
        f"Your directory is now {workdir}: a checkout of its own for this task. Read and change files only here, "
        "by paths under this directory; a file read above may have been in another checkout.",
        "Your tools are the ones this launch gives you: any tool or server named above that you do not have now "
        "is gone, and one you have now is yours to use.",
    ]
    if role:
        lines.append("Your role from here, in place of anything above:\n" + role)
    if capabilities:
        lines.append(capabilities)
    return "\n\n".join(lines)


def trial(store: Store, repo: str | None) -> dict[str, dict[str, Any]]:
    rows = [{"arm": r["arm"], "tries": 1, "passed": int(r["passed_first"]), "duration_s": r["duration_s"]}
            for r in store.arm_trial(repo, KEY)]
    return arms_mod.tally(rows, ARMS)


def arm(store: Store, repo: str | None, task_id: int) -> tuple[str, str]:
    """Whether this task's first attempt forks the host, and why (`locks.arms`)."""
    if repo is None:
        return "fork", "no repository to run a trial in"
    return arms_mod.pick(trial(store, repo), ARMS, task_id, TRIAL, "host fork", repo, pooled=trial(store, None))
