"""A worker that died is noticed by whoever looks next (the mod's board poll), not an hour later."""
import argparse

from locks import cli, dispatch
from locks.store import Store


def test_the_board_poll_sweeps_a_worker_whose_process_is_gone(store: Store, monkeypatch):
    task = store.create_task("going", status="running", source="locks", repo="r", pid=999_999)
    seen = []
    monkeypatch.setattr(dispatch, "recover", lambda st, root, env, **kw: seen.append(task["id"]) or [])
    monkeypatch.setattr(dispatch, "_alive", lambda pid: False)
    cli._notice_deaths(store)
    assert seen == [task["id"]]


def test_the_poll_stays_a_read_while_every_worker_lives(store: Store, monkeypatch):
    store.create_task("going", status="running", source="locks", repo="r", pid=1)
    monkeypatch.setattr(dispatch, "_alive", lambda pid: True)
    monkeypatch.setattr(dispatch, "recover", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no sweep")))
    cli._notice_deaths(store)


def test_a_worker_s_transcript_is_kept_with_the_trace_on(tmp_path, monkeypatch):
    from locks import launch, usage

    source = tmp_path / "w.jsonl"
    source.write_text('{"type":"user"}\n')
    monkeypatch.setattr(usage, "transcript_of", lambda sid, env=None: source)
    env = {"LOCKS_HOME": str(tmp_path / "home"), "LOCKS_TRACE": "on"}
    result = argparse.Namespace(session_id="w1")
    launch._keep_worker_transcript(result, env)
    assert (tmp_path / "home" / "traces" / "w1.transcript.jsonl").read_text() == '{"type":"user"}\n'
    launch._keep_worker_transcript(result, {**env, "LOCKS_TRACE": "off", "LOCKS_HOME": str(tmp_path / "off")})
    assert not (tmp_path / "off" / "traces").exists()
