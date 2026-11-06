from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from locks import preflight, stops


def _bin(folder: Path, *names: str, body: str = "exit 0") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        tool = folder / name
        tool.write_text(f"#!/bin/sh\n{body}\n")
        tool.chmod(0o755)
    return folder


@pytest.fixture
def env(tmp_path):
    """A PATH with only the shell's basics and the tools a test adds, and an empty nvm."""
    tools = tmp_path / "path"
    tools.mkdir()
    for name in ("sh", "cat", "grep", "tail"):
        found = shutil.which(name)
        if found:
            (tools / name).symlink_to(found)
    (tmp_path / "nvm").mkdir()
    return {"PATH": str(tools), "HOME": str(tmp_path), "NVM_DIR": str(tmp_path / "nvm")}


@pytest.fixture
def project(tmp_path):
    where = tmp_path / "project"
    where.mkdir()
    return where


def _package(where: Path, scripts: dict[str, str], **more) -> None:
    (where / "package.json").write_text(json.dumps({"name": "p", "scripts": scripts, **more}))


def test_a_program_missing_from_the_path_stops_with_the_line_that_fixes_it(project, env):
    _bin(Path(env["PATH"]), "python3")
    result = preflight.check("python -m ruff check .", project, env=env)
    assert not result.ok
    assert "`python` is not on the PATH" in result.blockers[0].what
    assert "python3" in result.blockers[0].fix
    assert "nothing ran, no attempt counted" in result.reason()


def test_a_tool_only_the_venv_has_is_named_by_its_path(project, env):
    _bin(project / ".venv" / "bin", "pytest")
    result = preflight.check("pytest -q", project, env=env)
    assert result.blockers[0].fix.startswith("write `.venv/bin/pytest`")


def test_a_program_on_the_path_and_shell_words_pass(project, env):
    _bin(Path(env["PATH"]), "ruff")
    assert preflight.check("set -o pipefail; cd . && ruff check . 2>&1 | tail -n 5 || exit 1", project,
                           env=env).ok


def test_a_missing_npm_script_stops_and_lists_the_ones_there_are(project, env):
    _bin(Path(env["PATH"]), "npm")
    _package(project, {"test": "exit 0", "check": "exit 0"})
    result = preflight.check("npm run build && npm test", project, env=env)
    assert [b.what.split(":")[0] for b in result.blockers] == ["`npm run build`"]
    assert "(it has: check, test)" in result.blockers[0].what


def test_npm_test_without_a_test_script_stops(project, env):
    _bin(Path(env["PATH"]), "npm")
    _package(project, {"lint": "exit 0"})
    assert "has no `test`" in preflight.check("npm test", project, env=env).blockers[0].what


def test_a_missing_deno_task_stops(project, env):
    _bin(Path(env["PATH"]), "deno")
    (project / "deno.json").write_text(json.dumps({"tasks": {"test": "deno test"}}))
    result = preflight.check("deno task check", project, env=env)
    assert "deno.json has no `check`" in result.blockers[0].what


def test_a_script_the_request_names_may_be_the_work_to_add(project, env):
    _bin(Path(env["PATH"]), "npm")
    _package(project, {"test": "exit 0"})
    result = preflight.check("npm run lint", project, asked="add a lint script with eslint", env=env)
    assert result.ok and "the request names it" in result.unknown[0]


def test_a_workspace_flag_or_a_cd_is_not_told(project, env):
    _bin(Path(env["PATH"]), "npm")
    _package(project, {})
    assert preflight.check("npm run build -w api", project, env=env).ok
    assert preflight.check("cd web && npm run build", project, env=env).ok


def test_a_script_body_runs_with_node_modules_bin_on_its_path(project, env):
    _bin(Path(env["PATH"]), "npm")
    _bin(project / "node_modules" / ".bin", "eslint")
    _package(project, {"lint": "eslint . && tsc"}, devDependencies={"typescript": "5"})
    result = preflight.check("npm run lint", project, env=env)
    # eslint is installed; tsc is not, and is a declared dependency's: said, never a stop
    assert result.ok


def test_a_script_body_tool_nobody_installs_stops(project, env):
    _bin(Path(env["PATH"]), "npm")
    _package(project, {"lint": "golangci-lint run"})
    assert "`golangci-lint` is not on the PATH" in preflight.check("npm run lint", project, env=env).blockers[0].what


def test_dependencies_the_worker_installs_are_no_stop(project, env):
    result = preflight.check(".venv/bin/python -m pytest", project, env=env, installing=True)
    assert result.ok
    result = preflight.check(".venv/bin/python -m pytest", project, env=env)
    assert "`uv sync`" in result.blockers[0].fix


def test_a_script_the_work_may_write_and_a_substitution_are_unknown(project, env):
    result = preflight.check("./run-tests.sh && $(which pytest)", project, env=env)
    assert result.ok and len(result.unknown) == 2


