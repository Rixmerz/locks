"""The machine's lock (`capacity.held`) settles, allots and claims: every repository's dispatcher
waits on it, so the model call that judges whether a task starts beside running work never runs
inside it (`flow.work`, `flow.decide_outside`)."""
from __future__ import annotations

import contextlib
import fcntl

import pytest

from locks import capacity, dispatch, flow
from locks.store import Store, home


def _lock_is_free() -> bool:
    """Whether another dispatcher could take the machine's lock right now: a second open file of the
    same lock file, asked without waiting (flock conflicts between open files, even in one process)."""
    path = home() / "work" / capacity.LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh, fcntl.LOCK_UN)
        return True


def _done(store: Store, started: list[int]):
    def start(task: dict, where) -> flow.Handle:
        started.append(task["id"])
        store.update_task(task["id"], status="done")
        return flow._Done(task["id"], "done")

    return start


@pytest.fixture
def entered(monkeypatch) -> list[int]:
    """How many times each pass took the machine's lock: one entry per `capacity.held`."""
    seen: list[int] = []
    real = capacity.held

    @contextlib.contextmanager
    def counted(root):
        with real(root):
            seen.append(1)
            yield

    monkeypatch.setattr(capacity, "held", counted)
    return seen


def _running(store: Store, text: str = "rewrite the importer") -> dict:
    running = store.enqueue(text, repo="r", cwd="/")
    assert store.claim(running["id"])
    return running


def test_the_model_is_never_asked_while_the_machine_s_lock_is_held(store: Store, tmp_path):
    _running(store)
    a = store.enqueue("add a flag to the exporter", repo="r", cwd=str(tmp_path))
    b = store.enqueue("add a page for the invoices", repo="r", cwd=str(tmp_path))
    free_when_asked: list[bool] = []

    def decide(task, ahead):
        free_when_asked.append(_lock_is_free())
        return dispatch.Decision(True, "independent")

    started: list[int] = []
    flow.work(store, parallel=True, decide=decide, start=_done(store, started), pause=lambda: None)
    assert started == [a["id"], b["id"]]
    assert free_when_asked == [True, True]
    assert store.get_task(a["id"])["parallel"] == 1 and store.last_event(a["id"], "dispatch_decided")


def test_the_probe_sees_the_lock_when_it_is_held(store: Store):
    # The probe above must tell: with the lock taken, it says so.
    with capacity.held(home()):
        assert _lock_is_free() is False
    assert _lock_is_free() is True


def test_a_pick_with_no_task_that_needs_the_model_asks_nothing_and_takes_one_pass(store: Store):
    _running(store)
    reader = store.enqueue("Where is the importer?", repo="r", cwd="/", options={"kind": "explore"})
    undecided: list[flow.Undecided] = []
    picked, _ = flow.pick(store, "r", parallel=True, decide=lambda t, a: pytest.fail("no model call"),
                          level=4, defer=undecided)
    assert [t["id"] for t in picked] == [reader["id"]] and undecided == []


def test_a_pick_hands_the_task_that_needs_the_model_back_and_stops_the_lane_there(store: Store):
    running = _running(store)
    needs = store.enqueue("add a flag to the exporter", repo="r", cwd="/")
    store.enqueue("Where is the importer?", repo="r", cwd="/", options={"kind": "explore"})
    undecided: list[flow.Undecided] = []
    picked, _ = flow.pick(store, "r", parallel=True, decide=lambda t, a: pytest.fail("no model call"),
                          level=4, defer=undecided)
    assert picked == [] and [u.task["id"] for u in undecided] == [needs["id"]]
    assert [t["id"] for t in undecided[0].writing] == [running["id"]]
    assert store.get_task(needs["id"])["parallel"] is None  # nothing is kept until it is decided


