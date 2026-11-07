"""How a worker worked: an attempt's profile from its own tool calls, kept on `attempt_finished`
and read back by `locks stats`, its table and its export."""
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from locks import analysis, behavior, habits, mcp_guard, signature
from locks.store import Store


def call(tool, sig, at, *, ok=1, arg="a"):
    return {"ts": f"2026-11-01T10:00:{at:02d}.000+00:00", "tool": tool, "sig": sig, "arg_hash": arg, "ok": ok}


def signed(tool, **tool_input):
    """The signature the PostToolUse hook really writes for a call, never one typed by hand."""
    return signature.signature(tool, tool_input)


def bash(command):
    return signed("Bash", command=command)


READING = [
    ("Read", {"file_path": "a.py"}), ("Grep", {"pattern": "x"}), ("Glob", {"pattern": "*.py"}),
    *[("Bash", {"command": c}) for c in (
        "git diff main", "git show HEAD", "git log -3", "git blame a.py", "git grep needle", "cat a.py",
        "head -5 a.py", "tail -5 a.py", "sed -n 1,5p a.py", "nl a.py", "less a.py", "rg needle",
        "grep -rn needle .", "FOO=1 git diff",
        # A chain is signed by its first program, and a command no program can be read from is unknown.
        "cd /repo && git diff main", "")],
    ("mcp__livespec__search", {}), ("mcp__livespec__get_symbol_source", {}),
]
NOT_READING = [
    ("Bash", {"command": "ls"}), ("Bash", {"command": "git status"}), ("Bash", {"command": "pytest -q"}),
    ("Edit", {"file_path": "a.py"}), ("Write", {"file_path": "a.py"}), ("mcp__layout-inspector__x", {}),
    # livespec's tools that change the index or the specs are not reads.
    ("mcp__livespec__export_openspec", {}), ("mcp__livespec__sync_openspec", {}),
    ("LS", {}), ("WebFetch", {}), ("TodoWrite", {}),
]


def test_a_profile_reads_how_far_a_worker_read_before_it_wrote_and_its_fix_loops():
    calls = [call("read", "read:py", 0, arg="x"), call("grep", "grep", 2), call("read", "read:py", 4, arg="x"),
             call("edit", "edit:py", 10), call("bash", "bash:pytest", 12, ok=0), call("edit", "edit:py", 20),
             call("bash", "bash:pytest", 22), call("edit", "edit:py", 50)]
    got = behavior.profile(calls)
    assert got["calls"] == 8 and got["failed"] == 1
    assert (got["explore"], got["implement"], got["check_runs"]) == (3, 3, 2)
    assert got["calls_before_edit"] == 3 and got["s_before_edit"] == 10.0
    # A check, a write, a check again: one fix loop; the last write no check followed.
    assert got["fix_cycles"] == 1 and got["unchecked_end"] is True
    assert got["rereads"] == 1 and got["repeat_max"] == 1
    assert got["gap_max_s"] == 28.0 and got["active_s"] == 50.0
    assert got["tools"] == {"read": 2, "edit": 3, "search": 1, "shell": 2}


def test_a_worker_that_only_read_has_no_first_write_and_repeats_are_counted():
    calls = [call("bash", "bash:ls", 0), call("bash", "bash:ls", 1), call("bash", "bash:ls", 2),
             call("mcp__livespec__search", "mcp:livespec.search", 3)]
    got = behavior.profile(calls)
    assert "calls_before_edit" not in got and got["unchecked_end"] is False and got["check_runs"] == 0
    assert got["repeat_max"] == 3 and got["tools"] == {"shell": 3, "mcp:livespec": 1}
    assert behavior.profile([]) == {}
    one = behavior.profile([call("read", "read:py", 0)])
    assert "gap_max_s" not in one and one["repeat_max"] == 1


def test_the_table_adds_profiles_up_by_kind_outcome_and_cell():
    def finished(task, passed, cell, worked):
        return {"task_id": task, "kind": "attempt_finished",
                "data": {"passed": passed, "cell": cell, "behavior": worked}}

    a = behavior.profile([call("read", "read:py", 0), call("edit", "edit:py", 5), call("bash", "bash:pytest", 9)])
    b = behavior.profile([call("edit", "edit:py", 0), call("edit", "edit:py", 1, arg="b")])
    events = [finished(1, True, "sonnet/high", a), finished(2, False, "sonnet/high", b),
              finished(3, True, "opus/high", a), {"task_id": 4, "kind": "attempt_finished", "data": {"passed": True}},
              finished(9, True, "x", a)]
    got = behavior.table(events, {1: "implement", 2: "implement", 3: "implement", 4: "implement"})
    rows = {(r["kind"], r["outcome"]): r for r in got["by_kind"]}
    assert rows[("implement", "passed")]["attempts"] == 2 and rows[("implement", "failed")]["attempts"] == 1
    assert rows[("implement", "passed")]["calls_before_edit_p50"] == 1.0
    assert rows[("implement", "failed")]["never_checked_share"] == 1.0
    assert rows[("implement", "failed")]["unchecked_end_share"] == 1.0
    assert [r["cell"] for r in got["by_cell"]] == ["sonnet/high", "opus/high"]
    assert behavior.summarize([])["calls_p50"] is None


