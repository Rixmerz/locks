"""The memory: tasks, the messages coupled to them, every attempt, and what failed.

One SQLite file per machine, shared by every repository on it. That is the
point of it being one file: a fix that failed in one checkout is a dead end
worth knowing about in the next one, and a task kind that always lands a rung
above its start in every repo is a ladder that starts too low.

Four things are kept:

- **tasks** — a unit of work, whether it arrived as a prompt in a session or
  was dispatched by `locks run`;
- **messages** — what was said about a task, in order: the prompt, every
  follow-up typed while it ran, the result. A follow-up belongs to the task it
  was typed into, not to a task of its own;
- **attempts** — each worker run on a task: the cell, the turns, what it cost,
  how it failed and which way the router moved next. This is the reroute trace,
  and the router reads it back to choose where the next task of the same kind
  starts;
- **problems and fixes** — what was tried against a failure, and whether it
  worked. Searched across repositories, so a dead end is not rediscovered.

And, apart from those, each project's **notes**: what it knows, by topic, with
the links between notes and the code they are about (see `locks.notes`). Unlike
fixes they are never searched from another project.
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from locks import __version__, legacy
from locks.text import fold, title_of, words

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, cwd TEXT, repo TEXT, started_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT, parent_id INTEGER, repo TEXT, cwd TEXT,
  title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
  kind TEXT, complexity TEXT, class_source TEXT, class_reason TEXT,
  status TEXT NOT NULL, source TEXT NOT NULL, prompt_id TEXT,
  result TEXT, start_cell TEXT, final_cell TEXT,
  cost_usd REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_session ON tasks (session_id, status);
CREATE INDEX IF NOT EXISTS tasks_kind ON tasks (kind, status);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks (status, repo);
CREATE INDEX IF NOT EXISTS tasks_repo ON tasks (repo, source);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, role TEXT NOT NULL,
  text TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_task ON messages (task_id);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, seq INTEGER NOT NULL,
  cell TEXT NOT NULL, max_turns INTEGER NOT NULL, passed INTEGER NOT NULL,
  failure TEXT, summary TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
  cost_usd REAL NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0, turns INTEGER NOT NULL DEFAULT 0,
  duration_s REAL NOT NULL DEFAULT 0, changed_paths TEXT NOT NULL DEFAULT '[]',
  capabilities TEXT NOT NULL DEFAULT '[]',
  move TEXT, move_reason TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_task ON attempts (task_id, seq);
CREATE TABLE IF NOT EXISTS problems (
  id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, title TEXT NOT NULL,
  symptom TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fixes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, problem_id INTEGER NOT NULL, repo TEXT,
  description TEXT NOT NULL, outcome TEXT NOT NULL, why TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '', task_id INTEGER, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fixes_problem ON fixes (problem_id);
CREATE TABLE IF NOT EXISTS usage (
  message_id TEXT PRIMARY KEY, session_id TEXT, task_id INTEGER, repo TEXT, model TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0, cache_write INTEGER NOT NULL DEFAULT 0,
  sidechain INTEGER NOT NULL DEFAULT 0, ts TEXT
);
CREATE INDEX IF NOT EXISTS usage_ts ON usage (ts);
CREATE INDEX IF NOT EXISTS usage_task ON usage (task_id);
CREATE INDEX IF NOT EXISTS usage_session ON usage (session_id);
CREATE TABLE IF NOT EXISTS transcript_offsets (path TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tool_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, task_id INTEGER, attempt INTEGER, ts TEXT NOT NULL,
  tool TEXT, sig TEXT NOT NULL, arg_hash TEXT NOT NULL, ok INTEGER NOT NULL, cwd TEXT
);
CREATE INDEX IF NOT EXISTS tool_events_ts ON tool_events (ts);
CREATE INDEX IF NOT EXISTS tool_events_task ON tool_events (task_id, attempt);
CREATE TABLE IF NOT EXISTS habits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, candidate TEXT, steps TEXT NOT NULL, command TEXT NOT NULL,
  trigger_suffix TEXT NOT NULL, settings_path TEXT NOT NULL, installed_at TEXT NOT NULL,
  runs INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, disabled_at TEXT,
  removed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS workflow_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, scope TEXT NOT NULL, source TEXT NOT NULL,
  definition TEXT NOT NULL, inputs TEXT NOT NULL DEFAULT '{}', options TEXT NOT NULL DEFAULT '{}',
  repo TEXT, cwd TEXT, session_id TEXT, status TEXT NOT NULL DEFAULT 'active', reason TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lanes (
  repo TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0, reason TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, ts TEXT NOT NULL,
  kind TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_task ON events (task_id, id);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind, id);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, topic TEXT NOT NULL,
  title TEXT NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'current', state_reason TEXT,
  author TEXT NOT NULL, filed_by TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
  task_id INTEGER, session_id TEXT, proposed_topic TEXT, replaced_by INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_project ON notes (project, topic, state);
CREATE TABLE IF NOT EXISTS note_links (
  from_id INTEGER NOT NULL, to_id INTEGER NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (from_id, to_id, kind)
);
CREATE INDEX IF NOT EXISTS note_links_to ON note_links (to_id);
CREATE TABLE IF NOT EXISTS note_anchors (
  id INTEGER PRIMARY KEY AUTOINCREMENT, note_id INTEGER NOT NULL, path TEXT NOT NULL, symbol TEXT,
  commit_sha TEXT, blob TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS note_anchors_note ON note_anchors (note_id);
CREATE INDEX IF NOT EXISTS note_anchors_path ON note_anchors (path);
CREATE TABLE IF NOT EXISTS note_topics (
  project TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (project, name)
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  kind UNINDEXED, ref_id UNINDEXED, repo UNINDEXED, text, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS standards (
  repo TEXT NOT NULL, kind TEXT NOT NULL, band INTEGER NOT NULL, start_cell TEXT, start_turns INTEGER,
  measured TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL, PRIMARY KEY (repo, kind, band)
);
CREATE TABLE IF NOT EXISTS calibrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, repo TEXT NOT NULL, kind TEXT NOT NULL,
  band INTEGER NOT NULL, field TEXT NOT NULL, old_value TEXT, new_value TEXT, rule TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '{}', version TEXT NOT NULL, reverts INTEGER
);
CREATE INDEX IF NOT EXISTS calibrations_band ON calibrations (repo, kind, band, field);
CREATE TABLE IF NOT EXISTS calibration_verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, change_id INTEGER NOT NULL UNIQUE, verdict TEXT NOT NULL,
  window_before TEXT NOT NULL DEFAULT '{}', window_after TEXT NOT NULL DEFAULT '{}', reason TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evolution (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, day TEXT NOT NULL, since TEXT NOT NULL,
  until TEXT NOT NULL, repo TEXT NOT NULL, kind TEXT NOT NULL, tasks INTEGER NOT NULL, done INTEGER NOT NULL,
  points_done INTEGER NOT NULL, points_estimated INTEGER NOT NULL, points_realized INTEGER NOT NULL,
  hours REAL NOT NULL, cost_usd REAL NOT NULL, velocity REAL, cost_per_point REAL, time_per_point_s REAL,
  pass_first REAL, climbed REAL, version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evolution_day ON evolution (day, repo, kind);
CREATE TABLE IF NOT EXISTS role_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, repo TEXT NOT NULL, role TEXT NOT NULL,
  record TEXT NOT NULL, revision INTEGER, hash TEXT NOT NULL, role_version TEXT,
  lessons TEXT NOT NULL DEFAULT '[]', evidence TEXT NOT NULL DEFAULT '{}', reason TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS role_revisions_role ON role_revisions (repo, role, id);
"""

#: The records of how locks evolves are append-only: a change, its verdict and a day's
#: snapshot are written once, and what came after is a new record, never an edit. So is
#: each record of a role's revision (`locks.revisions`).
_APPEND_ONLY = ("calibrations", "calibration_verdicts", "evolution", "role_revisions")
_APPEND_ONLY_TRIGGERS = "".join(
    f"CREATE TRIGGER IF NOT EXISTS {table}_{verb}_refused BEFORE {verb.upper()} ON {table} "
    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;\n"
    for table in _APPEND_ONLY for verb in ("update", "delete"))

#: Events that keep the version that wrote them: what `locks doctor` reads a bug from.
VERSIONED = frozenset({"briefed", "planned", "attempt_started", "attempt_finished", "moved", "finished",
                       "check_preexisting"})

#: Words a query and a problem must share for a strict match.
#: Sessions (space or comma separated) that hear a copy of the endings of work created with it set.
NOTIFY_ENV = "LOCKS_NOTIFY"

STRICT_OVERLAP = 3
#: A word held by this share of the problems or more is no evidence of a match: every
#: run of a workflow shares its step's template, every task its repository's name.
COMMON_SHARE = 0.2
#: The tools a worker changes a file with, as the tool-call log names them.
EDIT_TOOLS = ("edit", "write", "multiedit", "notebookedit")
#: The query's first distinct words weighed for a strict match.
STRICT_CANDIDATES = 40
#: A strict match needs this many distinctive words in its query to go on.
STRICT_MIN_TERMS = 2
#: Below this many problems, no word is common yet.
COMMON_MIN = 10

TASK_STATES = ("queued", "running", "done", "failed", "interrupted", "blocked", "replan", "needs_approval",
               "cancelled", "dismissed")
#: The work a relay hands to the session that continues the one that sent it:
#: still going, or waiting on the person.
OPEN_FOR_A_RELAY = ("queued", "running", "blocked", "needs_approval", "replan")

#: Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` never
#: alters a table that exists, so each one is added here when it is missing.
#: Append-only: a column is never renamed or removed once a database has it.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    # A habit's runs whose check found something: the linter's findings after an edit, shown to
    # the session; never a failure of the hook (`habits.run_habit`).
    "habits": {"caught": "INTEGER NOT NULL DEFAULT 0"},
    "tasks": {
        "pid": "INTEGER",  # the `locks run` process driving it, while it runs
        "current_cell": "TEXT",  # the cell of the attempt in flight
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        # A cell a person chose. The task ran there because they said so, which
        # says nothing about where the router should start the next one.
        "pinned": "INTEGER NOT NULL DEFAULT 0",
        # What a queued task is run with: verify command, kind, start, turns.
        "options": "TEXT NOT NULL DEFAULT '{}'",
        # Set when the dispatcher, not a person, started the run: only those
        # pause their lane when they fail.
        "dispatched": "INTEGER NOT NULL DEFAULT 0",
        # Whether a queued task may start beside the work already going in its
        # repository (1) or waits its turn (0), and why. NULL: not decided yet.
        "parallel": "INTEGER",
        "parallel_reason": "TEXT",
        # The workflow run a task is a step of, and which step: the engine, not
        # a session, starts the next step when this one passes.
        "workflow_run": "INTEGER",
        "workflow_step": "TEXT",
        # Its size on the Fibonacci scale when it was planned, and the reasons (`locks.points`).
        "points": "INTEGER",
        "points_why": "TEXT",
    },
    "attempts": {
        # The model the CLI says served the attempt, not the alias it was asked for.
        "served_model": "TEXT",
        # The worker's Claude Code session, kept in its worktree: more turns continue it.
        "session_id": "TEXT",
    },
    "notes": {
        # `constraint`: a rule a change must meet, briefed as one (`notes.CONSTRAINT`). NULL: a fact.
        "kind": "TEXT",
    },
    "problems": {
        "first_seen": "TEXT",
        "last_seen": "TEXT",
    },
    "fixes": {
        # The window a fix was trusted in. A fix that worked and later stopped
        # holding is the most valuable dead end there is: it looked solved.
        "believed_from": "TEXT",
        "invalidated_on": "TEXT",
        # Evidence a person can check: the commit the verdict is about.
        "commit_sha": "TEXT",
        # The cell it was met in (`locks.cells`): the role the worker wore, and the
        # areas of what it changed (comma-separated). NULL: recorded before scopes.
        "role": "TEXT",
        "areas": "TEXT",
    },
}
FIX_OUTCOMES = ("worked", "failed", "partial", "pending")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def home(env: dict[str, str] | None = None) -> Path:
    env = dict(os.environ) if env is None else env
    if given := legacy.getenv(env, "LOCKS_HOME"):
        return Path(given).expanduser()
    xdg = env.get("XDG_DATA_HOME", "")
    # The home of the environment given, not of this process: a caller that
    # passes an environment means all of it.
    base = Path(xdg) if xdg.startswith("/") else Path(env.get("HOME") or Path.home()) / ".local" / "share"
    return legacy.home(base)  # moved from the old name's the first time


SETUP_RETRIES = 100

# Bump SCHEMA_VERSION by hand, and SCHEMA_HASH with it, whenever SCHEMA, the append-only
# triggers, _ADDED_COLUMNS or a migration changes: an open whose file carries this
# `user_version` runs none of them. tests/test_store_open.py fails on a stale hash.
SCHEMA_VERSION = 1
SCHEMA_HASH = "ad51750d0b432871ed85cd42aa5dbb23bea65aea701a5edc3d1f86ef87c41c09"


