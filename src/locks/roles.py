"""Roles: what a worker is, declared once and reused across projects.

A role is a Markdown file whose frontmatter says what it is for and what it
carries, and whose body is its instructions:

    ---
    name: debugger
    description: Finds the cause of a failure and fixes it with the smallest change
    kinds: [debug-repro, debug-unclear]
    skills: [engineering-baseline, ponytail]
    on_demand: [python-rules, typescript-rules]
    capabilities: [livespec]
    disallowed_tools: [WebSearch]
    ---
    Reproduce first. ...

They live in two places, the nearer winning by name:

- `user`: `<locks home>/roles/<name>.md`, the person's own, copied from a
  bundled one (`locks roles copy`) or written by hand;
- `bundled`: `roles/` beside this module, the team locks ships.

Never in a repository: a role decides what a worker is told and which of the
person's capabilities it gets, and a repository must not be able to change
that by committing a file. A role a person wrote that shadows a bundled one is
recorded as shadowing it; one that fails the checks is refused with its
reason and costs only itself.

A role is not a subagent. locks still runs each attempt as a one-shot worker
whose model, effort and tools it chooses per attempt: a role adds identity
and instructions, never a dial. So a role names no model, no effort and no
tool to add. It may take tools away (`disallowed_tools`), name skills (their
text goes into the worker's system prompt, `skills`, or is offered by path,
`on_demand`) and name capabilities, which come from the person's registry
and adopted neighbours only: a capability that is not there is not there.

`extends` builds one role on another: a field the child gives replaces the
parent's, and the child's body follows the parent's unless it says
`replace_prompt: true`. `match` makes a role a specialization: paths or globs
in the project (`pyproject.toml`, `**/*.rs`) that, when present, make it the
one picked among the roles of its kind.

Skills are folders with a `SKILL.md`, found by name, nearest first: the
person's `<locks home>/skills/`, the ones bundled with locks, then Claude
Code's own (`~/.claude/skills/`, and `plugin:skill` in an installed plugin).
"""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from locks.matrix import KINDS
from locks.store import home

SCOPES = ("user", "bundled")
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}(?::[a-z0-9][a-z0-9_.-]{0,63})?$")
#: A built-in tool's name. An MCP tool (`mcp__…`) is not one: capabilities
#: come from the person's registry, never from a role.
TOOL = re.compile(r"^[A-Z][A-Za-z]{1,40}$")
#: The kinds a role may serve: every routed kind but the classifier's own.
ROLE_KINDS = tuple(k for k in KINDS if k != "classify")
#: Lists a role may add to its parent's (`add_skills: [...]`) instead of replacing
#: them: the parent's list changes, and the role keeps following it.
ADDABLE = ("skills", "on_demand", "capabilities", "disallowed_tools", "match")
KEYS = frozenset({"name", "description", "kinds", "extends", "skills", "on_demand", "capabilities",
                  "disallowed_tools", "match", "replace_prompt", "derived_from", *(f"add_{k}" for k in ADDABLE)})
_ADDS = tuple(f"add_{k}" for k in ADDABLE)
LIST_KEYS = frozenset({"kinds", *ADDABLE, *_ADDS})
MAX_EXTENDS = 5
#: A skill's text is put in front of every attempt of its role: past this it is
#: offered by path instead, so one large skill cannot crowd out the task.
MAX_INLINE_CHARS = 24000
#: How many files a `match` glob reads before it stops looking.
MAX_MATCH_FILES = 20000


class RoleError(ValueError):
    """A role or a skill that cannot be used as asked; the message says why."""


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    kinds: tuple[str, ...]
    prompt: str
    skills: tuple[str, ...] = ()
    on_demand: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    match: tuple[str, ...] = ()
    extends: str | None = None
    derived_from: str | None = None
    scope: str = "bundled"
    path: str = ""
    #: The bundled role of the same name this one hides, when it does.
    shadows: str | None = None

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "kinds": list(self.kinds),
                "skills": list(self.skills), "on_demand": list(self.on_demand),
                "capabilities": list(self.capabilities), "disallowed_tools": list(self.disallowed_tools),
                "match": list(self.match), "extends": self.extends, "derived_from": self.derived_from,
                "scope": self.scope, "path": self.path, "shadows": self.shadows}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    folder: Path
    origin: str
    #: The installed plugin version it was read from, for an upstream plugin's copy.
    version: str = ""

    @property
    def source(self) -> str:
        return f"{self.origin}@{self.version}" if self.version else self.origin


