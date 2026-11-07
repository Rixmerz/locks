"""What kind of work a request is.

Two passes. **Rules** first: a handful of patterns that are right when they
fire — an explicit `#kind` tag, a stack trace, a bare "commit and push". They
cost nothing and they are deterministic, so they decide everything they can.
They are deliberately *not* asked to decide everything: a keyword list that
must classify every request ends up ordering its rules so the first match wins,
and "the header no longer looks right" lands in debug because of "no longer".

Everything the rules do not claim goes to **Haiku**, with a JSON schema and no
tools. A request classified by a model below the confidence floor, or a model
that could not be run, is the default kind — which is the middle of the matrix,
not a guess.

`frontier` (Fable) is never a model's decision. Only the `#fable` tag reaches it.

The two mistakes are not equal. A change read as a reading task gets a worker
with no write tool, which can only report what it did not do; a reading task
read as a change gets a worktree that is dropped empty. So a request that opens
a clause with a verb of change is never read-only unless a tag or the caller
says so.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from locks import legacy, models
from locks.matrix import DEFAULT_KIND, KINDS
from locks.text import clip, fold, words

COMPLEXITIES = ("trivial", "low", "medium", "high")
MIN_CONFIDENCE = 0.6
CLASSIFIER_TIMEOUT_S = 60

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Classification:
    kind: str
    complexity: str = "medium"
    source: str = "default"  # rule | model | default
    reason: str = ""
    cost_usd: float = 0.0


# --- rules ----------------------------------------------------------------

_CRITICAL = re.compile(
    r"\b(auth\w*|login|contrasena\w*|password\w*|token\w*|pagos?|payments?|cripto\w*|crypto\w*"
    r"|seguridad|security|permis\w*|secret\w*|credencial\w*|credentials?)\b"
)

#: Where a clause opens: the text's start, a punctuation mark, or a joining word.
#: A clause may open with where the change goes before its verb: "In src/app.py, add …", "En el
#: módulo de pagos agrega …" (a path or a name, with a few words of its own).
_WHERE = r"(?:(?:in|en|inside|dentro de|under|bajo)\s+(?:\S+\s+){1,4}?)?"
_OPENS = (r"(?:^|[.;:\n!?(]\s*|,\s*|\b(?:y|e|and|then|luego|despues|tambien|also|please|por favor)\s+)"
          + _WHERE)
#: Verbs that join work kept apart: a merge, a cherry-pick, a rebase, conflicts resolved.
_INTEGRATE = (r"merge|mergea|mergear|mergealo|mergeala|mergealas|mergealos|haz (?:el |un )?merge|fusiona|fusionar"
              r"|integra|integrar|integrate|cherry-pick\w*|rebase|rebasea|rebasear"
              r"|(?:resuelve|resolver|resolve)\s+(?:\w+\s+){0,2}conflict\w*")

#: (pattern, kind, reason). Order matters only where two could fire; each one is
#: written to be right on its own.
_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"(traceback|stack ?trace|^\s*\w*error:|\berror: |exit code \d|codigo de salida \d"
                   r"|\bline \d+|\blinea \d+)", re.M),
        "debug-repro",
        "carries an error, a trace or a line number",
    ),
    (
        re.compile(r"\b(intermitente\w*|flaky|race condition|aleatoriamente|a veces falla|sometimes fails"
                   r"|intermittent\w*|sporadic\w*|no se (por que|la causa)|no reproduce\w*)\b"),
        "debug-unclear",
        "a failure without a clear cause",
    ),
    (
        # Joining work is writing work, whatever it joins: "integra la rama … de la auditoría".
        re.compile(_OPENS + r"(" + _INTEGRATE + r")\b"),
        "implement",
        "asks to merge, cherry-pick or resolve conflicts",
    ),
    (
        re.compile(r"\b(code review|haz (un )?review|review (the|this|my) (pr|diff|change)"
                   r"|revisa (el|la|los|este|esta|mi) (pr|diff|codigo|cambios?|pull request))\b"),
        "review",
        "asks for a review",
    ),
    (
        # An audit reports what it finds: "audita el módulo", "busca bugs en calc.py".
        re.compile(r"\b(audit\w*|audita\w*|auditoria\w*|find (the )?(bugs|issues|problems|vulnerabilit\w*)"
                   r"|look for (bugs|issues|problems|vulnerabilit\w*)|busca (bugs|errores|problemas|fallos"
                   r"|vulnerabilidades)|que (bugs|problemas|errores) tiene)\b"),
        "review",
        "asks for an audit",
    ),
    (
        # A request that opens by asking for a plan ("Plan how to migrate…", "Planifica la migración"):
        # it fell to the default build, and a workflow's own plan step read as a change ("Plan, without
        # changing any file: Add…"). One that asks for it in a file is still widened (`asks_for_change`).
        re.compile(r"^\s*(plan|planifica\w*|planea\w*|make a plan|haz un plan|write a plan|draft a plan)\b"
                   r"(?!\s+(file|archivo)s?\b)"),
        "plan",
        "opens by asking for a plan",
    ),
    (
        re.compile(r"\b(arquitectura|architecture|plan tecnico|technical plan|disena (el|un) sistema"
                   r"|design (the|a) system|roadmap|propuesta tecnica|trade-?offs?)\b"),
        "plan",
        "asks for a plan or an architecture",
    ),
    (
        re.compile(r"\b(donde (esta|se define|se usa)|where (is|are|do we)|find where|busca (donde|el|la|los)"
                   r"|encuentra (donde|el|la)|lista (los|las) archivos)\b"),
        "explore",
        "asks where something is",
    ),
)

_DOCS_ONLY = re.compile(r"\b(commit\w*|push|changelog|readme|release notes?|docstrings?)\b")
_QUESTION = re.compile(
    r"^\s*[¿¡]?\s*(que|como|por que|cual|cuando|what|how|why|which|when|explica\w*|explain|is it|es)\b"
)
_TEST = re.compile(r"\b(escribe|agrega|anade|add|write|corre|run|arregla|fix)\s+(\w+\s+){0,2}(tests?|pruebas?)\b")
_TAG = re.compile(r"#([a-z][a-z-]+)\b")

#: A verb of change opening a clause: "Crear 5 módulos", "... y registrar la ruta".
#: Not anywhere in the text: "busca dónde se crea el usuario" is still a search.
_CHANGE = re.compile(
    _OPENS + r"(" + _INTEGRATE
    + r"|crea|crear|creen|creame|create|implementa|implementar|implement|agrega|agregar|anade|anadir|add"
    r"|escribe|escribir|write|registra|registrar|register|modifica|modificar|modify|edita|editar|edit"
    r"|arregla|arreglar|fix|refactoriza|refactorizar|refactor|elimina|eliminar|borra|borrar|delete|remove"
    r"|actualiza|actualizar|update|genera|generar|generate|migra|migrar|migrate|renombra|renombrar|rename"
    r"|construye|construir|build)\b"
)
#: Kinds whose worker cannot write. `plan` is not here: "crea un plan" is a plan.
_READING = frozenset({"chat", "explore", "review-routine", "review-critical"})


def by_rules(text: str) -> Classification | None:
    folded = fold(text)
    for tag in _TAG.findall(folded):
        if tag == "fable":
            return Classification("frontier", "high", "rule", "tagged #fable")
        if tag in KINDS:
            return Classification(tag, "medium", "rule", f"tagged #{tag}")
    for pattern, kind, reason in _RULES:
        if pattern.search(folded):
            if kind == "review":
                critical = _CRITICAL.search(folded)
                kind = "review-critical" if critical else "review-routine"
            return Classification(kind, "medium", "rule", reason)
    tokens = words(text)
    if _DOCS_ONLY.search(folded) and len(tokens) <= 12:
        return Classification("docs", "low", "rule", "a short commit, push or docs request")
    if (found := _TEST.search(folded)) and _tests_lead(folded, found.start()):
        return Classification("test", "medium", "rule", "asks for tests")
    if _QUESTION.search(folded) and folded.rstrip().endswith("?") and len(tokens) <= 25:
        return Classification("chat", "low", "rule", "a short question")
    return None


def _tests_lead(folded: str, at: int) -> bool:
    """Tests are the deliverable only when the request's first change is about them ("write
    tests for X", "fix the failing tests"): a fix or a feature that asks for its own tests
    ("Fix the chart labels … add a test for each") is the fix, its tests how it proves itself."""
    first = _CHANGE.search(folded)
    return first is None or first.start(1) >= at


# --- the model -------------------------------------------------------------

_KIND_HELP: Mapping[str, str] = {
    "chat": "a question, an explanation or a summary; nothing to change",
    "explore": "find files, code or facts in a repository; nothing to change",
    "docs": "README, changelog, docstrings, a commit message",
    "test": "write, fix or run tests",
    "refactor": "restructure code without new behaviour",
    "implement": "carry out a change whose shape is already clear",
    "ui": "user interface, layout, styling",
    "feature": "a new capability that crosses several parts of the code",
    "debug-repro": "a failure with a clear error or reproducible steps",
    "debug-unclear": "a failure whose cause is unknown, intermittent or hard to reproduce",
    "review-routine": "review or audit ordinary code and report what is wrong with it",
    "review-critical": "review or audit code touching auth, money, crypto or security",
    "plan": "design an architecture, a migration or a technical plan",
}

SYSTEM_PROMPT = (
    "You classify a software task by the kind of work it asks for. The task may be in any "
    "language. Read what is being asked, not the words it uses: 'the header no longer looks "
    "right' is UI work, not debugging. Kinds:\n"
    + "\n".join(f"- {k}: {v}" for k, v in _KIND_HELP.items())
    + "\nYou are given the request only to classify it: you do not carry it out, so whether you could "
    "find its files is not the question. An audit or a hunt for bugs is a review, not a search.\n"
    "Complexity is how hard the work is, not how long the text is: trivial, low, medium "
    "or high. Confidence is how sure you are of the kind, from 0 to 1."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(_KIND_HELP)},
        "complexity": {"type": "string", "enum": list(COMPLEXITIES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["kind", "complexity", "confidence", "reason"],
    "additionalProperties": False,
}

#: Where a kind a team does not cover goes: the nearest kind of the same nature
#: (one that writes, or one that reads), then the nearest of the other.
_WRITE_ORDER = ("implement", "feature", "refactor", "debug-repro", "debug-unclear", "test", "ui", "docs",
                "frontier")
_READ_ORDER = ("explore", "chat", "review-routine", "review-critical", "plan")


def _prompt_for(kinds: Sequence[str] | None) -> tuple[str, Mapping[str, object]]:
    """The system prompt and schema, offering only `kinds` when a team limits them."""
    if not kinds:
        return SYSTEM_PROMPT, SCHEMA
    offered = [k for k in _KIND_HELP if k in kinds]
    if not offered:
        return SYSTEM_PROMPT, SCHEMA
    prompt = SYSTEM_PROMPT.replace(
        "\n".join(f"- {k}: {v}" for k, v in _KIND_HELP.items()),
        "\n".join(f"- {k}: {_KIND_HELP[k]}" for k in offered))
    schema = json.loads(json.dumps(SCHEMA))
    schema["properties"]["kind"]["enum"] = offered
    return prompt, schema


def narrow(found: Classification, kinds: Sequence[str] | None) -> Classification:
    """A kind the project's team does not cover, read as the nearest one it does."""
    if not kinds or found.kind in kinds:
        return found
    reading = found.kind in _READ_ORDER
    order = (*_READ_ORDER, *_WRITE_ORDER) if reading else (*_WRITE_ORDER, *_READ_ORDER)
    kind = next((k for k in order if k in kinds), kinds[0])
    return Classification(kind, found.complexity, found.source,
                          f"read as {found.kind}, which this project's team has no role for: {kind}"
                          + (f" ({found.reason})" if found.reason else ""), found.cost_usd)