@contextlib.contextmanager
def _setup_lock(db: Path) -> Iterator[None]:
    """An exclusive lock on `<db>.lock` where the platform has one; a no-op elsewhere."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - not POSIX
        yield
        return
    with open(f"{db}.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


_SESSION_COLUMNS = (
    "(SELECT COUNT(*) FROM tasks t WHERE t.session_id = s.id AND t.source = 'hook') AS prompts, "
    "(SELECT t.title FROM tasks t WHERE t.session_id = s.id AND t.source = 'hook' "
    " ORDER BY t.id DESC LIMIT 1) AS last_prompt, "
    "(SELECT COUNT(*) FROM tasks t WHERE t.session_id = s.id AND t.status = 'running') AS running, "
    "(SELECT COALESCE(SUM(t.cost_usd), 0) FROM tasks t WHERE t.session_id = s.id) AS cost_usd"
)


def _in(column: str, values: Iterable[str] | None) -> tuple[str | None, list[str]]:
    """A WHERE clause keeping `column` in `values`: ("", []) for no filter, (None, []) for an
    empty one, which matches nothing."""
    if values is None:
        return "", []
    values = list(values)
    if not values:
        return None, []
    return f"WHERE {column} IN ({', '.join('?' * len(values))})", values


#: The file `Store.pulse` touches when a task ends, beside the database.
PULSE = "pulse"


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self._make_private()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        try:
            self._setup()
        except BaseException:
            self._conn.close()
            raise
        self._make_private()

    def _make_private(self) -> None:
        """The home is the person's alone (0700) and so is every file of the database (0600).

        The home may have been created first by another writer (a hook's fast path, a
        scribe) with the process umask, so it is narrowed on every open; SQLite gives the
        -wal and -shm files the mode of the database file.
        """
        targets = [(self.path.parent, 0o700)] + [
            (self.path.with_name(self.path.name + suffix), 0o600) for suffix in ("", "-wal", "-shm")]
        for target, mode in targets:
            # A missing -wal/-shm, or a folder that is not ours to narrow, is no failure.
            with contextlib.suppress(OSError):
                os.chmod(target, mode)

    def _setup(self) -> None:
        """WAL, the schema and the added columns, one opener at a time.

        Switching a file to WAL and altering a table take locks SQLite may refuse
        at once instead of waiting for (a hook, the dispatcher and the UI often
        open the file in the same instant). A lock file beside the database
        queues the openers; a refusal that still gets through is retried.
        """
        for attempt in range(SETUP_RETRIES):
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                if self._stamped():
                    # Rows are data, not schema: an old install still running may write its name
                    # again at any time, so they are read on every open, stamped or not.
                    legacy.upgrade_store(self._conn)
                    return
                with _setup_lock(self.path):
                    # Another opener may have finished the schema while this one waited.
                    if self._stamped():
                        legacy.upgrade_store(self._conn)
                        return
                    self._conn.executescript(SCHEMA + _APPEND_ONLY_TRIGGERS)
                    self._migrate()
                    self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == SETUP_RETRIES - 1:
                    raise
                time.sleep(0.02 + random.random() * 0.05)  # noqa: S311 - jitter, not security

    def _stamped(self) -> bool:
        return self._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    def _migrate(self) -> None:
        for table, columns in _ADDED_COLUMNS.items():
            have = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for column, decl in columns.items():
                if column in have:
                    continue
                try:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                except sqlite3.OperationalError as exc:
                    # Another process or thread opened the file at the same moment
                    # and added it first: the column is there, which is the point.
                    if "duplicate column" not in str(exc):
                        raise
        legacy.upgrade_store(self._conn)

    @classmethod
    def open(cls, env: dict[str, str] | None = None) -> Store:
        return cls(legacy.database(home(env)))

    def close(self) -> None:
        self._conn.close()

    # --- sessions --------------------------------------------------------

    def touch_session(self, session_id: str, cwd: str | None, repo: str | None) -> None:
        stamp = now()
        self._conn.execute(
            "INSERT INTO sessions (id, cwd, repo, started_at, last_seen_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET cwd = COALESCE(excluded.cwd, cwd), "
            "repo = COALESCE(excluded.repo, repo), last_seen_at = excluded.last_seen_at",
            (session_id, cwd, repo, stamp, stamp),
        )

    def get_session(self, session_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def sessions(self, *, repos: Iterable[str] | None = None, limit: int = 100) -> list[dict]:
        """Claude Code sessions locks has seen, latest first, each with how many prompts it
        took, the last one, and whether one is still running."""
        scope, params = _in("s.repo", repos)
        if scope is None:
            return []
        query = f"SELECT s.*, {_SESSION_COLUMNS} FROM sessions s {scope} ORDER BY s.last_seen_at DESC LIMIT ?"  # noqa: S608
        rows = self._conn.execute(query, [*params, limit]).fetchall()
        return [dict(r) for r in rows]

    def projects(self) -> list[dict]:
        """Every repository locks has worked in, from its tasks and the sessions it saw: the
        directory it was last used from, when, and how its tasks stand."""
        rows = self._conn.execute(
            "WITH seen AS ("
            " SELECT repo, cwd, updated_at AS at FROM tasks WHERE repo IS NOT NULL AND cwd IS NOT NULL "
            "   AND source != 'delegation' "
            " UNION ALL SELECT repo, cwd, last_seen_at FROM sessions WHERE repo IS NOT NULL AND cwd IS NOT NULL) "
            "SELECT repo, MAX(at) AS last_seen, "
            " (SELECT cwd FROM seen s2 WHERE s2.repo = seen.repo ORDER BY at DESC LIMIT 1) AS cwd "
            "FROM seen GROUP BY repo ORDER BY last_seen DESC"
        ).fetchall()
        out = []
        for r in rows:
            counts = dict(self._conn.execute(
                "SELECT status, COUNT(*) FROM tasks WHERE repo = ? AND source IN ('locks', 'queue') GROUP BY status",
                (r["repo"],)).fetchall())
            sessions = self._conn.execute("SELECT COUNT(*) FROM sessions WHERE repo = ?", (r["repo"],)).fetchone()[0]
            out.append({**dict(r), "tasks": counts, "sessions": int(sessions)})
        return out

    # --- tasks -----------------------------------------------------------

    def create_task(self, body: str, *, status: str, source: str, author: str = "user", **fields: Any) -> dict:
        """A task, with its text as its first message. `author` is who wrote that
        text: `person` for someone who typed it, `orchestrator` for the main
        session that sent it. `user` is a session's own prompt — and, on work
        recorded before authors were kept, unknown."""
        if status not in TASK_STATES:
            raise ValueError(f"unknown task status {status!r}")
        stamp = now()
        row = {
            "title": fields.pop("title", None) or title_of(body),
            "body": body,
            "status": status,
            "source": source,
            "created_at": stamp,
            "updated_at": stamp,
            **fields,
        }
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = self._conn.execute(f"INSERT INTO tasks ({cols}) VALUES ({marks})", list(row.values()))  # noqa: S608
        task_id = int(cur.lastrowid)
        self.add_message(task_id, author, body)
        # A supervising session asked for a copy of this work's ending (`--notify`, LOCKS_NOTIFY).
        notify = [s for s in os.environ.get(NOTIFY_ENV, "").replace(",", " ").split() if s]
        if notify and source in ("locks", "queue"):
            self._conn.execute("INSERT INTO events (task_id, ts, kind, data) VALUES (?, ?, 'notify', ?)",
                               (task_id, stamp, json.dumps({"sessions": notify})))
        return self.get_task(task_id)

    def get_task(self, task_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def update_task(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "status" in fields and fields["status"] not in TASK_STATES:
            raise ValueError(f"unknown task status {fields['status']!r}")
        fields["updated_at"] = now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", [*fields.values(), task_id])  # noqa: S608

    def list_tasks(
        self,
        *,
        repo: str | None = None,
        status: Sequence[str] | None = None,
        session_id: str | None = None,
        source: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses, params = [], []
        if source:
            clauses.append(f"source IN ({', '.join('?' for _ in source)})")
            params.extend(source)
        if repo is not None:
            clauses.append("repo = ?")
            params.append(repo)
        if status:
            clauses.append(f"status IN ({', '.join('?' for _ in status)})")
            params.extend(status)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM tasks {where} ORDER BY id DESC LIMIT ?", [*params, limit]  # noqa: S608
        ).fetchall()
        return [dict(r) for r in rows]

    def running_task(self, session_id: str, prompt_id: str | None = None) -> dict | None:
        if prompt_id is not None:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE session_id = ? AND prompt_id = ? AND status = 'running' "
                "ORDER BY id DESC LIMIT 1",
                (session_id, prompt_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE session_id = ? AND status = 'running' AND source = 'hook' "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def delegation(self, session_id: str, tool_use_id: str | None) -> dict | None:
        """The running delegation a tool call opened; by its id when the event
        carries one, else the session's newest."""
        if tool_use_id:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE session_id = ? AND source = 'delegation' AND prompt_id = ? "
                "ORDER BY id DESC LIMIT 1", (session_id, tool_use_id)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE session_id = ? AND source = 'delegation' AND status = 'running' "
                "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        return dict(row) if row else None

    def last_prompt_task(self, session_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE session_id = ? AND source = 'hook' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def interrupt_running(self, session_id: str, exclude: Iterable[int] = ()) -> int:
        skip = list(exclude)
        extra = f" AND id NOT IN ({', '.join('?' for _ in skip)})" if skip else ""
        cur = self._conn.execute(
            "UPDATE tasks SET status = 'interrupted', updated_at = ? "  # noqa: S608
            "WHERE session_id = ? AND status = 'running' AND source = 'hook'" + extra,
            [now(), session_id, *skip],
        )
        return cur.rowcount

    # --- messages --------------------------------------------------------

    def add_message(self, task_id: int, role: str, text: str) -> None:
        self._conn.execute(
            "INSERT INTO messages (task_id, role, text, ts) VALUES (?, ?, ?, ?)",
            (task_id, role, text, now()),
        )

    def messages(self, task_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT role, text, ts FROM messages WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- attempts: the reroute trace -------------------------------------

    def add_attempt(self, task_id: int, **fields: Any) -> int:
        seq = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM attempts WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        for key in ("changed_paths", "capabilities"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(list(fields[key]))
        row = {"task_id": task_id, "seq": seq, "created_at": now(), **fields}
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._conn.execute(f"INSERT INTO attempts ({cols}) VALUES ({marks})", list(row.values()))  # noqa: S608
        self._conn.execute(
            "UPDATE tasks SET cost_usd = cost_usd + ?, updated_at = ? WHERE id = ?",
            (float(fields.get("cost_usd") or 0), now(), task_id),
        )
        return int(seq)

    def set_move(self, task_id: int, seq: int, move: str, reason: str) -> None:
        self._conn.execute(
            "UPDATE attempts SET move = ?, move_reason = ? WHERE task_id = ? AND seq = ?",
            (move, reason, task_id, seq),
        )

    def next_attempt(self, task_id: int) -> int:
        """The number the task's next attempt takes: one past every attempt it kept and every one it
        started, so an attempt whose worker died (started, never kept) is never numbered again and
        the calls the tool log keeps under its number are never read as the next one's."""
        kept = self._conn.execute("SELECT MAX(seq) FROM attempts WHERE task_id = ?", (task_id,)).fetchone()[0]
        started = self._conn.execute(
            "SELECT MAX(CAST(json_extract(data, '$.seq') AS INTEGER)) FROM events "
            "WHERE task_id = ? AND kind = 'attempt_started'", (task_id,)).fetchone()[0]
        return max(int(kept or 0), int(started or 0)) + 1

    def attempts(self, task_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def landings(self, kind: str, *, repo: str | None = None, limit: int = 20, climbs: bool = True) -> list[str]:
        """Where recent finished tasks of this kind had to climb to, newest first: the cell
        a task passed at when it climbed there from its start, "" when it passed where it
        started. A pass at the start is no evidence that a lower cell fails: counted as the
        cell it landed on, a start history had raised kept every later task as high,
        whatever the cells below would have done. `climbs=False` gives the cell each
        passed at, wherever it started: a reading kind's pass is its answer, never proof a
        lower cell would have answered as well.

        Only tasks the core ran count: a prompt answered in a session has no
        cell, and guessing one would teach the router a number nobody measured.
        A task with no start recorded says neither, and is left out.
        """
        scope = " AND t.repo = ?" if repo is not None else ""
        params: list[Any] = [kind, *([repo] if repo is not None else []), limit]
        rows = self._conn.execute(
            "SELECT t.start_cell, t.final_cell FROM tasks t WHERE t.kind = ? AND t.status = 'done' "  # noqa: S608
            "AND t.final_cell IS NOT NULL AND t.start_cell IS NOT NULL AND t.source = 'locks' AND t.pinned = 0"
            + scope + " ORDER BY t.id DESC LIMIT ?",
            params,
        ).fetchall()
        return ["" if climbs and start == final else final for start, final in rows]

    def turn_history(self, kind: str, *, repo: str, limit: int = 10) -> list[list[tuple[int, bool, str | None]]]:
        """The attempts of the last tasks of this kind here that passed, newest task first:
        `(max_turns, passed, failure)` each, in the order they ran."""
        rows = self._conn.execute(
            "SELECT a.task_id, a.max_turns, a.passed, a.failure FROM attempts a WHERE a.task_id IN ("
            "SELECT t.id FROM tasks t WHERE t.kind = ? AND t.repo = ? AND t.status = 'done' "
            "AND EXISTS (SELECT 1 FROM attempts x WHERE x.task_id = t.id) ORDER BY t.id DESC LIMIT ?) "
            "ORDER BY a.task_id DESC, a.seq", (kind, repo, limit)).fetchall()
        runs: dict[int, list[tuple[int, bool, str | None]]] = {}
        for task_id, turns, passed, failure in rows:
            runs.setdefault(task_id, []).append((int(turns), bool(passed), failure))
        return list(runs.values())

    def attempts_of(self, task_ids: Sequence[int]) -> dict[int, list[dict]]:
        """The attempts of each of these tasks, in the order they ran."""
        out: dict[int, list[dict]] = {}
        ids = list(task_ids)
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for row in self._conn.execute(
                    f"SELECT * FROM attempts WHERE task_id IN ({', '.join('?' * len(chunk))}) "  # noqa: S608
                    "ORDER BY task_id, seq", chunk):
                out.setdefault(int(row["task_id"]), []).append(dict(row))
        return out

    def ended_tasks(self, *, repo: str | None = None, since: str = "", until: str | None = None,
                    limit: int = 2000) -> list[dict]:
        """locks' tasks that ended, oldest ending first, each with when it last ended
        (`ended_at`), that ending's data, its first plan's data (where it started and on
        which standard), and the wall time of every run it had: what the standards and the
        evolution read (`locks.standards`, `locks.evolution`). The newest `limit` endings."""
        clauses, params = ["f.ts >= ?"], [since]
        if until is not None:
            clauses.append("f.ts < ?")
            params.append(until)
        if repo is not None:
            clauses.append("t.repo = ?")
            params.append(repo)
        rows = self._conn.execute(
            "SELECT t.*, f.id AS ended_id, f.ts AS ended_at, f.data AS ended_data, "  # noqa: S608
            "(SELECT p.data FROM events p WHERE p.task_id = t.id AND p.kind = 'planned' ORDER BY p.id LIMIT 1) "
            "AS planned_data, (SELECT COALESCE(SUM(json_extract(w.data, '$.wall_s')), 0) FROM events w "
            "WHERE w.task_id = t.id AND w.kind = 'finished') AS wall_s FROM tasks t "
            "JOIN events f ON f.id = (SELECT MAX(id) FROM events WHERE task_id = t.id AND kind = 'finished') "
            "WHERE t.source IN ('locks', 'queue') AND t.status NOT IN ('queued', 'running') AND "
            + " AND ".join(clauses) + " ORDER BY f.id DESC LIMIT ?", [*params, limit]).fetchall()
        return [{**dict(r), "ended_data": json.loads(r["ended_data"]),
                 "planned_data": json.loads(r["planned_data"] or "{}")} for r in reversed(rows)]

    def endings_after(self, event_id: int) -> list[dict]:
        """The `finished` events after `event_id`, oldest first, each with its task's
        repository and options."""
        rows = self._conn.execute(
            "SELECT e.id, e.task_id, e.ts, t.repo, t.options FROM events e JOIN tasks t ON t.id = e.task_id "
            "WHERE e.kind = 'finished' AND e.id > ? ORDER BY e.id", (event_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- standards, and the record of how they moved -----------------------------

    def meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                           "value = excluded.value", (key, value))

    def metas(self, prefix: str) -> dict[str, str]:
        """Every meta value whose key starts with `prefix`."""
        rows = self._conn.execute("SELECT key, value FROM meta WHERE substr(key, 1, ?) = ?",
                                  (len(prefix), prefix)).fetchall()
        return {r[0]: r[1] for r in rows}

    def drop_meta(self, keys: Sequence[str]) -> None:
        self._conn.executemany("DELETE FROM meta WHERE key = ?", [(k,) for k in keys])

    def standard(self, repo: str, kind: str, band: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM standards WHERE repo = ? AND kind = ? AND band = ?",
                                 (repo, kind, band)).fetchone()
        return dict(row) if row else None

    def standards(self, *, repo: str | None = None) -> list[dict]:
        scope, params = ("WHERE repo = ?", [repo]) if repo is not None else ("", [])
        return [dict(r) for r in self._conn.execute(
            f"SELECT * FROM standards {scope} ORDER BY repo, kind, band", params)]  # noqa: S608

    def set_standard(self, repo: str, kind: str, band: int, **fields: Any) -> None:
        """Where tasks of this kind and size start here now; `measured` is kept as JSON."""
        if "measured" in fields and not isinstance(fields["measured"], str):
            fields["measured"] = json.dumps(fields["measured"])
        row = {"repo": repo, "kind": kind, "band": band, "updated_at": now(), **fields}
        sets = ", ".join(f"{k} = excluded.{k}" for k in row if k not in ("repo", "kind", "band"))
        self._conn.execute(f"INSERT INTO standards ({', '.join(row)}) VALUES ({', '.join('?' * len(row))}) "  # noqa: S608
                           f"ON CONFLICT(repo, kind, band) DO UPDATE SET {sets}", list(row.values()))

    def add_calibration(self, **record: Any) -> int:
        """One change of a standard, appended: never edited, never removed."""
        if not isinstance(record.get("evidence", ""), str):
            record["evidence"] = json.dumps(record["evidence"], ensure_ascii=False)
        cur = self._conn.execute(f"INSERT INTO calibrations ({', '.join(record)}) "  # noqa: S608
                                 f"VALUES ({', '.join('?' * len(record))})", list(record.values()))
        return int(cur.lastrowid)

    def calibrations(self, *, repo: str | None = None, since: str = "") -> list[dict]:
        """The changes of the standards since `since`, oldest first."""
        scope, params = ("AND repo = ?", [repo]) if repo is not None else ("", [])
        return [dict(r) for r in self._conn.execute(
            f"SELECT * FROM calibrations WHERE at >= ? {scope} ORDER BY id", [since, *params])]  # noqa: S608

    def add_verdict(self, **record: Any) -> int:
        """What a change did, judged once and appended."""
        for key in ("window_before", "window_after"):
            if not isinstance(record.get(key, ""), str):
                record[key] = json.dumps(record[key])
        cur = self._conn.execute(f"INSERT INTO calibration_verdicts ({', '.join(record)}) "  # noqa: S608
                                 f"VALUES ({', '.join('?' * len(record))})", list(record.values()))
        return int(cur.lastrowid)

    def verdicts(self) -> dict[int, dict]:
        """Each judged change's verdict, by the change's id."""
        return {int(r["change_id"]): dict(r) for r in self._conn.execute("SELECT * FROM calibration_verdicts")}

    def add_snapshot(self, **row: Any) -> int:
        """One repository's and kind's row of a day's snapshot, appended."""
        cur = self._conn.execute(f"INSERT INTO evolution ({', '.join(row)}) "  # noqa: S608
                                 f"VALUES ({', '.join('?' * len(row))})", list(row.values()))
        return int(cur.lastrowid)

    def snapshots(self, *, since_day: str = "", repo: str | None = None) -> list[dict]:
        """The snapshots taken on `since_day` or after, oldest first."""
        scope, params = ("AND repo = ?", [repo]) if repo is not None else ("", [])
        return [dict(r) for r in self._conn.execute(
            f"SELECT * FROM evolution WHERE day >= ? {scope} ORDER BY id", [since_day, *params])]  # noqa: S608

    # --- the queue and its lanes ---------------------------------------------

    def enqueue(self, body: str, *, repo: str | None, cwd: str | None, session_id: str | None = None,
                options: dict | None = None, author: str = "user") -> dict:
        return self.create_task(body, status="queued", source="queue", repo=repo, cwd=cwd, author=author,
                                session_id=session_id, options=json.dumps(options or {}))

    # --- workflow runs -------------------------------------------------------------

    def create_run(self, **fields: Any) -> dict:
        stamp = now()
        row = {"created_at": stamp, "updated_at": stamp, **fields}
        cur = self._conn.execute(f"INSERT INTO workflow_runs ({', '.join(row)}) "  # noqa: S608
                                 f"VALUES ({', '.join('?' for _ in row)})", list(row.values()))
        return self.get_run(int(cur.lastrowid))

    def get_run(self, run_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def update_run(self, run_id: int, **fields: Any) -> None:
        fields["updated_at"] = now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(f"UPDATE workflow_runs SET {sets} WHERE id = ?", [*fields.values(), run_id])  # noqa: S608

    def list_runs(self, *, repos: set[str] | None = None, limit: int = 50) -> list[dict]:
        where, params = _in("repo", repos)
        if where is None:
            return []
        rows = self._conn.execute(f"SELECT * FROM workflow_runs {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
                                  [*params, limit]).fetchall()
        return [dict(r) for r in rows]

    def session_runs(self, session_id: str, *, status: str = "active") -> list[dict]:
        """The workflow runs a session started, oldest first."""
        rows = self._conn.execute("SELECT * FROM workflow_runs WHERE session_id = ? AND status = ? ORDER BY id",
                                  (session_id, status)).fetchall()
        return [dict(r) for r in rows]

    def runs_from(self, source: str) -> list[dict]:
        """The runs still active that were started from this definition file."""
        rows = self._conn.execute("SELECT * FROM workflow_runs WHERE source = ? AND status = 'active' ORDER BY id",
                                  (source,)).fetchall()
        return [dict(r) for r in rows]

    def run_tasks(self, run_id: int) -> list[dict]:
        """A run's step tasks, oldest first: a step retried has more than one, the newest counts."""
        rows = self._conn.execute("SELECT * FROM tasks WHERE workflow_run = ? ORDER BY id", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def queued_in(self, repo: str | None) -> list[dict]:
        """A lane's queued tasks, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status = 'queued' AND repo IS ? ORDER BY id", (repo,)).fetchall()
        return [dict(r) for r in rows]

    def queued_everywhere(self) -> list[dict]:
        """Every queued task, of every lane, oldest first."""
        return [dict(r) for r in self._conn.execute("SELECT * FROM tasks WHERE status = 'queued' ORDER BY id")]

    def dispatched_running(self, repo: str | None) -> list[dict]:
        """What the dispatcher has running in a lane now."""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status = 'running' AND dispatched = 1 AND repo IS ? ORDER BY id",
            (repo,)).fetchall()
        return [dict(r) for r in rows]

    def dispatched_counts(self) -> dict[str | None, int]:
        """How many tasks the dispatcher runs now in each lane, every repository's."""
        rows = self._conn.execute("SELECT repo, count(*) AS n FROM tasks WHERE status = 'running' AND dispatched = 1 "
                                  "GROUP BY repo").fetchall()
        return {r["repo"]: int(r["n"]) for r in rows}

    def capacity_events(self, after: int, *, limit: int = 2000) -> list[dict]:
        """The attempts and endings since event `after`, oldest first, each with whether
        the dispatcher started its task: what the machine's level reads (`locks.capacity`)."""
        rows = self._conn.execute(
            "SELECT e.*, COALESCE(t.dispatched, 0) AS dispatched FROM events e LEFT JOIN tasks t ON t.id = e.task_id "
            "WHERE e.id > ? AND e.kind IN ('attempt_finished', 'finished') ORDER BY e.id LIMIT ?",
            (after, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def set_parallel(self, task_id: int, parallel: bool, reason: str) -> None:
        self.update_task(task_id, parallel=int(parallel), parallel_reason=reason)

    def claim(self, task_id: int) -> bool:
        """Move a queued task to running, once: two dispatchers never run one task."""
        cur = self._conn.execute(
            "UPDATE tasks SET status = 'running', dispatched = 1, updated_at = ? "
            "WHERE id = ? AND status = 'queued'", (now(), task_id))
        return cur.rowcount == 1

    def pause_lane(self, repo: str | None, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO lanes (repo, paused, reason, updated_at) VALUES (?, 1, ?, ?) "
            "ON CONFLICT(repo) DO UPDATE SET paused = 1, reason = excluded.reason, updated_at = excluded.updated_at",
            (repo or "", reason, now()))
        self.pulse()  # a session whose work waits in the lane hears of it (`hooks.endings`)

    def unpause_lane(self, repo: str | None) -> None:
        self._conn.execute(
            "INSERT INTO lanes (repo, paused, reason, updated_at) VALUES (?, 0, NULL, ?) "
            "ON CONFLICT(repo) DO UPDATE SET paused = 0, reason = NULL, updated_at = excluded.updated_at",
            (repo or "", now()))

    def release_lane(self, repo: str | None, task_id: int) -> bool:
        """Unpause a lane paused on this task: what it stopped on no longer waits
        on anyone (dismissed, superseded, resumed). A pause on another task stays."""
        cur = self._conn.execute(
            "UPDATE lanes SET paused = 0, reason = NULL, updated_at = ? WHERE repo IS ? AND paused = 1 "
            "AND (reason LIKE ? OR reason LIKE ?)", (now(), repo, f"task #{task_id} %", f"task #{task_id}:%"))
        return cur.rowcount > 0

    def lane_paused_on(self, repo: str | None, task_id: int) -> int:
        """How many tasks wait behind a lane paused on this task; 0 when it is not."""
        row = self._conn.execute(
            "SELECT (SELECT count(*) FROM tasks q WHERE q.repo IS l.repo AND q.status = 'queued') AS queued "
            "FROM lanes l WHERE l.repo IS ? AND l.paused = 1 AND (l.reason LIKE ? OR l.reason LIKE ?)",
            (repo, f"task #{task_id} %", f"task #{task_id}:%")).fetchone()
        return int(row["queued"]) if row else 0

    def paused_with_work(self, session_id: str) -> list[dict]:
        """The paused lanes that hold queued work this session sent: work that waits
        on nobody's word unless the session hears of it."""
        rows = self._conn.execute(
            "SELECT l.repo, l.reason, l.updated_at, count(q.id) AS queued FROM lanes l "
            "JOIN tasks q ON q.repo IS l.repo AND q.status = 'queued' AND q.session_id = ? "
            "WHERE l.paused = 1 GROUP BY l.repo ORDER BY l.repo", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def lanes(self) -> list[dict]:
        """Every repository with queued work or a lane record, and its state."""
        rows = self._conn.execute(
            "SELECT r.repo, COALESCE(l.paused, 0) AS paused, l.reason, "
            "(SELECT count(*) FROM tasks q WHERE q.repo IS r.repo AND q.status = 'queued') AS queued "
            "FROM (SELECT DISTINCT repo FROM tasks WHERE status = 'queued' UNION SELECT repo FROM lanes) r "
            "LEFT JOIN lanes l ON l.repo = r.repo ORDER BY r.repo"
        ).fetchall()
        return [dict(r) for r in rows]

    def children(self, task_id: int, status: Sequence[str] | None = None) -> list[dict]:
        clause = f" AND status IN ({', '.join('?' for _ in status)})" if status else ""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE parent_id = ?" + clause + " ORDER BY id",  # noqa: S608
            [task_id, *(status or [])]).fetchall()
        return [dict(r) for r in rows]

    # --- spend -----------------------------------------------------------------

    def add_usage(self, **record: Any) -> None:
        cols = ", ".join(record)
        marks = ", ".join("?" for _ in record)
        self._conn.execute(f"INSERT OR REPLACE INTO usage ({cols}) VALUES ({marks})",  # noqa: S608
                           list(record.values()))

    def transcript_offset(self, path: str) -> int:
        row = self._conn.execute("SELECT offset FROM transcript_offsets WHERE path = ?", (path,)).fetchone()
        return int(row[0]) if row else 0

    def set_transcript_offset(self, path: str, offset: int) -> None:
        self._conn.execute(
            "INSERT INTO transcript_offsets (path, offset) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET offset = excluded.offset", (path, offset))

    def usage_by_model(self, *, days: int = 7, repo: str | None = None) -> list[dict]:
        scope, params = ("AND repo = ?", [repo]) if repo is not None else ("", [])
        rows = self._conn.execute(
            "SELECT model, count(*) AS messages, sum(input_tokens) AS input_tokens, "  # noqa: S608
            "sum(output_tokens) AS output_tokens, sum(cache_read) AS cache_read, sum(cache_write) AS cache_write "
            f"FROM usage WHERE ts >= datetime('now', ?) {scope} GROUP BY model ORDER BY output_tokens DESC",
            [f"-{int(days)} days", *params]).fetchall()
        return [dict(r) for r in rows]

    def usage_by(self, column: str, values: Iterable[Any]) -> list[dict]:
        """Tokens per model for each task id or session id in `values`, as `key`."""
        if column not in ("task_id", "session_id"):
            raise ValueError(f"usage is kept by task_id or session_id, not {column}")
        keys = list(values)
        if not keys:
            return []
        marks = ", ".join("?" for _ in keys)
        rows = self._conn.execute(
            f"SELECT {column} AS key, model, sum(input_tokens) AS input_tokens, "  # noqa: S608
            "sum(output_tokens) AS output_tokens, sum(cache_read) AS cache_read, sum(cache_write) AS cache_write "
            f"FROM usage WHERE {column} IN ({marks}) GROUP BY {column}, model", keys).fetchall()
        return [dict(r) for r in rows]

    def session_models(self, *, days: int = 30) -> list[str]:
        """Every model id a person's own sessions used in `days`."""
        rows = self._conn.execute("SELECT DISTINCT model FROM usage WHERE model IS NOT NULL AND ts >= ?",
                                  (_days_ago(days),))
        return [r[0] for r in rows]

    def worker_models(self) -> dict[str, str]:
        """Per model family, the model that served the latest worker attempt of it."""
        from locks.models import family

        out: dict[str, str] = {}
        for (served,) in self._conn.execute(
                "SELECT served_model FROM attempts WHERE served_model IS NOT NULL AND served_model != '' "
                "ORDER BY id DESC LIMIT 500"):
            alias = family(served)
            if alias and alias not in out:
                out[alias] = served
        return out

    def worker_spend(self, *, days: int = 7, repo: str | None = None) -> list[dict]:
        """What workers cost, by cell: attempts, passes, dollars, tokens."""
        scope, params = ("AND t.repo = ?", [repo]) if repo is not None else ("", [])
        rows = self._conn.execute(
            "SELECT a.cell, count(*) AS attempts, sum(a.passed) AS passed, round(sum(a.cost_usd), 4) AS usd, "  # noqa: S608
            "sum(a.input_tokens) AS input_tokens, sum(a.output_tokens) AS output_tokens "
            "FROM attempts a JOIN tasks t ON t.id = a.task_id "
            f"WHERE a.created_at >= ? {scope} GROUP BY a.cell ORDER BY usd DESC",
            [_days_ago(days), *params]).fetchall()
        return [dict(r) for r in rows]

    def spend_by_day(self, *, days: int = 14) -> list[dict]:
        """Per day: session usage per model family and worker dollars."""
        sessions = self._conn.execute(
            "SELECT substr(ts, 1, 10) AS day, model, sum(input_tokens) AS input_tokens, "
            "sum(output_tokens) AS output_tokens, sum(cache_read) AS cache_read, sum(cache_write) AS cache_write "
            "FROM usage WHERE ts >= ? GROUP BY day, model ORDER BY day", (_days_ago(days),)).fetchall()
        workers = self._conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, round(sum(cost_usd), 4) AS usd, count(*) AS attempts "
            "FROM attempts WHERE created_at >= ? GROUP BY day ORDER BY day", (_days_ago(days),)).fetchall()
        return [{"kind": "session", **dict(r)} for r in sessions] + [{"kind": "worker", **dict(r)} for r in workers]

    def routing_stats(self, since: str = "") -> list[dict]:
        """Per kind of task: how many ran, where they started and passed, how often
        they had to climb, and what they cost. The evidence for changing a ladder.
        `since` (ISO time) keeps only tasks created from then on."""
        rows = self._conn.execute(
            "SELECT t.kind, t.start_cell, t.final_cell, t.status, t.cost_usd, t.pinned, "
            "(SELECT count(*) FROM attempts a WHERE a.task_id = t.id) AS attempts "
            "FROM tasks t WHERE t.source = 'locks' AND t.kind IS NOT NULL AND t.created_at >= ?", (since,)).fetchall()
        return [dict(r) for r in rows]

    def moves(self, since: str = "") -> list[dict]:
        """Every move a run made after a failed attempt, with the task's kind, the
        cell it left, the failure that moved it, and the cell the next attempt ran
        at and whether that one passed: how each ladder is really climbed. The
        task's status says why a move that goes on has no next attempt yet."""
        rows = self._conn.execute(
            "SELECT t.kind, t.status AS task_status, a.cell, a.failure, a.move, b.cell AS next_cell, "
            "b.passed AS next_passed FROM attempts a JOIN tasks t ON t.id = a.task_id "
            "LEFT JOIN attempts b ON b.task_id = a.task_id AND b.seq = a.seq + 1 "
            "WHERE a.move IS NOT NULL AND t.source = 'locks' AND t.kind IS NOT NULL AND a.created_at >= ?",
            (since,)).fetchall()
        return [dict(r) for r in rows]

    #: The events that tell how a run went and where its time went.
    RUN_EVENTS = ("planned", "briefed", "attempt_started", "attempt_finished", "verified", "moved", "finished")

    def runs_since(self, since: str, *, repo: str | None = None) -> tuple[list[dict], list[dict]]:
        """The tasks locks ran (not a session's turns, not an investigation's
        reading) that moved since `since`, and the events of their runs, oldest
        first: what `locks.analysis` reads."""
        scope, params = ("AND t.repo = ?", [repo]) if repo is not None else ("", [])
        tasks = [dict(r) for r in self._conn.execute(
            "SELECT t.id, t.repo, t.title, t.kind, t.status, t.source, t.session_id, t.options, t.workflow_run, "  # noqa: S608
            "t.workflow_step, t.start_cell, t.final_cell, t.cost_usd, t.pinned, t.created_at, t.updated_at, "
            "w.name AS workflow FROM tasks t LEFT JOIN workflow_runs w ON w.id = t.workflow_run "
            f"WHERE t.source IN ('locks', 'queue') AND t.updated_at >= ? {scope} ORDER BY t.id",
            [since, *params])]
        tasks = [t for t in tasks if '"investigation"' not in (t.get("options") or "")]
        if not tasks:
            return [], []
        ids = [t["id"] for t in tasks]
        events = []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = self._conn.execute(
                f"SELECT task_id, ts, kind, data FROM events WHERE task_id IN ({', '.join('?' * len(chunk))}) "  # noqa: S608
                f"AND kind IN ({', '.join('?' * len(self.RUN_EVENTS))}) ORDER BY id", [*chunk, *self.RUN_EVENTS])
            events += [{**dict(r), "data": json.loads(r["data"])} for r in rows]
        return tasks, events

    def turns_since(self, since: str, *, repo: str | None = None) -> list[dict]:
        """A session's own turns and its subagents' delegations since `since`, each
        with the tokens its messages used, by model: what they took and cost."""
        scope, params = ("AND t.repo = ?", [repo]) if repo is not None else ("", [])
        rows = self._conn.execute(
            "SELECT t.id, t.source, t.status, t.session_id, t.created_at, t.updated_at, u.model, "  # noqa: S608
            "sum(u.input_tokens) AS input_tokens, sum(u.output_tokens) AS output_tokens, "
            "sum(u.cache_read) AS cache_read, sum(u.cache_write) AS cache_write "
            "FROM tasks t LEFT JOIN usage u ON u.task_id = t.id "
            f"WHERE t.source IN ('hook', 'delegation') AND t.created_at >= ? {scope} "
            "GROUP BY t.id, u.model ORDER BY t.id", [since, *params]).fetchall()
        return [dict(r) for r in rows]

    def events_of(self, task_id: int, kind: str) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind))
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def version_of(self, task_id: int) -> str | None:
        """The release that ran a task: the newest `version` its events keep (None for records from
        before releases were kept)."""
        row = self._conn.execute(
            "SELECT json_extract(data, '$.version') FROM events WHERE task_id = ? "
            "AND json_extract(data, '$.version') IS NOT NULL ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        return str(row[0]) if row else None

    def check_tails(self, task_id: int) -> dict[int, str]:
        """What locks' check said on each attempt of a task that it failed (`launch.check_tail`),
        by the attempt's `seq`: kept on the `attempt_finished` event, so attempts recorded
        before it was kept have none."""
        return {int(e["data"]["seq"]): str(e["data"]["check_tail"]) for e in self.events_of(task_id, "attempt_finished")
                if e["data"].get("seq") is not None and e["data"].get("check_tail")}

    def events_of_kinds(self, kinds: Sequence[str], *, since: str | None = None) -> list[dict]:
        """Every task's events of these kinds, oldest first, from `since` (an ISO time) on."""
        clause = " AND ts >= ?" if since else ""
        rows = self._conn.execute(
            f"SELECT * FROM events WHERE kind IN ({', '.join('?' for _ in kinds)}){clause} ORDER BY id",  # noqa: S608
            [*kinds, *([since] if since else [])])
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def attempt(self, task_id: int, seq: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM attempts WHERE task_id = ? AND seq = ?", (task_id, seq)).fetchone()
        return dict(row) if row else None

    def kind_record(self, kind: str, repo: str | None, *, limit: int = 30) -> dict[str, Any]:
        """The last passes of a kind in a repository: how many, the median wall time of their runs
        (`finished.wall_s`) and the mean cost per pass; what an advice says a worker costs here."""
        scope, params = ("AND repo = ?", [repo]) if repo is not None else ("", [])
        rows = self._conn.execute(
            "SELECT id, cost_usd FROM tasks WHERE kind = ? AND status = 'done' AND source IN ('locks', 'queue') "  # noqa: S608
            f"{scope} ORDER BY id DESC LIMIT ?", [kind, *params, limit]).fetchall()
        walls = sorted(float(w) for tid, _ in rows
                       if (w := (((self.final_event(int(tid)) or {}).get("data")) or {}).get("wall_s")))
        return {"passes": len(rows), "p50_s": walls[(len(walls) - 1) // 2] if walls else None,
                "cost_per_pass": round(sum(float(c or 0) for _, c in rows) / len(rows), 4) if rows else None}

    def routed_followed(self, *, since: str = "", repo: str | None = None,
                        within_s: int = 1800) -> list[dict]:
        """Each `routed` advice with whether the session sent locks work after it, within
        `within_s`: the evidence a route's advice is weighed by, never a rule's input."""
        scope, args = ("AND t.repo = ?", [repo]) if repo is not None else ("", [])
        rows = self._conn.execute(
            "SELECT e.ts, e.data, t.session_id, EXISTS (SELECT 1 FROM tasks w WHERE w.session_id = t.session_id "  # noqa: S608
            "AND w.source IN ('locks', 'queue') AND w.created_at >= e.ts "
            "AND w.created_at <= strftime('%Y-%m-%dT%H:%M:%S+00:00', e.ts, ?)) "
            f"FROM events e JOIN tasks t ON t.id = e.task_id WHERE e.kind = 'routed' AND e.ts >= ? {scope}",
            [f"+{int(within_s)} seconds", since, *args]).fetchall()
        out = []
        for r in rows:
            data = json.loads(r[1])
            out.append({"ts": r[0], "mode": data.get("mode"), "sent": bool(r[3]),
                        "read_said": bool(data.get("read_said"))})
        return out

    def notes_relied(self, project: str) -> dict[int, int]:
        """How many attempts in a project named each note as relied on (`attempt_finished.notes_used`)."""
        out: dict[int, int] = {}
        for (raw,) in self._conn.execute(
                "SELECT e.data FROM events e JOIN tasks t ON t.id = e.task_id "
                "WHERE e.kind = 'attempt_finished' AND t.repo = ? AND e.data LIKE '%notes_used%'", (project,)):
            for n in json.loads(raw).get("notes_used") or ():
                if isinstance(n, int):
                    out[n] = out.get(n, 0) + 1
        return out

    def notes_since(self, project: str, since: str, *, limit: int = 5) -> list[dict]:
        """A project's live notes filed at `since` or after, newest first."""
        rows = self._conn.execute(
            "SELECT topic, title, text FROM notes WHERE project = ? AND state IN ('current', 'candidate') "
            "AND created_at >= ? ORDER BY id DESC LIMIT ?", (project, since, limit)).fetchall()
        return [dict(r) for r in rows]

    def last_event(self, task_id: int, kind: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1", (task_id, kind)).fetchone()
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    def orientations(self, repo: str, *, limit: int = 40) -> list[dict]:
        """What passing workers in a repository had to find before their first change
        (`oriented` events' data: lines, role, areas), newest first."""
        rows = self._conn.execute(
            "SELECT e.data FROM events e JOIN tasks t ON t.id = e.task_id WHERE e.kind = 'oriented' AND t.repo = ? "
            "ORDER BY e.id DESC LIMIT ?", (repo, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def primer_trial(self, repo: str | None) -> list[dict]:
        """The tasks of a repository (None: of every one) whose brief was put in a primer arm: the arm, whether
        the first try passed, its input tokens, and how many tool calls it made before
        its first change (None when it made none)."""
        return self.arm_trial(repo, "primer_arm")

    def arm_trial(self, repo: str | None, key: str) -> list[dict]:
        """The tasks of a repository (None: of every one) whose first `briefed` event put them in an arm
        under `key` (`primer_arm`, `trunk_arm`): the arm, whether the first try passed, its input
        tokens, its duration, and how many tool calls it made before its first change (None when
        it made none). `nature` is the kind of work its first brief kept beside the arm (`primer_nature`
        for `primer_arm`), None for a record written before it was kept."""
        rows = self._conn.execute(
            "SELECT e.task_id, e.data FROM events e JOIN tasks t ON t.id = e.task_id "
            "WHERE e.kind = 'briefed' AND (? IS NULL OR t.repo = ?) ORDER BY e.id", (repo, repo)).fetchall()
        out: dict[int, dict] = {}
        for task_id, raw in rows:
            data = json.loads(raw)
            arm = data.get(key)
            if arm and task_id not in out:
                out[task_id] = {"task_id": task_id, "arm": arm,
                                "nature": data.get(f"{key.removesuffix('_arm')}_nature")}
        for task_id, row in out.items():
            first = self._conn.execute(
                "SELECT passed, input_tokens, duration_s FROM attempts WHERE task_id = ? AND seq = 1",
                (task_id,)).fetchone()
            tools = [r[0] for r in self._conn.execute(
                "SELECT tool FROM tool_events WHERE task_id = ? AND attempt = 1 ORDER BY id", (task_id,))]
            edit = next((i for i, tool in enumerate(tools) if tool in EDIT_TOOLS), None)
            row.update(passed_first=bool(first and first[0]), input_tokens=int(first[1]) if first else 0,
                       duration_s=float(first[2] or 0) if first else 0.0, calls_before_edit=edit)
        return [r for r in out.values() if r["input_tokens"] or r["passed_first"]]

    def verifier_record(self, repo: str | None) -> list[dict]:
        """Every independent check (`verified`) and every pass its sampling let through unchecked
        (`verifier_sampled`) of a repository (None: of every one), oldest first: the task, its kind
        and repository, the event and its data, and the cell of the attempt it read (`attempt_cell`,
        for events written before they named it themselves)."""
        rows = self._conn.execute(
            "SELECT e.id, e.task_id, e.ts, e.kind AS event, e.data, t.kind, t.repo, a.cell AS attempt_cell "
            "FROM events e JOIN tasks t ON t.id = e.task_id LEFT JOIN attempts a ON a.task_id = e.task_id "
            "AND a.seq = json_extract(e.data, '$.seq') WHERE e.kind IN ('verified', 'verifier_sampled') "
            "AND (? IS NULL OR t.repo = ?) ORDER BY e.id", (repo, repo)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def probation_arms(self, repo: str, kind: str, band: int, *, since: str = "") -> dict[str, int]:
        """How many tasks of a kind and size here were planned in each arm of a probation trial
        (`planned.probation.arm`) since `since`: running ones count, so arms alternate as they start."""
        rows = self._conn.execute(
            "SELECT json_extract(e.data, '$.probation.arm'), count(DISTINCT e.task_id) FROM events e "
            "JOIN tasks t ON t.id = e.task_id WHERE e.kind = 'planned' AND t.repo = ? AND t.kind = ? "
            "AND t.points = ? AND e.ts >= ? AND json_extract(e.data, '$.probation.arm') IS NOT NULL "
            "GROUP BY 1", (repo, kind, band, since)).fetchall()
        counts = {"probation": 0, "ladder": 0}
        counts.update({str(arm): int(n) for arm, n in rows})
        return counts

    def capability_use(self, name: str, *, since: str = "", repo: str | None = None) -> list[dict]:
        """Every attempt that was handed the capability `name` (its `attempt_started` event lists
        it): how many of the worker's calls went to its tools, and whether the attempt passed."""
        rows = self._conn.execute(
            "SELECT e.task_id, e.data FROM events e JOIN tasks t ON t.id = e.task_id "
            "WHERE e.kind = 'attempt_started' AND e.ts >= ? AND (? IS NULL OR t.repo = ?) ORDER BY e.id",
            (since, repo, repo)).fetchall()
        out = []
        for task_id, raw in rows:
            data = json.loads(raw)
            if name not in (data.get("capabilities") or ()):
                continue
            seq = data.get("seq")
            calls = self._conn.execute(
                "SELECT COUNT(*) FROM tool_events WHERE task_id = ? AND attempt = ? AND tool LIKE ?",
                (task_id, seq, f"mcp__{name}__%")).fetchone()[0]
            done = self._conn.execute("SELECT passed FROM attempts WHERE task_id = ? AND seq = ?",
                                      (task_id, seq)).fetchone()
            out.append({"task_id": task_id, "seq": seq, "calls": int(calls), "passed": bool(done and done[0])})
        return out

    def attempt_calls(self, task_id: int, attempt: int) -> list[dict]:
        """An attempt's worker's tool calls, oldest first: when, the tool, its signature, its
        argument's hash and whether it went through (`behavior.profile`)."""
        return self._calls("WHERE task_id = ? AND attempt = ?", (task_id, attempt))

    def session_calls(self, session_id: str) -> list[dict]:
        """The tool calls of one Claude Code session, oldest first, shaped as `attempt_calls`: the
        independent check's session is found by this, its calls being logged under no attempt."""
        return self._calls("WHERE session_id = ?", (session_id,))

    def unattributed_calls(self, task_id: int, since: str, until: str) -> list[dict]:
        """A task's calls that belong to no attempt (the independent check's, `orchestrate._verifier_env`)
        between `since` and `until`, oldest first, shaped as `attempt_calls`. The log keeps milliseconds
        and the store's own clock seconds, so a bound counts as the second it names: a call in the
        second `until` was taken at is inside."""
        return self._calls("WHERE task_id = ? AND attempt IS NULL AND substr(ts, 1, 19) >= ? "
                           "AND substr(ts, 1, 19) <= ?", (task_id, since[:19], until[:19]))

    def _calls(self, where: str, params: tuple) -> list[dict]:
        rows = self._conn.execute(
            f"SELECT ts, tool, sig, arg_hash, ok FROM tool_events {where} ORDER BY id", params)  # noqa: S608
        return [{"ts": ts, "tool": tool, "sig": sig, "arg_hash": arg_hash, "ok": ok}
                for ts, tool, sig, arg_hash, ok in rows]

    def tool_counts(self, task_id: int, attempt: int, prefix: str, *, ok_only: bool = False) -> dict[str, int]:
        """How many times an attempt's worker called each tool whose name starts with `prefix`
        (`mcp__livespec__`), by the name after it; `ok_only`, only the calls that went through."""
        rows = self._conn.execute(
            "SELECT tool, COUNT(*) FROM tool_events WHERE task_id = ? AND attempt = ? "
            "AND substr(tool, 1, ?) = ? AND (? = 0 OR ok = 1) GROUP BY tool ORDER BY tool",
            (task_id, attempt, len(prefix), prefix, int(ok_only)))
        return {str(tool)[len(prefix):]: int(n) for tool, n in rows}

    def briefs(self, repo: str) -> list[dict]:
        """Each task of a repository with its first `briefed` event (a resume's brief is not
        the one its first try ran under): the task's id and status, and the brief's time and data."""
        rows = self._conn.execute(
            "SELECT e.task_id, e.ts, e.data, t.status FROM events e JOIN tasks t ON t.id = e.task_id "
            "WHERE e.kind = 'briefed' AND t.repo = ? ORDER BY e.id", (repo,)).fetchall()
        out: dict[int, dict] = {}
        for task_id, ts, raw, status in rows:
            out.setdefault(int(task_id), {"task_id": int(task_id), "status": status, "ts": ts,
                                          "data": json.loads(raw)})
        return list(out.values())

    def role_dead_ends(self, repo: str, role: str, *, since: str = "") -> list[dict]:
        """What a role's workers tried here that failed (`failed` or `partial` fixes kept with
        the role's cell), after `since`, oldest first: the problem's title, the fix and why."""
        return [dict(r) for r in self._conn.execute(
            "SELECT f.id, f.task_id, f.created_at, f.description, f.why, f.outcome, p.title FROM fixes f "
            "JOIN problems p ON p.id = f.problem_id WHERE f.repo = ? AND f.role = ? "
            "AND f.outcome IN ('failed', 'partial') AND f.created_at > ? ORDER BY f.id", (repo, role, since))]

    def add_role_revision(self, **record: Any) -> int:
        """One record of a role's revision (proposed, adopted, dropped, kept, reverted), appended:
        never edited, never removed."""
        for key in ("lessons", "evidence"):
            if key in record and not isinstance(record[key], str):
                record[key] = json.dumps(record[key], ensure_ascii=False)
        cur = self._conn.execute(f"INSERT INTO role_revisions ({', '.join(record)}) "  # noqa: S608
                                 f"VALUES ({', '.join('?' * len(record))})", list(record.values()))
        return int(cur.lastrowid)

    def role_revisions(self, *, repo: str | None = None, role: str | None = None, since: str = "") -> list[dict]:
        """The records of the roles' revisions, oldest first, their lessons and evidence read back."""
        rows = self._conn.execute(
            "SELECT * FROM role_revisions WHERE (? IS NULL OR repo = ?) AND (? IS NULL OR role = ?) AND at >= ? "
            "ORDER BY id", (repo, repo, role, role, since)).fetchall()
        return [{**dict(r), "lessons": json.loads(r["lessons"] or "[]"),
                 "evidence": json.loads(r["evidence"] or "{}")} for r in rows]

    def session_events(self, session_id: str | None, kinds: Sequence[str], *, since: str = "",
                       repo: str | None = None) -> list[dict]:
        """Events of these kinds on any task of a session (None: of every session),
        oldest first, each with its task's session and repository."""
        scope, params = "", [*kinds, since]
        if session_id is not None:
            scope, params = scope + " AND t.session_id = ?", [*params, session_id]
        if repo is not None:
            scope, params = scope + " AND t.repo = ?", [*params, repo]
        rows = self._conn.execute(
            "SELECT e.*, t.session_id, t.repo FROM events e JOIN tasks t ON t.id = e.task_id "  # noqa: S608
            f"WHERE e.kind IN ({', '.join('?' * len(kinds))}) AND e.ts >= ?{scope} ORDER BY e.id", params).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def final_event(self, task_id: int, status: str | None = None) -> dict | None:
        """The `finished` event that ended the task as it stands: the last one whose
        status is the task's own. A task resumed after dying has an ending per try,
        and between its status and its final event (the run files its notes there)
        the latest one on record is an earlier death's, which says nothing of the
        work that passed. None while no ending matches."""
        if status is None:
            row = self._conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            status = row["status"] if row else None
        row = self._conn.execute(
            "SELECT * FROM events WHERE task_id = ? AND kind = 'finished' AND json_extract(data, '$.status') = ? "
            "ORDER BY id DESC LIMIT 1", (task_id, status)).fetchone()
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    def last_event_id(self) -> int:
        return int(self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])

    def expected_cost(self, kind: str, *, repo: str | None = None, limit: int = 20) -> tuple[float, int] | None:
        """The average cost of the last finished tasks of a kind, and how many."""
        scope, params = ("AND repo = ?", [repo]) if repo is not None else ("", [])
        rows = self._conn.execute(
            "SELECT cost_usd FROM tasks WHERE kind = ? AND source = 'locks' "  # noqa: S608
            f"AND status = 'done' {scope} ORDER BY id DESC LIMIT ?", [kind, *params, limit]).fetchall()
        if not rows:
            return None
        return sum(r[0] for r in rows) / len(rows), len(rows)

    def investigator_costs(self, *, limit: int = 20) -> list[float]:
        """What the last finished investigators of `locks investigate` cost: the
        estimate of the next investigation's cost is made from them."""
        rows = self._conn.execute(
            "SELECT cost_usd FROM tasks WHERE status = 'done' AND options LIKE '%\"investigation\"%' "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [float(r[0] or 0) for r in rows]

    # --- habits ------------------------------------------------------------------

    def add_tool_event(self, **record: Any) -> None:
        record = {"ts": now(), **record}
        cols = ", ".join(record)
        self._conn.execute(f"INSERT INTO tool_events ({cols}) VALUES ({', '.join('?' for _ in record)})",  # noqa: S608
                           list(record.values()))

    def tool_events(self, *, days: int = 30, repo: str | None = None, limit: int = 50_000) -> list[dict]:
        """Tool events, each with the repository it happened in: its task's, else its
        session's. Read from what locks already knows of both; the fast path that
        records an event never writes a path. With `repo`, that repository's only."""
        where, params = "e.ts >= ?", [_days_ago(days)]
        if repo is not None:
            where += " AND COALESCE(t.repo, s.repo) = ?"
            params.append(repo)
        rows = self._conn.execute(
            "SELECT e.*, COALESCE(t.repo, s.repo) AS repo FROM tool_events e "  # noqa: S608
            "LEFT JOIN tasks t ON t.id = e.task_id LEFT JOIN sessions s ON s.id = e.session_id "
            f"WHERE {where} ORDER BY e.id LIMIT ?", [*params, limit]).fetchall()
        return [dict(r) for r in rows]

    def passed_attempt_events(self, kind: str, *, repo: str | None = None, limit: int = 20_000) -> list[dict]:
        """Tool events recorded inside worker attempts that passed, for one kind."""
        scope, params = ("AND t.repo = ?", [repo]) if repo is not None else ("", [])
        rows = self._conn.execute(
            "SELECT e.* FROM tool_events e JOIN tasks t ON t.id = e.task_id "  # noqa: S608
            "JOIN attempts a ON a.task_id = e.task_id AND a.seq = e.attempt AND a.passed = 1 "
            f"WHERE t.kind = ? {scope} ORDER BY e.id LIMIT ?", [kind, *params, limit]).fetchall()
        return [dict(r) for r in rows]

    def add_habit(self, *, steps: list[str], command: str, trigger_suffix: str, settings_path: str,
                  candidate: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO habits (candidate, steps, command, trigger_suffix, settings_path, installed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (candidate, json.dumps(steps), command, trigger_suffix, settings_path, now()))
        return int(cur.lastrowid)

    def habit(self, habit_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
        return dict(row) if row else None

    def habits(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM habits WHERE removed = 0 ORDER BY id")]

    def update_habit(self, habit_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(f"UPDATE habits SET {sets} WHERE id = ?", [*fields.values(), habit_id])  # noqa: S608

    def habit_ran(self, habit_id: int, *, ok: bool, max_failures: int, found: bool = False) -> None:
        """Count a run; reset the streak on success, turn the habit off on the third failure. A run
        whose check found something (`found`) is counted as caught and resets the streak: the
        command ran, which is what the streak measures."""
        if ok or found:
            self._conn.execute("UPDATE habits SET runs = runs + 1, failures = 0, caught = caught + ? WHERE id = ?",
                               (int(found), habit_id))
            return
        self._conn.execute(
            "UPDATE habits SET runs = runs + 1, failures = failures + 1, "
            "disabled_at = CASE WHEN failures + 1 >= ? THEN ? ELSE disabled_at END WHERE id = ?",
            (max_failures, now(), habit_id))

    # --- events: what a watcher reads ---------------------------------------

    def add_event(self, task_id: int, event: str, /, **data: Any) -> int:
        if event in VERSIONED:
            # The version that wrote it: `locks doctor` tells a bug that came back from the
            # records a release that fixes it never wrote.
            data.setdefault("version", __version__)
        cur = self._conn.execute(
            "INSERT INTO events (task_id, ts, kind, data) VALUES (?, ?, ?, ?)",
            (task_id, now(), event, json.dumps(data, ensure_ascii=False, default=str)),
        )
        if event == "finished":
            self.pulse()
        self._trace(task_id, event, data)
        return int(cur.lastrowid)

    def _trace(self, task_id: int, event: str, data: dict[str, Any]) -> None:
        """The opt-in trace (`locks.trace`): the event under the session that sent the task."""
        try:
            from locks import trace

            env = os.environ
            if not trace.enabled(env):
                return
            row = self._conn.execute("SELECT session_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            session = (row[0] if row else None) or env.get("LOCKS_SESSION_ID")
            trace.locks_event(env, session, task_id, event, data)
        except Exception:  # noqa: S110 - a trace never fails what it watches
            pass

    @property
    def pulse_path(self) -> Path:
        """A file touched each time a task ends (`PULSE`): what the mod looks at between its
        full polls, a stat instead of a process, to wake its session soon after."""
        return self.path.parent / PULSE

    def pulse(self) -> None:
        """Touch `pulse_path`. Never raises: a run's ending is never lost to it."""
        with contextlib.suppress(OSError):
            self.pulse_path.touch()

    def events(self, *, after: int = 0, task_id: int | None = None, limit: int = 200) -> list[dict]:
        """Events after `after`, oldest first: a watcher keeps the last id it saw
        and asks for what came since."""
        scope, params = ("AND task_id = ?", [task_id]) if task_id is not None else ("", [])
        rows = self._conn.execute(
            f"SELECT * FROM events WHERE id > ? {scope} ORDER BY id LIMIT ?",  # noqa: S608
            [after, *params, limit],
        ).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def unreported(self, session_id: str, *, hours: int = 24, limit: int = 10) -> list[dict]:
        """Work a session sent that has ended since the session last heard of it:
        its final `finished` event (`final_event`: the one of its current status) is
        newer than its latest `reported` one. Only endings of the last `hours`, so a
        first look does not bring back old work."""
        rows = self._conn.execute(
            "SELECT t.*, f.id AS finished_id, f.data AS finished_data FROM tasks t "
            "JOIN events f ON f.id = (SELECT MAX(id) FROM events WHERE task_id = t.id AND kind = 'finished' "
            "AND json_extract(data, '$.status') = t.status) "
            "WHERE t.session_id = ? AND t.source IN ('locks', 'queue') AND t.status NOT IN ('queued', 'running') "
            "AND f.ts >= ? AND f.id > COALESCE((SELECT MAX(id) FROM events WHERE task_id = t.id "
            "AND kind = 'reported'), 0) ORDER BY f.id LIMIT ?",
            (session_id, (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds"), limit))
        return [{**dict(r), "finished_data": json.loads(r["finished_data"])} for r in rows]

    def unreported_copies(self, session_id: str, *, hours: int = 24, limit: int = 10) -> list[dict]:
        """Endings of work another session sent that asked for this session to hear of it
        too (a `notify` event naming it): a copy, never the owner's delivery, so the session
        that sent the work still hears of it once. New since this session's last `copied`."""
        rows = self._conn.execute(
            "SELECT t.*, f.id AS finished_id, f.data AS finished_data FROM tasks t "
            "JOIN events f ON f.id = (SELECT MAX(id) FROM events WHERE task_id = t.id AND kind = 'finished' "
            "AND json_extract(data, '$.status') = t.status) "
            "WHERE EXISTS (SELECT 1 FROM events n, json_each(n.data, '$.sessions') s WHERE n.task_id = t.id "
            "AND n.kind = 'notify' AND s.value = ?) "
            "AND t.status NOT IN ('queued', 'running') AND f.ts >= ? "
            "AND f.id > COALESCE((SELECT MAX(id) FROM events WHERE task_id = t.id AND kind = 'copied' "
            "AND json_extract(data, '$.session') = ?), 0) ORDER BY f.id LIMIT ?",
            (session_id, (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds"),
             session_id, limit))
        return [{**dict(r), "finished_data": json.loads(r["finished_data"])} for r in rows]

    def claim_copies(self, session_id: str, *, via: str) -> list[dict]:
        """`unreported_copies`, marked delivered in the same transaction."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            copies = self.unreported_copies(session_id)
            for t in copies:
                self._conn.execute("INSERT INTO events (task_id, ts, kind, data) VALUES (?, ?, 'copied', ?)",
                                   (t["id"], now(), json.dumps({"session": session_id, "via": via})))
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return copies

    def claim_unreported(self, session_id: str, *, via: str) -> list[dict]:
        """Read and mark in one transaction: two deliverers at once (the Stop hook
        and a mod's poll at a turn's end) never both hand the session the same
        ending."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            ended = self.unreported(session_id)
            for t in ended:
                self._conn.execute("INSERT INTO events (task_id, ts, kind, data) VALUES (?, ?, 'reported', ?)",
                                   (t["id"], now(), json.dumps({"via": via})))
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return ended

    @contextlib.contextmanager
    def immediate(self) -> Iterator[None]:
        """One write transaction: what is read in it is still true when it writes."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def busy(self, repos: Sequence[str]) -> list[int]:
        """Tasks of these projects that are queued or running."""
        if not repos:
            return []
        marks = ",".join("?" * len(repos))
        return [int(r[0]) for r in self._conn.execute(
            f"SELECT id FROM tasks WHERE repo IN ({marks}) AND status IN ('queued', 'running')",  # noqa: S608
            list(repos))]

    def rekey(self, old: str, new: str, prefix: str = "", inside: str | None = None) -> dict[str, int]:
        """Move a repository's memory and history to another key, in one transaction
        (a repository joining a workspace). Its tasks, workflow runs, sessions and
        their usage move where they ran in `inside` (its checkout); a clone elsewhere
        with the same key keeps its own. Its notes move, their anchors named with
        `prefix` in front, and its topics with them; while such a clone remains,
        the notes are copied instead, so neither loses them. Problems and fixes are
        global and stay as they are. How many notes and tasks moved."""
        where, args = ("", []) if inside is None else (" AND (cwd = ? OR cwd LIKE ?)",
                                                       [inside, inside.rstrip("/") + "/%"])
        with self.immediate():
            tasks = self._conn.execute(f"UPDATE tasks SET repo = ? WHERE repo = ?{where}",  # noqa: S608
                                       [new, old, *args]).rowcount
            for table in ("workflow_runs", "sessions"):
                self._conn.execute(f"UPDATE {table} SET repo = ? WHERE repo = ?{where}",  # noqa: S608
                                   [new, old, *args])
            self._conn.execute("UPDATE usage SET repo = ? WHERE repo = ? AND (session_id IN (SELECT id FROM sessions "
                               "WHERE repo = ?) OR task_id IN (SELECT id FROM tasks WHERE repo = ?))",
                               (new, old, new, new))
            # Work left under the key that ran somewhere else: another clone of the repository.
            clone = self._conn.execute("SELECT 1 FROM tasks WHERE repo = ? AND cwd != '' UNION SELECT 1 FROM sessions "
                                       "WHERE repo = ? AND cwd != '' LIMIT 1", (old, old)).fetchone() is not None
            notes = [int(r[0]) for r in self._conn.execute("SELECT id FROM notes WHERE project = ? ORDER BY id",
                                                           (old,))]
            moved = self._copy_notes(notes, new, prefix) if clone else notes
            if not clone:
                self._conn.execute("UPDATE notes SET project = ? WHERE project = ?", (new, old))
                self._conn.execute("UPDATE memory_fts SET repo = ? WHERE kind = 'note' AND repo = ?", (new, old))
                for start in range(0, len(notes), 500):
                    chunk = notes[start:start + 500]
                    self._conn.execute(f"UPDATE note_anchors SET path = ? || path "  # noqa: S608
                                       f"WHERE note_id IN ({','.join('?' * len(chunk))})", [prefix, *chunk])
            self._conn.execute("INSERT OR IGNORE INTO note_topics (project, name, description, created_at) "
                               "SELECT ?, name, description, created_at FROM note_topics WHERE project = ?", (new, old))
            if not clone:
                self._conn.execute("DELETE FROM note_topics WHERE project = ?", (old,))
                self._conn.execute("DELETE FROM lanes WHERE repo = ?", (old,))
            self._rekey_standards(old, new, clone=clone)
        return {"notes": len(moved), "tasks": tasks}

    def _rekey_standards(self, old: str, new: str, *, clone: bool) -> None:
        """What calibration kept for `old` goes with its tasks to `new`: where its tasks
        start (a standard `new` already has wins), the count toward the next calibration,
        and each change still waiting for its verdict, carried as a new change of `new`
        that names it (the records are append-only: the old one gets a `carried` verdict).
        A clone that keeps the old key keeps its own copy of the standards."""
        self._conn.execute("INSERT OR IGNORE INTO standards (repo, kind, band, start_cell, start_turns, measured, "
                           "updated_at) SELECT ?, kind, band, start_cell, start_turns, measured, updated_at "
                           "FROM standards WHERE repo = ?", (new, old))
        since_old = int(self.meta(f"standards:since:{old}") or 0)
        if since_old:
            self.set_meta(f"standards:since:{new}", str(int(self.meta(f"standards:since:{new}") or 0) + since_old))
        if clone:
            return
        self._conn.execute("DELETE FROM standards WHERE repo = ?", (old,))
        self._conn.execute("DELETE FROM meta WHERE key = ?", (f"standards:since:{old}",))
        judged = {int(r[0]) for r in self._conn.execute("SELECT change_id FROM calibration_verdicts")}
        stamp = now()
        for change in [dict(r) for r in self._conn.execute(
                "SELECT * FROM calibrations WHERE repo = ? AND reverts IS NULL ORDER BY id", (old,))]:
            if change["id"] in judged:
                continue
            evidence = {**json.loads(change["evidence"] or "{}"), "carried_from": change["id"]}
            carried = self.add_calibration(
                at=change["at"], repo=new, kind=change["kind"], band=change["band"], field=change["field"],
                old_value=change["old_value"], new_value=change["new_value"],
                rule=f"{change['rule']} (carried from #{change['id']} when {old} joined {new})",
                evidence=evidence, version=change["version"])
            self.add_verdict(at=stamp, change_id=change["id"], verdict="carried",
                             reason=f"{old} joined {new}: judged as #{carried}", version=change["version"])

    def _copy_notes(self, ids: list[int], project: str, prefix: str) -> list[int]:
        """Copies of these notes under `project`: their anchors (named with `prefix`
        in front) and the links between them come along. The new ids."""
        made: dict[int, int] = {}
        for note_id in ids:
            n = self.get_note(note_id)
            if n is None:
                continue
            made[note_id] = self.add_note(project, n["topic"], n["title"], n["text"], author=n["author"],
                                          filed_by=n["filed_by"], source=n["source"], task_id=n["task_id"],
                                          session_id=n["session_id"], proposed_topic=n["proposed_topic"])
            if n["state"] != "current":
                self._conn.execute("UPDATE notes SET state = ?, state_reason = ? WHERE id = ?",
                                   (n["state"], n["state_reason"], made[note_id]))
            for a in self.note_anchors([note_id]):
                self.add_anchor(made[note_id], prefix + a["path"], symbol=a["symbol"], commit_sha=a["commit_sha"],
                                blob=a["blob"])
        for edge in self.note_links(list(made)):
            if edge["from_id"] in made and edge["to_id"] in made:
                self._conn.execute("INSERT OR IGNORE INTO note_links (from_id, to_id, kind, created_at) VALUES "
                                   "(?, ?, ?, ?)", (made[edge["from_id"]], made[edge["to_id"]], edge["kind"], now()))
        return list(made.values())

    def merge_note(self, dup: int, kept: int, why: str) -> None:
        """Retire `dup` for `kept`, the same fact: its anchors and links go to `kept`."""
        self._conn.execute("UPDATE note_anchors SET note_id = ? WHERE note_id = ? AND path NOT IN "
                           "(SELECT path FROM note_anchors WHERE note_id = ?)", (kept, dup, kept))
        for column, other in (("from_id", "to_id"), ("to_id", "from_id")):
            self._conn.execute(f"UPDATE OR IGNORE note_links SET {column} = ? WHERE {column} = ? "  # noqa: S608
                               f"AND {other} != ?", (kept, dup, kept))
        self.update_note(dup, state="replaced", replaced_by=kept, state_reason=why)

    def hand_over(self, old: str, new: str, *, repos: Sequence[str] | None = None) -> int:
        """A relay: the work one session sent that is still open, waits on the
        person, or ended without that session hearing of it reports to the session
        that continues it, and so do its active workflow runs. With `repos`, only
        the work in those projects (an orchestrator adopting its own). How many
        tasks moved."""
        with self.immediate():
            ids = {int(t["id"]): t["repo"] for t in self.unreported(old, limit=200)}
            marks = ", ".join("?" * len(OPEN_FOR_A_RELAY))
            ids |= {int(r[0]): r[1] for r in self._conn.execute(
                f"SELECT id, repo FROM tasks WHERE session_id = ? AND source IN ('locks', 'queue') "  # noqa: S608
                f"AND status IN ({marks})", (old, *OPEN_FOR_A_RELAY))}
            if repos is not None:
                ids = {i: r for i, r in ids.items() if r in repos}
            self._conn.executemany("UPDATE tasks SET session_id = ? WHERE id = ?", [(new, i) for i in ids])
            scope, params = _in("repo", repos)
            if scope is not None:
                self._conn.execute(
                    "UPDATE workflow_runs SET session_id = ? WHERE session_id = ? AND status = 'active'"  # noqa: S608
                    + (f" AND {scope.removeprefix('WHERE ')}" if scope else ""), (new, old, *params))
        return len(ids)

    def worker_footprint(self, repo: str | None, *, limit: int = 50) -> tuple[int, int] | None:
        """What one worker attempt reads here, as measured: the median input tokens (what the
        cache served included) and the median turns of the last `limit` attempts in the
        repository, else anywhere. None with fewer than five measured."""
        def median(values: list[int]) -> int:
            values = sorted(values)
            return values[len(values) // 2]

        for scope, params in ((("AND t.repo = ?", [repo]) if repo else ("AND 0", [])), ("", [])):
            # An attempt's input tokens hold what the cache served: they are what it read.
            rows = self._conn.execute(
                "SELECT json_extract(e.data, '$.input_tokens'), json_extract(e.data, '$.turns') "  # noqa: S608
                "FROM events e JOIN tasks t ON t.id = e.task_id WHERE e.kind = 'attempt_finished' "
                f"AND json_extract(e.data, '$.input_tokens') > 0 {scope} ORDER BY e.id DESC LIMIT ?",
                [*params, limit]).fetchall()
            measured = [(int(r[0] or 0), int(r[1] or 0)) for r in rows if r[1]]
            if len(measured) >= 5:
                return median([m[0] for m in measured]), median([m[1] for m in measured])
        return None

    def quiet_senders(self, repos: Sequence[str], before: str, *, but: str) -> list[str]:
        """Sessions not seen since `before` (or never) that sent work in these projects
        that is still open or ended unheard: whose work an orchestrator takes."""
        if not repos:
            return []
        marks = ", ".join("?" * len(repos))
        rows = self._conn.execute(
            f"SELECT DISTINCT t.session_id FROM tasks t LEFT JOIN sessions s ON s.id = t.session_id "  # noqa: S608
            f"WHERE t.repo IN ({marks}) AND t.session_id IS NOT NULL AND t.session_id != ? "
            "AND t.source IN ('locks', 'queue') AND (s.last_seen_at IS NULL OR s.last_seen_at < ?) "
            f"AND (t.status IN ({', '.join('?' * len(OPEN_FOR_A_RELAY))}) OR t.updated_at >= ?)",
            (*repos, but, before, *OPEN_FOR_A_RELAY,
             (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds")))
        return [str(r[0]) for r in rows]

    def mark_reported(self, task_ids: Iterable[int], *, via: str) -> None:
        for task_id in task_ids:
            self.add_event(task_id, "reported", via=via)

    def request_cancel(self, task_id: int, via: str = "cli") -> dict | None:
        """Flag a run to stop, and say where the request came from: the run that
        stops reads it back, so its account can say a person cancelled it, and how."""
        self._conn.execute("UPDATE tasks SET cancel_requested = 1, updated_at = ? WHERE id = ?", (now(), task_id))
        self.add_event(task_id, "cancel_requested", via=via)
        return self.get_task(task_id)

    def denials(self, task_id: int) -> dict[int, list[str]]:
        """What the permission settings refused, per attempt `seq`: kept on the
        attempt's event, not in the attempts table."""
        return self._per_attempt(task_id, "denied")

    def allowances(self, task_id: int) -> dict[int, list[str]]:
        """The `--allow` rules that would have let each attempt's refusals through."""
        return self._per_attempt(task_id, "allow")

    def _per_attempt(self, task_id: int, key: str) -> dict[int, list[str]]:
        rows = self._conn.execute(
            "SELECT data FROM events WHERE task_id = ? AND kind = 'attempt_finished' ORDER BY id", (task_id,))
        out: dict[int, list[str]] = {}
        for row in rows:
            data = json.loads(row[0])
            if data.get("seq") is not None and data.get(key):
                out[int(data["seq"])] = list(data[key])
        return out

    def refusals(self, repo: str, *, limit: int = 200) -> list[dict]:
        """Of the repository's latest `limit` attempts, the ones the permission settings
        refused something to: what was refused, and the rules if they were recorded. A
        refusal ages out as work goes on, however rare attempts with one are."""
        rows = self._conn.execute(
            "SELECT task_id, data FROM (SELECT e.id, e.task_id, e.data FROM events e "
            "JOIN tasks t ON t.id = e.task_id WHERE t.repo = ? AND e.kind = 'attempt_finished' "
            "ORDER BY e.id DESC LIMIT ?) WHERE data LIKE '%\"denied\": [\"%' ORDER BY id DESC", (repo, limit))
        out = []
        for task_id, raw in rows:
            data = json.loads(raw)
            if data.get("denied"):
                out.append({"task_id": task_id, "denied": data["denied"], "allow": data.get("allow") or [],
                            "mode": data.get("permission_mode") or ""})
        return out

    def cancel_requested(self, task_id: int) -> bool:
        row = self._conn.execute("SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return bool(row and row[0])

    # --- problems and fixes: memory across repositories -------------------

    def open_problem(self, title: str, *, repo: str | None, symptom: str = "") -> int:
        """The problem with this title in this repository, opening it if new.

        A problem that was solved and turns up again is *recurring*: the fix
        that solved it stopped holding, and is marked as disproved now.
        """
        stamp = now()
        # A long request's title is clipped in its middle, so two requests can share it: with a
        # symptom (the work's own text), a problem is that text's, never another's of the same title.
        row = self._conn.execute(
            "SELECT id, state FROM problems WHERE title = ? AND repo IS ? AND (? = '' OR symptom = ? OR symptom = '') "
            "ORDER BY (symptom = ?) DESC, id DESC LIMIT 1",
            (title, repo, symptom, symptom, symptom),
        ).fetchone()
        if row:
            problem_id, state = int(row[0]), row[1]
            if state == "solved":
                self._conn.execute(
                    "UPDATE problems SET state = 'recurring', last_seen = ?, updated_at = ? WHERE id = ?",
                    (stamp, stamp, problem_id))
                self._conn.execute(
                    "UPDATE fixes SET invalidated_on = ? WHERE problem_id = ? AND outcome = 'worked' "
                    "AND invalidated_on IS NULL", (stamp, problem_id))
            else:
                self._conn.execute("UPDATE problems SET last_seen = ? WHERE id = ?", (stamp, problem_id))
            return problem_id
        cur = self._conn.execute(
            "INSERT INTO problems (repo, title, symptom, state, created_at, updated_at, first_seen, last_seen) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
            (repo, title, symptom, stamp, stamp, stamp, stamp),
        )
        problem_id = int(cur.lastrowid)
        self._index("problem", problem_id, repo, f"{title}\n{symptom}")
        return problem_id

    def add_fix(
        self,
        problem_id: int,
        description: str,
        outcome: str,
        *,
        repo: str | None,
        why: str = "",
        evidence: str = "",
        task_id: int | None = None,
        commit_sha: str | None = None,
        role: str | None = None,
        areas: Sequence[str] | None = None,
    ) -> int:
        if outcome not in FIX_OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        stamp = now()
        cur = self._conn.execute(
            "INSERT INTO fixes (problem_id, repo, description, outcome, why, evidence, task_id, created_at, "
            "believed_from, commit_sha, role, areas) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (problem_id, repo, description, outcome, why, evidence[:4000], task_id, stamp,
             stamp if outcome == "worked" else None, commit_sha, role,
             None if areas is None else ",".join(areas)),
        )
        fix_id = int(cur.lastrowid)
        self._index("fix", fix_id, repo, f"{description}\n{why}")
        if outcome == "worked":
            self._conn.execute(
                "UPDATE problems SET state = 'solved', updated_at = ?, last_seen = ? WHERE id = ?",
                (stamp, stamp, problem_id))
        return fix_id

    def task_has_failed_fix(self, task_id: int) -> bool:
        """Whether a task's attempts left a failed fix, in any of its runs."""
        return self._conn.execute("SELECT 1 FROM fixes WHERE task_id = ? AND outcome = 'failed' LIMIT 1",
                                  (task_id,)).fetchone() is not None

    def regressed(self, text: str, *, repo: str | None, task_id: int, keep: int | None = None) -> list[int]:
        """The solved problems of `repo` that work strictly matching `text` failed on again,
        made `recurring` and their fixes disproved, named by the task that met them: the
        memory hears of a regression under another title too. `keep` (the failing work's own
        problem) is left alone. Returns their ids."""
        stamp, found = now(), []
        for problem in self.search(text, repo=repo, limit=10, strict=True):
            if problem["repo"] != repo or problem["state"] != "solved" or problem["id"] == keep or any(
                    f["task_id"] == task_id for f in problem["fixes"]):
                continue
            self._conn.execute("UPDATE problems SET state = 'recurring', last_seen = ?, updated_at = ? WHERE id = ?",
                               (stamp, stamp, problem["id"]))
            self._conn.execute(
                "UPDATE fixes SET invalidated_on = ?, why = CASE WHEN why = '' THEN ? ELSE why || '; ' || ? END "
                "WHERE problem_id = ? AND outcome = 'worked' AND invalidated_on IS NULL",
                (stamp, f"task #{task_id} met it again", f"task #{task_id} met it again", problem["id"]))
            found.append(int(problem["id"]))
        return found

    def invalidate_fix(self, fix_id: int, why: str) -> dict | None:
        """A fix that was believed to work, shown wrong: its problem is open again."""
        row = self._conn.execute("SELECT problem_id FROM fixes WHERE id = ?", (fix_id,)).fetchone()
        if row is None:
            return None
        stamp = now()
        self._conn.execute(
            "UPDATE fixes SET invalidated_on = ?, why = CASE WHEN why = '' THEN ? ELSE why || '; ' || ? END "
            "WHERE id = ?", (stamp, why, why, fix_id))
        self._conn.execute(
            "UPDATE problems SET state = 'recurring', updated_at = ? WHERE id = ? AND state = 'solved'",
            (stamp, row[0]))
        self._index("fix", fix_id, None, why)
        return self.problem(int(row[0]))

    def forget_problem(self, problem_id: int) -> dict | None:
        """A problem a person says was never one (a failure the environment or a
        clash caused, filed as the work's): it and its fixes leave memory, and
        no brief shows them again. The problem as it was, or None."""
        problem = self.problem(problem_id)
        if problem is None:
            return None
        fix_ids = [f["id"] for f in problem["fixes"]]
        self._conn.execute("DELETE FROM fixes WHERE problem_id = ?", (problem_id,))
        self._conn.execute("DELETE FROM problems WHERE id = ?", (problem_id,))
        self._conn.execute("DELETE FROM memory_fts WHERE kind = 'problem' AND ref_id = ?", (problem_id,))
        for fix_id in fix_ids:
            self._conn.execute("DELETE FROM memory_fts WHERE kind = 'fix' AND ref_id = ?", (fix_id,))
        return problem

    def unscoped_fixes(self) -> list[dict]:
        """The fixes a task recorded before scopes were kept (`locks.cells.backfill`)."""
        rows = self._conn.execute("SELECT * FROM fixes WHERE areas IS NULL AND task_id IS NOT NULL ORDER BY id")
        return [dict(r) for r in rows.fetchall()]

    def scope_fix(self, fix_id: int, *, role: str | None, areas: Sequence[str]) -> None:
        self._conn.execute("UPDATE fixes SET role = ?, areas = ? WHERE id = ?", (role, ",".join(areas), fix_id))

    def set_fix_commit(self, task_id: int, commit_sha: str) -> None:
        self._conn.execute(
            "UPDATE fixes SET commit_sha = ? WHERE task_id = ? AND outcome = 'worked' AND commit_sha IS NULL",
            (commit_sha, task_id))

    def problem(self, problem_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM problems WHERE id = ?", (problem_id,)).fetchone()
        if not row:
            return None
        fixes = self._conn.execute(
            "SELECT * FROM fixes WHERE problem_id = ? ORDER BY id", (problem_id,)
        ).fetchall()
        return {**dict(row), "fixes": [dict(f) for f in fixes]}

    def search(self, query: str, *, repo: str | None = None, limit: int = 10, strict: bool = False) -> list[dict]:
        """Problems matching `query` in any repository, this repository's first.

        Every word must match; when nothing does, any word may. `strict` keeps
        only problems sharing several words with the query: a hook putting text
        in front of the model wants no loose matches, and a whole prompt rarely
        matches word for word. The repository
        only orders the results — scoping it out would hide the dead end that
        was found next door.
        """
        terms = [w.replace('"', "") for w in words(query)]
        if strict:
            terms = self._distinctive(terms)
            if len(terms) < STRICT_MIN_TERMS:
                return []  # what is left is too little to tell this problem from another
        terms = terms[:12]
        if not terms:
            return []
        hits: list[sqlite3.Row] = []
        for joiner in (" ", " OR "):
            match = joiner.join(f'"{t}"' for t in terms)
            hits = self._conn.execute(
                "SELECT kind, ref_id FROM memory_fts WHERE memory_fts MATCH ? AND kind IN ('problem', 'fix') "
                "ORDER BY bm25(memory_fts) LIMIT 100",
                (match,),
            ).fetchall()
            if hits:
                break
        problem_ids: list[int] = []
        for hit in hits:
            pid = int(hit["ref_id"]) if hit["kind"] == "problem" else self._problem_of_fix(int(hit["ref_id"]))
            if pid is not None and pid not in problem_ids:
                problem_ids.append(pid)
        found = [p for p in (self.problem(pid) for pid in problem_ids) if p]
        if strict:
            wanted = set(terms)
            found = [p for p in found if _overlap(wanted, p) >= min(STRICT_OVERLAP, len(wanted))]
        found.sort(key=lambda p: 0 if repo is not None and p["repo"] == repo else 1)
        return found[:limit]

    def _distinctive(self, terms: Sequence[str]) -> list[str]:
        """The query's words that tell one problem from another: a word most problems hold
        (a workflow step's template, the repository's name) matches them all, so a strict
        match never counts it. Below `COMMON_MIN` problems every word counts."""
        total = self._conn.execute("SELECT count(*) FROM problems").fetchone()[0]
        if total < COMMON_MIN:
            return list(dict.fromkeys(terms))[:STRICT_CANDIDATES]
        kept = []
        for term in list(dict.fromkeys(terms))[:STRICT_CANDIDATES]:
            held = self._conn.execute(
                "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ? AND kind = 'problem'",
                (f'"{term}"',)).fetchone()[0]
            if held < COMMON_SHARE * total:
                kept.append(term)
        return kept

    def dead_ends(
        self, query: str | None = None, *, repo: str | None = None, limit: int = 8, strict: bool = False,
        scope: Callable[[dict], str | None] | None = None,
    ) -> list[dict]:
        """Fixes that failed — and fixes that worked until they did not — each
        with what worked instead when something did. With `scope`, only problems
        it names a scope for (`locks.cells`), each dead end carrying it."""
        problems = (self.search(query, repo=repo, limit=50, strict=strict) if query
                    else self._recent_problems(repo, 50))
        out: list[dict] = []
        for problem in problems:
            found = scope(problem) if scope is not None else None
            if scope is not None and found is None:
                continue
            holding = [f for f in problem["fixes"] if f["outcome"] == "worked" and not f["invalidated_on"]]
            for fix in problem["fixes"]:
                disproved = fix["outcome"] == "worked" and fix["invalidated_on"]
                if fix["outcome"] != "failed" and not disproved:
                    continue
                out.append({
                    "problem_id": problem["id"],
                    "problem": problem["title"],
                    "repo": problem["repo"],
                    "tried": fix["description"],
                    "why": fix["why"] or ("believed to work, then the problem came back" if disproved else ""),
                    "worked_instead": holding[-1]["description"] if holding else "",
                    "disproved_on": fix["invalidated_on"] if disproved else None,
                    **({"scope": found} if found else {}),
                })
                if len(out) >= limit:
                    return out
        return out

    def recent_problems(self, *, repos: Iterable[str] | None = None, limit: int = 40) -> list[dict]:
        """Problems most recently touched, with their fixes; with `repos`, only theirs."""
        scope, params = _in("repo", repos)
        if scope is None:
            return []
        rows = self._conn.execute(
            f"SELECT id FROM problems {scope} ORDER BY updated_at DESC, id DESC LIMIT ?",  # noqa: S608
            [*params, limit],
        ).fetchall()
        return [p for p in (self.problem(int(r[0])) for r in rows) if p]

    def _recent_problems(self, repo: str | None, limit: int) -> list[dict]:
        scope, params = ("WHERE repo = ?", [repo]) if repo is not None else ("", [])
        rows = self._conn.execute(
            f"SELECT id FROM problems {scope} ORDER BY updated_at DESC, id DESC LIMIT ?",  # noqa: S608
            [*params, limit],
        ).fetchall()
        return [p for p in (self.problem(int(r[0])) for r in rows) if p]

    # --- notes: what a project knows, by topic ------------------------------
    #
    # The logic (topics, filing, recall, review) is in `locks.notes`; these are
    # the rows. A note belongs to one project and is never searched from another.

    def add_note(self, project: str, topic: str, title: str, text: str, *, author: str, filed_by: str,
                 source: str = "", task_id: int | None = None, session_id: str | None = None,
                 proposed_topic: str | None = None, state: str = "current", kind: str | None = None) -> int:
        stamp = now()
        cur = self._conn.execute(
            "INSERT INTO notes (project, topic, title, text, author, filed_by, source, task_id, session_id, "
            "proposed_topic, state, kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project, topic, title, text, author, filed_by, source, task_id, session_id, proposed_topic, state,
             kind, stamp, stamp))
        note_id = int(cur.lastrowid)
        self._index("note", note_id, project, f"{title}\n{text}")
        return note_id

    def get_note(self, note_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return dict(row) if row else None

    def update_note(self, note_id: int, **fields: Any) -> dict | None:
        allowed = {"topic", "title", "text", "state", "state_reason", "replaced_by", "proposed_topic", "source"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self._conn.execute(f"UPDATE notes SET {sets}, updated_at = ? WHERE id = ?",  # noqa: S608 - known keys
                               [*fields.values(), now(), note_id])
        note = self.get_note(note_id)
        if note and ("title" in fields or "text" in fields):
            self._conn.execute("DELETE FROM memory_fts WHERE kind = 'note' AND ref_id = ?", (note_id,))
            self._index("note", note_id, note["project"], f"{note['title']}\n{note['text']}")
        return note

    def notes(self, project: str, *, topics: Iterable[str] | None = None, states: Iterable[str] | None = None,
              ids: Iterable[int] | None = None, limit: int = 200, kind: str | None = None) -> list[dict]:
        """A project's notes, latest first; with `kind`, only that kind (`fact` is a note of none)."""
        where, params = ["project = ?"], [project]
        if kind == "fact":
            where.append("kind IS NULL")
        elif kind is not None:
            where.append("kind = ?")
            params.append(kind)
        for column, values in (("topic", topics), ("state", states), ("id", ids)):
            if values is not None:
                values = list(values)
                if not values:
                    return []
                where.append(f"{column} IN ({', '.join('?' * len(values))})")
                params += values
        rows = self._conn.execute(
            f"SELECT * FROM notes WHERE {' AND '.join(where)} ORDER BY updated_at DESC, id DESC LIMIT ?",  # noqa: S608
            [*params, limit]).fetchall()
        return [dict(r) for r in rows]

    def search_notes(self, project: str, query: str, *, limit: int = 20) -> list[int]:
        """Note ids of a project matching `query`, best first: every word, else any."""
        terms = [w.replace('"', "") for w in words(query)][:12]
        if not terms:
            return []
        for joiner in (" ", " OR "):
            rows = self._conn.execute(
                "SELECT ref_id FROM memory_fts WHERE memory_fts MATCH ? AND kind = 'note' AND repo = ? "
                "ORDER BY bm25(memory_fts) LIMIT ?", (joiner.join(f'"{t}"' for t in terms), project, limit),
            ).fetchall()
            if rows:
                return [int(r[0]) for r in rows]
        return []

    def note_counts(self, project: str) -> dict[str, dict[str, int]]:
        """topic -> state -> how many."""
        out: dict[str, dict[str, int]] = {}
        for topic, state, n in self._conn.execute(
                "SELECT topic, state, COUNT(*) FROM notes WHERE project = ? GROUP BY topic, state", (project,)):
            out.setdefault(topic, {})[state] = int(n)
        return out

    def note_projects(self) -> list[str]:
        return [r[0] for r in self._conn.execute("SELECT DISTINCT project FROM notes ORDER BY project")]

    def link_notes(self, from_id: int, to_id: int, kind: str) -> bool:
        if from_id == to_id:
            return False
        cur = self._conn.execute("INSERT OR IGNORE INTO note_links (from_id, to_id, kind, created_at) "
                                 "VALUES (?, ?, ?, ?)", (from_id, to_id, kind, now()))
        return cur.rowcount > 0

    def unlink_notes(self, from_id: int, to_id: int) -> int:
        return self._conn.execute("DELETE FROM note_links WHERE (from_id = ? AND to_id = ?) "
                                  "OR (from_id = ? AND to_id = ?)", (from_id, to_id, to_id, from_id)).rowcount

    def note_links(self, note_ids: Iterable[int]) -> list[dict]:
        """Every link touching these notes, in either direction."""
        ids = list(note_ids)
        if not ids:
            return []
        marks = ", ".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT * FROM note_links WHERE from_id IN ({marks}) OR to_id IN ({marks})",  # noqa: S608
            [*ids, *ids]).fetchall()
        return [dict(r) for r in rows]

    def add_anchor(self, note_id: int, path: str, *, symbol: str | None = None, commit_sha: str | None = None,
                   blob: str | None = None) -> int:
        found = self._conn.execute("SELECT id FROM note_anchors WHERE note_id = ? AND path = ? AND symbol IS ?",
                                   (note_id, path, symbol)).fetchone()
        if found:
            self._conn.execute("UPDATE note_anchors SET commit_sha = ?, blob = ? WHERE id = ?",
                               (commit_sha, blob, found[0]))
            return int(found[0])
        cur = self._conn.execute(
            "INSERT INTO note_anchors (note_id, path, symbol, commit_sha, blob, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (note_id, path, symbol, commit_sha, blob, now()))
        return int(cur.lastrowid)

    def update_anchor(self, anchor_id: int, *, commit_sha: str | None, blob: str | None) -> None:
        self._conn.execute("UPDATE note_anchors SET commit_sha = ?, blob = ? WHERE id = ?",
                           (commit_sha, blob, anchor_id))

    def note_anchors(self, note_ids: Iterable[int]) -> list[dict]:
        ids = list(note_ids)
        if not ids:
            return []
        rows = self._conn.execute(
            f"SELECT * FROM note_anchors WHERE note_id IN ({', '.join('?' * len(ids))}) ORDER BY id",  # noqa: S608
            ids).fetchall()
        return [dict(r) for r in rows]

    def project_anchors(self, project: str, *, states: Sequence[str] = ("current",)) -> list[dict]:
        """The anchors of a project's notes in these states, with the note's state."""
        rows = self._conn.execute(
            f"SELECT a.*, n.state FROM note_anchors a JOIN notes n ON n.id = a.note_id "  # noqa: S608
            f"WHERE n.project = ? AND n.state IN ({', '.join('?' * len(states))}) ORDER BY a.id",
            [project, *states]).fetchall()
        return [dict(r) for r in rows]

    def notes_at(self, project: str, paths: Iterable[str]) -> list[int]:
        """Notes anchored to any of these paths."""
        paths = list(dict.fromkeys(paths))
        if not paths:
            return []
        rows = self._conn.execute(
            f"SELECT DISTINCT a.note_id FROM note_anchors a JOIN notes n ON n.id = a.note_id "  # noqa: S608
            f"WHERE n.project = ? AND a.path IN ({', '.join('?' * len(paths))})", [project, *paths]).fetchall()
        return [int(r[0]) for r in rows]

    def note_topics(self, project: str) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM note_topics WHERE project = ? ORDER BY name", (project,))
        return [dict(r) for r in rows]

    def add_note_topic(self, project: str, name: str, description: str) -> None:
        self._conn.execute("INSERT INTO note_topics (project, name, description, created_at) VALUES (?, ?, ?, ?) "
                           "ON CONFLICT (project, name) DO UPDATE SET description = excluded.description",
                           (project, name, description, now()))

    def _problem_of_fix(self, fix_id: int) -> int | None:
        row = self._conn.execute("SELECT problem_id FROM fixes WHERE id = ?", (fix_id,)).fetchone()
        return int(row[0]) if row else None

    def _index(self, kind: str, ref_id: int, repo: str | None, text: str) -> None:
        self._conn.execute(
            "INSERT INTO memory_fts (kind, ref_id, repo, text) VALUES (?, ?, ?, ?)",
            (kind, ref_id, repo, fold(text)),
        )


def _overlap(wanted: set[str], problem: dict) -> int:
    text = " ".join([problem["title"], problem["symptom"], *(f["description"] for f in problem["fixes"])])
    return len(wanted & set(words(text)))