@dataclass
class Catalog:
    """Every role locks can use, resolved, and the ones it refused."""
    roles: dict[str, Role] = field(default_factory=dict)
    #: name → why: a file that failed the checks costs only itself.
    refused: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> Role:
        if name in self.roles:
            return self.roles[name]
        if name in self.refused:
            raise RoleError(f"role {name!r} is refused: {self.refused[name]}")
        raise RoleError(f"no role {name!r}; known: {', '.join(sorted(self.roles)) or 'none'}")

    def bundled(self) -> list[Role]:
        """The roles locks ships, as the person sees them: a user role of the same
        name stands in for the bundled one it shadows."""
        return [r for r in self.roles.values() if r.scope == "bundled" or r.shadows]


# --- reading ---------------------------------------------------------------------


def bundled_dir() -> Path:
    return Path(__file__).resolve().parent / "roles"


def bundled_skills_dir() -> Path:
    return Path(__file__).resolve().parent / "skills"


def dirs() -> dict[str, Path]:
    return {"user": home() / "roles", "bundled": bundled_dir()}


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """A Markdown file's frontmatter, read as the small part of YAML roles and
    skills use (scalars, `[a, b]`, and `- item` lists), and its body."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise RoleError("the frontmatter is not closed with ---") from None
    meta: dict[str, Any] = {}
    current: str | None = None
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s*(.*)$", raw)
        if item and current is not None:
            if not isinstance(meta.get(current), list):
                meta[current] = []
            meta[current].append(_scalar(item.group(1)))
            continue
        pair = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", raw)
        if not pair:
            if current is not None and isinstance(meta.get(current), str) and raw.startswith((" ", "\t")):
                meta[current] = (meta[current] + " " + raw.strip()).strip()
                continue
            raise RoleError(f"frontmatter line not understood: {raw.strip()[:80]!r}")
        current, value = pair.group(1), pair.group(2).strip()
        if value.startswith("[") and value.endswith("]"):
            meta[current] = [_scalar(v) for v in value[1:-1].split(",") if v.strip()]
        elif value in ("", ">", "|", ">-", "|-"):
            meta[current] = ""
        else:
            meta[current] = _scalar(value)
    return meta, "\n".join(lines[end + 1:]).strip()


def _scalar(value: str) -> Any:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value in ("true", "false"):
        return value == "true"
    return value


def parse(text: str, *, scope: str, path: str = "", expected: str | None = None) -> dict[str, Any]:
    """One role file as raw fields, checked on its own (not against other roles)."""
    meta, body = split_frontmatter(text)
    unknown = sorted(set(meta) - KEYS)
    if unknown:
        raise RoleError(f"unknown field(s) {', '.join(unknown)}; a role takes {', '.join(sorted(KEYS))}")
    name = meta.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise RoleError("`name` is required: lowercase letters, digits, - and _")
    if expected is not None and name != expected:
        raise RoleError(f"`name` is {name!r} but the file is {expected}.md")
    for key in LIST_KEYS:
        value = meta.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = [value] if value else []
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise RoleError(f"`{key}` is a list of names")
        meta[key] = list(dict.fromkeys(value))
    for kind in meta.get("kinds", []):
        if kind not in ROLE_KINDS:
            raise RoleError(f"no kind {kind!r}; one of {', '.join(ROLE_KINDS)}")
    for skill in (*meta.get("skills", []), *meta.get("on_demand", []), *meta.get("add_skills", []),
                  *meta.get("add_on_demand", [])):
        if not SKILL_NAME.match(skill):
            raise RoleError(f"{skill!r} is no skill name")
    for tool in (*meta.get("disallowed_tools", []), *meta.get("add_disallowed_tools", [])):
        if not TOOL.match(tool):
            raise RoleError(f"{tool!r} is no built-in tool: a role takes tools away, and only built-in ones")
    for cap in (*meta.get("capabilities", []), *meta.get("add_capabilities", [])):
        if not NAME.match(cap):
            raise RoleError(f"{cap!r} is no capability name")
    for glob in (*meta.get("match", []), *meta.get("add_match", [])):
        if glob.startswith("/") or ".." in Path(glob).parts:
            raise RoleError(f"`match` {glob!r} must stay inside the project")
    if "replace_prompt" in meta and not isinstance(meta["replace_prompt"], bool):
        raise RoleError("`replace_prompt` is true or false")
    for key in ("description", "extends", "derived_from"):
        if key in meta and not isinstance(meta[key], str):
            raise RoleError(f"`{key}` is text")
    if meta.get("extends") == name:
        raise RoleError("a role cannot extend itself")
    meta["prompt"] = body
    meta["scope"], meta["path"] = scope, path
    return meta


def load(*, folders: Mapping[str, Path] | None = None) -> Catalog:
    """Every role in every scope, resolved through `extends`, nearest scope winning.
    Never raises: a bad file is refused with its reason."""
    folders = dict(folders) if folders is not None else dirs()
    raw: dict[str, dict[str, Any]] = {}
    bundled_names: set[str] = set()
    catalog = Catalog()
    for scope in reversed(SCOPES):
        folder = folders.get(scope)
        if folder is None or not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            try:
                fields = parse(path.read_text(encoding="utf-8"), scope=scope, path=str(path), expected=path.stem)
            except (OSError, UnicodeDecodeError, RoleError) as exc:
                catalog.refused[path.stem] = f"{path}: {exc}"
                continue
            if scope == "bundled":
                bundled_names.add(fields["name"])
            elif fields["name"] in raw:
                fields["shadows"] = raw[fields["name"]]["path"]
            raw[fields["name"]] = fields
            catalog.refused.pop(fields["name"], None)
    for name in sorted(raw):
        try:
            catalog.roles[name] = _resolve(name, raw, bundled=bundled_names)
        except RoleError as exc:
            catalog.refused[name] = f"{raw[name]['path']}: {exc}"
    return catalog


def _resolve(name: str, raw: Mapping[str, dict[str, Any]], *, bundled: set[str]) -> Role:
    chain: list[dict[str, Any]] = []
    current: str | None = name
    while current is not None:
        if any(c["name"] == current for c in chain):
            raise RoleError(f"`extends` goes round in a circle: {' → '.join([*(c['name'] for c in chain), current])}")
        if len(chain) > MAX_EXTENDS:
            raise RoleError(f"`extends` is more than {MAX_EXTENDS} deep")
        if current not in raw:
            raise RoleError(f"extends {current!r}, which is no role")
        fields = raw[current]
        chain.append(fields)
        parent = fields.get("extends")
        current = parent
    merged: dict[str, Any] = {}
    prompt = ""
    for fields in reversed(chain):
        for key, value in fields.items():
            if key not in ("prompt", "replace_prompt", "name", "scope", "path", "shadows", *_ADDS):
                merged[key] = value
        for key in ADDABLE:
            if fields.get(f"add_{key}"):
                merged[key] = list(dict.fromkeys([*merged.get(key, []), *fields[f"add_{key}"]]))
        body = fields.get("prompt", "")
        prompt = body if fields.get("replace_prompt") or not prompt else (prompt + "\n\n" + body if body else prompt)
    top = chain[0]
    if not merged.get("description"):
        raise RoleError("`description` is required: it says what the role is for")
    if not merged.get("kinds"):
        raise RoleError("`kinds` is required: the kinds of task this role takes")
    return Role(
        name=name, description=merged["description"], kinds=tuple(merged["kinds"]), prompt=prompt.strip(),
        skills=tuple(merged.get("skills", ())), on_demand=tuple(merged.get("on_demand", ())),
        capabilities=tuple(merged.get("capabilities", ())),
        disallowed_tools=tuple(merged.get("disallowed_tools", ())), match=tuple(merged.get("match", ())),
        extends=top.get("extends"), derived_from=top.get("derived_from"), scope=top["scope"], path=top["path"],
        shadows=top.get("shadows") if top.get("shadows") and name in bundled else None,
    )


#: The roles locks runs itself, outside every team: a person names one only to replace it.
INTERNAL = ("verifier", "surveyor")


def internal(name: str, catalog: Catalog | None = None) -> Role:
    """A role locks itself runs as, outside every team (the independent verifier).
    A role of the person's of the same name takes its place."""
    catalog = catalog or load()
    if name in catalog.roles and catalog.roles[name].scope == "user":
        return catalog.roles[name]
    path = Path(__file__).resolve().parent / "roles_internal" / f"{name}.md"
    fields = parse(path.read_text(encoding="utf-8"), scope="bundled", path=str(path), expected=name)
    return _resolve(name, {name: fields}, bundled={name})


