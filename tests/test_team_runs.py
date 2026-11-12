"""A run inside a team: the role its workers are, what they are told, and what
the classifier may pick."""
from __future__ import annotations

import json
from pathlib import Path

from locks import capabilities as caps
from locks import cli, launch, roles, teams
from locks.matrix import Cell
from locks.orchestrate import Options, run
from locks.store import home
from locks.ui import api

from .test_orchestrate import Script, kind, ok
from .test_roles import write_role
from .test_ui import call, ui  # noqa: F401  (the fixture)


def test_a_worker_is_told_its_role_and_carries_its_skills(git_repo, store):
    registry = {"browser": caps.Capability("browser", {"command": "pw"}, when=(), hint="open the page")}
    script = Script(ok())
    report = run("write tests for the parser", git_repo, store, registry=registry, launcher=script,
                 classifier=kind("test"))
    spec = script.specs[0]
    assert spec.append_system_prompt.startswith("You work as the `tester` role")
    assert "## Skill `ponytail`" in spec.append_system_prompt
    assert "## Skill `engineering-baseline`" not in spec.append_system_prompt  # writing roles no longer carry it
    assert "browser: open the page" in spec.append_system_prompt  # the role names it; its `when` does not
    assert set(spec.mcp_servers) == {"browser"} and "mcp__browser" in spec.allowed_tools
    assert f"Read(/{roles.bundled_skills_dir().resolve()}/**)" in spec.allowed_tools
    assert report.plan.role.name == "tester" and report.plan.team == "locks"
    assert "role tester (bundled) of the locks team: the role that takes test" in report.text()
    started = store.last_event(report.task_id, "attempt_started")["data"]
    assert started["role"] == "tester" and started["team"] == "locks" and "ponytail" in started["skills"]
    assert store.last_event(report.task_id, "planned")["data"]["role"] == "tester"


def test_a_project_s_team_limits_the_kinds_and_its_role_takes_tools_away(git_repo, store):
    write_role(home() / "roles", "careful", "Go slow.", description="careful changes", kinds=["implement"],
               disallowed_tools=["WebFetch"], skills=["ghost"], capabilities=["absent"])
    teams.create("narrow", ["careful"])
    teams.use(teams_key(git_repo), "narrow")
    script = Script(ok())
    report = run("explain the parser?", git_repo, store, registry={}, launcher=script,
                 classifier=kind("explore"))
    spec = script.specs[0]
    assert report.plan.kind == "implement" and "no role for" in report.plan.classification.reason
    assert spec.disallowed_tools == ("WebFetch",) and "Go slow." in spec.append_system_prompt
    reasons = " | ".join(report.plan.reasons)
    assert "the team chosen for this project (narrow)" in reasons
    assert "skill(s) ghost not found" in reasons and "capability absent is not in your registry" in reasons


def test_a_reading_role_still_loses_the_write_tools(git_repo, store):
    script = Script(ok())
    run("review the parser", git_repo, store, registry={}, launcher=script, classifier=kind("review-routine"))
    assert set(launch.WRITE_TOOLS) <= set(script.specs[0].disallowed_tools)


def test_a_role_a_person_names_is_the_only_one_and_survives_a_resume(git_repo, store):
    dry = run("x", git_repo, store, Options(dry_run=True, role="architect"), registry={},
              classifier=kind("implement"))
    assert dry.plan.kind == "plan" and dry.plan.role.name == "architect" and dry.plan.team == "named for this run"
    assert Options(role="architect").stored()["role"] == "architect"
    gone = run("x", git_repo, store, Options(dry_run=True, role="ghost"), registry={}, classifier=kind("docs"))
    assert gone.plan.role.name == "docs-writer" and any("no role 'ghost'" in r for r in gone.plan.reasons)


