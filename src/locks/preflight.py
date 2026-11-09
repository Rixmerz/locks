"""Whether a task's check can run here at all, read before any worker is spent on it.

A check that cannot start (a program missing from the PATH it runs with, a script its
package.json lacks, a Node no installed one satisfies) fails after a worker already ran,
and says nothing about the work. The preflight reads the same command at plan time, in
the directory it will run in, with the PATH it will run with (`runtime.env_for`), and
names what would stop it with the exact line that fixes it. No model, no network, a few
file reads and at most one short process.

It fails open: what it cannot tell (a substitution, a `cd`, a workspace flag, a program
or script the request itself may add) is said, never a stop. Ports are not its business:
`locks.services` already leaves a port that answers alone and stops a task whose service
never listens.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from locks import ci, runtime

#: Words a shell runs itself, or that open a construct whose program comes later.
_SHELL = frozenset({
    "cd", "set", "export", "unset", "test", "[", "[[", "true", "false", "echo", "printf", "exit", ":",
    "source", ".", "if", "then", "else", "elif", "fi", "for", "do", "done", "while", "until", "case",
    "esac", "!", "time", "exec", "command", "builtin", "eval", "return", "shift", "read", "wait",
    "trap", "ulimit", "umask", "pushd", "popd", "alias", "type", "hash", "local", "declare",
})
_NODE_TOOLS = frozenset({"node", "npm", "npx", "pnpm", "yarn"})
#: `npm run x`, `pnpm run x`, `yarn run x`, `bun run x`, and npm's own `test`, which needs a script.
_RUN = re.compile(r"\b(npm|pnpm|yarn|bun)\s+run(?:-script)?\s+([\w:.-]+)|\bnpm\s+(?:test|t)\b(?![\w:.-])")
#: Flags that run a script of another package than the one locks reads.
_ELSEWHERE = re.compile(r"(?:^|\s)(?:-w|--workspace|--workspaces|--prefix|-C|--dir|--filter|-F|--cwd)(?:[\s=]|$)")
_OPAQUE = re.compile(r"\$\(|`|\$\{?[A-Za-z_]|<\(")
IMPORT_TIMEOUT_S = 10


@dataclass(frozen=True)
class Blocker:
    """What would stop the check from running at all, and the line that fixes it."""
    what: str
    fix: str

    def line(self) -> str:
        return f"{self.what}; fix: {self.fix}"


@dataclass
class Result:
    blockers: list[Blocker] = field(default_factory=list)
    #: What it could not tell, said in the plan and never a stop.
    unknown: list[str] = field(default_factory=list)
    #: What it found that may not stop the check but makes it check something else.
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers

    def reason(self) -> str:
        """The stop's reason: every blocker, each with its fix."""
        return "the check cannot run here, so no worker was started (nothing ran, no attempt counted): " + \
            "; ".join(b.line() for b in self.blockers)

    def plan_lines(self) -> list[str]:
        lines = [f"preflight: {w}" for w in self.warnings]
        if self.unknown:
            lines.append("preflight could not tell, and goes on: " + "; ".join(dict.fromkeys(self.unknown)))
        return lines


def _path(env: Mapping[str, str], workdir: Path) -> str:
    return {**env, **runtime.env_for(workdir, env)}.get("PATH", "")


def _names(text: str, word: str) -> bool:
    """Whether a request names a program or script as a word of its own."""
    return bool(re.search(rf"(?<![\w:.-]){re.escape(word)}(?![\w:-])", text))


def _stages(text: str) -> list[str]:
    return [stage for part in ci._parts(text) for stage in ci._pipeline(part)]