# --- picking -----------------------------------------------------------------------


def kinds_of(roles: Iterable[Role]) -> tuple[str, ...]:
    """The kinds a set of roles covers, in the matrix's order."""
    covered = {k for r in roles for k in r.kinds}
    return tuple(k for k in ROLE_KINDS if k in covered)


def matches(role: Role, repo_dir: Path, files: Sequence[str] | None = None) -> int:
    """How many of a specialization's `match` entries the project holds."""
    hits = 0
    for pattern in role.match:
        if not any(c in pattern for c in "*?["):
            hits += (repo_dir / pattern).exists()
            continue
        listed = files if files is not None else project_files(repo_dir)
        hits += any(fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(Path(f).name, pattern) for f in listed)
    return hits


def project_files(repo_dir: Path) -> list[str]:
    """The project's files, relative: git's list when it is a checkout, else a
    bounded walk."""
    try:
        proc = subprocess.run(["git", "ls-files"], cwd=str(repo_dir), capture_output=True, text=True,
                              timeout=10, check=False)
        if proc.returncode == 0:
            return proc.stdout.splitlines()[:MAX_MATCH_FILES]
    except (OSError, subprocess.TimeoutExpired):
        pass
    out: list[str] = []
    for root, folders, names in os.walk(repo_dir):
        folders[:] = [d for d in folders if not d.startswith(".") and d != "node_modules"]
        for n in names:
            out.append(str(Path(root, n).relative_to(repo_dir)))
            if len(out) >= MAX_MATCH_FILES:
                return out
    return out