def test_a_node_nothing_installed_satisfies_stops_with_nvm_install(project, env):
    _bin(Path(env["PATH"]), "npm")
    _bin(Path(env["PATH"]), "node", body="echo v20.1.0")
    (project / ".nvmrc").write_text("22\n")
    _package(project, {"test": "exit 0"})
    result = preflight.check("npm test", project, env=env)
    assert result.blockers[0].fix == "`nvm install 22`"
    assert "the PATH's is 20.1.0" in result.blockers[0].what


def test_a_node_that_fits_on_the_path_or_one_nvm_has_passes(project, env, tmp_path):
    _bin(Path(env["PATH"]), "npm")
    _bin(Path(env["PATH"]), "node", body="echo v22.3.0")
    (project / ".nvmrc").write_text("22\n")
    _package(project, {"test": "exit 0"})
    assert preflight.check("npm test", project, env=env).ok
    _bin(tmp_path / "nvm" / "versions" / "node" / "v24.0.0" / "bin", "node", body="echo v24.0.0")
    (project / ".nvmrc").write_text("24\n")
    assert preflight.check("npm test", project, env=env).ok


def test_a_venv_that_imports_the_package_from_elsewhere_is_warned(project, env, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "mypkg").mkdir(parents=True)
    (elsewhere / "mypkg" / "__init__.py").write_text("")
    (project / "src" / "mypkg").mkdir(parents=True)
    (project / "pyproject.toml").write_text('[project]\nname = "mypkg"\n')
    venv = project / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text(f"#!/bin/sh\nPYTHONPATH={elsewhere} exec {sys.executable} \"$@\"\n")
    (venv / "python").chmod(0o755)
    result = preflight.check(".venv/bin/python -m pytest", project, env=env)
    assert result.ok and "imports `mypkg` from" in result.warnings[0]
    assert any(line.startswith("preflight: ") for line in result.plan_lines())


def test_an_empty_check_and_a_broken_preflight_say_nothing_stops(project, env, monkeypatch):
    assert preflight.check("  ", project, env=env).ok

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(preflight, "_programs", boom)
    result = preflight.check("missing-tool", project, env=env)
    assert result.ok and "preflight itself failed" in result.unknown[0]


def test_the_stop_has_its_cause_and_words():
    stop = stops.Stop("preflight", "the check cannot run here")
    assert stop.status == "blocked" and stop.who == "environment"
    assert "no attempt was counted" in stops.what_to_do(stop)
    assert stops.next_step(7, stop) == "locks resume 7"


def test_the_env_default_is_this_process(project):
    assert preflight.check("sh -c true", project).ok or os.environ.get("PATH") is None


def test_a_check_that_cannot_start_stops_the_task_before_any_worker(git_repo, store):
    from locks.orchestrate import Options, run

    from .test_orchestrate import Script, kind, ok

    script = Script(ok("fixed"))
    report = run("fix the total", git_repo, store, Options(verify="no-such-tool-anywhere --run"), registry={},
                 launcher=script, classifier=kind("implement"))
    assert script.specs == [] and store.attempts(report.task_id) == []
    assert report.status == "blocked" and report.stop.cause == "preflight"
    assert "`no-such-tool-anywhere` is not on the PATH" in report.stop.reason
    assert report.stop.extra["fixes"]
    [event] = store.events_of(report.task_id, "preflight")
    assert event["data"]["ok"] is False


def test_a_check_that_can_start_runs_the_worker(git_repo, store):
    from locks.orchestrate import Options, run

    from .test_orchestrate import Script, kind, ok

    script = Script(ok("fixed"))
    report = run("fix the total", git_repo, store, Options(verify="true"), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert len(script.specs) == 1 and report.status == "done"


def test_a_python_module_the_interpreter_lacks_stops(project, env):
    py = sys.executable
    result = preflight.check(f"{py} -m no_such_module_here -q", project, env=env)
    assert not result.ok
    assert "no module `no_such_module_here`" in result.reason()
    assert preflight.check(f"{py} -m json.tool --help", project, env=env).ok


def test_a_module_of_the_project_or_one_the_work_adds_is_no_stop(project, env):
    (project / "mine").mkdir()
    (project / "mine" / "__init__.py").write_text("")
    py = sys.executable
    assert preflight.check(f"{py} -m mine", project, env=env).ok
    named = preflight.check(f"{py} -m no_such_module_here", project, env=env, asked="add no_such_module_here")
    assert named.ok and named.unknown
    assert preflight.check(f"{py} -m no_such_module_here", project, env=env, installing=True).ok


def test_a_venv_that_has_the_module_is_named_in_the_fix(project, env):
    venv = project / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    result = preflight.check(f"{sys.executable} -m no_such_module_here", project, env=env)
    assert ".venv/bin/python -m no_such_module_here" in result.reason()
