"""How long each tool call took, as Claude Code's PostToolUse says (`duration_ms`): kept on the call's
record, and where the workers' tool time went shown by `locks stats`."""
from __future__ import annotations

from locks import analysis, habits, signature
from locks.store import Store

from .test_analysis import seed


def test_a_call_keeps_the_time_claude_code_measured_and_never_a_made_up_one():
    timed = signature.entry({"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "pytest -q"},
                             "duration_ms": 1234}, {})
    assert timed["ms"] == 1234
    for odd in (None, "12", True, -5):
        assert "ms" not in signature.entry({"session_id": "s", "tool_name": "Read", "duration_ms": odd}, {})


def test_the_time_reaches_the_store_and_the_workers_tool_time_is_shown(store: Store, _isolated_home, git_repo,
                                                                      tmp_path):
    env = {"LOCKS_HOME": str(_isolated_home), "LOCKS_WORKER_TASK": "7", "LOCKS_WORKER_ATTEMPT": "1"}
    for ms in (30_000, 90_000):
        signature.append({"session_id": "w", "tool_name": "Bash", "tool_input": {"command": "pytest -q"},
                          "duration_ms": ms}, env)
    signature.append({"session_id": "w", "tool_name": "Read", "tool_input": {"file_path": "a.py"},
                      "duration_ms": 5}, env)
    # The verifier's calls are kept under no attempt: they are not the worker's time.
    signature.append({"session_id": "v", "tool_name": "Bash", "tool_input": {"command": "pytest -q"},
                      "duration_ms": 999_000}, {**env, "LOCKS_WORKER_ATTEMPT": ""})
    assert habits.load_events(store, env) == 4
    rows = store.tool_time([7])
    assert rows[0]["calls"] == 2 and rows[0]["ms"] == 120_000 and rows[0]["longest_ms"] == 90_000
    assert rows[1]["ms"] == 5 and store.tool_time([8]) == []
    seed(git_repo, tmp_path, store)
    report = analysis.compute(store, days=0, keep_runs=None)
    assert report["tool_time"] == []  # the seeded runs' workers kept no timed call
    shown = analysis.text({**report, "tool_time": rows})
    assert "where the workers' tool calls spent their time" in shown and "bash:pytest" in shown
    assert "2.0m" in shown and "longest 90s" in shown


def test_a_call_longer_than_its_attempt_is_a_machine_that_slept_and_is_left_out(store: Store, git_repo, tmp_path):
    task = store.create_task("do it", status="done", source="cli", kind="implement")
    tid = int(task["id"])
    store._conn.execute("INSERT INTO attempts (task_id, seq, cell, max_turns, passed, duration_s, created_at) "
                        "VALUES (?, 1, 'sonnet/medium', 10, 1, 600, '2026-01-01')", (tid,))
    for sig, ms in (("bash:sed", 31_000_000), ("bash:pytest", 120_000)):
        store.add_tool_event(session_id="w", task_id=tid, attempt=1, sig=sig, arg_hash="h", ok=1, ms=ms)
    rows = store.tool_time([tid])
    assert [r["sig"] for r in rows] == ["bash:pytest"] and store.tool_time_impossible([tid]) == 1
    seed(git_repo, tmp_path, store)
    report = analysis.compute(store, days=0, keep_runs=None)
    shown = analysis.text({**report, "tool_time": rows, "tool_time_impossible": 1})
    assert "1 call(s) left out" in shown
