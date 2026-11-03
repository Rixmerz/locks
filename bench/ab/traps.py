"""A/B of two locks checkouts on trap tasks: does a change to locks waste less?

Real money: run on purpose. Two arms that differ only by the checkout `locks run` imports
(`PYTHONPATH=<checkout>/src`): the base (main) and the head (a branch). Same model, same tasks,
same reps, each run in a fresh repository with its own `LOCKS_HOME`. Each task is a trap built
so one class of waste fires (after hermes-agent's `evals/toolperf_abeval`): a change that claims
to fix a waste class must move its trap.

Judged by locks' records, never a worker's words: the task's status, its attempts, turns and
dollars from its store, the tool calls that failed from its tool-call log, and a hidden check the
worker never sees run after it ends.

    python bench/ab/traps.py run --base ../locks-main --head . --reps 2 [--traps a,b]
    python bench/ab/traps.py report
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
OUT = HERE / "results.jsonl"


@dataclass(frozen=True)
class Trap:
    name: str
    waste: str
    request: str
    files: dict[str, str]
    verify: str
    hidden: str


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


TRAPS: tuple[Trap, ...] = (
    Trap("already-applied", "a fix asked for that the code already has",
         "Make `total` in calc.py return 0 for an empty list instead of raising.",
         {"calc.py": "def total(xs):\n    if not xs:\n        return 0\n    return sum(xs)\n",
          "test_calc.py": "from calc import total\n\ndef test_empty():\n    assert total([]) == 0\n"},
         "python3 -m pytest -q", "python3 -c 'from calc import total; assert total([]) == 0'"),
    Trap("ambiguous-edit", "an edit whose text matches in several places",
         "In shapes.py, make only `area_square` return side*side (it returns side*2 by mistake).",
         {"shapes.py": "def area_square(side):\n    return side * 2\n\n\ndef perimeter_half(side):\n"
                       "    return side * 2\n",
          "test_shapes.py": "from shapes import area_square, perimeter_half\n\ndef test_area():\n"
                            "    assert area_square(3) == 9\n\ndef test_half():\n    assert perimeter_half(3) == 6\n"},
         "python3 -m pytest -q", "python3 -m pytest -q"),
    Trap("giant-output", "a check whose output is too large to read whole",
         "The test suite fails: fix the bug in parse.py so every test passes.",
         {"parse.py": "def number(text):\n    return int(text.strip()) + 1\n",
          "test_parse.py": "from parse import number\nimport pytest\n\n@pytest.mark.parametrize('i', range(400))\n"
                           "def test_noise(i):\n    print('x' * 400)\n\ndef test_number():\n"
                           "    assert number(' 41 ') == 41\n"},
         "python3 -m pytest -q", "python3 -c 'from parse import number; assert number(\"7\") == 7'"),
    Trap("hidden-config", "the value to change lives in a hidden folder",
         "Raise the retry limit the app reads from its config from 3 to 5.",
         {".config/app.json": "{\"retries\": 3}\n",
          "app.py": "import json, pathlib\n\ndef retries():\n    return json.loads(pathlib.Path(__file__)"
                    ".with_name('.config').joinpath('app.json').read_text())['retries']\n"},
         "python3 -c 'import app; print(app.retries())'",
         "python3 -c 'import app; assert app.retries() == 5'"),
    Trap("wrong-python", "`python` missing, only `python3`",
         "Add a function `double(x)` to util.py that returns x*2, with a test.",
         {"util.py": "def half(x):\n    return x / 2\n"},
         "python3 -m pytest -q", "python3 -c 'from util import double; assert double(4) == 8'"),
)


def build(trap: Trap, where: Path) -> Path:
    """A fresh repository holding the trap's files, one commit."""
    repo = where / trap.name
    repo.mkdir(parents=True)
    for name, text in trap.files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=bench@locks", "-c", "user.name=bench", "commit", "-qm", "trap")
    return repo


def measure(home: Path) -> dict:
    """What locks' own store says of the run: the last task's status, attempts, turns, dollars, and
    the tool calls that failed (`tool_events` after mining, else the raw log)."""
    db = home / "locks.db"
    out = {"status": None, "attempts": 0, "turns": 0, "usd": 0.0, "failed_calls": 0, "calls": 0}
    if db.is_file():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT id, status FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                out["status"] = row[1]
                a = conn.execute("SELECT COUNT(*), SUM(turns), SUM(cost_usd) FROM attempts WHERE task_id = ?",
                                 (row[0],)).fetchone()
                out.update(attempts=a[0] or 0, turns=a[1] or 0, usd=round(a[2] or 0.0, 4))
        finally:
            conn.close()
    for log in (home / "tool-events").glob("*.ndjson"):
        for line in log.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            out["calls"] += 1
            out["failed_calls"] += 0 if event.get("ok") else 1
    return out


def run_one(trap: Trap, checkout: Path, arm: str, rep: int,
            runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"locks-ab-{arm}-") as tmp:
        root = Path(tmp)
        repo = build(trap, root)
        home = root / "home"
        env = {**os.environ, "PYTHONPATH": str(checkout / "src"), "LOCKS_HOME": str(home),
               "LOCKS_LATE_VERIFIER": "off"}
        env.pop("LOCKS_WORKER_TASK", None)
        runner([sys.executable, "-m", "locks", "run", "--verify", trap.verify, "--no-isolate", trap.request],
               cwd=repo, env=env, capture_output=True, text=True, timeout=1800, check=False)
        hidden = runner(["sh", "-c", trap.hidden], cwd=repo, capture_output=True, text=True, timeout=120,
                        check=False)
        return {"trap": trap.name, "arm": arm, "rep": rep, "right": hidden.returncode == 0, **measure(home)}


def compare(rows: list[dict]) -> dict:
    """Per trap and arm: runs, right, and the medians of attempts, turns, dollars and failed calls."""
    out: dict[str, dict[str, dict]] = {}
    for row in rows:
        out.setdefault(row["trap"], {}).setdefault(row["arm"], []).append(row)
    report = {}
    for trap, arms in out.items():
        report[trap] = {}
        for arm, got in arms.items():
            report[trap][arm] = {"runs": len(got), "right": sum(1 for r in got if r["right"]),
                                 **{k: median(r[k] for r in got) for k in ("attempts", "turns", "usd",
                                                                         "failed_calls")}}
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--base", required=True, type=Path)
    r.add_argument("--head", required=True, type=Path)
    r.add_argument("--reps", type=int, default=2)
    r.add_argument("--traps", default="")
    sub.add_parser("report")
    args = p.parse_args(argv)
    if args.cmd == "run":
        chosen = [t for t in TRAPS if not args.traps or t.name in args.traps.split(",")]
        with OUT.open("a") as fh:
            for trap in chosen:
                for rep in range(args.reps):
                    for arm, checkout in (("base", args.base), ("head", args.head)):
                        row = run_one(trap, checkout.resolve(), arm, rep)
                        fh.write(json.dumps(row) + "\n")
                        print(json.dumps(row))
        return 0
    rows = [json.loads(line) for line in OUT.read_text().splitlines()] if OUT.is_file() else []
    print(json.dumps(compare(rows), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
