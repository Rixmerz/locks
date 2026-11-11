from __future__ import annotations

from pathlib import Path

import pytest

from locks import roles, teams
from locks.classify import Classification, by_model, classify, narrow
from locks.matrix import KINDS
from locks.store import home
from tests.conftest import git


def write_role(folder: Path, name: str, body: str = "Do the work.", **fields) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}"]
    for key, value in fields.items():
        lines.append(f"{key}: [{', '.join(value)}]" if isinstance(value, list | tuple) else f"{key}: {value}")
    lines += ["---", body, ""]
    path = folder / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def user_roles() -> Path:
    return home() / "roles"


# --- the bundled team ----------------------------------------------------------------


def test_the_bundled_team_covers_every_kind_and_every_skill_it_names_ships():
    catalog = roles.load()
    assert not catalog.refused
    assert set(roles.kinds_of(catalog.roles.values())) == set(roles.ROLE_KINDS)
    assert "classify" not in roles.ROLE_KINDS and set(roles.ROLE_KINDS) | {"classify"} == set(KINDS)
    for role in catalog.roles.values():
        briefing = roles.brief(role)
        assert not briefing.missing, role.name
        assert role.description and role.prompt and role.scope == "bundled"
        # A role says how its findings reach locks: the result block, never a format of its own.
        assert "evidence" in role.prompt or "summary" in role.prompt, role.name


def test_the_security_auditor_carries_cloudflare_s_skill_and_its_license():
    role = roles.load().get("security-auditor")
    assert role.kinds == ("review-critical",) and "security-audit" in role.skills
    skill = roles.find_skill("security-audit")
    assert skill is not None and skill.origin == "bundled"
    assert "Cloudflare" in (skill.folder / "LICENSE").read_text()
    briefing = roles.brief(role)
    assert "## Skill `security-audit`" in briefing.system_prompt and "HUNTING.md" in briefing.system_prompt


def test_ponytail_is_carried_by_every_writing_role_and_a_prototype_is_functional_work():
    catalog = roles.load()
    for name in ("implementer", "feature-builder", "refactorer", "debugger", "tester", "frontend"):
        assert "ponytail" in catalog.get(name).skills, name
    frontend = catalog.get("frontend")
    assert "A prototype is functional work" in frontend.prompt and "ponytail: prototype styles" in frontend.prompt
    # a design that exists is applied in full; only a non-prototype without one stops for a brief
    assert "apply them in full" in frontend.prompt and "that is not a\n  prototype" in frontend.prompt
    system = roles.brief(frontend).system_prompt
    assert "unless the request is for a prototype" in system


def test_every_vendored_skill_is_credited():
    readme = (roles.bundled_skills_dir() / "README.md").read_text()
    for skill in roles.bundled_skills():
        assert f"`{skill.name}`" in readme, skill.name
    assert (roles.bundled_skills_dir() / "LICENSE-vise").is_file()
    credits = (roles.bundled_dir() / "CREDITS.txt").read_text()
    for name in roles.load().roles:
        assert name in credits


# --- reading ---------------------------------------------------------------------------


def test_frontmatter_reads_inline_and_block_lists_and_folded_text():
    meta, body = roles.split_frontmatter(
        "---\nname: x\ndescription: one\n  two\nkinds:\n  - implement\n  - test\nskills: [a, 'b']\n"
        "replace_prompt: true\n# a comment\n---\nbody\n")
    assert meta == {"name": "x", "description": "one two", "kinds": ["implement", "test"], "skills": ["a", "b"],
                    "replace_prompt": True}
    assert body == "body"
    assert roles.split_frontmatter("no frontmatter") == ({}, "no frontmatter")
    with pytest.raises(roles.RoleError, match="not closed"):
        roles.split_frontmatter("---\nname: x\n")
    with pytest.raises(roles.RoleError, match="not understood"):
        roles.split_frontmatter("---\n!!\n---\n")
    meta, _ = roles.split_frontmatter("---\nkinds:\nname: >\n---\n")
    assert meta == {"kinds": "", "name": ""}


