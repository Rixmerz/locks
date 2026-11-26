from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from locks import capabilities, cli, flow, hooks, loop_cli, loops, mcp_guard
from locks.store import Store, home

from .test_orchestrate import ok
from .test_workflows import _runner, _save

ROUND = {"name": "probe", "inputs": ["goal"], "steps": [
    {"id": "work", "prompt": "Work toward: {goal}", "kind": "explore"}]}


def _loop(cwd, **over) -> loops.Loop:
    raw = {"name": "leads", "goal": "find clients", "workflow": "probe", "cwd": str(cwd),
           "inputs": {"goal": "find clients"}, **over}
    loop = loops.normalize(raw)
    loops.save(loop)
    return loop


def test_a_loop_is_checked_and_kept_in_the_locks_home(tmp_path):
    with pytest.raises(loops.LoopError) as bad:
        loops.normalize({"name": "Bad Name", "direction": "sideways", "mode": "loud", "target": 3,
                         "hours": "21-8", "variants": ["only one"], "stale": 0})
    said = str(bad.value)
    for part in ("no loop name", "needs its goal", "direction is up or down", "mode is draft or act",
                 "a target needs a metric", "hours '21-8'", "one variant is no trial", "stale is at least 1"):
        assert part in said
    loop = _loop(tmp_path, policy=["only the catalog", " "], exclude=["a@b.c"])
    assert loop.mode == "draft" and loop.policy == ["only the catalog"]
    assert (home() / "loops" / "leads.json").is_file() and loops.names() == ["leads"]
    assert loops.load("leads") == loop
    with pytest.raises(loops.LoopError, match="exists"):
        loops.save(loop, new=True)
    with pytest.raises(loops.LoopError, match="no loop named 'nope'"):
        loops.load("nope")
    (home() / "loops" / "broken.json").write_text("{")
    with pytest.raises(loops.LoopError, match=r"broken\.json"):
        loops.load("broken")
    assert loops.state("leads") == {"status": "idle"}
    loops.set_state("leads", status="running")
    with pytest.raises(loops.LoopError, match="is running"):
        loops.remove("leads")
    loops.set_state("leads", status="stopped")
    loops.remove("leads")
    assert "leads" not in loops.names()


def test_durations_and_windows_read_as_a_person_writes_them():
    assert loops.duration("90") == 90 and loops.duration("30m") == 1800 and loops.duration("2h") == 7200
    assert loops.duration("1d") == 86400
    with pytest.raises(loops.LoopError):
        loops.duration("soon")
    now = datetime(2026, 10, 25, 22, 0).astimezone()
    assert loops.until_at("07:00", now).startswith("2026-10-26T07:00")
    assert loops.until_at("23:30", now).startswith("2026-10-25T23:30")
    assert loops.until_at("2026-10-27T06:00", now).startswith("2026-10-27T06:00")
    for wrong in ("25:00", "tomorrow"):
        with pytest.raises(loops.LoopError):
            loops.until_at(wrong, now)


def test_the_metric_is_the_last_number_its_command_prints(tmp_path):
    loop = _loop(tmp_path, metric="echo x")

    def runs(out, code=0):
        return lambda *a, **k: SimpleNamespace(returncode=code, stdout=out)

    assert loops.measure(loop, run=runs("leads 3 of 10\nreplied: 4\n")) == (4.0, "")
    assert loops.measure(loop, run=runs("none"))[1] == "the metric printed no number"
    assert loops.measure(loop, run=runs("1", 2))[1] == "the metric exited 2"
    assert loops.measure(loops.normalize({**loops.asdict(loop), "metric": ""}))[1] == "no metric"

    def broken(*a, **k):
        raise subprocess.TimeoutExpired("x", 1)

    assert "could not run" in loops.measure(loop, run=broken)[1]
    (tmp_path / "count").write_text("7\n")
    assert loops.measure(loops.normalize({**loops.asdict(loop), "metric": "cat count"})) == (7.0, "")


