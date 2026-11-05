"""A supervising session hears a copy of the endings of work it asked another session to send."""

from locks import hooks
from locks.store import NOTIFY_ENV


def _ended(store, *, status="done"):
    task = store.create_task("fix the login form", status="running", source="locks", session_id="owner", repo="r")
    store.update_task(task["id"], status=status)
    store.add_event(task["id"], "finished", status=status)
    return task


def test_a_notified_session_gets_a_copy_and_the_owner_still_gets_its_ending(store, monkeypatch):
    monkeypatch.setenv(NOTIFY_ENV, "boss")
    task = _ended(store)
    monkeypatch.delenv(NOTIFY_ENV)
    text, ids = hooks.copies(store, "boss", claim="stop")
    assert ids == [task["id"]]
    assert "sent by session owner" in text and hooks.COPIES_HEADER in text
    # Delivered once to the supervisor...
    assert hooks.copies(store, "boss", claim="stop") == (None, [])
    # ...and never taken from the session that sent it.
    assert hooks.endings(store, "owner", claim="stop")[1] == [task["id"]]


def test_work_without_notify_reaches_no_one_else(store, monkeypatch):
    monkeypatch.delenv(NOTIFY_ENV, raising=False)
    _ended(store)
    assert hooks.copies(store, "boss") == (None, [])


def test_several_supervisors_each_hear_it_once(store, monkeypatch):
    monkeypatch.setenv(NOTIFY_ENV, "boss, lead")
    task = _ended(store, status="failed")
    monkeypatch.delenv(NOTIFY_ENV)
    for who in ("boss", "lead"):
        assert hooks.copies(store, who, claim="prompt")[1] == [task["id"]]
        assert hooks.copies(store, who, claim="prompt")[1] == []
