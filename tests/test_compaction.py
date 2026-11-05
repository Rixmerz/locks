from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from locks import analysis, cli, compact, compaction, config, hooks, isolate, stops
from locks.store import Store, home
from tests.conftest import git

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def assistant(tokens: int, at: datetime, *, model: str = "claude-opus-5-5", split: dict | None = None,
              sidechain: bool = False) -> dict:
    used = {"input_tokens": 10, "cache_read_input_tokens": tokens - 110, "cache_creation_input_tokens": 100,
            "output_tokens": 40}
    if split is not None:
        used["cache_creation"] = split
    return {"type": "assistant", "timestamp": iso(at), "isSidechain": sidechain,
            "message": {"model": model, "usage": used, "content": [{"type": "text", "text": "done"}]}}


def user(text, at: datetime) -> dict:
    return {"type": "user", "timestamp": iso(at), "message": {"role": "user", "content": text}}


def tool_result(at: datetime) -> dict:
    return user([{"type": "tool_result", "tool_use_id": "x", "content": "ok"}], at)


def write(tmp_path: Path, entries: list[dict], name: str = "t.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def big(tmp_path: Path, at: datetime, *, tokens: int = 150_000, split: dict | None = None) -> Path:
    return write(tmp_path, [
        user("fix the login redirect and keep `?next=`", at - timedelta(minutes=3)),
        assistant(tokens - 5000, at - timedelta(minutes=2)),
        tool_result(at - timedelta(minutes=1)),
        assistant(tokens, at, split=split),
    ])


# --- the transcript ---------------------------------------------------------------------


def test_the_context_is_the_latest_message_s_input_cache_read_and_creation(tmp_path):
    path = write(tmp_path, [
        user("first", NOW - timedelta(minutes=9)),
        assistant(60_000, NOW - timedelta(minutes=8), split={"ephemeral_1h_input_tokens": 100}),
        user("now the second thing", NOW - timedelta(minutes=2)),
        tool_result(NOW - timedelta(minutes=1)),
        assistant(120_000, NOW - timedelta(seconds=30)),
        assistant(999_999, NOW, sidechain=True),
        {"type": "assistant", "timestamp": iso(NOW), "message": {"model": "<synthetic>", "usage": {
            "input_tokens": 0, "output_tokens": 0}}},
    ])
    found = compaction.read_context(path)
    assert found.tokens == 120_000 and found.model == "claude-opus-5-5" and found.window == compaction.WINDOW
    assert found.last_turn_at == NOW - timedelta(seconds=30)
    assert found.turn_started == NOW - timedelta(minutes=2)  # a tool's result opens no turn
    assert found.ttl_s == compaction.CACHE_TTL_LONG_S and found.ttl_known


def test_unknown_cache_and_large_windows(tmp_path):
    plain = compaction.read_context(write(tmp_path, [assistant(50_000, NOW)]))
    assert plain.ttl_s == compaction.CACHE_TTL_S and not plain.ttl_known and plain.turn_started is None
    five = compaction.read_context(write(tmp_path, [assistant(5000, NOW, split={"ephemeral_5m_input_tokens": 9})]))
    assert five.ttl_s == compaction.CACHE_TTL_S and five.ttl_known
    assert compaction.read_context(write(tmp_path, [assistant(300_000, NOW)])).window == compaction.WINDOW_1M
    assert compaction.window_of("claude-opus-5-5[1m]", 10) == compaction.WINDOW_1M
    assert compaction.read_context(tmp_path / "missing.jsonl") == compaction.Context()


def test_a_session_that_went_past_200k_is_judged_against_a_million(tmp_path):
    """The model id Claude Code writes never says `[1m]`: a 1M session that held
    700k and holds 120k after a compaction is at 12% of its window, not 60%."""
    path = write(tmp_path, [
        user("first", NOW - timedelta(minutes=30)),
        assistant(700_000, NOW - timedelta(minutes=29)),
        user("after the compaction", NOW - timedelta(minutes=2)),
        assistant(120_000, NOW),
        assistant(900_000, NOW, sidechain=True),
    ])
    found = compaction.read_context(path)
    assert found.tokens == 120_000 and found.window == compaction.WINDOW_1M and not compaction.worth(found)
    # a subagent's larger context says nothing of the main conversation's window
    small = compaction.read_context(write(tmp_path, [assistant(120_000, NOW), assistant(900_000, NOW, sidechain=True)],
                                          name="side.jsonl"))
    assert small.window == compaction.WINDOW and compaction.worth(small)


def test_only_the_tail_of_a_long_transcript_is_read(tmp_path):
    path = write(tmp_path, [user("x" * 500, NOW), assistant(80_000, NOW)])
    entries = compaction._tail(path, size=len(json.dumps(assistant(80_000, NOW))) + 40)
    assert [e["type"] for e in entries] == ["assistant"]  # the cut first line is dropped


# --- the decision -------------------------------------------------------------------------


def ready(breaks: tuple[str, ...] = ("#12 ended (done) and was reported",), **context) -> compaction.Signals:
    fields = {"tokens": 150_000, "model": "claude-opus-5-5", "last_turn_at": NOW - timedelta(seconds=20),
              "turn_started": NOW - timedelta(minutes=4), **context}
    return compaction.Signals(context=compaction.Context(**fields), breaks=breaks)


def test_now_when_every_condition_holds_and_each_reason_is_said():
    decided = compaction.decide(ready(), NOW)
    assert decided.now and decided.worth
    said = " | ".join(decided.because)
    assert "150,000 tokens of 200,000 (75%)" in said and "a natural break: #12 ended" in said
    assert "no run of this session holds the checkout" in said and "ended 20s ago" in said
    assert "assumed: nothing says the plan" in said and "nothing compacted or suggested" in said
    assert not any(b.startswith("not now") for b in decided.because)
    # the cache read is a tenth of reprocessing the history cold
    assert decided.warm_usd and decided.cold_usd and decided.warm_usd / decided.cold_usd == pytest.approx(0.1)
    assert decided.warm_until == NOW - timedelta(seconds=20) + timedelta(seconds=300)


@pytest.mark.parametrize(("change", "words"), [
    ({"breaks": ()}, "not now: no natural break"),
    ({"holding": 7}, "not now: #7, a run this session started, holds the checkout"),
    ({"last_compaction": NOW - timedelta(minutes=10)}, "not now: compacted or suggested 10m ago"),
    ({"context": compaction.Context(tokens=150_000, last_turn_at=NOW - timedelta(seconds=290))},
     "not now: the last turn ended 290s ago: the cache (5m, assumed: nothing says the plan) is cold"),
    ({"context": compaction.Context(tokens=150_000)}, "not now: when the last turn ended is not known"),
])
def test_each_condition_alone_says_not_now(change, words):
    signals = ready()
    signals = compaction.Signals(**{**{f: getattr(signals, f) for f in signals.__slots__}, **change})
    decided = compaction.decide(signals, NOW)
    assert not decided.now and decided.worth
    assert words in " | ".join(decided.because)


def test_a_known_hour_long_cache_is_warm_after_twenty_minutes():
    decided = compaction.decide(ready(last_turn_at=NOW - timedelta(minutes=20), ttl_s=3600, ttl_known=True), NOW)
    assert decided.now and "the cache (60m, the session's cache writes say so) is warm" in " ".join(decided.because)


def test_too_small_unknown_or_a_worker_is_never_worth_a_record():
    small = compaction.decide(ready(tokens=40_000), NOW)
    assert not small.now and not small.worth and "under the 50%" in small.because[0]
    assert not compaction.decide(ready(tokens=None), NOW).worth
    worker = compaction.Signals(context=ready().context, worker=True)
    assert not compaction.decide(worker, NOW).worth
    assert compaction.cost("mystery-model", 100) == (None, None)


# --- what the decision reads: git, the lock, the store -----------------------------------------


def test_a_merge_into_the_checkout_is_a_break(git_repo):
    git(git_repo, "checkout", "-q", "-b", "feature")
    (git_repo / "f.py").write_text("x\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "feature")
    git(git_repo, "checkout", "-q", "main")
    git(git_repo, "merge", "-q", "feature")
    since = datetime.now(UTC) - timedelta(minutes=1)
    assert compaction.merges(str(git_repo), since) == ["merge feature: Fast-forward"]
    assert compaction.merges(str(git_repo), datetime.now(UTC) + timedelta(minutes=5)) == []
    assert compaction.merges(None, since) == [] and compaction.merges(str(git_repo / "nope"), since) == []


def test_reported_work_is_a_break_and_a_step_the_engine_went_past_is_not(store: Store):
    sent = store.create_task("build the export", status="done", source="locks", session_id="s1")
    step = store.create_task("a workflow step", status="done", source="queue", session_id="s1")
    turn = store.create_task("a turn", status="done", source="hook", session_id="s1")
    store.add_event(sent["id"], "reported", via="stop")
    store.add_event(step["id"], "reported", via="workflow")
    store.add_event(turn["id"], "reported", via="stop")
    since = datetime.now(UTC) - timedelta(minutes=5)
    assert compaction.breaks(store, "s1", None, since) == [f"#{sent['id']} ended (done) and was reported"]
    assert compaction.breaks(store, "s1", None, datetime.now(UTC) + timedelta(minutes=5)) == []


def test_only_this_session_s_run_holding_the_checkout_counts(store: Store, git_repo):
    root = home()
    mine = store.create_task("mine", status="running", source="locks", session_id="s1")
    other = store.create_task("other", status="running", source="locks", session_id="s2")
    assert compaction.holding(store, root, "s1", str(git_repo)) is None
    with isolate.in_place(root, git_repo, mine["id"], waiting=print, check=lambda: None):
        assert compaction.holding(store, root, "s1", str(git_repo)) == mine["id"]
        assert compaction.holding(store, root, "s2", str(git_repo)) is None
    with isolate.in_place(root, git_repo, other["id"], waiting=print, check=lambda: None):
        assert compaction.holding(store, root, "s1", str(git_repo)) is None
    assert compaction.holding(store, root, "s1", None) is None
    assert isolate.holder(root, git_repo) is None


# --- the instructions -------------------------------------------------------------------------


def test_the_instructions_keep_the_person_s_words_and_the_open_work_within_one_line():
    requests = [{"text": "use the token sk-abcdefghijklmnop for the call", "correction": False},
                {"text": "no, never touch the billing table", "correction": True},
                *({"text": f"request {i}\nsecond line", "correction": False} for i in range(6))]
    text = compaction.instructions(requests=requests, open_work=["#12 [blocked] the export, next: locks resume 12"],
                                   branches=["locks/task-7 (#7 the login fix)"])
    assert "\n" not in text and text.startswith(compaction.HEAD)
    assert "Open work: #12 [blocked] the export, next: locks resume 12." in text
    assert "Branches waiting to merge: locks/task-7 (#7 the login fix)." in text
    assert '"request 2 second line" "request 3 second line" "request 4 second line" "request 5 second line"' in text
    assert "request 1" not in text
    assert 'Decisions the person made (oldest first): "no, never touch the billing table".' in text
    line = compaction.line(42, text)
    assert line.startswith(compact.ASKED) and line.startswith("/compact [locks-compaction #42] ")
    assert compaction.MARKER_RE.search(line).group(1) == "42"


def test_the_instructions_are_bounded_and_mask_secrets():
    requests = [{"text": f"password: hunter{i} " + "y" * 400, "correction": i % 2 == 0} for i in range(50)]
    text = compaction.instructions(requests=requests, open_work=[f"#{i} [running] " + "w" * 200 for i in range(30)],
                                   branches=[f"locks/task-{i}" for i in range(30)])
    assert len(compaction.line(999999, text)) <= compaction.INSTRUCTIONS_CHARS
    assert "Open work: #0 [running]" in text and "hunter" not in text  # what is most needed comes first
    alone = compaction.instructions(requests=requests, open_work=[], branches=[])
    assert len(alone) <= compaction.INSTRUCTIONS_CHARS - 36
    assert "hunter" not in alone and alone.count("[secret]") == compaction.RECENT_REQUESTS


def test_open_work_carries_its_next_command_and_only_unmerged_branches_wait(store: Store, git_repo):
    blocked = store.create_task("allow ls", status="blocked", source="locks", session_id="s1", cwd=str(git_repo))
    store.add_event(blocked["id"], "finished", status="blocked",
                    stop=stops.Stop("permission", "refused", denied=["Bash(ls)"], allow=["Bash(ls:*)"]).data())
    for name in ("locks/task-8", "locks/task-9"):
        git(git_repo, "checkout", "-q", "-b", name)
        (git_repo / f"{name[-1]}.py").write_text("x\n")
        git(git_repo, "add", "-A")
        git(git_repo, "commit", "-q", "-m", name)
        git(git_repo, "checkout", "-q", "main")
    git(git_repo, "merge", "-q", "locks/task-9")
    for name in ("locks/task-8", "locks/task-9", "locks/gone"):
        done = store.create_task(f"work on {name}", status="done", source="locks", session_id="s1")
        store.add_event(done["id"], "finished", status="done", branch=name)
    work, branches = compaction.what_is_open(store, "s1", str(git_repo))
    assert work == [f"#{blocked['id']} [blocked] allow ls, next: locks resume {blocked['id']} --allow 'Bash(ls:*)'"]
    assert len(branches) == 1 and branches[0].startswith("locks/task-8 (#")


# --- the Stop hook records it ------------------------------------------------------------------


def turn(store: Store, cwd: Path, text: str = "fix the login redirect and keep it") -> dict:
    hooks.user_prompt_submit({"session_id": "s1", "prompt": text, "prompt_id": "p1", "cwd": str(cwd)}, store)
    return store.last_prompt_task("s1")


def decided(store: Store) -> list[dict]:
    return store.session_events("s1", (compaction.DECIDED,))


def test_stop_decides_at_a_break_and_records_the_line_s_instructions(store: Store, tmp_path, git_repo):
    turn(store, git_repo)
    sent = store.create_task("build the export", status="done", source="locks", session_id="s1")
    store.add_event(sent["id"], "finished", status="done")
    store.add_event(sent["id"], "reported", via="prompt")
    path = big(tmp_path, datetime.now(UTC))
    assert hooks.stop({"session_id": "s1", "prompt_id": "p1", "transcript_path": str(path),
                       "cwd": str(git_repo)}, store) is None
    [event] = decided(store)
    data = event["data"]
    assert data["now"] and data["mode"] == "orchestrator" and data["cwd"] == str(git_repo)
    assert '"fix the login redirect and keep `?next=`"' in data["instructions"]  # as the transcript has it
    assert any("a natural break" in b for b in data["because"])


def test_stop_records_nothing_when_off_in_a_worker_small_or_still_going(store: Store, tmp_path, git_repo,
                                                                        monkeypatch):
    turn(store, git_repo)
    event = {"session_id": "s1", "prompt_id": "p1", "transcript_path": str(big(tmp_path, datetime.now(UTC))),
             "cwd": str(git_repo)}
    monkeypatch.setenv("LOCKS_COMPACT", "off")
    hooks.stop(event, store)
    monkeypatch.setenv("LOCKS_COMPACT", "auto")
    monkeypatch.setenv("LOCKS_WORKER_TASK", "3")
    hooks.stop(event, store)
    monkeypatch.delenv("LOCKS_WORKER_TASK")
    small = write(tmp_path, [assistant(20_000, datetime.now(UTC))], "small.jsonl")
    hooks.stop({**event, "transcript_path": str(small)}, store)
    assert decided(store) == []
    # endings that block the turn's end: the turn goes on, it is no moment yet
    ended = store.create_task("work", status="done", source="locks", session_id="s1")
    store.add_event(ended["id"], "finished", status="done")
    assert hooks.stop(event, store)["decision"] == "block"
    assert decided(store) == []
    hooks.stop(event, store)
    assert decided(store)[0]["data"]["mode"] == "auto"


def test_a_decision_that_fails_costs_the_turn_nothing(store: Store, tmp_path, git_repo, monkeypatch):
    turn(store, git_repo)
    monkeypatch.setattr(compaction, "consider", lambda *a, **k: 1 / 0)
    assert hooks.stop({"session_id": "s1", "prompt_id": "p1", "transcript_path": str(tmp_path / "t.jsonl"),
                       "cwd": str(git_repo)}, store) is None
    assert "Stop (compaction)" in (home() / hooks.ERROR_LOG).read_text()


# --- the hooks see compactions, and keep the digest ----------------------------------------------


def test_a_compaction_locks_asked_for_is_seen_by_its_marker_and_still_gets_the_digest(store: Store, tmp_path,
                                                                                      git_repo):
    turn(store, git_repo)
    path = big(tmp_path, datetime.now(UTC))
    event = {"session_id": "s1", "transcript_path": str(path), "cwd": str(git_repo), "trigger": "manual",
             "custom_instructions": compaction.line(5, "keep it")[len("/compact "):]}
    hooks.handle("PreCompact", event, store, home(), {})
    after = {k: v for k, v in event.items() if k != "custom_instructions"}
    assert hooks.handle("PostCompact", {**after, "compact_summary": "s"}, store, home(), {}) is None
    answer = hooks.session_start({"session_id": "s1", "source": "compact", "cwd": str(git_repo)}, store, home(), {})
    assert "fix the login redirect" in answer["hookSpecificOutput"]["additionalContext"]
    seen = [e["data"] for e in store.session_events("s1", (compaction.SEEN,))]
    assert seen == [{"source": "PreCompact", "trigger": "manual", "marker": 5},
                    {"source": "PostCompact", "trigger": "manual", "marker": None},
                    {"source": "SessionStart", "trigger": None, "marker": None}]
    assert compaction.seen(store, "nobody", "PreCompact") is None


def test_the_line_reaching_the_model_as_a_message_is_no_request(store: Store, git_repo):
    text = compaction.line(9, compaction.HEAD)
    assert hooks.user_prompt_submit({"session_id": "s1", "prompt": text, "prompt_id": "p9",
                                     "cwd": str(git_repo)}, store) is None
    assert store.last_prompt_task("s1") is None
    assert compact._typed({"message": {"content": text}}) is None
    assert "it is no request" in text and "did not run as a command" in text


# --- the mod's side: deliver once, settle, learn the build -----------------------------------------


@pytest.fixture
def pending_decision(store: Store, tmp_path, git_repo) -> dict:
    task = turn(store, git_repo)
    data = {**compaction.decide(ready(last_turn_at=datetime.now(UTC)), datetime.now(UTC)).data(),
            "mode": "suggest", "cwd": str(git_repo), "instructions": "keep the requests"}
    data["id"] = store.add_event(task["id"], compaction.DECIDED, **data)
    return data


def step(store: Store, mode: str, version: str = "2.1.302", at: datetime | None = None) -> dict:
    return compaction.next_step(store, home(), "s1", version=version, mode=mode, at=at)


def test_suggest_hands_the_line_once(store: Store, pending_decision):
    first = step(store, "suggest")
    assert first == {"action": "suggest", "decision": pending_decision["id"],
                     "line": f"/compact [locks-compaction #{pending_decision['id']}] keep the requests"}
    assert step(store, "suggest") == {"action": None}
    assert step(store, "off") == {"action": None}


def test_off_takes_nothing_and_a_cold_or_compacted_decision_is_gone(store: Store, pending_decision):
    assert step(store, "off") == {"action": None}
    later = datetime.now(UTC) + timedelta(seconds=compaction.CACHE_TTL_S)
    assert step(store, "suggest", at=later) == {"action": None}
    compaction.seen(store, "s1", "PreCompact", trigger="auto")
    assert step(store, "suggest") == {"action": None}


def test_a_run_that_took_the_checkout_since_leaves_it_waiting(store: Store, pending_decision, git_repo):
    run = store.create_task("run", status="running", source="locks", session_id="s1")
    with isolate.in_place(home(), git_repo, run["id"], waiting=print, check=lambda: None):
        assert step(store, "auto") == {"action": None, "wait": True}
    assert step(store, "auto")["action"] == "submit"


def test_auto_submits_and_the_marker_shows_this_build_runs_it(store: Store, pending_decision):
    decision = pending_decision["id"]
    assert step(store, "auto")["action"] == "submit"
    compaction.seen(store, "s1", "PreCompact", trigger="manual", instructions=f"[locks-compaction #{decision}] x")
    assert step(store, "auto") == {"action": None}
    assert compaction.builds(home())["2.1.302"]["verdict"] == compaction.RUNS
    [settled] = store.session_events("s1", (compaction.SETTLED,))
    assert settled["data"]["ran"] and settled["data"]["evidence"] == "PreCompact carried its marker"


def test_a_build_that_never_ran_it_falls_back_to_suggest_and_says_so_once(store: Store, pending_decision,
                                                                          git_repo):
    assert step(store, "auto")["action"] == "submit"
    assert step(store, "auto") == {"action": None}  # still within the window: undecided
    late = datetime.now(UTC) + timedelta(seconds=compaction.DETECT_S + 1)
    found = step(store, "auto", at=late)
    assert "did not run the /compact locks submitted" in found["said"] and "2.1.302" in found["said"]
    assert compaction.builds(home())["2.1.302"]["verdict"] == compaction.IGNORED
    [settled] = store.session_events("s1", (compaction.SETTLED,))
    assert not settled["data"]["ran"]
    # the next decision on that build is shown, never submitted; said once
    task = store.last_prompt_task("s1")
    again = store.add_event(task["id"], compaction.DECIDED, **{**pending_decision, "id": None})
    assert step(store, "auto") == {"action": "suggest", "decision": again,
                                   "line": f"/compact [locks-compaction #{again}] keep the requests"}
    # a new Claude Code version is tried again
    third = store.add_event(task["id"], compaction.DECIDED, **{**pending_decision, "id": None})
    assert step(store, "auto", version="2.1.400") == {
        "action": "submit", "decision": third, "line": f"/compact [locks-compaction #{third}] keep the requests",
        "instructions": f"[locks-compaction #{third}] keep the requests"}


@pytest.mark.parametrize(("seen", "ran"), [
    ([("PostCompact", "manual")], "a manual compaction followed (PostCompact)"),
    ([("SessionStart", None)], "the session started again from a compaction"),
    ([("PreCompact", "auto"), ("SessionStart", None)], None),
])
def test_what_counts_as_the_submitted_line_having_run(store: Store, pending_decision, seen, ran):
    step(store, "auto")
    for source, trigger in seen:
        compaction.seen(store, "s1", source, trigger=trigger)
    step(store, "auto", at=datetime.now(UTC) + timedelta(seconds=compaction.DETECT_S + 1))
    [settled] = store.session_events("s1", (compaction.SETTLED,))
    assert settled["data"]["ran"] is (ran is not None)
    assert settled["data"]["evidence"] == (ran or f"no compaction seen within {compaction.DETECT_S}s of the submit")


def test_a_line_shown_instead_of_submitted_is_never_read_as_ignored(store: Store, pending_decision):
    assert step(store, "auto")["action"] == "submit"
    assert compaction.delivered(store, "s1", pending_decision["id"], how="suggest", why="a turn started")
    assert not compaction.delivered(store, "s1", 99999, how="suggest", why="x")
    step(store, "auto", at=datetime.now(UTC) + timedelta(seconds=compaction.DETECT_S + 1))
    assert store.session_events("s1", (compaction.SETTLED,)) == []
    assert compaction.builds(home()) == {}


def test_what_the_engine_answered_is_kept_and_only_a_compaction_teaches_the_build(store: Store, pending_decision,
                                                                                   capsys):
    decision = str(pending_decision["id"])
    assert step(store, "auto")["action"] == "submit"
    # a call that failed (a turn started, an error the mod cannot read) says nothing of the build
    assert cli.main(["compaction", "delivered", decision, "--session", "s1", "--version", "2.1.302",
                     "--rejected", "Error: a turn is running", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"kept": True}
    [shown] = [e for e in store.session_events("s1", (compaction.DELIVERED,)) if e["data"]["how"] == "suggest"]
    assert "the engine rejected it: Error: a turn is running" in shown["data"]["why"]
    assert compaction.settle(store, home(), "s1", at=datetime.now(UTC) + timedelta(hours=1)) is None
    assert compaction.builds(home()) == {}
    # a hook's veto is settled, and is no verdict on the build either
    task = store.last_prompt_task("s1")
    vetoed = store.add_event(task["id"], compaction.DECIDED, **{**pending_decision, "id": None})
    assert step(store, "auto")["action"] == "submit"
    assert cli.main(["compaction", "delivered", str(vetoed), "--session", "s1", "--version", "2.1.302",
                     "--skipped", "blocked by a PreCompact hook", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"kept": True}
    [settled] = store.session_events("s1", (compaction.SETTLED,))
    assert settled["data"]["ran"] is False
    assert settled["data"]["evidence"] == "a hook vetoed it: blocked by a PreCompact hook"
    assert compaction.builds(home()) == {}
    # a compaction that stood teaches the build, settled once
    ran = store.add_event(task["id"], compaction.DECIDED, **{**pending_decision, "id": None})
    assert step(store, "auto")["action"] == "submit"
    assert cli.main(["compaction", "delivered", str(ran), "--session", "s1", "--version", "2.1.302", "--ran",
                     "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"kept": True}
    assert compaction.builds(home())["2.1.302"]["verdict"] == compaction.RUNS
    assert [e["data"]["ran"] for e in store.session_events("s1", (compaction.SETTLED,))] == [False, True]
    assert compaction.compacted(store, home(), "s1", 99999, version="2.1.302") == {"kept": False}


# --- the setting and the command ---------------------------------------------------------------------


def test_the_compact_setting_takes_its_words():
    # Every environment carries the fixture's home: one without it is the person's.
    env = {"LOCKS_HOME": str(home())}
    assert config.choice("compact", env) == "orchestrator"  # the orchestrating session decides
    config.sync_plugin_options({**env, "CLAUDE_PLUGIN_OPTION_COMPACT": "Auto", "CLAUDE_PLUGIN_OPTION_NOTES": "false"})
    assert config.load(env)["compact"] == "auto" and config.load(env)["notes"] is False
    config.sync_plugin_options({**env, "CLAUDE_PLUGIN_OPTION_COMPACT": "off"})
    assert config.load(env)["compact"] == "off"  # a word, never read as a boolean
    config.sync_plugin_options({**env, "CLAUDE_PLUGIN_OPTION_COMPACT": "sometimes"})
    assert config.choice("compact", env) == "off"
    assert config.choice("compact", {**env, "LOCKS_COMPACT": "suggest"}) == "suggest"
    assert config.choice("compact", {**env, "LOCKS_COMPACT": "bogus"}) == "off"


def test_locks_config_compact(capsys, monkeypatch):
    monkeypatch.delenv("LOCKS_COMPACT", raising=False)
    assert cli.main(["config", "compact", "auto"]) == 0
    assert capsys.readouterr().out.strip() == 'compact = "auto"'
    assert cli.main(["config", "compact", "on"]) == 1
    assert "expected one of orchestrator, suggest, auto, off" in capsys.readouterr().err
    assert config.load()["compact"] == "auto"


def test_locks_compaction_next_and_delivered(store: Store, pending_decision, capsys, monkeypatch):
    monkeypatch.setenv("LOCKS_COMPACT", "auto")
    assert cli.main(["compaction", "next", "--session", "s1", "--version", "2.1.302", "--json"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["action"] == "submit" and answer["decision"] == pending_decision["id"]
    assert cli.main(["compaction", "delivered", str(answer["decision"]), "--session", "s1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"kept": True}
    assert cli.main(["compaction", "next", "--session", "s1"]) == 0
    assert capsys.readouterr().out == ""
    monkeypatch.setenv("LOCKS_WORKER_TASK", "4")
    assert cli.main(["compaction", "next", "--session", "s1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"action": None}
    # with the setting off, the idle poll's question opens no store
    monkeypatch.delenv("LOCKS_WORKER_TASK")
    monkeypatch.setenv("LOCKS_COMPACT", "off")
    monkeypatch.setattr(cli.Store, "open", lambda *a, **k: pytest.fail("the store was opened"))
    assert cli.main(["compaction", "next", "--session", "s1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"action": None}


def test_orchestrator_mode_advises_the_session_and_never_submits_locks_s_own_moment(store: Store,
                                                                                   pending_decision):
    found = step(store, "orchestrator")
    assert found["action"] == "advise" and found["decision"] == pending_decision["id"] and "line" not in found
    assert found["advice"].startswith(f"locks [compaction #{pending_decision['id']}]: this looks like a good moment")
    assert "It is your call" in found["advice"] and "`compact` tool" in found["advice"]
    assert not any(r in found["advice"] for r in ("not now",))
    assert step(store, "orchestrator") == {"action": None}  # once


def test_the_session_s_own_request_runs_whatever_the_cache_with_its_keep_quoted_apart(store: Store, tmp_path,
                                                                                      git_repo):
    assert compaction.request(store, home(), session_id="s1", cwd=str(git_repo)) == {
        "error": "locks has seen no turn of this session yet"}
    turn(store, git_repo)
    sent = store.create_task("build the export", status="running", source="locks", session_id="s1")
    asked = compaction.request(store, home(), session_id="s1", cwd=str(git_repo), why="the feature landed",
                               keep="the open question on refunds", transcript=big(tmp_path, datetime.now(UTC)))
    assert asked["requested"] and asked["tokens"] == 150_000
    assert asked["because"] == ["the orchestrating session decided to compact: the feature landed"]
    assert asked["instructions"].endswith(
        'The orchestrating session asks the summary to also keep: "the open question on refunds".')
    assert f"#{sent['id']} [running] build the export" in asked["instructions"]
    # An hour later, the cache long cold: still the session's call, submitted once.
    later = datetime.now(UTC) + timedelta(minutes=50)
    found = step(store, "orchestrator", at=later)
    assert found["action"] == "submit" and found["decision"] == asked["id"]
    assert found["instructions"].startswith(f"[locks-compaction #{asked['id']}] ")
    assert step(store, "orchestrator") == {"action": None}
    # Without a reason or a keep, it still goes, saying so.
    bare = compaction.request(store, home(), session_id="s1", cwd=str(git_repo), transcript=tmp_path / "none")
    assert bare["why"] == "no reason given" and not bare["keep"] and "also keep" not in bare["instructions"]
    assert step(store, "suggest")["action"] == "submit"  # whatever the mode: the session asked


def test_locks_compaction_request(store: Store, git_repo, capsys, monkeypatch):
    turn(store, git_repo)
    assert cli.main(["compaction", "request", "--session", "s1", "--cwd", str(git_repo), "--why", "done",
                     "--keep", "x", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["requested"]
    monkeypatch.setenv("LOCKS_COMPACT", "off")
    assert cli.main(["compaction", "request", "--session", "s1", "--json"]) == 0
    assert "turned locks' compaction off" in json.loads(capsys.readouterr().out)["error"]
    assert cli.main(["compaction", "request", "--session", "s1"]) == 1
    assert "turned locks' compaction off" in capsys.readouterr().err
    monkeypatch.setenv("LOCKS_WORKER_TASK", "4")
    assert cli.main(["compaction", "request", "--session", "s1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"error": "a locks worker never compacts"}


def test_the_human_form_prints_the_line(store: Store, pending_decision, capsys, monkeypatch):
    monkeypatch.setenv("LOCKS_COMPACT", "suggest")
    assert cli.main(["compaction", "next", "--session", "s1"]) == 0
    assert capsys.readouterr().out.startswith(f"/compact [locks-compaction #{pending_decision['id']}]")


# --- locks stats ---------------------------------------------------------------------------------------


def test_stats_lists_each_decision_its_cost_against_a_cold_one_and_whether_it_ran(store: Store, pending_decision,
                                                                                  capsys):
    task = store.last_prompt_task("s1")
    wait = compaction.decide(ready(breaks=()), datetime.now(UTC))
    store.add_event(task["id"], compaction.DECIDED, **wait.data(), mode="suggest")
    step(store, "suggest")
    compaction.seen(store, "s1", "PreCompact", trigger="manual",
                    instructions=f"[locks-compaction #{pending_decision['id']}] keep")
    report = analysis.compute(store, days=1)
    first, second = report["compactions"]
    assert first["now"] and first["delivered"] == "suggest" and first["ran"] is True
    assert first["evidence"] == "PreCompact carried its marker" and first["warm_share"] == 0.1
    assert not second["now"] and second["ran"] is None
    text = analysis.text(report)
    assert "compaction decisions 2  said now 1  delivered 1  ran 1" in text
    assert "now  warm $" in text and "(10%)  suggest, ran" in text
    assert "wait" in text and "not now: no natural break" in text
    assert cli.main(["stats", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["compactions"]) == 2


def test_a_context_near_full_does_not_wait_for_a_natural_break():
    # 172,000 of 200,000 (86%) and no work ended: the harness's own compaction is near.
    decided = compaction.decide(ready(breaks=(), tokens=172_000), NOW)
    assert decided.now and "the harness's own compaction is near" in " | ".join(decided.because)
    # Under the pressing share, a break is still waited for.
    assert not compaction.decide(ready(breaks=(), tokens=150_000), NOW).now


def test_a_then_reaches_the_session_once_its_compaction_finished(store: Store, tmp_path, git_repo):
    turn(store, git_repo)
    asked = compaction.request(store, home(), session_id="s1", cwd=str(git_repo), why="the feature landed",
                               then="merge #12, then start the export", transcript=tmp_path / "none")
    assert asked["then"] == "merge #12, then start the export"
    assert "merge #12" not in asked["instructions"]  # the summarizer is not told to do it
    assert step(store, "orchestrator")["action"] == "submit"
    # Delivered, not finished yet: the mod is told to look again.
    assert step(store, "orchestrator") == {"action": None, "wait": True}
    compaction.seen(store, "s1", "PreCompact", trigger="manual",
                    instructions=f"[locks-compaction #{asked['id']}] x")
    assert step(store, "orchestrator") == {"action": None, "wait": True}  # started, not done
    compaction.seen(store, "s1", "PostCompact", trigger="manual")
    found = step(store, "orchestrator")
    assert found["action"] == "continue" and found["decision"] == asked["id"]
    assert found["text"].startswith(f"locks [compaction #{asked['id']}]: this session compacted.")
    assert '"merge #12, then start the export". Go on with it now.' in found["text"]
    assert step(store, "orchestrator") == {"action": None}  # once
    assert [e["data"]["decision"] for e in store.session_events("s1", (compaction.CONTINUED,))] == [asked["id"]]


def test_a_then_follows_a_settled_compaction_a_veto_too_and_never_one_never_delivered(store: Store, tmp_path,
                                                                                      git_repo):
    turn(store, git_repo)
    asked = compaction.request(store, home(), session_id="s1", cwd=str(git_repo), then="go on",
                               transcript=tmp_path / "none")
    assert compaction.continuation(store, "s1", datetime.now(UTC)) is None  # never delivered
    assert step(store, "orchestrator")["action"] == "submit"
    compaction.compacted(store, home(), "s1", asked["id"], version="2.1.302", skip="blocked by a hook")
    assert step(store, "orchestrator")["action"] == "continue"
    # A delivery long past that never finished is not waited for.
    again = compaction.request(store, home(), session_id="s1", cwd=str(git_repo), then="go on",
                               transcript=tmp_path / "none")
    assert step(store, "orchestrator")["decision"] == again["id"]
    late = datetime.now(UTC) + timedelta(seconds=compaction.DETECT_S + 1)
    assert compaction.continuation(store, "s1", late) is None
    # Without a then, a finished compaction hands nothing over.
    bare = compaction.request(store, home(), session_id="s1", cwd=str(git_repo), transcript=tmp_path / "none")
    assert "then" not in bare


def test_locks_compaction_request_then(store: Store, git_repo, capsys):
    turn(store, git_repo)
    assert cli.main(["compaction", "request", "--session", "s1", "--cwd", str(git_repo), "--why", "done",
                     "--then", "start the export", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["then"] == "start the export"


def _own(store: Store, base: dict) -> int:
    """Another decision of locks' own, after whatever came before."""
    task = store.last_prompt_task("s1")
    return store.add_event(task["id"], compaction.DECIDED, **{**base, "id": None})


def test_what_the_session_asked_for_is_not_buried_by_a_later_decision_of_locks(store: Store, pending_decision):
    asked = compaction.request(store, home(), session_id="s1", cwd=pending_decision["cwd"], why="done with E5")
    _own(store, pending_decision)
    _own(store, pending_decision)
    got = step(store, "orchestrator")
    assert got["action"] == "submit" and got["decision"] == asked["id"]


def test_nothing_is_advised_over_a_compaction_the_session_asked_for_while_it_runs(store: Store, pending_decision):
    compaction.request(store, home(), session_id="s1", cwd=pending_decision["cwd"], why="done")
    assert step(store, "orchestrator")["action"] == "submit"
    for _ in range(3):  # locks keeps deciding while the context is full
        _own(store, pending_decision)
        assert step(store, "orchestrator") == {"action": None}


def test_locks_advises_at_most_once_per_prompt_of_the_person(store: Store, pending_decision):
    assert step(store, "orchestrator")["action"] == "advise"
    _own(store, pending_decision)
    assert step(store, "orchestrator") == {"action": None}
    before = store.last_prompt_task("s1")["id"]
    hooks.user_prompt_submit({"session_id": "s1", "prompt": "now merge the filters", "prompt_id": "p2",
                              "cwd": pending_decision["cwd"]}, store)  # the person speaks again
    assert store.last_prompt_task("s1")["id"] != before
    _own(store, pending_decision)
    assert step(store, "orchestrator")["action"] == "advise"