def test_strategies_alternate_until_each_has_its_trial_and_the_best_is_adopted(tmp_path):
    loop = _loop(tmp_path, metric="x", variants=["short message", "long message"], trial=2)
    assert loops.arm(loop, [], {}) == (0, "on trial (0 of 2 measured rounds)")
    rows = [{"round": 1, "arm": 0, "before": 0, "after": 1}]
    assert loops.arm(loop, rows, {})[0] == 1
    rows += [{"round": 2, "arm": 1, "before": 1, "after": 4}, {"round": 3, "arm": 0, "before": 4, "after": 4}]
    assert loops.judge(loop, rows) is None
    rows.append({"round": 4, "arm": 1, "before": 4, "after": 6})
    chosen, means = loops.judge(loop, rows)
    assert chosen == 1 and means == {0: 0.5, 1: 2.5}
    assert loops.arm(loop, rows, {"adopted": 1}) == (1, "adopted")
    assert loops.arm(_loop(tmp_path), rows, {}) == (None, "") and loops.judge(_loop(tmp_path), rows) is None
    # less is better: a fall is the gain
    down = _loop(tmp_path, metric="x", direction="down", variants=["a", "b"], trial=1)
    assert loops.judge(down, [{"arm": 0, "before": 5, "after": 3}, {"arm": 1, "before": 3, "after": 2}])[0] == 0


def test_a_loop_stops_on_evidence_never_on_money(tmp_path):
    now = datetime(2026, 10, 26, 8, 0).astimezone()
    loop = _loop(tmp_path, metric="x", target=10, stale=2, until="2026-10-26T07:00")
    assert "window ended" in loops.stop_reason(loop, [], now=now)
    loop = _loop(tmp_path, metric="x", target=10, stale=2)
    assert loops.stop_reason(loop, [{"round": 1, "before": 0}], now=now) == ""
    rows = [{"round": 1, "before": 0, "after": 3, "ended": "t", "cost_usd": 500}]
    assert loops.stop_reason(loop, rows, now=now) == ""
    assert "target was reached: 10" in loops.stop_reason(loop, [*rows, {"round": 2, "after": 10, "ended": "t"}],
                                                          now=now)
    flat = [*rows, {"round": 2, "after": 3, "ended": "t"}, {"round": 3, "after": None, "ended": "t"}]
    assert "2 round(s) in a row did not improve" in loops.stop_reason(loop, flat, now=now)
    person = [*rows, {"round": 2, "after": 4, "ended": "t", "person": "#9 permission"}]
    assert "round 2 waits on the person: #9 permission" in loops.stop_reason(loop, person, now=now)
    down = _loop(tmp_path, metric="x", direction="down", target=0)
    assert "target was reached" in loops.stop_reason(down, [{"round": 1, "after": 0, "ended": "t"}], now=now)


def test_a_round_is_told_its_goal_mode_rules_strategy_and_diary_as_data(tmp_path):
    loop = _loop(tmp_path, policy=["offer only the audit, USD 900"], variants=["short", "long"])
    rows = [{"round": n, "ended": "t", "state": "done", "before": n, "after": n + 1, "summary": f"did {n}"}
            for n in range(1, 11)]
    text = loops.brief(loop, rows, 11, 1)
    assert "round 11 of the loop 'leads'" in text and "find clients" in text
    assert "a draft: nothing leaves" in text and "- offer only the audit, USD 900" in text
    assert "This round runs the strategy: long" in text and loops.NEEDS in text
    assert "(2 earlier left out)" in text and "> round 10 (done, metric 10 → 11): did 10" in text
    assert "round 2 (" not in text
    loop.mode = "act"
    assert "may act, only within the person's rules" in loops.brief(loop, [], 1, None)


def test_a_loop_runs_rounds_until_its_metric_stops_moving(tmp_path, store: Store, kicks):
    folder = tmp_path / "leads"
    folder.mkdir()
    (folder / "count").write_text("1\n")
    _save(ROUND, folder)
    _loop(folder, metric="cat count", stale=2, policy=["only the catalog"])
    assert loops.tick(store, "leads") == "idle"
    loops.set_state("leads", status="running")
    now = datetime(2026, 10, 25, 23, 0).astimezone()
    assert loops.tick(store, "leads", now=now) == "started"
    row = loops.diary("leads")[0]
    assert row["round"] == 1 and row["before"] == 1.0
    task = store.run_tasks(row["run"])[0]
    assert "This is round 1 of the loop 'leads'" in task["body"] and "only the catalog" in task["body"]
    assert json.loads(task["options"])["loop"] == "leads"
    assert loops.tick(store, "leads", now=now) == "waiting"
    flow.work(store, parallel=False, runner=_runner([ok("found 3 leads\nNeeds the person:\n- a discount for ACME")],
                                                    []))
    (folder / "count").write_text("4\n")
    assert loops.tick(store, "leads", now=now) == "started"
    first, second = loops.diary("leads")
    assert first["after"] == 4.0 and first["state"] == "done" and first["needs"] == ["a discount for ACME"]
    assert second["before"] == 4.0
    assert "round 1 (done, metric 1 → 4): work: found 3 leads" in store.run_tasks(second["run"])[0]["body"]
    for _ in range(2):
        flow.work(store, parallel=False, runner=_runner([ok("nothing new")], []))
        loops.tick(store, "leads", now=now)
    assert loops.state("leads")["status"] == "stopped"
    assert "2 round(s) in a row did not improve" in loops.state("leads")["why"]
    told = loops.summary("leads")
    assert "metric: 1.0 → 4.0" in told and "round 1: a discount for ACME" in told and "stopped:" in told


