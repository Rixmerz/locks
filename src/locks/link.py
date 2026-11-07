"""`locks link`: the `locks` command in a terminal, outside Claude Code.

Claude Code installs the plugin under a directory named for its version, so a
symlink to it breaks at the next update. What is written instead is a small
shim that, each time it runs, finds the newest locks Claude Code has installed.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path

from locks import legacy

MARKER = "# Written by `locks link`."
SHIM = """#!/usr/bin/env bash
{marker} It runs the newest locks Claude Code has installed, so a plugin
# update never breaks it. `locks link --remove` takes it away.
set -euo pipefail
CONFIG="${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}"
newest=""
for c in "$CONFIG"/plugins/cache/*/locks/*/bin/locks; do
  [ -x "$c" ] || continue
  if [ -z "$newest" ] || [ "$c" -nt "$newest" ]; then newest="$c"; fi
done
if [ -z "$newest" ] && [ -x {fallback} ]; then newest={fallback}; fi
if [ -z "$newest" ]; then
  echo "locks: no installed locks found; claude plugin install locks@rixmerz" >&2
  exit 127
fi
exec "$newest" "$@"
"""


#: What the old name's link command wrote before the rename: a shim that only finds old installs.
OLD_MARKER = f"# Written by `{legacy.OLD} link`."


def write_old(target_dir: Path, launcher: Path) -> Path | None:
    """The shim the old name's link command wrote, rewritten to run the newest Locks (and say
    once that it is Locks now), so the old command survives the old plugin's removal. Never a
    file that command did not write. None when there is none to rewrite."""
    target = target_dir / legacy.OLD
    try:
        if target.is_symlink() or not target.is_file() or OLD_MARKER not in target.read_text(errors="replace"):
            return None
        target.write_text(SHIM.format(marker=MARKER, fallback=_quote(launcher)).replace(
            'exec "$newest"', f'LOCKS_INVOKED_AS={legacy.OLD} exec "$newest"'))
    except OSError:
        return None
    return target


def default_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or Path.home()) / ".local" / "bin"


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def write(target_dir: Path, launcher: Path) -> Path:
    """Write the shim; refuses to replace a `locks` that this command did not write."""
    target = target_dir / "locks"
    if target.is_symlink() or (target.exists() and MARKER not in target.read_text(errors="replace")):
        raise FileExistsError(f"{target} exists and was not written by `locks link`; remove it first")
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(SHIM.format(marker=MARKER, fallback=_quote(launcher)))
    target.chmod(0o755)
    return target


def remove(target_dir: Path) -> Path | None:
    target = target_dir / "locks"
    if target.is_file() and not target.is_symlink() and MARKER in target.read_text(errors="replace"):
        target.unlink()
        return target
    return None


def on_path(target_dir: Path, env: Mapping[str, str]) -> bool:
    entries = [Path(p).expanduser() for p in env.get("PATH", "").split(os.pathsep) if p]
    return target_dir.resolve() in {e.resolve() for e in entries}


# --- what Claude Code has installed ------------------------------------------


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)*)\s*", text or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def installed(env: Mapping[str, str], *, old: bool = False) -> list[tuple[tuple[int, ...], Path]]:
    """Every locks Claude Code has installed, as (version, launcher), oldest first; with `old`,
    the installs of its old name (`legacy.PLUGIN`) instead. The version is read from each install's
    own plugin.json, never its mtime."""
    config = Path(env.get("CLAUDE_CONFIG_DIR") or Path(env.get("HOME") or Path.home()) / ".claude")
    name = legacy.PLUGIN if old else "locks"
    found = []
    for launcher in config.glob(f"plugins/cache/*/{name}/*/bin/{name}"):
        try:
            manifest = json.loads((launcher.parents[1] / ".claude-plugin" / "plugin.json").read_text())
        except (OSError, ValueError):
            continue
        version = parse_version(str(manifest.get("version") or "")) if isinstance(manifest, dict) else None
        if version and os.access(launcher, os.X_OK):
            found.append((version, launcher))
    return sorted(found)


#: The version a command was forwarded from (`forward_to`): the launcher on the PATH of the
#: session that typed it. That PATH is fixed when the session starts, so after `/reload-plugins`
#: it still names the old plugin while the session's skills and hooks are the new one's: only a
#: hint, which `session_loaded` overrides. None when it was not forwarded.
LOADED: str | None = None

#: What each session's hooks last ran as, kept in the locks home's `work/`; the newest only.
LOADED_BY_HOOKS, LOADED_KEPT = "loaded-plugin.json", 200

#: When each session's hooks last ran, beside `LOADED_BY_HOOKS`: a version a session ran from
#: within `LIVE_S` is one a live session's hooks still point at, so its folder is not offered for
#: removal (a compaction's SessionStart hook in that session would fail to start, and the prompt it
#: carries be lost). Refreshed at most every `LOADED_REFRESH_S`, never on every hook.
LOADED_AT, LIVE_S, LOADED_REFRESH_S = "loaded-plugin-at.json", 12 * 3600, 600


def _read_loaded(path: Path) -> dict[str, str]:
    try:
        seen = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in seen.items() if isinstance(v, str)} if isinstance(seen, dict) else {}


def record_loaded(root: Path, session_id: str, version: str) -> None:
    """A hook runs the plugin its session has loaded now (Claude Code re-reads the hooks on
    `/reload-plugins`), so the version it runs is what the session's text is."""
    _touch_loaded(root, session_id)
    path = root / "work" / LOADED_BY_HOOKS
    seen = _read_loaded(path)
    if seen.get(session_id) == version:
        return
    seen.pop(session_id, None)
    seen[session_id] = version
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(list(seen.items())[-LOADED_KEPT:])))


