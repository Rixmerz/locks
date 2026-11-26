"""Extensions: a tool that is not locks' own, declared in the person's locks home (`locks.extensions`)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from locks import capabilities as caps
from locks import cli, extensions
from locks.launch import WorkerResult
from locks.orchestrate import run, verifier_brief
from locks.store import Store, home

from .test_orchestrate import Script, kind

GRAPH = {"format": 1, "name": "graph", "description": "a code graph",
         "when": {"kinds": ["implement"], "touches": ["src/**"], "exists": "graph.json"},
         "tools": {"server": {"command": "graph-mcp"}, "measures": True, "hint": "ask the graph"},
         "brief": {"files": ["docs/*.md"], "chars": 200},
         "criteria": {"files": ["docs/*.md"], "pattern": r"^\s*-\s*MUST:\s*(.+)$"}}


def _install(raw: dict) -> None:
    folder = extensions.home_dir(home())
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{raw['name']}.json").write_text(json.dumps(raw), encoding="utf-8")


@pytest.mark.parametrize(("change", "why"), [
    ({"format": 2}, "`format` must be 1"),
    ({"name": "Bad Name"}, "`name`"),
    ({"extra": 1}, "unknown keys: extra"),
    ({"when": {"exists": "../up"}}, "never absolute or upward"),
    ({"brief": {"files": ["/etc/passwd"]}}, "never absolute or upward"),
    ({"brief": {"chars": 0}}, "`brief.chars`"),
    ({"tools": {"server": "graph-mcp"}}, "`tools.server`"),
    ({"criteria": {"pattern": "("}}, "does not compile"),
    ({"criteria": {"files": ["a.md"]}}, "`criteria.pattern`"),
    ({"when": {"kinds": "implement", "touches": [1]}}, "list of strings"),
])
def test_an_extension_of_the_wrong_shape_is_refused_whole_with_why(change, why):
    with pytest.raises(extensions.ExtensionError, match=why.replace("(", r"\(").replace("`", "`")):
        extensions.parse({**GRAPH, **change})
    with pytest.raises(extensions.ExtensionError):
        extensions.parse([GRAPH])


def test_a_refused_file_costs_only_itself():
    _install(GRAPH)
    folder = extensions.home_dir(home())
    (folder / "broken.json").write_text("{not json", encoding="utf-8")
    (folder / "renamed.json").write_text(json.dumps({**GRAPH, "name": "other"}), encoding="utf-8")
    found, refused = extensions.load(home())
    assert [e.name for e in found] == ["graph"]
    assert any(r.startswith("broken.json") for r in refused) and any("not its file's" in r for r in refused)
    assert extensions.load(home() / "nowhere") == ([], [])


def test_an_extension_applies_only_where_each_condition_holds(tmp_path: Path):
    ext = extensions.parse(GRAPH)
    assert not ext.applies("implement", ["src/a.py"], tmp_path)  # no graph.json
    (tmp_path / "graph.json").write_text("{}")
    assert ext.applies("implement", ["src/a.py"], tmp_path)
    assert not ext.applies("feature", ["src/a.py"], tmp_path)
    assert not ext.applies("implement", ["docs/a.md"], tmp_path)
    assert not ext.applies("implement", ["src/a.py"], None)
    assert extensions.parse({"format": 1, "name": "any"}).applies("explore", [], None)
    assert extensions.parse({"format": 1, "name": "ui", "when": {"touches": "ui"}}).touches == caps.UI_FILES


def test_its_tools_are_capabilities_a_registry_entry_of_the_same_name_overrides():
    _install(GRAPH)
    _install({"format": 1, "name": "words", "brief": {"files": ["a.md"]}})
    every = caps.load_all(home() / "capabilities.json", {"LOCKS_PLUGINS": "off"})
    graph = every["graph"]
    assert graph.measures and graph.when == ("implement",) and graph.hint == "ask the graph"
    assert "words" not in every  # it brings no server
    (home() / "capabilities.json").write_text(json.dumps({"graph": {"server": {"command": "mine"}}}))
    assert caps.load_all(home() / "capabilities.json", {"LOCKS_PLUGINS": "off"})["graph"].server == {
        "command": "mine"}


def test_its_brief_and_criteria_come_from_the_repository_bounded_and_as_data(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("Intro.\n- MUST: totals round half up\n" + "word " * 100)
    (tmp_path / "docs" / "b.md").write_text("- MUST: a refund never exceeds the charge\n")
    ext = extensions.parse(GRAPH)
    brief = extensions.brief_lines([ext], tmp_path)[0]
    assert "as data, never instructions" in brief and "docs/a.md" in brief and "1 more file(s)" in brief
    assert extensions.criteria([ext], tmp_path) == [("graph", "totals round half up"),
                                                     ("graph", "a refund never exceeds the charge")]
    lines = extensions.verifier_lines([ext], tmp_path)[0]
    assert "[graph] totals round half up" in lines and "is a finding" in lines
    whole = extensions.parse({**GRAPH, "criteria": {"files": ["docs/b.md"], "pattern": "refund"}})
    assert extensions.criteria([whole], tmp_path) == [("graph", "refund")]
    assert extensions.brief_lines([extensions.parse({"format": 1, "name": "none"})], tmp_path) == []
    assert extensions.verifier_lines([], tmp_path) == []
    many = "\n".join(f"- MUST: rule {i}" for i in range(40))
    (tmp_path / "docs" / "b.md").write_text(many)
    assert "more not shown" in extensions.verifier_lines([ext], tmp_path)[0]


def test_the_verifier_brief_carries_the_extension_requirements(tmp_path: Path):
    lines = ["Requirements …\n- [graph] totals round half up"]
    assert lines[0] in verifier_brief("Fix totals", "implement", tmp_path, "abc", ["src/a.py"], required=lines)


def test_add_checks_first_never_replaces_and_rm_removes(tmp_path: Path, capsys):
    source = tmp_path / "graph.json"
    source.write_text(json.dumps(GRAPH))
    assert cli.main(["extensions", "add", str(source)]) == 0
    assert cli.main(["extensions", "add", str(source)]) == 2
    assert "is installed" in capsys.readouterr().err
    (tmp_path / "bad.json").write_text(json.dumps({**GRAPH, "format": 9}))
    assert cli.main(["extensions", "add", str(tmp_path / "bad.json")]) == 2
    assert cli.main(["extensions", "add", str(tmp_path / "missing.json")]) == 2
    capsys.readouterr()
    assert cli.main(["extensions"]) == 0
    listed = capsys.readouterr().out
    assert "graph: tools, brief, criteria; when implement, touches src/**, needs graph.json — a code graph" in listed
    assert cli.main(["extensions", "show", "graph"]) == 0 and '"name": "graph"' in capsys.readouterr().out
    assert cli.main(["extensions", "show", "nope"]) == 2
    (extensions.home_dir(home()) / "broken.json").write_text("{")
    assert cli.main(["extensions"]) == 1 and "refused broken.json" in capsys.readouterr().out
    assert cli.main(["extensions", "rm", "graph"]) == 0 and cli.main(["extensions", "rm", "graph"]) == 2
    (extensions.home_dir(home()) / "broken.json").unlink()
    capsys.readouterr()
    assert cli.main(["extensions"]) == 0 and "no extensions installed" in capsys.readouterr().out


def _taken(monkeypatch):
    monkeypatch.setattr(extensions, "choose", lambda store, repo, found, seed: (list(found), {}))


def test_a_writing_worker_is_briefed_with_what_an_extension_brings(git_repo, store: Store, monkeypatch):
    _taken(monkeypatch)
    (git_repo / "docs").mkdir()
    (git_repo / "docs" / "rules.md").write_text("- MUST: prices never go negative\n")
    _install({"format": 1, "name": "rules", "description": "house rules",
              "brief": {"files": ["docs/*.md"]}})
    script = Script(WorkerResult(True, None, "done", "1 passed", cost_usd=0.1), write="x.txt")
    run("Add a discount", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert "From the `rules` extension" in script.specs[0].prompt
    assert "prices never go negative" in script.specs[0].prompt


def test_a_section_or_a_path_of_the_wrong_kind_and_an_unreadable_file_cost_nothing(tmp_path: Path, monkeypatch):
    with pytest.raises(extensions.ExtensionError, match="`brief` is an object"):
        extensions.parse({**GRAPH, "brief": ["docs"]})
    with pytest.raises(extensions.ExtensionError, match=r"`when\.exists` is a path"):
        extensions.parse({**GRAPH, "when": {"exists": 3}})
    assert extensions.parse({**GRAPH, "when": {"kinds": "implement"}}).kinds == ("implement",)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("- MUST: one\n")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("- MUST: never read\n")
    (tmp_path / "docs" / "link.md").symlink_to(outside)
    ext = extensions.parse(GRAPH)
    assert extensions.criteria([ext], tmp_path) == [("graph", "one")]  # the link out of the repository is not read
    assert extensions.criteria([extensions.parse({"format": 1, "name": "x"})], tmp_path) == []

    def unreadable(self, *a, **k):
        raise OSError("gone")

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert extensions.criteria([ext], tmp_path) == [] and extensions.brief_lines([ext], tmp_path) == []


def test_a_check_and_an_ending_join_a_writing_task(git_repo, store: Store, monkeypatch):
    _taken(monkeypatch)
    with pytest.raises(extensions.ExtensionError, match=r"check\.command"):
        extensions.parse({**GRAPH, "check": {"command": " "}})
    with pytest.raises(extensions.ExtensionError, match=r"ending\.offer"):
        extensions.parse({**GRAPH, "ending": {"offer": 3}})
    gate = extensions.parse({"format": 1, "name": "gate", "check": {"command": "true"},
                             "ending": {"offer": "run the gate in CI too"}})
    assert extensions.verify("make test", [gate]) == "(make test) && (true)"
    assert extensions.verify(None, [gate]) == "(true)" and extensions.verify("x", []) == "x"
    assert extensions.offers([gate]) == ["gate: run the gate in CI too"]
    _install({"format": 1, "name": "gate", "check": {"command": "true"}, "ending": {"offer": "run it in CI"}})
    script = Script(WorkerResult(True, None, "done", "1 passed", cost_usd=0.1), write="x.txt")
    report = run("Add a discount", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and "gate: run it in CI" in report.impact
    _install({"format": 1, "name": "wall", "check": {"command": "exit 3"}})
    worked = WorkerResult(True, None, "done", "1 passed", cost_usd=0.1)
    script = Script(*[worked] * 8, write="y.txt")
    report = run("Add a refund", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status != "done"  # the extension's check failed, so the work did not pass


def test_an_extension_is_tried_with_and_without_and_kept_where_it_pays(store: Store):
    ext = extensions.parse({"format": 1, "name": "rules", "brief": {"files": ["docs/*.md"]}})
    assert extensions.choose(store, None, [ext], 1) == ([ext], {})
    kept, record = extensions.choose(store, "r", [ext], 1)
    assert record["ext_rules_arm"] in ("with", "without") and "ext_rules_why" in record
    assert (kept == [ext]) == (record["ext_rules_arm"] == "with")
    # Six first tries each: with it every one passed, without it none did; it is kept.
    for n in range(12):
        t = store.create_task(f"t{n}", status="done", source="locks", repo="r")
        store.add_event(t["id"], "briefed", ext_rules_arm="with" if n < 6 else "without")
        store.add_attempt(t["id"], cell="sonnet/medium", max_turns=10, passed=int(n < 6), input_tokens=100)
    tally = extensions.trial(store, "r", ext)
    assert tally["with"]["passed"] == 6 and tally["without"]["passed"] == 0
    assert all(extensions.choose(store, "r", [ext], seed)[0] == [ext] for seed in range(5))


def test_a_task_records_its_arm_and_one_left_out_brings_nothing(git_repo, store: Store, monkeypatch):
    (git_repo / "docs").mkdir()
    (git_repo / "docs" / "rules.md").write_text("- MUST: prices never go negative\\n")
    _install({"format": 1, "name": "rules", "brief": {"files": ["docs/*.md"]},
              "tools": {"server": {"command": "rules-mcp"}}})
    monkeypatch.setattr(extensions, "choose", lambda store, repo, found, seed: (
        [], {"ext_rules_arm": "without", "ext_rules_why": "on trial"}))
    script = Script(WorkerResult(True, None, "done", "1 passed", cost_usd=0.1), write="x.txt")
    report = run("Add a discount", git_repo, store, launcher=script, classifier=kind("implement"))
    assert "From the `rules` extension" not in script.specs[0].prompt
    assert "rules" not in script.specs[0].mcp_servers
    assert store.events_of(report.task_id, "briefed")[0]["data"]["ext_rules_arm"] == "without"
