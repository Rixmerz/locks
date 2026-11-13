from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from locks import capacity, cli, config, dispatch, flow, launch
from locks.capacity import Lane, Machine
from locks.store import Store, home

from .test_launch import envelope, proc, spec

ROOMY = Machine(cpus=8, memory_mb=64_000, disk_mb=500_000)
#: Events are stamped with the real time: a test's clock starts there.
NOW = datetime.now(UTC)


def _ended(store: Store, n: int, *, repo: str = "r", stop: dict | None = None) -> list[int]:
    """`n` tasks the dispatcher started that ended, as `finished` events."""
    ids = []
    for _ in range(n):
        task = store.enqueue("work", repo=repo, cwd="/x")
        store.claim(task["id"])
        store.update_task(task["id"], status="done" if stop is None else "interrupted")
        # A costly ending is as clean as a cheap one: money never moves the level.
        store.add_event(task["id"], "finished", status="done" if stop is None else "interrupted", stop=stop,
                        cost_usd=250.0)
        ids.append(task["id"])
    return ids


def _limited(store: Store, kind: str = "rate_limit", retry: float | None = None) -> int:
    task = store.enqueue("work", repo="r", cwd="/x")
    store.claim(task["id"])
    store.add_event(task["id"], "attempt_finished", seq=1, passed=False, limited=kind,
                    **({"retry_after_s": retry} if retry is not None else {}))
    return task["id"]


# --- the machine ---------------------------------------------------------------------


