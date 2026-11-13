#!/usr/bin/env python3
"""Demo data for the README's screenshots of `locks ui`, with generic project names.

It runs locks' real commands, as processes, in two small repositories it makes, with the
scenario tests' fake `claude` (`tests/scenarios/fake_claude.py`) answering: every task, attempt,
stop and workflow run is one locks wrote itself. The fake answers in seconds and costs nothing,
so the records are spread afterwards over two weeks, with costs and turns of the size real
workers have, and models by cell: only times, costs and turns are rewritten, never a state.

    python scripts/demo.py DIR             # a locks home and the repositories under DIR
    python scripts/demo.py DIR --shots     # and the screenshots, into docs/images/

Nothing outside DIR is touched (`LOCKS_HOME` is DIR/home); no money is spent.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "scenarios" / "fake_claude.py"
SHOTS = ROOT / "docs" / "images"
#: What each cell's attempt is made to cost and take, of the size real ones did here.
CELL_COST = {"haiku": 0.04, "sonnet": 0.35, "opus": 1.1}
#: The screens the README shows: file name, the view, the colour scheme.
SCREENS = [("board.png", "board", "light"), ("board-dark.png", "board", "dark"),
           ("analysis.png", "analysis", "light"),
           ("workflows.png", "workflows", "light"), ("map.png", "map", "light"), ("routing.png", "routing", "light"),
           ("spend.png", "spend", "light"), ("teams.png", "teams", "light")]

SHOP = {
    "pricing.py": "def total(items):\n    return sum(i['price'] * i['qty'] for i in items)\n",
    "test_pricing.py": ("from pricing import total\n\n\ndef test_total():\n"
                        "    assert total([{'price': 2, 'qty': 3}]) == 6\n"),
}
WEB = {
    "src/chart.ts": "export const series = (xs: number[]) => xs.map((x, i) => ({ x: i, y: x }));\n",
    "src/table.ts": "export const rows = (xs: string[]) => xs.map((x) => ({ label: x }));\n",
}
#: The work asked of each repository: (request, its kind, how the fake worker answers, a check or None).
SHOP_WORK = [
    ("Add a discount rule for orders over 100: 5% off the total, with its test", "implement",
     [{"pass": True, "write": {"discounts.py": "def discount(t):\n    return t * 0.95 if t > 100 else t\n"}}], True),
    ("Fix the rounding of totals with three decimals in pricing.py", "debug-repro",
     [{"pass": False}, {"pass": True, "write": {"pricing.py": SHOP["pricing.py"] + "\n# rounded\n"}}], True),
    ("Explain how the tax rules are applied across pricing.py and the checkout", "explore",
     [{"pass": True}], False),
    ("Add an invoice export as CSV with a header row", "implement",
     [{"turns": True, "write": {"invoice.py": "# csv\n"}}], True),
    ("Rename total() to order_total() everywhere and keep the tests passing", "refactor",
     [{"pass": True, "write": {"pricing.py": SHOP["pricing.py"].replace("total", "order_total")}}], True),
    ("Review the refund flow for missing validation", "review", [{"pass": True}], False),
]
WEB_WORK = [
    ("Add a legend to the revenue chart with one entry per series", "implement",
     [{"pass": True, "write": {"src/legend.ts": "export const legend = (n: string[]) => n;\n"}}], False),
    ("Sort the orders table by date, newest first", "implement",
     [{"pass": True, "write": {"src/sort.ts": "export {};\n"}}], False),
    ("Plan the move of the dashboard from polling to server-sent events", "plan", [{"pass": True}], False),
]


def sh(cwd: Path, *argv: str) -> None:
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True)


def repository(at: Path, files: dict[str, str]) -> Path:
    at.mkdir(parents=True)
    for name, text in files.items():
        (at / name).parent.mkdir(parents=True, exist_ok=True)
        (at / name).write_text(text)
    sh(at, "git", "init", "-q", "-b", "main")
    sh(at, "git", "-c", "user.name=demo", "-c", "user.email=demo@example.invalid", "add", ".")
    sh(at, "git", "-c", "user.name=demo", "-c", "user.email=demo@example.invalid", "commit", "-qm", "start")
    (at / ".locks").mkdir()
    return at


def environment(base: Path) -> dict[str, str]:
    bin_dir = base / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    (bin_dir / "claude").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LOCKS_", "CLAUDE"))}
    return {**env, "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}", "PYTHONPATH": str(ROOT / "src"),
            "LOCKS_HOME": str(base / "home"), "LOCKS_AUTOWORK": "off", "LOCKS_LIVESPEC": "off",
            "LOCKS_VERIFIER": "off", "LOCKS_LATE_VERIFIER": "off", "LOCKS_NOTES": "off", "LOCKS_NAMES": "off",
            "LOCKS_HABIT_REVIEW": "off", "LOCKS_BROWSER_CHECK": "off", "LOCKS_AUTOSPLIT": "off",
            "LOCKS_SCRIPTED_CHECK": "off", "CLAUDE_CONFIG_DIR": str(base / "claude"),
            "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.invalid",
            "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@example.invalid",
            "FAKE_CLAUDE_SCRIPT": str(base / "script.json"), "FAKE_CLAUDE_LOG": str(base / "calls.jsonl")}


def run(env: dict, cwd: Path, request: str, kind: str, answers: list[dict], check: str | None) -> None:
    Path(env["FAKE_CLAUDE_SCRIPT"]).write_text(json.dumps([{"match": ".", "do": answers}]))
    argv = [sys.executable, "-m", "locks", "run", request, "--whole", "--kind", kind]
    if check:
        argv += ["--verify", check]
    subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)


def settled(db: Path, *, within_s: float = 300) -> None:
    """Wait until no task is queued or running: the workflow's steps follow each other on the dispatcher."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        con = sqlite3.connect(db)
        busy = con.execute("SELECT count(*) FROM tasks WHERE status IN ('queued', 'running')").fetchone()[0]
        con.close()
        if not busy:
            return
        time.sleep(1)