def pick(kind: str, roles: Sequence[Role], repo_dir: Path) -> tuple[Role | None, str]:
    """The role a task of `kind` runs as, among `roles` in the team's order, and why.

    A specialization whose `match` the project holds wins over the roles that
    match nothing; among equals the team's order decides, a person's choice and
    never the alphabet. A specialization that matches nothing is used only when
    nothing else takes the kind."""
    candidates = [r for r in roles if kind in r.kinds]
    if not candidates:
        return None, f"no role takes {kind}"
    files: list[str] | None = None
    scored: list[tuple[int, int, Role]] = []
    for i, role in enumerate(candidates):
        if role.match:
            if files is None and any(any(c in p for c in "*?[") for p in role.match):
                files = project_files(repo_dir)
            scored.append((matches(role, repo_dir, files), i, role))
        else:
            scored.append((0, i, role))
    specialized = [s for s in scored if s[0] > 0]
    if specialized:
        best = max(specialized, key=lambda s: (s[0], -s[1]))
        return best[2], f"{best[2].name} matches this project ({', '.join(best[2].match)})"
    general = [s for s in scored if not s[2].match]
    chosen = (general or scored)[0][2]
    why = "the role that takes " + kind if len(candidates) == 1 else f"first of {len(candidates)} that take {kind}"
    return chosen, why


# --- skills ------------------------------------------------------------------------


def claude_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    configured = env.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path(env.get("HOME", "~")).expanduser() / ".claude"


