from __future__ import annotations

import json

import pytest

from locks import runtime

from .conftest import git


def _nvm(tmp_path, *versions):
    for v in versions:
        b = tmp_path / "nvm" / "versions" / "node" / f"v{v}" / "bin"
        b.mkdir(parents=True)
        (b / "node").write_text("")
    return {"NVM_DIR": str(tmp_path / "nvm"), "PATH": "/usr/bin"}


@pytest.mark.parametrize(
    ("spec", "fits"),
    [("22", ["22.16.0", "22.23.2"]), ("v22.23.2", ["22.23.2"]), ("22.x", ["22.16.0", "22.23.2"]),
     (">=22.22.3", ["22.23.2", "24.1.0"]), ("^22.22.3", ["22.23.2"]), ("~22.16", ["22.16.0"]),
     (">=22 <24", ["22.16.0", "22.23.2"]), ("20 || >=24", ["20.1.0", "24.1.0"]), ("lts/*", []), ("node", [])],
)
def test_a_declared_version_matches_as_nvm_and_engines_write_it(spec, fits):
    every = [(20, 1, 0), (22, 16, 0), (22, 23, 2), (24, 1, 0)]
    assert [".".join(map(str, v)) for v in every if runtime.satisfies(v, spec)] == fits


def test_the_project_s_node_goes_first_on_the_path(tmp_path):
    env = _nvm(tmp_path, "22.16.0", "22.23.2", "24.1.0")
    project = tmp_path / "app"
    (project / "src").mkdir(parents=True)
    git(project, "init", "-q")
    assert runtime.node_bin(project / "src", env) is None and runtime.env_for(project, env) == {}
    (project / "package.json").write_text(json.dumps({"engines": {"node": ">=22.22.3 <24"}}))
    node = runtime.node_bin(project / "src", env)
    assert node.version == "22.23.2" and node.source == "package.json engines.node"
    assert runtime.env_for(project, env)["PATH"].startswith(str(node.bin))
    (project / ".nvmrc").write_text("v22.16\n")
    assert runtime.node_bin(project, env).version == "22.16.0"
    (project / ".nvmrc").write_text("18\n")
    assert runtime.node_bin(project, env) is None  # declared, not installed: nothing is guessed
    assert runtime.installed({"NVM_DIR": str(tmp_path / "none")}) == []


def test_a_folder_of_projects_gets_the_node_that_fits_them_all(tmp_path):
    env = _nvm(tmp_path, "22.16.0", "22.23.2")
    holder = tmp_path / "work"
    for name, spec in (("web", ">=22.22.3"), ("api", "22")):
        (holder / name).mkdir(parents=True)
        git(holder / name, "init", "-q")
        (holder / name / ".node-version").write_text(spec)
    node = runtime.node_bin(holder, env)
    assert node.version == "22.23.2" and node.source == "the projects in this folder"


# --- a project that declares nothing, and dependencies that do ------------------------

def _project(tmp_path, deps, *, dev=None, manifest_extra=None):
    """A checkout with direct dependencies installed, each with its own engines.node."""
    project = tmp_path / "web"
    project.mkdir()
    git(project, "init", "-q")
    names = {name: "*" for name in deps}
    (project / "package.json").write_text(json.dumps({"dependencies": names, "devDependencies": dev or {},
                                                      **(manifest_extra or {})}))
    for name, spec in {**deps, **{k: v for k, v in (dev or {}).items() if v != "*"}}.items():
        if spec is None:
            continue  # declared, not installed
        d = project / "node_modules" / name
        d.mkdir(parents=True)
        (d / "package.json").write_text(json.dumps({"name": name, "engines": {"node": spec}} if spec else {}))
    return project


def at(version):
    return lambda env: tuple(int(p) for p in version.split("."))


def test_a_dependency_that_needs_a_newer_node_gets_one_of_the_same_major(tmp_path):
    env = _nvm(tmp_path, "22.16.0", "22.23.2", "24.1.0")
    project = _project(tmp_path, {"@scope/cli": "^20.19.0 || ^22.22.3 || >=24.0.0", "lib": ">= 18"})
    node = runtime.node_bin(project, env, current=at("22.16.0"))
    # 22.23.2, not 24.1.0: native modules built for 22 still load
    assert node.version == "22.23.2" and node.source == "@scope/cli requires ^20.19.0 || ^22.22.3 || >=24.0.0"
    # the Node already on the PATH meets them all: nothing changes, a newer one installed or not
    assert runtime.node_bin(project, env, current=at("22.23.2")) is None
    assert runtime.needed(project, env, current=at("24.0.5")) == (None, None)
    # no same major fits: the newest that does
    assert runtime.node_bin(project, env, current=at("21.0.0")).version == "24.1.0"
    # a project that declares its Node is taken at its word, whatever its dependencies say
    (project / ".nvmrc").write_text("22.16.0\n")
    assert runtime.node_bin(project, env, current=at("22.16.0")).version == "22.16.0"


