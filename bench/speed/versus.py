"""locks against one plain Claude Code session, on the same build.

Real: each run is a fresh copy of the pricing engine (`build.repository`) and the same request
(`build.ASK`, `build.HARD` added for `--hard`). The hidden tests the workers never see judge the
result (`build.hidden`). Arms:

- `opus/low`, `opus/medium`, `sonnet/high`, ...: one `claude -p` at that model and effort, the
  request as its whole prompt, nothing of locks around it;
- `karpathy:<cell>`: the same session with `andrej-karpathy-skills`' CLAUDE.md (behavioural
  guidelines: think first, simplicity, surgical changes, verifiable goals) committed in the copy,
  where Claude Code reads it as the project's instructions (`KARPATHY`, a clone of that repository);
- `omega`: the "Omega" orchestrator-and-workers recipe (`OMEGA_CLAUDE_MD`, `OMEGA_AGENTS`): one
  `claude -p` on Opus at xhigh that plans and checks, Haiku subagents that build and review, every
  brief with a "Done when:" check the boss reruns, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=40`;
- `locks`: `locks run <request> --verify 'make test'` in the copy, with a locks home of its own
  (no history: every start comes from locks' rules), the work judged on the branch it kept. Its
  wall time ends when the command exits; a late independent check (`late_verifier`) that runs after
  it is waited for and kept apart (`late_s`, `late_passed`), never in the wall time.
  `-noverify` (no independent check) and `-baseline` (`engineering-baseline` put back in the writing
  roles, which no longer carry it: it was slower, so the old default stays measurable) are suffixes of a
  locks arm; a row whose arm says `-nobaseline` is history from when the baseline was the default;
  every locks row carries the size of the system prompt its first worker was briefed with (`system_chars`).

The wall time is the whole command's, from start to its exit; the cost is what the CLI reports
(for locks, the task's: classification, attempts and the independent check).

    python bench/speed/versus.py run locks,opus/low,opus/medium 3 3 [--hard]
    python bench/speed/versus.py report
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

import build  # noqa: E402

from locks import __version__, roles  # noqa: E402
from locks.matrix import READ_ONLY_KINDS  # noqa: E402

OUT = HERE / "results-versus.jsonl"
#: A clone of github.com/multica-ai/andrej-karpathy-skills, for the `karpathy:` arms.
KARPATHY = Path(os.environ.get("KARPATHY_SKILLS", "/home/user/multica-ai/andrej-karpathy-skills"))
TOOLS = ("Bash(make:*)", "Bash(python3:*)", "Bash(git:*)", "Read", "Edit", "Write", "Glob", "Grep")


def ask(variant: str) -> str:
    return build.ASK.replace("\n\nAdd tests", build.HARD + "\n\nAdd tests") if variant == "hard" else build.ASK


def plain(cell: str, where: Path, request: str) -> dict:
    model, effort = cell.split("/")
    done = subprocess.run(["claude", "-p", "--model", model, "--effort", effort, "--output-format", "json",
                           "--permission-mode", "acceptEdits", "--max-turns", str(build.MAX_TURNS),
                           "--allowedTools", *TOOLS],
                          input=request, cwd=where, capture_output=True, text=True, timeout=3600)
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        return {"error": (done.stderr or done.stdout)[-300:]}
    return {"cost": round(float(reply.get("total_cost_usd") or 0), 4), "turns": reply.get("num_turns")}


#: The "Omega" setup (an orchestrator-and-workers recipe published for Claude Code): the session runs
#: Opus at xhigh effort as the boss, which plans, writes a brief with a "Done when:" check for each
#: worker and reruns that check itself; the workers are Haiku subagents; context compacts at 40%.
#: Written into the copy as that recipe's prompt would have it set up, committed, before the run.
OMEGA_CLAUDE_MD = """# tally

A small pricing engine: rules price a cart for a customer at checkout.

## Rules

- You (the main session) are the orchestrator. You plan, write briefs and check results. You don't do
  bulk work yourself when a worker can do it.
- Workers are subagents on Haiku. Every brief states the goal, the files to read, and a "Done when:"
  check that can be run or seen.