def _program(stage: str) -> str | None:
    """The program a stage starts, or None when a shell word or nothing readable starts it."""
    try:
        words = shlex.split(stage, comments=True)
    except ValueError:
        return None
    words = [w for w in words if not ci._ENV_ASSIGN.match(w)]
    if not words:
        return None
    # A subshell's or a group's brackets are the shell's, not the program's name.
    first = words[0].lstrip("({").rstrip(");}")
    if not first and len(words) > 1:
        first = words[1].rstrip(");}")
    if not first or first in _SHELL or first[:1] in "<>0123456789&" or _OPAQUE.search(first):
        return None
    return first


def _declared(workdir: Path) -> set[str]:
    """The packages package.json depends on: their tools appear once they are installed."""
    pkg = ci._package(workdir)
    names: set[str] = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        if isinstance(pkg.get(key), dict):
            names.update(pkg[key])
    return names


def _missing_program(name: str, workdir: Path, path: str) -> Blocker:
    venv = workdir / ".venv" / "bin" / name
    local = workdir / "node_modules" / ".bin" / name
    if name == "python" and shutil.which("python3", path=path):
        fix = "write `python3` (or `.venv/bin/python`, or `uv run python`) in --verify"
    elif venv.exists():
        fix = f"write `.venv/bin/{name}` (or `uv run {name}`) in --verify"
    elif local.exists():
        fix = f"write `npx {name}` in --verify, or run it through a package.json script"
    else:
        fix = f"install `{name}` so it is on the PATH, or name the program the project has in --verify"
    return Blocker(f"`{name}` is not on the PATH the check runs with (exit 127)", fix)


def _programs(command: str, workdir: Path, path: str, asked: str, result: Result,
              installing: bool = False) -> set[str]:
    """Check every program the command (and each script it names) starts; return the names seen."""
    seen: set[str] = set()
    local = os.pathsep.join([str(workdir / "node_modules" / ".bin"), path])
    declared: set[str] | None = None
    for depth, text in enumerate(ci.runs(command, workdir)):
        moves = bool(re.search(r"(?:^|[\s;&|(])(?:cd|pushd)\s", text))
        for stage in _stages(text):
            name = _program(stage)
            if name is None:
                if _OPAQUE.search(stage.split(maxsplit=1)[0] if stage.split() else ""):
                    result.unknown.append(f"a program named by a substitution in `{stage}`")
                continue
            seen.add(Path(name).name)
            if "/" in name:
                where = Path(name) if Path(name).is_absolute() else workdir / name
                if where.exists() or moves:
                    continue
                if not installing and any(part in ("node_modules", ".venv", "venv") for part in Path(name).parts):
                    result.blockers.append(Blocker(f"`{name}` does not exist in the task's directory",
                                                   "install the project's dependencies there (`uv sync`, "
                                                   "`npm ci`), or name a program that exists in --verify"))
                else:
                    result.unknown.append(f"`{name}` does not exist yet (the work may write it)")
                continue
            if shutil.which(name, path=local if depth else path):
                continue
            if _names(asked, name):
                result.unknown.append(f"`{name}` is not on the PATH, and the request names it")
            elif depth and installing:
                result.unknown.append(f"`{name}` is not installed yet (the worker installs the dependencies)")
            elif depth and (declared := _declared(workdir) if declared is None else declared):
                # A package's tool is named by its `bin`, not by the package: one may provide it.
                result.unknown.append(f"`{name}`, run by a script, is not installed, and package.json has "
                                      "dependencies that may provide it")
            else:
                result.blockers.append(_missing_program(name, workdir, path))
    return seen