def skill_folders(name: str, env: Mapping[str, str] | None = None) -> list[tuple[str, Path]]:
    """Where a skill of this name may be, nearest first, as (origin, folder)."""
    claude = claude_dir(env)
    if ":" in name:
        plugin, skill = name.split(":", 1)
        from locks import plugins

        return [("plugin", root / "skills" / skill) for root in plugins.versions(os.environ if env is None else env,
                                                                                    plugin)]
    return [("user", home() / "skills" / name), ("bundled", bundled_skills_dir() / name),
            ("claude", claude / "skills" / name)]


#: Bundled skills whose upstream authors publish them as a Claude Code plugin:
#: name -> (plugin, skill). When the person has that plugin installed, its own
#: `SKILL.md` (newest version, read from the plugin cache, never a repository) takes
#: the place of the vendored copy, which stays the fallback.
UPSTREAM: dict[str, tuple[str, str]] = {"ponytail": ("ponytail", "ponytail")}


def _read_skill(name: str, folder: Path, origin: str, version: str = "", *, strict: str = "") -> Skill | None:
    """A skill's file read; with `strict`, one with no body or another `name` is none."""
    path = folder / "SKILL.md"
    try:
        if not path.is_file():
            return None
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, RoleError):
        return None
    if strict and (not body.strip() or meta.get("name", strict) != strict):
        return None
    description = meta.get("description") if isinstance(meta.get("description"), str) else ""
    return Skill(name, description, body, folder, origin, version)


def upstream_skill(name: str, env: Mapping[str, str] | None = None) -> Skill | None:
    """The installed upstream plugin's copy of a bundled skill, when it is there and
    well formed (frontmatter that parses, a body, its `name` the skill's if given)."""
    if name not in UPSTREAM:
        return None
    plugin, skill_name = UPSTREAM[name]
    from locks import plugins

    for root in plugins.versions(os.environ if env is None else env, plugin)[:1]:
        return _read_skill(name, root / "skills" / skill_name, "plugin", root.name, strict=skill_name)
    return None


def find_skill(name: str, env: Mapping[str, str] | None = None) -> Skill | None:
    for origin, folder in skill_folders(name, env):
        if origin == "bundled" and (installed := upstream_skill(name, env)) is not None:
            return installed
        if (skill := _read_skill(name, folder, origin)) is not None:
            return skill
    return None


def bundled_skills() -> list[Skill]:
    out = []
    folder = bundled_skills_dir()
    for path in sorted(folder.glob("*/SKILL.md")) if folder.is_dir() else []:
        skill = find_skill(path.parent.name)
        if skill is not None:
            out.append(skill)
    return out


# --- what a worker is handed --------------------------------------------------------


@dataclass(frozen=True)
class Briefing:
    """A role turned into what one attempt gets."""
    role: Role
    system_prompt: str
    #: Skill folders the worker reads: they become read rules, never write.
    read_dirs: tuple[Path, ...] = ()
    skills: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    #: Where each skill found came from: origin, and the plugin version for one
    #: read from an installed upstream plugin (`ponytail` -> `plugin@5.3.2`).
    sources: Mapping[str, str] = field(default_factory=dict)


#: How many hex digits of its hash name a version of a role's text.
VERSION_CHARS = 12


def skill_sources(role: Role, env: Mapping[str, str] | None = None, *,
                  held: frozenset[str] | None = None) -> dict[str, str]:
    """Where each skill the role carries or offers here is read from on this machine."""
    out = {}
    for name in dict.fromkeys(n for n in (*role.skills, *role.on_demand) if fits(n, held)):
        skill = find_skill(name, env)
        out[name] = skill.source if skill is not None else "missing"
    return out


def version(role: Role, env: Mapping[str, str] | None = None) -> str:
    """Which version of its text a role is: a short hash of its resolved instructions (name,
    description, its own text after `extends`) and the names of the skills it carries and
    offers. A skill's text and where it lives on this machine are left out: the same role
    copied to another machine is the same version. A skill read from an installed upstream
    plugin is another text, so its plugin and version are in the hash (a role's trial then
    compares like with like); with none, the hash is what it always was."""
    fields: list[Any] = [role.name, role.description, role.prompt, list(role.skills), list(role.on_demand)]
    upstream = {n: s for n, s in skill_sources(role, env).items() if s.startswith("plugin@") and n in UPSTREAM}
    if upstream:
        fields.append(sorted(upstream.items()))
    payload = json.dumps(fields, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:VERSION_CHARS]