@pytest.mark.parametrize(("fields", "why"), [
    ({"description": "d", "kinds": ["implement"], "model": "opus"}, "unknown field"),
    ({"description": "d", "kinds": ["nope"]}, "no kind"),
    ({"description": "d", "kinds": ["classify"]}, "no kind"),
    ({"description": "d", "kinds": ["implement"], "skills": ["Bad Name"]}, "no skill name"),
    ({"description": "d", "kinds": ["implement"], "disallowed_tools": ["mcp__x__y"]}, "no built-in tool"),
    ({"description": "d", "kinds": ["implement"], "capabilities": ["Bad"]}, "no capability name"),
    ({"description": "d", "kinds": ["implement"], "match": ["../up"]}, "inside the project"),
    ({"description": "d", "kinds": ["implement"], "replace_prompt": "maybe"}, "true or false"),
    ({"description": "d", "kinds": ["implement"], "extends": "x"}, "itself"),
    ({"kinds": ["implement"]}, "`description` is required"),
    ({"description": "d"}, "`kinds` is required"),
    ({"description": "d", "kinds": ["implement"], "extends": "ghost"}, "no role"),
])
def test_a_role_that_fails_a_check_is_refused_with_its_reason_and_costs_only_itself(fields, why):
    write_role(user_roles(), "x", **fields)
    catalog = roles.load()
    assert why in catalog.refused["x"] and "implementer" in catalog.roles
    with pytest.raises(roles.RoleError, match="refused"):
        catalog.get("x")


def test_a_role_file_needs_its_own_name_and_a_good_one():
    folder = user_roles()
    folder.mkdir(parents=True)
    (folder / "a.md").write_text("---\nname: b\ndescription: d\nkinds: [implement]\n---\n")
    (folder / "c.md").write_text("---\ndescription: d\n---\n")
    (folder / "e.md").write_text("---\nname: e\nkinds: implement\ndescription: [x]\n---\n")
    (folder / "f.md").write_text("---\nname: f\ndescription: d\nkinds: [implement]\nskills: [1, ]\n---\n")
    refused = roles.load().refused
    assert "file is a.md" in refused["a"] and "`name` is required" in refused["c"]
    assert "`description` is text" in refused["e"]
    with pytest.raises(roles.RoleError, match="no role 'ghost'"):
        roles.load().get("ghost")


def test_a_list_given_as_one_word_or_nothing_is_still_a_list():
    write_role(user_roles(), "solo", description="d", kinds="implement", skills="")
    role = roles.load().get("solo")
    assert role.kinds == ("implement",) and role.skills == ()
    write_role(user_roles(), "odd", description="d", kinds="implement", skills="true")
    assert "list of names" in roles.load().refused["odd"]


def test_a_person_s_role_of_a_bundled_name_shadows_it_and_says_so():
    write_role(user_roles(), "reviewer", "Be kind.", description="mine", kinds=["review-routine"])
    catalog = roles.load()
    mine = catalog.get("reviewer")
    assert mine.scope == "user" and mine.prompt == "Be kind." and mine.shadows.endswith("reviewer.md")
    assert "reviewer" in [r.name for r in catalog.bundled()]
    assert mine.summary()["shadows"] == mine.shadows


def test_extends_builds_on_a_parent_and_replace_prompt_starts_over():
    write_role(user_roles(), "base", "Parent says.", description="base", kinds=["implement"], skills=["ponytail"])
    write_role(user_roles(), "child", "Child adds.", extends="base", skills=["engineering-baseline"])
    write_role(user_roles(), "fresh", "Only me.", extends="base", replace_prompt="true", description="fresh")
    write_role(user_roles(), "quiet", "", extends="base")
    catalog = roles.load()
    child = catalog.get("child")
    assert child.prompt == "Parent says.\n\nChild adds." and child.description == "base"
    assert child.kinds == ("implement",) and child.skills == ("engineering-baseline",) and child.extends == "base"
    assert catalog.get("fresh").prompt == "Only me." and catalog.get("quiet").prompt == "Parent says."


