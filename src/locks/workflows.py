"""Workflows: saved chains of locks tasks that advance on their own.

A workflow is a definition — named steps, each a task text with what it needs
before it — kept as JSON in one of three places, the nearest winning:

- `project`: `<checkout>/.locks/workflows/<name>.json`, beside the project's code
- `user`: `<locks home>/workflows/<name>.json`, the person's own
- `bundled`: the templates that ship with locks

A run of one is not driven by a session. Its first steps are queued as
ordinary tasks; when a step passes, the engine itself (`advance`, called at the
end of every run) queues the steps that were waiting on it, each starting its
worktree from the commit the step before it kept, and starts the repository's
dispatcher. Every step keeps locks' own guarantees: its cell, its check, its
escalation, its stops. A step that stops for a person stops the run there until
it is resumed, retried or the run is cancelled; resuming it to a pass goes on.

The session that started a run hears of it once at its end, or when a step
waits on the person: the endings of steps the engine went past are marked
reported. A watcher reads where a run is with `status`, and changes nothing.

A definition grants nothing: no permission rule, no approval, no capability.
Those stay a person's flags on `locks workflow run`, so a workflow committed
to a repository runs with exactly what the person running it gives.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from locks import after, dispatch, isolate, ledger, project, repo, surfaces, trunks
from locks import findings as findings_mod
from locks import specs as specs_mod
from locks.escalate import MAX_TURNS_CEILING
from locks.matrix import KINDS, READ_ONLY_KINDS, Cell
from locks.store import Store, home
from locks.text import clip

SCOPES = ("project", "user", "bundled")
#: Where a new or copied workflow goes when the caller does not say.
DEFAULT_SCOPE = "user"
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
INPUT = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
#: An input's place in a step's `verify`, a shell command: `${name}` is the shell's own
#: variable, never an input, and `{a,b}` or `{}` never match.
CHECK_INPUT = re.compile(r"(?<!\$)\{([a-z][a-z0-9_]{0,31})\}")
TOP_KEYS = frozenset({"name", "description", "inputs", "steps", "version", "requires"})
#: What a step may say. Nothing here widens what a worker may do: rules,
#: approvals and capabilities are the person's flags on `run`, never a file's.
STEP_KEYS = frozenset({"id", "title", "prompt", "kind", "needs", "verify", "start", "max_turns",
                       "memory", "role", "surfaces", "ledger", "fanout", "integrate", "trunk",
                       "when", "ask", "options", "workflow", "with"})
#: A question step (`ask`) waits on the person and a sub-workflow step (`workflow`) runs another
#: saved workflow: neither runs a worker of its own, so neither takes a worker's fields.
ASK_KEYS = frozenset({"id", "title", "needs", "when", "ask", "options"})
SUB_KEYS = frozenset({"id", "title", "needs", "when", "workflow", "with"})
#: What a step's `when` may ask of a step it needs, read from that step's ending, never from a
#: worker's words: whether the work it judged had findings the change caused (`findings`, `clean`),
#: whether it changed files (`changed`, `unchanged`), or, of a question step, the option chosen.
OUTCOMES = ("findings", "clean", "changed", "unchanged")
#: A question's options: two to `ASK_OPTIONS`, each a short lowercase word or phrase.
ASK_OPTIONS = 6
OPTION = re.compile(r"^[a-z0-9][a-z0-9 ._-]{0,39}$")
#: How deep sub-workflows nest in one run; a definition that goes deeper, or calls itself, is
#: refused when its run is checked (`subflows`).
SUBFLOW_DEPTH = 3
#: Keys an older locks took in a step and nothing reads any more: a definition or
#: a run's frozen copy that still holds one loads, and the key is dropped. A step
#: has no dollar limit: its room is its turns.
LEGACY_STEP_KEYS = frozenset({"budget_usd"})
#: What a step may carry from locks' memory into its brief: a project note by
#: id, every live note of a topic, or a problem with every fix tried on it.
MEMORY_REF = re.compile(r"^(?:(note|problem):)?(\d{1,9})$|^topic:([a-z0-9][a-z0-9_-]{0,39})$")
MEMORY_CHARS = 2000
#: The findings a step hands the step that waits on it: as records, bounded.
FINDINGS_HANDED = 30
FINDINGS_CHARS = 4000
PROMPT_CHARS = 8000
RESULT_CHARS = 1500
#: A review of a change this large (changed lines, added and removed) that touches two or
#: more areas runs as one blind review per area, side by side, into one ledger.
AREA_REVIEW_LINES = 600
#: The most other files an area's review is told the change touched.
AREA_OTHERS = 40

#: Step states as `status` shows them.
NEEDS_YOU = frozenset({"failed", "blocked", "replan", "needs_approval", "interrupted"})
STOPPED = frozenset({"cancelled", "dismissed"})


class WorkflowError(ValueError):
    """A definition, a name or a run that cannot be used as asked; the message says why."""


# --- where definitions live --------------------------------------------------------


def bundled_dir() -> Path:
    return Path(__file__).resolve().parent / "workflow_templates"


def dirs(cwd: str | Path | None) -> dict[str, Path | None]:
    """Each scope's folder. A directory outside an enrolled project has no project scope.
    A repository in a workspace keeps its own project-scope workflows when it has them."""
    mine = project.own(cwd) if cwd else None
    folder = mine if mine is not None and (mine / "workflows").is_dir() else (project.find(cwd) if cwd else None)
    return {"project": folder / "workflows" if folder else None, "user": home() / "workflows",
            "bundled": bundled_dir()}


def listing(cwd: str | Path | None) -> list[dict[str, Any]]:
    """Every workflow in every scope, nearest first; one a nearer scope hides is marked `shadowed`."""
    out, seen = [], set()
    for scope in SCOPES:
        folder = dirs(cwd)[scope]
        if folder is None or not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            name = path.stem
            if not NAME.match(name):
                continue
            entry: dict[str, Any] = {"name": name, "scope": scope, "path": str(path), "shadowed": name in seen}
            try:
                defn = normalize(_read(path), name)
                entry.update(description=defn.get("description", ""), steps=[s["id"] for s in defn["steps"]],
                             inputs=defn.get("inputs", []), input_specs=input_specs(defn),
                             memory=list(dict.fromkeys(m for s in defn["steps"] for m in s.get("memory") or [])))
            except WorkflowError as exc:
                entry.update(error=str(exc))
            seen.add(name)
            out.append(entry)
    return out


def locate(name: str, cwd: str | Path | None, scope: str | None = None) -> tuple[str, Path]:
    _check_name(name)
    for where in [scope] if scope else SCOPES:
        if where not in SCOPES:
            raise WorkflowError(f"no scope {where!r}: one of {', '.join(SCOPES)}")
        folder = dirs(cwd)[where]
        if folder is not None and (folder / f"{name}.json").is_file():
            return where, folder / f"{name}.json"
    raise WorkflowError(f"no workflow {name!r}" + (f" in the {scope} scope" if scope else "")
                        + "; `locks workflow list` shows them")


#: A feature this big, or one that touches this many areas, runs in parts by default.
FAN_OUT_POINTS = 8
FAN_OUT_AREAS = 2


def fanned(name: str, cwd: str | Path | None, inputs: Mapping[str, str] | None,
           scope: str | None = None) -> tuple[str, str | None]:
    """The workflow a large feature runs as, and why: `feature` (locks' own, not one the
    person saved) becomes `feature-parallel` when its goal is `FAN_OUT_POINTS` or more, or
    touches `FAN_OUT_AREAS` areas or more, the same pure estimate a task's plan makes. Its plan
    splits the work into parts that build side by side, and a plan of one part is the
    feature run whole, so nothing is lost when it should not split. (name, None) otherwise."""
    from locks import points, risk

    goal = str((inputs or {}).get("goal") or "")
    if name != "feature" or not goal:
        return name, None
    try:
        if locate("feature", cwd, scope)[0] != "bundled" or locate("feature-parallel", cwd)[0] != "bundled":
            return name, None
    except WorkflowError:
        return name, None
    found = risk.of_request(goal, "feature")
    size = points.of_request(goal, "feature", found)
    areas = sorted({area for area, _ in risk.areas(found)})
    if size.points < FAN_OUT_POINTS and len(areas) < FAN_OUT_AREAS:
        return name, None
    why = f"{size.points} points" + (f", {len(areas)} areas ({', '.join(areas)})" if len(areas) >= FAN_OUT_AREAS
                                     else "")
    return "feature-parallel", (f"runs as feature-parallel ({why}): its plan splits it into parts that build side "
                                "by side, and a plan of one part is the feature run whole; `--whole` keeps one build")


#: A goal listing this many items, at the largest size, is several features: said before it runs.
MANY_FEATURES = 6


def many_features(goal: str) -> str | None:
    """What a run is told of a goal that is several features at once: its items (`criteria`)
    are `MANY_FEATURES` or more and its estimate is the largest size. Advice, never a refusal:
    its plan can still order the parts in phases, but one plan for every feature reads every
    repository before it splits anything."""
    from locks import criteria, points, risk

    items = criteria.items(goal)
    if len(items) < MANY_FEATURES:
        return None
    found = risk.of_request(goal, "feature")
    if points.of_request(goal, "feature", found).points < max(points.SCALE):
        return None
    return (f"this goal lists {len(items)} items at the largest size: one plan for all of them reads every "
            "repository it touches before it can split anything. Runs per feature, or per phase in dependency "
            "order (each later one `--after run:<N>`), each plan smaller, cost less and show progress sooner; "
            "this run goes on as asked")


def load(name: str, cwd: str | Path | None, scope: str | None = None) -> dict[str, Any]:
    """A saved definition, checked, with where it came from (`scope`, `path`)."""
    where, path = locate(name, cwd, scope)
    return {**normalize(_read(path), name), "scope": where, "path": str(path)}


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"{path} holds no workflow object")
    return data


def _check_name(name: str) -> None:
    if not NAME.match(name or ""):
        raise WorkflowError(f"{name!r} is no workflow name: lowercase letters, digits, '.', '_' and '-'")


# --- the definition ------------------------------------------------------------------


def skeleton(name: str, description: str = "") -> dict[str, Any]:
    """A new workflow to fill: one step, which `step add` and `step set` change."""
    _check_name(name)
    return {"name": name, "description": description, "inputs": ["goal"],
            "steps": [{"id": "do", "prompt": "{goal}"}]}


def normalize(defn: Mapping[str, Any], name: str | None = None) -> dict[str, Any]:
    """The definition checked and completed: every step has `needs` (the step
    before it, when it does not say) and a title. Raises with every problem found."""
    errors: list[str] = []
    data = {k: copy.deepcopy(v) for k, v in defn.items() if k not in ("scope", "path")}
    unknown = set(data) - TOP_KEYS
    if unknown:
        errors.append(f"unknown keys {sorted(unknown)}: a workflow has {sorted(TOP_KEYS)}")
    data["name"] = name or data.get("name") or ""
    if not NAME.match(str(data["name"])):
        errors.append(f"{data['name']!r} is no workflow name")
    version = data.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("version is a whole number from 1")
        version = 1
    data["version"] = version
    if not isinstance(data.get("description", ""), str):
        errors.append("description is text")
    # Paths, from the repository's top, a run needs there before it starts (`prepare`): a workflow built on a
    # method's files stopped at its first step, a worker spent, in a repository that kept none.
    requires = data.get("requires", [])
    if not isinstance(requires, list) or not all(
            isinstance(r, str) and r.strip() and not r.startswith("/") and ".." not in r.split("/") for r in requires):
        errors.append("requires is a list of paths inside the repository")
        requires = []
    if requires:
        data["requires"] = list(dict.fromkeys(r.strip().rstrip("/") for r in requires))
    else:
        data.pop("requires", None)
    inputs = data.get("inputs", [])
    if not isinstance(inputs, list) or not all(isinstance(i, str) and INPUT.fullmatch("{" + i + "}") for i in inputs):
        errors.append("inputs is a list of names (lowercase, digits, '_')")
        inputs = []
    data["inputs"] = list(dict.fromkeys(inputs))
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps is a list of at least one step")
        steps = []
    ids: list[str] = []
    for n, raw in enumerate(steps):
        where = f"step {n + 1}"
        if not isinstance(raw, dict):
            errors.append(f"{where} is not an object")
            continue
        step = {k: v for k, v in raw.items() if k not in LEGACY_STEP_KEYS}
        steps[n] = step
        extra = set(step) - STEP_KEYS
        if extra:
            errors.append(f"{where} has unknown keys {sorted(extra)}: a step has {sorted(STEP_KEYS)}"
                          + (" (rules and approvals are flags of `locks workflow run`, never of a file)"
                             if extra & {"allow", "allow_tools", "allow_approval", "capabilities"} else ""))
        sid = step.get("id")
        if not isinstance(sid, str) or not NAME.match(sid):
            errors.append(f"{where} needs an id (lowercase letters, digits, '.', '_', '-')")
            sid = f"#{n + 1}"
        elif sid in ids:
            errors.append(f"step id {sid!r} is used twice")
        where = f"step {sid!r}"
        if step.get("ask") is not None and step.get("workflow") is not None:
            errors.append(f"{where} asks the person or runs a workflow, not both")
        elif step.get("ask") is not None:
            errors += _ask_problems(step, where, data["inputs"])
        elif step.get("workflow") is not None:
            errors += _sub_problems(step, where, data["inputs"])
        else:
            prompt = step.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                errors.append(f"{where} needs a prompt: the task its worker is given")
            elif len(prompt) > PROMPT_CHARS:
                errors.append(f"{where}'s prompt is over {PROMPT_CHARS} characters")
            else:
                for used in INPUT.findall(prompt):
                    if used not in data["inputs"]:
                        errors.append(f"{where} uses {{{used}}}, which is not among the inputs {data['inputs']}")
        if step.get("kind") is not None and step["kind"] not in KINDS:
            errors.append(f"{where}: no kind {step['kind']!r}; one of {', '.join(KINDS)}")
        if step.get("start") is not None:
            try:
                Cell.parse(str(step["start"]))
            except ValueError as exc:
                errors.append(f"{where}: start {step['start']!r} is no cell ({exc})")
        if step.get("role") is not None and (not isinstance(step["role"], str)
                                             or not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", step["role"])):
            errors.append(f"{where}: role is a role's name (`locks roles list`)")
        if step.get("surfaces") is not None:
            try:
                if not isinstance(step["surfaces"], list):
                    raise surfaces.SurfaceError("a list of paths or globs")
                step["surfaces"] = list(surfaces.check(step["surfaces"])) or None
                if step["surfaces"] is None:
                    step.pop("surfaces")
            except surfaces.SurfaceError as exc:
                errors.append(f"{where}: surfaces are {exc}" if "list" in str(exc) else f"{where}: {exc}")
        if step.get("ledger") is not None:
            role = step["ledger"]
            if role not in ledger.ROLES:
                errors.append(f"{where}: ledger is one of {', '.join(ledger.ROLES)}")
            elif not any(s.get("ledger") is None for s in steps[:n] if isinstance(s, dict)):
                errors.append(f"{where}: a ledger step comes after the reviews whose findings it takes")
            elif role == "recheck" and not any(isinstance(s, dict) and s.get("ledger") == "fix" for s in steps[:n]):
                errors.append(f"{where}: a recheck comes after the fix it checks")
        if step.get("fanout") is not None and not isinstance(step["fanout"], str):
            errors.append(f"{where}: fanout is the id of the plan step whose parts it runs")
        if step.get("integrate") is not None and not isinstance(step["integrate"], bool):
            errors.append(f"{where}: integrate is true or false")
        if step.get("verify") is not None and not isinstance(step["verify"], str):
            errors.append(f"{where}: verify is a command, as text")
        turns = step.get("max_turns")
        if turns is not None and (not isinstance(turns, int) or isinstance(turns, bool) or turns <= 0):
            errors.append(f"{where}: max_turns is a whole number from 1 (at most {MAX_TURNS_CEILING} are used)")
        elif turns is not None and turns > MAX_TURNS_CEILING:
            # More than an attempt ever gets (a definition or a run an older locks
            # kept): read as the ceiling, as a run would clamp it.
            step["max_turns"] = MAX_TURNS_CEILING
        if step.get("memory") is not None:
            refs = step["memory"] if isinstance(step["memory"], list) else None
            if refs is None or not all(isinstance(r, (str, int)) and not isinstance(r, bool)
                                       and MEMORY_REF.match(str(r)) for r in refs):
                errors.append(f"{where}: memory is a list of note:<id> (or a bare id), topic:<name> or problem:<id>")
            else:
                step["memory"] = list(dict.fromkeys(_memory_ref(r) for r in refs)) or None
                if step["memory"] is None:
                    step.pop("memory")
        if "needs" not in step:
            step["needs"] = ids[-1:]
        if not isinstance(step["needs"], list) or not all(isinstance(x, str) for x in step["needs"]):
            errors.append(f"{where}: needs is a list of step ids")
            step["needs"] = []
        for dep in step["needs"]:
            if dep not in ids:
                errors.append(f"{where} needs {dep!r}, which is not a step before it")
        if step.get("when") is not None:
            errors += _when_problems(step, where, {s["id"]: s for s in steps[:n]
                                                   if isinstance(s, dict) and isinstance(s.get("id"), str)})
        step["title"] = str(step.get("title") or sid)
        ids.append(sid)
    errors += _parts_problems([s for s in steps if isinstance(s, dict)], ids)
    errors += trunks.problems([s for s in steps if isinstance(s, dict)], ids)
    if errors:
        raise WorkflowError("; ".join(errors))
    data["steps"] = steps
    return data


def _ask_problems(step: Mapping[str, Any], where: str, inputs: Sequence[str]) -> list[str]:
    """What is wrong with a question step: its question, its options, and fields it does not take."""
    out = []
    if extra := sorted(set(step) - ASK_KEYS):
        out.append(f"{where} asks the person, so it runs no worker: it takes no {', '.join(extra)}")
    question = step.get("ask")
    if not isinstance(question, str) or not question.strip():
        out.append(f"{where}: ask is the question, as text")
    elif len(question) > PROMPT_CHARS:
        out.append(f"{where}'s question is over {PROMPT_CHARS} characters")
    else:
        out += [f"{where} uses {{{used}}}, which is not among the inputs {list(inputs)}"
                for used in INPUT.findall(question) if used not in inputs]
    options = step.get("options")
    if not isinstance(options, list) or not 2 <= len(options) <= ASK_OPTIONS or not all(
            isinstance(o, str) and OPTION.match(o) for o in options) or len(set(options)) != len(options):
        out.append(f"{where}: options are 2 to {ASK_OPTIONS} different answers, each a short lowercase word or "
                   "phrase (letters, digits, spaces, '.', '_', '-')")
    return out


def _sub_problems(step: Mapping[str, Any], where: str, inputs: Sequence[str]) -> list[str]:
    """What is wrong with a sub-workflow step, from its own fields: the workflow it names is
    read when its run is checked (`subflows`), where it is found."""
    out = []
    if extra := sorted(set(step) - SUB_KEYS):
        out.append(f"{where} runs another workflow, whose steps run its workers: it takes no {', '.join(extra)}")
    if not isinstance(step.get("workflow"), str) or not NAME.match(step["workflow"]):
        out.append(f"{where}: workflow is the name of a saved workflow")
    given = step.get("with", {})
    if not isinstance(given, dict) or not all(isinstance(k, str) and INPUT.fullmatch("{" + k + "}")
                                              and isinstance(v, str) for k, v in given.items()):
        out.append(f"{where}: with maps the inputs of the workflow it runs to text ({{input}} names this "
                   "workflow's inputs)")
    else:
        out += [f"{where} uses {{{used}}}, which is not among the inputs {list(inputs)}"
                for value in given.values() for used in INPUT.findall(value) if used not in inputs]
    return out


def _when_problems(step: Mapping[str, Any], where: str, before: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """What is wrong with a step's `when`: each condition names a step it waits on directly and an
    outcome that step's ending can say (`OUTCOMES`, or a question's options)."""
    when = step["when"]
    if not isinstance(when, dict) or not when:
        return [f"{where}: when maps steps it needs to an outcome ({', '.join(OUTCOMES)}, or a question's option)"]
    out = []
    for dep, outcome in when.items():
        if dep not in step.get("needs") or ():
            out.append(f"{where}: when names {dep!r}, which it does not need: add it to its needs")
            continue
        asked = (before.get(dep) or {}).get("options")
        if asked is not None and (before.get(dep) or {}).get("ask") is not None:
            if outcome not in asked:
                out.append(f"{where}: {dep!r} asks the person; when takes one of its options {asked}, not {outcome!r}")
        elif outcome not in OUTCOMES:
            out.append(f"{where}: when {dep!r} is one of {', '.join(OUTCOMES)}, not {outcome!r}")
    return out


def _parts_problems(steps: list[dict[str, Any]], ids: list[str]) -> list[str]:
    """What is wrong with the steps that run in parts: a `fanout` names a plan step
    before it that it waits on, and an `integrate` step joins the parts of one."""
    by_id = {s["id"]: s for s in steps if isinstance(s.get("id"), str)}
    out = []
    for step in steps:
        sid = step.get("id")
        if not isinstance(sid, str) or sid not in ids:
            continue
        where, waits = f"step {sid!r}", _waits_on(by_id, sid)
        target = step.get("fanout")
        if isinstance(target, str):
            if target not in ids[:ids.index(sid)] or target not in by_id:
                out.append(f"{where}: fanout names {target!r}, which is no step before it")
            elif by_id[target].get("kind") != "plan":
                out.append(f"{where}: fanout names {target!r}, a {by_id[target].get('kind') or 'kindless'} step: "
                           "the parts come from a plan step's result (kind plan)")
            elif target not in waits:
                out.append(f"{where} fans out over {target!r} but does not wait on it: add it to its needs")
            if step.get("ledger") or step.get("integrate"):
                out.append(f"{where}: a fanout step is no ledger step and no integrate step")
        if step.get("integrate") is True:
            if not any(by_id[w].get("fanout") for w in waits if w in by_id):
                out.append(f"{where}: an integrate step joins the parts of a fanout step it waits on, and none "
                           "comes before it")
            if step.get("ledger") or step.get("kind") in READ_ONLY_KINDS:
                out.append(f"{where}: an integrate step merges, so it writes: no ledger and no reading kind")
        else:
            # A step starts from one branch (`_base`) and a step run in parts keeps one per part:
            # only the integrate step that joins them may wait on it.
            for dep in step.get("needs") or ():
                if isinstance(dep, str) and (by_id.get(dep) or {}).get("fanout"):
                    out.append(f"{where} waits on {dep!r}, a step run in parts: wait on an integrate step that "
                               "joins its parts instead")
    return out


def _waits_on(by_id: Mapping[str, Mapping[str, Any]], sid: str) -> set[str]:
    """Every step `sid` waits on, directly or not; ids that name no step are left out."""
    seen: set[str] = set()
    todo = [d for d in by_id[sid].get("needs") or () if d in by_id]
    while todo:
        dep = todo.pop()
        if dep not in seen:
            seen.add(dep)
            todo += [d for d in by_id[dep].get("needs") or () if d in by_id]
    return seen


def _memory_ref(ref: str | int) -> str:
    """`12` and `note:12` are the same note; a topic and a problem keep their prefix."""
    match = MEMORY_REF.match(str(ref))
    if match and match.group(3):
        return f"topic:{match.group(3)}"
    return f"{match.group(1) or 'note'}:{int(match.group(2))}"


def memory_problems(store: Store, project_key: str | None, refs: list[str]) -> list[str]:
    """Why each memory a step names cannot be read in this project: a note of
    another project is not this one's, and a dropped note says nothing now. A
    topic with no note yet is no problem (`memory_gaps`): a new project has none."""
    out = []
    for ref in refs:
        kind, _, value = ref.partition(":")
        if kind == "note":
            note = store.get_note(int(value))
            if note is None or note["project"] != project_key:
                out.append(f"no note #{value} in this project")
            elif note["state"] == "dropped":
                out.append(f"note #{value} was dropped")
        elif kind == "problem" and store.problem(int(value)) is None:
            out.append(f"no problem #{value}")
    return out


def _topic(store: Store, project_key: str | None, value: str) -> str:
    """A memory topic as the notes file it: an alias (`style`) is its topic (`conventions`)."""
    from locks import notes as notes_mod

    try:
        return notes_mod.resolve_topic(store, project_key or "", value)
    except ValueError:
        return value


def memory_gaps(store: Store, project_key: str | None, refs: list[str]) -> list[str]:
    """What to say of each topic a step names that holds no note in this project yet:
    the step starts without it, and the person is told how to fill it before it does."""
    from locks import notes as notes_mod

    known = notes_mod.topics(store, project_key or "")
    out = []
    for kind, _, value in (ref.partition(":") for ref in refs):
        topic = _topic(store, project_key, value) if kind == "topic" else ""
        if not topic or store.notes(project_key or "", topics=[topic], states=notes_mod.LIVE, limit=1):
            continue
        how = (f"`locks note --topic {topic} \"…\"` adds one" if topic in known else
               f"this project has no such topic: `locks notes topic add {topic} \"<what it holds>\"`, then "
               f"`locks note --topic {topic} \"…\"`")
        out.append(f"topic {value!r} holds no note in this project yet; the step starts without it ({how})")
    return out


def memory_text(store: Store, project_key: str | None, refs: list[str]) -> str:
    """The memory a step carries, written for its worker. A replaced note is read
    as the note that replaced it; one gone since the run started says so."""
    from locks import notes as notes_mod

    lines: list[str] = []
    for ref in refs:
        kind, _, value = ref.partition(":")
        if kind == "note":
            note, seen = store.get_note(int(value)), set()
            while note and note["state"] == "replaced" and note.get("replaced_by") and note["id"] not in seen:
                seen.add(note["id"])
                note = store.get_note(int(note["replaced_by"]))
            if note is None or note["project"] != project_key or note["state"] == "dropped":
                lines.append(f"- note #{value}: no longer kept; go on without it")
            else:
                lines.append("- " + notes_mod.line(note, text_chars=MEMORY_CHARS))
        elif kind == "topic":
            found = store.notes(project_key or "", topics=[_topic(store, project_key, value)], states=notes_mod.LIVE,
                                limit=20)
            lines += [f"- {notes_mod.line(n, text_chars=MEMORY_CHARS)}" for n in found] or [
                f"- topic {value}: no live note now"]
        else:
            problem = store.problem(int(value))
            if problem is None:
                lines.append(f"- problem #{value}: no longer kept")
                continue
            lines.append(f"- problem #{problem['id']} [{problem['state']}] {problem['title']}"
                         + (f": {problem['symptom']}" if problem.get("symptom") else ""))
            for fix in problem["fixes"]:
                outcome = "worked, then stopped holding" if fix.get("invalidated_on") else fix["outcome"]
                why = f" — {fix['why']}" if fix.get("why") else ""
                lines.append(f"    · {outcome}: {fix['description']}{why}")
    if not lines:
        return ""
    return ("Memory this workflow attaches to this step (locks' notes and problems, kept from earlier work). "
            "Take it into account here; check a note against the code, and trust the code when they disagree:\n"
            + "\n".join(lines))


class Saved(NamedTuple):
    """Where a change went: the definition itself, or — while runs use it — its pending revision."""

    scope: str
    path: Path
    held_by: list[int]


def pending_path(path: Path) -> Path:
    """Where a change to a definition in use waits: beside it, out of every listing."""
    return path.parent / ".pending" / path.name


def in_use(path: Path | str, store: Store | None = None) -> list[int]:
    """The active runs started from this definition, in any project."""
    own = store is None
    store = store or Store.open()
    try:
        return [r["id"] for r in store.runs_from(str(path))]
    finally:
        if own:
            store.close()


def promote(path: Path | str, store: Store | None = None) -> bool:
    """Apply a pending revision once no active run uses the definition. Never raises."""
    path = Path(path)
    waiting = pending_path(path)
    if not waiting.is_file():
        return False
    try:
        if in_use(path, store):
            return False
        normalize(_read(waiting), path.stem)
        waiting.replace(path)
        return True
    except (OSError, WorkflowError):
        return False


def _write(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(_stored(data), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def revision(path: Path | str) -> str:
    """What a change builds on, as a short hash of its file (`sha256:` and 16 hex digits): the
    pending revision while one waits, else the definition; `none` for one not written yet. A
    change made with the revision its author read (`expect`) is refused once another landed
    since, so two sessions editing one workflow never overwrite each other unseen."""
    path = Path(path)
    source = pending_path(path) if pending_path(path).is_file() else path
    if not source.is_file():
        return "none"
    return "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()[:16]


def _expected(path: Path | str, expect: str | None) -> None:
    """Refuse a change made on a revision that is no longer the one it builds on."""
    if expect is None:
        return
    now = revision(path)
    if expect.strip() != now:
        raise WorkflowError(f"it changed since you read it: you read {expect.strip()}, it is {now} now. Read it "
                            "again (`locks workflow show`), then make the change on what is there")


def save(defn: Mapping[str, Any], cwd: str | Path | None, scope: str = DEFAULT_SCOPE, *,
         replace: bool = False, now: bool = False, store: Store | None = None,
         expect: str | None = None) -> Saved:
    """Write a definition, checked first, one version on from the one it replaces.
    While an active run (of any project) uses it, the change is kept as its
    pending revision and applied when the last such run ends; `now` writes it
    at once. A run never sees either: it runs the copy it started with.
    A bundled template is never written: copy it."""
    if scope not in ("project", "user"):
        raise WorkflowError(f"a workflow is saved in the project or the user scope, not {scope!r}")
    checked = normalize(defn)
    folder = dirs(cwd)[scope]
    if folder is None:
        raise WorkflowError("this directory is in no enrolled project (a .locks/ folder): save it in the user scope, "
                            "or `locks init` the project first")
    path = folder / f"{checked['name']}.json"
    _expected(path, expect)
    if path.exists() and not replace:
        raise WorkflowError(f"{checked['name']!r} exists in the {scope} scope; edit it, or remove it first")
    if not path.exists():
        _write(path, {**checked, "version": 1})
        return Saved(scope, path, [])
    live = normalize(_read(path), checked["name"])
    held = [] if now else in_use(path, store)
    if held:
        _write(pending_path(path), {**checked, "version": live["version"] + 1})
        return Saved(scope, pending_path(path), held)
    _write(path, {**checked, "version": live["version"] + 1})
    pending_path(path).unlink(missing_ok=True)
    return Saved(scope, path, [])


def _stored(defn: Mapping[str, Any]) -> dict[str, Any]:
    """As written to a file: a title equal to the id and the implicit `needs` left out."""
    steps, before = [], []
    for step in defn["steps"]:
        out = {k: v for k, v in step.items() if v is not None}
        if out.get("title") == out["id"]:
            out.pop("title")
        if out.get("needs") == before[-1:]:
            out.pop("needs")
        steps.append(out)
        before.append(step["id"])
    return {"name": defn["name"], "version": defn.get("version", 1), "description": defn.get("description", ""),
            "inputs": defn.get("inputs", []), "steps": steps}


def copy_to(source: str, target: str, cwd: str | Path | None, *, scope: str = DEFAULT_SCOPE,
            from_scope: str | None = None) -> Path:
    """A saved workflow under a new name, to edit without starting from nothing."""
    defn = load(source, cwd, from_scope)
    _check_name(target)
    return save({**defn, "name": target, "version": 1}, cwd, scope).path


def remove(name: str, cwd: str | Path | None, scope: str, *, force: bool = False) -> Path:
    if scope == "bundled":
        raise WorkflowError("a bundled template is not removed; a workflow of the same name in the user or project "
                            "scope hides it")
    _, path = locate(name, cwd, scope)
    held = in_use(path)
    if held and not force:
        raise WorkflowError(f"{name} is in use by run {', '.join(f'#{r}' for r in held)}; remove it when they end, "
                            "or pass --force (they go on with the copy they started with)")
    path.unlink()
    pending_path(path).unlink(missing_ok=True)
    return path


def edit(name: str, cwd: str | Path | None, change: Callable[[dict[str, Any]], None],
         scope: str | None = None, *, now: bool = False, expect: str | None = None) -> Saved:
    """Load, change, check, write back where it was. A bundled template is copied
    to the user scope first: the change is the person's, the template stays.
    While runs use it, changes build on its pending revision (see `save`)."""
    defn = load(name, cwd, scope)
    _expected(defn["path"], expect)
    where = defn["scope"] if defn["scope"] != "bundled" else DEFAULT_SCOPE
    if where == defn["scope"]:
        promote(defn["path"])
        waiting = pending_path(Path(defn["path"]))
        if waiting.is_file() and not now:
            defn = {**normalize(_read(waiting), name), "scope": where, "path": defn["path"]}
    change(defn)
    return save(defn, cwd, where, replace=True, now=now)


def pending(cwd: str | Path | None) -> list[dict[str, Any]]:
    """Every change waiting for the runs that use its workflow to end."""
    out = []
    for scope in ("project", "user"):
        folder = dirs(cwd)[scope]
        if folder is None or not (folder / ".pending").is_dir():
            continue
        for waiting in sorted((folder / ".pending").glob("*.json")):
            live = folder / waiting.name
            out.append({"name": waiting.stem, "scope": scope, "path": str(waiting), "held_by": in_use(live)})
    return out


def apply_pending(name: str, cwd: str | Path | None, scope: str | None = None, *, now: bool = False) -> Path:
    """Apply a waiting change: when no run uses the workflow, or at once with `now`."""
    where, path = locate(name, cwd, scope)
    waiting = pending_path(path)
    if not waiting.is_file():
        raise WorkflowError(f"{name} ({where}) has no pending change")
    held = in_use(path)
    if held and not now:
        raise WorkflowError(f"{name} is in use by run {', '.join(f'#{r}' for r in held)}; the change applies when "
                            "they end, or now with --now (they go on with the copy they started with)")
    normalize(_read(waiting), name)
    waiting.replace(path)
    return path


def drop_pending(name: str, cwd: str | Path | None, scope: str | None = None) -> Path:
    where, path = locate(name, cwd, scope)
    waiting = pending_path(path)
    if not waiting.is_file():
        raise WorkflowError(f"{name} ({where}) has no pending change")
    waiting.unlink()
    return waiting


def add_step(defn: dict[str, Any], step: dict[str, Any], *, after: str | None = None) -> None:
    ids = [s["id"] for s in defn["steps"]]
    if step.get("id") in ids:
        raise WorkflowError(f"step {step['id']!r} exists; `step set` changes it")
    at = len(ids) if after is None else _index(defn, after) + 1
    if "needs" not in step and at < len(ids):
        # Inserted in a chain: it needs what was before it, and what came after it now needs it.
        step["needs"] = [ids[at - 1]] if at else []
        follower = defn["steps"][at]
        if follower.get("needs") == step["needs"]:
            follower["needs"] = [step["id"]]
    defn["steps"].insert(at, step)


def set_step(defn: dict[str, Any], step_id: str, changes: Mapping[str, Any]) -> None:
    step = defn["steps"][_index(defn, step_id)]
    for key, value in changes.items():
        if value is None:
            step.pop(key, None)
        else:
            step[key] = value


def remove_step(defn: dict[str, Any], step_id: str) -> None:
    at = _index(defn, step_id)
    gone = defn["steps"].pop(at)
    for step in defn["steps"]:
        if step_id in step.get("needs", []):
            # What waited on it waits on what it waited on.
            step["needs"] = list(dict.fromkeys(n for dep in step["needs"]
                                               for n in (gone.get("needs", []) if dep == step_id else [dep])))


def _index(defn: Mapping[str, Any], step_id: str) -> int:
    for n, step in enumerate(defn["steps"]):
        if step.get("id") == step_id:
            return n
    raise WorkflowError(f"no step {step_id!r}; the steps are {[s.get('id') for s in defn['steps']]}")


# --- the graph, as text --------------------------------------------------------------

#: How a step's state colours its node in a run's graph: each tone beside its word, never alone.
STATE_CLASSES = {"done": "fill:#d7f0dc,stroke:#2f7d43", "running": "fill:#d6e6fb,stroke:#2d5f9e",
                 "queued": "fill:#eef1f5,stroke:#6b7a8c", "pending": "fill:#ffffff,stroke:#9aa5b1",
                 "skipped": "fill:#f2f2f2,stroke:#9aa5b1,stroke-dasharray:4 3",
                 "stopped": "fill:#fbe0dc,stroke:#b03a2e"}


def _label(text: str) -> str:
    """Text inside a Mermaid label: its quotes and angle brackets as Mermaid's entities."""
    return text.replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")


