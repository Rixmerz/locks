"""scripts/demo.py: the README's demo data is written by locks' own commands, and holds no state
the store should never hold."""
import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _demo():
    spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_demo_data_has_every_kind_of_ending_and_a_healthy_store(tmp_path):
    env = _demo().make(tmp_path / "demo")
    db = Path(env["LOCKS_HOME"]) / "locks.db"
    con = sqlite3.connect(db)
    statuses = {r[0] for r in con.execute("SELECT status FROM tasks")}
    repos = {Path(r[0]).name for r in con.execute("SELECT DISTINCT cwd FROM tasks")}
    runs = con.execute("SELECT count(*) FROM workflow_runs").fetchone()[0]
    costs = [r[0] for r in con.execute("SELECT cost_usd FROM attempts")]
    con.close()
    assert {"done", "blocked"} <= statuses
    assert {"shop-api", "web-dashboard"} <= repos
    assert runs == 1
    assert min(costs) > 0.01  # spread to a real worker's size, never the fake's
    doctor = subprocess.run([sys.executable, "-m", "locks", "doctor"], env=env, capture_output=True, text=True)
    assert "nothing broken" in doctor.stdout, doctor.stdout
