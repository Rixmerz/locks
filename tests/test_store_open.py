"""Opening a store runs the schema and its migrations only when the file is not stamped."""

import hashlib
import sqlite3

from locks import store
from locks.store import Store


def _computed_hash() -> str:
    text = store.SCHEMA + store._APPEND_ONLY_TRIGGERS + repr(store._ADDED_COLUMNS)
    return hashlib.sha256(text.encode()).hexdigest()


def _ddl(path, monkeypatch) -> list[str]:
    seen: list[str] = []
    real = sqlite3.connect

    def connect(*a, **k):
        conn = real(*a, **k)
        conn.set_trace_callback(lambda sql: seen.append(sql))
        return conn

    monkeypatch.setattr(store.sqlite3, "connect", connect)
    Store(path).close()
    return [q for q in seen if q.lstrip().upper().startswith(("CREATE", "ALTER", "DROP"))]


def test_second_open_runs_no_schema_statement(tmp_path, monkeypatch):
    path = tmp_path / "locks.db"
    Store(path).close()
    assert _ddl(path, monkeypatch) == []


def test_first_open_runs_the_schema_and_stamps(tmp_path, monkeypatch):
    path = tmp_path / "locks.db"
    assert _ddl(path, monkeypatch)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    conn.close()


def test_unstamped_existing_store_is_migrated_and_stamped(tmp_path):
    path = tmp_path / "locks.db"
    Store(path).close()
    table, columns = next(iter(store._ADDED_COLUMNS.items()))
    column = next(iter(columns))
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=0")
    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.close()
    s = Store(path)
    cols = {r[1] for r in s._conn.execute(f"PRAGMA table_info({table})")}
    version = s._conn.execute("PRAGMA user_version").fetchone()[0]
    s.close()
    assert column in cols
    assert version == store.SCHEMA_VERSION


def test_schema_hash_matches_schema_version_bump():
    assert _computed_hash() == store.SCHEMA_HASH, (
        "The schema, its triggers or its added columns changed: bump store.SCHEMA_VERSION "
        f"and set store.SCHEMA_HASH to {_computed_hash()!r}.")