#: The files a language's rules skill is about, by the skill's name (issue #123): a rules skill of
#: a language the repository holds no file of is not listed in its workers' briefs.
LANGUAGE_FILES: dict[str, tuple[str, ...]] = {
    "python-rules": (".py", ".pyi"), "typescript-rules": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    "go-rules": (".go",), "rust-rules": (".rs",), "java-rules": (".java",), "kotlin-rules": (".kt", ".kts"),
    "csharp-rules": (".cs",), "ruby-rules": (".rb", ".rake"), "php-rules": (".php",), "swift-rules": (".swift",),
    "lua-rules": (".lua",), "cpp-rules": (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp"),
    "bash-rules": (".sh", ".bash"), "sql-rules": (".sql",),
}


def languages(repo_dir: Path | None) -> frozenset[str] | None:
    """The file suffixes git tracks in a repository (or the folders right inside one that holds
    several); None when it cannot tell, and then every rules skill is offered."""
    import subprocess

    if repo_dir is None:
        return None
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=repo_dir, capture_output=True, text=True,
                             timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return frozenset(Path(f).suffix.lower() for f in out.stdout.split("\0") if Path(f).suffix)


def fits(name: str, held: frozenset[str] | None) -> bool:
    """Whether a skill (carried or on demand) applies to a repository holding files of `held` suffixes."""
    wants = LANGUAGE_FILES.get(name)
    return held is None or wants is None or any(w in held for w in wants)


def brief(role: Role, env: Mapping[str, str] | None = None, *,
          held: frozenset[str] | None = None) -> Briefing:
    """The role's instructions and the text of its skills, for the worker's system
    prompt. A skill that is not found is named as missing; it never stops a run."""
    parts = [f"You work as the `{role.name}` role: {role.description}"]
    if role.prompt:
        parts.append(role.prompt)
    carried, offered, missing, read_dirs = [], [], [], []
    sources: dict[str, str] = {}
    for name in role.skills:
        if not fits(name, held):
            continue  # a language's rules where the repository holds none of its files
        skill = find_skill(name, env)
        if skill is None:
            missing.append(name)
            continue
        sources[name] = skill.source
        read_dirs.append(skill.folder)
        if len(skill.body) > MAX_INLINE_CHARS:
            offered.append(skill)
            continue
        carried.append(skill)
        others = sorted(p.name for p in skill.folder.iterdir() if p.name != "SKILL.md") if skill.folder.is_dir() \
            else []
        where = f" Its other files are in {skill.folder}: {', '.join(others)}." if others else ""
        parts.append(f"## Skill `{name}`\n{where.strip()}\n\n{skill.body}".replace("\n\n\n", "\n\n"))
    for name in role.on_demand:
        if name in role.skills or not fits(name, held):
            continue  # carried whole already, or a language the repository holds none of
        skill = find_skill(name, env)
        if skill is None:
            missing.append(name)
            continue
        sources[name] = skill.source
        read_dirs.append(skill.folder)
        offered.append(skill)
    if offered:
        parts.append("Skills to read when they apply: read the file before the work it is about, not after.\n"
                     + "\n".join(f"- `{s.name}`: {s.description or 'no description'} — {s.folder / 'SKILL.md'}"
                                 for s in offered))
    return Briefing(role, "\n\n".join(parts), tuple(dict.fromkeys(read_dirs)),
                    tuple(s.name for s in carried), tuple(missing), sources)


def read_rules(briefing: Briefing) -> tuple[str, ...]:
    """Read rules for the skill folders, one per root they share (locks' own, the
    person's), else one per folder. An absolute path takes two slashes."""
    roots = (bundled_skills_dir().resolve(), (home() / "skills").resolve())
    out: list[str] = []
    for folder in briefing.read_dirs:
        folder = folder.resolve()
        base = next((r for r in roots if folder.is_relative_to(r)), folder)
        out.append(f"Read(/{base}/**)")
    return tuple(dict.fromkeys(out))


# --- writing (a person's commands) -----------------------------------------------------


def copy(source: str, new: str, *, catalog: Catalog | None = None, kinds: Sequence[str] | None = None,
         description: str | None = None) -> Path:
    """A role copied into the person's scope to edit: the resolved fields and body
    of `source`, under a new name (or the same, to shadow it)."""
    catalog = catalog or load()
    role = catalog.get(source)
    if not NAME.match(new):
        raise RoleError(f"{new!r} is no role name: lowercase letters, digits, - and _")
    target = dirs()["user"] / f"{new}.md"
    if target.exists():
        raise RoleError(f"{target} already exists; edit it, or pick another name")
    if new != source and new in catalog.roles:
        raise RoleError(f"there is already a role {new!r}")
    for kind in kinds or ():
        if kind not in ROLE_KINDS:
            raise RoleError(f"no kind {kind!r}; one of {', '.join(ROLE_KINDS)}")
    role = replace(role, name=new, kinds=tuple(kinds) if kinds else role.kinds,
                   description=description or role.description)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(role, derived_from=source), encoding="utf-8")
    return target


