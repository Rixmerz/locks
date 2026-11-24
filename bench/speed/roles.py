"""Whether a worker's role and skills earn their place, per cell, and whether opus/low beats the
cells a build starts on (real money: run on purpose).

The harder build (`build.HARD`: rules added, moved and renamed everywhere) on a fresh copy, judged
by its 10 hidden tests. Each arm is one worker through `launch.run`, as locks launches one (lean,
its result contract), with what its system prompt carries:

- `full`: the role locks briefs a feature with (`feature-builder`: its instructions and skills,
  `roles.brief`), as a worker gets it;
- `minimal`: one line, the role's name and description;
- `none`: nothing appended.

    python bench/speed/roles.py run sonnet/medium,sonnet/high,opus/low full,minimal,none 3
    python bench/speed/roles.py report
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))

import build  # noqa: E402

from locks import __version__, grants, launch, roles  # noqa: E402
from locks.matrix import Cell  # noqa: E402

OUT = HERE / "results-roles.jsonl"
ROLE = "feature-builder"


def system(variant: str) -> str:
    role = roles.load().get(ROLE)
    if variant == "full":
        return roles.brief(role).system_prompt
    if variant == "minimal":
        return f"You work as the `{role.name}` role: {role.description}"
    return ""


def one(cell: str, variant: str) -> dict:
    model, effort = cell.split("/")
    where = build.repository("hard")
    ask = build.ASK.replace("\n\nAdd tests", build.HARD + "\n\nAdd tests")
    prompt = system(variant)
    spec = launch.LaunchSpec(prompt=ask, cell=Cell(model, effort), launch_dir=where, max_turns=build.MAX_TURNS,
                             timeout_s=launch.timeout_for(build.MAX_TURNS), permission_mode=grants.mode_for(model),
                             allowed_tools=("Bash(make:*)", "Bash(python3:*)", "Bash(git:*)"),
                             append_system_prompt=prompt)
    began = time.monotonic()
    try:
        r = launch.run(spec)
    except Exception as exc:  # a launch that broke is recorded, never retried here
        shutil.rmtree(where.parent, ignore_errors=True)
        return {"cell": cell, "role": variant, "error": str(exc)[:300]}
    wall = time.monotonic() - began
    visible = subprocess.run(["make", "test"], cwd=where, capture_output=True, text=True, timeout=300).returncode == 0
    row = {"cell": cell, "role": variant, "system_chars": len(prompt), "wall_s": round(wall, 1), "turns": r.turns,
           "cost": round(r.cost_usd, 4), "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
           "claimed": r.passed, "visible": visible, "hidden": build.hidden(where, "hard"), "of": 10,
           "version": __version__}
    shutil.rmtree(where.parent, ignore_errors=True)
    return row


def run(cells: list[str], variants: list[str], n: int, parallel: int = 3) -> None:
    jobs = [(c, v) for _ in range(n) for c in cells for v in variants]
    with ThreadPoolExecutor(parallel) as pool:
        for row in pool.map(lambda job: one(*job), jobs):
            print(json.dumps(row), flush=True)
            with OUT.open("a") as f:
                f.write(json.dumps(row) + "\n")


def report() -> None:
    rows = [json.loads(x) for x in OUT.read_text().splitlines() if x.strip()]
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["cell"], r["role"]), []).append(r)
    print("| Cell | Role | Right | Hidden p50 | Wall p50 | Turns p50 | Cost p50 | Errors |")
    print("|---|---|---|---|---|---|---|---|")
    for (cell, role), rs in sorted(groups.items()):
        ok = [r for r in rs if "error" not in r]
        right = sum(r["hidden"] == r["of"] for r in ok)
        def med(k: str, ok: list[dict] = ok) -> float | None:
            return median(r[k] for r in ok) if ok else None
        print(f"| {cell} | {role} | {right}/{len(ok)} | {med('hidden')} | {med('wall_s')} s | {med('turns')} | "
              f"${med('cost')} | {len(rs) - len(ok)} |")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["run"]:
        run(args[1].split(","), args[2].split(","), int(args[3]))
    else:
        report()