def _scripts(command: str, workdir: Path, asked: str, result: Result) -> bool:
    """Check every script the command names; return whether it names one."""
    named = False
    # A request that runs a script (it quotes the check: `npm run build`) is no request that adds it:
    # only a mention outside an invocation may be the work's to add (#161: a `build` the request only
    # ran spent a whole attempt before the check said it was missing).
    asked = ci._DENO_TASK.sub(" ", _RUN.sub(" ", asked))
    npm, deno = ci._package_scripts(workdir), ci._deno_tasks(workdir)
    has_pkg, has_deno = bool(ci._package(workdir)), bool(ci._deno(workdir)[0])
    for text in ci.runs(command, workdir):
        moves = bool(re.search(r"(?:^|[\s;&|(])(?:cd|pushd)\s", text))
        for part in ci._parts(text):
            elsewhere = moves or bool(_ELSEWHERE.search(part))
            found = [(m.group(1) or "npm", m.group(2) or "test") for m in _RUN.finditer(part)]
            found += [("deno", m.group(1)) for m in ci._DENO_TASK.finditer(part)]
            for runner, name in found:
                named = True
                if elsewhere:
                    result.unknown.append(f"`{runner} {name}` runs in another package than this directory's")
                    continue
                table, has = (deno, has_deno) if runner == "deno" else (npm, has_pkg)
                file = "deno.json" if runner == "deno" else "package.json"
                if not has:
                    result.unknown.append(f"no {file} here for `{runner} {name}`")
                    continue
                if name in table:
                    continue
                if _names(asked, name):
                    result.unknown.append(f"{file} has no `{name}` yet, and the request names it")
                    continue
                # The scripts whose name holds it (`build-qa` for `build`) first: the one meant is likely there.
                near = sorted(s for s in table if name in s or s in name)
                shown = ", ".join([*near, *sorted(s for s in table if s not in near)]) or "none"
                verb = "deno task" if runner == "deno" else f"{runner} run"
                result.blockers.append(Blocker(
                    f"`{verb} {name}`: {file} has no `{name}` (it has: {shown})",
                    f"name one it has in --verify, or add `{name}` to {file}'s "
                    f"{'tasks' if runner == 'deno' else 'scripts'}"))
    return named


def _node(workdir: Path, env: Mapping[str, str], path: str, result: Result) -> None:
    """A Node the project declares that neither nvm nor the PATH has."""
    found = runtime.wanted(workdir)
    if found is None or runtime.node_bin(workdir, env) is not None:
        return
    spec, source = found
    if not runtime.readable(spec):
        result.unknown.append(f"the Node {source} asks for ({spec!r}) is no version locks compares")
        return
    current = runtime.on_path({**env, "PATH": path})
    if current is None:
        result.unknown.append("the PATH's Node did not answer")
        return
    if runtime.satisfies(current, spec):
        return
    result.blockers.append(Blocker(
        f"{source} asks for Node {spec}, the PATH's is {'.'.join(map(str, current))} and nvm has none that fits",
        f"`nvm install {spec}`"))


def _python_package(workdir: Path, seen: set[str], result: Result) -> None:
    """A Python project whose `.venv` imports its own package from somewhere else (an editable install
    that points at the checkout, read from a worktree): the check would test that code, not the
    task's. locks puts the package here first on PYTHONPATH for the worker and the check
    (`runtime.env_for`), and says so; a test runner's own path settings may still win."""
    if not seen & {"python", "python3", "pytest", "uv", "coverage", "tox", "nox"}:
        return
    try:
        found = runtime.package_elsewhere(workdir)
    except runtime.ProbeBroke:
        result.unknown.append("where `.venv` imports the project's package from")
        return
    if found is not None:
        package, root, origin = found
        result.warnings.append(f"`.venv` here imports `{package}` from {origin}, not from this directory: {root} "
                               "goes first on PYTHONPATH for the worker and the check, so they import the work here")


#: `python -m tool` and `python3 -m tool`: the module the interpreter must import before anything runs.
_MODULE_RUN = re.compile(r"^(?P<py>\S*python[\d.]*)\s+(?:-[A-Za-z]+\s+)*-m\s+(?P<mod>[A-Za-z_][\w.]*)")