def spread(db: Path, *, days: int = 14, seed: int = 7) -> None:
    """Times over the last `days`, costs and turns of a real worker's size, by the cell each ran on."""
    rng = random.Random(seed)  # noqa: S311 - demo figures, no secret
    con = sqlite3.connect(db)
    now = datetime.now(UTC)
    tasks = [r[0] for r in con.execute("SELECT id FROM tasks ORDER BY id")]
    for n, task in enumerate(tasks):
        start = now - timedelta(days=days * (len(tasks) - n) / max(1, len(tasks)), minutes=rng.randint(0, 600))
        stamp = start.strftime("%Y-%m-%dT%H:%M:%S")
        total, costs = 0.0, {}
        for seq, cell in con.execute("SELECT seq, cell FROM attempts WHERE task_id = ?", (task,)).fetchall():
            cost = round(CELL_COST.get(str(cell).split("/")[0], 0.3) * rng.uniform(0.5, 1.8), 4)
            total += cost
            costs[seq] = cost
            con.execute("UPDATE attempts SET cost_usd = ?, turns = ?, created_at = ? WHERE task_id = ? AND seq = ?",
                        (cost, rng.randint(6, 34), stamp, task, seq))
        con.execute("UPDATE tasks SET cost_usd = ?, created_at = ?, updated_at = ? WHERE id = ?",
                    (round(total, 4), stamp, stamp, task))
        con.execute("UPDATE events SET ts = ? WHERE task_id = ?", (stamp, task))
        # Every timed phase grows by one factor, so the phases still add up to the run's wall time.
        factor = rng.uniform(40, 160)
        for eid, kind, data in con.execute("SELECT id, kind, data FROM events WHERE task_id = ?", (task,)).fetchall():
            body = json.loads(data or "{}")
            for key, value in list(body.items()):
                if key.endswith("_s") and isinstance(value, (int, float)):
                    body[key] = round(value * factor, 1)
            if kind == "attempt_finished" and "worker_cost_usd" in body:
                body["worker_cost_usd"] = costs.get(body.get("seq"), body["worker_cost_usd"])
            if "cost_usd" in body:
                body["cost_usd"] = (costs.get(body.get("attempt") or body.get("seq"), body["cost_usd"])
                                    if kind == "attempt_finished" else round(total, 4))
            con.execute("UPDATE events SET data = ? WHERE id = ?", (json.dumps(body), eid))
    con.commit()
    con.close()