def new(name: str, *, extends: str, description: str | None = None, kinds: Sequence[str] = (),
        skills: Sequence[str] = (), on_demand: Sequence[str] = (), capabilities: Sequence[str] = (),
        disallowed_tools: Sequence[str] = (), match: Sequence[str] = (), prompt: str = "",
        catalog: Catalog | None = None) -> Path:
    """A role of the person's built on another: it keeps its parent's
    instructions and lists, follows them as they change, and adds what is given
    here (`add_skills` and the like)."""
    catalog = catalog or load()
    parent = catalog.get(extends)
    if not NAME.match(name):
        raise RoleError(f"{name!r} is no role name: lowercase letters, digits, - and _")
    target = dirs()["user"] / f"{name}.md"
    if target.exists() or name in catalog.roles:
        raise RoleError(f"there is already a role {name!r}")
    lines = ["---", f"name: {name}",
             f"description: {description or parent.description + (' (' + ', '.join(match) + ')' if match else '')}",
             f"extends: {extends}"]
    if kinds:
        lines.append(f"kinds: [{', '.join(kinds)}]")
    for key, values in (("skills", skills), ("on_demand", on_demand), ("capabilities", capabilities),
                        ("disallowed_tools", disallowed_tools), ("match", match)):
        if values:
            lines.append(f"add_{key}: [{', '.join(dict.fromkeys(values))}]")
    text = "\n".join([*lines, "---", "", prompt.strip(), ""])
    parse(text, scope="user", expected=name)  # the same bar as every role, before anything is written
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    if name in load().refused:
        why = load().refused[name]
        target.unlink()
        raise RoleError(why)
    return target


def render(role: Role, *, derived_from: str | None = None) -> str:
    def items(values: Sequence[str]) -> str:
        return "[" + ", ".join(values) + "]"

    lines = ["---", f"name: {role.name}", f"description: {role.description}", f"kinds: {items(role.kinds)}"]
    for key in ("skills", "on_demand", "capabilities", "disallowed_tools", "match"):
        values = getattr(role, key)
        if values:
            lines.append(f"{key}: {items(values)}")
    if derived_from or role.derived_from:
        lines.append(f"derived_from: {derived_from or role.derived_from}")
    lines += ["---", "", role.prompt, ""]
    return "\n".join(lines)


def remove(name: str) -> Path:
    """A person's own role removed. A bundled role is never removed."""
    target = dirs()["user"] / f"{name}.md"
    if not target.is_file():
        raise RoleError(f"no role of yours named {name!r}: a bundled role is not removed")
    with contextlib.suppress(OSError):
        target.unlink()
    return target


