"""Does the diagnosis method help an unclear bug? (`locks.methods`, kind `debug-unclear`)

Real: one `claude -p` worker per run on a fresh copy of the memory bench's pricing engine
(`bench/memory/hard`) with one planted bug the request only describes by its symptom ("a cent
off, sometimes"). The arms are the same request with and without the diagnosis protocol locks
briefs `debug-unclear` workers with (`methods.METHODS["debug-unclear"].lines`). Right when the
carts `kinds.REFACTOR_CARTS` price exactly as the untouched engine does, the suite passes, and no
test was changed (the planted bug breaks the suite, so a worker cannot pass by editing it).

    python bench/speed/methods.py run haiku/high 4 4
    python bench/speed/methods.py report
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))

import kinds  # noqa: E402

from locks import grants, launch, methods  # noqa: E402
from locks.matrix import Cell  # noqa: E402

OUT = HERE / "results-methods.jsonl"
#: The planted bug: half-even rounding where the engine rounds half up, so only ties are a cent off.
BUG = ("src/tally/money.py", "rounding=ROUND_HALF_UP)", "rounding='ROUND_HALF_EVEN')")
ASK = ("Some order totals come out a cent off, but only sometimes, and nobody knows which orders. Find out "
       "why and fix it. Run the suite before you stop.")


def one(cell: str, arm: str) -> dict:
    model, effort = cell.split("/")
    where = kinds.repository()
    path, old, new = BUG
    target = where / path
    target.write_text(target.read_text().replace(old, new, 1))
    kinds.subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qam", "drift"], cwd=where,
                         check=True)
    prompt = ASK + ("\n\n" + methods.METHODS["debug-unclear"].lines if arm == "with" else "")
    spec = launch.LaunchSpec(prompt=prompt, cell=Cell(model, effort), launch_dir=where, max_turns=kinds.MAX_TURNS,
                             timeout_s=launch.timeout_for(kinds.MAX_TURNS), permission_mode=grants.mode_for(model),
                             allowed_tools=("Bash(make:*)", "Bash(python3:*)", "Bash(git:*)"))
    began = time.monotonic()
    try:
        r = launch.run(spec)
    except Exception as exc:  # a launch that broke is recorded, never retried here
        shutil.rmtree(where.parent, ignore_errors=True)
        return {"cell": cell, "arm": arm, "error": str(exc)[:300]}
    wall = time.monotonic() - began
    judged = kinds.judge_refactor(where)
    # Fixed in the source: the suite passes, the prices are the engine's, and no test was touched to get there.
    # No test line was taken away (the planted bug breaks the suite, so a worker cannot pass by editing a test
    # out); a test the worker added for the bug is its work, never a miss: a run on opus/high that fixed the
    # rounding and added a regression test was judged wrong under the earlier "no test changed".
    stat = kinds.subprocess.run(["git", "diff", "--numstat", "HEAD", "--", "tests"], cwd=where,
                                capture_output=True, text=True).stdout
    tests_kept = all(line.split("\t")[1] in ("0", "-") for line in stat.splitlines() if line.strip())
    right = judged["checks"].get("prices kept", False) and kinds._tests_pass(where) and tests_kept
    row = {"cell": cell, "arm": arm, "wall_s": round(wall, 1), "turns": r.turns, "cost": round(r.cost_usd, 4),
           "claimed": r.passed, "right": bool(right)}
    shutil.rmtree(where.parent, ignore_errors=True)
    return row


def report() -> None:
    rows = [json.loads(x) for x in OUT.read_text().splitlines() if x.strip() and "error" not in x]
    print(f"{'cell':<15}{'arm':<9}{'n':>3}{'right':>8}{'wall p50':>10}{'turns':>7}{'$ p50':>8}")
    for cell, arm in sorted({(r["cell"], r["arm"]) for r in rows}):
        rs = [r for r in rows if r["cell"] == cell and r["arm"] == arm]
        print(f"{cell:<15}{arm:<9}{len(rs):>3}{sum(r['right'] for r in rs):>5}/{len(rs):<2}"
              f"{median(r['wall_s'] for r in rs):>10.0f}{median(r['turns'] for r in rs):>7}"
              f"{median(r['cost'] for r in rs):>8.3f}")


def main(argv: list[str]) -> None:
    if argv[0] == "report":
        report()
        return
    cells = argv[1].split(",")
    reps = int(argv[2]) if len(argv) > 2 else 3
    jobs = [(c, a) for _ in range(reps) for c in cells for a in ("with", "without")]

    def go(job: tuple[str, str]) -> None:
        row = one(*job)
        with OUT.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    with ThreadPoolExecutor(int(argv[3]) if len(argv) > 3 else 4) as pool:
        list(pool.map(go, jobs))


if __name__ == "__main__":
    main(sys.argv[1:])