def make(base: Path) -> dict[str, str]:
    base.mkdir(parents=True, exist_ok=True)
    env = environment(base)
    # The board names a project by its folder and the one above it: "work/shop-api".
    shop = repository(base / "work" / "shop-api", SHOP)
    web = repository(base / "work" / "web-dashboard", WEB)
    for cwd, work in ((shop, SHOP_WORK), (web, WEB_WORK)):
        for request, kind, answers, checked in work:
            run(env, cwd, request, kind, answers, f"{sys.executable} -m pytest -q" if checked else None)
    Path(env["FAKE_CLAUDE_SCRIPT"]).write_text(json.dumps([{"match": ".", "do": [{"pass": True}]}]))
    subprocess.run([sys.executable, "-m", "locks", "workflow", "run", "bugfix",
                    "Checkout fails for carts with a single free item"], cwd=shop, env=env,
                   capture_output=True, timeout=120)
    subprocess.run([sys.executable, "-m", "locks", "work"], cwd=shop, env=env, capture_output=True, timeout=600)
    settled(Path(env["LOCKS_HOME"]) / "locks.db")
    subprocess.run([sys.executable, "-m", "locks", "note", "Prices are kept in cents everywhere",
                    "--topic", "pricing"], cwd=shop, env=env, capture_output=True, timeout=60)
    spread(Path(env["LOCKS_HOME"]) / "locks.db")
    return env


#: Takes the screenshots with Playwright for Node (the browser a machine has for its tests): one page per
#: screen, its view's tab clicked, at twice the pixels.
SHOOT_JS = """
const { chromium } = require('playwright');
(async () => {
  const [url, out, screens] = [process.argv[2], process.argv[3], JSON.parse(process.argv[4])];
  const browser = await chromium.launch();
  for (const [name, view, scheme] of screens) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2,
                                         colorScheme: scheme });
    await page.goto(url);
    await page.waitForTimeout(1200);
    await page.click(`button[data-view="${view}"]`);
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `${out}/${name}` });
    await page.close();
  }
  await browser.close();
})();
"""


def shots(env: dict, out: Path, screens: list = SCREENS, port: int = 8977) -> None:
    server = subprocess.Popen([sys.executable, "-m", "locks", "ui", "--port", str(port)], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2)
        script = Path(env["LOCKS_HOME"]).parent / "shoot.js"
        script.write_text(SHOOT_JS)
        subprocess.run(["node", str(script), f"http://127.0.0.1:{port}/", str(out), json.dumps(screens)],
                       check=True, timeout=300)
    finally:
        server.terminate()
        server.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dir", type=Path, help="an empty folder for the demo's locks home and repositories")
    parser.add_argument("--shots", action="store_true", help="take the README's screenshots into docs/images/")
    args = parser.parse_args(argv)
    if args.dir.exists() and any(args.dir.iterdir()):
        parser.error(f"{args.dir} is not empty")
    env = make(args.dir.resolve())
    print(f"demo data in {env['LOCKS_HOME']}; see it with: LOCKS_HOME={env['LOCKS_HOME']} locks ui")
    if args.shots:
        shots(env, SHOTS)
        print(f"screenshots in {SHOTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
