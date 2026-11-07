from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

from locks import cli, hooks, scribe
from locks.launch import WorkerResult
from locks.orchestrate import run
from locks.store import home

from .test_orchestrate import Script, kind
from .test_survey import assistant, result, session, tool, user, write


def transcript_of(tmp_path, where, sid="s1", entries=None):
    return write(tmp_path / "claude-config", f"-{where.name}", sid, entries if entries is not None else session(where))


def append(path, *entries):
    with path.open("a") as fh:
        fh.write("".join(json.dumps(e) + "\n" for e in entries))


# --- what the scribe keeps --------------------------------------------------------


def test_the_scribe_keeps_the_person_s_words_the_files_and_what_failed_then_passed(git_repo, tmp_path):
    path = transcript_of(tmp_path, git_repo)
    state = scribe.update(home(), "s1", path)
    texts = [r["text"] for r in state["requests"]]
    # The person's words verbatim; harness text (an interrupt, a slash command) is not theirs.
    assert texts[:4] == ["Planifica la migración del carrito a centavos", "implementa el cambio en cart.py",
                         "no, eso no: te dije que no toques el redondeo", "sigue"]
    # A long paste is kept cut, the sidechain's words are no request of the person's.
    assert len(texts) == 5 and texts[4].startswith("xxx") and len(texts[4]) == scribe.REQUEST_KEEP
    assert [r["correction"] for r in state["requests"]] == [False, False, True, False, False]
    assert state["turns"] == 5 and state["corrections"] == 1 and state["cwd"] == str(git_repo)
    assert state["written"] == [f"{git_repo}/cart.py"]
    # A failing test that later passed is a recovery; a person's "no" and the tool's own
    # complaint are no failures; a build still failing stays open.
    assert [f["error"] for f in state["failures"]] == ["AssertionError: total 1.0 != 100 at /x/cart.py:3",
                                                       "AssertionError: total 2.0 != 100",
                                                       "npm ERR! Missing script: build"]
    assert state["recovered"][0]["command"] == "uv run pytest" and state["recovered"][0]["tries"] == 2
    assert list(state["open"]) == ["npm run build"] and scribe.load(home(), "s1")["offset"] == state["offset"]


def test_it_reads_only_what_the_transcript_gained_and_starts_over_on_a_new_file(git_repo, tmp_path):
    path = transcript_of(tmp_path, git_repo, entries=[user("first request here", git_repo)])
    first = scribe.update(home(), "s1", path)
    assert len(first["requests"]) == 1 and scribe.update(home(), "s1", path)["offset"] == first["offset"]
    append(path, assistant([tool("b1", "Bash", command="npm run build")]),
           result("b1", "Error: Cannot find module 'vite'", error=True), user("second one, please", git_repo))
    # A line still being written is read next time, not half now.
    with path.open("a") as fh:
        fh.write('{"type": "user", "message": {"content": "half')
    state = scribe.update(home(), "s1", path)
    assert [r["text"] for r in state["requests"]] == ["first request here", "second one, please"]
    assert state["open"]["npm run build"]["error"] == "Error: Cannot find module 'vite'"
    path.write_text(json.dumps(user("a new file", git_repo)) + "\n")
    assert [r["text"] for r in scribe.update(home(), "s1", path)["requests"]] == ["a new file"]
    assert scribe.update(home(), "s1", tmp_path / "missing.jsonl") is None
    assert scribe.update(home(), "../escape", path) is None and scribe.path(home(), "../x") is None
    assert scribe.load(home(), "nobody") is None


def test_a_secret_private_text_or_a_long_paste_is_never_kept_whole(git_repo, tmp_path):
    secret = "sk-ant-api03-" + "a" * 40
    path = transcript_of(tmp_path, git_repo, entries=[
        user(f"usa la clave {secret} y " + "x" * 3000, git_repo),
        user("el cliente es <private>Nombre Real</private>, arregla su reporte", git_repo)])
    requests = scribe.update(home(), "s1", path)["requests"]
    assert secret not in requests[0]["text"] and "[secret]" in requests[0]["text"]
    assert len(requests[0]["text"]) == scribe.REQUEST_KEEP
    assert requests[1]["text"] == "el cliente es [private], arregla su reporte"


