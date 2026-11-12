"""One real run of each bundled workflow the speed benches never start, on the bench repository (real money: run
on purpose). Each has its own locks home and a fresh copy of the build with what its workflow reads planted
in it (a bug, a flawed change, a thin design document, documents that drifted, nothing for a TRACER slice in
a repository that keeps no method), is driven to its end as `flow.py` drives a run, and ends with `locks
doctor`. A row per workflow is written beside its store in the folder given: status, each step, cost, the
doctor's words and `locks workflow status`.

    python bench/speed/matrix.py <folder> [bugfix,panel,...]
"""
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

import build  # noqa: E402
import flow  # noqa: E402

from locks.store import Store  # noqa: E402

LOCKS = str(ROOT / "bin" / "locks")


def commit(where, msg):
    subprocess.run(["git", "add", "-A"], cwd=where, check=True)
    subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", msg], cwd=where, check=True)


def plant_bug(where):
    p = where / "src/tally/checkout.py"
    s = p.read_text()
    old = "total += sum(a.cents for a in rule.apply_order(subtotal, cart, customer))"
    assert old in s
    p.write_text(s.replace(old, "total += sum(a.cents for a in rule.apply_order(total, cart, customer))"))
    commit(where, "Tidy the order stage")


def flawed_change(where):
    p = where / "src/tally/checkout.py"
    p.write_text(p.read_text() + '''

def quote(cart, customer, cache={}):
    """The total and the average price per unit of a cart."""
    key = str(cart)
    if key in cache:
        return cache[key]
    cart["lines"].sort(key=lambda line: line["price"])
    total = price(cart, customer)
    units = sum(line["qty"] for line in cart["lines"])
    out = {"total": total, "per_unit": total / units, "customer": customer}
    cache[key] = out
    return out
''')
    commit(where, "Add quote(): a cart's total and its price per unit")


def design_doc(where):
    (where / "docs").mkdir(exist_ok=True)
    (where / "docs/DESIGN.md").write_text("""# Coupons

We will add coupons. A coupon is a code the customer types. It gives a discount.

## Design
A new rule `coupon` reads the code from the cart and applies its discount at some stage. Codes live in a file.
Expired codes are handled. Stacking with other discounts follows the usual policy.

## Rollout
Ship it when ready.
""")
    commit(where, "Draft the coupons design")


def drifted_docs(where):
    (where / "docs").mkdir(exist_ok=True)
    (where / "docs/pricing.md").write_text("""# Pricing

- `tally.checkout.price(cart)` returns the total in dollars as a float.
- Rules are enabled in `rules.yaml`; a rule missing from the registry raises `KeyError`.
- Order rules run before line rules.
- The `loyalty` rule gives 5% to returning customers.
""")
    commit(where, "Document pricing")


TARGET = "the last commit (git show HEAD): quote() in src/tally/checkout.py"
SCENARIOS = {
    "bugfix": (plant_bug, ["--verify", "make test"], "Customers with two order-level discounts (staff plus a "
               "spend tier) are charged a different total than before last week's tidy-up: the second discount "
               "looks computed on the already discounted amount instead of the order's subtotal."),
    "review-fix": (flawed_change, ["--verify", "make test"], TARGET),
    "dual-review": (flawed_change, ["--verify", "make test"], TARGET),
    "panel": (flawed_change, ["--verify", "make test"], TARGET),
    "design-review": (design_doc, ["--input", "doc=docs/DESIGN.md"],
                      "Decide whether the coupons design is ready to build"),
    "drift": (drifted_docs, [], "docs"),
    "tracer-slice": (lambda where: None, ["--verify", "make test"], "Add quote(cart, customer) to tally.checkout: "
                     "it returns the total in cents and the list of adjustments applied, and price() returns "
                     "its total."),
}


def one(name, out):
    setup, flags, text = SCENARIOS[name]
    where = build.repository("plain")
    setup(where)
    home = Path(tempfile.mkdtemp(prefix="locks-home-"))
    env = {**os.environ, "LOCKS_HOME": str(home), "LOCKS_AUTOWORK": "off", "LOCKS_NAMES": "off"}
    for k in ("LOCKS_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        env.pop(k, None)
    began = time.monotonic()
    started = subprocess.run([LOCKS, "workflow", "run", name, text, *flags, "--json"], cwd=where, env=env,
                             capture_output=True, text=True, timeout=600)
    row = {"workflow": name}
    try:
        run_id = int(json.loads(started.stdout.splitlines()[-1])["id"])
    except Exception:
        row["error"] = (started.stderr or started.stdout)[-600:]
        return row
    error, status = flow.drive(env, where, home, run_id, began + 2400)
    row["wall_s"] = round(time.monotonic() - began, 1)
    store = Store(home / "locks.db")
    try:
        row.update(flow.row_of(store, run_id))
        if status:
            row["status"] = status
    finally:
        store.close()
    if error:
        row["error"] = error
    doc = subprocess.run([LOCKS, "doctor"], cwd=where, env=env, capture_output=True, text=True, timeout=300)
    row["doctor"] = (doc.stdout + doc.stderr).strip()[-800:]
    st = subprocess.run([LOCKS, "workflow", "status", str(run_id)], cwd=where, env=env, capture_output=True,
                        text=True, timeout=120)
    row["status_text"] = st.stdout[-2500:]
    out.mkdir(parents=True, exist_ok=True)
    source, target = sqlite3.connect(home / "locks.db"), sqlite3.connect(out / f"{name}.db")
    with target:
        source.backup(target)  # a backup, never a copy: the write-ahead log comes too
    source.close()
    target.close()
    (out / f"{name}.json").write_text(json.dumps(row, indent=1))
    shutil.rmtree(home, ignore_errors=True)
    shutil.rmtree(where.parent, ignore_errors=True)
    return row


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    folder = Path(sys.argv[1])
    names = sys.argv[2].split(",") if len(sys.argv) > 2 else list(SCENARIOS)
    with ThreadPoolExecutor(3) as pool:
        for row in pool.map(lambda name: one(name, folder), names):
            print(json.dumps({k: row.get(k) for k in ("workflow", "status", "wall_s", "cost", "error")}), flush=True)
