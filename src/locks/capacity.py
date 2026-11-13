"""How many workers this machine runs at once, and how the lanes share them.

Horizontal scaling is the number of workers: this level. Vertical scaling is the
cell and the turns each task starts with and climbs by evidence (`matrix`,
`escalate`), and it is not decided here. Money moves neither: a costly ending is
as clean as a cheap one.

The level is kept per machine, in the locks home (`capacity.json`), and moves the
way congestion control does. It starts at what the machine can hold: its CPUs,
its free memory (each worker counted at `WORKER_MEMORY_MB`) and the free disk
where worktrees live (each counted at what the ones here hold, `worktree_mb`, never under
`WORKTREE_DISK_MB`), at least 1. It rises by one when
work waits for a worker and a round of tasks it started (as many as the level)
ended with no limit. It halves, never below 1, when an attempt says the API
turned it away (a rate limit or an overload, read from the CLI's own result,
never from a check's output), when the machine starved a worker (killed for
memory, a full disk), or when free memory or disk falls under its constant, or
the load average climbs over `LOAD_PER_CPU_HIGH` per CPU; after a rate limit it
does not rise again before the API's retry-after. A level the machine itself
lowered (memory, disk, load) goes back up as soon as the machine is clear again
(over `RECOVER_MARGIN` times its constants, the load under `LOAD_PER_CPU_CLEAR`)
for `RECOVER_AFTER_S` and work waits: freeing the disk brings the workers back
without waiting for a round of tasks to end. The person's
`max_parallel` caps it; `auto` puts no ceiling over it. Each change is a
`level_changed` event, on the task that caused it, or on task 0 when the machine
did.

The lanes share the level by reserve and lending. With level L and k lanes that
have work to run, each is reserved at least max(1, L // k) workers, granted in
the order their oldest work was queued while L lasts. A lane runs its reserve
first; what a lane does not use is lent, in the same order, to the lanes that
have more; a lent worker goes back to its lane as soon as that lane has work and
a borrowed task ends: nothing running is ever stopped to give it back. A lane
paused on a person reserves nothing, and its running work still holds its
workers.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from locks.store import Store

#: What one worker is counted at in memory: a `claude -p` with its tools, and a check.
WORKER_MEMORY_MB = 2048
#: What one worktree is counted at on the disk where worktrees live, with its build output.
WORKTREE_DISK_MB = 1024
#: A worktree here is counted at what the ones on this disk really hold (`worktree_mb`: the median
#: of up to `WORKTREE_SAMPLES` of the newest, never under `WORKTREE_DISK_MB`), measured at most
#: every `MEASURE_EVERY_S` within `MEASURE_BUDGET_S`: a repository whose build fills 3 GB a tree
#: filled the disk under a level counted at 1 GB each.
WORKTREE_SAMPLES = 5
MEASURE_EVERY_S = 600
MEASURE_BUDGET_S = 5.0
SIZES = "worktree-size.json"
#: Free memory and free disk under which the level halves.
LOW_MEMORY_MB = 1024
LOW_DISK_MB = 2048
#: A machine whose 1-minute load average is over this many per CPU is loaded: the level
#: halves on it as on low memory, and does not rise. Under `LOAD_PER_CPU_CLEAR` it is calm
#: again. Test suites under load time out and look like bugs (`launch.LOAD_TIMEOUT`).
LOAD_PER_CPU_HIGH = 2.0
LOAD_PER_CPU_CLEAR = 1.0
#: How far over its constants free memory and disk must be, and for how long the machine
#: must stay clear, before a level the machine lowered goes back up: no flapping.
RECOVER_MARGIN = 2
RECOVER_AFTER_S = 60
#: A rate limit that names no retry-after: the level does not rise before this has passed.
RETRY_AFTER_S = 300
#: A machine low on memory or disk halves the level at most this often: the work it
#: runs needs time to end and give back what it holds.
HALVE_EVERY_S = 60
#: What an attempt says starved it, and how the level's halving says it.
STARVED = {"memory": "halved, a worker was killed for memory", "disk": "halved, the disk filled up",
           "load": "halved, a check's tests timed out under load"}
STATE = "capacity.json"
LOCK = "capacity.lock"
MB = 1024 * 1024


@dataclass(frozen=True)
class Machine:
    cpus: int
    #: Free memory and free disk where worktrees live, in MB; None where they cannot be read.
    memory_mb: int | None
    disk_mb: int | None
    #: The 1-minute load average; None where it cannot be read (calm).
    load: float | None = None
    #: What a worktree on this disk holds, measured (`worktree_mb`); None with none to measure.
    worktree_mb: int | None = None


def available_mb(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    """Memory a new process can have without swapping (`MemAvailable`), where /proc says;
    else the free pages the stdlib reads; None when neither can say."""
    with contextlib.suppress(OSError, ValueError):
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    with contextlib.suppress(AttributeError, OSError, ValueError):
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // MB
    return None


def _tree_mb(path: Path, deadline: float) -> int:
    """The disk a folder holds, links never followed; what was read by `deadline`, a floor."""
    total = 0
    for dirpath, _dirs, files in os.walk(path, onerror=lambda _e: None):
        with contextlib.suppress(OSError):
            total += os.lstat(dirpath).st_blocks * 512
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dirpath, name)).st_blocks * 512
        if time.monotonic() > deadline:
            break
    return total // MB


def worktree_mb(root: Path, *, now: float | None = None) -> int | None:
    """What a worktree on this disk holds, in MB: the median of the newest `WORKTREE_SAMPLES`
    under the locks home, kept for `MEASURE_EVERY_S`; None with no worktree. Never raises."""
    now = time.time() if now is None else now
    kept = root / SIZES
    with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
        data = json.loads(kept.read_text(encoding="utf-8"))
        if 0 <= now - float(data["at"]) < MEASURE_EVERY_S:
            return None if data["mb"] is None else int(data["mb"])
    try:
        trees = sorted((p for p in (root / "worktrees").glob("task-*") if p.is_dir() and not p.is_symlink()),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:WORKTREE_SAMPLES]
    except OSError:
        return None
    deadline = time.monotonic() + MEASURE_BUDGET_S
    sizes = sorted(_tree_mb(tree, deadline) for tree in trees)
    mb = sizes[len(sizes) // 2] if sizes else None
    with contextlib.suppress(OSError):
        tmp = kept.with_name(f".{kept.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"at": now, "mb": mb, "trees": len(sizes)}), encoding="utf-8")
        tmp.replace(kept)
    return mb


def per_worktree_mb(m: Machine) -> int:
    """What one more worker is counted at on the disk: the measured tree, never under the constant."""
    return max(WORKTREE_DISK_MB, m.worktree_mb or 0)


def _read(root: Path) -> Machine:
    """What this machine has now: CPUs this process may use, free memory, free disk under `root`."""
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    where = next((p for p in (root / "worktrees", root, Path.home()) if p.is_dir()), None)
    disk = None
    if where is not None:
        with contextlib.suppress(OSError):
            disk = shutil.disk_usage(where).free // MB
    load = None
    with contextlib.suppress(AttributeError, OSError):
        load = os.getloadavg()[0]
    return Machine(max(1, cpus), available_mb(), disk, load, worktree_mb(root))


def machine(root: Path) -> Machine:
    """The machine the dispatcher reads each pass. A test replaces it with a fixed one."""
    return _read(root)


def holds(m: Machine) -> int:
    """How many workers the machine can hold now: the scarcest of its CPUs, its free memory
    and its free disk, each worker counted at its constants; at least 1."""
    limits = [m.cpus]
    if m.memory_mb is not None:
        limits.append(m.memory_mb // WORKER_MEMORY_MB)
    if m.disk_mb is not None:
        limits.append(m.disk_mb // per_worktree_mb(m))
    return max(1, min(limits))


def starved(m: Machine) -> str | None:
    """Why the machine is too low to keep its workers, or None."""
    if m.memory_mb is not None and m.memory_mb < LOW_MEMORY_MB:
        return f"free memory is {m.memory_mb} MB (under {LOW_MEMORY_MB})"
    if m.disk_mb is not None and m.disk_mb < LOW_DISK_MB:
        return f"free disk is {m.disk_mb} MB (under {LOW_DISK_MB})"
    if m.load is not None and m.load > m.cpus * LOAD_PER_CPU_HIGH:
        return f"the load average is {m.load:.1f} on {m.cpus} CPUs (over {LOAD_PER_CPU_HIGH:g} per CPU)"
    return None


def loaded(m: Machine) -> bool:
    """Whether the machine is busier than its CPUs carry: a check that timed out on it
    may have timed out for the load, not the code."""
    return m.load is not None and m.load > m.cpus * LOAD_PER_CPU_CLEAR


def clear(m: Machine) -> bool:
    """Whether the machine is clear with room to spare (`RECOVER_MARGIN`, `LOAD_PER_CPU_CLEAR`):
    a level it lowered may go back up."""
    return ((m.memory_mb is None or m.memory_mb >= LOW_MEMORY_MB * RECOVER_MARGIN)
            and (m.disk_mb is None or m.disk_mb >= LOW_DISK_MB * RECOVER_MARGIN)
            and (m.load is None or m.load <= m.cpus * LOAD_PER_CPU_CLEAR))


def _described(m: Machine) -> str:
    parts = [f"{m.cpus} CPUs"]
    if m.memory_mb is not None:
        parts.append(f"{m.memory_mb} MB free memory")
    if m.disk_mb is not None:
        parts.append(f"{m.disk_mb} MB free disk")
    if m.worktree_mb is not None and m.worktree_mb > WORKTREE_DISK_MB:
        parts.append(f"{m.worktree_mb} MB a worktree")
    if m.load is not None:
        parts.append(f"load {m.load:.1f}")
    return ", ".join(parts)


# --- the level -----------------------------------------------------------------------------


@dataclass
class Level:
    value: int
    #: Why it is where it is, as `locks lanes` says it after the number.
    reason: str
    #: When it last changed, and when it last halved for the machine's own state.
    at: str
    #: The last event read: what ended after it is read on the next pass.
    seen: int = 0
    #: Tasks the dispatcher started that ended with no limit since the last change.
    clean: int = 0
    #: No rise before this: a rate limit's retry-after.
    hold_until: str | None = None
    halved_at: str | None = None
    #: What the level was before the machine itself lowered it (memory, disk, load): it goes
    #: back there once the machine is clear. None when the machine lowered nothing.
    dropped_from: int | None = None
    #: Since when the machine has been clear while `dropped_from` waits.
    clear_since: str | None = None


def _path(root: Path) -> Path:
    return root / STATE


def load(root: Path) -> Level | None:
    """The level this machine keeps; None before the dispatcher first set it, or when unreadable."""
    try:
        data = json.loads(_path(root).read_text(encoding="utf-8"))
        return Level(**{k: data[k] for k in Level.__dataclass_fields__ if k in data})
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _save(root: Path, level: Level) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(asdict(level), indent=2), encoding="utf-8")
    tmp.replace(path)


def current(root: Path, *, ceiling: int | None = None) -> Level:
    """The level as kept, or where it would start on this machine; read only."""
    kept = load(root)
    if kept is not None:
        return kept
    start = holds(machine(root))
    return Level(start if ceiling is None else min(start, ceiling), "not set yet: what this machine holds",
                 _iso(datetime.now(UTC)))


@contextlib.contextmanager
def held(root: Path) -> Iterator[None]:
    """The machine's level is one, shared by every repository's dispatcher: each settles
    it, allots it and claims what it starts under this lock, so two never take one worker."""
    path = root / "work" / LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")


def _when(stamp: str | None) -> datetime | None:
    try:
        found = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _changed(store: Store, level: Level, task_id: int, before: int, reason: str, at: datetime,
             ceiling: int | None) -> None:
    level.reason, level.at, level.clean = reason, _iso(at), 0
    store.add_event(task_id, "level_changed", level=level.value, before=before, reason=reason, ceiling=ceiling)


def _dropped(level: Level, before: int) -> None:
    """The machine lowered the level from `before`: remember the highest it was."""
    level.dropped_from = max(level.dropped_from or 0, before) or None
    level.clear_since = None


def disk_filled(store: Store, root: Path, *, waited_s: float, ceiling: int | None,
                now: datetime | None = None) -> Level:
    """The dispatcher could not write for a full disk and waited `waited_s` for space:
    the level halves as on any full disk, recorded once the store takes writes again."""
    now = now or datetime.now(UTC)
    level = load(root) or current(root, ceiling=ceiling)
    before = level.value
    level.value, level.halved_at = max(1, level.value // 2), _iso(now)
    _dropped(level, before)
    _changed(store, level, 0, before, f"halved, the disk filled up (the dispatcher waited {int(waited_s)}s "
             "for space)", now, ceiling)
    _save(root, level)
    return level


def settle(store: Store, root: Path, *, machine: Machine, ceiling: int | None, wanting: bool,
           now: datetime | None = None) -> Level:
    """The level after what ended since the last pass, and what the machine has now.
    `wanting`: work waited for a worker on the last pass (a reason to rise). Read from
    the attempts' and endings' own records; cost is never read."""
    now = now or datetime.now(UTC)
    level = load(root)
    if level is None:
        start = holds(machine)
        level = Level(start if ceiling is None else min(start, ceiling), "", _iso(now), seen=store.last_event_id())
        _changed(store, level, 0, 0, f"started at what this machine holds ({_described(machine)})"
                 + (f", under your max_parallel {ceiling}" if ceiling is not None and ceiling < start else ""),
                 now, ceiling)
        _save(root, level)
        return level
    before, seen, kept = level.value, level.seen, asdict(level)
    limited: dict[str, Any] | None = None
    hungry: tuple[dict[str, Any], str] | None = None
    marked: set[int] = set()
    ended: list[dict[str, Any]] = []
    for event in store.capacity_events(level.seen):
        level.seen = event["id"]
        data = event["data"] or {}
        stop = data.get("stop") if isinstance(data.get("stop"), dict) else {}
        if event["kind"] == "attempt_finished" and data.get("limited"):
            limited = event
            marked.add(event["task_id"])
        elif data.get("starved") in STARVED or stop.get("starved") in STARVED:
            hungry = (event, str(data.get("starved") or stop.get("starved")))
            marked.add(event["task_id"])
        elif event["kind"] == "finished" and event.get("dispatched"):
            ended.append(event)
    low = starved(machine)
    # Read every pass, so a relapse resets the clear spell even while nothing moves.
    recovered = level.dropped_from is not None and _recovered(level, machine, now)
    if limited is not None:
        kind = limited["data"]["limited"]
        reason = {"rate_limit": "halved, the API answered 429",
                  "usage": "halved, the account's usage limit was reached"}.get(kind, "halved, the API was overloaded")
        at = _when(limited["ts"]) or now
        wait = limited["data"].get("retry_after_s")
        wait = float(wait) if isinstance(wait, int | float) and not isinstance(wait, bool) else RETRY_AFTER_S
        until = at + timedelta(seconds=wait)
        if level.hold_until is None or (_when(level.hold_until) or now) < until:
            level.hold_until = _iso(until)
        level.value = max(1, level.value // 2)
        _changed(store, level, int(limited["task_id"]), before, reason, at, ceiling)
    elif hungry is not None:
        event, what = hungry
        level.value = max(1, level.value // 2)
        if what != "memory":  # a disk frees and a load passes, and both can be read again; a worker
            _dropped(level, before)  # killed for memory needs more than the reading shows: back by rounds
        _changed(store, level, int(event["task_id"]), before, STARVED[what], _when(event["ts"]) or now, ceiling)
    elif low is not None and ((_when(level.halved_at) or datetime.min.replace(tzinfo=UTC))
                              + timedelta(seconds=HALVE_EVERY_S) <= now):
        level.value, level.halved_at = max(1, level.value // 2), _iso(now)
        _dropped(level, before)
        _changed(store, level, 0, before, f"halved, {low}", now, ceiling)
    elif recovered and wanting:
        back = min(level.dropped_from, holds(machine), *(() if ceiling is None else (ceiling,)))
        level.dropped_from, level.clear_since = None, None
        if back > level.value:
            level.value = back
            _changed(store, level, 0, before, f"rose back to {back}, the machine is clear again "
                     f"({_described(machine)})", now, ceiling)
    else:
        level.clean += sum(1 for e in ended if e["task_id"] not in marked)
        waited = level.hold_until is None or (_when(level.hold_until) or now) <= now
        if wanting and waited and low is None and level.clean >= level.value and (
                ceiling is None or level.value < ceiling):
            clean = level.clean
            level.value += 1
            last = ended[-1]["task_id"] if ended else 0
            _changed(store, level, int(last), before, f"rose, no limits in the last {clean} tasks", now, ceiling)
    if ceiling is not None and level.value > ceiling:
        level.value = ceiling
        _changed(store, level, 0, before, f"capped at your max_parallel {ceiling}", now, ceiling)
    if level.value != before or level.seen != seen or level.at == _iso(now) or asdict(level) != kept:
        _save(root, level)
    return level


def _recovered(level: Level, m: Machine, now: datetime) -> bool:
    """Whether a level the machine lowered may go back: the machine has been clear with room
    to spare for `RECOVER_AFTER_S`. Marks when it first was seen clear; a relapse resets it."""
    if not clear(m):
        level.clear_since = None
        return False
    since = _when(level.clear_since)
    if since is None:
        level.clear_since = _iso(now)
        return False
    return since + timedelta(seconds=RECOVER_AFTER_S) <= now


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        n = seconds // 60
        return f"{n} minute{'' if n == 1 else 's'} ago"
    n = seconds // 3600
    return f"{n} hour{'' if n == 1 else 's'} ago"


def line(level: Level, now: datetime | None = None) -> str:
    """`5: rose, no limits in the last 10 tasks`; a halving says how long ago it was."""
    text = f"{level.value}: {level.reason}"
    at = _when(level.at)
    if level.reason.startswith("halved") and at is not None:
        text += " " + _ago(((now or datetime.now(UTC)) - at).total_seconds())
    return text


# --- reserve and lending ---------------------------------------------------------------------


@dataclass(frozen=True)
class Lane:
    """A repository's lane as the allotment reads it."""
    repo: str | None
    #: Tasks the dispatcher runs there now.
    running: int
    #: It has queued work that may run (none held behind other work).
    waiting: bool
    #: The id of its oldest such work: reserves are granted in this order.
    oldest: int
    paused: bool = False