def test_work_that_ended_while_the_model_was_asked_is_read_again_before_the_task_starts(store: Store, tmp_path):
    running = _running(store)
    held = store.enqueue("add a flag to the exporter", repo="r", cwd=str(tmp_path))
    asked: list[int] = []

    def decide(task, ahead):
        asked.append(task["id"])
        store.update_task(running["id"], status="done")  # the work it waits for ends during the call
        return dispatch.Decision(False, f"may conflict with #{running['id']}")

    started: list[int] = []
    flow.work(store, parallel=True, decide=decide, start=_done(store, started), pause=lambda: None)
    # The wait it was given was judged against work that is gone: the lane is idle, so it starts.
    assert asked == [held["id"]] and started == [held["id"]]


def test_work_that_changed_files_while_the_model_was_asked_is_judged_again(store: Store, tmp_path, monkeypatch):
    running = _running(store, "build the course API")
    held = store.enqueue("add the student page", repo="r", cwd=str(tmp_path))
    changed: list[str] = []
    monkeypatch.setattr(flow, "with_changes", lambda store, t: {**t, "changed": list(changed)})
    monkeypatch.setattr(flow, "REJUDGE_S", 0.0)
    asked: list[list[str]] = []

    def decide(task, ahead):
        asked.append(list(ahead[0]["changed"]))
        if len(asked) == 1:
            changed.append("src/api/courses.ts")  # the running task wrote while the model thought
            return dispatch.Decision(False, f"unsure: #{running['id']} changed nothing yet")
        return dispatch.Decision(True, "the API changes no page")

    started: list[int] = []
    flow.work(store, parallel=True, decide=decide, start=_done(store, started), pause=lambda: None)
    assert asked == [[], ["src/api/courses.ts"]] and started == [held["id"]]
    assert store.last_event(held["id"], "dispatch_decided")["data"]["moved"] == [running["id"]]


def test_a_task_that_left_the_queue_while_the_model_was_asked_keeps_no_decision(store: Store):
    _running(store)
    task = store.enqueue("add a flag to the exporter", repo="r", cwd="/")
    undecided: list[flow.Undecided] = []
    flow.pick(store, "r", parallel=True, decide=lambda t, a: pytest.fail("no model call"), level=4, defer=undecided)

    def decide(t, ahead):
        store.claim(t["id"])  # another dispatcher took it
        return dispatch.Decision(True, "independent", 0.01)

    flow.decide_outside(store, undecided, decide)
    assert store.get_task(task["id"])["parallel"] is None and store.last_event(task["id"], "dispatch_decided") is None


def test_work_that_keeps_moving_under_the_judgments_is_bounded_and_left_for_the_next_pass(store: Store, tmp_path):
    ahead = [_running(store)]
    task = store.enqueue("add a flag to the exporter", repo="r", cwd=str(tmp_path))
    asked: list[int] = []

    def decide(t, writing):
        asked.append(t["id"])
        # The work it was judged against ends and other work takes its place, every time.
        store.update_task(ahead[-1]["id"], status="done")
        ahead.append(_running(store))
        return dispatch.Decision(False, "may conflict")

    class Stop(Exception):
        pass

    def pause():
        raise Stop

    started: list[int] = []
    with pytest.raises(Stop):  # the dispatcher goes on to another pass: it is not done with the task
        flow.work(store, parallel=True, decide=decide, start=_done(store, started), pause=pause)
    assert len(asked) == flow.DECIDE_ROUNDS and started == []
    assert store.get_task(task["id"])["status"] == "queued"


def test_a_tasks_that_needs_no_model_is_claimed_in_the_pass_that_reads_it(store: Store, entered):
    _running(store)
    reader = store.enqueue("Where is the importer?", repo="r", cwd="/", options={"kind": "explore"})
    flow.work(store, parallel=True, decide=lambda t, a: pytest.fail("no model call"),
              start=_done(store, started := []), pause=lambda: None)
    assert started == [reader["id"]]
    assert len(entered) == 2  # the pass that started it, and the one that found nothing left