def _modules(command: str, workdir: Path, path: str, asked: str, result: Result, installing: bool) -> None:
    """A `python -m X` whose interpreter cannot import `X` fails before any test runs (`No module named
    X`, issue: the smoke run's system python had no pytest). Read with the interpreter itself, from the
    task's directory, so the project's own packages are found; a module the request names or the worker
    installs, an interpreter it cannot ask, or an answer it cannot read is said, never a stop."""
    for stage in _stages(command):
        try:
            words = [w for w in shlex.split(stage, comments=True) if not ci._ENV_ASSIGN.match(w)]
        except ValueError:
            continue
        found = _MODULE_RUN.match(" ".join(words))
        if not found:
            continue
        python, module = found["py"], found["mod"].split(".")[0]
        exe = str(workdir / python) if "/" in python and not Path(python).is_absolute() else \
            (python if "/" in python else shutil.which(python, path=path))
        if not exe or not Path(exe).exists():
            continue  # a missing interpreter is `_programs`' to say
        if _names(asked, module) or installing:
            result.unknown.append(f"whether `{python}` imports `{module}` (the work may install it)")
            continue
        probe = f"import importlib.util as u, sys; sys.exit(0 if u.find_spec({module!r}) else 3)"
        try:
            proc = subprocess.run([exe, "-c", probe], cwd=str(workdir), capture_output=True, text=True,
                                  timeout=IMPORT_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError):
            result.unknown.append(f"whether `{python}` imports `{module}`")
            continue
        if proc.returncode == 3:
            venv = workdir / ".venv" / "bin" / "python"
            fix = (f"write `.venv/bin/python -m {found['mod']}` in --verify" if venv.exists() and exe != str(venv)
                   else f"install `{module}` for `{python}` (`{python} -m pip install {module}`, `uv add --dev "
                   f"{module}`), or name an interpreter that has it in --verify")
            result.blockers.append(Blocker(f"`{python}` here has no module `{module}` (No module named {module})",
                                           fix))
        elif proc.returncode != 0:
            result.unknown.append(f"whether `{python}` imports `{module}`")


#: A check that runs through a container stack named after its folder (issue #119).
_CONTAINER_CHECK = re.compile(r"(^|[\s;&|(])(\./vendor/bin/sail|sail|docker[ -]compose|docker\s+compose|"
                              r"podman[ -]compose|make\s+up)\b")
_COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")


def _container(command: str, workdir: Path, result: Result) -> None:
    """A warning when the check runs through a container stack in a worktree: the stack the
    person runs is named after their checkout's folder, and a worktree has none up (nor its
    ignored `.env`). Said, never a stop: a stack may well start here."""
    if not _CONTAINER_CHECK.search(command) or not (workdir / ".git").is_file():
        return
    if any((workdir / name).is_file() for name in _COMPOSE_FILES):
        result.warnings.append(
            f"`{command}` runs through a container stack, and this is a worktree: the stack that is up is "
            "named after the person's checkout, and a worktree has neither it nor its ignored `.env`. If the "
            "check can only run there, send the task with --no-isolate")


def check(command: str, workdir: Path, *, asked: str = "", env: Mapping[str, str] | None = None,
          installing: bool = False) -> Result:
    """What would keep `command` from running in `workdir`, read without running it. `asked` is the
    request's text: a program or script it names may be the work's to add; `installing`, that the worker
    installs the dependencies first (the worktree's were not linked), so a missing one is no stop."""
    env = os.environ if env is None else env
    result = Result()
    if not command.strip():
        return result
    path = _path(env, workdir)
    try:
        seen = _programs(command, workdir, path, asked, result, installing)
        named = _scripts(command, workdir, asked, result)
        if named or seen & _NODE_TOOLS:
            _node(workdir, env, path, result)
        _modules(command, workdir, path, asked, result, installing)
        _python_package(workdir, seen, result)
        _container(command, workdir, result)
    except Exception as exc:  # a preflight that breaks is one that could not tell
        result.blockers.clear()
        result.unknown.append(f"the preflight itself failed ({type(exc).__name__})")
    return result
