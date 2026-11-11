"""Retention of the trace folder and the hook error log."""
import os
import time

from locks import hooks, trace


def _session(home, sid, age_days, size=10, transcript=True):
    folder = home / trace.DIR
    folder.mkdir(parents=True, exist_ok=True)
    names = [f"{sid}.jsonl"] + ([f"{sid}.transcript.jsonl"] if transcript else [])
    when = time.time() - age_days * 86400
    for name in names:
        (folder / name).write_bytes(b"x" * size)
        os.utime(folder / name, (when, when))


def test_prune_removes_old_sessions_with_their_transcripts(tmp_path):
    _session(tmp_path, "old", trace.KEEP_DAYS + 1)
    _session(tmp_path, "new", 1)
    out = trace.prune(tmp_path)
    assert out == {"sessions": 1, "bytes": 20}
    assert sorted(p.name for p in (tmp_path / trace.DIR).iterdir()) == ["new.jsonl", "new.transcript.jsonl"]


def test_prune_keeps_the_current_session_and_a_session_with_a_fresh_file(tmp_path):
    _session(tmp_path, "cur", 99)
    _session(tmp_path, "mixed", 99)
    fresh = tmp_path / trace.DIR / "mixed.transcript.jsonl"
    os.utime(fresh, None)
    assert trace.prune(tmp_path, keep="cur")["sessions"] == 0
    assert (tmp_path / trace.DIR / "cur.jsonl").exists()
    assert (tmp_path / trace.DIR / "mixed.jsonl").exists()


def test_prune_cuts_oldest_first_down_to_the_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(trace, "KEEP_MB", 1)
    mb = 1024 * 1024
    _session(tmp_path, "a", 3, size=mb, transcript=False)
    _session(tmp_path, "b", 2, size=mb, transcript=False)
    _session(tmp_path, "c", 1, size=mb, transcript=False)
    out = trace.prune(tmp_path)
    assert out["sessions"] == 2
    assert [p.name for p in (tmp_path / trace.DIR).iterdir()] == ["c.jsonl"]


def test_prune_without_a_folder_does_nothing(tmp_path):
    assert trace.prune(tmp_path / "none") == {"sessions": 0, "bytes": 0}


def test_prune_skips_what_it_cannot_remove(tmp_path, monkeypatch):
    _session(tmp_path, "old", 99)
    def refuse(self, *a, **k):
        raise PermissionError
    monkeypatch.setattr(type(tmp_path), "unlink", refuse)
    assert trace.prune(tmp_path) == {"sessions": 0, "bytes": 0}


def test_session_start_prunes_once_a_day_and_says_so(tmp_path, monkeypatch):
    _session(tmp_path, "old", 99)
    env = {"LOCKS_TRACE": "1"}
    line = hooks._prune_traces(env, tmp_path, "cur")
    assert line and "1 session" in line and "KEEP_DAYS" in line and "KEEP_MB" in line
    _session(tmp_path, "old2", 99)
    assert hooks._prune_traces(env, tmp_path, "cur") is None
    assert (tmp_path / trace.DIR / "old2.jsonl").exists()


def test_session_start_does_not_prune_with_the_trace_off(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCKS_TRACE", raising=False)
    _session(tmp_path, "old", 99)
    assert hooks._prune_traces({"LOCKS_TRACE": "0"}, tmp_path, "cur") is None
    assert (tmp_path / trace.DIR / "old.jsonl").exists()


def test_error_log_over_a_megabyte_keeps_its_last_256k_from_a_line_start(tmp_path):
    log = tmp_path / hooks.ERROR_LOG
    log.write_text("".join(f"line {i:07d} " + "y" * 100 + "\n" for i in range(12000)))
    assert log.stat().st_size > hooks.ERROR_LOG_MAX
    try:
        raise ValueError("boom")
    except ValueError:
        hooks.note_error(tmp_path, "Stop")
    text = log.read_text()
    assert len(text) < hooks.ERROR_LOG_KEEP + 1000
    assert text.startswith("line ") and "ValueError: boom" in text
    assert "line 0011999" in text and "line 0000000" not in text