def _what(step: Mapping[str, Any]) -> str:
    """A step's kind of work, in the words its node carries."""
    if step.get("ask") is not None:
        return "asks: " + " / ".join(step.get("options") or ())
    if step.get("workflow"):
        return f"runs {step['workflow']}"
    if step.get("ledger"):
        return f"{step['ledger']} (ledger)"
    if step.get("fanout"):
        return f"parts of {step['fanout']}"
    if step.get("integrate"):
        return "joins the parts"
    return str(step.get("kind") or "classified when it runs")


def render(defn: Mapping[str, Any], run: Mapping[str, Any] | None = None) -> str:
    """A definition as a Mermaid flowchart: a node per step, shaped by what it does (a question a
    rhombus, a sub-workflow a subroutine, a ledger step a hexagon, a reading step a stadium), an
    edge per need, labelled with the outcome a `when` waits for. With a run's `status`, each node
    says its state and task, and is coloured by it."""
    states = {s["id"]: s for s in (run or {}).get("steps") or ()}
    ids = {step["id"]: f"s{n}" for n, step in enumerate(defn["steps"])}
    lines = ["flowchart TD"]
    classes: dict[str, list[str]] = {}
    for step in defn["steps"]:
        text = _label(clip(step["title"], 60, one_line=True)) + "<br/><i>" + _label(_what(step)) + "</i>"
        state = states.get(step["id"])
        if state is not None:
            word = "skipped" if state.get("skipped") else str(state["state"])
            said = word + (f" #{state['task']}" if state.get("task") else "")
            if state.get("answer"):
                said += f": {state['answer']}"
            if state.get("subrun"):
                said += f" (run #{state['subrun']})"
            text += "<br/>" + _label(said)
            tone = word if word in STATE_CLASSES else "stopped"
            classes.setdefault(tone, []).append(ids[step["id"]])
        node = ids[step["id"]]
        if step.get("ask") is not None:
            lines.append(f'    {node}{{"{text}"}}')
        elif step.get("workflow"):
            lines.append(f'    {node}[["{text}"]]')
        elif step.get("ledger"):
            lines.append(f'    {node}{{{{"{text}"}}}}')
        elif step.get("kind") in READ_ONLY_KINDS:
            lines.append(f'    {node}(["{text}"])')
        else:
            lines.append(f'    {node}["{text}"]')
    for step in defn["steps"]:
        when = dict(step.get("when") or {})
        for dep in step["needs"]:
            arrow = f'-->|"{_label(str(when[dep]))}"|' if dep in when else "-->"
            lines.append(f"    {ids[dep]} {arrow} {ids[step['id']]}")
    for tone, nodes in classes.items():
        lines.append(f"    classDef {tone} {STATE_CLASSES[tone]}")
        lines.append(f"    class {','.join(nodes)} {tone}")
    return "\n".join(lines) + "\n"