def test_an_attempt_s_profile_is_kept_and_read_back_by_stats_and_its_export(git_repo, store: Store, monkeypatch):
    """`orchestrate` keeps the worker's calls as `attempt_finished.behavior`; `locks stats` reads it
    back by kind, on the run and in the per-attempt export."""
    from locks.orchestrate import run

    from .test_orchestrate import Script, kind, ok

    class Calling(Script):
        def __call__(self, spec):
            env = {**os.environ, **spec.env}
            for tool, command in (("Read", None), ("Edit", None), ("Bash", "pytest -q")):
                event = {"tool_name": tool, "session_id": "w",
                         "tool_input": {"command": command} if command else {"file_path": "a.py"}}
                signature.append(event, env)
            return super().__call__(spec)

    report = run("Rename the helper in notes", git_repo, store, registry={}, launcher=Calling(ok()),
                 classifier=kind("implement"))
    worked = store.last_event(report.task_id, "attempt_finished")["data"]["behavior"]
    assert worked["calls"] == 3 and worked["calls_before_edit"] == 1 and worked["check_runs"] == 1
    said = analysis.compute(store, days=0)
    assert said["behavior"]["by_kind"][0]["kind"] == "implement"
    assert "how workers worked" in analysis.text(said)
    tasks, events = store.runs_since("")
    found = analysis.runs(tasks, events)[0]
    assert found["calls"] == 3 and found["check_runs"] == 1 and found["calls_before_edit"] == 1
    rows = analysis.attempts(tasks, events)
    assert rows[0]["task"] == report.task_id and rows[0]["calls"] == 3 and rows[0]["tools"]["edit"] == 1
    csv_text = analysis.attempts_csv(rows)
    assert csv_text.splitlines()[0].startswith("task,seq,repo,kind") and "edit:1" in csv_text
    assert analysis.attempts(tasks, events, since="9999") == []


def test_the_independent_check_s_calls_are_never_its_attempt_s():
    from locks.orchestrate import _verifier_env

    assert _verifier_env({"LOCKS_WORKER_TASK": "4", "LOCKS_WORKER_ATTEMPT": "2"})["LOCKS_WORKER_ATTEMPT"] == "2-verify"
    assert "LOCKS_WORKER_ATTEMPT" not in _verifier_env({"LOCKS_WORKER_TASK": "4"})
    kept = signature.entry({"tool_name": "Read"}, {"LOCKS_WORKER_TASK": "4", "LOCKS_WORKER_ATTEMPT": "2-verify"})
    assert kept["task_id"] == 4 and kept["attempt"] is None