def test_what_the_machine_can_hold_is_read_from_the_stdlib_and_proc(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16000000 kB\nMemAvailable:    6291456 kB\n")
    assert capacity.available_mb(meminfo) == 6144
    assert capacity.available_mb(tmp_path / "nowhere") is None or isinstance(capacity.available_mb(tmp_path), int)
    found = capacity._read(tmp_path)  # the real reader: every test runs on a fake machine (conftest)
    assert found.cpus >= 1 and found.disk_mb is not None and found.disk_mb > 0
    # Each worker is counted at its memory constant; the scarcest of CPU, memory and disk decides; at least 1.
    assert capacity.holds(Machine(8, 3 * capacity.WORKER_MEMORY_MB, 500_000)) == 3
    assert capacity.holds(Machine(2, None, None)) == 2
    assert capacity.holds(Machine(8, 100, 10)) == 1
    assert capacity.starved(Machine(8, capacity.LOW_MEMORY_MB - 1, 500_000)).startswith("free memory is")
    assert capacity.starved(Machine(8, None, capacity.LOW_DISK_MB - 1)).startswith("free disk is")
    assert capacity.starved(ROOMY) is None


# --- the level -----------------------------------------------------------------------


def test_the_level_starts_at_what_the_machine_holds_and_a_ceiling_caps_it(store):
    level = capacity.settle(store, home(), machine=ROOMY, ceiling=None, wanting=False, now=NOW)
    assert level.value == 8 and level.reason.startswith("started at what this machine holds (8 CPUs")
    assert json.loads((home() / "capacity.json").read_text())["value"] == 8
    changed = store.events_of(0, "level_changed")
    assert [e["data"]["level"] for e in changed] == [8]
    # The person's ceiling caps it, and saying so is a change too.
    level = capacity.settle(store, home(), machine=ROOMY, ceiling=3, wanting=True, now=NOW)
    assert level.value == 3 and level.reason == "capped at your max_parallel 3"
    assert capacity.line(level, NOW) == "3: capped at your max_parallel 3"
    # Nothing new: nothing moves, and no event is written.
    capacity.settle(store, home(), machine=ROOMY, ceiling=3, wanting=True, now=NOW)
    assert len(store.events_of(0, "level_changed")) == 2


def test_the_level_rises_by_one_after_a_round_of_clean_endings_while_work_waits(store):
    capacity.settle(store, home(), machine=Machine(2, None, None), ceiling=None, wanting=False, now=NOW)
    _ended(store, 2)
    # Nothing waits for a worker: there is no reason to rise.
    assert capacity.settle(store, home(), machine=ROOMY, ceiling=None, wanting=False, now=NOW).value == 2
    level = capacity.settle(store, home(), machine=ROOMY, ceiling=None, wanting=True, now=NOW)
    assert level.value == 3 and capacity.line(level, NOW) == "3: rose, no limits in the last 2 tasks"
    # One round at the new level (three clean endings) before the next rise; with auto there is no ceiling.
    _ended(store, 2)
    assert capacity.settle(store, home(), machine=ROOMY, ceiling=None, wanting=True, now=NOW).value == 3
    _ended(store, 1)
    assert capacity.settle(store, home(), machine=ROOMY, ceiling=None, wanting=True, now=NOW).value == 4
    # A ceiling the person set holds it, though the endings were clean.
    _ended(store, 4)
    assert capacity.settle(store, home(), machine=ROOMY, ceiling=4, wanting=True, now=NOW).value == 4


def test_a_rate_limit_halves_the_level_and_it_waits_out_the_retry_after_before_rising(store):
    capacity.settle(store, home(), machine=ROOMY, ceiling=None, wanting=False, now=NOW)
    culprit = _limited(store, retry=600)
    level = capacity.settle(store, home(), machine=ROOMY, ceiling=None, wanting=True, now=NOW)
    assert level.value == 4 and level.reason == "halved, the API answered 429"
    at = datetime.fromisoformat(level.at)
    assert capacity.line(level, at + timedelta(minutes=3)) == "4: halved, the API answered 429 3 minutes ago"
    # The change is kept on the task whose attempt was limited.
    assert store.events_of(culprit, "level_changed")[0]["data"] == {
        "level": 4, "before": 8, "reason": "halved, the API answered 429", "ceiling": None}
    # Clean endings, but the retry-after has not passed: no rise.
    _ended(store, 6)
    later = datetime.now(UTC)
    assert capacity.settle(store, home(), machine=ROOMY, ceiling=None, wanting=True, now=later).value == 4
    assert capacity.settle(store, home(), machine=ROOMY, ceiling=None, wanting=True,
                           now=later + timedelta(minutes=11)).value == 5


def test_an_overloaded_api_a_starved_worker_and_a_low_machine_halve_it_never_below_one(store):
    capacity.settle(store, home(), machine=Machine(4, None, None), ceiling=None, wanting=False, now=NOW)
    _limited(store, "overloaded")
    assert capacity.settle(store, home(), machine=ROOMY, ceiling=None, wanting=False,
                           now=NOW).reason == "halved, the API was overloaded"
    _ended(store, 1, stop={"cause": "died", "reason": "killed", "starved": "memory"})
    level = capacity.settle(store, home(), machine=ROOMY, ceiling=None, wanting=False, now=NOW)
    assert level.value == 1 and level.reason == "halved, a worker was killed for memory"
    _ended(store, 1, stop={"cause": "environment", "reason": "No space left on device", "starved": "disk"})
    level = capacity.settle(store, home(), machine=ROOMY, ceiling=None, wanting=True, now=NOW)
    assert level.value == 1 and level.reason == "halved, the disk filled up"
    # The machine itself, low on memory: it halves, and not again until a while has passed.
    for _ in range(4):
        _ended(store, 1)
        capacity.settle(store, home(), machine=ROOMY, ceiling=None, wanting=True, now=NOW + timedelta(hours=1))
    low = Machine(8, capacity.LOW_MEMORY_MB - 10, 500_000)
    at = NOW + timedelta(hours=2)
    before = capacity.load(home()).value
    level = capacity.settle(store, home(), machine=low, ceiling=None, wanting=True, now=at)
    assert level.value == max(1, before // 2) and level.reason.startswith("halved, free memory is")
    again = capacity.settle(store, home(), machine=low, ceiling=None, wanting=True, now=at + timedelta(seconds=5))
    assert again.value == level.value


def test_the_cli_reads_a_rate_limit_from_its_own_result_never_from_a_check(tmp_path):
    limited = launch.parse(proc(envelope("API Error: 429 {\"type\":\"rate_limit_error\"} retry-after: 120",
                                         is_error=True, subtype="success")), spec(tmp_path))
    assert limited.limited == "rate_limit" and limited.retry_after_s == 120.0
    over = launch.parse(proc(envelope("API Error: 529 overloaded_error", is_error=True)), spec(tmp_path))
    assert over.limited == "overloaded" and over.retry_after_s is None
    no_json = launch.parse(proc("", code=1, stderr="Error: 429 Too Many Requests"), spec(tmp_path))
    assert no_json.limited == "rate_limit"
    # A worker's evidence (a test's log) that mentions a 429 is no limit of the API's.
    passed = launch.parse(proc(envelope("done\n```locks-result\n" + json.dumps(
        {"verdict": "pass", "summary": "s", "evidence": "test_429 ok: rate limit handled"}) + "\n```")),
        spec(tmp_path))
    assert passed.passed and passed.limited == ""
    killed = launch.parse(proc("", code=-9, stderr=""), spec(tmp_path))
    assert killed.starved == "memory" and "killed" in killed.summary
    full = launch.parse(proc("", code=1, stderr="ENOSPC: no space left on device, write"), spec(tmp_path))
    assert full.starved == "disk"


# --- reserve and lending ---------------------------------------------------------------


def _take(wants: dict[str, int]):
    left = dict(wants)

    def take(repo: str) -> bool:
        if left.get(repo, 0) > 0:
            left[repo] -= 1
            return True
        return False

    return take


def test_two_lanes_with_uneven_queues_share_by_reserve_and_lend_what_one_does_not_use():
    lanes = [Lane("a", running=0, waiting=True, oldest=1), Lane("b", running=0, waiting=True, oldest=2)]
    shares = capacity.allot(4, lanes, _take({"a": 5, "b": 1}))
    a, b = shares["a"], shares["b"]
    assert (a.reserve, b.reserve) == (2, 2)
    # b uses one of its two and lends the other: a borrows it.
    assert (a.started, b.started) == (3, 1) and (b.lent, a.borrowed) == (1, 1)
    # Reserves are granted in the order the lanes' oldest work was queued: one worker, two lanes.
    first = capacity.allot(1, [Lane("b", 0, True, oldest=9), Lane("a", 0, True, oldest=3)], _take({"a": 2, "b": 2}))
    assert (first["a"].reserve, first["b"].reserve, first["a"].started, first["b"].started) == (1, 0, 1, 0)


def test_a_lent_worker_comes_back_when_its_lane_has_work_and_the_borrowed_task_ends():
    # a borrowed b's worker while b had nothing; b now has work, and every worker is busy.
    busy = [Lane("a", running=4, waiting=True, oldest=1), Lane("b", running=0, waiting=True, oldest=5)]
    shares = capacity.allot(4, busy, _take({"a": 3, "b": 2}))
    assert shares["b"].started == 0 and shares["a"].borrowed == 2  # nothing running is stopped to give it back
    # One of a's tasks ends: the worker goes back to b first, though a has more queued.
    freed = [Lane("a", running=3, waiting=True, oldest=1), Lane("b", running=0, waiting=True, oldest=5)]
    shares = capacity.allot(4, freed, _take({"a": 3, "b": 2}))
    assert (shares["a"].started, shares["b"].started) == (0, 1)


def test_a_lane_that_empties_or_pauses_reserves_nothing():
    emptied = [Lane("a", running=1, waiting=True, oldest=1), Lane("b", running=0, waiting=False, oldest=0)]
    shares = capacity.allot(4, emptied, _take({"a": 5}))
    assert shares["b"].reserve == 0 and shares["a"].reserve == 4 and shares["a"].started == 3
    paused = [Lane("a", running=0, waiting=True, oldest=1), Lane("b", running=0, waiting=True, oldest=2, paused=True)]
    shares = capacity.allot(4, paused, _take({"a": 5, "b": 5}))
    assert shares["b"].reserve == 0 and shares["b"].started == 0 and shares["a"].started == 4
    # A paused lane's running work still holds its workers.
    held = [Lane("a", running=0, waiting=True, oldest=1), Lane("b", running=3, waiting=True, oldest=2, paused=True)]
    assert capacity.allot(4, held, _take({"a": 5}))["a"].started == 1


def test_the_dispatcher_shares_the_level_between_repositories(store):
    yes = lambda task, ahead: dispatch.Decision(True, "independent")  # noqa: E731
    for i in range(5):
        store.enqueue(f"read file a{i}", repo="a", cwd="/x")
    store.enqueue("read file b0", repo="b", cwd="/x")
    picked, shares = flow.pick(store, None, parallel=True, decide=yes, level=4)
    assert [t["repo"] for t in picked].count("a") == 3 and [t["repo"] for t in picked].count("b") == 1
    # A dispatcher of one repository starts only its own, within what the others reserve.
    picked, shares = flow.pick(store, "a", parallel=True, decide=yes, level=4)
    assert [t["repo"] for t in picked] == ["a", "a", "a"] and shares["b"].reserve == 2


def test_the_lanes_say_the_level_and_each_lane_s_reserve_use_and_loans(store, capsys):
    capacity.settle(store, home(), machine=Machine(4, None, None), ceiling=None, wanting=False, now=NOW)
    for i in range(4):
        task = store.enqueue(f"work a{i}", repo="a", cwd="/x")
        if i < 3:
            store.claim(task["id"])
    store.enqueue("work b", repo="b", cwd="/x")
    assert cli.main(["lanes"]) == 0
    out = capsys.readouterr().out
    assert "workers: 4: started at what this machine holds (4 CPUs" in out and "max_parallel auto" in out
    assert "a  1 queued  open · reserve 2 · runs 3 · borrows 1" in out
    assert "b  1 queued  open · reserve 2 · runs 0 · lends 2" in out
    from locks.ui import api

    board = api.board(store)
    assert board["capacity"]["level"] == 4 and board["capacity"]["lanes"]["a"]["borrowed"] == 1


def test_max_parallel_is_auto_or_a_ceiling_set_by_cli_or_plugin_option(capsys):
    env = dict(os.environ)
    assert config.max_parallel(env) is None and config.DEFAULTS["max_parallel"] == "auto"
    assert cli.main(["config", "max_parallel", "4"]) == 0 and config.max_parallel(env) == 4
    assert cli.main(["config", "max_parallel", "0"]) == 1
    assert config.ceiling_word("²") is None and config.ceiling_word("\u0663") is None
    assert "auto or a whole number from 1" in capsys.readouterr().err
    assert cli.main(["config", "max_parallel", "auto"]) == 0 and config.max_parallel(env) is None
    config.sync_plugin_options({**env, "CLAUDE_PLUGIN_OPTION_MAX_PARALLEL": "6"})
    assert config.max_parallel(env) == 6 and config.max_parallel({**env, "LOCKS_MAX_PARALLEL": "2"}) == 2
    assert config.max_parallel({**env, "LOCKS_MAX_PARALLEL": "lots"}) == 6


def test_the_dispatcher_settles_the_level_each_pass_and_starts_no_more_than_it(git_repo, store):
    key = str(git_repo.resolve())
    for text in ("a", "b", "c", "d"):
        store.enqueue(text, repo=key, cwd=str(git_repo))
    started, live = [], []

    class Slow:
        def __init__(self, task_id):
            self.task_id, self.left = task_id, 2

        def poll(self):
            live.append(len([h for h in started if store.get_task(h)["status"] == "running"]))
            self.left -= 1
            if self.left > 0:
                return None
            store.update_task(self.task_id, status="done")
            store.add_event(self.task_id, "finished", status="done")
            return "done"

    def start(task, where):
        started.append(task["id"])
        return Slow(task["id"])

    report = flow.work(store, parallel=True, start=start, pause=lambda: None, machine=lambda: Machine(2, None, None),
                       decide=lambda task, ahead: dispatch.Decision(True, "independent"))
    assert len(report.ran) == 4 and max(live) <= 2
    assert capacity.load(home()).value >= 2 and store.events_of(0, "level_changed")


@pytest.fixture(autouse=True)
def _no_ceiling(monkeypatch):
    monkeypatch.delenv("LOCKS_MAX_PARALLEL", raising=False)


# --- load, recovery and a full disk -------------------------------------------------------


def test_a_loaded_machine_halves_the_level_like_low_memory_and_a_calm_one_reads_nothing():
    calm = Machine(10, None, None, load=8.0)
    assert capacity.starved(calm) is None and not capacity.loaded(calm) and capacity.clear(calm)
    busy = Machine(10, None, None, load=12.0)
    assert capacity.starved(busy) is None and capacity.loaded(busy) and not capacity.clear(busy)
    hot = Machine(10, None, None, load=41.0)
    assert capacity.starved(hot).startswith("the load average is 41.0 on 10 CPUs")
    assert "load 41.0" in capacity._described(hot)
    read = capacity._read(home())
    assert read.load is None or read.load >= 0


def test_a_level_the_machine_lowered_goes_back_once_it_is_clear_without_waiting_for_tasks(store):
    capacity.settle(store, home(), machine=Machine(8, None, None), ceiling=None, wanting=False, now=NOW)
    full = Machine(8, None, capacity.LOW_DISK_MB - 1)
    level = capacity.settle(store, home(), machine=full, ceiling=None, wanting=True, now=NOW)
    assert level.value == 4 and level.dropped_from == 8
    level = capacity.settle(store, home(), machine=full, ceiling=None, wanting=True,
                            now=NOW + timedelta(seconds=capacity.HALVE_EVERY_S))
    assert level.value == 2 and level.dropped_from == 8
    # Freed, but only just over the line: no rise, no flapping.
    edge = Machine(8, None, capacity.LOW_DISK_MB + 1)
    at = NOW + timedelta(minutes=5)
    assert capacity.settle(store, home(), machine=edge, ceiling=None, wanting=True, now=at).value == 2
    roomy = Machine(8, None, 500_000)
    first = capacity.settle(store, home(), machine=roomy, ceiling=None, wanting=True, now=at)
    assert first.value == 2 and first.clear_since is not None  # clear from now on
    # A relapse resets the spell.
    hot = Machine(8, None, 500_000, load=13.0)
    assert capacity.settle(store, home(), machine=hot, ceiling=None, wanting=True,
                           now=at + timedelta(seconds=10)).clear_since is None
    capacity.settle(store, home(), machine=roomy, ceiling=None, wanting=True, now=at + timedelta(seconds=20))
    # Clear for long enough, but nothing waits: it stays where it is.
    later = at + timedelta(seconds=20 + capacity.RECOVER_AFTER_S)
    assert capacity.settle(store, home(), machine=roomy, ceiling=None, wanting=False, now=later).value == 2
    back = capacity.settle(store, home(), machine=roomy, ceiling=3, wanting=True, now=later)
    assert back.value == 3 and back.dropped_from is None  # back, under the person's ceiling
    assert back.reason.startswith("rose back to 3, the machine is clear again")
    assert store.events_of(0, "level_changed")[-1]["data"]["before"] == 2


def test_a_worker_killed_for_memory_climbs_back_by_rounds_not_by_a_reading(store):
    capacity.settle(store, home(), machine=Machine(4, None, None), ceiling=None, wanting=False, now=NOW)
    _ended(store, 1, stop={"cause": "died", "reason": "killed", "starved": "memory"})
    level = capacity.settle(store, home(), machine=Machine(4, None, None), ceiling=None, wanting=True, now=NOW)
    assert level.value == 2 and level.dropped_from is None
    task = store.enqueue("work", repo="r", cwd="/x")
    store.claim(task["id"])
    store.add_event(task["id"], "attempt_finished", seq=1, passed=False, starved="load")
    level = capacity.settle(store, home(), machine=Machine(4, None, None), ceiling=None, wanting=True, now=NOW)
    assert level.value == 1 and level.reason == "halved, a check's tests timed out under load"
    assert level.dropped_from == 2


def test_a_full_disk_halves_the_level_once_the_store_takes_writes_again(store):
    capacity.settle(store, home(), machine=Machine(4, None, None), ceiling=None, wanting=False, now=NOW)
    level = capacity.disk_filled(store, home(), waited_s=95.4, ceiling=None, now=NOW)
    assert level.value == 2 and level.dropped_from == 4
    assert level.reason == "halved, the disk filled up (the dispatcher waited 95s for space)"
    assert capacity.load(home()).value == 2


def test_max_parallel_says_it_is_a_ceiling_over_a_level_that_adapts(capsys):
    assert cli.main(["config", "max_parallel", "3"]) == 0
    out = capsys.readouterr().out
    assert "a ceiling only: the level under it already adapts to the API, memory, disk and load" in out
    assert cli.main(["config", "max_parallel", "auto"]) == 0
    assert "a ceiling only" not in capsys.readouterr().out


def test_a_worktree_is_counted_at_what_the_ones_on_this_disk_hold(tmp_path):
    assert capacity.worktree_mb(tmp_path, now=0.0) is None  # no worktree: nothing measured
    for n, mb in ((1, 1), (2, 3), (3, 5)):
        tree = tmp_path / "worktrees" / f"task-{n}"
        tree.mkdir(parents=True)
        (tree / "build.bin").write_bytes(b"x" * mb * capacity.MB)
    # The first reading is kept for `MEASURE_EVERY_S`; then the median of the trees is read.
    assert capacity.worktree_mb(tmp_path, now=1.0) is None
    measured = capacity.worktree_mb(tmp_path, now=capacity.MEASURE_EVERY_S + 1.0)
    assert measured == 3
    (tmp_path / "worktrees" / "task-2" / "more.bin").write_bytes(b"x" * 4 * capacity.MB)
    assert capacity.worktree_mb(tmp_path, now=capacity.MEASURE_EVERY_S + 2.0) == 3  # kept, not walked again
    # Never under the constant; a repository whose trees hold more is counted at what they hold.
    assert capacity.per_worktree_mb(Machine(8, None, 500_000, None, 3)) == capacity.WORKTREE_DISK_MB
    big = Machine(8, None, 7000, None, 3200)
    assert capacity.per_worktree_mb(big) == 3200 and capacity.holds(big) == 2
    assert capacity.holds(Machine(8, None, 7000)) == 6
    assert "3200 MB a worktree" in capacity._described(big)