def render_markdown(defn: Mapping[str, Any], run: Mapping[str, Any] | None = None) -> str:
    """The graph in a fenced block, then each step on a line: what it is given or asks."""
    head = f"## {defn['name']}" + (f" — run #{run['id']}, {run['state']}" if run else "")
    out = [head, ""]
    if defn.get("description"):
        out += [str(defn["description"]), ""]
    out += ["```mermaid", render(defn, run).rstrip("\n"), "```", ""]
    for step in defn["steps"]:
        said = step.get("ask") or (f"runs `{step['workflow']}`" if step.get("workflow") else step.get("prompt") or "")
        when = ", ".join(f"{k} is {v}" for k, v in dict(step.get("when") or {}).items())
        out.append(f"- **{step['id']}** ({_what(step)}{'; when ' + when if when else ''}): "
                   + clip(str(said), 200, one_line=True))
    return "\n".join(out) + "\n"


# --- runs ----------------------------------------------------------------------------

Kick = Callable[[str, str | None], object]


def _kick(cwd: str, key: str | None) -> bool:
    """Start the repository's dispatcher unless one runs. A workflow run is the
    person asking for its work, so this does not wait for the `autowork` setting."""
    root = home()
    return dispatch.start(Path(cwd), root, key or "*") or dispatch.held(root, key or "*")


def example(defn: Mapping[str, Any]) -> str:
    """The command that starts a run of `defn`, every input named: one that only a
    step's check uses is given as the run's `--verify`."""
    checks = check_inputs(defn)
    return " ".join([f"locks workflow run {defn['name']}",
                     *(f"--input {i}='…'" for i in defn["inputs"] if i not in checks),
                     *(["--verify '<check>'"] if checks else [])])


def check_inputs(defn: Mapping[str, Any]) -> list[str]:
    """The inputs only a step's own `verify` uses (no prompt names them): the run's
    `--verify` fills them when they are not given by name (`fill_checks`)."""
    steps = defn.get("steps") or ()
    in_prompts = {used for step in steps for text in (step.get("prompt"), step.get("ask"),
                                                      *dict(step.get("with") or {}).values())
                  for used in INPUT.findall(str(text or ""))}
    in_checks = {used for step in steps for used in CHECK_INPUT.findall(str(step.get("verify") or ""))}
    return [i for i in defn.get("inputs") or () if i in in_checks and i not in in_prompts]


