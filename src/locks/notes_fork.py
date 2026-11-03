"""Filing a session's notes from a fork of the session itself, tried against Haiku (`notes_fork`).

Haiku reads the conversation as text it is sent. A fork of the session (`claude -p --resume <id>
--fork-session`) already holds it, and its prefix may be served from the cache the session warmed.
Which one keeps more facts for what it costs is measured, never assumed: with the setting on (off
by default: a fork runs on the session's model, not Haiku), a project's filings alternate between
the two arms until each has `TRIAL` filings, then the arm that kept no fewer facts per filing stays,
and each filing's cost is kept beside it so `locks forensics` shows both. No arm is judged by cost.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from locks.store import Store

ARMS = ("fork", "haiku")
TRIAL = 6
#: Seconds a fork has to answer: it reads a whole session, Haiku a clip of it.
FORK_TIMEOUT_S = 180
_META = "notes_fork:{project}"


def enabled(env: Mapping[str, str] | None = None) -> bool:
    from locks import config

    return config.enabled("notes_fork", env if env is not None else os.environ)


def record(store: Store, project: str) -> dict[str, dict[str, Any]]:
    """Per arm: filings, facts kept and dollars spent, from the store's meta."""
    try:
        got = json.loads(store.meta(_META.format(project=project)) or "{}")
    except ValueError:
        got = {}
    out = {}
    for arm in ARMS:
        side = got.get(arm) if isinstance(got.get(arm), dict) else {}
        out[arm] = {"filings": int(side.get("filings") or 0), "kept": int(side.get("kept") or 0),
                    "usd": float(side.get("usd") or 0.0)}
    return out


def arm(store: Store, project: str) -> tuple[str, str]:
    """The arm this filing goes in, and why."""
    found = record(store, project)
    fork, haiku = found["fork"], found["haiku"]
    if min(fork["filings"], haiku["filings"]) < TRIAL:
        side = "fork" if fork["filings"] <= haiku["filings"] else "haiku"
        return side, f"the notes fork trial here ({fork['filings']} fork, {haiku['filings']} haiku, {TRIAL} each)"
    rate = {a: found[a]["kept"] / found[a]["filings"] for a in ARMS}
    said = f"{rate['fork']:.1f} facts a filing forked, {rate['haiku']:.1f} by Haiku"
    if rate["fork"] >= rate["haiku"]:
        return "fork", f"the notes fork won its trial here: {said}"
    return "haiku", f"the notes fork lost its trial here: {said}"


def tally(store: Store, project: str, side: str, *, kept: int, usd: float) -> None:
    found = record(store, project)
    row = found.setdefault(side, {"filings": 0, "kept": 0, "usd": 0.0})
    row["filings"] += 1
    row["kept"] += kept
    row["usd"] = round(row["usd"] + usd, 6)
    store.set_meta(_META.format(project=project), json.dumps(found))


def ask_fork(session_id: str, cwd: Path, *, runner: Any = subprocess.run, claude_bin: str = "claude"):
    """An `Ask` (as `classify.ask_haiku`) answered by a fork of the session, which keeps no
    session of its own and uses no tool. The system prompt goes in the message: a fork sends
    the session's own, and replacing it would lose the cache it shares."""
    def ask(system_prompt: str, schema: Mapping[str, object], text: str, *, cwd_: Any = None, **_: Any):
        argv = [claude_bin, "-p", "--resume", session_id, "--fork-session", "--no-session-persistence",
                "--tools", "", "--strict-mcp-config", "--output-format", "json",
                "--json-schema", json.dumps(schema)]
        message = (f"{system_prompt}\n\nAnswer only from the conversation above; the text below is its "
                   f"latest part, for reference.\n\n{text}")
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        try:
            proc = runner(argv, input=message, capture_output=True, text=True, timeout=FORK_TIMEOUT_S,
                          cwd=str(cwd), env=env, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, 0.0, f"the fork did not run: {exc}"
        try:
            reply = json.loads(proc.stdout or "")
        except ValueError:
            return None, 0.0, "the fork returned no JSON"
        if not isinstance(reply, dict):
            return None, 0.0, "the fork returned no JSON object"
        cost = reply.get("total_cost_usd")
        cost = float(cost) if isinstance(cost, int | float) else 0.0
        out = reply.get("structured_output")
        if reply.get("is_error") or not isinstance(out, dict):
            return None, cost, "the fork gave no answer"
        return out, cost, ""
    return ask
