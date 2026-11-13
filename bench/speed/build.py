"""Which cell gets a medium build done soonest, and right?

Real: one `claude -p` worker per run, started by locks' `launch.run`, on a fresh copy of the
pricing engine `bench/memory/hard`, asked for one feature: three pricing rules, a receipt and a
CSV export. Seven hidden tests judge it, and the worker never sees them (`hidden.tgz`). One
attempt per run: its wall time, its turns, and how many hidden tests pass.

    python bench/speed/build.py run sonnet/high,sonnet/xhigh,opus/high,opus/xhigh 3 4
    python bench/speed/build.py report
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from locks import grants, launch  # noqa: E402
from locks.matrix import Cell  # noqa: E402

BASE = HERE.parent / "memory" / "hard"
ASK = ("Add pricing rules: loyalty members get their tier's discount on the order (gold 10%, silver 5%); cart "
       "lines tagged promo get 15% off; a line of 10 or more of one item gets 5% off that line. Add a printed "
       "receipt, `tally.receipt.receipt(cart, customer) -> str`, one row per line and per discount and then the "
       "total, and a CSV export of an order for finance, `tally.export.rows(cart, customer) -> list[str]`, a "
       "header row, one row per line (item, qty, unit price, line total) and a total row.\n\nAdd tests for it, "
       "and run the suite before you stop.")
HARD = (" Also reorganize the rules: move every rule module into `src/tally/rules/line/` or "
        "`src/tally/rules/order/` by its stage, and rename the rule methods everywhere, `apply_line` to `on_line` "
        "and `apply_order` to `on_order`, so every rule, the checkout and the tests use the new names and every "
        "rule still prices as it did.")
MAX_TURNS = 120
#: An arm `<cell>+batch` adds this to the request: what `launch` would tell every writing worker.
BATCH = ("Make the tool calls that do not depend on each other in the same turn: read the files you need "
         "together, and make independent edits together, rather than one call a turn.")


def repository(variant: str = "plain") -> Path:
    where = Path(tempfile.mkdtemp(prefix="tally-build-")) / "repo"
    shutil.copytree(BASE, where, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if variant == "hard":
        sys.path.insert(0, str(HERE.parent / "review"))
        import volume

        volume.base(where)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=where, check=True)
    subprocess.run(["git", "add", "-A"], cwd=where, check=True)
    subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", "init"], cwd=where,
                   check=True)
    return where


def hidden(where: Path, variant: str = "plain") -> int:
    with tempfile.TemporaryDirectory() as tmp:
        packed = HERE / ("hidden-hard.tgz" if variant == "hard" else "hidden.tgz")
        subprocess.run(["tar", "xzf", str(packed), "-C", tmp], check=True)
        done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "ci/hbuild.py"],
                              cwd=tmp, capture_output=True, text=True, timeout=300,
                              env={"PYTHONPATH": f"{where / 'src'}:{Path(tmp) / 'ci'}", "PATH": "/usr/bin:/bin"})
    found = re.search(r"(\d+) passed", done.stdout)
    return int(found.group(1)) if found else 0


def one(cell: str, variant: str = "plain") -> dict:
    model, effort = cell.split("+")[0].split("/")
    where = repository(variant)
    ask = ASK.replace("\n\nAdd tests", HARD + "\n\nAdd tests") if variant == "hard" else ASK
    if cell.endswith("+batch"):
        ask += "\n\n" + BATCH
    spec = launch.LaunchSpec(prompt=ask, cell=Cell(model, effort), launch_dir=where, max_turns=MAX_TURNS,
                             timeout_s=launch.timeout_for(MAX_TURNS), permission_mode=grants.mode_for(model),
                             allowed_tools=("Bash(make:*)", "Bash(python3:*)", "Bash(git:*)"))
    began = time.monotonic()
    try:
        r = launch.run(spec)
    except Exception as exc:  # a launch that broke is recorded, never retried here
        shutil.rmtree(where.parent, ignore_errors=True)
        return {"cell": cell, "error": str(exc)[:300]}
    wall = time.monotonic() - began
    visible = subprocess.run(["make", "test"], cwd=where, capture_output=True, text=True, timeout=300).returncode == 0
    row = {"cell": cell, "wall_s": round(wall, 1), "turns": r.turns, "cost": round(r.cost_usd, 4),
           "claimed": r.passed, "visible": visible, "hidden": hidden(where, variant),
           "of": 10 if variant == "hard" else 7,
           "variant": variant,
           "s_per_turn": round(wall / r.turns, 1) if r.turns else None}
    shutil.rmtree(where.parent, ignore_errors=True)
    return row


def main(argv: list[str]) -> None:
    variant = "hard" if "--hard" in argv else "plain"
    argv = [a for a in argv if a != "--hard"]
    out = HERE / ("results-build-hard.jsonl" if variant == "hard" else "results-build.jsonl")
    if argv[0] == "report":
        rows = [json.loads(x) for x in out.read_text().splitlines() if x.strip() and "error" not in x]
        print(f"{'cell':<14}{'n':>3}{'all':>7}{'hidden':>8}{'wall p50':>10}{'turns':>7}{'s/turn':>8}{'$ p50':>7}"
              f"{'wall per right build':>22}")
        for cell in sorted({r["cell"] for r in rows}):
            rs = [r for r in rows if r["cell"] == cell]
            right = sum(r["hidden"] == r["of"] for r in rs)  # a build is right only when every hidden test passes
            tests = f"{sum(r['hidden'] for r in rs)}/{sum(r['of'] for r in rs)}"
            print(f"{cell:<14}{len(rs):>3}{right:>4}/{len(rs):<2}{tests:>8}"
                  f"{median(r['wall_s'] for r in rs):>10.0f}{median(r['turns'] for r in rs):>7}"
                  f"{median(r['s_per_turn'] for r in rs):>8.1f}{median(r['cost'] for r in rs):>7.2f}"
                  f"{(sum(r['wall_s'] for r in rs) / right if right else float('inf')):>22.0f}")
        return
    cells, reps = argv[1].split(","), int(argv[2]) if len(argv) > 2 else 3
    jobs = [c for _ in range(reps) for c in cells]

    def run(cell: str) -> None:
        row = one(cell, variant)
        with out.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    with ThreadPoolExecutor(int(argv[3]) if len(argv) > 3 else 4) as pool:
        list(pool.map(run, jobs))


if __name__ == "__main__":
    main(sys.argv[1:])