def test_a_round_waits_its_turn_and_a_loop_s_ending_reaches_its_session_once(tmp_path, store: Store, kicks):
    folder = tmp_path / "f"
    folder.mkdir()
    _save(ROUND, folder)
    _loop(folder, every_s=3600, session_id="s1", until="2026-10-26T07:00")
    loops.set_state("leads", status="running")
    now = datetime(2026, 10, 25, 23, 0).astimezone()
    assert loops.tick(store, "leads", now=now) == "started"
    flow.work(store, parallel=False, runner=_runner([ok("did it")], []))
    assert loops.tick(store, "leads", now=now + timedelta(minutes=5)) == "waiting"
    assert loops.tick(store, "leads", now=now + timedelta(hours=9)) == "stopped"
    assert loops.endings("other") == [] and loops.endings(None) == []
    notice = hooks._endings(store, "s1", via="prompt")
    assert "A loop you started stopped" in notice and "window ended" in notice
    assert hooks._endings(store, "s1", via="prompt") is None


def test_the_outbound_guard_holds_acting_tools_to_the_loop(tmp_path):
    env = {"LOCKS_HOME": str(home()), "LOCKS_OUTBOUND_ACTS": "send_message,reply"}
    assert mcp_guard.outbound("search", {}, env) == (None, {})
    assert "only in a round of a loop" in mcp_guard.outbound("send_message", {}, env)[0]
    env["LOCKS_LOOP"] = "ghost"
    assert "cannot be read" in mcp_guard.outbound("send_message", {}, env)[0]
    loop = _loop(tmp_path, exclude=["boss@acme.com"], per_hour=2, hours="8-21")
    env["LOCKS_LOOP"] = "leads"
    assert "is a draft" in mcp_guard.outbound("send_message", {"to": "x@y.z"}, env)[0]
    loop.mode = "act"
    loops.save(loop)
    noon = datetime(2026, 10, 25, 12, 0).timestamp()
    night = datetime(2026, 10, 25, 23, 0).timestamp()
    assert "between 8:00 and 21:00" in mcp_guard.outbound("send_message", {}, env, now=night)[0]
    assert "excludes" in mcp_guard.outbound("reply", {"to": "Boss@Acme.com"}, env, now=noon)[0]
    assert mcp_guard.outbound("send_message", {"to": "a@b.c"}, env, now=noon)[0] is None
    assert mcp_guard.outbound("reply", {"to": "d@e.f"}, env, now=noon)[0] is None
    assert "at most 2 times an hour" in mcp_guard.outbound("send_message", {}, env, now=noon + 60)[0]
    assert mcp_guard.outbound("send_message", {}, env, now=noon + 3700)[0] is None
    sent = (home() / "loops" / "leads.sent.jsonl").read_text()
    assert "a@b.c" not in sent and sent.count("send_message") == 2


def test_a_capability_s_acting_tools_run_behind_the_guard(tmp_path, on_path):
    on_path("mail-mcp", "docs-mcp")
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps({"mail": {"server": {"command": "mail-mcp", "args": ["serve"]},
                                         "outbound": {"acts": ["send_message"]}},
                                "docs": {"server": {"command": "docs-mcp"}}}))
    registry = capabilities.load(path)
    server = registry["mail"].server
    assert server["args"][:4] == ["-m", "locks.mcp_guard", "outbound", "--"] and server["args"][4:] == [
        "mail-mcp", "serve"]
    assert server["env"]["LOCKS_OUTBOUND_ACTS"] == "send_message"
    chosen = capabilities.select(registry, "implement")
    told = capabilities.for_loop(chosen, "leads", home())
    assert told.servers["mail"]["env"]["LOCKS_LOOP"] == "leads" and "env" not in told.servers["docs"]
    assert "LOCKS_LOOP" not in capabilities.for_loop(chosen, None, home()).servers["mail"]["env"]
    for wrong in ({"acts": []}, "send", {"acts": [3]}):
        path.write_text(json.dumps({"mail": {"server": {"command": "m"}, "outbound": wrong}}))
        with pytest.raises(capabilities.RegistryError, match="outbound"):
            capabilities.load(path)
    path.write_text(json.dumps({"mail": {"server": {"url": "https://m"}, "outbound": {"acts": ["send"]}}}))
    with pytest.raises(capabilities.RegistryError, match="run by a `command`"):
        capabilities.load(path)


