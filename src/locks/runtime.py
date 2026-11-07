"""The Node a project declares, put first on a worker's PATH.

A worker inherits the PATH of whoever ran `locks`, and with it whatever Node
was the default there. A project that needs another one sends the worker
hunting: `source nvm.sh`, `nvm use`, `~/.nvm/versions/node/v22.x/bin/node`,
`/opt/homebrew/bin/node` — each a different command, each refused, each a
new `--allow` to pass. So locks reads the version the project declares —
`.nvmrc`, `.node-version`, or `engines.node` in `package.json` — finds it
among the versions nvm installed, and puts its `bin` first on the PATH of the
worker and of its `--verify` check. Nothing is installed or switched.

A project that declares nothing still has dependencies that do: a CLI that
refuses to start on an older Node says so in its own `engines.node`. When
the Node on the PATH fails what the installed dependencies require, locks
puts one that meets them first instead, of the same major when one is
installed, so native modules built for it still load. When none installed
fits, the plan says which dependency needs what, before any money is spent.

Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

VERSION_FILES = (".nvmrc", ".node-version")
#: Dependencies read for their `engines.node`, at most: a manifest is small,
#: and a project with more direct dependencies than this is rare.
MAX_DEPENDENCIES = 400
_PARTS = re.compile(r"v?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?")


@dataclass(frozen=True)
class Node:
    version: str
    bin: Path
    #: Where the wanted version came from, e.g. `.nvmrc`.
    source: str


def wanted(workdir: Path) -> tuple[str, str] | None:
    """(the version spec, the file it came from), looking in `workdir` and its
    parents up to the top of its checkout."""
    for d in (workdir, *workdir.parents):
        for name in VERSION_FILES:
            f = d / name
            if f.is_file():
                text = f.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    return text.splitlines()[0].strip(), name
        manifest = d / "package.json"
        if manifest.is_file():
            try:
                engines = json.loads(manifest.read_text(encoding="utf-8")).get("engines") or {}
            except (OSError, ValueError, AttributeError):
                engines = {}
            if isinstance(engines, dict) and isinstance(engines.get("node"), str):
                return engines["node"].strip(), "package.json engines.node"
        if (d / ".git").exists():
            break
    return None


def installed(env: Mapping[str, str] | None = None) -> list[tuple[tuple[int, ...], Path]]:
    """The Node versions nvm installed, oldest first, with their `bin`."""
    env = os.environ if env is None else env
    root = Path(env.get("NVM_DIR") or Path(env.get("HOME") or Path.home()) / ".nvm") / "versions" / "node"
    found = []
    try:
        for d in root.iterdir():
            version = _version(d.name)
            if version and len(version) == 3 and (d / "bin" / "node").exists():
                found.append((version, d / "bin"))
    except OSError:
        return []
    return sorted(found)


def node_bin(workdir: Path, env: Mapping[str, str] | None = None, *,
             current: Callable[[Mapping[str, str]], tuple[int, ...] | None] | None = None) -> Node | None:
    """The installed Node that satisfies what the project declares, the newest
    that does; when it declares nothing, one its dependencies need and the
    Node on the PATH is not. None when nothing is declared or needed, or
    nothing installed fits."""
    found = wanted(workdir)
    if found is not None:
        specs, source = [found[0]], found[1]
    else:
        # A folder that holds several projects declares nothing itself: the
        # Node that fits every project in it does.
        from locks.launch import nested_checkouts

        inner = [w for w in (wanted(sub) for sub in nested_checkouts(workdir)) if w is not None]
        if not inner:
            return needed(workdir, env, current=current)[0]
        specs, source = [w[0] for w in inner], "the projects in this folder"
    fits = [(v, b) for v, b in installed(env) if all(satisfies(v, spec) for spec in specs)]
    if not fits:
        return None
    version, binary = fits[-1]
    return Node(".".join(map(str, version)), binary, source)


def needed(workdir: Path, env: Mapping[str, str] | None = None, *,
           current: Callable[[Mapping[str, str]], tuple[int, ...] | None] | None = None,
           ) -> tuple[Node | None, str | None]:
    """For a project that declares no Node: (the installed one its dependencies
    need, or None; what is unmet when none installed fits, or None).

    Only when the Node on the PATH fails a dependency's `engines.node`, and
    never on a guess: an unknown current Node, or a spec locks cannot read (a
    name, a typo), changes nothing."""
    env = os.environ if env is None else env
    engines = dependency_engines(workdir)
    if not engines:
        return None, None
    now = (current or on_path)(env)
    if now is None:
        return None, None
    have = installed(env)
    # A spec locks cannot read (a name, a typo) says nothing about which Node to run.
    meaningful = [(name, spec) for name, spec in engines if readable(spec)]
    failing = [(name, spec) for name, spec in meaningful if not satisfies(now, spec)]
    if not failing:
        return None, None
    fits = [(v, b) for v, b in have if all(satisfies(v, spec) for _, spec in meaningful)]
    name, spec = failing[0]
    if not fits:
        shown = ".".join(map(str, now))
        return None, (f"this task needs Node {spec} ({name} requires it) and the Node workers get is {shown}: "
                      f"install one that fits (`nvm install <version>`), or the worker cannot run the project's tools")
    same = [(v, b) for v, b in fits if v[0] == now[0]]
    version, binary = (same or fits)[-1]
    return Node(".".join(map(str, version)), binary, f"{name} requires {spec}"), None


def dependency_engines(workdir: Path) -> list[tuple[str, str]]:
    """`(package, engines.node)` for each direct dependency installed in the
    project's `node_modules`, looking from `workdir` up to its checkout's top."""
    for d in (workdir, *workdir.parents):
        manifest = d / "package.json"
        if manifest.is_file():
            names = _dependency_names(manifest)
            out = []
            for name in names[:MAX_DEPENDENCIES]:
                spec = _engines_node(d / "node_modules" / name / "package.json")
                if spec:
                    out.append((name, spec))
            return out
        if (d / ".git").exists():
            break
    return []


def _dependency_names(manifest: Path) -> list[str]:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    names: list[str] = []
    for field in ("dependencies", "devDependencies"):
        group = data.get(field)
        if isinstance(group, dict):
            # A name is a path below node_modules: one that climbs out is no package.
            names += [n for n in group if isinstance(n, str) and re.fullmatch(r"(@[\w.-]+/)?[\w.-]+", n)
                      and ".." not in n]
    return list(dict.fromkeys(names))


def _engines_node(manifest: Path) -> str | None:
    try:
        engines = json.loads(manifest.read_text(encoding="utf-8")).get("engines")
    except (OSError, ValueError, AttributeError):
        return None
    node = engines.get("node") if isinstance(engines, dict) else None
    return node.strip() if isinstance(node, str) and node.strip() else None


def on_path(env: Mapping[str, str]) -> tuple[int, ...] | None:
    """The version of the `node` a worker would run, or None when there is none
    or it does not answer."""
    binary = shutil.which("node", path=env.get("PATH", ""))
    if binary is None:
        return None
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return _version(proc.stdout.strip()) if proc.returncode == 0 else None


#: How long the project's own Python may take to say where it imports the project's package from.
PROBE_TIMEOUT_S = 10


class ProbeBroke(Exception):
    """The project's own Python could not be asked where it imports the package from."""


