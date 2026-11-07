from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

from locks import hooks, stops
from locks.store import Store

ROOT = Path(__file__).resolve().parents[1]


def prompt(store, text, prompt_id="p1", session="s", cwd=None):
    return hooks.user_prompt_submit({"session_id": session, "prompt": text, "prompt_id": prompt_id, "cwd": cwd},
                                    store)


def test_a_prompt_is_a_task_and_a_follow_up_joins_it(store: Store, git_repo):
    prompt(store, "fix the login redirect", cwd=str(git_repo))
    prompt(store, "and keep the query string", prompt_id="p1")
    tasks = store.list_tasks()
    assert len(tasks) == 1 and tasks[0]["repo"] == str(git_repo.resolve())
    assert [m["text"] for m in store.messages(tasks[0]["id"])] == ["fix the login redirect",
                                                                    "and keep the query string"]


def test_stop_writes_the_result_and_interrupts_abandoned_turns(store: Store):
    prompt(store, "first thing", prompt_id="p1")
    prompt(store, "second thing", prompt_id="p2")
    hooks.stop({"session_id": "s", "prompt_id": "p2", "last_assistant_message": "done"}, store)
    first, second = sorted(store.list_tasks(), key=lambda t: t["id"])
    assert second["status"] == "done" and second["result"] == "done"
    assert first["status"] == "interrupted"
    assert store.messages(second["id"])[-1] == {**store.messages(second["id"])[-1], "role": "assistant"}


def test_a_bare_continue_folds_into_the_interrupted_turn(store: Store):
    prompt(store, "migrate the users table", prompt_id="p1")
    store.interrupt_running("s")
    prompt(store, "Sigue", prompt_id="p2")
    tasks = store.list_tasks()
    assert len(tasks) == 1 and tasks[0]["status"] == "running" and tasks[0]["prompt_id"] == "p2"


def test_harness_turns_and_empty_prompts_are_not_tasks(store: Store):
    prompt(store, "<task-notification>done</task-notification>")
    prompt(store, "   ")
    assert hooks.user_prompt_submit({"prompt": "no session"}, store) is None
    assert store.list_tasks() == []


