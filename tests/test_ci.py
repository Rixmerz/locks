"""What a repository's own checks run: its scripts, whether they drive a browser, and its CI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from locks import ci

from .conftest import git

WORKFLOW = """\
# CI: the suite in memory, against Postgres, and the browser tests.
name: CI
on:
  pull_request:
  push:
env:
  DENO_VERSION: 2.9.7
jobs:
  verify:
    name: Check and build
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - run: deno install --frozen-lockfile
      - run: deno task check  # fmt, lint and the suite
      - run: deno task build
  postgres:
    runs-on: ubuntu-24.04
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_HOST_AUTH_METHOD: trust
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-retries 12
    steps:
      - uses: actions/checkout@v4
      - name: Make a key
        run: |
          key="$(openssl rand -base64 48)"

          echo "KEY=${key}" >> "${GITHUB_ENV}"
      - name: Run the suite against the service
        env:
          DATABASE_URL: postgres://postgres@localhost:5432/app_test
          APP_TRUNCATE: "1"
        run: deno test -A tests/
  e2e:
    needs: verify
    runs-on: ubuntu-24.04
    steps:
    - run: deno task e2e
  deploy:
    runs-on: ubuntu-24.04
    steps:
      - run: ./deploy.sh
"""


@pytest.fixture
def project(git_repo: Path) -> Path:
    (git_repo / "deno.json").write_text(json.dumps({
        "tasks": {"check": "deno fmt --check . && deno lint . && deno task test",
                  "test": "APP_DB=memory deno test -A tests/", "build": "vite build",
                  "e2e": {"command": "deno test -A e2e/", "dependencies": ["build"]}},
        "imports": {"postgres": "npm:postgres@3.4.7"}}))
    (git_repo / "db.ts").write_text('const url = Deno.env.get("DATABASE_URL");\n'
                                    'const truncate = Deno.env.get("APP_TRUNCATE");\n')
    (git_repo / ".github" / "workflows").mkdir(parents=True)
    (git_repo / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW)
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "ci")
    return git_repo


# --- the line reader ---------------------------------------------------------------------


def test_the_line_reader_takes_the_shapes_a_workflow_is_written_in():
    doc = ci.read(WORKFLOW)
    assert doc["name"] == "CI" and doc["on"] == {"pull_request": None, "push": None}
    assert doc["jobs"]["verify"]["steps"][2] == {"run": "deno task check"}
    service = doc["jobs"]["postgres"]["services"]["postgres"]
    assert service["image"] == "postgres:16" and service["ports"] == ["5432:5432"]
    assert service["options"].startswith('--health-cmd "pg_isready -U postgres"')
    key = doc["jobs"]["postgres"]["steps"][1]["run"]
    assert key.splitlines()[0] == 'key="$(openssl rand -base64 48)"' and key.splitlines()[-1].startswith("echo")
    assert doc["jobs"]["postgres"]["steps"][2]["env"]["APP_TRUNCATE"] == "1"
    assert doc["jobs"]["e2e"]["steps"] == [{"run": "deno task e2e"}]
    assert ci.read("a: 'it''s'\nb: [1, 2]\nc: \"x # y\"\n") == {"a": "it's", "b": "[1, 2]", "c": "x # y"}


@pytest.mark.parametrize("text", ["jobs:\n  a: b\n c: d\n", "just a line\n", "jobs: [\n"])
def test_a_shape_it_does_not_take_is_unreadable_not_a_guess(text):
    with pytest.raises(ci.Unreadable):
        doc = ci.read(text)
        if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
            raise ci.Unreadable("no jobs")


# --- scripts and the browser ---------------------------------------------------------------


def test_a_command_runs_the_scripts_it_names(project):
    ran = ci.runs("deno task check", project)
    assert ran[0] == "deno task check" and "APP_DB=memory deno test -A tests/" in ran
    # a task's dependencies run before it
    assert ci.runs("deno task e2e", project) == ["deno task e2e", "deno task build && deno test -A e2e/",
                                                 "vite build"]
    (project / "package.json").write_text(json.dumps({"scripts": {"test": "vitest", "e2e": "npm run pw",
                                                                  "pw": "playwright test", "loop": "npm run loop"}}))
    assert ci.runs("npm test", project) == ["npm test", "vitest"]
    assert ci.runs("npm run e2e", project)[-1] == "playwright test"
    assert ci.runs("npm run loop", project) == ["npm run loop", "npm run loop"]  # a cycle ends
    assert ci.runs("npm ci && npm run nothing", project) == ["npm ci && npm run nothing"]


def test_a_check_that_runs_playwright_drives_the_browser(project):
    assert ci.browser(project, None) is None and ci.browser(project, "deno task check") is None
    assert "playwright" in ci.browser(project, "npx playwright test --project chromium")
    (project / "deno.json").write_text(json.dumps({"tasks": {"e2e": "npx playwright test"}}))
    assert ci.browser(project, "deno task e2e") == "its check runs playwright (`deno task e2e`)"


@pytest.mark.parametrize(("files", "why"), [
    ({"package.json": {"devDependencies": {"@playwright/test": "1.56.1"}}}, "@playwright/test in package.json"),
    ({"deno.json": {"imports": {"pw": "npm:playwright-core@1.56.1"}}}, "playwright-core in deno.json"),
    ({"deno.lock": {"specifiers": {"npm:playwright@1.56.1": "1.56.1"}}}, "playwright in deno.lock"),
    ({"package.json": {"scripts": {"test:e2e": "playwright test"}}}, "its test:e2e script runs playwright"),
])
def test_the_project_s_own_tests_drive_a_headless_browser(git_repo, files, why):
    for name, content in files.items():
        (git_repo / name).write_text(json.dumps(content))
    assert ci.browser(git_repo, None) == f"the project's own tests drive a headless browser ({why})"


def test_a_deno_jsonc_with_comments_is_read(git_repo):
    (git_repo / "deno.jsonc").write_text('{\n  // the tasks\n  "tasks": {"e2e": "playwright test", /* x */},\n}\n')
    assert ci.scripts(git_repo) == {"e2e": "playwright test"}


# --- what CI runs and a check leaves out ------------------------------------------------------


def test_ci_jobs_carry_their_commands_services_and_env(project):
    jobs, unreadable = ci.jobs(project)
    assert unreadable == []
    by = {j.name: j for j in jobs}
    assert by["postgres"].services == ("postgres",)
    assert by["postgres"].env == ("DENO_VERSION", "DATABASE_URL", "APP_TRUNCATE")
    assert by["postgres"].commands[-1] == "deno test -A tests/"
    assert by["verify"].commands == ("deno install --frozen-lockfile", "deno task check", "deno task build")


def test_a_check_that_covers_less_than_ci_names_the_jobs_it_leaves_out(project, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_TRUNCATE", raising=False)
    note, unreadable = ci.left_out(project, "deno task check")
    assert unreadable == []
    # the suite runs, but not the build, not against the database, and the browser tests not at all; a
    # deploy is no check; DENO_VERSION is CI's own, no code reads it
    assert note == ("CI also runs: verify (`deno task build`); postgres (a postgres service, DATABASE_URL, "
                    "APP_TRUNCATE); e2e (`deno task e2e`), which this check does not" + ci.QUIET_HINT)
    covering = "DATABASE_URL=postgres://localhost/app APP_TRUNCATE=1 deno task check && deno task e2e"
    assert ci.left_out(project, covering) == (None, [])
    # an env the check runs with is set; a service the task starts is up
    monkeypatch.setenv("DATABASE_URL", "postgres://localhost/app")
    monkeypatch.setenv("APP_TRUNCATE", "1")
    assert ci.left_out(project, "deno task check && deno task e2e", services=("pg_ctl start@5432",))[0] == (
        "CI also runs: postgres (a postgres service), which this check does not" + ci.QUIET_HINT)
    assert ci.left_out(project, "deno task check && deno task e2e", services=("postgres -D data@5432",)) == (None, [])


def test_a_job_is_covered_only_when_the_check_runs_every_command_it_checks_with(git_repo):
    (git_repo / ".github" / "workflows").mkdir(parents=True)
    (git_repo / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n      - run: npm ci\n"
        "      - run: npx --yes playwright-core@1.56.1 install chromium\n      - run: |\n"
        "          KEY=\"$(openssl rand -hex 8)\"\n          echo \"KEY=$KEY\" >> \"$GITHUB_ENV\"\n"
        "      - run: npm run lint\n      - run: npm test\n      - run: npm run build\n"
        "  setup:\n    steps:\n      - run: npm ci\n")
    # one command of three is no cover, and the note names the ones left out; installs and the shell's
    # bookkeeping are no check, so a job that only readies itself is none either
    assert ci.left_out(git_repo, "npm run lint")[0] == (
        "CI also runs: test (`npm test`, `npm run build`), which this check does not" + ci.QUIET_HINT)
    assert ci.left_out(git_repo, "npm test")[0] == (
        "CI also runs: test (`npm run lint`, `npm run build`), which this check does not" + ci.QUIET_HINT)
    assert ci.left_out(git_repo, "npm run lint && npm test && npm run build") == (None, [])
    # a command continued on the next line is one command, and printing a tool's version checks nothing
    (git_repo / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - run: |\n          python -m pytest \\\n            -q\n"
        "          node --version\n          npx tsc --noEmit\n")
    assert ci.left_out(git_repo, "python -m pytest -q && npx tsc --noEmit") == (None, [])
    assert ci.left_out(git_repo, "npx tsc --noEmit")[0] == (
        "CI also runs: test (`python -m pytest -q`), which this check does not" + ci.QUIET_HINT)
    # a check behind a `cd` or an `export` is still a check, and `-v` is verbose, not a version
    (git_repo / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - run: npm ci\n      - run: cd api && npm test\n"
        "      - run: pip install -e .\n      - run: pytest -v\n      - run: export CI=1 && ruff check .\n")
    assert ci.left_out(git_repo, "npm run lint")[0] == (
        "CI also runs: test (`npm test`, `pytest -v`, `ruff check .`), which this check does not" + ci.QUIET_HINT)
    assert ci.left_out(git_repo, "cd api && npm test && pytest -v && ruff check .") == (None, [])


def test_an_unreadable_workflow_is_skipped_and_said(project):
    (project / ".github" / "workflows" / "broken.yaml").write_text("jobs:\n  a: b\n c: d\n")
    jobs, unreadable = ci.jobs(project)
    assert unreadable == [".github/workflows/broken.yaml"] and {j.name for j in jobs} >= {"postgres"}
    (project / ".github" / "workflows" / "ci.yml").unlink()
    assert ci.left_out(project, "deno task check") == (None, [".github/workflows/broken.yaml"])


def test_no_ci_says_nothing(git_repo, tmp_path):
    assert ci.jobs(git_repo) == ([], []) and ci.left_out(git_repo, "npm test") == (None, [])
    assert ci.jobs(tmp_path / "nowhere") == ([], [])


def test_a_check_that_runs_a_tool_its_script_runs_with_more_is_said(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {
        "test": "ng test --code-coverage --watch=false", "lint": "eslint src", "build": "ng build"}}))
    said = ci.bypassed("npx ng test --watch=false", tmp_path)
    assert said.startswith("this task's check `npx ng test --watch=false` runs `ng` itself, while `npm test` runs "
                           "`ng test --code-coverage --watch=false`: what it adds (--code-coverage) is not checked")
    assert "`--verify 'npm test'`" in said
    # through the script, the same command, another tool, or no project: nothing to say
    assert ci.bypassed("npm test", tmp_path) is None
    assert ci.bypassed("ng test --code-coverage --watch=false", tmp_path) is None
    assert ci.bypassed("pytest -q", tmp_path) is None
    assert ci.bypassed("ng build", tmp_path) is None
    assert ci.bypassed("npx ng test", None) is None
    assert "`npm run lint`" in ci.bypassed("CI=1 npx eslint", tmp_path)


def test_the_runner_a_check_uses_gives_the_command_for_the_tests_near_a_change(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest --coverage", "e2e": "vitest"}}))
    assert ci.related("npm test", tmp_path) == ("jest", "npx jest --findRelatedTests {files}")
    assert ci.related("npm run e2e", tmp_path)[0] == "vitest"
    assert ci.related("python3 -m pytest -q", None)[0] == "pytest"
    assert ci.related("cargo test", None) is None


def test_a_job_quieted_here_is_named_in_no_plan(project, monkeypatch, capsys):
    from locks import cli, repo
    from locks.store import Store

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_TRUNCATE", raising=False)
    monkeypatch.chdir(project)
    assert cli.main(["ci", "quiet", "e2e", "postgres"]) == 0
    assert "quiet here: e2e, postgres" in capsys.readouterr().out
    store = Store.open()
    try:
        quieted = ci.quiet(store, repo.key(project))
    finally:
        store.close()
    note, _ = ci.left_out(project, "deno task check", quieted=quieted)
    assert note == "CI also runs: verify (`deno task build`), which this check does not" + ci.QUIET_HINT
    assert cli.main(["ci"]) == 0
    listed = capsys.readouterr().out
    assert "e2e  (quiet)" in listed and "verify\n" in listed + "\n"
    assert cli.main(["ci", "speak", "postgres"]) == 0 and "quiet here: e2e" in capsys.readouterr().out


def test_a_workflow_that_runs_on_no_change_is_no_job_of_ci(project):
    (project / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (project / ".github" / "workflows" / "pin.yml").write_text(
        "name: pin\non:\n  schedule:\n    - cron: '0 6 * * 1'\n  workflow_dispatch:\njobs:\n  pin:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: python scripts/pin.py\n")
    (project / ".github" / "workflows" / "release.yml").write_text(
        "name: release\non:\n  workflow_run:\n    workflows: [ci]\n    types: [completed]\njobs:\n  tag:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: git tag v1\n")
    (project / ".github" / "workflows" / "ci.yml").write_text(
        "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: npm test\n")
    found, unreadable = ci.jobs(project)
    assert [(j.name, j.commands) for j in found] == [("test", ("npm test",))] and unreadable == []
    assert ci.checks_changes({"on": "push"}) and ci.checks_changes({"on": ["schedule", "pull_request"]})
    assert not ci.checks_changes({"on": {"schedule": [], "workflow_dispatch": None}})
    assert ci.checks_changes({"jobs": {}})  # no readable trigger: taken as a check of a change
