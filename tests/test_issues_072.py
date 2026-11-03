"""What 0.72 fixed from the field reports (issues #118, #119, #123, #124)."""
from __future__ import annotations

import subprocess
from datetime import timedelta
from pathlib import Path

from locks import capabilities, compaction, isolate, orchestrate, preflight, roles, route, surfaces
from locks.store import Store
from tests.test_compaction import NOW, ready


def test_a_container_runtime_that_is_not_up_could_not_run_the_check():
    for line in ("Sail is not running.", "Docker or Podman is not running.",
                 "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
                 'service "laravel.test" is not running', "no configuration file provided: not found"):
        assert orchestrate.cannot_run(1, f"something\n{line}\n") is not None
    assert orchestrate.cannot_run(1, "FAILED tests/test_x.py::test_y") is None


def test_surfaces_are_read_from_the_repository_inside_a_folder_and_without_a_leading_dot():
    assert surfaces.allows(["src", "db", "test"], "api/db/migrations/0001.sql")
    assert surfaces.allows(["./src/"], "src/app.ts")
    assert surfaces.outside(["src"], ["api/src/a.ts", "README.md"]) == ["README.md"]


def test_a_dependency_folder_is_never_a_surface_to_offer():
    assert surfaces.dependency("node_modules/jest/bin/jest.js")
    assert surfaces.dependency("vendor/laravel/sail/bin/sail")
    assert not surfaces.dependency("src/vendors.ts")


def test_composer_s_vendor_is_a_dependency_folder():
    assert "vendor" in isolate.DEPENDENCY_DIRS and isolate.LOCKFILES["vendor"] == ("composer.lock",)


def test_a_capability_about_ui_comes_after_a_failure_only_to_work_that_touches_ui():
    layout = capabilities.Capability("layout-inspector", {}, when=("ui",), after_failure=("feature",),
                                     touches=capabilities.UI_FILES, measures=True)
    assert not layout.applies("feature", failed_before=True, paths=["src/api/orders.ts"])
    assert layout.applies("feature", failed_before=True, paths=["components/Button.tsx"])
    assert layout.applies("feature", failed_before=True, paths=[])


def test_only_the_rules_of_languages_the_repository_holds_are_offered():
    held = frozenset({".ts", ".json"})
    assert roles.fits("typescript-rules", held) and not roles.fits("cpp-rules", held)
    assert roles.fits("engineering-baseline", held) and roles.fits("cpp-rules", None)


def test_languages_reads_what_git_tracks(git_repo: Path):
    (git_repo / "main.go").write_text("package main\n")
    subprocess.run(["git", "add", "main.go"], cwd=git_repo, check=True)
    assert ".go" in (roles.languages(git_repo) or set())
    assert roles.languages(git_repo / "nowhere") is None


def test_a_container_check_in_a_worktree_is_warned(tmp_path: Path):
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n")
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    result = preflight.Result()
    preflight._container("./vendor/bin/sail composer test", tmp_path, result)
    assert result.warnings and "--no-isolate" in result.warnings[0]
    quiet = preflight.Result()
    preflight._container("npm test", tmp_path, quiet)
    assert not quiet.warnings


def test_no_stack_is_stopped_without_a_compose_file(tmp_path: Path):
    assert isolate.stop_stacks(tmp_path, runner=lambda *a, **k: None) == []


def test_from_ninety_percent_compaction_is_now_whatever_holds_it_back():
    signals = ready(breaks=(), tokens=192_000, last_turn_at=NOW - timedelta(seconds=290))
    decided = compaction.decide(signals, NOW)
    assert decided.now and "from 90%" in " ".join(decided.because)
    assert not compaction.decide(ready(breaks=(), tokens=130_000), NOW).now


def test_a_request_that_leans_on_the_conversation_stays_with_the_session():
    chosen = route.choose("hotfix the null check in the order handler during the incident",
                          context_tokens=150_000, footprint=(270_000, 20))
    assert chosen.mode == route.MAIN and "leans on this conversation" in chosen.reasons[0]


def test_a_resume_hears_its_instruction_and_the_notes_filed_since(store: Store):
    task = store.create_task("build it", status="blocked", source="locks", repo="r", cwd="/tmp")
    assert orchestrate.resume_context(store, task, "r") == []
    store.add_attempt(task["id"], cell="sonnet/medium", max_turns=40, passed=0)
    store.add_note("r", "environment", "a local Postgres", "Postgres runs on localhost:5432, not in Docker",
                   author="person", filed_by="person")
    store.add_event(task["id"], orchestrate.RESUME_INSTRUCTION, text="use the local Postgres")
    said = "\n".join(orchestrate.resume_context(store, task, "r"))
    assert "use the local Postgres" in said and "localhost:5432" in said