@dataclass(frozen=True)
class Share:
    repo: str | None
    reserve: int
    running: int
    #: What it starts on this pass.
    started: int = 0
    #: It has work to run (and is not paused on a person).
    waiting: bool = False

    @property
    def lent(self) -> int:
        return max(0, self.reserve - self.running - self.started)

    @property
    def borrowed(self) -> int:
        return max(0, self.running + self.started - self.reserve)

    def data(self) -> dict[str, Any]:
        return {"reserve": self.reserve, "running": self.running, "started": self.started, "lent": self.lent,
                "borrowed": self.borrowed}

    def text(self) -> str:
        loan = f" · lends {self.lent}" if self.lent else f" · borrows {self.borrowed}" if self.borrowed else ""
        return f"reserve {self.reserve} · runs {self.running}" + loan


def allot(level: int, lanes: Sequence[Lane], take: Callable[[str | None], bool]) -> dict[str | None, Share]:
    """Share `level` workers between the lanes: each lane with work fills its reserve
    first, in the order its oldest work was queued, then what is left is lent in that
    order to the lanes with more. `take(repo)` starts one more of a lane's tasks and
    says whether it had one. Nothing running is ever counted out to make room."""
    active = sorted((x for x in lanes if x.waiting and not x.paused), key=lambda x: x.oldest)
    each = max(1, level // len(active)) if active else 0
    reserve: dict[str | None, int] = {}
    left = level
    for x in active:
        reserve[x.repo] = min(each, left)
        left -= reserve[x.repo]
    free = max(0, level - sum(x.running for x in lanes))
    started: dict[str | None, int] = dict.fromkeys(reserve, 0)
    for x in active:  # each lane's own reserve first: a lent worker comes back here
        while free and x.running + started[x.repo] < reserve[x.repo] and take(x.repo):
            started[x.repo] += 1
            free -= 1
    for x in active:  # then what is left, lent in the same order
        while free and take(x.repo):
            started[x.repo] += 1
            free -= 1
    return {x.repo: Share(x.repo, reserve.get(x.repo, 0), x.running, started.get(x.repo, 0),
                          x.waiting and not x.paused) for x in lanes}


def full(level: int, shares: Mapping[str | None, Share]) -> bool:
    """Every worker is busy and some lane still has work waiting: a reason to rise, and for
    a lane's dispatcher to wait for a worker rather than stop."""
    busy = sum(s.running + s.started for s in shares.values())
    return busy >= level and any(s.waiting for s in shares.values())
