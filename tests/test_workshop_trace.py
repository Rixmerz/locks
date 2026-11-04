"""The board's terminal reads a worker's calls from the person's own trace, only when it is on."""
from locks import trace


def test_a_worker_s_traced_calls_show_command_and_output(tmp_path):
    env = {"LOCKS_HOME": str(tmp_path), "LOCKS_TRACE": "on", "LOCKS_WORKER_TASK": "7"}
    trace.write(env, "s1", "tool:Bash", phase="PreToolUse", input={"command": "pytest -q"})
    trace.write(env, "s1", "tool:Bash", phase="PostToolUse", input={"command": "pytest -q"},
                response={"stdout": "1 failed", "stderr": "boom"})
    trace.write({**env, "LOCKS_WORKER_TASK": "8"}, "s1", "tool:Read", phase="PostToolUse", input={"file_path": "x"})
    calls = trace.worker_calls(env, "s1", 7)
    assert [(c["tool"], c["input"], c["output"]) for c in calls] == [("Bash", "pytest -q", "1 failed\nboom")]


def test_nothing_shows_with_the_trace_off(tmp_path):
    env = {"LOCKS_HOME": str(tmp_path), "LOCKS_TRACE": "on"}
    trace.write(env, "s1", "tool:Bash", phase="PostToolUse", input={"command": "ls"})
    assert trace.worker_calls({**env, "LOCKS_TRACE": "off"}, "s1", 1) == []
    assert trace.worker_calls(env, None, 1) == []