- Before accepting any worker's output, run its check yourself. If it fails, send it back with the
  failure. Never pass on a worker's report unchecked.
- Start of every session: read CLAUDE.md and the three docs. End of every session: update
  progress.md, add new decisions and gotchas.

## Map

CLAUDE.md               what this project is, the rules, where everything lives (read first)
docs/progress.md        where we are: done, next, a dated log line per session
docs/decisions.md       what's settled, with the date and why
docs/known-issues.md    gotchas and their fixes
.claude/agents/         worker agents, each with model: haiku and one job
src/tally/              the engine; tests/ its tests; `make test` runs them
"""
OMEGA_AGENTS = {
    "builder.md": """---
name: builder
description: Writes code to a brief, runs the brief's check, reports what it ran.
model: haiku
---

You get a brief with a goal, the files to read and a "Done when:" check.
Do the work, run the check, and report the exact command you ran and its output.
If the check fails, say so. Don't report done until it passes.
""",
    "reviewer.md": """---
name: reviewer
description: Reads a diff against a brief and lists what fails it.
model: haiku
---

You get a brief and a diff. Read the diff against the brief, run its "Done when:" check,
and list every point where the change fails the brief. Change nothing.
""",
}
OMEGA_DOCS = {"progress.md": "# Progress\n", "decisions.md": "# Decisions\n", "known-issues.md": "# Known issues\n"}
#: The tools the Omega session gets: the plain arm's plus the one that starts a subagent (`Agent`,
#: once called `Task`).
OMEGA_TOOLS = (*TOOLS, "Agent", "Task")


def omega_setup(where: Path) -> None:
    (where / "CLAUDE.md").write_text(OMEGA_CLAUDE_MD)
    for name, text in OMEGA_AGENTS.items():
        (where / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
        (where / ".claude" / "agents" / name).write_text(text)
    for name, text in OMEGA_DOCS.items():
        (where / "docs").mkdir(exist_ok=True)
        (where / "docs" / name).write_text(text)
    subprocess.run(["git", "add", "-A"], cwd=where, check=True)
    subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "--amend", "-qm", "init"],
                   cwd=where, check=True)


def omega(where: Path, request: str) -> dict:
    """`omega`: one `claude -p` on Opus at xhigh, the Omega files in the copy, its Haiku subagents
    counted in the CLI's own cost and `modelUsage`; locks' hooks off, as a program's session."""
    env = {**os.environ, "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "40", "LOCKS_HOOKS_OFF": "1"}
    done = subprocess.run(["claude", "-p", "--model", "opus", "--effort", "xhigh", "--output-format", "json",
                           "--permission-mode", "acceptEdits", "--max-turns", str(build.MAX_TURNS),
                           "--allowedTools", *OMEGA_TOOLS],
                          input=request, cwd=where, env=env, capture_output=True, text=True, timeout=3600)
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        return {"error": (done.stderr or done.stdout)[-300:]}
    usage = reply.get("modelUsage") or {}
    by_model = {name: round(float(u.get("costUSD") or 0), 4) for name, u in usage.items()}
    return {"cost": round(float(reply.get("total_cost_usd") or 0), 4), "turns": reply.get("num_turns"),
            "by_model": by_model, "subagents": any("haiku" in name for name in by_model)}


#: The suffixes `locks` reads off a locks arm's name (`locks@opus/low-noverify-baseline`).
SUFFIXES = ("-noverify", "-baseline")
#: The skill `-baseline` puts back in the writing roles: the precedence rule and the general rules.
BASELINE = "engineering-baseline"


def shadow_roles(home: Path) -> list[str]:
    """`-baseline`: under the locks home's `roles/`, a copy of every bundled writing role (one with a kind
    outside `READ_ONLY_KINDS`), with `BASELINE` put first on its `skills:` line and nothing else changed. A
    person's role of a bundled name shadows the bundled one (`roles.load`), so the worker's system prompt is
    the same but for that skill. Returns the names written."""
    folder = home / "roles"
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for path in sorted(roles.bundled_dir().glob("*.md")):
        text = path.read_text(encoding="utf-8")
        fields, _ = roles.split_frontmatter(text)
        if not set(fields.get("kinds") or ()) - READ_ONLY_KINDS:
            continue  # a reading role keeps what it has: the arm is about the writing workers
        (folder / path.name).write_text(with_skill(text, BASELINE), encoding="utf-8")
        written.append(fields["name"])
    return written


def with_skill(text: str, skill: str) -> str:
    """A role's text with `skill` first on its frontmatter's one-line `skills:` list, every other line kept
    as it was; a role with no `skills:` line gets one after its `kinds:` line. A role that already names it,
    or whose list is not one line, is an error, never a guess."""
    lines = text.splitlines(keepends=True)
    end = next(i for i, line in enumerate(lines) if i and line.strip() == "---")
    names = list(roles.split_frontmatter(text)[0].get("skills") or [])
    at = [i for i in range(1, end) if lines[i].startswith("skills:")]
    if skill in names or len(at) > 1 or (at and not lines[at[0]].rstrip().endswith("]")):
        raise ValueError(f"`{skill}` is already on this role or its `skills:` is not one line: {names}")
    line = f"skills: [{', '.join([skill, *names])}]\n"
    if at:
        lines[at[0]] = line
        return "".join(lines)
    kinds = [i for i in range(1, end) if lines[i].startswith("kinds:")]
    if len(kinds) != 1:
        raise ValueError("this role has no single `kinds:` line to put `skills:` after")
    lines.insert(kinds[0] + 1, line)
    return "".join(lines)


def system_chars(db: sqlite3.Connection, task_id: int) -> int | None:
    """The system prompt's size the task's first `briefed` event recorded; None when no worker was briefed."""
    row = db.execute("SELECT data FROM events WHERE task_id = ? AND kind = 'briefed' ORDER BY id LIMIT 1",
                     (task_id,)).fetchone()
    return json.loads(row[0]).get("system_chars") if row else None


def locks(where: Path, request: str, arm: str = "locks") -> dict:
    """`locks`, `locks@<cell>` (a pinned start), and either with `-noverify` (no independent check) or
    `-baseline` (the writing roles with `BASELINE` put back), in either order."""
    home = Path(tempfile.mkdtemp(prefix="locks-home-"))
    env = {**os.environ, "LOCKS_HOME": str(home), "LOCKS_AUTOWORK": "off", "LOCKS_NAMES": "off"}
    env.pop("LOCKS_SESSION_ID", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    suffixes = set()
    while tail := next((s for s in SUFFIXES if arm.endswith(s)), None):
        suffixes.add(tail)
        arm = arm.removesuffix(tail)
    if "-noverify" in suffixes:
        env["LOCKS_VERIFIER"] = "off"
    if "-baseline" in suffixes:
        shadow_roles(home)  # before the run: a worker reads its role when it is briefed
    start = ["--start", arm.split("@", 1)[1]] if "@" in arm else []
    done = subprocess.run([str(ROOT / "bin" / "locks"), "run", request, "--verify", "make test", *start], cwd=where,
                          env=env, capture_output=True, text=True, timeout=7200)
    ended = time.monotonic()
    db = sqlite3.connect(home / "locks.db")
    task = db.execute("SELECT id, status, start_cell, final_cell, cost_usd FROM tasks WHERE source = 'locks' "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    attempts = db.execute("SELECT COUNT(*) FROM attempts WHERE task_id = ?", (task[0],)).fetchone()[0] if task else 0
    late = waited_late(db, task[0]) if task else {}
    if late:
        late["late_s"] = round(time.monotonic() - ended, 1)
        task = db.execute("SELECT id, status, start_cell, final_cell, cost_usd FROM tasks WHERE id = ?",
                          (task[0],)).fetchone()
    chars = system_chars(db, task[0]) if task else None
    db.close()
    shutil.rmtree(home, ignore_errors=True)
    if not task:
        return {"error": (done.stderr or done.stdout)[-300:], "system_chars": chars}
    branch = f"locks/task-{task[0]}"
    if subprocess.run(["git", "rev-parse", "--verify", "-q", branch], cwd=where, capture_output=True).returncode == 0:
        subprocess.run(["git", "checkout", "-q", branch], cwd=where, check=True)
    return {"cost": round(float(task[4] or 0), 4), "status": task[1], "start": task[2], "final": task[3],
            "attempts": attempts, "system_chars": chars, **late}


def waited_late(db: sqlite3.Connection, task_id: int, within_s: float = 900) -> dict:
    """A late independent check (`late_verifier`) runs after `locks run` exits: wait for its verdict, so
    the locks home is not removed under it, and keep what it said apart from the foreground wall time."""
    if not db.execute("SELECT 1 FROM events WHERE task_id = ? AND kind = 'verifier_deferred'",
                      (task_id,)).fetchone():
        return {}
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        row = db.execute("SELECT data FROM events WHERE task_id = ? AND kind = 'verified' ORDER BY id DESC LIMIT 1",
                         (task_id,)).fetchone()
        if row and json.loads(row[0]).get("late"):
            data = json.loads(row[0])
            return {"late_passed": data.get("passed"), "late_blocking": data.get("blocking")}
        time.sleep(2)
    return {"late_passed": None}


def diffstat(where: Path) -> dict:
    """What the work changed against the commit it started from, all of it (new files too)."""
    subprocess.run(["git", "add", "-A"], cwd=where, check=True)
    base = subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=where, capture_output=True,
                          text=True).stdout.split()[0]
    out = subprocess.run(["git", "diff", "--cached", "--numstat", base], cwd=where, capture_output=True,
                         text=True).stdout.splitlines()
    rows = [line.split("\t") for line in out if line.count("\t") >= 2]
    added = sum(int(a) for a, _, _ in rows if a.isdigit())
    removed = sum(int(r) for _, r, _ in rows if r.isdigit())
    tests = sum(int(a) for a, _, f in rows if a.isdigit() and "test" in f)
    return {"files": len(rows), "added": added, "removed": removed, "added_tests": tests}


#: The medium build as the two tasks a session would send side by side: files apart, merged after.
SPLIT = (
    ("feature", "Add pricing rules: loyalty members get their tier's discount on the order (gold 10%, silver "
     "5%); cart lines tagged promo get 15% off; a line of 10 or more of one item gets 5% off that line. Add tests "
     "for them."),
    ("implement", "Add a printed receipt, `tally.receipt.receipt(cart, customer) -> str`, one row per line and "
     "one per discount the checkout applies (with its sign) and then the total, and a CSV export of an order for "
     "finance, `tally.export.rows(cart, customer) -> list[str]`, a header row, one row per line (item, qty, unit "
     "price, line total) and a total row. Take the discounts from the checkout as it applies them; do not add or "
     "change pricing rules. Add tests for them."),
)


def split(where: Path) -> dict:
    """`locks-split`: the two tasks of `SPLIT` run at once, each with the kind a session gives it, in one locks
    home; their branches merged into the checkout after (a conflict is recorded, nothing resolved)."""
    home = Path(tempfile.mkdtemp(prefix="locks-home-"))
    env = {**os.environ, "LOCKS_HOME": str(home), "LOCKS_AUTOWORK": "off", "LOCKS_NAMES": "off"}
    env.pop("LOCKS_SESSION_ID", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    runs = [subprocess.Popen([str(ROOT / "bin" / "locks"), "run", text, "--verify", "make test", "--kind", kind],
                             cwd=where, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for kind, text in SPLIT]
    for run in runs:
        run.wait(timeout=7200)
    db = sqlite3.connect(home / "locks.db")
    tasks = db.execute("SELECT id, status, cost_usd FROM tasks WHERE source = 'locks' ORDER BY id").fetchall()
    db.close()
    shutil.rmtree(home, ignore_errors=True)
    git = ["git", "-c", "user.email=b@b", "-c", "user.name=b"]
    conflict = False
    for task_id, _, _ in tasks:
        merged = subprocess.run([*git, "merge", "--no-ff", "-q", "-m", f"task {task_id}", f"locks/task-{task_id}"],
                                cwd=where, capture_output=True, text=True)
        if merged.returncode != 0:
            conflict = True
            subprocess.run(["git", "merge", "--abort"], cwd=where, capture_output=True)
    return {"cost": round(sum(float(c or 0) for _, _, c in tasks), 4), "status": [s for _, s, _ in tasks],
            "conflict": conflict}


def one(arm: str, variant: str) -> dict:
    where = build.repository(variant)
    if arm.startswith("karpathy:") or arm.endswith("+karpathy"):
        shutil.copy(KARPATHY / "CLAUDE.md", where / "CLAUDE.md")
        subprocess.run(["git", "add", "CLAUDE.md"], cwd=where, check=True)
        subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "--amend", "-qm", "init"],
                       cwd=where, check=True)
    if arm == "omega":
        omega_setup(where)
    began = time.monotonic()
    try:
        got = (omega(where, ask(variant)) if arm == "omega"
               else split(where) if arm == "locks-split"
               else locks(where, ask(variant), arm.removesuffix("+karpathy")) if arm.startswith("locks")
               else plain(arm.removeprefix("karpathy:"), where, ask(variant)))
    except subprocess.TimeoutExpired:
        got = {"error": "timed out"}
    # The wall time ends when the command exits: a late check waited for after it is `late_s`.
    wall = time.monotonic() - began - float(got.get("late_s") or 0)
    visible = subprocess.run(["make", "test"], cwd=where, capture_output=True, text=True, timeout=600).returncode == 0
    label = arm.replace("locks", f"locks {__version__}", 1) if arm.startswith("locks") else arm
    row = {"arm": label, "variant": variant, "wall_s": round(wall, 1), "foreground": True,
           "visible": visible,
           "hidden": build.hidden(where, variant), "of": 10 if variant == "hard" else 7, **got,
           **diffstat(where)}
    shutil.rmtree(where.parent, ignore_errors=True)
    return row


def main(argv: list[str]) -> None:
    variant = "hard" if "--hard" in argv else "plain"
    argv = [a for a in argv if a != "--hard"]
    if argv[0] == "report":
        rows = [json.loads(x) for x in OUT.read_text().splitlines() if x.strip()]
        for r in rows:
            r["arm"] = "locks 0.49.0" if r["arm"] == "locks" else r["arm"]  # rows from before versions were kept
            if r.get("late_s") and not r.get("foreground"):  # rows whose wall time held the late wait
                r["wall_s"] = round(r["wall_s"] - r["late_s"], 1)
        print(f"{'build':<7}{'arm':<34}{'n':>3}{'right':>8}{'hidden':>9}{'wall p50':>10}{'mean':>7}{'$ p50':>8}"
              f"{'$ mean':>8}{'+lines p50':>12}{'-lines p50':>12}")
        for key in sorted({(r["variant"], r["arm"]) for r in rows}):
            rs = [r for r in rows if (r["variant"], r["arm"]) == key]
            right = sum(r["hidden"] == r["of"] for r in rs)
            costs = [r.get("cost", 0) for r in rs]
            plus = [r["added"] for r in rs if "added" in r]
            minus = [r["removed"] for r in rs if "removed" in r]
            print(f"{key[0]:<7}{key[1]:<34}{len(rs):>3}{right:>5}/{len(rs):<2}"
                  f"{sum(r['hidden'] for r in rs):>5}/{sum(r['of'] for r in rs):<3}"
                  f"{median(r['wall_s'] for r in rs):>10.0f}{sum(r['wall_s'] for r in rs) / len(rs):>7.0f}"
                  f"{median(costs):>8.2f}{sum(costs) / len(costs):>8.2f}"
                  f"{(median(plus) if plus else float('nan')):>12.0f}"
                  f"{(median(minus) if minus else float('nan')):>12.0f}")
        return
    arms, reps = argv[1].split(","), int(argv[2]) if len(argv) > 2 else 3
    jobs = [a for _ in range(reps) for a in arms]

    def run(arm: str) -> None:
        row = one(arm, variant)
        with OUT.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    with ThreadPoolExecutor(int(argv[3]) if len(argv) > 3 else 3) as pool:
        list(pool.map(run, jobs))


if __name__ == "__main__":
    main(sys.argv[1:])
