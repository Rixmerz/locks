"""Where the advice lands against the window the session really runs in and the share the harness
compacts at by itself: a million-token session named only at its SessionStart, an environment that
moves or turns off the harness's compaction, the automatic compactions the transcript keeps, and the
moment work the session sent still runs."""
from __future__ import annotations

from locks import compaction, hooks
from locks.store import Store

from .test_compaction import NOW, assistant, ready, user, write


def _boundary(trigger: str, pre: int) -> dict:
    return {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-10-18T11:00:00Z",
            "compactMetadata": {"trigger": trigger, "preTokens": pre}}


def test_the_window_follows_the_session_model_and_the_harness_s_own_window(tmp_path):
    path = write(tmp_path, [assistant(150_000, NOW)])
    assert compaction.read_context(path).window == compaction.WINDOW
    assert compaction.read_context(path, model_hint="claude-opus-5-5[1m]").window == compaction.WINDOW_1M
    capped = compaction.read_context(path, model_hint="opus[1m]", env={compaction.AUTO_WINDOW_ENV: "400000"})
    assert capped.window == 400_000
    assert compaction.read_context(path, env={compaction.AUTO_WINDOW_ENV: "nope"}).window == compaction.WINDOW


def test_a_compaction_the_transcript_keeps_says_the_window_and_where_the_harness_compacts(tmp_path):
    path = write(tmp_path, [_boundary("manual", 502_035), user("go on", NOW), assistant(90_000, NOW)])
    found = compaction.read_context(path)
    assert found.window == compaction.WINDOW_1M and found.auto_share is None
    auto = compaction.read_context(write(tmp_path, [_boundary("auto", 167_000), assistant(90_000, NOW)], "a.jsonl"))
    assert auto.window == compaction.WINDOW and auto.auto_share == 167_000 / 200_000


def test_the_shares_stay_below_where_the_harness_compacts():
    assert compaction.Context().shares() == (compaction.WORTH_SHARE, compaction.PRESSING_SHARE,
                                             compaction.URGENT_SHARE)
    worth, pressing, urgent = compaction.Context(auto_share=0.6).shares()
    assert (round(urgent, 2), round(pressing, 2), round(worth, 2)) == (0.55, 0.5, 0.2)
    share, off = compaction.auto_share({compaction.AUTO_PCT_ENV: "70"}, 200_000, [190_000])
    assert share == 0.7 and not off  # the environment's override wins over what a compaction showed
    assert compaction.auto_share({"DISABLE_AUTO_COMPACT": "1"}, 200_000) == (None, True)


def test_a_lowered_harness_threshold_brings_the_advice_forward():
    plain = compaction.decide(ready(breaks=(), tokens=120_000), NOW)
    assert not plain.now  # 60% of 200k, no break: wait
    early = compaction.decide(ready(breaks=(), tokens=120_000, auto_share=0.7), NOW)
    assert early.now and "the harness compacts by itself near 70%" in early.because[0]
    assert early.data()["auto_share"] == 0.7


def test_with_the_harness_s_compaction_off_nothing_is_pressing():
    decided = compaction.decide(ready(breaks=(), tokens=190_000, auto_off=True), NOW)
    assert not decided.now and "compaction is off" in decided.because[0]


def test_work_still_running_is_a_moment_to_decide_and_the_advice_says_so():
    signals = ready(breaks=(), tokens=140_000)
    waiting = compaction.decide(signals, NOW)
    assert not waiting.now
    signals = compaction.Signals(context=signals.context, running=(41, 42))
    decided = compaction.decide(signals, NOW)
    assert decided.now and decided.running == (41, 42)
    assert "work this session sent still runs (#41, #42)" in " | ".join(decided.because)
    text = compaction.advice({"id": 9, "data": decided.data()})
    assert "its ending reaches you after a compaction too" in text
    # below the running share it is no moment yet
    low = compaction.decide(compaction.Signals(context=ready(tokens=110_000).context, running=(41,)), NOW)
    assert not low.now


def test_session_start_keeps_the_model_it_names(store: Store, tmp_path):
    hooks.handle("SessionStart", {"session_id": "s-1m", "cwd": str(tmp_path), "model": "claude-opus-5-5[1m]"},
                 store, tmp_path, {})
    assert store.meta(compaction.MODEL_META + "s-1m") == "claude-opus-5-5[1m]"


def test_running_reads_the_session_s_queued_and_running_work(store: Store):
    a = store.create_task("one", status="running", source="locks", session_id="s")
    store.create_task("two", status="done", source="locks", session_id="s")
    b = store.create_task("three", status="queued", source="queue", session_id="s")
    assert compaction.running(store, "s") == (a["id"], b["id"])