#: Words in an agent's name or description, and the kinds a role made of it serves; the first
#: that matches wins, and an agent none matches builds (`implement`, `feature`).
_AGENT_KINDS = (
    (re.compile(r"\b(review\w*|audit\w*|critic\w*)\b", re.I), ("review-routine", "review-critical")),
    (re.compile(r"\b(test\w*|qa|e2e)\b", re.I), ("test",)),
    (re.compile(r"\b(debug\w*|troubleshoot\w*|incident\w*|bug\s*fix\w*)\b", re.I), ("debug-repro", "debug-unclear")),
    (re.compile(r"\b(doc\w*|writer|technical writ\w*)\b", re.I), ("docs",)),
    (re.compile(r"\b(architect\w*|plan\w*|design\s+system\w*)\b", re.I), ("plan",)),
    (re.compile(r"\b(refactor\w*|clean\s*up|legacy|moderni\w*)\b", re.I), ("refactor",)),
    (re.compile(r"\b(ui|ux|front-?end|css|react|vue|angular|svelte)\b", re.I), ("ui", "feature")),
    (re.compile(r"\b(research\w*|explor\w*|analy\w*|search\w*)\b", re.I), ("explore",)),
)
#: The built-in tools an agent's `tools` list can leave out, and a role then takes away.
_TAKEABLE = ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch")
#: The most characters of an agent's description a role keeps (examples and all go).
_AGENT_DESCRIPTION = 240


def from_agent(text: str, *, fallback: str) -> tuple[str, str] | None:
    """A Claude Code agent file as a role of the person's: (name, the role's file text), or None
    when it is no agent. Its description and instructions are kept; its `model` is dropped (the
    ladder chooses); its `tools`, which a role cannot add, become the built-in tools it takes
    away; its kinds are read from its name and description."""
    from locks.text import clip

    try:
        meta, body = split_frontmatter(text)
    except RoleError:
        return None
    if not meta or not body.strip():
        return None
    raw = str(meta.get("name") or fallback).lower()
    name = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-_")[:64]
    if not name or not NAME.match(name):
        return None
    said = " ".join(str(meta.get("description") or "").replace("\\n", " ").split())
    said = re.split(r"<example>|Examples?:", said, maxsplit=1)[0].strip() or f"imported agent {name}"
    description = clip(said, _AGENT_DESCRIPTION, one_line=True)
    found = f"{name.replace('-', ' ')} {description}"
    kinds = next((k for pattern, k in _AGENT_KINDS if pattern.search(found)), ("implement", "feature"))
    tools = meta.get("tools")
    listed = {t.strip() for t in (tools.split(",") if isinstance(tools, str) else tools or ()) if str(t).strip()}
    takes = [t for t in _TAKEABLE if listed and t not in listed]
    lines = ["---", f"name: {name}", f"description: {description}", f"kinds: [{', '.join(kinds)}]"]
    if takes:
        lines.append(f"disallowed_tools: [{', '.join(takes)}]")
    lines += [f"derived_from: agent:{fallback}", "---", "", body.strip(), ""]
    return name, "\n".join(lines)


def import_agents(source: Path, *, dry_run: bool = False) -> tuple[list[tuple[str, Path]], list[tuple[str, str]]]:
    """Claude Code agents (one file, or a folder of them, read only) as roles in the person's
    locks home: (the roles written, as name and file; the files skipped, with why). A role of
    the same name is never replaced. A role is used only where a team names it, so importing
    one changes no worker until a team takes it (`locks team change … --add`)."""
    files = [source] if source.is_file() else sorted(source.rglob("*.md")) if source.is_dir() else []
    if not files:
        raise RoleError(f"no agent file at {source}")
    catalog = load()
    written: list[tuple[str, Path]] = []
    skipped: list[tuple[str, str]] = []
    for path in files:
        try:
            made = from_agent(path.read_text(encoding="utf-8", errors="replace"), fallback=path.stem)
        except OSError as exc:
            skipped.append((str(path), f"unreadable: {exc}"))
            continue
        if made is None:
            skipped.append((str(path), "no agent: no frontmatter or no instructions"))
            continue
        name, text = made
        target = dirs()["user"] / f"{name}.md"
        if name in catalog.roles or target.exists() or any(n == name for n, _ in written):
            skipped.append((str(path), f"a role {name!r} exists already"))
            continue
        try:
            parse(text, scope="user", expected=name)
        except RoleError as exc:
            skipped.append((str(path), str(exc)))
            continue
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        written.append((name, target))
    return written, skipped