def test_extends_that_goes_round_or_too_deep_is_refused():
    write_role(user_roles(), "a", extends="b", description="d", kinds=["implement"])
    write_role(user_roles(), "b", extends="a", description="d", kinds=["implement"])
    for i in range(8):
        write_role(user_roles(), f"d{i}", extends=f"d{i + 1}", description="d", kinds=["implement"])
    write_role(user_roles(), "d8", description="d", kinds=["implement"])
    refused = roles.load().refused
    assert "circle" in refused["a"] and "deep" in refused["d0"]


def test_an_unreadable_folder_is_no_role_and_no_error(tmp_path):
    catalog = roles.load(folders={"user": tmp_path / "none", "bundled": tmp_path / "none"})
    assert catalog.roles == {} and catalog.refused == {}
    assert catalog.bundled() == []


# --- picking ---------------------------------------------------------------------------


def test_a_specialization_the_project_matches_wins_and_the_team_order_breaks_ties(git_repo):
    (git_repo / "pyproject.toml").write_text("[project]\n")
    (git_repo / "pkg").mkdir()
    (git_repo / "pkg" / "lib.rs").write_text("fn main() {}\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "files")
    base = roles.Role("base", "d", ("implement",), "p")
    other = roles.Role("other", "d", ("implement",), "p")
    py = roles.Role("py", "d", ("implement",), "p", match=("pyproject.toml",))
    rs = roles.Role("rs", "d", ("implement",), "p", match=("**/*.rs", "Cargo.toml"))
    go = roles.Role("go", "d", ("implement",), "p", match=("go.mod",))
    assert roles.pick("implement", [base, go], git_repo)[0] is base
    picked, why = roles.pick("implement", [base, py, rs], git_repo)
    assert picked is py and "matches this project" in why
    assert roles.pick("implement", [base, other], git_repo) == (base, "first of 2 that take implement")
    assert roles.pick("implement", [go], git_repo)[0] is go
    assert roles.pick("test", [base], git_repo) == (None, "no role takes test")
    assert roles.matches(rs, git_repo) == 1


def test_project_files_outside_a_checkout_are_a_bounded_walk(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.py").write_text("")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "y.py").write_text("")
    assert roles.project_files(tmp_path) == ["a/x.py"]
    monkeypatch.setattr(roles, "MAX_MATCH_FILES", 1)
    (tmp_path / "b.py").write_text("")
    assert len(roles.project_files(tmp_path)) == 1


# --- skills ---------------------------------------------------------------------------


def test_skills_are_found_nearest_first_and_in_installed_plugins(tmp_path, monkeypatch):
    mine = home() / "skills" / "ponytail"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: ponytail\ndescription: mine\n---\nMy ponytail.\n")
    found = roles.find_skill("ponytail")
    assert found.origin == "user" and found.body == "My ponytail."
    claude = Path(roles.claude_dir())
    for version in ("1.2.0", "1.10.0"):
        folder = claude / "plugins" / "cache" / "market" / "layout-inspector" / version / "skills" / "layout-inspection"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\ndescription: v{version}\n---\nmeasure\n")
    clone = claude / "plugins" / "cache" / "temp_github_1" / "layout-inspector" / "9.0.0" / "skills"
    clone = clone / "layout-inspection"
    clone.mkdir(parents=True)
    (clone / "SKILL.md").write_text("---\ndescription: half a clone\n---\nx\n")
    plugin = roles.find_skill("layout-inspector:layout-inspection")
    assert plugin.origin == "plugin" and plugin.description == "v1.10.0"
    personal = claude / "skills" / "mine"
    personal.mkdir(parents=True)
    (personal / "SKILL.md").write_text("---\nbroken\n---\n")
    assert roles.find_skill("mine") is None and roles.find_skill("nowhere") is None
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert roles.claude_dir({"HOME": str(tmp_path)}) == tmp_path / ".claude"


def test_an_installed_upstream_plugin_s_skill_takes_the_vendored_copy_s_place_and_its_version():
    role = roles.Role("r", "does r", ("implement",), "Be r.", skills=("ponytail",))
    vendored = roles.version(role)
    assert roles.find_skill("ponytail").origin == "bundled"
    assert roles.brief(role).sources == {"ponytail": "bundled"}
    cache = Path(roles.claude_dir()) / "plugins" / "cache"

    def install(market: str, version: str, text: str) -> Path:
        folder = cache / market / "ponytail" / version / "skills" / "ponytail"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(text, encoding="utf-8")
        return folder

    install("temp_github_1", "9.0.0", "---\nname: ponytail\n---\nhalf a clone\n")
    assert roles.find_skill("ponytail").origin == "bundled"
    install("market", "5.0.0", "---\nname: ponytail\n---\nUpstream five.\n")
    newest = install("market", "5.3.2", "---\nname: ponytail\ndescription: up\n---\nUpstream newest.\n")
    found = roles.find_skill("ponytail")
    assert found.origin == "plugin" and found.version == "5.3.2" and found.body == "Upstream newest."
    briefing = roles.brief(role)
    assert "Upstream newest." in briefing.system_prompt and briefing.sources == {"ponytail": "plugin@5.3.2"}
    assert newest in briefing.read_dirs
    assert roles.skill_sources(role) == {"ponytail": "plugin@5.3.2"}
    upstream = roles.version(role)
    assert upstream != vendored
    # malformed, empty or another skill's file: the vendored copy again, and its version
    for text in ("---\nbroken\n---\n", "---\nname: ponytail\n---\n\n", "---\nname: other\n---\nx\n"):
        (newest / "SKILL.md").write_text(text, encoding="utf-8")
        assert roles.find_skill("ponytail").origin == "bundled" and roles.version(role) == vendored
    (newest / "SKILL.md").unlink()
    assert roles.find_skill("ponytail").origin == "bundled"
    # the person's own copy still wins, and a skill with no upstream never looks in the cache
    mine = home() / "skills" / "ponytail"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: ponytail\n---\nMine.\n", encoding="utf-8")
    assert roles.find_skill("ponytail").origin == "user"
    assert roles.upstream_skill("security-audit") is None


def test_a_brief_carries_skills_offers_the_rest_by_path_and_names_what_is_missing(monkeypatch):
    role = roles.Role("r", "does r", ("implement",), "Be r.", skills=("ponytail", "ghost", "engineering-baseline"),
                      on_demand=("python-rules", "nope"))
    monkeypatch.setattr(roles, "MAX_INLINE_CHARS", 5000)
    briefing = roles.brief(role)
    text = briefing.system_prompt
    assert text.startswith("You work as the `r` role: does r\n\nBe r.")
    assert "## Skill `ponytail`" in text and "## Skill `engineering-baseline`" not in text
    assert "`engineering-baseline`" in text and "python-rules" in text and "SKILL.md" in text
    assert briefing.skills == ("ponytail",) and briefing.missing == ("ghost", "nope")
    rules = roles.read_rules(briefing)
    assert rules == (f"Read(/{roles.bundled_skills_dir().resolve()}/**)",)


def test_a_carried_language_rules_skill_reaches_only_a_repository_of_that_language():
    # A role that carries every language's rules, in a Rust repository: only Rust's reach the brief.
    role = roles.Role("r", "does r", ("implement",), "", skills=("rust-rules", "python-rules", "cpp-rules", "ponytail"))
    rust = frozenset({".rs", ".toml", ".md"})
    briefing = roles.brief(role, held=rust)
    assert "python-rules" not in briefing.system_prompt and "cpp-rules" not in briefing.system_prompt
    assert "rust-rules" in briefing.system_prompt and "ponytail" in briefing.system_prompt
    assert set(roles.skill_sources(role, held=rust)) == {"rust-rules", "ponytail"}
    assert "python-rules" in roles.brief(role).system_prompt  # unknown languages: every one offered


def test_a_skill_of_the_person_s_or_elsewhere_gets_its_own_read_rule(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    mine = home() / "skills" / "x"
    mine.mkdir(parents=True)
    briefing = roles.Briefing(roles.Role("r", "d", ("implement",), ""), "", (other, mine))
    assert roles.read_rules(briefing) == (f"Read(/{other.resolve()}/**)", f"Read(/{(home() / 'skills').resolve()}/**)")


# --- writing ---------------------------------------------------------------------------


def test_copy_writes_the_resolved_role_to_edit_and_refuses_what_it_should():
    path = roles.copy("tester", "my-tester", kinds=["test", "implement"], description="tests my way")
    copied = roles.load().get("my-tester")
    assert copied.scope == "user" and copied.derived_from == "tester" and copied.kinds == ("test", "implement")
    assert copied.prompt == roles.load().get("tester").prompt and copied.capabilities == ("browser",)
    assert "on_demand: [python-rules" in path.read_text()
    with pytest.raises(roles.RoleError, match="already exists"):
        roles.copy("tester", "my-tester")
    with pytest.raises(roles.RoleError, match="already a role"):
        roles.copy("tester", "reviewer")
    with pytest.raises(roles.RoleError, match="no role name"):
        roles.copy("tester", "Bad Name")
    with pytest.raises(roles.RoleError, match="no kind"):
        roles.copy("tester", "t2", kinds=["nope"])
    shadow = roles.copy("tester", "tester")
    assert roles.load().get("tester").shadows and shadow.parent == user_roles()
    assert roles.remove("my-tester") == path and "my-tester" not in roles.load().roles
    with pytest.raises(roles.RoleError, match="bundled role is not removed"):
        roles.remove("reviewer")


# --- teams -----------------------------------------------------------------------------


def test_teams_are_made_used_by_projects_changed_and_deleted(git_repo):
    catalog = roles.load()
    team, why = teams.of(None, catalog)
    assert team.name == "locks" and "bundled" in why and team.kinds(catalog) == roles.ROLE_KINDS
    teams.create("web", ["frontend"], description="web", start_from=None)
    teams.change("web", add=["implementer", "reviewer"], drop=["frontend"], description="web work")
    web = teams.get("web")
    assert web.role_names == ("implementer", "reviewer") and web.description == "web work"
    teams.use("github.com/o/r", "web")
    assert teams.of("github.com/o/r")[0].name == "web" and teams.projects_of("web") == ["github.com/o/r"]
    staffing = teams.staff("review-critical", "github.com/o/r", git_repo)
    assert staffing.role is None and staffing.covered == ("implement", "review-routine")
    assert teams.staff("implement", "github.com/o/r", git_repo).role.name == "implementer"
    with pytest.raises(teams.TeamError, match=r"team of github\.com/o/r"):
        teams.delete("web")
    teams.use("github.com/o/r", None)
    assert teams.delete("web").name == "web.json" and teams.assignments() == {}
    described = teams.describe(teams.bundled(catalog), catalog)
    assert described["projects"] == [] and len(described["roles"]) == 14


def test_a_team_refuses_what_it_should():
    with pytest.raises(teams.TeamError, match="no team name"):
        teams.create("Bad Name", ["tester"])
    with pytest.raises(teams.TeamError, match="team locks ships"):
        teams.create("locks", ["tester"])
    with pytest.raises(teams.TeamError, match="no role 'ghost'"):
        teams.create("t", ["ghost"])
    with pytest.raises(teams.TeamError, match="at least one role"):
        teams.create("t", [])
    teams.create("t", [], start_from="locks")
    with pytest.raises(teams.TeamError, match="already a team"):
        teams.create("t", ["tester"])
    with pytest.raises(teams.TeamError, match="is not changed"):
        teams.change("locks", add=["tester"])
    with pytest.raises(teams.TeamError, match="is not deleted"):
        teams.delete("locks")
    with pytest.raises(teams.TeamError, match="no team 'gone'"):
        teams.delete("gone")
    with pytest.raises(teams.TeamError, match="no team 'gone'"):
        teams.use("k", "gone")
    teams.use("k", "locks")
    assert teams.assignments() == {}


def test_a_team_file_that_is_wrong_is_refused_and_a_gone_team_falls_back(tmp_path):
    folder = teams.folder()
    folder.mkdir(parents=True)
    (folder / "bad.json").write_text("{")
    (folder / "list.json").write_text("[]")
    (folder / "roles.json").write_text('{"roles": "tester"}')
    (folder / "locks.json").write_text('{"roles": []}')
    (folder / "ok.json").write_text('{"roles": ["tester", "ghost"], "description": 3}')
    found, refused = teams.listing()
    assert [t.name for t in found] == ["locks", "ok"] and found[1].description == ""
    assert set(refused) == {"bad", "list", "roles", "locks"}
    assert found[1].members(roles.load())[1] == ["ghost"]
    teams.use("k", "ok")
    (folder / "ok.json").unlink()
    team, why = teams.of("k")
    assert team.name == "locks" and "is gone" in why


# --- the classifier, inside a team ------------------------------------------------------


def test_a_kind_the_team_does_not_cover_is_read_as_the_nearest_it_does():
    found = Classification("explore", "low", "rule", "a search")
    assert narrow(found, None) is found and narrow(found, ("explore",)) is found
    assert narrow(found, ("review-routine", "implement")).kind == "review-routine"
    assert narrow(Classification("ui"), ("refactor", "chat")).kind == "refactor"
    assert narrow(Classification("ui"), ("chat",)).kind == "chat"
    assert "which this project's team has no role for" in narrow(found, ("implement",)).reason
    assert classify("how does this work?", use_model=False, kinds=("implement",)).kind == "implement"
    assert classify("refactor the parser", use_model=False, kinds=("docs",)).kind == "docs"


def test_the_model_is_offered_only_the_team_s_kinds():
    seen = {}

    def runner(argv, **kwargs):
        seen["schema"] = argv[argv.index("--json-schema") + 1]
        seen["prompt"] = argv[argv.index("--system-prompt") + 1]

        class P:
            stdout = ('{"structured_output": {"kind": "test", "complexity": "low", "confidence": 0.9, '
                      '"reason": "r"}}')
            stderr = ""
        return P()

    result = by_model("x", kinds=("test", "docs", "frontier"), runner=runner)
    assert result.kind == "test" and '"enum": ["docs", "test"]' in seen["schema"]
    assert "- docs:" in seen["prompt"] and "- ui:" not in seen["prompt"]
    by_model("x", kinds=("frontier",), runner=runner)
    assert '"ui"' in seen["schema"]


def test_claude_code_agents_import_as_roles_of_the_person_s_and_change_no_team(tmp_path, capsys):
    from locks import cli

    agents = tmp_path / "agents"
    (agents / "quality").mkdir(parents=True)
    (agents / "quality" / "code-reviewer.md").write_text(
        "---\nname: code-reviewer\ndescription: Reviews code for bugs.\\n\\nExamples: <example>x</example>\n"
        "tools: Read, Grep, Glob\nmodel: opus\ncolor: red\n---\nYou review changes carefully.\n")
    (agents / "react-pro.md").write_text("---\nname: React Pro\ndescription: Builds React UI\n---\nBuild UI.\n")
    (agents / "notes.md").write_text("just a readme, no frontmatter")
    (agents / "empty.md").write_text("---\nname: empty\n---\n")
    assert cli.main(["roles", "import", str(agents), "--dry-run"]) == 0
    assert capsys.readouterr().out.startswith("would import 2 role(s)")
    assert cli.main(["roles", "import", str(agents)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("imported 2 role(s)") and "locks team change" in out
    catalog = roles.load()
    reviewer = catalog.get("code-reviewer")
    assert reviewer.kinds == ("review-routine", "review-critical") and reviewer.description == "Reviews code for bugs."
    assert set(reviewer.disallowed_tools) == {"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch",
                                              "WebSearch"}
    assert reviewer.prompt == "You review changes carefully." and reviewer.derived_from == "agent:code-reviewer"
    assert catalog.get("react-pro").kinds == ("ui", "feature") and not catalog.get("react-pro").disallowed_tools
    # imported again: nothing replaced
    assert cli.main(["roles", "import", str(agents)]) == 0
    assert capsys.readouterr().out.startswith("imported 0 role(s)")