def test_the_hook_reads_a_long_transcript_a_slice_per_turn(git_repo, tmp_path):
    path = transcript_of(tmp_path, git_repo, entries=[user(f"pedido número {n}", git_repo) for n in range(6)])
    first = scribe.update(home(), "s1", path, max_bytes=1)
    assert len(first["requests"]) == 1 and first["offset"] < path.stat().st_size
    assert len(scribe.update(home(), "s1", path)["requests"]) == 6


def test_the_notes_are_due_every_few_turns_or_after_enough_conversation(git_repo, tmp_path):
    entries = [user(f"request number {n}", git_repo) for n in range(scribe.NOTES_EVERY_TURNS - 1)]
    path = transcript_of(tmp_path, git_repo, entries=entries)
    state = scribe.update(home(), "s1", path)
    assert not scribe.notes_due(state)
    append(path, assistant("y" * scribe.NOTES_EVERY_CHARS))
    state = scribe.update(home(), "s1", path)
    assert scribe.notes_due(state)
    scribe.notes_started(home(), state)
    assert not scribe.notes_due(scribe.load(home(), "s1"))


def test_a_transcript_is_found_by_its_session_id(git_repo, tmp_path):
    path = transcript_of(tmp_path, git_repo, sid="abc")
    assert scribe.find_transcript("abc") == path
    assert scribe.find_transcript("nope") is None and scribe.find_transcript("../x") is None


# --- what a worker's brief gets ---------------------------------------------------


def _thread(requests, written=(), open_=None):
    return {"requests": [{"text": t, "correction": t.startswith("no,")} for t in requests],
            "written": list(written), "open": open_ or {}}


def test_a_brief_gets_the_newest_requests_and_the_older_ones_about_the_task(git_repo):
    state = _thread(["the checkout page must keep the coupon field", "unrelated: rename the logger",
                     "update the readme badge", "fix the footer link", "make the header sticky",
                     "no, keep the old palette", "Fix the coupon field on the checkout page"],
                    written=["/r/src/Checkout.tsx", "/r/src/Footer.tsx"],
                    open_={"npm test": {"error": "1 failed", "tries": 2}})
    lines, got = scribe.context_for(state, "Fix the coupon field on the checkout page")
    text = "\n".join(lines)
    # Its own text is not repeated; the four newest go in, and the old one that shares its words.
    assert "Fix the coupon field" not in text and "must keep the coupon field" in text
    assert "rename the logger" not in text and "(correction) no, keep the old palette" in text
    assert got["requests"] == 5 and got["files"] == 2 and got["failing"] == 1 and got["chars"] == len("".join(lines))
    assert "`npm test` still fails there (2x): 1 failed" in text and "not instructions" in text
    assert scribe.context_for(None, "x") == ([], {"requests": 0, "files": 0, "failing": 0, "chars": 0})
    assert scribe.context_for(_thread(["same"]), "same")[0] == []
    small, _ = scribe.context_for(state, "the checkout", budget=400)
    assert sum(len(x) for x in small) <= 400


def test_a_task_a_session_sent_is_briefed_with_its_thread(git_repo, tmp_path, store, monkeypatch):
    transcript_of(tmp_path, git_repo, sid="sess-1",
                  entries=[user("the totals must stay in cents, never floats", git_repo),
                           user("now round the cart total once at checkout", git_repo)])
    script = Script(WorkerResult(True, None, "rounded once", "pytest: 3 passed"))
    report = run("Round the cart total once at checkout", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), session_id="sess-1")
    prompt = script.specs[0].prompt
    assert "Context from the session that sent this task" in prompt
    assert "the totals must stay in cents, never floats" in prompt
    briefed = store.last_event(report.task_id, "briefed")["data"]
    assert briefed["session"]["requests"] == 2 and briefed["context_chars"] > 0
    # Off, or no session: the task's text alone.
    monkeypatch.setenv("LOCKS_SCRIBE", "off")
    script = Script(WorkerResult(True, None, "ok", "pytest: ok"))
    report = run("Round again", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
                 session_id="sess-1")
    assert "Context from the session" not in script.specs[0].prompt
    assert store.last_event(report.task_id, "briefed")["data"]["session"] is None


