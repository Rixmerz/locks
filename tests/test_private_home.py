from __future__ import annotations

import os
import stat

from locks import scribe, signature
from locks.store import Store


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_the_tool_event_log_creates_the_home_private(tmp_path):
    home = tmp_path / "fresh" / "locks-home"
    old = os.umask(0o022)
    try:
        signature.append({"tool_name": "Read", "tool_input": {}}, {"LOCKS_HOME": str(home)})
    finally:
        os.umask(old)
    assert mode(home) == 0o700
    assert mode(home / "tool-events") == 0o700
    assert all(mode(f) == 0o600 for f in (home / "tool-events").iterdir())


def test_a_hook_error_note_creates_the_home_private(tmp_path):
    home = tmp_path / "locks-home"
    old = os.umask(0o022)
    try:
        signature.note_error({"LOCKS_HOME": str(home)})
    finally:
        os.umask(old)
    assert mode(home) == 0o700


def test_the_store_narrows_a_home_someone_else_created_open(tmp_path):
    home = tmp_path / "locks-home"
    home.mkdir()
    home.chmod(0o755)
    store = Store(home / "locks.db")
    try:
        store.add_event(1, "note", text="x")  # the -wal and -shm exist once something is written
        store._make_private()
        assert mode(home) == 0o700
        for suffix in ("", "-wal", "-shm"):
            target = home / f"locks.db{suffix}"
            assert target.exists(), suffix
            assert mode(target) == 0o600, suffix
    finally:
        store.close()


def test_the_store_narrows_a_database_that_was_open_to_others(tmp_path):
    db = tmp_path / "locks.db"
    Store(db).close()
    db.chmod(0o644)
    Store(db).close()
    assert mode(db) == 0o600


def test_a_scribe_state_file_and_a_relay_mark_are_private(tmp_path):
    old = os.umask(0o022)
    try:
        saved = scribe.save(tmp_path, {"session": "s1", "version": scribe.VERSION})
        marked = scribe.mark_relay(tmp_path, "s1", tmp_path)
    finally:
        os.umask(old)
    assert saved is not None and mode(saved) == 0o600
    assert mode(marked) == 0o600
    assert mode(saved.parent) == 0o700


def test_a_scribe_state_file_that_existed_open_is_closed_again(tmp_path):
    saved = scribe.save(tmp_path, {"session": "s1", "version": scribe.VERSION})
    saved.chmod(0o644)
    scribe.save(tmp_path, {"session": "s1", "version": scribe.VERSION})
    assert mode(saved) == 0o600