def input_specs(defn: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each input a run takes, and whether it must be named: an input only a step's
    check uses is required unless the run's `--verify` gives it."""
    checks = set(check_inputs(defn))
    return [{"name": i, "required": i not in checks, **({"from_verify": True} if i in checks else {})}
            for i in defn.get("inputs") or ()]


def describe_inputs(defn: Mapping[str, Any]) -> str:
    """The inputs on one line: `goal (required), check (or --verify)`."""
    return ", ".join(f"{spec['name']} ({'required' if spec['required'] else 'required, or --verify'})"
                     for spec in input_specs(defn)) or "none"


def fill_checks(defn: Mapping[str, Any], inputs: Mapping[str, str], verify: str | None) -> dict[str, str]:
    """The inputs with each check input not given by name filled from the run's `--verify`."""
    out = dict(inputs)
    if verify and verify.strip():
        for name in check_inputs(defn):
            if not str(out.get(name) or "").strip():
                out[name] = verify
    return out


def _top(where: Path) -> Path:
    """The top of the checkout `where` is in, else `where` itself: what a definition's `requires` is read from."""
    return repo.toplevel(where) or where


def prepare(store: Store, name: str, cwd: str | Path, inputs: Mapping[str, str] | None = None, *,
            scope: str | None = None) -> dict[str, Any]:
    """What a run is checked against before it starts, all at once: the inputs it
    takes, the memory it can read here. Raises with every problem; `gaps` are the
    topics it would start without. Writes nothing (`--dry-run` is this alone)."""
    defn = load(name, cwd, scope)
    where = Path(cwd).resolve()
    given = {k: str(v) for k, v in (inputs or {}).items()}
    problems = []
    missing = [i for i in defn["inputs"] if not given.get(i, "").strip()]
    if missing:
        checks = [i for i in missing if i in check_inputs(defn)]
        problems.append(f"{name} needs {', '.join(missing)}: `{example(defn)}`"
                        + (f" ({', '.join(checks)} is the command a step checks with: `--verify` gives it)"
                           if checks else ""))
    extra = sorted(set(given) - set(defn["inputs"]))
    if extra:
        problems.append(f"{name} takes no input {', '.join(extra)}; it takes {defn['inputs'] or 'none'}")
    absent = [r for r in defn.get("requires") or () if not (_top(where) / r).exists()]
    if absent:
        problems.append(f"{name} needs {', '.join(absent)} in this repository, and "
                        f"{'it is' if len(absent) == 1 else 'they are'} not there: nothing was started")
    key = repo.key(where)
    # The workflows its sub-workflow steps run, read now and frozen with the run (`subflows`).
    flows: dict[str, dict[str, Any]] = {}
    try:
        flows = subflows(defn, where)
    except WorkflowError as exc:
        problems.append(str(exc))
    every = [*defn["steps"], *(step for flow in flows.values() for step in flow["steps"])]
    unreadable = [f"step {s['id']!r}: {why}" for s in every
                  for why in memory_problems(store, key, s.get("memory") or [])]
    if unreadable:
        problems.append("its memory cannot be read here: " + "; ".join(unreadable))
    if not missing:
        problems += unfilled_checks(defn, given)
    if problems:
        raise WorkflowError("; ".join(problems))
    gaps = [f"step {s['id']!r}: {gap}" for s in every for gap in memory_gaps(store, key, s.get("memory") or [])]
    return {"defn": defn, "where": where, "given": given, "key": key, "gaps": gaps, "subflows": flows}


def start(store: Store, name: str, cwd: str | Path, inputs: Mapping[str, str] | None = None, *,
          scope: str | None = None, session_id: str | None = None, options: Mapping[str, Any] | None = None,
          kick: Kick | None = None, after_run: int | None = None) -> dict[str, Any]:
    """A run of a saved workflow in a directory: its first steps queued, the dispatcher started.
    `options` are the person's (`allow_tools`, `allow_approval`, `verify`). `after_run`
    holds its first steps behind that run until it passes (`locks.after`), checked first."""
    held = after.target(store, run_id=after_run) if after_run is not None else None
    promote(locate(name, cwd, scope)[1], store)
    ready = prepare(store, name, cwd, inputs, scope=scope)
    defn, where, given, key = ready["defn"], ready["where"], ready["given"], ready["key"]
    if repo.toplevel(where) is not None:
        # Where the run started: every step's brief can say what the run changed since.
        with contextlib.suppress(isolate.IsolationError):
            options = {**dict(options or {}), "origin": isolate.head(where, "HEAD")}
        # The branch it started from: each step is compared with it as it moves on.
        if branch := isolate.current_branch(where):
            options = {**dict(options or {}), "origin_branch": branch}
    if held is not None:
        options = {**dict(options or {}), after.KEY: held}
    if ready["subflows"]:
        options = {**dict(options or {}), "subflows": ready["subflows"]}
    run = store.create_run(name=defn["name"], scope=defn["scope"], source=defn["path"],
                           definition=json.dumps(_stored(defn), ensure_ascii=False),
                           inputs=json.dumps(given, ensure_ascii=False), options=json.dumps(dict(options or {})),
                           repo=key, cwd=str(where), session_id=session_id)
    queued = _queue_ready(store, run)
    if held is not None:
        # Queued after a run that passed already: it starts now, on what that run kept.
        for task_id in queued:
            after.settle(store, store.get_task(task_id))
    (kick or _kick)(str(where), key)
    return {**run, "queued": queued, "gaps": ready["gaps"]}


def _definition(run: Mapping[str, Any]) -> dict[str, Any]:
    return normalize(json.loads(run["definition"]))


def _latest(store: Store, run_id: int) -> dict[str, dict]:
    """Each step's newest task: a retried step's last try is the one that counts. A step
    run in parts is its parts' newest tries together (`_together`)."""
    out: dict[str, dict] = {}
    parts: dict[str, dict[str, dict]] = {}
    for task in store.run_tasks(run_id):
        part = _part(task)
        if part is None:
            out[task["workflow_step"]] = task
            parts.pop(task["workflow_step"], None)
        else:
            parts.setdefault(task["workflow_step"], {})[part["id"]] = task
    for step, found in parts.items():
        out[step] = _together(list(found.values()))
    return out


def _part(task: Mapping[str, Any] | None) -> dict | None:
    """The part of its step a task runs (a plan's part, or one area of a review); None for a whole step."""
    try:
        found = json.loads((task or {}).get("options") or "{}").get("part")
    except ValueError:
        return None
    return found if isinstance(found, dict) and found.get("id") else None


def _together(tasks: list[dict]) -> dict:
    """A step run in parts, as one: its newest part's row with the step's state (the first
    part that stopped, else running while one runs, else queued, else done) and its parts in order."""
    ordered = sorted(tasks, key=lambda t: (int((_part(t) or {}).get("n") or 0), t["id"]))
    states = [t["status"] for t in ordered]
    stopped = next((state for state in states if state not in ("queued", "running", "done")), None)
    state = stopped or ("running" if "running" in states else "queued" if "queued" in states else "done")
    return {**max(ordered, key=lambda t: t["id"]), "status": state, "parts": ordered}


def _tasks_of(entry: Mapping[str, Any] | None) -> list[dict]:
    """The tasks a step's entry in `_latest` stands for: its parts, or itself."""
    if not entry:
        return []
    return list(entry.get("parts") or [entry])


#: How many rounds in a row `advance` may leave its transaction without reading anything new from
#: git before it gives up: a round that read a new call is progress and costs none of them.
READ_RETRIES = 3


class _Reread(Exception):
    """A call the transaction needs from git that was not read before it opened."""

    def __init__(self, key: tuple, thunk: Callable[[], Any]) -> None:
        super().__init__(key)
        self.key, self.thunk = key, thunk


class Reads:
    """What the steps about to be queued ask of git, read before the write transaction opens: git
    runs while SQLite's one write lock is held by no one, and the transaction only reads this
    memo (a call's value, or the error it raised) and the store. Open (`frozen` false) it runs the
    call and keeps its answer; frozen, a call it holds no answer for raises `_Reread`, which sends
    the caller out of the transaction to read it and try again."""

    def __init__(self) -> None:
        self.memo: dict[tuple, tuple[bool, Any]] = {}
        self.frozen = False

    def read(self, key: tuple, thunk: Callable[[], Any]) -> Any:
        if key not in self.memo:
            if self.frozen:
                raise _Reread(key, thunk)
            self.fill(key, thunk)
        done, value = self.memo[key]
        if done:
            return value
        raise value

    def fill(self, key: tuple, thunk: Callable[[], Any]) -> None:
        try:
            self.memo[key] = (True, thunk())
        except Exception as exc:
            self.memo[key] = (False, exc)


def _reading(reads: Reads | None, key: tuple, thunk: Callable[[], Any]) -> Any:
    """A git call: made now when nobody read it ahead (`reads` None), else answered from `reads`."""
    return thunk() if reads is None else reads.read(key, thunk)


def _numstat(run: Mapping[str, Any], start: str, end: str, reads: Reads | None) -> list[tuple[int, str]] | None:
    return _reading(reads, ("numstat", str(run["cwd"]), start, end),
                 lambda: isolate.numstat(Path(run["cwd"]), start, end))


def _ready(defn: Mapping[str, Any], latest: Mapping[str, dict]) -> list[dict]:
    """The steps whose needs all passed and that have no task yet (or a ledger step to go again)."""
    return [s for s in defn["steps"] if all((latest.get(d) or {}).get("status") == "done" for d in s["needs"])
            and (s["id"] not in latest or _rerun(s, latest))]


def _read_ahead(store: Store, run: Mapping[str, Any], reads: Reads) -> None:
    """Read from git, outside any transaction, what `_queue_ready` is about to ask for the steps
    ready now. A step made ready by one queued in the same pass is read when the transaction
    asks for it (`_Reread`)."""
    if run["status"] != "active":
        return
    defn, latest = _definition(run), _latest(store, run["id"])
    options = json.loads(run["options"])
    for step in _ready(defn, latest):
        _read_step(store, run, defn, step, latest, options, reads)


def _read_step(store: Store, run: Mapping[str, Any], defn: Mapping[str, Any], step: Mapping[str, Any],
               latest: Mapping[str, dict], options: Mapping[str, Any], reads: Reads) -> None:
    """The git reads `_queue_step` (and `_fan`) make for a step, made on `reads`. What goes wrong here
    is kept in `reads` and raised where the call is made, not here."""
    if step.get("ask") is not None or not _holds(store, step, latest)[0]:
        return  # a question runs no git, and a step that will be skipped must not restore a branch
    kind = str(step.get("kind") or "")
    with contextlib.suppress(Exception):
        base = _base(store, run, defn, step["id"], latest, reads)
        if step.get("workflow") is not None:
            return
        if step.get("ledger"):
            _ledger(store, run, defn, step, latest, options, reads)
        if step.get("integrate"):
            base = _joining(store, run, defn, step, latest, reads)["base"] or base
        if not step.get("fanout"):
            _areas(store, run, defn, step, latest, options, reads)
        if kind.startswith("review"):
            if not step.get("ledger") and options.get("origin") and base and base != options["origin"] \
                    and run["cwd"] and Path(run["cwd"]).is_dir():
                _generated_note(store, run, options["origin"], base, reads)
            if options.get("origin"):
                _links_expected(run, reads)
        if kind.startswith("review") or step.get("ledger"):
            _touches(run, options, base, None, reads)


def _queue_ready(store: Store, run: Mapping[str, Any], reads: Reads | None = None) -> list[int]:
    """Queue every step whose needs all passed and that has no task yet."""
    if run["status"] != "active":
        return []
    defn, latest = _definition(run), _latest(store, run["id"])
    inputs, options = json.loads(run["inputs"]), json.loads(run["options"])
    queued: list[int] = []
    while True:
        ready = _ready(defn, latest)
        if not ready:
            return queued
        for step in ready:
            tasks = _queue_one(store, run, defn, step, inputs, options, latest, reads)
            if not tasks:
                return queued  # the plan's pass was taken back: the run stops there
            latest[step["id"]] = _together(tasks) if _part(tasks[0]) else tasks[0]
            queued += [t["id"] for t in tasks if t["status"] == "queued"]
        # A ledger step with nothing to decide passed at once: what waits on it may be ready now.
        if all(latest[s["id"]]["status"] == "queued" for s in ready):
            return queued


def _queue_one(store: Store, run: Mapping[str, Any], defn: Mapping[str, Any], step: Mapping[str, Any],
               inputs: Mapping[str, str], options: Mapping[str, Any], latest: Mapping[str, dict],
               reads: Reads | None = None) -> list[dict]:
    """A step made ready, as its tasks: skipped when its `when` does not hold, in parts when it
    fans out, else whole (a question waits on the person, a sub-workflow starts its run). []
    when the plan it fans out over had its pass taken back."""
    holds, why = _holds(store, step, latest)
    if not holds:
        return [_skip(store, run, defn, step, inputs, why)]
    fanned = _fan(store, run, defn, step, inputs, options, latest, reads=reads)
    if fanned == []:
        return []
    return fanned or [_queue_step(store, run, defn, step, inputs, options, latest, reads=reads)]


def outcome(store: Store, task: Mapping[str, Any] | None) -> dict[str, Any]:
    """What a step's ending says for a `when`, from locks' records alone: the option a question
    was answered with, whether the work it judged had findings the change caused, and whether
    it changed files (each part of a step run in parts counted)."""
    if not task:
        return {}
    ended = [((store.final_event(t["id"]) or {}).get("data") or {}) for t in _tasks_of(task)]
    found = findings_mod.blocking(findings_mod.read({"findings": _findings_of(store, task)}))
    return {"answer": next((e["answer"] for e in ended if e.get("answer")), None), "findings": len(found),
            "changed": sum(len(e.get("changed") or ()) for e in ended)}


def _step_kind(defn: Mapping[str, Any], step_id: str) -> str:
    return next((str(s.get("kind") or "") for s in defn["steps"] if s.get("id") == step_id), "")


def _holds(store: Store, step: Mapping[str, Any], latest: Mapping[str, dict]) -> tuple[bool, str]:
    """Whether a step's `when` holds, and when it does not, why: every condition must."""
    for dep, wanted in dict(step.get("when") or {}).items():
        said = outcome(store, latest.get(dep))
        if wanted in ("findings", "clean"):
            got = "findings" if said.get("findings") else "clean"
        elif wanted in ("changed", "unchanged"):
            got = "changed" if said.get("changed") else "unchanged"
        else:
            got = str(said.get("answer") or "unanswered")
        if got != wanted:
            return False, f"its condition {dep} is {wanted!r} did not hold: {dep} was {got!r}"
    return True, ""


def _skip(store: Store, run: Mapping[str, Any], defn: Mapping[str, Any], step: Mapping[str, Any],
          inputs: Mapping[str, str], why: str) -> dict:
    """A step whose `when` did not hold: it passes with no worker and no change, said, and what
    waits on it goes on from the steps before it (`_base` looks past a step that kept no branch)."""
    task = store.create_task(f"(skipped) {step['title']}: {why}", status="done", source="queue", author="workflow",
                             repo=run["repo"], cwd=run["cwd"], session_id=run["session_id"],
                             options=json.dumps({"skipped": why}),
                             title=f"[{defn['name']}#{run['id']} {step['id']}] skipped: "
                                   f"{_headline(inputs) or step['title']}"[:200],
                             workflow_run=run["id"], workflow_step=step["id"])
    store.update_task(task["id"], result=f"skipped: {why}")
    store.add_event(task["id"], "finished", status="done", final_cell=None, cost_usd=0, branch=None, changed=[],
                    stop=None, findings=[], skipped=True, when_unmet=why, at=time.time())
    store.add_event(task["id"], "reported", via="workflow")
    return store.get_task(task["id"]) or task


def _question(store: Store, run: Mapping[str, Any], defn: Mapping[str, Any], step: Mapping[str, Any],
              inputs: Mapping[str, str]) -> dict:
    """A question step made ready: it waits on the person (`stops.PERSON_ONLY`), and its session
    hears of it as an ending with the line that answers it (`answer`). No worker runs."""
    from locks import stops

    question = _render(step["ask"], inputs)
    options = list(step["options"])
    asks = clip(question, 120, one_line=True)
    # Blocked from the start: no dispatcher may take a question for work, even for a moment.
    task = store.create_task(question + "\n\nAnswer with one of: " + ", ".join(options), status="blocked",
                             source="queue", author="workflow", repo=run["repo"], cwd=run["cwd"],
                             session_id=run["session_id"], options=json.dumps({"question": {"options": options}}),
                             title=f"[{defn['name']}#{run['id']} {step['id']}] asks: {asks}"[:200],
                             workflow_run=run["id"], workflow_step=step["id"])
    line = f"locks workflow answer {run['id']} {step['id']} <{'|'.join(options)}>"
    stops.record(store, task["id"], stops.Stop("question", clip(question, 2000), by="workflow",
                                               extra={"options": options, "answer": line}))
    return store.get_task(task["id"]) or task


def answer(store: Store, run_id: int, step_id: str, option: str, *, via: str = "cli",
           kick: Kick | None = None) -> dict[str, Any]:
    """The person's answer to a question step: it passes with that option, the steps whose `when`
    names it are chosen, and the run goes on (`advance`). Refused unless the step is waiting."""
    from locks import stops

    run = store.get_run(run_id)
    if run is None:
        raise WorkflowError(f"no workflow run #{run_id}")
    if run["status"] != "active":
        raise WorkflowError(f"run #{run_id} is {run['status']}")
    defn = _definition(run)
    step = defn["steps"][_index(defn, step_id)]
    if step.get("ask") is None:
        raise WorkflowError(f"step {step_id!r} asks nothing: it runs a worker")
    task = _latest(store, run_id).get(step_id)
    stop = stops.of(store, task) if task else None
    if task is None or task["status"] != "blocked" or stop is None or stop.cause != "question":
        state = "not reached yet" if task is None else f"{task['status']} (task #{task['id']})"
        raise WorkflowError(f"step {step_id!r} waits for no answer: it is {state}")
    chosen = " ".join(option.split()).lower()
    if chosen not in step["options"]:
        raise WorkflowError(f"{option!r} is no answer to {step_id!r}: one of {', '.join(step['options'])}")
    with store.immediate():
        store.update_task(task["id"], status="done", result=f"answered: {chosen}")
        store.add_event(task["id"], "answered", option=chosen, via=via)
        store.add_event(task["id"], "finished", status="done", final_cell=None, cost_usd=0, branch=None, changed=[],
                        stop=None, findings=[], answer=chosen, at=time.time())
        # Its question was an ending; answered, it is nothing to tell.
        store.mark_reported([task["id"]], via="answer")
    queued = advance(store, store.get_task(task["id"]), kick=kick)
    return {"task": task["id"], "answer": chosen, "queued": queued}


def subflows(defn: Mapping[str, Any], cwd: str | Path, *, depth: int = 1,
             seen: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    """The workflows a definition's sub-workflow steps run, read where they are found from `cwd`
    (the nearest scope), and theirs: frozen with the run, so a change to one never reaches a run
    already started. Raises when one is missing, calls itself, nests past `SUBFLOW_DEPTH`, or is
    not given every input it takes but a check's (the run's `--verify` gives those)."""
    out: dict[str, dict[str, Any]] = {}
    chain = (*seen, defn["name"])
    for step in defn["steps"]:
        name = step.get("workflow")
        if not name:
            continue
        where = f"step {step['id']!r} of {defn['name']}"
        if name in chain:
            raise WorkflowError(f"{where} runs {name}, which is already running it ({' → '.join((*chain, name))})")
        if depth > SUBFLOW_DEPTH:
            raise WorkflowError(f"{where} runs {name} {depth} levels deep: sub-workflows nest {SUBFLOW_DEPTH} at most")
        child = load(name, cwd)
        given = set(dict(step.get("with") or {}))
        if missing := [i for i in child["inputs"] if i not in given and i not in check_inputs(child)]:
            raise WorkflowError(f"{where} runs {name} without {', '.join(missing)}: give each in its `with`")
        if extra := sorted(given - set(child["inputs"])):
            raise WorkflowError(f"{where} gives {name} {', '.join(extra)}, which it does not take "
                                f"({', '.join(child['inputs']) or 'no input'})")
        out[name] = {**_stored(child), "scope": child["scope"], "path": str(child["path"])}
        out.update(subflows(child, cwd, depth=depth + 1, seen=chain))
    return out


def _subrun(store: Store, run: Mapping[str, Any], defn: Mapping[str, Any], step: Mapping[str, Any],
            inputs: Mapping[str, str], options: Mapping[str, Any], latest: Mapping[str, dict],
            reads: Reads | None = None) -> dict:
    """A sub-workflow step made ready: a run of the workflow it names starts from the commit this
    step would (its frozen copy, the person's flags of this run), and the step waits for it as
    work queued after a run does (`locks.after`): the dispatcher never starts it, and the child's
    end passes it with the branch the child kept (`_finish_parent`). A child that stops stops this
    run there; one that is cancelled cancels the step."""
    flows = dict(options.get("subflows") or {})
    frozen = flows.get(step["workflow"])
    if frozen is None:
        raise WorkflowError(f"step {step['id']!r}: {step['workflow']} was not frozen with run #{run['id']}")
    child_defn = normalize(frozen)
    given = fill_checks(child_defn, {k: _render(v, inputs) for k, v in dict(step.get("with") or {}).items()},
                        options.get("verify"))
    base = _base(store, run, defn, step["id"], latest, reads)
    kept = {k: options[k] for k in ("allow_tools", "allow_approval", "verify", "add_dirs", "origin_branch", "loop")
            if options.get(k)}
    child = store.create_run(name=child_defn["name"], scope=str(frozen.get("scope") or run["scope"]),
                             source=str(frozen.get("path") or run["source"]),
                             definition=json.dumps(_stored(child_defn), ensure_ascii=False),
                             inputs=json.dumps(given, ensure_ascii=False),
                             options=json.dumps({**kept, "subflows": flows,
                                                 **({"origin": base or options.get("origin")}
                                                    if base or options.get("origin") else {}),
                                                 **({"base": base} if base else {})}),
                             repo=run["repo"], cwd=run["cwd"], session_id=run["session_id"])
    task = store.create_task(f"Run the workflow {child_defn['name']} as step {step['id']!r} (run #{child['id']})",
                             status="queued", source="queue", author="workflow", repo=run["repo"], cwd=run["cwd"],
                             session_id=run["session_id"],
                             options=json.dumps({"subflow": {"run": child["id"], "workflow": child_defn["name"]},
                                                 after.KEY: {"run": child["id"]}}),
                             title=f"[{defn['name']}#{run['id']} {step['id']}] runs {child_defn['name']} "
                                   f"#{child['id']}"[:200], workflow_run=run["id"], workflow_step=step["id"])
    store.update_run(child["id"], options=json.dumps({**json.loads(child["options"]),
                                                      "parent": {"run": run["id"], "step": step["id"],
                                                                 "task": task["id"]}}))
    _queue_ready(store, store.get_run(child["id"]) or child, reads)
    return store.get_task(task["id"]) or task


def _finish_parent(store: Store, child: Mapping[str, Any], kick: Kick | None) -> list[int]:
    """A sub-workflow's run passed: the step that started it passes with the branch its last step
    kept, its cost the child's, and its run goes on (`advance`). The child's own end is no ending
    of its own: the session hears of the run that holds it."""
    parent = json.loads(child.get("options") or "{}").get("parent") or {}
    task = store.get_task(int(parent["task"])) if parent.get("task") else None
    if task is None or task["status"] != "queued":
        return []
    branch, paths, _, _ = after._kept(store, {"run": int(child["id"])})
    tip = None
    if branch and child["cwd"] and Path(child["cwd"]).is_dir():
        with contextlib.suppress(isolate.IsolationError):
            tip = isolate.head(Path(child["cwd"]), f"refs/heads/{branch}")
    steps = store.run_tasks(int(child["id"]))
    cost = round(sum(float(t.get("cost_usd") or 0) for t in steps), 4)
    with store.immediate():
        options = json.loads(task.get("options") or "{}")
        options.pop(after.KEY, None)
        store.update_task(task["id"], status="done", options=json.dumps(options), cost_usd=cost,
                          result=f"run #{child['id']} of {child['name']} passed"
                                 + (f"; its work is on {branch}" if branch else ", changing nothing"))
        store.add_event(task["id"], "finished", status="done", final_cell=None, cost_usd=cost, branch=branch, tip=tip,
                        changed=list(paths)[:200], changed_n=len(paths), stop=None, findings=[],
                        subrun=int(child["id"]), at=time.time())
        store.mark_reported([t["id"] for t in steps], via="workflow")
    return advance(store, store.get_task(task["id"]), kick=kick)


def _rerun(step: Mapping[str, Any], latest: Mapping[str, dict]) -> bool:
    """A ledger step goes again when a step it needs ran again after it: a recheck after the next fix round."""
    mine = latest[step["id"]]
    return bool(step.get("ledger")) and mine["status"] == "done" and any(
        (latest.get(d) or {}).get("id", 0) > mine["id"] for d in step["needs"])


def _fan(store: Store, run: Mapping[str, Any], defn: Mapping[str, Any], step: Mapping[str, Any],
         inputs: Mapping[str, str], options: Mapping[str, Any], latest: Mapping[str, dict], *,
         only: set[str] | None = None, reads: Reads | None = None) -> list[dict] | None:
    """The tasks of a step that runs in parts, queued: one per part of the plan it fans
    out over (`only`: those parts). None for a step that runs whole; [] when the plan's
    parts cannot run side by side, which takes its pass back. A plan of one part is the
    step run whole, as it is without one."""
    if not step.get("fanout"):
        areas = _areas(store, run, defn, step, latest, options, reads)
        if areas is None:
            return None
        return [_queue_step(store, run, defn, step, inputs, options, latest,
                            part={**area, "n": n, "of": len(areas)}, reads=reads)
                for n, area in enumerate(areas, 1) if only is None or area["id"] in only]
    plan = latest.get(step["fanout"])
    data = ((store.final_event(plan["id"]) or {}).get("data") or {}) if plan else {}
    parts = [p for p in data.get("parts") or () if isinstance(p, dict) and p.get("id")]
    contract = str(data.get("contract") or "")
    if len(parts) <= 1:
        one = {**parts[0], "n": 1, "of": 1, "plan": plan["id"], "contract": contract} if parts and plan else None
        return [_queue_step(store, run, defn, step, inputs, options, latest, part=one, reads=reads)]
    bare = [p["id"] for p in parts if not p.get("surfaces")]
    overlaps = [f"{pid} declares no surfaces" for pid in bare] + surfaces.overlapping(
        {p["id"]: list(p.get("surfaces") or ()) for p in parts})
    if overlaps:
        assert plan is not None
        why = ("its parts' surfaces overlap, so they cannot run side by side: " + "; ".join(overlaps))[:500]
        _take_back(store, run, plan, why)
        return []
    before = {_part(t)["id"]: t for t in _tasks_of(latest.get(step["id"])) if _part(t)} if only is not None else {}
    queued: dict[str, dict] = {}
    for n, p in enumerate(parts, 1):
        if only is not None and p["id"] not in only:
            continue
        queued[p["id"]] = _queue_step(store, run, defn, step, inputs, options, latest,
                                      part={**p, "n": n, "of": len(parts), "plan": plan["id"], "contract": contract,
                                            **_ordered(store, run, p, queued, before, reads)},
                                      reads=reads)
        if p["id"] in before:
            # A part still held behind the try this retry replaced waits for the retry instead.
            replaced = {"task": int(before[p["id"]]["id"])}
            for waiting in before.values():
                if after.held(waiting) and after.hold(waiting) == replaced:
                    options = json.loads(waiting.get("options") or "{}")
                    options[after.KEY] = {"task": int(queued[p["id"]]["id"])}
                    store.update_task(waiting["id"], options=json.dumps(options))
    return list(queued.values())


def _ordered(store: Store, run: Mapping[str, Any], part: Mapping[str, Any], queued: Mapping[str, dict],
             before: Mapping[str, dict], reads: Reads | None = None) -> dict[str, Any]:
    """Where a part that builds on another (`after`) starts: held behind that part's task
    queued with it, so it starts from what that part keeps (`locks.after`), or, on a retry
    whose earlier part passed already, from that part's kept tip. {} for a part with none."""
    dep = part.get("after")
    if not dep:
        return {}
    if dep in queued:
        return {"hold": {"task": int(queued[dep]["id"])}}
    done = before.get(dep)
    tip = _kept_tip(store, done, run["cwd"], reads) if done is not None and done["status"] == "done" else None
    return {"start": tip} if tip else {}


def _areas(store: Store, run: Mapping[str, Any], defn: Mapping[str, Any], step: Mapping[str, Any],
           latest: Mapping[str, dict], options: Mapping[str, Any],
           reads: Reads | None = None) -> list[dict[str, Any]] | None:
    """The areas a blind review of a large change reads apart, side by side: the surfaces
    the parts before it declared (`rest` for what none of them holds). None for a review that
    runs whole: no change to read, no parts that declared surfaces, one area, or at most
    `AREA_REVIEW_LINES` changed lines. A change with no parts is reviewed whole: split by its
    top-level folders it found the same bugs slower, at three times the cost, with four times
    the rows to refute (`bench/review/areas.py`)."""
    if not str(step.get("kind") or "").startswith("review") or step.get("ledger") or not options.get("origin"):
        return None
    base = _base(store, run, defn, step["id"], latest, reads)
    if not base or base == options["origin"] or not run["cwd"] or not Path(run["cwd"]).is_dir():
        return None
    changed = _numstat(run, options["origin"], base, reads)
    made = _generated(store, run, [path for _, path in changed or ()], reads)
    changed = [(n, path) for n, path in changed or () if path not in made]
    if not changed or sum(n for n, _ in changed) <= AREA_REVIEW_LINES:
        return None
    declared = {(_part(t) or {}).get("id"): list(json.loads(t.get("options") or "{}").get("surfaces") or ())
                for s in defn["steps"] if s.get("fanout") and s["id"] in _before(defn, step["id"])
                for t in _tasks_of(latest.get(s["id"])) if _part(t)}
    if not any(declared.values()):
        return None
    groups: dict[str, dict[str, Any]] = {}
    for _, path in changed:
        pid = next((pid for pid, bound in declared.items() if pid and bound and surfaces.allows(bound, path)), None)
        if pid is not None:
            spec = [f":(glob){x}" if any(c in x for c in "*?[") else x for x in declared[pid]]
        else:
            pid, spec = "rest", None
        area = groups.setdefault(pid, {"id": pid, "paths": [], "spec": spec})
        area["paths"].append(path)
    if len(groups) < 2:
        return None
    lines = sum(n for n, _ in changed)
    every = [path for _, path in changed]
    return [{**area, "spec": area["spec"] or area["paths"], "lines": lines,
             "others": [p for p in every if p not in area["paths"]]} for area in groups.values()]


def _generated(store: Store, run: Mapping[str, Any], paths: Sequence[str], reads: Reads | None = None) -> set[str]:
    """The changed paths no review reads: the ones the repository marks generated
    (`isolate.generated`) and the ones a step's own check wrote (its `check_rewrote`
    events). They are still changed files, listed as such; no model decides it."""
    if not paths:
        return set()
    wrote = {f for task in store.run_tasks(run["id"]) for e in store.events_of(task["id"], "check_rewrote")
             for f in e["data"].get("files") or ()}
    marked = _reading(reads, ("generated", str(run["cwd"]), tuple(paths)),
                   lambda: isolate.generated(Path(run["cwd"]), paths))
    return {p for p in paths if p in wrote} | marked


def _generated_note(store: Store, run: Mapping[str, Any], origin: str, base: str,
                    reads: Reads | None = None) -> str:
    """What a blind review of a change is told of the generated files in it, or ""."""
    changed = [path for _, path in _numstat(run, origin, base, reads) or ()]
    made = sorted(_generated(store, run, changed, reads))
    if not made:
        return ""
    more = f" and {len(made) - AREA_OTHERS} more" if len(made) > AREA_OTHERS else ""
    return (f"{len(made)} of the changed files are generated (marked so in `.gitattributes`, or written by the "
            f"run's own check): {', '.join(made[:AREA_OTHERS])}{more}. They are no review's: judge the sources "
            "that produce them, never their contents.")


def _area_brief(area: Mapping[str, Any], origin: str) -> str:
    """What one area's review reads: its part of the diff, and the other files the change touched."""
    others = list(area.get("others") or ())
    more = f" and {len(others) - AREA_OTHERS} more" if len(others) > AREA_OTHERS else ""
    return (f"This change is large ({area['lines']} changed lines), so it is reviewed by area, side by side, each "
            f"review blind and on its own; yours is `{area['id']}` ({area['n']} of {area['of']}). Its part of the "
            f"change is in this checkout: `git diff {origin[:12]} -- {' '.join(area['spec'])}` shows it. The other "
            f"files the change touched, which the other reviews read: {', '.join(others[:AREA_OTHERS])}{more}. "
            "Judge the whole request where this area answers it; what lies only in another area is its review's.")


def _take_back(store: Store, run: Mapping[str, Any], plan: Mapping[str, Any], why: str) -> None:
    """A plan's pass the engine cannot use, taken back: it stops as `parts`, a replan, and
    the run stops there, its lane paused as a step's stop pauses it; the session is told."""
    from locks import stops

    stops.record(store, plan["id"], stops.Stop("parts", why))
    if plan.get("dispatched"):
        store.pause_lane(run["repo"], f"task #{plan['id']} ended replan: {clip(why, 200, one_line=True)}")


def _quoted(text: str) -> str:
    """Text as a quoted block: context to read, never items a worker answers as criteria."""
    return "\n".join(f"> {line}" if line.strip() else ">" for line in str(text).strip().splitlines())


def _render(prompt: str, inputs: Mapping[str, str], *, quoted: bool = False) -> str:
    """A step's prompt with its inputs in it. `quoted`: each input as a quoted block, the
    request as context, for a step whose own criteria are a part's (its prompt), never
    the whole request's: the review that follows judges the whole."""
    if not quoted:
        return INPUT.sub(lambda m: inputs.get(m.group(1), m.group(0)), prompt).strip()
    out = INPUT.sub(lambda m: "\n" + _quoted(inputs[m.group(1)]) + "\n" if m.group(1) in inputs else m.group(0),
                    prompt)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", out)).strip()


def _part_brief(part: Mapping[str, Any]) -> str:
    """What a part's worker is told of its part, and the contract every part keeps, quoted as context."""
    if int(part.get("of") or 1) > 1:
        lines = [f"Your part: {part['id']} ({part['n']} of {part['of']}), as the plan (task #{part['plan']}) split "
                 "the work. Build this part only: the other parts are built beside you or after you, each in "
                 f"surfaces of its own, and joined after.\n{part['prompt']}"]
        if part.get("after"):
            lines.append(f"It builds on part {part['after']}, which was built before it: that part's work is "
                         "already in your checkout. Use it as it is; it is not yours to change.")
    else:
        lines = [f"The plan for it (task #{part['plan']}):\n{part['prompt']}"]
    if part.get("contract"):
        lines.append("The contract every part keeps, from the plan, verbatim. It is context, not a task of its own: "
                     "keep these names, types and interfaces exactly as written, so the parts join:\n"
                     + _quoted(part["contract"]))
    return "\n\n".join(lines)


#: The most files a step is handed as what the change it reads touches.
TOUCHES_MAX = 300


def _touches(run: Mapping[str, Any], options: Mapping[str, Any], base: str | None,
             area: Mapping[str, Any] | None, reads: Reads | None = None) -> list[str]:
    """The files of the change a reading step reads (its area's, for a review by area), for
    the capabilities about them (`Capability.touches`): a review of rendered UI gets what
    measures it. Empty when there is no change to read."""
    if area is not None:
        return list(area.get("paths") or ())[:TOUCHES_MAX]
    origin = options.get("origin")
    if not base or not origin or base == origin or not run["cwd"] or not Path(run["cwd"]).is_dir():
        return []
    return [path for _, path in _numstat(run, origin, base, reads) or ()][:TOUCHES_MAX]


def _links_expected(run: Mapping[str, Any], reads: Reads | None) -> bool:
    return bool(_reading(reads, ("links", str(run["cwd"])), lambda: specs_mod.links_expected(run["cwd"], os.environ)))


def _headline(inputs: Mapping[str, str]) -> str:
    """What a step's task is called after its bracket: the first line of the run's goal (its
    first input when it has none), so the runs of one workflow read apart on every list; the
    bracket already says which step it is."""
    text = str(inputs.get("goal") or next(iter(inputs.values()), "") or "")
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def reviewed_next(defn: Mapping[str, Any], step: Mapping[str, Any]) -> bool:
    """Whether a blind review of this step's change waits on it: a review step with no
    ledger that needs it directly. That review is given the request and the diff, as the
    independent verifier is, so a step it follows runs no verifier of its own. A step
    that only reads, decides a ledger or fixes its rows is never one; the parts an integrate
    step joins are, when a blind review follows the join."""
    if str(step.get("kind") or "").startswith("review") or step.get("ledger"):
        return False
    after = [s for s in defn["steps"] if step["id"] in (s.get("needs") or ())]
    # Parts a step runs in are reviewed through the integrate step that joins them.
    return any((str(s.get("kind") or "").startswith("review") and not s.get("ledger"))
               or (s.get("integrate") and reviewed_next(defn, s)) for s in after)


def _queue_step(store: Store, run: Mapping[str, Any], defn: Mapping[str, Any], step: Mapping[str, Any],
                inputs: Mapping[str, str], options: Mapping[str, Any], latest: Mapping[str, dict], *,
                part: Mapping[str, Any] | None = None, reads: Reads | None = None) -> dict:
    """Queue one step (or one `part` of it) as a task."""
    if step.get("ask") is not None:
        return _question(store, run, defn, step, inputs)
    if step.get("workflow") is not None:
        return _subrun(store, run, defn, step, inputs, options, latest, reads)
    split = part is not None and int(part.get("of") or 1) > 1 and "spec" not in part
    area = part if part is not None and "spec" in part else None
    joining = _joining(store, run, defn, step, latest, reads) if step.get("integrate") else None
    joins = joining is not None and len(joining["branches"]) > 1
    body = _render(step["prompt"], inputs, quoted=split or joins)
    if part is not None and area is None:
        body += "\n\n" + _part_brief(part)
    if joins:
        assert joining is not None
        body += "\n\n" + _join_brief(joining)
    earlier = _before(defn, step["id"])
    notes = []
    held: dict[str, Any] | None = None
    if step.get("ledger"):
        # A ledger step is scoped: its rows, never what the steps before said of their work.
        held = _ledger(store, run, defn, step, latest, options, reads)
        body += "\n\n" + held.pop("brief")
        earlier = []
    elif str(step.get("kind") or "").startswith("review") and options.get("origin") and \
            _base(store, run, defn, step["id"], latest, reads):
        # A review of a change is blind, as the independent check is: the request and
        # the change (the diff below), never what the steps before said of their own
        # work. With no change to read (a plan to critique), it gets what came before.
        earlier = []
    for sid in earlier:
        for task in _tasks_of(latest.get(sid)):
            said = []
            if task.get("result"):
                said.append(task["result"] if len(task["result"]) <= RESULT_CHARS
                            else task["result"][:RESULT_CHARS] + "…")
            # A step it waits on hands over its findings as records, not only its summary.
            found = findings_mod.read({"findings": _findings_of(store, task)}) if sid in step["needs"] else ()
            if found:
                said.append("Its findings:\n" + findings_mod.text(found[:FINDINGS_HANDED])[:FINDINGS_CHARS])
            if said:
                label = f"{sid}/{_part(task)['id']}" if _part(task) else sid
                notes.append(f"- {label} (task #{task['id']}): " + "\n".join(said))
    if notes:
        # Quoted: context, never items a worker answers as the request's criteria.
        body += (f"\n\nThis is step {step['id']!r} of the workflow {defn['name']!r}. What the steps before it "
                 "found and did (their changes are already in this checkout):\n" + _quoted("\n".join(notes)))
    if step.get("memory"):
        # Never empty: `normalize` drops an empty `memory`, and each reference writes a line.
        body += "\n\n" + memory_text(store, run["repo"], step["memory"])
    if options.get("loop"):
        from locks import loops

        # A round of a loop: its goal, mode, the person's rules and the diary so far.
        if told := loops.round_brief(store, options):
            body += "\n\n" + told
    # An integrate step starts where its parts started, and joins their branches there; a
    # part that builds on another starts from what that one kept.
    base = (part or {}).get("start") or (joining or {}).get("base") \
        or _base(store, run, defn, step["id"], latest, reads)
    if area is not None:
        body += "\n\n" + _area_brief(area, options["origin"])
    if str(step.get("kind") or "").startswith("review") and not step.get("ledger") and options.get("origin") \
            and base and base != options["origin"] and run["cwd"] and Path(run["cwd"]).is_dir() \
            and (made := _generated_note(store, run, options["origin"], base, reads)):
        body += "\n\n" + made
    elif base and options.get("origin") and base != options["origin"]:
        body += ("\n\nEverything this workflow changed so far is in this checkout: "
                 f"`git diff {options['origin'][:12]}` shows it.")
    if str(step.get("kind") or "").startswith("review") and options.get("origin") and \
            _links_expected(run, reads):
        # Workers here were told to link the specs they write to their code: a review checks that they did.
        body += "\n\n" + specs_mod.LINK_CHECK
    touches = _touches(run, options, base, area, reads) if (str(step.get("kind") or "").startswith("review")
                                                     or step.get("ledger")) else None
    trunk_for = trunks.is_trunk(defn, step["id"]) and trunks.enabled()
    if trunk_for and not base:
        # A trunk runs in a worktree at the run's commit: only there is its session kept to fork.
        base = options.get("origin")
    forks = trunks.source(store, (_tasks_of(latest.get(step["trunk"])) or [None])[-1], step["trunk"],
                          wrote=_step_kind(defn, step["trunk"]) in trunks.WRITING_TRUNKS) if step.get("trunk") else None
    opts = {k: v for k, v in {
        "kind": step.get("kind"), "verify": step_check(step, options, inputs), "start": step.get("start"),
        # The files the change it reads touches: capabilities about them join it.
        "touches": touches or None,
        # A step with no kind is classified when it runs: the run's check goes with it, for
        # orchestrate to apply if it turns out to write.
        "run_verify": options.get("verify") if not step.get("kind") and not step.get("verify")
        and step.get("ledger") in (None, "fix") else None,
        "max_turns": step.get("max_turns"),
        "allow_tools": options.get("allow_tools") or None, "allow_approval": options.get("allow_approval") or None,
        "base": base, "step_role": step.get("role"),
        # Another repository a run queued after work there reads (`after.reads`).
        "add_dirs": options.get("add_dirs") or None,
        "surfaces": list(part["surfaces"]) if split and part else step.get("surfaces"),
        "part": ({k: part[k] for k in ("id", "n", "of", "plan")} if split and part else
                 {"id": area["id"], "n": area["n"], "of": area["of"], "area": True} if area else None),
        # A plan a later step fans out over: its pass carries the parts and their contract.
        "parts": True if any(s.get("fanout") == step["id"] for s in defn["steps"]) else None,
        # A blind review of this step's change follows: it does the verifier's work.
        "reviewed_next": True if reviewed_next(defn, step) else None,
        "ledger": held,
        "integrate": {k: joining[k] for k in ("branches", "contract")} if joins and joining else None,
        # A round of a loop: the guard reads its mode and rules by this name.
        "loop": (options.get("loop") or {}).get("name") or None,
        # A step its later steps fork (its role goes in its brief), and the trunk this one forks.
        "trunk_for": True if trunk_for else None,
        "trunk": forks,
        # A run queued after other work: its steps wait for that work to pass, as it does; a
        # part that builds on another part waits for that one.
        after.KEY: (part or {}).get("hold") or options.get(after.KEY)}.items() if v is not None}
    empty = (held is not None and (not held["ids"] or bool(held.get("answered")))) or \
        (joining is not None and not joins)
    task = store.create_task(body, status="done" if empty else "queued", source="queue", author="workflow",
                             repo=run["repo"], cwd=run["cwd"], session_id=run["session_id"],
                             options=json.dumps(opts),
                             title=f"[{defn['name']}#{run['id']} {step['id']}"
                                   f"{'/' + part['id'] if part and (split or area) else ''}] "
                                   f"{_headline(inputs) or step['title']}"[:200],
                             workflow_run=run["id"], workflow_step=step["id"])
    if empty:
        # Nothing to decide, fix or join: the step passes without a worker, and no session is told of it.
        if held is not None:
            nothing = {"refute": "no finding needed a refuter", "fix": "no confirmed finding to fix",
                       "recheck": "no fix to check"}[held["role"]]
            outcome = ledger.Outcome(held["role"], held["round"], confirmed=list(held.get("agreed") or ()),
                                     held=list(held.get("held") or ()))
            if held.get("answered"):
                nothing = (f"the fix's independent check showed {', '.join(held['ids'])} fixed in the same diff: "
                           "no second reading")
                outcome = ledger.outcome(held, held["answered"])
                store.add_event(task["id"], "ledger_decided", seq=0, by="verifier", **outcome.data())
            decided = {"ledger": outcome.data()}
        else:
            assert joining is not None
            nothing = ("one part kept a branch: nothing to join, and what follows starts from it"
                       if joining["branches"] else "no part changed anything: nothing to join")
            decided = {}
        store.update_task(task["id"], result=nothing)
        store.add_event(task["id"], "finished", status="done", final_cell=None, cost_usd=0, branch=None,
                        changed=[], stop=None, findings=[], skipped=True, **decided)
        store.add_event(task["id"], "reported", via="workflow")
        task = store.get_task(task["id"])
    return task


def _joining(store: Store, run: Mapping[str, Any], defn: Mapping[str, Any], step: Mapping[str, Any],
             latest: Mapping[str, dict], reads: Reads | None = None) -> dict[str, Any]:
    """What an integrate step joins: the branch each part of the fanout steps it waits on
    kept, in the plan's order, the contract they keep, and the commit they started from."""
    fanned = [s for s in defn["steps"] if s.get("fanout") and s["id"] in _before(defn, step["id"])]
    branches, contracts, base = [], [], None
    for s in fanned:
        plan = latest.get(s["fanout"])
        said = (((store.final_event(plan["id"]) or {}).get("data") or {}).get("contract") or "") if plan else ""
        if said and said not in contracts:
            contracts.append(said)
        for t in _tasks_of(latest.get(s["id"])):
            branch = ((store.final_event(t["id"]) or {}).get("data") or {}).get("branch")
            if branch:
                _kept_tip(store, t, run["cwd"], reads)  # a part's branch is joined by name: put back when gone
                branches.append({"part": (_part(t) or {}).get("id") or s["id"], "task": t["id"], "branch": branch})
            started = store.last_event(t["id"], "attempt_started")
            base = base or ((started or {}).get("data") or {}).get("base")
        base = base or _base(store, run, defn, s["id"], latest, reads)
    return {"branches": branches, "contract": "\n\n".join(contracts), "base": base}


def _join_brief(joining: Mapping[str, Any]) -> str:
    """What an integrate step's brief says it joins (no list items: they are no criteria)."""
    lines = [f"  {b['part']} — {b['branch']} (task #{b['task']})" for b in joining["branches"]]
    return ("The parts to join, in the plan's order, each kept on its own branch from "
            f"{str(joining.get('base') or 'the same commit')[:12]}. locks merges them before any worker runs; a "
            "worker is needed only where they conflict or the check fails on the joined work:\n" + "\n".join(lines)
            + ("\n\nThe contract every part keeps, from the plan, verbatim (context: keep it):\n"
               + _quoted(joining["contract"]) if joining.get("contract") else ""))


def writes(step: Mapping[str, Any]) -> bool:
    """A step whose kind changes code: the one a run's `--verify` checks."""
    kind = step.get("kind")
    return bool(kind) and kind not in READ_ONLY_KINDS and step.get("ledger") in (None, "fix")


def step_check(step: Mapping[str, Any], options: Mapping[str, Any],
               inputs: Mapping[str, str] | None = None) -> str | None:
    """The check a step runs: its own, with the run's inputs in it as its prompt has them,
    else the run's `--verify` for a step that writes (the person's command, taken as typed)."""
    own = step.get("verify")
    if own:
        given = inputs or {}
        return CHECK_INPUT.sub(lambda m: given.get(m.group(1), m.group(0)), str(own)).strip() or None
    return options.get("verify") if writes(step) else None


def unfilled_checks(defn: Mapping[str, Any], inputs: Mapping[str, str]) -> list[str]:
    """Each step whose own `verify` still holds a `{name}` once the inputs are in it: run
    as it is, the shell would only answer `{check}: not found` (exit 127) and stop work
    that passed. Said with the input to pass, or the shell text to write instead."""
    out = []
    for step in defn["steps"]:
        check = step_check(step, {}, inputs) if step.get("verify") else None
        for name in dict.fromkeys(CHECK_INPUT.findall(check or "")):
            how = (f"pass it with `--input {name}='<command>'`" if name in defn["inputs"]
                   else f"add `{name}` to the workflow's inputs and pass it with `--input {name}='<command>'`, "
                        "or, when it is shell text, write it without the braces touching the name")
            out.append(f"step {step['id']!r} checks with `{step['verify']}`, which leaves {{{name}}} unfilled: {how}")
    return out


def _findings_of(store: Store, task: Mapping[str, Any] | None) -> list[dict]:
    """The findings a step's task passed with; a step run in parts, every part's."""
    out: list[dict] = []
    for t in _tasks_of(task):
        finished = store.final_event(t["id"]) if t.get("status") == "done" else None
        out += list((finished or {}).get("data", {}).get("findings") or [])
    return out


def reviewer_yield(store: Store, repo: str | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
    """What each blind review of a ledger with two reviews or more added, over the last
    `limit` runs (one repository's, or every one): the rows it put on the ledger, the ones no
    other review saw, and of those the ones the refuter or a proof kept. A review whose rows
    another always saw too costs its run and adds nothing: `locks stats` says so, it changes no
    definition. Read only."""
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for run in store.list_runs(repos=None if repo is None else {repo}, limit=limit):
        defn = _definition(run)
        latest = _latest(store, run["id"])
        by_id = {s["id"]: s for s in defn["steps"]}
        for step in defn["steps"]:
            decided = _decided(store, latest.get(step["id"])) if step.get("ledger") == "refute" else {}
            if not decided:
                continue
            sources = [sid for sid in _before(defn, step["id"]) if not by_id[sid].get("ledger")
                       and by_id[sid].get("kind") in READ_ONLY_KINDS and latest.get(sid)]
            if len(sources) < 2:
                continue
            rows = ledger.collect([(sid, _findings_of(store, latest[sid])) for sid in sources])
            kept = set(decided.get("confirmed") or ())
            for sid in sources:
                key = (defn["name"], sid, str(latest[sid].get("final_cell") or "?"))
                row = out.setdefault(key, {"workflow": key[0], "step": sid, "cell": key[2], "runs": 0, "rows": 0,
                                           "alone": 0, "alone_kept": 0})
                mine = [r for r in rows if sid in r["seen_by"]]
                alone = [r for r in mine if r["seen_by"] == [sid]]
                row["runs"] += 1
                row["rows"] += len(mine)
                row["alone"] += len(alone)
                row["alone_kept"] += sum(1 for r in alone if r["id"] in kept)
    return sorted(out.values(), key=lambda r: (r["workflow"], r["step"], r["cell"]))


def _decided(store: Store, task: Mapping[str, Any] | None) -> dict:
    finished = store.final_event(task["id"]) if task and task.get("status") == "done" else None
    return dict((finished or {}).get("data", {}).get("ledger") or {})


def _ledger(store: Store, run: Mapping[str, Any], defn: Mapping[str, Any], step: Mapping[str, Any],
            latest: Mapping[str, dict], options: Mapping[str, Any], reads: Reads | None = None) -> dict[str, Any]:
    """The rows a ledger step is given, read from the steps before it, never from a worker's words.
    Every step sees the same rows by the same ids: the ledger is rebuilt the same way each time."""
    role, earlier = step["ledger"], _before(defn, step["id"])
    by_id = {s["id"]: s for s in defn["steps"]}
    sources = [sid for sid in earlier if not by_id[sid].get("ledger")]
    reviewers = [sid for sid in sources if by_id[sid].get("kind") in READ_ONLY_KINDS]
    rows = ledger.collect([(sid, _findings_of(store, latest.get(sid))) for sid in sources])
    known = {r["id"]: r for r in rows}
    nearest = {r: next((sid for sid in reversed(earlier) if by_id[sid].get("ledger") == r), None)
               for r in ledger.ROLES}
    round_, diff, held, noted_only = 1, "", [], []
    if role == "refute":
        mine = [r for r in rows if ledger.disputed(r)]
        agreed = [r["id"] for r in rows if not ledger.disputed(r)]
    else:
        refuted = _decided(store, latest.get(nearest["refute"])) if nearest["refute"] else {}
        confirmed = refuted.get("confirmed", [r["id"] for r in rows]) if refuted else [r["id"] for r in rows]
        held, agreed = list(refuted.get("held") or ()), []
        if role == "fix":
            # A later round fixes what the recheck after this fix left open.
            after = next((s for s in defn["steps"] if s.get("ledger") == "recheck"
                          and step["id"] in _before(defn, s["id"])), None)
            checked = _decided(store, latest.get(after["id"])) if after else {}
            if checked and checked.get("survivors"):
                confirmed, round_ = checked["survivors"], int(checked.get("round") or 1) + 1
            mine = [known[i] for i in confirmed if i in known]
        else:
            fix = latest.get(nearest["fix"]) if nearest["fix"] else None
            spec = json.loads(fix.get("options") or "{}").get("ledger") or {} if fix else {}
            mine = [known[i] for i in spec.get("ids", []) if i in known] if fix and fix["status"] == "done" else []
            round_, held = int(spec.get("round") or 1), list(spec.get("held") or held)
            finished = store.final_event(fix["id"]) if fix else None
            branch = (finished or {}).get("data", {}).get("branch")
            start = spec.get("base") or options.get("origin")
            if branch and start and run["cwd"] and Path(run["cwd"]).is_dir():
                with contextlib.suppress(isolate.IsolationError):
                    tip = _reading(reads, ("head", str(run["cwd"]), branch),
                                lambda: isolate.head(Path(run["cwd"]), branch))
                    diff = f"git diff {start[:12]} {tip[:12]}"
                    changed = [path for _, path in _numstat(run, start, tip, reads) or ()]
                    if changed and all(ledger.written_down(path) for path in changed):
                        # The fix only wrote things down: no row but one about those files is fixed by it.
                        noted_only = [r["id"] for r in mine
                                      if not ledger.about_notes(str(r.get("where") or ""))]
    deferred: list[str] = []
    if role == "recheck":
        deferred = list(spec.get("deferred") or ()) if fix else []
    elif mine:
        mine, deferred = ledger.fit(mine)
    sha = ledger.digest(mine)
    spec = {"role": role, "round": round_, "ids": [r["id"] for r in mine], "digest": sha, "held": held,
            "rows": len(rows)}
    if deferred:
        spec["deferred"] = deferred
    if role == "refute":
        spec["agreed"] = agreed
    if role == "fix":
        spec["base"] = _base(store, run, defn, step["id"], latest, reads) or options.get("origin")
    if noted_only:
        spec["noted_only"] = noted_only
    if role == "recheck" and mine and not noted_only and fix and fix["status"] == "done":
        # The fix's own independent check read the same diff and answered every row fixed: the
        # re-check would read it again, so it is decided with no worker (`orchestrate.verifier_brief`).
        checked = (store.last_event(fix["id"], "verified") or {}).get("data") or {}
        said = {v.get("id"): v.get("verdict") for v in checked.get("verdicts") or ()}
        if checked.get("passed") and all(said.get(r["id"]) == "fixed" for r in mine):
            spec["answered"] = [v for v in checked["verdicts"] if v.get("id") in spec["ids"]]
    spec["brief"] = ledger.brief(role, mine, round_=round_, sha=sha, diff=diff,
                                 reviewers=max(1, len(reviewers)),
                                 waiting=len(deferred) if role != "recheck" else 0) if mine else \
        f"Ledger ({role}): nothing to do."
    return spec


def _before(defn: Mapping[str, Any], step_id: str) -> list[str]:
    """Every step a step waits on, directly or not, in definition order."""
    needs = {s["id"]: s["needs"] for s in defn["steps"]}
    seen, todo = set(), list(needs[step_id])
    while todo:
        sid = todo.pop()
        if sid not in seen:
            seen.add(sid)
            todo.extend(needs[sid])
    return [s["id"] for s in defn["steps"] if s["id"] in seen]


def _kept_tip(store: Store, task: Mapping[str, Any], cwd: str | None, reads: Reads | None = None) -> str | None:
    """The commit a step's task kept, as the run reads it (`_kept_tip_now`), read ahead when `reads` holds it."""
    return _reading(reads, ("tip", task["id"], str(cwd)), lambda: _kept_tip_now(store, task, cwd))


def _kept_tip_now(store: Store, task: Mapping[str, Any], cwd: str | None) -> str | None:
    """The commit a step's task kept, as the run reads it: its branch's tip. A branch
    gone while the run still builds on it is put back at the tip its `finished` event
    kept; one that cannot be is a `WorkflowError`, never a silent start elsewhere.
    None when the task kept no branch."""
    data = (store.final_event(task["id"]) or {}).get("data") or {}
    branch, tip = data.get("branch"), data.get("tip")
    if not branch or not cwd or not Path(cwd).is_dir():
        return None
    try:
        return isolate.head(Path(cwd), f"refs/heads/{branch}")
    except isolate.IsolationError:
        pass
    if tip and isolate.restore(Path(cwd), branch, tip):
        store.add_event(task["id"], "branch_restored", branch=branch, tip=tip,
                        why="a later step of its run builds on it")
        return tip
    raise WorkflowError(f"task #{task['id']} kept its work on {branch}, and that branch is gone"
                        + (f" (its tip {tip[:12]} is not in the repository either)" if tip else "")
                        + f": `git fsck --lost-found` in {cwd} may find it; restore the branch, "
                        f"then `locks workflow retry` the step that needs it")


def _base(store: Store, run: Mapping[str, Any], defn: Mapping[str, Any], step_id: str,
          latest: Mapping[str, dict], reads: Reads | None = None) -> str | None:
    """The commit a step starts from: the tip the nearest step before it kept,
    read when it passed. A reading step keeps no branch, so the search goes on
    past it; with none before it, the commit a run queued after other work was
    released onto (`locks.after`). None: the checkout's HEAD. A fix's later round
    starts from what the round before it kept, never from what it fixed again."""
    step = next((s for s in defn["steps"] if s["id"] == step_id), {})
    before = latest.get(step_id)
    if step.get("ledger") == "fix" and before and before["status"] == "done":
        tip = _kept_tip(store, before, run["cwd"], reads)
        if tip:
            return tip
    for sid in reversed(_before(defn, step_id)):
        # A step run in parts keeps a branch per part: what comes after starts from the
        # integrate step that joins them, unless only one part kept one.
        kept = [t for t in _tasks_of(latest.get(sid))
                if ((store.final_event(t["id"]) or {}).get("data") or {}).get("branch")]
        if len(kept) == 1:
            tip = _kept_tip(store, kept[0], run["cwd"], reads)
            if tip:
                return tip
    return json.loads(run.get("options") or "{}").get("base")


def drift(store: Store, task: Mapping[str, Any], tip: str, when: str) -> dict[str, Any] | None:
    """How far a step's work (`tip`: the commit it starts from, or its branch) is behind
    the branch its run started from, as that branch is now, kept as a `behind_base`
    event (`when`: start or end). Said, never acted on: nothing is merged or rebased."""
    run = store.get_run(int(task["workflow_run"])) if task.get("workflow_run") else None
    base = json.loads(run["options"] or "{}").get("origin_branch") if run else None
    if not base or not run or not run["cwd"] or not Path(run["cwd"]).is_dir():
        return None
    found = isolate.behind(Path(run["cwd"]), tip, base)
    if found is not None:
        store.add_event(task["id"], "behind_base", when=when, **found)
    return found


def _behind(store: Store, run: Mapping[str, Any], task: Mapping[str, Any], finished: Mapping[str, Any] | None,
            *, live: bool) -> str | None:
    """A step's distance from its run's base branch: read from git now (`live`), else as last kept."""
    base = json.loads(run["options"] or "{}").get("origin_branch")
    if not base:
        return None
    if live and run["cwd"] and Path(run["cwd"]).is_dir():
        branch = ((finished or {}).get("data") or {}).get("branch")
        started = store.last_event(task["id"], "attempt_started")
        tip = f"refs/heads/{branch}" if branch else ((started or {}).get("data") or {}).get("base")
        found = isolate.behind(Path(run["cwd"]), tip, base) if tip else None
        if found is not None:
            return isolate.behind_text(found)
    kept = store.last_event(task["id"], "behind_base")
    return isolate.behind_text(kept["data"]) if kept else None


def advance(store: Store, task: Mapping[str, Any] | None, env: Mapping[str, str] | None = None,
            kick: Kick | None = None) -> list[int]:
    """After a task's run ended: when it passed, release the work queued after it
    (`locks.after`) and, for a step, queue what waited on it; then start the
    dispatcher. A run whose last step passed releases the work queued after the
    run. The session that started the run is told at its end, or when a step waits
    on the person, never of each step the engine went past. The ids queued or released."""
    if not task:
        return []
    released = _release(store, kick, task=task)
    if not task.get("workflow_run"):
        return released
    run = store.get_run(int(task["workflow_run"]))
    if run is None or run["status"] != "active":
        return released
    if task["status"] != "done":
        # A step that stops stops the run there: the run ends now, as far as its branches go.
        _drift_at_end(store, run)
        return []
    # Two steps a third waits on may pass at the same moment: one of them queues it.
    def ready(reads: Reads) -> list[int]:
        queued = _queue_ready(store, run, reads)
        if queued:
            store.add_event(task["id"], "reported", via="workflow")
            store.add_event(task["id"], "workflow_advanced", run=run["id"], queued=queued)
            _advanced(store, task, queued)
        return queued

    queued = _transaction(store, run, lambda reads: _read_ahead(store, run, reads), ready)
    if (store.get_task(task["id"]) or {}).get("status") != "done":
        # Its pass was taken back (a plan whose parts cannot run side by side): the run stops there.
        _drift_at_end(store, run)
        return released
    if not queued:
        queued = _next_round(store, run, task)
    if queued:
        (kick or _kick)(run["cwd"], run["repo"])
        store.update_run(run["id"])
    elif all(t["status"] == "done" for t in _latest(store, run["id"]).values()) and \
            len(_latest(store, run["id"])) == len(_definition(run)["steps"]):
        # The run is over: its definition is free, a change that waited for it applies,
        # and the work queued after it starts on what its last step kept.
        store.update_run(run["id"], status="done")
        _drift_at_end(store, run)
        promote(run["source"], store)
        # A sub-workflow's run passes the step of the run that started it, before anything is released.
        released += _finish_parent(store, store.get_run(run["id"]) or run, kick)
        released += _release(store, kick, run=store.get_run(run["id"]))
    else:
        # A step that passed while a sibling still runs: the engine went past it, and the session is not told.
        store.add_event(task["id"], "reported", via="workflow")
        store.update_run(run["id"])
    return [*queued, *released]


def _transaction(store: Store, run: Mapping[str, Any], read_ahead: Callable[[Reads], None],
                 write: Callable[[Reads], Any]) -> Any:
    """`write` in one write transaction, with git read before it opens (`read_ahead`, on a `Reads`):
    every other writer waits while the transaction is open, and a git command is the slowest thing
    it could hold them for. A call `write` makes that was not read ahead (another process advanced
    the run since) leaves the transaction, is read, and the whole is tried again; it gives up
    when `READ_RETRIES` rounds in a row read nothing new."""
    reads = Reads()
    stalled = 0
    while True:
        reads.frozen = False
        read_ahead(reads)
        reads.frozen = True
        try:
            with store.immediate():
                return write(reads)
        except _Reread as missing:
            reads.frozen = False
            known = len(reads.memo)
            reads.fill(missing.key, missing.thunk)
            # Steps made ready in the same pass (by a skipped one) each ask for calls nobody could
            # read ahead: a round that read something new is progress, one that read nothing is stuck.
            stalled = 0 if len(reads.memo) > known else stalled + 1
            if stalled > READ_RETRIES:
                raise WorkflowError(f"run #{run['id']} kept changing while its next steps were being queued: "
                                    "nothing was queued, advance it again") from None


def _advanced(store: Store, task: Mapping[str, Any], queued: list[int]) -> None:
    """On each task the engine queued after `task` passed: how long that took, from its
    ending (`advance_s`), kept for the run of each to put on its own ending."""
    passed = (((store.final_event(task["id"]) or {}).get("data") or {}).get("at"))
    if not isinstance(passed, int | float) or isinstance(passed, bool):
        return
    took = round(max(time.time() - passed, 0.0), 3)
    for task_id in queued:
        store.add_event(task_id, "advanced", after=task["id"], advance_s=took)


def _release(store: Store, kick: Kick | None, *, task: Mapping[str, Any] | None = None,
             run: Mapping[str, Any] | None = None) -> list[int]:
    """The work queued after a task or a run that passed, released, and each of its lanes started."""
    freed = after.release(store, task=task, run=run)
    for t in freed:
        (kick or _kick)(t["cwd"], t["repo"])
    return [t["id"] for t in freed]


def _drift_at_end(store: Store, run: Mapping[str, Any]) -> None:
    """At a run's end, done or stopped at a step: each kept branch against the base as it is then."""
    for entry in _latest(store, run["id"]).values():
        for step in _tasks_of(entry):
            branch = ((store.final_event(step["id"]) or {}).get("data") or {}).get("branch")
            if branch:
                drift(store, step, f"refs/heads/{branch}", "end")


def _next_round(store: Store, run: Mapping[str, Any], task: Mapping[str, Any]) -> list[int]:
    """A recheck that passed with rows still open queues its fix again, for one more round."""
    spec = json.loads(task.get("options") or "{}").get("ledger") or {}
    decided = _decided(store, task)
    if spec.get("role") != "recheck" or not decided.get("survivors") or int(decided.get("round") or 1) >= ledger.ROUNDS:
        return []
    defn = _definition(run)

    def fix_step(latest: Mapping[str, dict]) -> dict | None:
        """The fix to go again, or None when this recheck is not the newest word on it."""
        fix = next((s for s in reversed(defn["steps"]) if s.get("ledger") == "fix"
                    and s["id"] in _before(defn, task["workflow_step"])), None)
        return None if fix is None or latest[fix["id"]]["id"] > task["id"] else fix

    def read_ahead(reads: Reads) -> None:
        latest = _latest(store, run["id"])
        if (fix := fix_step(latest)) is not None:
            _read_step(store, run, defn, fix, latest, json.loads(run["options"]), reads)

    def again(reads: Reads) -> dict | None:
        latest = _latest(store, run["id"])
        if (fix := fix_step(latest)) is None:
            return None
        queued = _queue_step(store, run, defn, fix, json.loads(run["inputs"]), json.loads(run["options"]), latest,
                             reads=reads)
        store.add_event(task["id"], "reported", via="workflow")
        store.add_event(task["id"], "workflow_advanced", run=run["id"], queued=[queued["id"]], round=True)
        _advanced(store, task, [queued["id"]])
        return queued

    queued = _transaction(store, run, read_ahead, again)
    return [queued["id"]] if queued and queued["status"] == "queued" else []


def progress(store: Store, run: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
    """How far one step's task got, read from git and its events: its attempt and
    what it is doing (running), the files it touched, the last commit it kept."""
    from locks import phase

    out: dict[str, Any] = {}
    started = store.last_event(task["id"], "attempt_started")
    if started:
        out["attempt"] = started["data"].get("seq")
    if task["status"] == "running":
        out["phase"] = phase.of(task["id"], out.get("attempt"))
        files = isolate.changed_so_far(home(), Path(run["cwd"]), task["id"], started["data"] if started else {})
        if files is not None:
            out["files"] = list(files)
        return out
    finished = (store.final_event(task["id"]) or {}).get("data") or {}
    out["files"] = list(finished.get("changed") or [])
    out["files_n"] = finished.get("changed_n") or len(out["files"])
    branch = finished.get("branch")
    if branch and run["cwd"] and Path(run["cwd"]).is_dir():
        with contextlib.suppress(isolate.IsolationError, OSError):
            line = isolate.last_commit(Path(run["cwd"]), branch)
            if line:
                out["commit"] = line
    return out


def status(store: Store, run_id: int, *, detail: bool = False) -> dict[str, Any]:
    """Where a run is: each step's state and task, the steps running now, and the run's own state.
    `detail` adds each started step's progress (git and events; `progress`)."""
    run = store.get_run(run_id)
    if run is None:
        raise WorkflowError(f"no workflow run #{run_id}")
    defn, latest = _definition(run), _latest(store, run_id)
    every = store.run_tasks(run_id)
    steps = []
    for step in defn["steps"]:
        task = latest.get(step["id"])
        state = "pending" if task is None else task["status"]
        entry: dict[str, Any] = {"id": step["id"], "title": step["title"], "needs": step["needs"], "state": state,
                                 # What the step is, for a graph: a question, another workflow, a condition.
                                 **({"asks": list(step.get("options") or ())} if step.get("ask") is not None else {}),
                                 **({"runs": step["workflow"]} if step.get("workflow") else {}),
                                 **({"when": dict(step["when"])} if step.get("when") else {})}
        if task and task.get("parts"):
            # A step run in parts: each part under it, the step's state theirs together.
            parts = [{"id": _part(t)["id"], "state": t["status"],
                      **_task_entry(store, run, t, detail, tries=sum(
                          1 for o in every if o["workflow_step"] == step["id"] and _part(o)
                          and _part(o)["id"] == _part(t)["id"]))} for t in task["parts"]]
            at = next((p for p in parts if p["state"] == state), parts[-1])
            entry.update(task=at["task"], cell=at.get("cell"), cost_usd=round(sum(p["cost_usd"] for p in parts), 2),
                         **({"cost_running": True} if any(p.get("cost_running") for p in parts) else {}),
                         points=sum(p["points"] or 0 for p in parts) or None,
                         result=None, branch=None, tries=max(p["tries"] for p in parts), waits=at.get("waits"),
                         parts=parts)
        elif task:
            entry.update(_task_entry(store, run, task, detail,
                                     tries=sum(1 for t in every if t["workflow_step"] == step["id"])))
        steps.append(entry)
    states = {s["state"] for s in steps}
    # Queued after other work (`locks.after`): every step that is open waits for that work to pass.
    holding = [s for s in steps if s["state"] in ("queued", "running")]
    holding = holding if holding and all(after.held(latest[s["id"]]) and not s.get("subrun")
                                         for s in holding) else []
    if run["status"] == "cancelled":
        state = "cancelled"
    elif run["status"] == "done" or states == {"done"}:
        state = "done"
    elif states & NEEDS_YOU or (states & STOPPED and not states & {"queued", "running"}):
        state = "waits on the orchestrator"
    elif holding:
        state = "queued"
    elif states & {"queued", "running"}:
        state = "running"
    else:
        state = "stalled"
    branch = next((s["branch"] for s in reversed(steps) if s.get("branch")), None)
    return {"id": run["id"], "name": run["name"], "scope": run["scope"], "version": defn["version"],
            "source": run["source"], "repo": run["repo"], "cwd": run["cwd"],
            "session_id": run["session_id"], "inputs": json.loads(run["inputs"]), "state": state,
            "now": [s["id"] for s in steps if s["state"] in ("queued", "running")],
            "waiting": [s["id"] for s in steps if s["state"] in NEEDS_YOU | STOPPED],
            "branch": branch, "cost_usd": round(sum(s.get("cost_usd") or 0 for s in steps), 2),
            # A running step whose cost so far could not be read: the total is a floor, said so.
            "cost_running": any(s.get("cost_running") for s in steps),
            # Its size: its steps' points, each part of a step run in parts counted.
            "points": sum(s.get("points") or 0 for s in steps),
            "created_at": run["created_at"], "updated_at": run["updated_at"], "reason": run["reason"],
            "waits": holding[0]["waits"] if holding else None, "steps": steps}


def _task_entry(store: Store, run: Mapping[str, Any], task: Mapping[str, Any], detail: bool, *,
                tries: int) -> dict[str, Any]:
    """One step's (or part's) task as `status` shows it: its cell, cost, result, branch, tries and wait."""
    from locks import usage

    finished = store.final_event(task["id"])
    entry: dict[str, Any] = dict(
        task=task["id"], cell=task.get("final_cell") or task.get("current_cell"),
        # A running attempt's cost so far, from its live transcript; "running" when it cannot be read.
        **usage.so_far(store, task), points=task.get("points"), result=task.get("result"),
        branch=(finished or {}).get("data", {}).get("branch") if task["status"] == "done" else None,
        tries=tries, waits=dispatch.waiting(store, task))
    own = json.loads(task.get("options") or "{}")
    if (own.get("subflow") or {}).get("run"):
        # A sub-workflow step: the run it started, which `locks workflow status` shows step by step.
        entry["subrun"] = int(own["subflow"]["run"])
    if own.get("question"):
        entry["options"] = list(own["question"].get("options") or ())
        if answered := outcome(store, task).get("answer"):
            entry["answer"] = answered
    if own.get("skipped"):
        entry["skipped"] = own["skipped"]
    if task["status"] not in ("queued", "running", "done"):
        from locks import stops

        if (stop := stops.of(store, task)) is not None:
            entry["stopped"] = stop.reason
        # The work a step that stopped short kept, unverified, on its branch.
        if kept := ((finished or {}).get("data") or {}).get("branch"):
            entry["kept_branch"] = kept
    if behind := _behind(store, run, task, finished, live=detail):
        entry["behind"] = behind
    if detail:
        entry.update(progress(store, run, task))
    return entry


def runs(store: Store, *, repos: set[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
    return [status(store, r["id"]) for r in store.list_runs(repos=repos, limit=limit)]


def cancel(store: Store, run_id: int, *, via: str = "cli") -> list[int]:
    """Stop a run: no step starts after this. Its queued steps are cancelled; a
    running one is asked to stop, as `locks cancel` asks."""
    from locks import stops

    run = store.get_run(run_id)
    if run is None:
        raise WorkflowError(f"no workflow run #{run_id}")
    store.update_run(run_id, status="cancelled", reason=f"cancelled {via}")
    promote(run["source"], store)
    stopped = []
    for task in (t for entry in _latest(store, run_id).values() for t in _tasks_of(entry)):
        if task["status"] == "queued":
            stops.record(store, task["id"], stops.Stop("cancelled", "its workflow run was cancelled", by="you",
                                                       extra={"via": via}))
            stopped.append(task["id"])
        elif task["status"] == "running":
            store.request_cancel(task["id"], via=via)
            if task.get("pid"):
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.kill(int(task["pid"]), signal.SIGTERM)
            stopped.append(task["id"])
    # Work queued after this run will never be released by it: it leaves the queue too.
    after.abandon(store, {"run": run_id}, f"it waited for run #{run_id} to pass, which was cancelled {via}")
    for task in (t for entry in _latest(store, run_id).values() for t in _tasks_of(entry)):
        # A sub-workflow's run started by this one stops with it.
        child = json.loads(task.get("options") or "{}").get("subflow") or {}
        if child.get("run") and (store.get_run(int(child["run"])) or {}).get("status") == "active":
            stopped += cancel(store, int(child["run"]), via=via)
    return stopped


def retry(store: Store, run_id: int, step_id: str, kick: Kick | None = None) -> dict:
    """Queue a step again as a new task, after its last try stopped: the run goes on from there."""
    from locks import stops

    run = store.get_run(run_id)
    if run is None:
        raise WorkflowError(f"no workflow run #{run_id}")
    if run["status"] != "active":
        raise WorkflowError(f"run #{run_id} is {run['status']}")
    defn, latest = _definition(run), _latest(store, run_id)
    step = defn["steps"][_index(defn, step_id)]
    last = latest.get(step_id)
    if last is not None and last["status"] in ("queued", "running", "done"):
        raise WorkflowError(f"step {step_id!r} is {last['status']} (task #{last['id']}): nothing to retry")
    if any((latest.get(d) or {}).get("status") != "done" for d in step["needs"]):
        raise WorkflowError(f"step {step_id!r} waits on {step['needs']}, which have not all passed")
    inputs, options = json.loads(run["inputs"]), json.loads(run["options"])
    if last is not None and last.get("parts"):
        # A step run in parts: only the parts that did not pass go again; the ones that passed stand.
        stopped = [t for t in last["parts"] if t["status"] not in ("queued", "running", "done")]
        tasks = _fan(store, run, defn, step, inputs, options, latest, only={_part(t)["id"] for t in stopped}) or []
        if not tasks:
            raise WorkflowError(f"step {step_id!r}: its parts could not be queued again")
        again = {_part(t)["id"]: t["id"] for t in tasks}
        for t in stopped:
            stops.retried(store, t, again.get(_part(t)["id"], tasks[0]["id"]))
            store.release_lane(run["repo"], t["id"])
        (kick or _kick)(run["cwd"], run["repo"])
        return {**tasks[0], "parts_retried": [(_part(t)["id"], t["id"]) for t in tasks]}
    tasks = _queue_one(store, run, defn, step, inputs, options, latest)
    if not tasks:
        raise WorkflowError(f"step {step_id!r}: the plan it fans out over was taken back; retry the plan")
    task = tasks[0]
    if last is not None:
        # The try that stopped is superseded by this one, and paused the lane; a retry is the person saying go on.
        stops.retried(store, last, task["id"])
        store.release_lane(run["repo"], last["id"])
    (kick or _kick)(run["cwd"], run["repo"])
    return task