def test_an_investigator_and_an_unreadable_thread_get_no_session_context(git_repo, tmp_path, store, monkeypatch):
    from locks import orchestrate

    transcript_of(tmp_path, git_repo, sid="sess-2", entries=[user("something the person asked", git_repo)])
    task = {"session_id": "sess-2", "options": json.dumps({"investigation": "inv-1"})}
    assert orchestrate._session_context(task, "x") == ([], None)
    monkeypatch.setattr(scribe, "update", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    assert orchestrate._session_context({"session_id": "sess-2", "options": "{}"}, "x") == ([], None)
    assert orchestrate._session_context({"session_id": None}, "x") == ([], None)


# --- the Stop hook ----------------------------------------------------------------


def stop_event(path, sid="s1", cwd=None):
    return {"session_id": sid, "transcript_path": str(path), "cwd": str(cwd or path.parent),
            "last_assistant_message": "done"}


def test_the_stop_hook_keeps_the_thread_and_has_the_notes_filed_when_due(git_repo, tmp_path, store, monkeypatch):
    entries = [user(f"pedido {n} sobre el carrito", git_repo) for n in range(scribe.NOTES_EVERY_TURNS)]
    path = transcript_of(tmp_path, git_repo, entries=entries)
    launched = []
    monkeypatch.setattr(hooks, "keep_notes", lambda event, root, env: launched.append(event["session_id"]) or True)
    hooks.stop(stop_event(path, cwd=git_repo), store)
    assert scribe.load(home(), "s1")["turns"] == scribe.NOTES_EVERY_TURNS and launched == ["s1"]
    assert not scribe.notes_due(scribe.load(home(), "s1"))
    # Not due, or the extraction already running: nothing more, and the counter waits.
    monkeypatch.setattr(hooks, "keep_notes", lambda *a: False)
    append(path, *[user(f"otro pedido {n}", git_repo) for n in range(scribe.NOTES_EVERY_TURNS)])
    hooks.stop(stop_event(path, cwd=git_repo), store)
    assert scribe.notes_due(scribe.load(home(), "s1"))


def test_a_scribe_that_fails_is_noted_and_the_endings_still_go_out(git_repo, tmp_path, store, monkeypatch):
    path = transcript_of(tmp_path, git_repo, entries=[user("hola", git_repo)])
    working = scribe.update
    monkeypatch.setattr(scribe, "update", lambda *a, **k: 1 / 0)
    task = store.create_task("queued work", status="done", source="locks", session_id="s1")
    store.add_event(task["id"], "finished", status="done")
    answer = hooks.stop(stop_event(path), store)
    assert answer["decision"] == "block" and "#" in answer["reason"]
    assert "ZeroDivisionError" in (home() / hooks.ERROR_LOG).read_text()
    # Turned off: no state is kept at all.
    monkeypatch.setattr(scribe, "update", working)
    monkeypatch.setenv("LOCKS_SCRIBE", "off")
    hooks.stop(stop_event(path, sid="s2"), store)
    assert scribe.load(home(), "s2") is None


# --- the relay --------------------------------------------------------------------


def test_a_relay_is_taken_once_by_another_session_in_the_same_place(git_repo, tmp_path):
    scribe.mark_relay(home(), "old", git_repo)
    assert scribe.take_relay(home(), "old", git_repo) is None  # the same session resumed: not a relay
    assert scribe.take_relay(home(), "new", git_repo / ".") == "old"
    assert scribe.take_relay(home(), "other", git_repo) is None
    marker = scribe.mark_relay(home(), "old", git_repo)
    stale = (datetime.now(UTC) - timedelta(hours=scribe.RELAY_HOURS + 1)).isoformat()
    marker.write_text(json.dumps({"session": "old", "at": stale}))
    assert scribe.take_relay(home(), "new", git_repo) is None and not marker.exists()
    marker = scribe.mark_relay(home(), "old", git_repo)
    marker.write_text(json.dumps({"session": "old", "at": "yesterday"}))
    assert scribe.take_relay(home(), "new", git_repo) is None
    marker.write_text("not json")
    assert scribe.take_relay(home(), "new", git_repo) is None


def test_a_fresh_session_after_a_relay_starts_from_the_thread_and_its_work_reports_there(git_repo, tmp_path, store):
    path = transcript_of(tmp_path, git_repo, sid="old")
    scribe.update(home(), "old", path)
    queued = store.enqueue("Add the coupon field", repo="k", cwd=str(git_repo), session_id="old")
    blocked = store.create_task("Migrate totals", status="blocked", source="locks", session_id="old")
    done = store.create_task("Old and heard", status="done", source="locks", session_id="old")
    store.add_event(done["id"], "finished", status="done")
    store.add_event(done["id"], "reported", via="stop")
    scribe.mark_relay(home(), "old", git_repo)
    event = {"session_id": "new", "cwd": str(git_repo), "source": "clear",
             "transcript_path": str(tmp_path / "new.jsonl")}
    answer = hooks.session_start(event, store, home(), {})
    text = answer["hookSpecificOutput"]["additionalContext"]
    assert "locks relay: this session continues old" in text
    assert "Planifica la migración del carrito a centavos" in text and "(correction) no, eso no" in text
    assert f"#{queued['id']} [queued] Add the coupon field" in text and "2 locks task(s)" in text
    assert store.get_task(queued["id"])["session_id"] == "new" and store.get_task(blocked["id"])["session_id"] == "new"
    assert store.get_task(done["id"])["session_id"] == "old"
    assert scribe.load(home(), "new")["relayed_from"] == "old"
    # Taken once; a resume never takes one.
    again = hooks.session_start({**event, "session_id": "newer"}, store, home(), {})
    assert "locks relay" not in (again or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
    scribe.mark_relay(home(), "old", git_repo)
    resumed = hooks.session_start({**event, "session_id": "x", "source": "resume"}, store, home(), {})
    assert "locks relay" not in (resumed or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


def test_a_relay_from_a_session_the_scribe_never_saw_is_nothing(git_repo, store):
    scribe.mark_relay(home(), "ghost", git_repo)
    answer = hooks.session_start({"session_id": "new", "cwd": str(git_repo), "source": "startup"}, store, home(), {})
    assert "locks relay" not in (answer or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


def test_the_relay_text_puts_the_person_s_words_first_and_fits(git_repo):
    state = {"session": "s", "requests": [{"text": "palabra " * 120, "correction": False}] * 40,
             "written": ["/r/x.py"], "open": {"npm test": {"error": "boom", "tries": 1}}}
    text = scribe.relay_text(state, tasks=[{"id": 3, "status": "queued", "title": "t"}], changed=["x.py"],
                             commits=["abc123 feat: x"], budget=6000)
    assert len(text) <= 6100 and text.index("requests there") < text.index("Commits made")
    assert "earlier left out" in text and "`npm test` (1x): boom" in text
    assert scribe.relay_text({"session": "s"}, tasks=[], changed=[], commits=[]) == ""
    assert scribe.commits_since(str(git_repo), "2000-01-01")[0].endswith("init")
    assert scribe.commits_since(None, "x") == [] and scribe.commits_since(str(git_repo), None) == []


def test_store_hands_a_session_s_open_work_and_its_workflow_runs_over(store):
    running = store.create_task("going", status="running", source="queue", session_id="a")
    turn = store.create_task("a prompt", status="running", source="hook", session_id="a")
    unheard = store.create_task("ended unheard", status="failed", source="locks", session_id="a")
    store.add_event(unheard["id"], "finished", status="failed")
    store._conn.execute("INSERT INTO workflow_runs (name, scope, source, definition, session_id, status, created_at, "
                        "updated_at) VALUES ('f', 'bundled', 'x', '{}', 'a', 'active', 'now', 'now')")
    assert store.hand_over("a", "b") == 2
    assert store.get_task(running["id"])["session_id"] == "b" and store.get_task(unheard["id"])["session_id"] == "b"
    assert store.get_task(turn["id"])["session_id"] == "a"
    assert store._conn.execute("SELECT session_id FROM workflow_runs").fetchone()[0] == "b"


# --- the command ------------------------------------------------------------------


def test_the_relay_command_marks_the_hand_over_from_this_session(git_repo, tmp_path, store, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    assert cli.main(["relay"]) == 1
    assert "no session to relay from here" in capsys.readouterr().err
    monkeypatch.setenv("LOCKS_SESSION_ID", "sess-9")
    assert cli.main(["relay"]) == 1
    assert "no thread to hand over yet" in capsys.readouterr().err
    transcript_of(tmp_path, git_repo, sid="sess-9")
    assert cli.main(["relay", "--show"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("relay ready") and "5 request(s) verbatim" in out and "locks relay: this session" in out
    assert scribe.take_relay(home(), "new", git_repo) == "sess-9"
    # From a terminal, the latest session locks saw in this repository.
    monkeypatch.delenv("LOCKS_SESSION_ID")
    from locks import repo

    store.touch_session("sess-9", str(git_repo), repo.key(git_repo))
    store._conn.commit()
    assert cli.main(["relay"]) == 0


def test_the_hook_entry_point_runs_the_scribe(git_repo, tmp_path, monkeypatch):
    path = transcript_of(tmp_path, git_repo, entries=[user("algo que pedí", git_repo)])
    out = io.StringIO()
    hooks.main("Stop", io.StringIO(json.dumps(stop_event(path))), out, {"LOCKS_HOME": str(home())})
    assert scribe.load(home(), "s1")["requests"][0]["text"] == "algo que pedí"


def test_old_states_and_relays_nobody_took_are_forgotten(git_repo, tmp_path, store):
    import os
    import time

    path = transcript_of(tmp_path, git_repo, entries=[user("hola", git_repo)])
    scribe.update(home(), "old", path)
    scribe.update(home(), "recent", path)
    marker = scribe.mark_relay(home(), "old", git_repo)
    week_ago = time.time() - 40 * 86400
    os.utime(scribe.path(home(), "old"), (week_ago, week_ago))
    os.utime(marker, (time.time() - 2 * 86400, time.time() - 2 * 86400))
    assert scribe.prune(home()) == 2
    assert scribe.load(home(), "old") is None and scribe.load(home(), "recent") is not None and not marker.exists()
    assert scribe.prune(tmp_path / "nowhere") == 0
    # A session that starts does the housekeeping.
    os.utime(scribe.path(home(), "recent"), (week_ago, week_ago))
    hooks.session_start({"session_id": "n", "cwd": str(git_repo), "source": "startup"}, store, home(), {})
    assert scribe.load(home(), "recent") is None


def test_the_scribe_counts_what_each_turn_read_with_the_session_s_own_tools(git_repo, tmp_path):
    entries = [
        user("mira cómo se calcula el total", git_repo),
        assistant([tool("r1", "Read", file_path=f"{git_repo}/cart.py"), tool("r2", "Bash", command="grep -rn total")]),
        result("r1", "x" * 3000), result("r2", "y" * 500),
        assistant([tool("w1", "Edit", file_path=f"{git_repo}/cart.py", old_string="a", new_string="b")]),
        result("w1", "edited"),
        user("ahora el envío", git_repo),
        assistant([tool("r3", "Grep", pattern="shipping")]), result("r3", "z" * 700),
    ]
    state = scribe.update(home(), "s9", transcript_of(tmp_path, git_repo, sid="s9", entries=entries))
    # The write's result is not reading; the turn before is kept as the last.
    assert state["read"] == {"chars": 700, "calls": 1, "last_chars": 3500, "last_calls": 2}