def test_stats_attempts_prints_one_record_per_attempt(store: Store, capsys):
    from locks import cli

    task = store.create_task("x", status="done", source="locks", repo="/r", kind="implement")["id"]
    store.add_event(task, "planned", kind="implement", start="sonnet/high")
    store.add_event(task, "attempt_finished", seq=1, cell="sonnet/high", passed=True,
                    behavior={"calls": 4, "tools": {"read": 4}})
    store.add_event(task, "finished", status="done")
    assert cli.main(["stats", "--attempts", "--days", "0"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["calls"] == 4 and rows[0]["cell"] == "sonnet/high"
    assert cli.main(["stats", "--attempts", "--csv"]) == 0
    assert "read:4" in capsys.readouterr().out


@pytest.mark.parametrize(("tool", "tool_input"), READING)
def test_read_calls_counts_each_call_that_reads_code(tool, tool_input):
    sig = signed(tool, **tool_input)
    assert behavior.read_calls([call(tool.lower(), sig, 0)]) == 1, sig


@pytest.mark.parametrize(("tool", "tool_input"), NOT_READING)
def test_read_calls_leaves_out_calls_that_read_nothing(tool, tool_input):
    sig = signed(tool, **tool_input)
    assert behavior.read_calls([call(tool.lower(), sig, 0)]) == 0, sig


def test_read_calls_adds_up_a_mixed_attempt_and_a_call_that_failed_still_read():
    sigs = [signed("Read", file_path="a.py"), bash("ls"), bash("git diff main"), signed("Edit", file_path="a.py"),
            signed("mcp__livespec__search"), signed("mcp__livespec__export_openspec"), bash("cd /r && git diff")]
    calls = [call("x", sig, i, ok=int(i != 2)) for i, sig in enumerate(sigs)]
    assert behavior.read_calls(calls) == 4
    assert behavior.read_calls([]) == 0
    # The livespec reads are the guard's own list: a tool it adds is a read here too.
    assert {"export_openspec", "sync_openspec"}.isdisjoint(mcp_guard.LIVESPEC_READ)
    for tool in mcp_guard.LIVESPEC_READ:
        assert behavior.read_calls([{"sig": signed(f"mcp__livespec__{tool}")}]) == 1, tool


def test_a_profile_carries_read_calls_after_rereads_and_an_attempt_that_read_nothing_has_zero():
    got = behavior.profile([call("read", "read:.py", 0), call("bash", "bash:git-diff", 1), call("edit", "edit:.py", 2),
                            call("bash", "bash:ls", 3)])
    assert got["read_calls"] == 2
    assert behavior.profile([call("edit", "edit:.py", 0)])["read_calls"] == 0
    assert behavior.profile([]) == {}
    fields = behavior.FIELDS
    assert fields[fields.index("rereads") + 1] == "read_calls"
    assert "read_calls" in analysis.ATTEMPT_COLUMNS


def test_an_attempt_s_read_calls_reach_attempt_finished(git_repo, store: Store):
    from locks.orchestrate import run

    from .test_orchestrate import Script, kind, ok

    class Calling(Script):
        def __call__(self, spec):
            env = {**os.environ, **spec.env}
            for tool, tool_input in (("Bash", {"command": "git diff main"}), ("Edit", {"file_path": "a.py"}),
                                     ("Read", {"file_path": "a.py"})):
                signature.append({"tool_name": tool, "session_id": "w", "tool_input": tool_input}, env)
            return super().__call__(spec)

    report = run("Rename the helper in notes", git_repo, store, registry={}, launcher=Calling(ok()),
                 classifier=kind("implement"))
    assert store.last_event(report.task_id, "attempt_finished")["data"]["behavior"]["read_calls"] == 2


def test_the_review_constants_and_the_event_s_data():
    assert isinstance(behavior.REVIEW_KINDS, frozenset)
    assert set(behavior.REVIEW_KINDS) == {"review-routine", "review-critical"}
    assert behavior.UNREAD_SHAPE == "unread-review"
    told = behavior.UNREAD_PROMPT.format(base="abc1234")
    assert "`git diff abc1234`" in told and "{" not in told
    assert told.startswith("Your review reported no finding but your calls show you read nothing: read the diff")
    assert told.endswith("then answer again under the same result contract.")
    assert behavior.unread_record("worker", again=False, calls=3) == {"who": "worker", "again": False, "calls": 3}
    assert behavior.unread_record("verifier", again=True, calls=0) == {"who": "verifier", "again": True, "calls": 0}


def log_call(tool, session, *, task=7, attempt="2-verify", **tool_input):
    """A call as the PostToolUse hook logs it; the independent check's attempt is `N-verify`, which the
    log keeps as no attempt."""
    env = {**os.environ, "LOCKS_WORKER_TASK": str(task)}
    if attempt:
        env["LOCKS_WORKER_ATTEMPT"] = attempt
    signature.append({"tool_name": tool, "session_id": session, "tool_input": tool_input}, env)


def test_the_check_s_calls_are_found_by_its_session_with_the_log_loaded_first(store: Store):
    log_call("Bash", "v1", command="git diff main")
    log_call("Read", "v1", file_path="a.py")
    log_call("Read", "other", file_path="b.py")
    log_call("Edit", "w1", attempt="2", file_path="a.py")
    assert store.session_calls("v1") == []  # the log reaches the store only when something loads it
    got = behavior.check_calls(store, 7, "v1", None, None)
    assert [c["sig"] for c in got] == ["bash:git-diff", "read:.py"]
    assert set(got[0]) == {"ts", "tool", "sig", "arg_hash", "ok"} == set(store.attempt_calls(7, 2)[0])
    assert behavior.read_calls(got) == 2
    # Found by its session, the window is not asked.
    assert behavior.check_calls(store, 7, "v1", "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:01+00:00") == got


def test_without_a_session_the_check_s_calls_are_the_task_s_unattributed_ones_in_its_window(store: Store):
    before = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    log_call("Bash", "v1", command="ls")
    log_call("Read", "v1", file_path="a.py", task=8)
    log_call("Edit", "w1", attempt="2", file_path="a.py")  # the worker's: an attempt of its own
    after = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    got = behavior.check_calls(store, 7, None, before, after)
    assert [c["sig"] for c in got] == ["bash:ls"]
    # A session that logged nothing falls back to the window.
    assert behavior.check_calls(store, 7, "never-logged", before, after) == got
    assert behavior.check_calls(store, 8, "", before, after)[0]["sig"] == "read:.py"


def test_a_window_counts_whole_seconds_because_the_log_keeps_milliseconds_and_the_store_seconds(store: Store):
    def logged(ts, sig):
        store.add_tool_event(ts=ts, task_id=7, attempt=None, session_id="v", tool="bash", sig=sig,
                             arg_hash="a", ok=1)

    logged("2026-11-01T10:00:04.900+00:00", "bash:before")
    logged("2026-11-01T10:00:05.100+00:00", "bash:first")
    logged("2026-11-01T10:00:09.800+00:00", "bash:last")
    logged("2026-11-01T10:00:10.400+00:00", "bash:in-the-until-second")
    logged("2026-11-01T10:00:11.000+00:00", "bash:after")
    rows = store.unattributed_calls(7, "2026-11-01T10:00:05+00:00", "2026-11-01T10:00:10+00:00")
    assert [r["sig"] for r in rows] == ["bash:first", "bash:last", "bash:in-the-until-second"]
    narrow = store.unattributed_calls(7, "2026-11-01T10:00:05+00:00", "2026-11-01T10:00:09+00:00")
    assert [r["sig"] for r in narrow] == ["bash:first", "bash:last"]
    assert [r["sig"] for r in store.unattributed_calls(7, "2026-11-01T10:00:11+00:00", "2026-11-01T10:00:12+00:00")
            ] == ["bash:after"]
    assert store.unattributed_calls(7, "2026-11-01T10:00:12+00:00", "2026-11-01T10:00:13+00:00") == []
    assert store.unattributed_calls(8, "2026-11-01T10:00:00+00:00", "2026-11-01T10:01:00+00:00") == []


def test_check_calls_is_none_when_nothing_is_logged_never_an_empty_list(store: Store, monkeypatch):
    window = ("2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00")
    assert behavior.check_calls(store, 7, "v1", *window) is None  # no log at all
    assert behavior.check_calls(store, 7, None, None, None) is None
    log_call("Edit", "w1", attempt="2", file_path="a.py")  # only the worker's own call is logged
    assert behavior.check_calls(store, 7, None, *window) is None
    assert behavior.check_calls(store, 7, "v1", None, None) is None
    assert behavior.check_calls(store, 7, "v1", window[0], None) is None

    def broken(_store):
        raise OSError("the log cannot be read")

    monkeypatch.setattr(habits, "load_events", broken)
    log_call("Read", "v1", file_path="a.py")
    assert behavior.check_calls(store, 7, "v1", *window) is None


def test_read_calls_counts_shell_reads_whose_verb_the_signature_lost():
    from locks import signature

    for command in ("git -C /x diff HEAD~1", "git -c core.pager=cat diff", "git -C . diff", "awk 1 f",
                    "find . -name x", "diff a b", "git diff-tree -p HEAD", "git cat-file -p HEAD", "jq . f"):
        sig = signature.signature("Bash", {"command": command})
        assert behavior.read_calls([{"sig": sig}]) == 1, (command, sig)
    assert behavior.read_calls([{"sig": "bash:git-commit"}, {"sig": "bash:pytest"}]) == 0


def test_read_calls_counts_a_git_call_whose_verb_is_a_directory_and_a_wrapped_command():
    from locks import signature

    for command in ("git -C repo diff main", "bash -c 'git diff'", "timeout 30 git diff", "gh pr diff 3",
                    "git rev-list HEAD", "git ls-tree HEAD"):
        sig = signature.signature("Bash", {"command": command})
        assert behavior.read_calls([{"sig": sig}]) == 1, (command, sig)
    for command in ("git status", "git commit -m x", "git rev-parse HEAD", "pytest -q", "ls"):
        sig = signature.signature("Bash", {"command": command})
        assert behavior.read_calls([{"sig": sig}]) == 0, (command, sig)