def test_a_team_left_with_no_role_falls_back_to_the_bundled_one(git_repo, store):
    write_role(home() / "roles", "temp", description="d", kinds=["docs"])
    teams.create("empty", ["temp"])
    teams.use(teams_key(git_repo), "empty")
    roles.remove("temp")
    write_role(home() / "roles", "broken", description="d", kinds=["nope"])
    dry = run("x", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"))
    reasons = " | ".join(dry.plan.reasons)
    assert dry.plan.team == "locks" and dry.plan.role.name == "implementer"
    assert "names role(s) that are not there: temp" in reasons and "has no role left" in reasons
    assert "roles refused: broken" in reasons


def test_a_critical_spec_does_not_upgrade_a_review_into_a_kind_the_team_lacks(git_repo, store):
    from locks.adapters import Briefing, Status

    class Critical:
        name = "spec"

        def inspect(self, repo_dir):
            return Status("spec", "present", True)

        def server(self):
            return None

        def brief(self, text, repo_dir, status):
            return Briefing(critical=True)

        def assess(self, *a):
            return []

    teams.create("reviews", ["reviewer"])
    teams.use(teams_key(git_repo), "reviews")
    dry = run("review it", git_repo, store, Options(dry_run=True), registry={}, adapters=[Critical()],
              classifier=kind("review-routine"))
    assert dry.plan.kind == "review-routine"


def teams_key(path):
    from locks import repo

    return repo.key(path)


def test_a_long_system_prompt_goes_in_a_file(tmp_path):
    seen = {}

    def runner(argv, **kwargs):
        i = argv.index("--append-system-prompt-file")
        seen["text"] = Path(argv[i + 1]).read_text()
        seen["argv"] = argv

        class P:
            returncode = 1
            stdout = ""
            stderr = "boom"
        return P()

    spec = launch.LaunchSpec("p", Cell("sonnet", "medium"), tmp_path, append_system_prompt="x" * 20000)
    launch.run(spec, runner=runner)
    assert seen["text"] == "x" * 20000 and "--append-system-prompt" not in seen["argv"]
    short = launch.build_argv(launch.LaunchSpec("p", Cell("sonnet", "medium"), tmp_path, append_system_prompt="y"))
    assert short[short.index("--append-system-prompt") + 1] == "y"


# --- the commands -----------------------------------------------------------------------


def test_roles_and_team_commands(capsys, git_repo):
    assert cli.main(["roles"]) == 0
    assert "security-auditor" in capsys.readouterr().out
    assert cli.main(["roles", "list", "--kind", "test", "--json"]) == 0
    assert [r["name"] for r in json.loads(capsys.readouterr().out)["roles"]] == ["tester"]
    assert cli.main(["roles", "show", "security-auditor"]) == 0
    out = capsys.readouterr().out
    assert "skills        security-audit" in out and "in teams      locks" in out
    assert cli.main(["roles", "show", "tester", "--full", "--json"]) == 0
    assert "## Skill `ponytail`" in json.loads(capsys.readouterr().out)["system_prompt"]
    assert cli.main(["roles", "copy", "reviewer", "strict", "--description", "no mercy"]) == 0
    assert "wrote" in capsys.readouterr().out
    assert cli.main(["roles", "copy", "reviewer", "reviewer"]) == 0
    assert "shadows the bundled role" in capsys.readouterr().out
    assert cli.main(["roles", "list"]) == 0
    assert "(shadows the bundled one)" in capsys.readouterr().out
    assert cli.main(["roles", "show", "reviewer"]) == 0
    assert "derived_from  reviewer" in capsys.readouterr().out
    assert cli.main(["roles", "skills"]) == 0
    assert "security-audit" in capsys.readouterr().out
    assert cli.main(["roles", "check"]) == 0
    assert "check out" in capsys.readouterr().out

    repo = str(git_repo)
    assert cli.main(["team", "create", "web", "--from", "locks", "--role", "strict", "--description", "web"]) == 0
    assert cli.main(["team", "change", "web", "--drop", "reviewer"]) == 0
    assert cli.main(["team", "use", "web", "--repo", repo]) == 0
    assert "uses the web team" in capsys.readouterr().out
    assert cli.main(["team", "--repo", repo]) == 0
    assert cli.main(["team", "show", "--repo", repo]) == 0
    out = capsys.readouterr().out
    assert "web: web" in out and "strict" in out and "used by" in out
    assert cli.main(["team", "show", "locks", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "locks"
    assert cli.main(["team", "list", "--repo", repo]) == 0
    assert "* web" in capsys.readouterr().out
    assert cli.main(["team", "list", "--json"]) == 0
    assert {t["name"] for t in json.loads(capsys.readouterr().out)["teams"]} == {"locks", "web"}
    assert cli.main(["team", "delete", "web"]) == 1
    assert "give them another first" in capsys.readouterr().err
    assert cli.main(["team", "use", "--none", "--repo", repo]) == 0
    assert cli.main(["team", "delete", "web"]) == 0
    assert cli.main(["roles", "rm", "strict"]) == 0
    assert "removed" in capsys.readouterr().out
    assert cli.main(["roles", "show", "ghost"]) == 1
    assert "no role 'ghost'" in capsys.readouterr().err


def test_roles_check_names_what_is_wrong(capsys):
    write_role(home() / "roles", "broken", description="d", kinds=["nope"])
    write_role(home() / "roles", "lacking", description="d", kinds=["docs"], skills=["ghost"])
    teams.create("t", ["lacking"])
    roles.remove("lacking")
    (teams.folder() / "bad.json").write_text("{")
    assert cli.main(["roles", "check"]) == 1
    out = capsys.readouterr().out
    assert "broken:" in out and "team bad:" in out and "team t: no role lacking" in out
    write_role(home() / "roles", "lacking", description="d", kinds=["docs"], skills=["ghost"])
    assert cli.main(["roles", "check"]) == 1
    assert "lacking: skill(s) not found: ghost" in capsys.readouterr().out
    assert cli.main(["team", "list"]) == 0
    assert "refused" in capsys.readouterr().out


def test_team_use_outside_a_directory_locks_can_key(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr("locks.team_cli._key", lambda args: None)
    assert cli.main(["team", "use", "locks"]) == 1
    assert "no directory" in capsys.readouterr().err


def test_run_with_a_role_that_is_not_there_says_so(capsys, git_repo):
    assert cli.main(["run", "x", "--role", "ghost", "--repo", str(git_repo), "--dry-run"]) == 2
    assert "no role 'ghost'" in capsys.readouterr().err
    assert cli.main(["route", "x", "--role", "architect", "--repo", str(git_repo), "--no-model"]) == 0
    assert "role architect" in capsys.readouterr().out


# --- the UI -----------------------------------------------------------------------------


def test_the_teams_view_shows_every_team_its_roles_and_who_uses_it(git_repo):
    write_role(home() / "roles", "loose", description="in no team", kinds=["docs"])
    teams.create("web", ["frontend", "reviewer"], description="web work")
    key = teams_key(git_repo)
    teams.use(key, "web")
    view = api.teams_view(key)
    assert view["here"] == "web" and view["assignments"] == {key: "web"}
    web = next(t for t in view["teams"] if t["name"] == "web")
    assert [r["name"] for r in web["roles"]] == ["frontend", "reviewer"] and web["projects"] == [key]
    loose = next(r for r in view["roles"] if r["name"] == "loose")
    assert loose["teams"] == [] and loose["scope"] == "user"
    auditor = next(r for r in view["roles"] if r["name"] == "security-auditor")
    assert "security-audit" in auditor["carried"] and auditor["teams"] == ["locks"]
    assert "security-audit" in view["skills"]
    assert api.teams_view()["here"] is None


def test_the_teams_route_reads(ui):  # noqa: F811
    res, data = call(ui, "GET", "/api/teams")
    assert res.status == 200 and any(t["name"] == "locks" for t in json.loads(data)["teams"])
    res, data = call(ui, "GET", "/static/teams.js")
    assert res.status == 200 and b"innerHTML" not in data