def test_none_installed_fits_says_which_dependency_needs_what_and_changes_nothing(tmp_path, monkeypatch):
    from locks import orchestrate
    from locks.store import Store

    env = _nvm(tmp_path, "22.16.0")
    project = _project(tmp_path, {"@scope/cli": ">=22.22.3"})
    node, unmet = runtime.needed(project, env, current=at("22.16.0"))
    assert node is None
    assert unmet.startswith("this task needs Node >=22.22.3 (@scope/cli requires it) and the Node workers get "
                            "is 22.16.0")
    # the plan says so before any money is spent, as a warning a run prints at once
    monkeypatch.setattr(runtime, "on_path", at("22.16.0"))
    monkeypatch.setenv("NVM_DIR", env["NVM_DIR"])
    store = Store.open()
    try:
        plan = orchestrate.plan("fix it", project, store, orchestrate.Options(use_model_classifier=False), {})
    finally:
        store.close()
    assert any(r.startswith("this task needs Node >=22.22.3") for r in plan.reasons)


@pytest.mark.parametrize("deps", [
    {"a": "lts/*", "b": "node", "c": "*", "d": "garbage 1"},  # specs locks cannot read
    {"a": None},  # declared, never installed
    {"a": ""},  # installed, no engines
    {"../../outside": ">=99"},  # a name that climbs out of node_modules is no package
    {"@x/..": ">=99"},  # nor one that climbs back up inside it
])
def test_what_says_nothing_about_node_changes_nothing(tmp_path, deps):
    env = _nvm(tmp_path, "22.16.0", "24.1.0")
    project = _project(tmp_path, {}, manifest_extra={"dependencies": dict.fromkeys(deps, "*")})
    for name, spec in deps.items():
        if spec is not None:
            # where the name leads, climbing or not, holds a spec nothing installed meets
            (project / "node_modules" / name).parent.mkdir(parents=True, exist_ok=True)  # so the climb is real
            target = (project / "node_modules" / name / "package.json").resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"engines": {"node": spec}} if spec else {}))
    assert runtime.needed(project, env, current=at("22.16.0")) == (None, None)


def test_an_unknown_current_node_or_a_broken_manifest_is_never_a_guess(tmp_path):
    env = _nvm(tmp_path, "22.23.2")
    project = _project(tmp_path, {"cli": ">=22.22.3"})
    assert runtime.needed(project, env, current=lambda env: None) == (None, None)
    (project / "package.json").write_text("{not json")
    assert runtime.needed(project, env, current=at("22.16.0")) == (None, None)
    (project / "package.json").write_text("[1, 2]")
    assert runtime.dependency_engines(project) == []
    # dev dependencies count, and an unreadable dependency manifest is skipped
    (project / "package.json").write_text(json.dumps({"devDependencies": {"cli": "1", "broken": "1"}}))
    (project / "node_modules" / "broken").mkdir()
    (project / "node_modules" / "broken" / "package.json").write_text("nope")
    assert runtime.dependency_engines(project / "src") == [("cli", ">=22.22.3")]


def test_the_node_on_the_path_is_asked_for_its_version(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    node = bindir / "node"
    node.write_text("#!/bin/sh\necho v22.16.0\n")
    node.chmod(0o755)
    assert runtime.on_path({"PATH": str(bindir)}) == (22, 16, 0)
    node.write_text("#!/bin/sh\nexit 1\n")
    assert runtime.on_path({"PATH": str(bindir)}) is None
    assert runtime.on_path({"PATH": str(tmp_path / "none")}) is None


def test_the_project_s_own_package_goes_first_on_pythonpath_when_the_venv_imports_it_from_elsewhere(tmp_path):
    import sys

    project, elsewhere = tmp_path / "wt", tmp_path / "checkout"
    (elsewhere / "src" / "my_pkg").mkdir(parents=True)
    (elsewhere / "src" / "my_pkg" / "__init__.py").write_text("")
    (project / "src" / "my_pkg").mkdir(parents=True)
    (project / "pyproject.toml").write_text('[project]\nname = "my-pkg"\n')
    assert runtime.package_elsewhere(project) is None  # no .venv here
    venv = project / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text(f"#!/bin/sh\nPYTHONPATH={elsewhere / 'src'} exec {sys.executable} \"$@\"\n")
    (venv / "python").chmod(0o755)
    package, root, origin = runtime.package_elsewhere(project)
    assert package == "my_pkg" and root == project / "src" and origin.startswith(str(elsewhere))
    env = {"PATH": "/bin", "PYTHONPATH": "/extra"}
    assert runtime.env_for(project, env) == {"PYTHONPATH": f"{project / 'src'}:/extra"}
    assert runtime.env_for(project, {"PATH": "/bin"}) == {"PYTHONPATH": str(project / "src")}
    # The venv that imports the package from here needs nothing.
    (venv / "python").write_text(f"#!/bin/sh\nPYTHONPATH={project / 'src'} exec {sys.executable} \"$@\"\n")
    assert runtime.package_elsewhere(project) is None and runtime.env_for(project, env) == {}
    # No name, or a python that cannot answer: nothing, never a failure.
    (project / "pyproject.toml").write_text("[tool.x]\n")
    assert runtime.package_elsewhere(project) is None
    (project / "pyproject.toml").write_text('[project]\nname = "my-pkg"\n')
    (venv / "python").write_text("#!/bin/sh\nexit 3\n")
    assert runtime.package_elsewhere(project) is None
    (venv / "python").write_text("not a program\n")  # cannot start: said, and env_for goes without
    with pytest.raises(runtime.ProbeBroke):
        runtime.package_elsewhere(project)
    assert runtime.env_for(project, env) == {}