def test_the_cli_makes_shows_and_moves_a_loop(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "f"
    folder.mkdir()
    _save(ROUND, folder)
    _save({"name": "two", "inputs": ["goal", "list"], "steps": [{"id": "s", "prompt": "{goal} {list}"}]}, folder)
    monkeypatch.chdir(folder)
    assert cli.main(["loop"]) == 0 and "no loops yet" in capsys.readouterr().out
    assert cli.main(["loop", "add", "leads", "--goal", "find clients", "--workflow", "two"]) == 2
    assert "two needs list" in capsys.readouterr().out
    assert cli.main(["loop", "add", "leads", "--goal", "g", "--workflow", "probe", "--input", "oops"]) == 2
    assert cli.main(["loop", "add", "leads", "--goal", "find clients", "--workflow", "probe", "--every", "30m",
                     "--until", "07:00", "--metric", "echo 2", "--target", "9", "--variant", "a", "--variant",
                     "b", "--policy", "only the catalog", "--hours", "8-21", "--per-hour", "5",
                     "--exclude", "x@y.z"]) == 0
    assert "as a draft" in capsys.readouterr().out
    loop = loops.load("leads")
    assert loop.every_s == 1800 and loop.inputs == {"goal": "find clients"} and loop.until
    assert cli.main(["loop", "add", "leads", "--goal", "g", "--workflow", "probe"]) == 2
    assert cli.main(["loop", "add", "x", "--goal", "g", "--workflow", "nope"]) == 2
    capsys.readouterr()
    assert cli.main(["loop", "list"]) == 0 and "leads: idle (draft), 0 round(s)" in capsys.readouterr().out
    assert cli.main(["loop", "list", "--json"]) == 0 and json.loads(capsys.readouterr().out)[0]["name"] == "leads"
    assert cli.main(["loop", "act", "leads"]) == 0
    assert "at most 5 acting calls an hour, 1 exclusion" in capsys.readouterr().out
    assert loops.load("leads").mode == "act"
    assert cli.main(["loop", "draft", "leads"]) == 0 and loops.load("leads").mode == "draft"
    monkeypatch.setattr(loops, "launch", lambda name, env: 4242)
    assert cli.main(["loop", "start", "leads"]) == 0
    assert "starts as a draft" in (out := capsys.readouterr().out) and "pid 4242" in out
    assert cli.main(["loop", "show", "leads"]) == 0 and "strategies on trial: 2" in capsys.readouterr().out
    assert cli.main(["loop", "show", "leads", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["loop"]["name"] == "leads"
    assert cli.main(["loop", "stop", "leads"]) == 0 and loops.state("leads")["status"] == "stopped"
    assert cli.main(["loop", "show", "nope"]) == 2
    assert cli.main(["loop", "rm", "leads"]) == 0 and loops.names() == []
    # a worker starts no loop
    monkeypatch.setenv("LOCKS_WORKER_TASK", "7")
    assert cli.main(["loop", "start", "leads"]) != 0


def test_the_runner_starts_detached_once_and_looks_until_the_loop_stops(tmp_path, monkeypatch):
    _loop(tmp_path)
    started = []

    def popen(argv, **kw):
        started.append((argv, kw["start_new_session"]))
        return SimpleNamespace(pid=99)

    assert loops.launch("leads", {"LOCKS_HOME": str(home())}, popen=popen) == 99
    assert started[0][0][-2:] == ["loop-runner", "leads"] and started[0][1]
    assert not loops.alive(None) and not loops.alive("x") and loops.alive(os.getpid())
    monkeypatch.setattr(loops, "alive", lambda pid: True)
    with pytest.raises(loops.LoopError, match="running already"):
        loops.launch("leads", {"LOCKS_HOME": str(home())}, popen=popen)
    seen = iter(["started", "waiting", "stopped"])
    slept = []
    assert loops.run_forever("leads", sleep=slept.append, tick_fn=lambda store, name, env=None: next(seen)) == \
        "stopped"
    assert slept == [loops.POLL_S, loops.POLL_S]

    def broken(store, name, env=None):
        raise loops.LoopError("its workflow is gone")

    assert loops.run_forever("leads", sleep=slept.append, tick_fn=broken) == "stopped"
    assert loops.state("leads")["why"] == "its workflow is gone"
    assert loop_cli.cmd_loop_runner(SimpleNamespace(name="leads")) == 0