def ask_haiku(
    system_prompt: str,
    schema: Mapping[str, object],
    text: str,
    *,
    runner: Runner = subprocess.run,
    claude_bin: str = "claude",
    cwd: str | Path | None = None,
) -> tuple[dict | None, float, str]:
    """One question to Haiku with a JSON schema and no tools: (answer, cost, why there is none).
    Never raises; a failure is (None, cost so far, the reason)."""
    argv = [
        claude_bin, "-p",
        "--model", models.resolve("haiku"),
        # A short JSON answer to a narrow question: thinking longer buys nothing.
        "--effort", "low",
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format", "json",
        "--system-prompt", system_prompt,
        "--json-schema", json.dumps(schema),
    ]
    try:
        if cwd:
            # A neutral directory, so no project's instructions ride along; it may
            # not exist yet on a machine that has never stored anything.
            Path(cwd).mkdir(parents=True, exist_ok=True)
        proc = runner(
            argv,
            input=text,
            capture_output=True,
            text=True,
            timeout=CLASSIFIER_TIMEOUT_S,
            cwd=str(cwd) if cwd else None,
            env=_quiet_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, 0.0, f"the model did not run: {exc}"
    try:
        reply = json.loads(proc.stdout or "")
    except ValueError:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        return None, 0.0, f"the model returned no JSON: {detail}"
    if not isinstance(reply, dict):
        return None, 0.0, "the model returned no JSON object"
    cost = reply.get("total_cost_usd")
    cost = float(cost) if isinstance(cost, (int, float)) else 0.0
    out = reply.get("structured_output")
    if reply.get("is_error") or not isinstance(out, dict):
        return None, cost, "the model gave no decision"
    return out, cost, ""


def question(text: str) -> str:
    """The request as data to classify. Bare, Haiku reads it as its own task and
    answers with what it would do first ("looking for calc.py"), which is always
    a search."""
    return f"Classify this request. Do not carry it out.\n\n<request>\n{text}\n</request>"


def by_model(text: str, *, kinds: Sequence[str] | None = None, **haiku_kwargs) -> Classification:
    """Haiku's reading of the request. Never raises: a failure is the default kind."""
    prompt, schema = _prompt_for(kinds)
    out, cost, failure = ask_haiku(prompt, schema, question(text), **haiku_kwargs)
    if out is None:
        return Classification(DEFAULT_KIND, reason=failure.replace("the model", "the classifier"), cost_usd=cost)
    kind, complexity = out.get("kind"), out.get("complexity")
    confidence = out.get("confidence")
    if kind not in _KIND_HELP or complexity not in COMPLEXITIES:
        return Classification(DEFAULT_KIND, reason="the classifier answered outside the schema", cost_usd=cost)
    if not isinstance(confidence, (int, float)) or confidence < MIN_CONFIDENCE:
        return Classification(
            DEFAULT_KIND,
            complexity=str(complexity),
            reason=f"the classifier was unsure ({confidence}); it guessed {kind}",
            cost_usd=cost,
        )
    return Classification(
        str(kind), str(complexity), "model", clip(str(out.get("reason") or ""), 200), cost
    )


def classify(text: str, *, use_model: bool = True, kinds: Sequence[str] | None = None,
             **model_kwargs) -> Classification:
    """The kind of work a request is. With `kinds` (the ones a project's team
    covers), only one of those."""
    ruled = by_rules(text)
    if ruled is not None:
        return narrow(asks_for_change(text, ruled), kinds)
    if not use_model:
        return narrow(Classification(DEFAULT_KIND, reason="no rule matched and the model was not asked"), kinds)
    return narrow(asks_for_change(text, by_model(text, kinds=kinds, **model_kwargs)), kinds)


def opens_with_change(text: str) -> bool:
    """Whether a request opens with a verb of change ("Fix the export…"): an order, whatever
    questions follow it."""
    return _CHANGE.match(fold(text).strip()) is not None


#: A file a request asks to be written: a verb of change, then within a few words a document's name.
_WRITES_FILE = re.compile(
    _OPENS + r"(escribe|escribir|write|crea|crear|create|redacta|redactar|draft|genera|generar|generate|deja|dejalo"
    r"|guarda|guardar|save|put|pon|ponlo|agrega|add|actualiza|update)\b[^.\n]{0,80}?"
    r"(\b[\w./-]+\.(md|mdx|rst|txt|adoc)\b|\b(in|into|en|a) (a |un )?(markdown|archivo|file)\b)")


def asks_for_change(text: str, found: Classification) -> Classification:
    """A reading kind for a request that asks for a change becomes `implement`, and so does a
    plan the request asks to be written to a file ("write the plan in docs/PLAN.md"): a plan's
    worker writes nothing, and a second task would have to. A tag is a person's word and
    stays; so does a question."""
    folded = fold(text).strip()
    if found.kind == "plan" and not found.reason.startswith("tagged") and (into := _WRITES_FILE.search(folded)):
        return Classification("implement", found.complexity, found.source,
                              f"read as plan, but it asks for it written to a file ({into.group(2)})",
                              found.cost_usd)
    if found.kind not in _READING or found.reason.startswith("tagged") or folded.endswith("?"):
        return found
    verb = _CHANGE.search(folded)
    if verb is None:
        return found
    return Classification("implement", found.complexity, found.source,
                          f"read as {found.kind}, but it asks for a change ({verb.group(1)})", found.cost_usd)


def _quiet_env() -> dict[str, str]:
    env = dict(os.environ)
    # A worker or a classifier call must not record itself as a task.
    env["LOCKS_HOOKS_OFF"] = "1"
    return dict(legacy.mirror(env))