def _touch_loaded(root: Path, session_id: str, now: float | None = None) -> None:
    path = root / "work" / LOADED_AT
    try:
        seen = json.loads(path.read_text())
        seen = {k: v for k, v in seen.items() if isinstance(v, (int, float))} if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        seen = {}
    now = time.time() if now is None else now
    if now - seen.get(session_id, 0) < LOADED_REFRESH_S:
        return
    seen.pop(session_id, None)
    seen[session_id] = now
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(list(seen.items())[-LOADED_KEPT:])))


def live_versions(root: Path, now: float | None = None) -> set[str]:
    """The versions sessions ran their hooks from within `LIVE_S`: still loaded somewhere."""
    try:
        at = json.loads((root / "work" / LOADED_AT).read_text())
    except (OSError, ValueError):
        return set()
    if not isinstance(at, dict):
        return set()
    now = time.time() if now is None else now
    loaded = _read_loaded(root / "work" / LOADED_BY_HOOKS)
    return {loaded[s] for s, t in at.items()
            if s in loaded and isinstance(t, (int, float)) and now - t < LIVE_S}


def session_loaded(root: Path, session_id: str | None) -> str | None:
    """The version the session's hooks last ran as, or None when none has run since."""
    return _read_loaded(root / "work" / LOADED_BY_HOOKS).get(session_id or "")


def forward_to(argv: list[str], env: Mapping[str, str], own: str, version: str) -> Path | None:
    """The newer installed locks a command should run instead of this copy, or None.

    A session's PATH keeps the plugin version it started with, so `locks <new
    command>` one update later would be "invalid choice". A command runs the
    newest install; a hook stays on the version that registered it, and a copy
    outside Claude Code's plugin cache (a checkout being worked on) never forwards."""
    marker = f"{os.sep}plugins{os.sep}cache{os.sep}"
    if (argv[:1] == ["hook"]) or marker not in own:
        return None
    found = installed(env)
    mine = parse_version(version)
    if not found or mine is None or found[-1][0] <= mine:
        return None
    return found[-1][1]