def package_elsewhere(workdir: Path) -> tuple[str, Path, str] | None:
    """A Python project whose `.venv` imports the project's own package from outside `workdir`: an
    editable install that points at the checkout while a worker edits a worktree (the worktree links
    the checkout's `.venv`). (the package, the directory here that holds it, where `.venv` imports it
    from); None for a project without `.venv`, a name, the package here, or one imported from here."""
    python = workdir / ".venv" / "bin" / "python"
    if not python.exists():
        return None
    try:
        name = tomllib.loads((workdir / "pyproject.toml").read_text())["project"]["name"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    package = re.sub(r"[-.]+", "_", str(name)).lower()
    root = next((r for r in (workdir / "src", workdir) if (r / package).is_dir()), None)
    if root is None:
        return None
    probe = f"import importlib.util as u; s = u.find_spec({package!r}); print(s.origin if s and s.origin else '')"
    try:
        proc = subprocess.run([str(python), "-c", probe], cwd=str(workdir), capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeBroke(str(exc)) from exc
    origin = proc.stdout.strip()
    if proc.returncode != 0 or not origin:
        return None
    try:
        Path(origin).resolve().relative_to(workdir.resolve())
    except ValueError:
        return package, root, origin
    return None


def env_for(workdir: Path, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """What a worker and the check run with here: `PATH` with the project's Node first, and
    `PYTHONPATH` with the project's own package here first when its `.venv` imports it from elsewhere
    (`package_elsewhere`), so a check tests the work and not the checkout it was cut from; `{}` when
    neither applies."""
    env = os.environ if env is None else env
    out: dict[str, str] = {}
    node = node_bin(workdir, env)
    if node is not None:
        out["PATH"] = os.pathsep.join([str(node.bin), env.get("PATH", "")])
    try:
        found = package_elsewhere(workdir)
    except ProbeBroke:
        found = None
    if found is not None:
        out["PYTHONPATH"] = os.pathsep.join([str(found[1]), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])])
    return out


def satisfies(version: tuple[int, ...], spec: str) -> bool:
    """Whether a version meets a spec as `.nvmrc` and `engines` write them: `22`,
    `v22.23.2`, `22.x`, `^22.12.0`, `~22.22`, `>=22.22.3`, `>=22 <24`, `a || b`.
    A name (`lts/*`, `node`) matches nothing: locks does not guess."""
    spec = _tight(spec)
    if "||" in spec:
        return any(satisfies(version, part) for part in spec.split("||"))
    terms = spec.split()
    return bool(terms) and all(_term(version, term) for term in terms)


def readable(spec: str) -> bool:
    """Whether every term of a spec is a version locks can compare."""
    alternatives = [part.split() for part in _tight(spec).split("||")]
    return all(alternatives) and all(_version(re.sub(r"^(>=|<=|>|<|\^|~|=)", "", term), partial=True) is not None
                                     for terms in alternatives for term in terms)


def _tight(spec: str) -> str:
    """`>= 18.0.0` as `>=18.0.0`: engines often put a space after the operator."""
    return re.sub(r"(>=|<=|>|<|=|\^|~)\s+", r"\1", spec.strip())


def _term(version: tuple[int, ...], term: str) -> bool:
    op = re.match(r"^(>=|<=|>|<|\^|~|=)?", term).group(0)
    parts = _version(term[len(op):], partial=True)
    if parts is None:
        return False
    padded = parts + (0,) * (3 - len(parts))
    if op == ">=":
        return version >= padded
    if op == ">":
        return version > padded
    if op == "<=":
        return version <= padded
    if op == "<":
        return version < padded
    if op == "^":
        return version[0] == parts[0] and version >= padded
    if op == "~":
        return version[:2] == padded[:2] and version >= padded
    return version[:len(parts)] == parts  # `22`, `22.23`, `22.23.2`, `22.x`


def _version(text: str, *, partial: bool = False) -> tuple[int, ...] | None:
    found = _PARTS.fullmatch(text.strip())
    if found is None:
        return None
    parts = []
    for group in found.groups():
        if group is None or group in ("x", "*"):
            break
        parts.append(int(group))
    if not partial and len(parts) < 3:
        return None
    return tuple(parts) or None
