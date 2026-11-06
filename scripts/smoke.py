#!/usr/bin/env python3
"""A real task on this checkout's locks, before a release: the CLI and the model are the one thing
no test here fakes faithfully (CLAUDE.md, "Verify before reporting done").

It makes a throwaway repository, runs one small `locks run` there with a real `claude` worker, in a
locks home of its own with the trace on, and checks what a release must not break:

- the task passes on its first attempt, its check run by locks (`--verify`);
- the worker's change is on the task's branch, and the check holds there;
- the worker's transcript is kept beside the trace.

It spends real money (cents). Run it on purpose: `python3 scripts/smoke.py`. Exit 0 when every
check holds; each failed check is printed. `--keep` leaves the folder to read.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUEST = "Add a function multiply(a, b) to calc.py that returns a times b."
CHECK = f"{sys.executable} -c 'from calc import multiply; assert multiply(6, 7) == 42'"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="leave the throwaway folder to read")
    args = parser.parse_args()
    if shutil.which("claude") is None:
        print("smoke: no `claude` on PATH: a real task cannot run here", file=sys.stderr)
        return 2
    folder = Path(tempfile.mkdtemp(prefix="locks-smoke-"))
    repo, home = folder / "repo", folder / "home"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=smoke@locks", "-c", "user.name=smoke", "commit", "-qm", "init")
    env = {**os.environ, "LOCKS_HOME": str(home), "LOCKS_TRACE": "on", "LOCKS_AUTOWORK": "off",
           "LOCKS_LIVESPEC": "off", "LOCKS_NOTES": "off", "LOCKS_LATE_VERIFIER": "off"}
    run = subprocess.run([str(ROOT / "bin" / "locks"), "run", REQUEST, "--repo", str(repo), "--verify", CHECK],
                         capture_output=True, text=True, env=env, timeout=1200, check=False)
    failed: list[str] = []
    first = run.stdout.splitlines()[0] if run.stdout.strip() else ""
    if run.returncode != 0 or not first.endswith(": done"):
        failed.append(f"the task did not pass (exit {run.returncode}): {run.stdout[-600:]}{run.stderr[-600:]}")
    shown = subprocess.run([str(ROOT / "bin" / "locks"), "show", "1", "--json"], capture_output=True, text=True,
                           env=env, check=False)
    try:
        attempts = json.loads(shown.stdout).get("attempts") or []
    except ValueError:
        attempts = []
    if len(attempts) != 1:
        failed.append(f"expected one attempt, found {len(attempts)}")
    branches = _git(repo, "branch", "--list", "locks/task-1")
    if "locks/task-1" not in branches:
        failed.append("no branch locks/task-1 keeps the work")
    else:
        source = _git(repo, "show", "locks/task-1:calc.py")
        if "def multiply" not in source:
            failed.append("the kept branch has no multiply in calc.py")
    kept = list((home / "traces").glob("*.transcript.jsonl")) if (home / "traces").is_dir() else []
    if not kept:
        failed.append("no worker transcript was kept beside the trace")
    for line in failed:
        print(f"smoke: FAILED: {line}")
    if not failed:
        print(f"smoke: passed ({run.stdout.splitlines()[-1] if run.stdout else ''})")
    if args.keep:
        print(f"smoke: left in {folder}")
    else:
        shutil.rmtree(folder, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
