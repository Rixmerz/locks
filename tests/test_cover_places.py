"""Branches of the modules that read the machine, the checkout and the person's folders: capacity,
repo, project, workspaces, orchestrator, plugins, signature, surfaces, usage, trunks, trust, route,
evolution, insights and investigate."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from locks import (
    capacity,
    dispatch,
    evolution,
    insights,
    investigate,
    orchestrator,
    plugins,
    project,
    repo,
    roles,
    route,
    signature,
    standards,
    surfaces,
    trunks,
    trust,
    usage,
    workspaces,
)
from locks.capacity import Machine
from locks.store import Store, home

from .conftest import git
from .test_checking import BUG
from .test_trust import inspected
from .test_workspaces import checkout

NOW = datetime.now(UTC)


# --- capacity ------------------------------------------------------------------------------------


def test_memory_reads_none_when_neither_proc_nor_the_stdlib_can_say(tmp_path, monkeypatch):
    def no_sysconf(name):
        raise ValueError(name)

    monkeypatch.setattr(os, "sysconf", no_sysconf)
    assert capacity.available_mb(tmp_path / "no-meminfo") is None


def test_the_machine_reads_cpus_without_affinity_and_no_disk_where_no_folder_is(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-home"))
    found = capacity._read(tmp_path / "no-root")
    assert found.cpus == 1 and found.disk_mb is None


def test_a_second_limit_with_a_shorter_retry_keeps_the_longer_hold(store: Store):
    roomy = Machine(cpus=8, memory_mb=64_000, disk_mb=500_000)
    capacity.settle(store, home(), machine=roomy, ceiling=None, wanting=False, now=NOW)
    from .test_capacity import _limited

    _limited(store, retry=600)
    first = capacity.settle(store, home(), machine=roomy, ceiling=None, wanting=True, now=NOW)
    _limited(store, retry=10)
    second = capacity.settle(store, home(), machine=roomy, ceiling=None, wanting=True, now=NOW)
    assert second.value == first.value // 2
    assert second.hold_until == first.hold_until


def test_a_clear_machine_under_a_ceiling_at_the_level_clears_the_drop_without_a_rise(store: Store):
    capacity.settle(store, home(), machine=Machine(8, None, None), ceiling=None, wanting=False, now=NOW)
    full = Machine(8, None, capacity.LOW_DISK_MB - 1)
    level = capacity.settle(store, home(), machine=full, ceiling=None, wanting=True, now=NOW)
    assert (level.value, level.dropped_from) == (4, 8)
    roomy = Machine(8, None, 500_000)
    capacity.settle(store, home(), machine=roomy, ceiling=None, wanting=True, now=NOW)
    later = NOW + timedelta(seconds=capacity.RECOVER_AFTER_S + 1)
    changes = len(store.events_of(0, "level_changed"))
    level = capacity.settle(store, home(), machine=roomy, ceiling=4, wanting=True, now=later)
    assert level.value == 4 and level.dropped_from is None and level.clear_since is None
    assert level.reason.startswith("halved, free disk is")
    assert len(store.events_of(0, "level_changed")) == changes


def test_a_halving_says_just_now_or_how_many_hours_ago():
    at = datetime(2026, 1, 1, 12, tzinfo=UTC)
    level = capacity.Level(2, "halved, the API was overloaded", at.isoformat())
    assert capacity.line(level, at + timedelta(seconds=30)) == "2: halved, the API was overloaded just now"
    assert capacity.line(level, at + timedelta(hours=1)) == "2: halved, the API was overloaded 1 hour ago"
    assert capacity.line(level, at + timedelta(hours=5)) == "2: halved, the API was overloaded 5 hours ago"


# --- repo ----------------------------------------------------------------------------------------


def test_a_checkout_whose_git_folder_went_is_asked_again(git_repo: Path):
    assert repo.toplevel(git_repo) == git_repo.resolve()
    import shutil

    shutil.rmtree(git_repo / ".git")
    assert repo._still_top(git_repo, git_repo) is False
    assert repo._stamp(git_repo) == -1
    # A directory under no checkout walks to `/` without finding its top.
    other = git_repo.parent / "elsewhere"
    other.mkdir()
    (other / ".git").mkdir()
    assert repo._still_top(git_repo, other) is False


def test_own_key_is_none_for_nothing_and_for_a_file(tmp_path: Path):
    assert repo.own_key(None) is None
    (tmp_path / "f.txt").write_text("x")
    assert repo.own_key(tmp_path / "f.txt") is None
    assert repo.own_key(tmp_path) == str(tmp_path.resolve())


def test_a_worktree_of_a_checkout_outside_every_workspace_keeps_its_own_key(tmp_path: Path):
    folder = tmp_path / "shop"
    checkout(folder / "api")
    checkout(folder / "web")
    workspaces.add(folder)
    lone = checkout(tmp_path / "lone")
    tree = tmp_path / "lone-tree"
    git(lone, "worktree", "add", "-q", str(tree))
    assert repo.key(tree) == str(tree.resolve())


# --- project, workspaces and orchestrator --------------------------------------------------------


def test_own_is_none_without_a_directory():
    assert project.own(None) is None
    assert project.own("") is None


def test_taking_names_from_an_empty_folder_or_names_already_kept_brings_none(tmp_path: Path):
    source, folder = tmp_path / "a", tmp_path / "b"
    source.mkdir()
    folder.mkdir()
    assert project.take_names(source, folder) == 0
    project.set_haiku_name(source, "s1", "Cart", 1, "t")
    project.set_haiku_name(folder, "s1", "Kept here", 1, "t")
    assert project.take_names(source, folder) == 0
    assert project.names(folder)["s1"]["name"] == "Kept here"


def test_a_path_that_cannot_be_resolved_belongs_to_nothing(tmp_path: Path, monkeypatch):
    bad = tmp_path / "unreadable"
    config_dir = tmp_path / "claude-config"
    real = Path.resolve

    def resolve(self, strict=False):
        if self in (bad, config_dir):
            raise OSError("cannot resolve")
        return real(self, strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert workspaces.of(bad) is None
    assert orchestrator._same(str(bad), str(tmp_path)) is False
    # Claude Code's folder that cannot be resolved is skipped, never a crash: the project is found.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert project._tools_home(plain.resolve()) is False


def test_taking_the_role_in_a_folder_that_is_gone_holds_nothing(store: Store, tmp_path: Path):
    assert orchestrator.take(store, tmp_path / "gone" / ".locks", "s1", str(tmp_path), via="sent work") == (False, 0)


def test_workspace_lookups_with_nothing_or_a_bare_checkout(tmp_path: Path, git_repo: Path):
    assert workspaces.of(None) is None
    assert workspaces.checkouts_of(None) == []
    assert workspaces.checkouts_of(git_repo) == [("", git_repo.resolve())]


def test_a_member_lookup_skips_a_broken_worktree_and_an_empty_workspace(tmp_path: Path):
    shop = tmp_path / "shop"
    checkout(shop / "api")
    checkout(shop / "web")
    workspaces.add(shop)
    path = workspaces._file()
    data = json.loads(path.read_text())
    data["workspaces"].append({"folder": str(tmp_path / "empty"), "members": []})
    path.write_text(json.dumps(data))
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nowhere\n")
    assert workspaces.member(broken) is None
    assert workspaces.member(shop / "api")["name"] == "api"


def test_adding_skips_what_is_no_checkout_a_member_named_again_and_a_link_already_inside(tmp_path: Path):
    shop = tmp_path / "shop"
    checkout(shop / "api")
    outside = checkout(tmp_path / "web")
    shop.mkdir(exist_ok=True)
    (shop / "web").symlink_to(outside, target_is_directory=True)
    (shop / "notes.txt").write_text("a file, not a checkout")
    (shop / ".hidden").mkdir()
    entry = workspaces.add(shop, [outside, shop / "api"])
    assert sorted(m["name"] for m in entry["members"]) == ["api", "web"]
    assert not (shop / "notes.txt").is_symlink()
    again = workspaces.add(shop, [outside])
    assert sorted(m["name"] for m in again["members"]) == ["api", "web"]


# --- plugins, signature, surfaces ----------------------------------------------------------------


def test_filling_the_plugin_root_leaves_other_values_as_they_are(tmp_path: Path):
    filled = plugins._fill({"args": ["${CLAUDE_PLUGIN_ROOT}/x", 3], "on": True, "n": None}, tmp_path)
    assert filled == {"args": [f"{tmp_path}/x", 3], "on": True, "n": None}


def test_a_neighbour_found_on_the_path_has_no_skill_folder():
    found = plugins.Found(plugins.KNOWN[0], {"command": "x"}, "PATH")
    assert found.skill_dir is None


def test_a_two_word_program_with_only_options_is_named_by_itself():
    assert signature._bash_signature("git --version") == "bash:git"
    assert signature._bash_signature("npm") == "bash:npm"


def test_a_hook_error_that_cannot_be_written_is_lost_quietly(tmp_path: Path):
    blocker = tmp_path / "home-file"
    blocker.write_text("a file where the home folder should be")
    try:
        raise RuntimeError("the hook broke")
    except RuntimeError:
        signature.note_error({"LOCKS_HOME": str(blocker / "inner")})
    assert blocker.read_text() == "a file where the home folder should be"


def test_a_surface_written_with_dots_and_a_deep_glob_reads_as_meant():
    assert surfaces.check(["././src/", "./lib/a.py"]) == ("src/**", "lib/a.py")
    assert surfaces.allows(["**/test_*.py"], "test_cart.py")
    assert surfaces.allows(["**/test_*.py"], "deep/down/test_cart.py")
    assert not surfaces.allows(["**/test_*.py"], "deep/cart.py")


# --- usage and trunks ----------------------------------------------------------------------------


def test_a_last_line_too_long_to_read_whole_is_skipped(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(usage, "MAX_LINE", 10)
    path = tmp_path / "t.jsonl"
    path.write_bytes(b'{"a": 1}\n' + b"x" * 50)
    read = list(usage._records(path, 0))
    assert read[0] == (9, {"a": 1})
    assert read[1] == (59, None)


def test_a_transcript_skips_what_is_no_assistant_message_and_one_without_usage(tmp_path: Path):
    path = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "message": {"id": "u"}},
        {"type": "assistant", "message": {"id": "m0"}},
        {"type": "assistant", "message": {"model": "claude-sonnet", "usage": {"input_tokens": 1}}},
        {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet",
                                          "usage": {"input_tokens": 1_000_000}}},
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    assert usage.transcript_cost(path) == round(usage.MODELS["sonnet"].input_usd_per_mtok, 4)
    assert usage.transcript_turns(path) == 2  # m0 and m1: the one without an id is no turn


def test_a_transcript_that_cannot_be_read_costs_nothing(tmp_path: Path):
    folder = tmp_path / "a-folder.jsonl"
    folder.mkdir()
    assert usage.transcript_cost(folder) == 0.0
    assert usage.transcript_turns(folder) == 0


def test_a_trunk_that_runs_a_ledger_is_refused_and_a_shared_need_is_walked_once():
    steps = [
        {"id": "explore", "kind": "explore"},
        {"id": "scan", "kind": "explore", "ledger": "review", "needs": ["explore"]},
        {"id": "a", "kind": "implement", "needs": ["explore"]},
        {"id": "b", "kind": "implement", "needs": ["explore"]},
        {"id": "build", "kind": "implement", "needs": ["a", "b"], "trunk": "explore"},
        {"id": "late", "kind": "implement", "needs": ["scan"], "trunk": "scan"},
    ]
    ids = [s["id"] for s in steps]
    found = trunks.problems(steps, ids)
    assert found == ["step 'late': trunk names 'scan', which runs a ledger, parts or a join: a trunk only reads"]


def test_a_session_whose_transcript_cannot_be_read_has_no_known_context(tmp_path: Path, monkeypatch):
    config = tmp_path / "claude-config"
    (config / "projects" / "p" / "sess.jsonl").mkdir(parents=True)
    assert trunks.context_tokens("sess", {"CLAUDE_CONFIG_DIR": str(config)}) is None
    good = config / "projects" / "p" / "ok.jsonl"
    good.write_text("not json\n" + json.dumps({"type": "assistant", "message": {"usage": {}}}) + "\n"
                    + json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 7,
                                                                             "cache_read_input_tokens": 3}}}) + "\n")
    assert trunks.context_tokens("ok", {"CLAUDE_CONFIG_DIR": str(config)}) == 10


# --- trust ---------------------------------------------------------------------------------------


def test_a_cell_that_cannot_be_read_is_none_and_a_repeated_blocker_is_told_by_its_identity():
    assert trust._cell_of({"data": {"worker": "nonsense/cell"}, "attempt_cell": None}) is None
    assert trust._repeats(BUG, [BUG]) is True
    other = {"where": BUG["where"], "what": "an unrelated formatting nit", "evidence_class": "inferential",
             "cause": "introduced"}
    assert trust._repeats(BUG, [other]) is False


def test_a_late_check_is_no_re_check(store: Store):
    first = inspected(store, "blocked", blockers=[BUG])
    store.add_event(first, "verified", seq=2, round=1, late=True, passed=False, blocking=1, blockers=[BUG],
                    worker="haiku/high")
    assert trust._rechecks(store, "r") == {}


# --- route ---------------------------------------------------------------------------------------


def test_a_report_asked_after_an_order_is_the_order():
    assert route.reports("Please look at the logs. Give me a list of what fails.") is True
    assert route.reports("Refactor the logs module. Give me a list of what fails.") is False


def test_a_build_of_the_largest_size_is_several_tasks_and_a_top_level_file_is_no_piece():
    text = ("Rebuild the payment and auth system:\n- migrate the database schema in src/db/schema.py\n"
            "- rewrite src/billing/charge.py\n- fix src/auth/login.py security\n- concurrency in src/jobs/retry.py\n"
            "- src/ui/checkout.tsx\n- src/api/routes.py")
    assert route.split_first(text, "feature") == f"{max(route.points_mod.SCALE)} points"
    assert route.listed_pieces("- change app.py\n- change lib/x.py") == 1


# --- evolution -----------------------------------------------------------------------------------


def _change(**over):
    base = {"id": 1, "kind": "implement", "repo": "github.com/o/shop", "band": 3, "at": "2026-10-20T10:00:00+00:00",
            "field": "start_cell", "reverts": None, "new_value": "opus/high", "old_value": None,
            "rule": "the cheapest rung", "evidence": json.dumps({"cells": {"sonnet/high": [2, 8]}})}
    return {**base, **over}


def test_a_change_carried_from_a_repository_says_why_and_a_skip_waits_for_its_climbs():
    carried = {"verdict": "carried", "reason": "carried as #9 when shop joined the folder",
               "window_after": "{}", "at": "2026-10-21T10:00:00+00:00"}
    line = evolution.change_line(_change(), carried, [], UTC)
    assert line.endswith("; carried as #9 when shop joined the folder")
    skip = _change(field=standards.SKIP, band=standards.ANY_SIZE, new_value="sonnet/high>opus/high",
                   evidence=json.dumps({"from": "sonnet/high", "to": "sonnet/xhigh", "onto": "opus/high",
                                        "passed": 1, "climbs": 9, "move": "more_effort"}))
    line = evolution.change_line(skip, None, [], UTC)
    assert "climbs from sonnet/high skip sonnet/xhigh for opus/high" in line
    assert line.endswith(f"; judged after {standards.JUDGE_AFTER} climbs past it")


def test_a_standard_with_no_time_measured_says_a_dash():
    row = {"kind": "implement", "repo": "r", "band": 3, "start_cell": None, "start_turns": None,
           "measured": {"tasks": 0}}
    assert evolution._standard_line(row) == ("implement in r, 3 points: starts at locks' rules with the kind's "
                                             "turns; over 0 tasks pass-first —, — and — per point")


# --- insights and investigate --------------------------------------------------------------------


def test_a_typescript_project_names_its_tsconfig_not_its_package(git_repo: Path):
    (git_repo / "tsconfig.json").write_text("{}")
    (git_repo / "package.json").write_text("{}")
    (git_repo / "index.ts").write_text("export {}\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "ts")
    found = insights.languages(git_repo)
    assert "tsconfig.json" in found["manifests"] and "package.json" not in found["manifests"]


def test_a_stopped_attempt_without_a_failure_counts_none(store: Store):
    task = store.create_task("t", status="blocked", source="locks", repo="r", kind="implement")
    store.add_attempt(task["id"], cell="opus/high", max_turns=10, passed=0)
    store.add_attempt(task["id"], cell="opus/high", max_turns=10, passed=0, failure="code_bug")
    assert insights.work(store, "r")["failures"] == {"code_bug": 1}


def test_a_language_without_a_rules_skill_gets_no_suggestion_and_a_project_without_files_no_line(monkeypatch):
    monkeypatch.setattr(roles, "find_skill", lambda name: None)
    found = {"languages": {"by_share": [{"language": "python", "files": 4, "share": 1.0}], "manifests": []},
             "work": {"tasks": 0, "by_kind": []}, "checks": []}
    assert insights.suggestions(found) == []
    report = {"projects": [{"repo": "r", "dir": "", "team": "default", "work": {"tasks": 0, "by_kind": []},
                            "sessions": 0, "purpose": ["general"], "languages": {"by_share": []},
                            "suggestions": []}],
              "roles": [], "teams": [], "claude_agents": []}
    text = insights.text(report)
    assert "languages:" not in text
    assert text.startswith("r (no checkout here): team default, 0 tasks")


def test_an_investigation_starts_its_lane_even_with_autowork_off(monkeypatch, tmp_path: Path):
    calls = []
    monkeypatch.setattr(dispatch, "start", lambda cwd, root, scope: calls.append(("start", scope)) or False)
    monkeypatch.setattr(dispatch, "held", lambda root, scope: calls.append(("held", scope)) or True)
    assert investigate._kick(str(tmp_path), None) is True
    assert calls == [("start", "*"), ("held", "*")]


def test_a_task_with_unreadable_options_is_no_investigation():
    assert investigate._of({"options": "{not json"}) is None
    assert investigate._of({"options": json.dumps({"investigation": "inv-1"})}) == "inv-1"


@pytest.fixture(autouse=True)
def _fresh_caches():
    repo._TOPS.clear()
    repo._KEYS.clear()
    workspaces._cache.clear()
    yield
    repo._TOPS.clear()
    repo._KEYS.clear()
    workspaces._cache.clear()


def test_a_disk_with_no_room_for_one_more_worktree_is_low_and_a_run_beside_work_says_so(
        store: Store, monkeypatch, capsys):
    from locks import cli, flow

    assert not flow.low_disk(Machine(8, None, None)) and not flow.low_disk(Machine(8, None, 500_000))
    assert flow.low_disk(Machine(8, None, capacity.LOW_DISK_MB - 1))
    tight = Machine(8, None, 2500, None, 3200)  # over the constant, under what a tree here holds
    assert flow.low_disk(tight)
    monkeypatch.setattr(capacity, "machine", lambda root=None: tight)
    cli._warn_disk(store)
    assert capsys.readouterr().err == ""  # nothing runs: nothing to fill the disk beside
    store.create_task("build the app", status="running", source="cli", repo="r")
    cli._warn_disk(store)
    said = capsys.readouterr().err
    assert "2500 MB" in said and "about 3200 MB" in said and "1 task(s) running" in said