def test_known_dead_ends_reach_the_model_before_it_starts(store: Store):
    p = store.open_problem("websocket reconnect loop", repo="github.com/x/y")
    store.add_fix(p, "raise the backoff to 30s", "failed", repo="github.com/x/y", why="loop is client side")
    answer = prompt(store, "the websocket reconnect loop is back in the dashboard")
    context = answer["hookSpecificOutput"]["additionalContext"]
    assert answer["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "raise the backoff to 30s" in context and "github.com/x/y" in context
    shown = store.last_event(store.list_tasks()[0]["id"], "dead_ends")["data"]["shown"]
    assert shown[0]["tried"] == "raise the backoff to 30s"
    assert prompt(store, "rename the button", prompt_id="p9") is None
    assert prompt(store, "ok", prompt_id="p10") is None


def test_stop_failure_and_missing_sessions(store: Store):
    prompt(store, "deploy staging")
    hooks.stop_failure({"session_id": "s", "error": "rate limited"}, store)
    assert store.list_tasks()[0]["status"] == "failed"
    assert hooks.stop({}, store) is None and hooks.stop_failure({}, store) is None
    assert hooks.stop({"session_id": "s"}, store) is None


def test_session_start_lists_unfinished_work_dead_ends_and_swallowed_errors(store: Store, git_repo, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    prompt(store, "half done thing", cwd=str(git_repo))
    p = store.open_problem("flaky import", repo=str(git_repo.resolve()))
    store.add_fix(p, "pin the version", "failed", repo=str(git_repo.resolve()))
    (root / hooks.ERROR_LOG).write_text("--- a Stop\nboom\n\n--- b Stop\nboom\n")
    event = {"session_id": "s", "cwd": str(git_repo), "source": "resume"}
    context = hooks.session_start(event, store, root)["hookSpecificOutput"]["additionalContext"]
    assert "2 hook error(s)" in context and "half done thing" in context and "pin the version" in context
    assert "tasks of this session that stopped" not in context
    # A turn or a subagent whose ending never arrived has nothing to resume: it is not listed.
    lost = store.create_task("a subagent that never said it ended", status="running", source="delegation",
                             session_id="s")
    stops.record(store, lost["id"], stops.Stop("turn", "the session's subagent ended without its ending"))
    cut = store.create_task("a worker that died", status="running", source="locks", session_id="s")
    stops.record(store, cut["id"], stops.Stop("died", "no sign of life"))
    context = hooks.session_start(event, store, root)["hookSpecificOutput"]["additionalContext"]
    assert "never said it ended" not in context and f"#{cut['id']} [interrupted] a worker that died" in context
    blocked = store.create_task("create the routes", status="running", source="locks", session_id="s", cwd="/x")
    stops.record(store, blocked["id"], stops.Stop("permission", "the worker was refused Bash(node a.js)",
                                                  denied=("Bash(node a.js)",)))
    context = hooks.session_start(event, store, root)["hookSpecificOutput"]["additionalContext"]
    assert f"#{blocked['id']} [blocked] create the routes" in context
    assert "stopped by your permission settings: the worker was refused Bash(node a.js)" in context
    assert f"locks resume {blocked['id']} --allow 'Bash(node:*)'" in context
    # Nothing is left waiting on the person: the session carries it and asks them for the rule.
    assert "yours to carry" in context and "next (ask the person for it here)" in context
    again = hooks.session_start({**event, "source": "startup"}, store, root)["hookSpecificOutput"]
    assert "hook error" not in again["additionalContext"]
    assert hooks.session_start({}, store, root) is None
    assert hooks.session_start({"session_id": "z", "cwd": str(tmp_path / "nowhere")}, store, root) is None


def test_main_answers_on_stdout_and_never_raises(tmp_path):
    env = {"LOCKS_HOME": str(tmp_path / "h")}
    out = io.StringIO()
    event = {"session_id": "s", "prompt": "write the docs", "prompt_id": "1"}
    assert hooks.main("UserPromptSubmit", io.StringIO(json.dumps(event)), out, env) == 0
    assert out.getvalue() == ""
    assert hooks.main("Unknown", io.StringIO("{}"), out, env) == 0
    assert hooks.main("Stop", io.StringIO("[]"), out, env) == 0
    assert hooks.main("Stop", io.StringIO("not json"), out, env) == 0
    assert "JSONDecodeError" in (tmp_path / "h" / hooks.ERROR_LOG).read_text()
    assert hooks.main("Stop", io.StringIO("not json"), out, {**env, "LOCKS_HOOKS_OFF": "1"}) == 0
    assert (tmp_path / "h" / hooks.ERROR_LOG).read_text().count("--- ") == 1


def test_the_launcher_runs_a_hook_as_its_own_process(tmp_path):
    """The contract is 'never take the session down', so it is tested the way
    Claude Code runs it: a separate interpreter, through bin/locks."""
    env = {**os.environ, "LOCKS_HOME": str(tmp_path / "h")}
    proc = subprocess.run([str(ROOT / "bin" / "locks"), "hook", "SessionStart"], input="garbage",
                          capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 0 and proc.stdout == ""
    event = json.dumps({"session_id": "s", "prompt": "add a cache layer to the api", "prompt_id": "1"})
    proc = subprocess.run([str(ROOT / "bin" / "locks"), "hook", "UserPromptSubmit"], input=event,
                          capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 0
    store = Store(tmp_path / "h" / "locks.db")
    assert store.list_tasks()[0]["title"] == "add a cache layer to the api"
    store.close()


def test_session_start_freshens_an_adopted_index_in_the_background(store, git_repo, tmp_path):
    from locks.adapters import ABSENT, PRESENT, Status

    class Fake:
        name = "livespec"

        def __init__(self, state, stale=False):
            self.state, self.stale, self.started = state, stale, []

        def inspect(self, repo_dir):
            return Status(self.name, self.state, stale=self.stale)

        def server(self):
            return {"command": "livespec"}

        def refresh_in_background(self, repo_dir, log):
            self.started.append(log)
            return True

    root = tmp_path / "root"
    event = {"session_id": "s", "cwd": str(git_repo), "source": "startup"}
    stranger = Fake(ABSENT)
    hooks.session_start(event, store, root, {}, adapters=[stranger])
    assert not stranger.started  # a repository that does not use locks is left alone
    from locks import project

    project.enroll(git_repo)
    for fake, word in ((Fake(ABSENT), "indexing"), (Fake(PRESENT, stale=True), "refreshing")):
        answer = hooks.session_start(event, store, root, {}, adapters=[fake])
        assert word in answer["hookSpecificOutput"]["additionalContext"] and fake.started
    quiet = Fake(PRESENT)
    assert hooks.session_start(event, store, root, {}, adapters=[quiet]) is None and not quiet.started
    outside = Fake(ABSENT)
    hooks.session_start({**event, "cwd": str(tmp_path)}, store, root, {}, adapters=[outside])
    assert not outside.started  # never index a directory that is not a repository


def test_session_start_copies_plugin_options(store, tmp_path):
    from locks import config

    env = {"LOCKS_HOME": str(tmp_path / "h"), "CLAUDE_PLUGIN_OPTION_LIVESPEC": "false"}
    hooks.session_start({"session_id": "s"}, store, tmp_path / "h", env, adapters=[])
    assert config.load(env)["livespec"] is False


def test_session_start_puts_locks_on_the_sessions_path(store, tmp_path):
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    (plugin / "bin" / "locks").write_text("#!/bin/sh\n")
    (plugin / "bin" / "locks").chmod(0o755)
    env_file = tmp_path / "session.env"
    env = {"CLAUDE_PLUGIN_ROOT": str(plugin), "PATH": "/usr/bin", "CLAUDE_ENV_FILE": str(env_file)}
    assert hooks.session_start({"session_id": "s"}, store, tmp_path / "h", env, adapters=[]) is None
    first, line = env_file.read_text().splitlines(keepends=True)
    assert first == "export LOCKS_SESSION_ID=s\n"  # `locks run` typed in the session is that session's
    assert line.startswith("export PATH=") and str(plugin / "bin") in line and line.endswith(':"$PATH"\n')
    # Already resolvable: the path is not written twice.
    on_path = {**env, "PATH": str(plugin / "bin")}
    hooks.session_start({"session_id": "s"}, store, tmp_path / "h", on_path, adapters=[])
    assert env_file.read_text().count("export PATH=") == 1
    # No env file: the model is given the full path.
    bare = {"CLAUDE_PLUGIN_ROOT": str(plugin), "PATH": "/usr/bin"}
    context = hooks.session_start({"session_id": "s"}, store, tmp_path / "h", bare, adapters=[])
    assert str(plugin / "bin" / "locks") in context["hookSpecificOutput"]["additionalContext"]
    unwritable = {**bare, "CLAUDE_ENV_FILE": str(tmp_path / "missing" / "x.env")}
    context = hooks.session_start({"session_id": "s"}, store, tmp_path / "h", unwritable, adapters=[])
    assert "not on PATH" in context["hookSpecificOutput"]["additionalContext"]
    # Not a plugin run, or a plugin without the launcher: nothing to do.
    assert hooks.session_start({"session_id": "s"}, store, tmp_path / "h", {"PATH": "/usr/bin"}, adapters=[]) is None
    hollow = {**bare, "CLAUDE_PLUGIN_ROOT": str(tmp_path)}
    assert hooks.session_start({"session_id": "s"}, store, tmp_path / "h", hollow, adapters=[]) is None


def test_the_launcher_follows_a_symlink_and_names_a_missing_python(tmp_path):
    link = tmp_path / "locks"
    link.symlink_to(ROOT / "bin" / "locks")
    env = {**os.environ, "LOCKS_HOME": str(tmp_path / "h")}
    proc = subprocess.run([str(link), "matrix"], capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 0 and proc.stdout
    proc = subprocess.run([str(link), "matrix"], capture_output=True, text=True, check=False,
                          env={**env, "LOCKS_PYTHON": "no-such-python3"})
    assert proc.returncode == 127 and "no-such-python3 not found" in proc.stderr
    old = shutil.which("python3.10")
    if old:
        # A person's choice is kept: an interpreter too old says so in one line,
        # not an import traceback.
        proc = subprocess.run([str(link), "matrix"], capture_output=True, text=True, check=False,
                              env={**env, "LOCKS_PYTHON": old})
        assert proc.returncode == 1 and "needs Python 3.11+" in proc.stderr and "Traceback" not in proc.stderr
        # A bare old python3 hands over to a newer one beside it.
        if any(shutil.which(name) for name in ("python3.11", "python3.12", "python3.13", "python3.14")):
            bare = {k: v for k, v in env.items() if k != "LOCKS_PYTHON"}
            proc = subprocess.run([old, "-m", "locks", "matrix"], capture_output=True, text=True, check=False,
                                  env={**bare, "PYTHONPATH": str(ROOT / "src")})
            assert proc.returncode == 0 and "debug-unclear" in proc.stdout


def test_double_plus_queues_without_a_turn(store: Store, git_repo):
    answer = prompt(store, "++ write the release notes", cwd=str(git_repo))
    assert answer["decision"] == "block" and "queued #" in answer["reason"]
    task = store.list_tasks()[0]
    assert task["status"] == "queued" and task["body"] == "write the release notes"
    assert task["repo"] == str(git_repo.resolve())
    assert prompt(store, "++", prompt_id="p2")["reason"].startswith('locks: "++ <task>"')


def test_a_turn_with_a_subagent_still_running_is_not_done(store: Store):
    prompt(store, "investigate the leak", prompt_id="p1")
    hooks.pre_tool_use({"session_id": "s", "tool_name": "Agent", "tool_use_id": "tu1",
                        "tool_input": {"description": "look at the pool", "prompt": "check the pool",
                                       "run_in_background": True}}, store)
    hooks.post_tool_use({"session_id": "s", "tool_name": "Agent", "tool_use_id": "tu1",
                         "tool_input": {"run_in_background": True}, "tool_response": "launched"}, store)
    hooks.stop({"session_id": "s", "prompt_id": "p1", "last_assistant_message": "waiting on the agent"}, store)
    parent = next(t for t in store.list_tasks() if t["source"] == "hook")
    child = next(t for t in store.list_tasks() if t["source"] == "delegation")
    assert store.messages(child["id"])[0]["role"] == "orchestrator"  # the main session wrote it
    assert parent["status"] == "running" and child["status"] == "running" and child["parent_id"] == parent["id"]
    note = ("<task-notification><tool-use-id>tu1</tool-use-id><status>completed</status>"
            "<result>found it</result></task-notification>")
    prompt(store, note, prompt_id="p2")
    assert store.get_task(child["id"])["result"] == "found it"
    assert store.get_task(parent["id"])["status"] == "done"
    assert len([t for t in store.list_tasks() if t["source"] == "hook"]) == 1  # a notification is no task


def test_a_foreground_subagent_finishes_with_its_tool_call(store: Store):
    prompt(store, "review it", prompt_id="p1")
    hooks.pre_tool_use({"session_id": "s", "tool_name": "Task", "tool_use_id": "tu2",
                        "tool_input": {"prompt": "review"}}, store)
    hooks.post_tool_use({"session_id": "s", "tool_name": "Task", "tool_use_id": "tu2",
                         "tool_input": {}, "tool_response": {"content": "lgtm"}}, store)
    child = next(t for t in store.list_tasks() if t["source"] == "delegation")
    assert child["status"] == "done" and "lgtm" in child["result"]
    assert hooks.pre_tool_use({"session_id": "s", "tool_name": "Bash"}, store) is None
    assert hooks.post_tool_use({"session_id": "s", "tool_name": "Agent", "tool_use_id": "nope"}, store) is None
    prompt(store, "<task-notification><tool-use-id>missing</tool-use-id></task-notification>", prompt_id="p9")


def test_stop_counts_what_the_turn_cost(store: Store, tmp_path):
    from .test_usage import line

    transcript = tmp_path / "session.jsonl"
    transcript.write_text(line("m1", out=42))
    prompt(store, "do a thing", prompt_id="p1")
    hooks.stop({"session_id": "s", "prompt_id": "p1", "last_assistant_message": "ok",
                "transcript_path": str(transcript)}, store)
    assert store.usage_by_model(days=100000)[0]["output_tokens"] == 42


def test_work_a_session_sent_is_reported_to_it_once_when_it_ends(store: Store, git_repo, monkeypatch):
    """A queued task finished while the main session waited on nothing: nothing
    told it to tell the person, continue or relaunch."""
    # A stop that outgrew its turns is resumed on its own first with autowork (`dispatch.recover`).
    monkeypatch.setenv("LOCKS_AUTOWORK", "off")
    from locks import stops

    done = store.create_task("write the changelog", status="running", source="locks", session_id="s",
                             cwd=str(git_repo), final_cell="sonnet/low")
    store.add_event(done["id"], "finished", status="done", branch="locks/task-1", final_cell="sonnet/low",
                    changed=["CHANGELOG.md"])
    store.update_task(done["id"], status="done", result="Wrote the 0.5.0 entry.")
    big = store.create_task("reduce the app to four modules", status="running", source="locks", session_id="s",
                            cwd=str(git_repo))
    stops.record(store, big["id"], stops.Stop("turns", "the task does not fit even the raised turn budget"))
    store.add_event(big["id"], "finished", status="replan", changed=[f"src/f{i}.ts" for i in range(7)],
                    stop=stops.Stop("turns", "the task does not fit even the raised turn budget").data())
    answer = store.create_task("where is slug defined?", status="running", source="locks", session_id="s",
                               cwd=str(git_repo), final_cell="haiku")
    store.add_event(answer["id"], "finished", status="done", final_cell="haiku")
    store.update_task(answer["id"], status="done", result="slug.js:1")
    store.add_event(answer["id"], "report", seq=1, passed=True, report={"unparsed": "# slug\n\nslug.js:1"})
    other = store.create_task("not this session's", status="failed", source="locks", session_id="t")
    store.add_event(other["id"], "finished", status="failed")
    store.create_task("still going", status="running", source="locks", session_id="s")

    out = hooks.stop({"session_id": "s"}, store)
    assert out["decision"] == "block"
    notice = out["reason"]
    assert "#1 [done] write the changelog" in notice and "its work is on branch locks/task-1" in notice
    assert "git diff" not in notice
    assert "the worker says: Wrote the 0.5.0 entry." in notice and "changed: CHANGELOG.md" in notice
    assert "passed at haiku; it changed nothing\n  the worker says: slug.js:1" in notice
    assert f"its full answer is kept on the task: `locks show {answer['id']}` prints it whole" in notice
    assert f"#{big['id']} [replan]" in notice and "stopped by locks' rules" in notice
    assert "src/f0.ts, src/f1.ts, src/f2.ts, src/f3.ts, src/f4.ts and 2 more" in notice
    assert f"locks resume {big['id']} --max-turns 120" in notice
    assert "not this session's" not in notice and "still going" not in notice
    assert hooks.stop({"session_id": "s"}, store) is None  # once
    # a later ending reaches the next prompt instead
    store.add_event(big["id"], "finished", status="replan",
                    stop=stops.Stop("turns", "again").data())
    context = prompt(store, "what is next for the app", session="s")["hookSpecificOutput"]["additionalContext"]
    assert f"#{big['id']} [replan]" in context and "#1 [done]" not in context
    assert prompt(store, "and after that", prompt_id="p2", session="s") is None


def test_an_old_ending_is_not_brought_back(store: Store):
    t = store.create_task("long ago", status="done", source="locks", session_id="s")
    store._conn.execute("INSERT INTO events (task_id, ts, kind, data) VALUES (?, ?, 'finished', '{}')",
                        (t["id"], "2020-01-01T00:00:00+00:00"))
    assert store.unreported("s") == []


def _died(store: Store, task_id: int) -> None:
    store.add_event(task_id, "finished", status="interrupted", reason="no sign of life",
                    stop=stops.Stop("died", "no sign of life").data())


def test_an_ending_with_no_worktree_names_the_repositories_it_changed_in_place(store: Store):
    t = store.create_task("edit both repos", status="running", source="locks", session_id="s")
    store.update_task(t["id"], status="done", final_cell="sonnet/medium")
    store.add_event(t["id"], "finished", status="done", final_cell="sonnet/medium", isolated=False,
                    changed=["api/routes.js", "web/app.ts"], changed_n=2, in_place=["api", "web"])
    notice = hooks.endings(store, "s", claim="stop")[0]
    assert "its changes are in your checkouts of api, web, none of them in a worktree" in notice
    assert "changed: api/routes.js, web/app.ts" in notice


def test_a_resumed_task_is_told_with_its_last_ending_never_an_earlier_one(store: Store):
    # Task died twice, then a resume ran to a pass: the notice is the pass's.
    t = store.create_task("add the portal", status="interrupted", source="locks", session_id="s")
    _died(store, t["id"])
    _died(store, t["id"])
    store.update_task(t["id"], status="running")
    store.update_task(t["id"], status="done", final_cell="sonnet/xhigh")
    # Between the status and the `finished` event the run files its notes: the
    # only ending on record is a death, and "done" must not be told with it.
    assert store.unreported("s") == []
    assert hooks.endings(store, "s") == (None, [])
    changed = [f"src/f{i}.ts" for i in range(106)]
    store.add_event(t["id"], "finished", status="done", final_cell="sonnet/xhigh", branch="locks/task-1",
                    changed=changed[:50], changed_n=106)
    notice = hooks.endings(store, "s", claim="stop")[0]
    assert "passed at sonnet/xhigh; its work is on branch locks/task-1, merged only on the person's word" in notice
    assert "changed nothing" not in notice and "and 98 more" in notice
    assert store.unreported("s") == []


def handback(store, agent, prompt_id, transcript=None):
    text = f'<agent-message from="{agent}">\n  [Subagent hand-back] The text below is the final report.\n  Done.'
    event = {"session_id": "s", "prompt": text, "prompt_id": prompt_id}
    if transcript:
        event["transcript_path"] = str(transcript)
    return hooks.user_prompt_submit(event, store)


def test_a_subagent_s_report_joins_the_task_that_delegated_it(store: Store, tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("")
    folder = transcript.with_suffix("") / "subagents"
    folder.mkdir(parents=True)
    (folder / "agent-a1.meta.json").write_text(json.dumps({"toolUseId": "tu1"}))
    prompt(store, "build the server", prompt_id="p1")
    first = store.last_prompt_task("s")
    hooks.pre_tool_use({"session_id": "s", "tool_name": "Agent", "tool_use_id": "tu1",
                        "tool_input": {"prompt": "write it", "run_in_background": True}}, store)
    hooks.stop({"session_id": "s", "prompt_id": "p1", "last_assistant_message": "started"}, store)
    prompt(store, "and the docs", prompt_id="p2")
    hooks.stop({"session_id": "s", "prompt_id": "p2", "last_assistant_message": "docs done"}, store)

    assert handback(store, "a1", "p3", transcript) is None
    assert sorted(t["id"] for t in store.list_tasks() if t["source"] == "hook") == [first["id"], first["id"] + 2]
    assert store.messages(first["id"])[-1]["role"] == "subagent"
    hooks.stop({"session_id": "s", "prompt_id": "p3", "last_assistant_message": "the server is ready"}, store)
    assert store.get_task(first["id"])["result"] == "the server is ready"

    # No meta to follow: the session's latest prompt takes it.
    assert handback(store, "zz", "p4") is None
    assert store.messages(first["id"] + 2)[-1]["role"] == "subagent"
    assert handback(store, "zz", "p5") is None and len(store.list_tasks()) == 3
    assert hooks.user_prompt_submit({"session_id": "t", "prompt": '<agent-message from="x"> [Subagent hand-back] x',
                                     "prompt_id": "q"}, store) is None
    assert not [t for t in store.list_tasks() if t["session_id"] == "t"]


def test_locks_s_own_notices_are_no_turn_and_a_lost_turn_is_nothing_to_resume(store: Store, capsys):
    from locks import cli

    notice = hooks.ENDINGS_HEADER + "\n#21 [done] add median"
    for text in (notice, "The locks plugin sent a message: " + notice):
        assert hooks.user_prompt_submit({"session_id": "s", "prompt": text}, store) is None
    assert store.list_tasks(source=("hook",)) == []
    hooks.user_prompt_submit({"session_id": "s", "prompt": "y ahora que resultado da?", "prompt_id": "p1"}, store)
    hooks.stop_failure({"session_id": "s", "error": "server_error"}, store)
    turn = store.list_tasks(source=("hook",))[0]
    assert turn["status"] == "failed"
    stop = stops.of(store, turn)
    assert stop.cause == "turn" and stop.reason == "server_error" and "nothing to resume" in stops.what_to_do(stop)
    store.create_task("real work", status="done", source="locks", kind="docs")
    assert cli.main(["tasks", "--all"]) == 0
    out = capsys.readouterr().out
    assert "real work" in out and "resultado" not in out
    assert cli.main(["tasks", "--all", "--turns"]) == 0
    assert "y ahora que resultado da? (turn)" in capsys.readouterr().out


def test_session_start_says_what_the_registry_shadows_once(store, tmp_path):
    root = tmp_path / "h"
    root.mkdir()
    (root / "capabilities.json").write_text(json.dumps({
        "layout-inspector": {"server": {"command": "layout-inspector"}, "when": ["implement"]}}))

    def said():
        out = hooks.session_start({"session_id": "s"}, store, root, {}, adapters=[])
        return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")

    assert "shadows the layout-inspector neighbour" in said()
    assert "shadows" not in said()
    (root / "capabilities.json").write_text(json.dumps({
        "layout-inspector": {"server": {"command": "npx", "args": ["li@latest"]}, "touches": "ui"}}))
    again = said()
    assert "with no fixed version" in again and "takes" not in again


def test_an_ending_names_what_was_already_broken_and_the_bugfix_that_would_take_it_on(store: Store):
    run = store.create_run(name="feature", scope="bundled", source="x", definition="{}", repo="r",
                           session_id="s", status="done")
    review = store.create_task("[feature#1 review] x", status="done", source="queue", session_id="s",
                               workflow_run=run["id"], workflow_step="review")
    old = {"where": "lib/tax.py:12", "what": "rounds half down", "evidence_class": "deterministic",
           "cause": "pre-existing", "proof": "pytest tests/test_tax.py"}
    guess = {**old, "where": "lib/x.py", "evidence_class": "inferential"}
    mine = {**old, "where": "app.py:3", "cause": "introduced"}
    store.add_event(review["id"], "finished", status="done", findings=[old, guess, mine, old])
    last = store.create_task("[feature#1 recheck] x", status="done", source="queue", session_id="s",
                             workflow_run=run["id"], workflow_step="recheck")
    store.add_event(last["id"], "finished", status="done")
    lines = hooks._found_before(store, store.get_task(last["id"]))
    assert lines[0].startswith("  it found 1 problem(s) that were already there")
    assert "lib/tax.py:12: rounds half down" in lines[1] and f"--after run:{run['id']}" in lines[1]
    assert "locks workflow run bugfix 'rounds half down (lib/tax.py:12)'" in lines[1]
    alone = store.create_task("x", status="done", source="locks", session_id="s")
    many = [{**old, "where": f"f{i}.py"} for i in range(5)]
    store.add_event(alone["id"], "finished", status="done", findings=many)
    found = hooks._found_before(store, store.get_task(alone["id"]))
    assert f"--after task:{alone['id']}" in found[1] and found[-1].startswith("    and 2 more")
    plain = store.create_task("y", status="done", source="locks", session_id="s")
    store.add_event(plain["id"], "finished", status="done")
    assert hooks._found_before(store, store.get_task(plain["id"])) == []


def test_an_ending_names_other_work_that_changes_the_same_files(store: Store, git_repo):
    from .conftest import git

    def kept(text: str, status: str, changed: list[str]) -> dict:
        task = store.create_task(text, status=status, source="queue", session_id="s", repo="r", cwd=str(git_repo))
        git(git_repo, "branch", f"locks/task-{task['id']}")
        store.add_event(task["id"], "finished", status=status, changed=changed, branch=f"locks/task-{task['id']}")
        return task

    beside = kept("add the curriculum API", "done", ["src/api.service.spec.ts", "src/api.service.ts"])
    elsewhere = kept("add the forum", "done", ["src/forum.ts"])
    running = store.create_task("add the admin screens", status="running", source="locks", repo="r",
                                cwd=str(git_repo))
    store.add_attempt(running["id"], cell="sonnet/medium", max_turns=40, passed=0,
                      changed_paths=["src/api.service.spec.ts"])
    unkept = store.create_task("gone", status="done", source="locks", repo="r", cwd=str(git_repo))
    store.add_event(unkept["id"], "finished", status="done", changed=["src/api.service.spec.ts"], branch="x")
    mine = kept("add the student screens", "done", ["src/api.service.spec.ts", "src/student.ts"])
    ended = {**store.get_task(mine["id"]), "finished_data": store.final_event(mine["id"])["data"]}
    lines = hooks._overlaps(store, ended)
    assert lines[0].startswith("  other work here changes the same files, so merging both conflicts there")
    assert any(f"#{beside['id']} (done, branch kept) add the curriculum API: src/api.service.spec.ts" in line
               for line in lines)
    assert any(f"#{running['id']} (running now)" in line for line in lines)
    assert not any(f"#{elsewhere['id']} " in line or f"#{unkept['id']} " in line for line in lines)
    alone = {**store.get_task(elsewhere["id"]), "finished_data": {"changed": ["src/forum.ts"]}}
    assert hooks._overlaps(store, alone) == []


def test_a_passing_ending_carries_its_verdict_so_the_session_never_redoes_the_work():
    full = {"criteria": [{"id": "S1", "met": True, "evidence": "test_export passes"},
                         {"id": "S2", "met": True, "evidence": ""}],
            "check": {"command": "npm test", "passed": True}, "independent": {"ran": True, "passed": True},
            "attempts": 1}
    lines = hooks.verdict_lines(full)
    assert lines[0] == "  check: `npm test` passed, run by locks in its worktree"
    assert "  S1 met: test_export passes" in lines and "  S2 met: no evidence given" in lines
    assert "  independent check: passed" in lines
    assert lines[-1].endswith("open the diff only if the person asks") and "do not re-read" in lines[-1]
    gaps = {"criteria": [{"id": "S1", "met": False, "evidence": "not built"}], "check": None,
            "independent": {"ran": False, "why": "a blind review step of this workflow reads the change next"}}
    lines = hooks.verdict_lines(gaps)
    assert lines[0].startswith("  check: none was given") and "  S1 NOT met: not built" in lines
    assert "  independent check: not run (a blind review step of this workflow reads the change next)" in lines
    assert lines[-1].endswith("not checked or not met")
    blocked = hooks.verdict_lines({**full, "independent": {"ran": True, "passed": False, "blocking": 2}})
    assert "  independent check: 2 blocker(s) left, recorded" in blocked
    assert hooks.verdict_lines(None) == [] and hooks.verdict_lines({}) [-1].endswith("not checked or not met")


def test_an_independent_check_that_read_nothing_twice_is_said_so_and_lets_the_diff_be_opened():
    held = {"criteria": [{"id": "S1", "met": True, "evidence": "test_export passes"}],
            "check": {"command": "npm test", "passed": True},
            "independent": {"ran": True, "passed": False, "blocking": 0, "unread": True}, "attempts": 1}
    lines = hooks.verdict_lines(held)
    assert "  independent check: read nothing twice; the pass stands on locks' own check" in lines
    assert not any("blocker(s) left" in line for line in lines)
    assert lines[-1].endswith("open the diff only where it says something was not checked or not met")
    # Its TRACER lines nobody checked are still said.
    unchecked = hooks.verdict_lines({**held, "independent": {**held["independent"], "unchecked": ["C2"]}})
    assert "  TRACER CORRECT lines nobody checked: C2" in unchecked


def test_a_command_line_flag_or_a_bare_slash_command_is_no_turn(store: Store, tmp_path):
    for echo in ("--continue", "--resume", "--resume 4f2a9c", "-c", "/clear", "/compact", "  --continue  "):
        prompt(store, echo)
    assert store.list_tasks() == []
    # A request that only opens with a dash or a slash is one.
    prompt(store, "--verify is missing from the run, add it", prompt_id="p2")
    prompt(store, "/tmp fills up after the build, find why", prompt_id="p3")
    assert len(store.list_tasks()) == 2
    # One recorded before is no unfinished work when the session comes back.
    old = store.create_task("--continue", status="running", source="hook", session_id="s2")
    real = store.create_task("wire the export", status="running", source="hook", session_id="s2")
    out = hooks.session_start({"session_id": "s2", "source": "resume"}, store, tmp_path)
    text = out["hookSpecificOutput"]["additionalContext"] if out else ""
    assert f"#{real['id']} [running]" in text and f"#{old['id']}" not in text


def test_a_session_hears_once_that_a_newer_locks_is_installed(tmp_path):
    from locks import __version__

    from .test_link import _plugin

    config = tmp_path / "config"
    env = {"CLAUDE_CONFIG_DIR": str(config)}
    _plugin(config, __version__)
    assert hooks._newer_plugin(tmp_path, "s", env) is None
    _plugin(config, "999.0.0")
    said = hooks._newer_plugin(tmp_path, "s", env)
    assert said.startswith(f"locks: locks 999.0.0 is installed, and this session loaded {__version__}")
    assert "/reload-plugins --force" in said
    assert "/locks:relay" in said
    assert hooks._newer_plugin(tmp_path, "s", env) is None  # once
    assert hooks._newer_plugin(tmp_path, "t", env) is not None  # each session hears it
    _plugin(config, "999.1.0")
    assert "999.1.0" in hooks._newer_plugin(tmp_path, "s", env)  # and each newer version


def test_many_endings_at_once_tell_each_pass_in_one_line(store: Store):
    verdict = {"check": {"command": "npm test"}, "criteria": [{"id": "S1", "met": True}, {"id": "S2", "met": False}],
               "independent": {"ran": True, "passed": True}}
    for n in range(4):
        t = store.create_task(f"task {n}", status="done", source="locks", session_id="s")
        store.add_event(t["id"], "finished", status="done", final_cell="sonnet/high", branch=f"locks/task-{t['id']}",
                        verdict=verdict, changed=["a.py"])
    notice, ids = hooks.endings(store, "s")
    assert len(ids) == 4 and "4 ended: 4 passed, 0 stopped" in notice
    assert notice.count("passed at sonnet/high, on branch locks/task-") == 4
    assert "`npm test` passed, 1/2 criteria met, independent check passed; changed 1 file(s)" in notice
    assert "this verdict is locks' own record" not in notice
    assert hooks.verdict_summary(None) == "no verdict kept"


def test_problems_already_there_that_the_work_fixed_are_not_offered(store: Store):
    task = store.create_task("write tests for the radio drivers", status="done", source="locks", session_id="s")
    old = {"where": "src/radio/mac.ts:40", "what": "drops the last frame", "evidence_class": "deterministic",
           "cause": "pre-existing", "proof": "deno test radio_test.ts"}
    marked = {**old, "where": "src/radio/nrf24.ts:9", "what": "reads the wrong register", "fixed": True}
    elsewhere = {**old, "where": "src/board/limits.ts:3", "what": "allows one board too many"}
    store.add_event(task["id"], "finished", status="done", findings=[old, marked, elsewhere],
                    changed=["src/radio/mac.ts", "tests/radio_test.ts"])
    lines = hooks._found_before(store, store.get_task(task["id"]))
    assert lines[0].startswith("  it found 1 problem(s)") and "src/board/limits.ts:3" in lines[1]
    assert not any("mac.ts" in line or "nrf24" in line for line in lines[1:-1])
    assert lines[-1] == "    and 2 more in files this work changed or marked fixed by it: not offered"
    store.add_event(task["id"], "finished", status="done", findings=[old, marked], changed=["src/radio/mac.ts"])
    assert hooks._found_before(store, store.get_task(task["id"])) == [
        "  it found 2 problem(s) that were already there, each in a file this work changed or marked fixed by it: "
        "nothing left to offer"]


def test_a_finding_keeps_fixed_only_when_the_worker_says_true():
    from locks import findings

    read = findings.read({"findings": [{"where": "a.py", "what": "x", "fixed": True},
                                       {"where": "b.py", "what": "y", "fixed": "yes"}]})
    assert read[0]["fixed"] is True and "fixed" not in read[1]


def test_older_installs_left_in_the_cache_are_named_once_with_how_to_remove_them(tmp_path):
    from locks import __version__

    from .test_link import _plugin

    config = tmp_path / "config"
    env = {"CLAUDE_CONFIG_DIR": str(config)}
    _plugin(config, __version__)
    assert hooks._older_installs(tmp_path, env) is None
    _plugin(config, "0.1.0")
    said = hooks._older_installs(tmp_path, env)
    assert said.startswith("locks: older locks installs remain in Claude Code's plugin cache (0.1.0)")
    assert "rm -rf " in said and "0.1.0" in said.split("rm -rf ")[1]
    assert hooks._older_installs(tmp_path, env) is None  # once per set
    _plugin(config, "999.0.0")
    assert hooks._older_installs(tmp_path, env) is None  # a newer one: the newer-plugin notice speaks


def test_problems_already_there_in_files_changed_since_are_not_offered(store: Store, git_repo):
    from .conftest import git

    base = git(git_repo, "rev-parse", "HEAD")
    (git_repo / "landing.md").write_text("binaries are published\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "landed after the work started")
    task = store.create_task("write tests for the radio", status="done", source="locks", session_id="s",
                             cwd=str(git_repo), repo="r")
    old = {"where": "landing.md:1", "what": "says no binaries are published", "evidence_class": "deterministic",
           "cause": "pre-existing", "proof": "git ls-remote --tags"}
    other = {**old, "where": "src/ble.ts:3", "what": "drops a frame"}
    open_one = {**old, "where": "src/board.ts:3", "what": "allows one board too many"}
    store.add_event(task["id"], "finished", status="done", findings=[old, other, open_one], changed=["tests/t.ts"],
                    base=base)
    # other passing work here changed src/ble.ts after this one started
    fixer = store.create_task("fix ble", status="done", source="locks", repo="r")
    store.add_event(fixer["id"], "finished", status="done", changed=["src/ble.ts"])
    lines = hooks._found_before(store, store.get_task(task["id"]))
    assert lines[0].startswith("  it found 1 problem(s)") and "src/board.ts:3" in lines[1]
    assert not any("landing.md" in line or "ble.ts" in line for line in lines)
    assert lines[-1].startswith("    and 2 more in files changed since this work started")
    store.add_event(task["id"], "finished", status="done", findings=[old], changed=["tests/t.ts"], base=base)
    assert hooks._found_before(store, store.get_task(task["id"]))[0] == (
        "  it found 1 problem(s) that were already there: nothing left to offer")


def test_a_session_start_and_a_prompt_keep_the_version_the_session_s_hooks_run(store, tmp_path):
    from locks import __version__, link

    hooks.session_start({"session_id": "s1"}, store, tmp_path / "h", {"LOCKS_HOME": str(tmp_path / "h")}, adapters=[])
    assert link.session_loaded(tmp_path / "h", "s1") == __version__
    link.record_loaded(store.path.parent, "s2", "0.1.0")
    hooks.user_prompt_submit({"session_id": "s2", "prompt": "hola", "prompt_id": "p"}, store)
    assert link.session_loaded(store.path.parent, "s2") == __version__


def test_an_older_install_a_live_session_still_runs_from_is_not_offered_for_removal(tmp_path):
    from locks import __version__, link

    from .test_link import _plugin

    config = tmp_path / "config"
    env = {"CLAUDE_CONFIG_DIR": str(config)}
    _plugin(config, __version__)
    _plugin(config, "0.1.0")
    _plugin(config, "0.2.0")
    link.record_loaded(tmp_path, "old-session", "0.1.0")  # its hooks still run from 0.1.0
    said = hooks._older_installs(tmp_path, env)
    assert "still loaded by a session" in said and "/reload-plugins" in said
    removal = said.split("rm -rf ")[1:]
    assert removal and all("0.1.0" not in r for r in removal) and any("0.2.0" in r for r in removal)


def test_a_session_quiet_past_the_live_window_frees_its_version():
    import time

    from locks import link

    root = __import__("pathlib").Path(__import__("tempfile").mkdtemp())
    link.record_loaded(root, "s", "0.1.0")
    assert link.live_versions(root) == {"0.1.0"}
    assert link.live_versions(root, now=time.time() + link.LIVE_S + 1) == set()


def test_verdict_summary_says_an_unread_independent_check():
    said = hooks.verdict_summary({"check": {"command": "pytest", "state": ""}, "criteria": [],
                                  "independent": {"ran": True, "passed": False, "blocking": 0, "unread": True}})
    assert "blocker" not in said and "read nothing" in said
