"""What a project knows, by topic: notes, the links between them, and the code they are about.

The memory of problems and fixes is global on purpose: a dead end in one
repository is a dead end in the next. What a project *is* is not: its business
rules, why its code is shaped the way it is, how it is run. That knowledge is
kept per project, in notes, so a session that compacted or a worker that just
started reads it back instead of rebuilding it from the code.

A library, not a heap:

- **Topics.** Every note is filed under one topic of a closed set (`TOPICS`,
  plus the ones a person adds to a project). Haiku files a note under a topic
  that exists; when none fits it may propose one, and a person decides.
- **Links.** Notes point at each other with a kind (`LINKS`): a business rule
  `depends_on` another, a decision `explains` a piece of code, a new fact
  `replaces` an old one. Recall follows them one step.
- **Anchors.** A note can be about a file or a symbol, as livespec ties a spec
  to code. An anchor remembers the file's content when the note was written; a
  later commit that changes it marks the note `review`. A note nobody checks
  again does not get to lie quietly.

Notes are written at three moments, never whenever a model feels like it: a
worker that passed lists what it `learned` in its result block; before a
session compacts or ends, Haiku reads what was said since the last time; and a
person keeps one with `locks note`. They are read back by topic, by the code a
task touches and by full-text search inside the project, never another one.

What a worker learned is a claim one pass made, not yet a fact: it starts as a
`candidate`, read back marked unproven, and the tasks whose brief carried it
decide (`weigh`, run when work settles, never in a GET): current after
`PROVE_AFTER` of them passed their first try, dropped after `DISPROVE_AFTER`
failed theirs with none passing. A person's note, and one Haiku filed from a
conversation, is current as it is written.

Haiku files, and fails closed: when it cannot run, a fact is still kept, under
the topic its words point to (`filed_by: rule`), with no links.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from locks import arms as arms_mod
from locks import repo, workspaces
from locks.store import Store, home
from locks.text import fold, reads_alike, title_of, words

#: The topics every project has. A person adds others per project.
TOPICS: dict[str, str] = {
    "business": "the domain: business rules, what users and the product need, the words the domain uses",
    "code": "how the code works: what a module or function does, its data flow, its traps",
    "decisions": "how the project evolved: why it is built this way, what was tried and dropped, migrations",
    "conventions": "how things are done here: naming, structure, style, review and commit habits",
    "environment": "how it is run, built, tested and deployed: commands, services, versions, configuration",
}
#: What a person may type for a topic, folded.
ALIASES = {
    "negocio": "business", "dominio": "business", "domain": "business",
    "codigo": "code", "logica": "code", "arquitectura": "code", "architecture": "code", "logic": "code",
    "decisiones": "decisions", "decision": "decisions", "evolucion": "decisions", "historia": "decisions",
    "history": "decisions", "evolution": "decisions",
    "convenciones": "conventions", "convencion": "conventions", "estilo": "conventions", "style": "conventions",
    "entorno": "environment", "env": "environment", "infra": "environment", "operacion": "environment",
    "ops": "environment",
}
LINKS = ("depends_on", "explains", "replaces", "contradicts", "example_of")
STATES = ("current", "candidate", "review", "replaced", "dropped")
#: The states recall reads: a note to review, or a candidate, is shown marked as such.
LIVE = ("current", "candidate", "review")
#: What a passing worker learned starts here: read back, marked unproven, until the tasks
#: whose brief carried it say whether it holds (`weigh`).
CANDIDATE = "candidate"
#: A note that says what a change must be (a policy a check enforces, a rule a person keeps),
#: briefed apart and as a requirement: a weak model reads a fact framed as data as optional.
#: It grants nothing: it bounds what the work is, never what a worker may do.
CONSTRAINT = "constraint"
#: What one brief carries of a project's constraints; the rest are said by count.
CONSTRAINT_CHARS = 3000
#: A candidate becomes current once this many later tasks whose brief carried it passed their
#: first try; it is dropped once this many failed theirs with none passing.
PROVE_AFTER = 2
DISPROVE_AFTER = 3
#: The `briefed` event's field that lists the notes a brief carried.
BRIEFED = "notes"
#: The `briefed` event's field that lists the candidate notes a brief left out by their trial.
WITHHELD = "notes_withheld"
#: A worker's candidate note is weighed against its absence (the `note_trials` setting): the tasks
#: it was recalled for alternate between carrying it and going without it, until each arm has this
#: many first tries; it becomes current only when the tasks that carried it passed no less often.
NOTE_TRIAL = 3
#: How the reason of a candidate made current begins (`doctor` reads it).
PROVEN = "proven: "
AUTHORS = ("person", "worker", "session")

#: Words that point a text at a topic: cheap routing for the hooks, and the
#: topic a fact gets when Haiku cannot file it. Matched on folded text.
_TOPIC_WORDS: dict[str, re.Pattern[str]] = {
    "business": re.compile(r"\b(negocio|cliente\w*|usuari[oa]s?|customers?|users?|business|regla de negocio|"
                           r"precio\w*|prices?|pricing|factura\w*|invoic\w*|pago\w*|payments?|suscripci\w*|subscription\w*|"
                           r"dominio|domain|producto|product)\b"),
    "decisions": re.compile(r"\b(por que|why|decidi\w*|decid\w*|decision\w*|evolu\w*|histori\w*|history|migra\w*|"
                            r"legacy|deprecat\w*|descart\w*|dropped|antes se|used to)\b"),
    "conventions": re.compile(r"\b(convenci\w*|conventions?|naming|nombra\w*|estilo|style|lint\w*|formato|format\w*|"
                              r"commit messages?|estructura de carpetas|folder structure)\b"),
    "environment": re.compile(r"\b(deploy\w*|despleg\w*|docker\w*|entorno|environment|build|compil\w*|ci|pipeline|"
                              r"servidor\w*|servers?|puerto|port|nvm|node version|version de node|instal\w*|install\w*|"
                              r"variables? de entorno|env vars?|\.env)\b"),
    "code": re.compile(r"\b(funci[oó]n\w*|functions?|m[oó]dul\w*|modules?|clase\w*|class\w*|endpoint\w*|component\w*|"
                       r"servicio\w*|service\w*|api|query|consulta\w*|flujo|flow|logica|logic)\b"),
}

#: A note shorter than this says nothing.
MIN_FACT = 12
MAX_FACT = 1000
MAX_FACTS = 8
#: Text in a fact that looks like a secret keeps the fact out: notes are put
#: in front of models and shown in the UI.
#: API keys are written with dashes inside (`sk-ant-api03-…`, `sk-proj-…`): a pattern of letters and
#: digits alone let every Anthropic and OpenAI key through (found by `tests/test_fuzz.py`).
#: A key starts a word, or holds a digit when glued to one: inside a word (`<task-notification>`,
#: `desk-reference`) `sk-` is no key.
_SECRET = re.compile(r"((?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}|(?<![A-Za-z0-9])sk-[A-Za-z0-9]{8,}|"
                     r"sk-(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_\w{8,}|"
                     r"xox[abposr]-[\w-]{8,}|AKIA[0-9A-Z]{12,}|AIza[0-9A-Za-z_-]{30,}|"
                     r"-----BEGIN [A-Z ]*PRIVATE KEY|\b[0-9a-f]{40,}\b|password\s*[:=]\s*\S+|"
                     r"bearer\s+[A-Za-z0-9._~+/-]{20,})",
                     re.IGNORECASE)

_PRIVATE = re.compile(r"<private>.*?</private>", re.IGNORECASE | re.DOTALL)

Ask = Callable[..., tuple[dict | None, float, str]]


# --- topics -----------------------------------------------------------------


def topics(store: Store, project: str) -> dict[str, str]:
    """Every topic a project's notes may be filed under, with what it holds."""
    return {**TOPICS, **{t["name"]: t["description"] for t in store.note_topics(project)}}


def resolve_topic(store: Store, project: str, name: str) -> str:
    """The topic a person or a model named, or ValueError."""
    folded = fold(name).strip().replace(" ", "-")
    known = topics(store, project)
    found = folded if folded in known else ALIASES.get(folded)
    if found is None:
        raise ValueError(f"unknown topic {name!r}; known: {', '.join(known)} "
                         f"(add one with `locks notes topic add <name> \"<what it holds>\"`)")
    return found


def topics_in(text: str) -> list[str]:
    """The topics a text's words point to, most specific first. Empty when none do."""
    folded = fold(text)
    return [t for t, pattern in _TOPIC_WORDS.items() if pattern.search(folded)]


def add_topic(store: Store, project: str, name: str, description: str) -> str:
    folded = fold(name).strip().replace(" ", "-")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", folded):
        raise ValueError("a topic is one short word: letters, digits and dashes")
    if not description.strip():
        raise ValueError("say what the topic holds: it is what Haiku files by")
    store.add_note_topic(project, folded, description.strip())
    return folded


# --- writing ------------------------------------------------------------------


#: Words that make a fact or a finding a security problem: a credential, a secret or a key
#: that is exposed, or a hole. A design `token` or a usage `token` count is none: a token
#: is one only beside a word that says it is exposed or rotated.
_SECURITY = re.compile(
    r"\b(credentials?|secrets?|private[ _-]?keys?|api[ _-]?keys?|access[ _-]?keys?|passwords?|"
    r"vulnerab\w*|security (?:issue|problem|hole|flaw|risk|bug)\w*|injection|xss|csrf|ssrf|rce|"
    r"(?:auth|access|bearer|api|npm|github|session)[ _-]?tokens?)\b"
    r"|\btokens?\b(?=.{0,60}\b(?:exposed|leak\w*|committed|rotat\w*|plain ?text|public)\b)"
    r"|\b(?:exposed|leak\w*|committed|rotat\w*)\b.{0,60}\btokens?\b",
    re.IGNORECASE)
#: The most security alerts an ending carries, and how long each may be.
MAX_ALERTS, ALERT_CHARS = 5, 300


def security_alerts(learned: Iterable[str], findings: Iterable[Mapping[str, Any]] = ()) -> list[str]:
    """What a passing worker said about a security problem (an exposed credential, a
    secret committed, a hole it found already there), one line each, so an ending
    carries it as an alert instead of a note among others. Anything that looks like the
    secret itself is masked (`_SECRET`), and `<private>` text left out."""
    texts = [str(t) for t in learned]
    for f in findings:
        what = " ".join(str(f.get("what") or "").split())
        where = str(f.get("where") or "").strip()
        if what:
            texts.append(f"{where}: {what}" if where else what)
    out: list[str] = []
    for text in texts:
        line = " ".join(_PRIVATE.sub(" ", text).split())
        if not line or not _SECURITY.search(line):
            continue
        line = _SECRET.sub("[secret]", line)[:ALERT_CHARS]
        if line not in out:
            out.append(line)
    return out[:MAX_ALERTS]


def clean_fact(text: str) -> str | None:
    """A fact as it is kept, or None when it is not worth keeping or must not be."""
    # What a person marked private never becomes a note.
    fact = " ".join(_PRIVATE.sub(" ", str(text or "")).split())
    if len(fact) < MIN_FACT or _SECRET.search(fact):
        return None
    return fact[:MAX_FACT]


def add(
    store: Store,
    project: str,
    text: str,
    *,
    topic: str,
    author: str,
    filed_by: str,
    title: str | None = None,
    source: str = "",
    task_id: int | None = None,
    session_id: str | None = None,
    links: Iterable[tuple[int, str]] = (),
    paths: Iterable[str] = (),
    symbols: Iterable[tuple[str, str]] = (),
    repo_dir: Path | None = None,
    commit: str | None = None,
    proposed_topic: str | None = None,
    state: str = "current",
    kind: str | None = None,
) -> tuple[int, bool]:
    """Keep a note: (its id, whether it is new). A note that says what a live one
    of the same kind in the same topic already says is that one, with the new anchors
    and links. A worker's starts as a `candidate` (`file_facts`)."""
    fact = clean_fact(text)
    if fact is None:
        raise ValueError("a note needs a fact of a few words, and never a secret")
    existing = same_as(store, project, topic, fact, kind=kind)
    if existing is None:
        note_id = store.add_note(project, topic, (title or "").strip()[:120] or title_of(fact, 80), fact,
                                 author=author, filed_by=filed_by, source=source, task_id=task_id,
                                 session_id=session_id, proposed_topic=proposed_topic, state=state, kind=kind)
    else:
        note_id = existing
    for to_id, kind in links:
        link(store, note_id, to_id, kind)
    if repo_dir is not None and (paths or symbols):
        anchor(store, note_id, repo_dir, paths, symbols, commit=commit)
    return note_id, existing is None


def same_as(store: Store, project: str, topic: str, fact: str, *, kind: str | None = None) -> int | None:
    near = store.search_notes(project, fact, limit=10)
    for note in store.notes(project, topics=[topic], states=LIVE, ids=near, kind=kind or "fact"):
        if reads_alike(fact, note["text"], 0.8):
            return int(note["id"])
    return None


def merge_same(store: Store, project: str) -> int:
    """Retire the copies of one fact under one topic (two repositories that joined a
    workspace each kept it): a current copy stays before one to review, the older
    before the newer, and takes the others' anchors and links. How many went."""
    kept: dict[tuple[str, str], int] = {}
    gone = 0
    order = sorted(store.notes(project, states=LIVE, limit=100_000), key=lambda n: (n["state"] != "current", n["id"]))
    for note in order:
        same = (note["topic"], fold(" ".join(note["text"].split())))
        if same not in kept:
            kept[same] = note["id"]
            continue
        store.merge_note(note["id"], kept[same],
                         f"the same fact as note #{kept[same]}, kept by another repository of the workspace")
        gone += 1
    return gone


def link(store: Store, from_id: int, to_id: int, kind: str) -> bool:
    """Link two notes of one project. `replaces` retires the note it replaces."""
    if kind not in LINKS:
        raise ValueError(f"unknown link {kind!r}; known: {', '.join(LINKS)}")
    a, b = store.get_note(from_id), store.get_note(to_id)
    if a is None or b is None or a["project"] != b["project"]:
        raise ValueError("both notes must exist, in the same project")
    made = store.link_notes(from_id, to_id, kind)
    if kind == "replaces" and b["state"] in LIVE:
        store.update_note(to_id, state="replaced", replaced_by=from_id, state_reason=f"replaced by #{from_id}")
    return made


# --- anchors and review -----------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30, check=False)


def _blobs(top: Path, commit: str, paths: Sequence[str]) -> dict[str, str]:
    """path -> blob at `commit`, for the paths that exist there."""
    if not paths:
        return {}
    out = _git(top, "ls-tree", "-r", commit, "--", *paths)
    found = {}
    for line in out.stdout.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3:
            found[path] = parts[2]
    return found


def _where(repo_dir: Path | None) -> tuple[bool, list[tuple[str, Path]]]:
    """Where note paths given from `repo_dir` live. In a checkout (or a worktree of
    one) they are its own paths, kept with its name in a workspace in front; in a
    workspace's folder they already carry the member's name, one checkout each."""
    top = repo.toplevel(repo_dir) if repo_dir is not None else None
    if top is not None:
        return True, [(workspaces.prefix(top), top)]
    return False, workspaces.checkouts_of(repo_dir)


def _placed(inside: bool, places: Sequence[tuple[str, Path]], path: str) -> tuple[str, Path | None, str]:
    """A path as the notes keep it, the checkout it is in, and its path there."""
    if inside:
        pre, top = places[0]
        return pre + path, top, path
    for pre, top in places:
        if path.startswith(pre):
            return path, top, path[len(pre):]
    return path, None, path


def anchor(store: Store, note_id: int, repo_dir: Path, paths: Iterable[str] = (),
           symbols: Iterable[tuple[str, str]] = (), *, commit: str | None = None) -> int:
    """Tie a note to files (relative to the checkout's top, or to a workspace's
    folder) and to livespec symbols, as (qualified name, path). Each anchor keeps
    the file's content at `commit` (default HEAD), which is what `review` compares
    against. Outside a checkout the anchor still names the file, for recall, and
    is never reviewed. In a workspace a member's file is named with the member in
    front (`api/…`): two members' files of the same name are two files."""
    wanted = [(p.strip().lstrip("./"), None) for p in paths if p and p.strip()]
    wanted += [(path, symbol) for symbol, path in symbols if path]
    if not wanted:
        return 0
    inside, places = _where(repo_dir)
    rows = [(*_placed(inside, places, path), symbol) for path, symbol in dict.fromkeys(wanted)]
    seen: dict[Path, tuple[str | None, dict[str, str]]] = {}
    for _, top, _, _ in rows:
        if top is not None and top not in seen:
            sha = (commit if inside else None) or _git(top, "rev-parse", "HEAD").stdout.strip() or None
            files = sorted({file for _, t, file, _ in rows if t == top})
            seen[top] = (sha, _blobs(top, sha, files) if sha else {})
    for kept, top, file, symbol in rows:
        sha, blobs = seen.get(top, (None, {})) if top is not None else (None, {})
        store.add_anchor(note_id, kept, symbol=symbol, commit_sha=sha if file in blobs else None,
                         blob=blobs.get(file))
    return len(rows)


def review(store: Store, project: str, repo_dir: Path) -> list[tuple[int, str]]:
    """Mark `review` every current note whose anchored file changed since the note
    was written. A change only counts once it is in this checkout's history: an
    anchor on a branch not merged yet is left alone, and one whose content
    reached HEAD another way (a squash merge) moves to HEAD. In a workspace a
    member reviews only its own anchors, and its folder reviews each member's
    in that member. Returns (note, why)."""
    _, places = _where(repo_dir)
    every = [a for a in store.project_anchors(project) if a["commit_sha"] and a["blob"]] if places else []
    flagged: dict[int, str] = {}
    for pre, top in places:
        _review_in(store, top, [{**a, "file": a["path"][len(pre):]} for a in every if a["path"].startswith(pre)],
                   flagged)
    for note_id, why in flagged.items():
        store.update_note(note_id, state="review", state_reason=why)
    return list(flagged.items())


def _head(top: Path) -> str:
    """The checkout's commit, "" with none yet: `git rev-parse HEAD` then prints `HEAD` itself and fails."""
    done = _git(top, "rev-parse", "--verify", "--quiet", "HEAD")
    return done.stdout.strip() if done.returncode == 0 else ""


def _review_in(store: Store, top: Path, anchors: Sequence[Mapping[str, Any]], flagged: dict[int, str]) -> None:
    if not anchors:
        return
    head = _head(top)
    if not head:
        return
    now = _blobs(top, head, sorted({a["file"] for a in anchors}))
    ancestors: dict[str, bool] = {}
    for a in anchors:
        current = now.get(a["file"])
        if current == a["blob"]:
            if a["commit_sha"] != head:
                store.update_anchor(a["id"], commit_sha=head, blob=current)
            continue
        sha = a["commit_sha"]
        if sha not in ancestors:
            ancestors[sha] = _git(top, "merge-base", "--is-ancestor", sha, head).returncode == 0
        if not ancestors[sha]:
            continue
        what = f"`{a['symbol']}` in {a['path']}" if a["symbol"] else a["path"]
        flagged.setdefault(a["note_id"], f"{what} {'changed' if current else 'was deleted'} "
                                         f"since this note was written ({sha[:7]} → {head[:7]})")


def confirm(store: Store, note_id: int, repo_dir: Path | None) -> dict | None:
    """A person checked a note: it is current again, anchored to the code as it is now.
    An anchor no checkout here holds is left as it was."""
    note = store.get_note(note_id)
    if note is None:
        return None
    if repo_dir is not None:
        _, places = _where(repo_dir)
        heads: dict[Path, str] = {}
        for a in store.note_anchors([note_id]):
            pre, top = next(((p, t) for p, t in places if a["path"].startswith(p)), ("", None))
            if top is None:
                continue
            head = heads.setdefault(top, _head(top))
            file = a["path"][len(pre):]
            blob = _blobs(top, head, [file]).get(file) if head else None
            store.update_anchor(a["id"], commit_sha=head if blob else None, blob=blob)
    return store.update_note(note_id, state="current", state_reason=None)


def carried(store: Store) -> dict[int, set[int]]:
    """Per task, the notes its first brief carried (the `briefed` event's `BRIEFED`); a task
    whose first brief kept no such field is left out."""
    out: dict[int, set[int]] = {}
    seen: set[int] = set()
    for event in store.events_of_kinds(["briefed"]):
        task_id = int(event["task_id"])
        if task_id in seen:
            continue
        seen.add(task_id)
        ids = event["data"].get(BRIEFED)
        if isinstance(ids, list):
            out[task_id] = {int(i) for i in ids if isinstance(i, int)}
    return out


def withheld(store: Store) -> dict[int, set[int]]:
    """Per task, the candidate notes its first brief left out by their trial (`WITHHELD`)."""
    out: dict[int, set[int]] = {}
    seen: set[int] = set()
    for event in store.events_of_kinds(["briefed"]):
        task_id = int(event["task_id"])
        if task_id in seen:
            continue
        seen.add(task_id)
        ids = event["data"].get(WITHHELD)
        if isinstance(ids, list):
            out[task_id] = {int(i) for i in ids if isinstance(i, int)}
    return out


def trials_on(env: Mapping[str, str] | None = None) -> bool:
    from locks import config

    return config.enabled("note_trials", os.environ if env is None else env)


def arms(store: Store, found: Sequence[Mapping[str, Any]]) -> tuple[list[dict], list[dict]]:
    """The notes a brief carries and the candidates it leaves out by their trial: each worker
    candidate (never a constraint, whose breaking is a finding) goes without in turn, the arm with
    fewer tasks next, until it was left out `NOTE_TRIAL` times. With `note_trials` off, all carried."""
    if not trials_on():
        return list(found), []
    trial = [n for n in found if n.get("state") == CANDIDATE and n.get("kind") != CONSTRAINT]
    if not trial:
        return list(found), []
    took, left = carried(store), withheld(store)
    out_ids = set()
    for note in trial:
        nid = int(note["id"])
        with_it = sum(1 for ids in took.values() if nid in ids)
        without = sum(1 for ids in left.values() if nid in ids)
        if without < NOTE_TRIAL and without < with_it:
            out_ids.add(nid)
    return ([n for n in found if int(n["id"]) not in out_ids], [n for n in found if int(n["id"]) in out_ids])


#: The `briefed` event's key for the order a brief's notes came in (`recall_arm`).
RECALL_KEY = "recall_arm"
RECALL_ARMS = ("fused", "ordered")
#: First tries per arm before the recall order's trial decides.
RECALL_TRIAL = 6


def recall_on(env: Mapping[str, str] | None = None) -> bool:
    from locks import config

    return config.enabled("recall_fusion", os.environ if env is None else env)


def recall_trial(store: Store, repo: str | None) -> dict[str, dict[str, Any]]:
    """Per arm of the recall order's trial: its tasks and first tries passed."""
    rows = [{"arm": r["arm"], "tries": 1, "passed": r["passed_first"], "duration_s": r.get("duration_s")}
            for r in store.arm_trial(repo, RECALL_KEY)]
    return arms_mod.tally(rows, RECALL_ARMS)


def recall_arm(store: Store, repo: str | None, task_id: int) -> tuple[str, str]:
    """Whether this brief's notes come by reciprocal rank fusion of what the code and the words
    found, or anchored first, and why: in turns until each arm has `RECALL_TRIAL` first tries, then
    fusion only where its arm passed no less often. Asked only when the two orders differ."""
    if repo is None:
        return "fused", "no repository to run a trial in"
    return arms_mod.pick(recall_trial(store, repo), RECALL_ARMS, task_id, RECALL_TRIAL, "recall order", repo,
                         pooled=recall_trial(store, None))


def anchored_to(store: Store, project: str, paths: Iterable[str], skip: Iterable[int] = ()) -> list[dict]:
    """The live notes anchored to these paths, but the ones in `skip`: what a later attempt's brief
    adds once the work changed files the first brief could not name."""
    skipped = {int(i) for i in skip}
    ids = [i for i in store.notes_at(project, paths) if i not in skipped]
    rows = [n for n in store.notes(project, states=LIVE, ids=ids) if n.get("kind") != CONSTRAINT]
    return expand(store, [int(n["id"]) for n in rows], {int(n["id"]): "about a file the work changed" for n in rows})


def weigh(store: Store) -> dict[str, list[int]]:
    """Settle the candidates by the tasks whose first brief carried them, other than the one
    that wrote them, once those ended: current after `PROVE_AFTER` passed their first try
    (`standards.samples`), dropped after `DISPROVE_AFTER` failed theirs with none passing.
    From `evolution.settle`, never from a GET. Which notes moved, by the state they took."""
    moved: dict[str, list[int]] = {"current": [], "dropped": []}
    candidates = [n for p in store.note_projects() for n in store.notes(p, states=[CANDIDATE], limit=100_000)]
    if not candidates:
        return moved
    from locks import standards

    briefs, left_out = carried(store), withheld(store)
    ended = {t.id: t for t in standards.ended(store)}
    briefed_at: dict[int, str] = {}
    for event in store.events_of_kinds(["briefed"]):
        briefed_at.setdefault(int(event["task_id"]), str(event["ts"]))

    def firsts(holding: Mapping[int, set[int]], note: Mapping[str, Any]) -> tuple[list[int], list[int]]:
        passed, failed = [], []
        # A note put back on trial (`retry_idle`) is weighed by the tasks briefed since, never the ones before.
        since = note["updated_at"] if str(note.get("state_reason") or "").startswith(RETRIED) else ""
        for task_id in sorted(t for t, ids in holding.items() if note["id"] in ids and t != note["task_id"]
                              and briefed_at.get(t, "") >= since):
            task = ended.get(task_id)
            first = standards.samples(task.attempts)[:1] if task is not None else []
            if first:
                (passed if first[0][1] else failed).append(task_id)
        return passed, failed

    trials = trials_on()
    with store.immediate():
        for note in candidates:
            passed, failed = firsts(briefs, note)
            if trials and note.get("kind") != CONSTRAINT:
                # Weighed against its absence: the tasks that carried it against the ones its trial
                # left without it. A note that never failed a reader still waits for its arm to fill.
                w_passed, w_failed = firsts(left_out, note)
                carried_n, without_n = len(passed) + len(failed), len(w_passed) + len(w_failed)
                if not passed and len(failed) >= DISPROVE_AFTER:
                    pass  # dropped below, as without a trial: every reader failed
                elif carried_n >= NOTE_TRIAL and without_n >= NOTE_TRIAL:
                    with_rate, without_rate = len(passed) / carried_n, len(w_passed) / without_n
                    said = (f"the {carried_n} tasks that carried it passed their first try {with_rate:.0%}, the "
                            f"{without_n} its trial left without it {without_rate:.0%}")
                    if with_rate >= without_rate:
                        store.update_note(note["id"], state="current", state_reason=PROVEN + said)
                        moved["current"].append(int(note["id"]))
                    else:
                        store.update_note(note["id"], state="dropped", state_reason="disproven: " + said)
                        moved["dropped"].append(int(note["id"]))
                    continue
                else:
                    continue
            if len(passed) >= PROVE_AFTER:
                store.update_note(note["id"], state="current", state_reason=PROVEN + "in the brief of "
                                  + ", ".join(f"#{t}" for t in passed) + ", which passed their first try")
                moved["current"].append(int(note["id"]))
            elif not passed and len(failed) >= DISPROVE_AFTER:
                store.update_note(note["id"], state="dropped", state_reason="unproven: in the brief of "
                                  + ", ".join(f"#{t}" for t in failed) + ", which failed their first try, and of "
                                  "none that passed")
                moved["dropped"].append(int(note["id"]))
    return moved


#: Ended tasks whose first brief carried a current note, none of whose workers named it
#: (`notes_used`), before it goes back on trial: a note every brief pays for and no worker
#: says it relied on is weighed again against its absence, never dropped on those words.
IDLE_AFTER = 8
#: The start of an idle note's reason: why it is a candidate again.
RETRIED = "back on trial: "


def retry_idle(store: Store) -> list[int]:
    """Current notes a worker or a session filed that the first briefs of `IDLE_AFTER` ended
    tasks or more carried since the note last changed, and that no worker of those tasks named
    as relied on: made candidates again, so the trial (`arms`, `weigh`) decides them by first
    tries with and without them. A person's note and a constraint are never put back; neither is
    one with the trial off. From `evolution.settle`, never a GET. The ids put back."""
    if not trials_on():
        return []
    current = [n for p in store.note_projects() for n in store.notes(p, states=["current"], limit=100_000)
               if n.get("author") != "person" and n.get("kind") != CONSTRAINT]
    if not current:
        return []
    from locks import standards

    ended = {t.id for t in standards.ended(store)}
    first: dict[int, tuple[str, set[int]]] = {}
    for event in store.events_of_kinds(["briefed"]):
        task_id = int(event["task_id"])
        ids = event["data"].get(BRIEFED)
        if task_id not in first and isinstance(ids, list):
            first[task_id] = (str(event["ts"]), {int(i) for i in ids if isinstance(i, int)})
    named: dict[int, set[int]] = {}
    for event in store.events_of_kinds(["attempt_finished"]):
        named.setdefault(int(event["task_id"]), set()).update(
            int(i) for i in event["data"].get("notes_used") or () if isinstance(i, int))
    back: list[int] = []
    with store.immediate():
        for note in current:
            readers = [t for t, (ts, ids) in first.items()
                       if note["id"] in ids and t in ended and t != note["task_id"] and ts >= note["updated_at"]]
            if len(readers) < IDLE_AFTER or any(note["id"] in named.get(t, ()) for t in readers):
                continue
            store.update_note(note["id"], state=CANDIDATE, state_reason=RETRIED + (
                f"carried by the first brief of {len(readers)} tasks since it last changed, and named as relied on "
                "by none of their workers"))
            back.append(int(note["id"]))
    return back


# --- reading ------------------------------------------------------------------


def recall(
    store: Store,
    project: str,
    query: str = "",
    *,
    in_topics: Sequence[str] | None = None,
    paths: Iterable[str] = (),
    limit: int = 8,
    hops: int = 1,
    states: Sequence[str] = LIVE,
    strict: bool = False,
    fuse: bool = False,
) -> list[dict]:
    """The notes a question or a task needs, each with why it was picked: those
    anchored to the code it touches, those that match it in its topic, and the
    notes those link to, one step away. With neither a query nor paths, the
    latest of the topic. `strict` keeps only notes sharing several words with
    the query, for a hook that must not put loose matches in front of a model. `fuse` orders the
    notes found by the code and by the words by reciprocal rank fusion (`fused`), so a note both
    find comes first and the words' best is not cut by every anchored one; else anchored first."""
    chosen: dict[int, str] = {}
    by_code: list[int] = []
    by_words: list[int] = []
    paths = list(dict.fromkeys(paths))
    anchored = store.notes_at(project, paths)
    for note in store.notes(project, states=states, ids=anchored):
        about = sorted({a["path"] for a in store.note_anchors([note["id"]]) if a["path"] in set(paths)})
        chosen.setdefault(int(note["id"]), "about " + ", ".join(about[:3]))
        by_code.append(int(note["id"]))
    if query.strip():
        ranked = store.search_notes(project, query, limit=50)
        rows = {n["id"]: n for n in store.notes(project, topics=in_topics, states=states, ids=ranked)}
        wanted = set(words(query))
        need = min(3, len(wanted))
        if strict:
            wanted = distinctive(store, project, wanted, states)
        for note_id in ranked:
            note = rows.get(note_id)
            if note is None:
                continue
            if strict and len(wanted & set(words(f"{note['title']} {note['text']}"))) < need:
                continue
            if note_id in chosen and note_id in by_code:
                chosen[note_id] += " and matches the question"
            chosen.setdefault(note_id, "matches the question")
            by_words.append(int(note_id))
    elif not anchored:
        for note in store.notes(project, topics=in_topics, states=states, limit=limit):
            chosen.setdefault(int(note["id"]), "latest in its topic")
    ordered = (fused([by_code, by_words], [i for i in chosen if i not in by_code and i not in by_words])
               if fuse else list(chosen))
    if len(ordered) > limit:
        # What the cut leaves out is weighed by what workers said they relied on here (issue #123):
        # the notes past the limit a worker named before come in ahead of ones none named.
        relied = store.notes_relied(project)
        head, tail = ordered[:limit], ordered[limit:]
        promoted = sorted((n for n in tail if relied.get(n)), key=lambda n: -relied[n])
        unnamed = sorted((n for n in head if not relied.get(n)), key=head.index, reverse=True)
        for incoming, outgoing in zip(promoted, unnamed, strict=False):
            if relied[incoming] >= RELIED_TO_PROMOTE:
                head[head.index(outgoing)] = incoming
        ordered = head
    picked = ordered[:limit]
    if hops > 0 and picked:
        extra = max(2, limit // 2)
        for edge in store.note_links(picked):
            if extra <= 0:
                break
            other = edge["to_id"] if edge["from_id"] in picked else edge["from_id"]
            kind = edge["kind"]
            if other in chosen:
                continue
            note = store.get_note(other)
            if note is None or note["state"] not in states:
                continue
            seen_from = edge["from_id"] if edge["to_id"] == other else edge["to_id"]
            chosen[other] = f"linked to #{seen_from} ({kind})"
            picked.append(other)
            extra -= 1
    return expand(store, picked, chosen)


#: A word this share of a project's notes or more hold is its common vocabulary, no evidence a note
#: is about a prompt (issue #127: notes on compaction recalled for a prompt about merging a stage).
COMMON_SHARE = 0.2
#: Fewer notes than this and every word counts: a share of a handful says nothing.
COMMON_MIN_NOTES = 5


def distinctive(store: Store, project: str, wanted: set[str], states: Sequence[str] = LIVE) -> set[str]:
    """The words of a query fewer than `COMMON_SHARE` of the project's notes hold."""
    held = [set(words(f"{n['title']} {n['text']}")) for n in store.notes(project, states=states)]
    if len(held) < COMMON_MIN_NOTES:
        return wanted
    return {w for w in wanted if sum(w in h for h in held) < COMMON_SHARE * len(held)}


#: Times workers here named a note as relied on before it takes a recalled place from one never named.
RELIED_TO_PROMOTE = 2

#: Reciprocal rank fusion's constant (Cormack et al., 2009): a list's first places count, its tail barely.
RRF_K = 60


def fused(lists: Sequence[Sequence[int]], rest: Sequence[int] = ()) -> list[int]:
    """The ids of several ranked lists by reciprocal rank fusion, each scored the sum of
    1/(`RRF_K` + rank) over the lists it is in, ties in the order first seen; `rest` after them."""
    score: dict[int, float] = {}
    for ranked in lists:
        for rank, nid in enumerate(ranked, 1):
            score[nid] = score.get(nid, 0.0) + 1.0 / (RRF_K + rank)
    order = list(score)
    return sorted(order, key=lambda nid: (-score[nid], order.index(nid))) + [n for n in rest if n not in score]


def expand(store: Store, ids: Sequence[int], why: Mapping[int, str] | None = None) -> list[dict]:
    """Notes with their anchors and links, in the order given."""
    if not ids:
        return []
    project_rows = {}
    for note_id in ids:
        note = store.get_note(note_id)
        if note is not None:
            project_rows[note_id] = note
    anchors: dict[int, list[dict]] = {}
    for a in store.note_anchors(list(project_rows)):
        anchors.setdefault(a["note_id"], []).append(
            {"path": a["path"], "symbol": a["symbol"], "commit": a["commit_sha"]})
    links: dict[int, list[dict]] = {}
    edges = store.note_links(list(project_rows))
    ends = {e[k] for e in edges for k in ("from_id", "to_id")} - set(project_rows)
    project = next(iter(project_rows.values()))["project"] if project_rows else ""
    dropped = {n["id"] for n in store.notes(project, states=["dropped"], ids=ends)} if ends else set()
    for edge in edges:
        if edge["from_id"] in dropped or edge["to_id"] in dropped:
            continue  # a note found wrong is no lead
        if edge["from_id"] in project_rows:
            links.setdefault(edge["from_id"], []).append({"to": edge["to_id"], "kind": edge["kind"], "out": True})
        if edge["to_id"] in project_rows:
            links.setdefault(edge["to_id"], []).append({"to": edge["from_id"], "kind": edge["kind"], "out": False})
    return [{**project_rows[i], "why": (why or {}).get(i, ""), "anchors": anchors.get(i, []),
             "links": links.get(i, [])} for i in ids if i in project_rows]


def line(note: Mapping[str, Any], *, text_chars: int = 400) -> str:
    """One note as a model reads it."""
    out = f"#{note['id']} [{note['topic']}] {note['title']}: {note['text'][:text_chars]}"
    if note.get("unmerged"):
        out += (" (WRITTEN ON WORK NOT MERGED where this task starts: what it says of the code may not be in "
                "your checkout; read the code before relying on it)")
    if note["state"] == "review":
        out += f" (TO REVIEW: {note.get('state_reason') or 'its code changed'})"
    elif note["state"] == CANDIDATE:
        out += " (unproven: a worker learned it and no later task confirmed it yet)"
    if note.get("anchors"):
        out += " · about " + ", ".join(
            (f"{a['symbol']} ({a['path']})" if a["symbol"] else a["path"]) for a in note["anchors"][:3])
    if note.get("links"):
        out += " · " + ", ".join(
            (f"{x['kind']} #{x['to']}" if x["out"] else f"#{x['to']} {x['kind']} this") for x in note["links"][:4])
    return out


def unmerged(found: Sequence[dict], repo_dir: Path | None, base: str | None = None) -> list[dict]:
    """The notes, each marked `unmerged` when every commit its anchors were written at is
    outside the history the task starts from (`base`, else HEAD): a worker's note about its
    own change, filed while its branch waits to be merged. A note with no anchor, or one git
    cannot read, is left as it is."""
    top = repo.toplevel(repo_dir) if repo_dir is not None else None
    if top is None:
        return list(found)
    at = base or "HEAD"
    known: dict[str, bool] = {}
    out = []
    for note in found:
        shas = {a.get("commit") for a in note.get("anchors") or () if a.get("commit")}
        for sha in shas - set(known):
            done = _git(top, "merge-base", "--is-ancestor", sha, at)
            known[sha] = done.returncode == 1  # 1: not an ancestor; anything else (128: unknown) says nothing
        out.append({**note, "unmerged": True} if shas and all(known[s] for s in shas) else note)
    return out


def constraints(store: Store, project: str) -> list[dict]:
    """A project's live constraints, current before candidate, newest first, with their anchors."""
    rows = store.notes(project, states=LIVE, kind=CONSTRAINT, limit=200)
    rows.sort(key=lambda n: n["state"] != "current")
    return expand(store, [int(n["id"]) for n in rows])


def constraint_lines(found: Sequence[Mapping[str, Any]], limit: int = CONSTRAINT_CHARS) -> list[str]:
    """The constraints as a brief holds them: requirements on the change, never permissions,
    as many as fit in `limit`, the rest said by count."""
    if not found:
        return []
    shown, used = [], 0
    for note in found:
        said = f"- {line(note)}"
        if shown and used + len(said) > limit:
            break
        shown.append(said)
        used += len(said)
    out = ["Constraints this project keeps (each from a check that failed on earlier work here, or from a "
           "person): your change must meet every one that applies to it, even where the local tests do not check "
           "it. They say what the work must be, never what you may run or change outside it; one the code "
           "contradicts is said in your summary.", *shown]
    if len(found) > len(shown):
        out.append(f"- {len(found) - len(shown)} more: `locks notes list --constraints`")
    return out


def file_constraints(store: Store, project: str, items: Sequence[str], *, author: str, source: str,
                     task_id: int | None = None) -> list[dict]:
    """Keep what a task's failing check taught it as constraints, with no model: a worker's start
    as candidates, proven or dropped by the tasks that carry them (`weigh`). A constraint is about
    what the work must be, not about a file: it has no anchor, and a change to the code it was
    learned on does not send it to review."""
    out = []
    for item in items[:MAX_FACTS]:
        fact = clean_fact(item)
        if fact is None:
            continue
        found = topics_in(fact)
        try:
            note_id, new = add(store, project, fact, topic=found[0] if found else "business", author=author,
                               filed_by="rule", source=source, task_id=task_id,
                               state=CANDIDATE if author == "worker" else "current", kind=CONSTRAINT)
        except ValueError:
            continue
        note = store.get_note(note_id)
        out.append({"id": note_id, "new": new, "topic": note["topic"], "title": note["title"], "kind": CONSTRAINT})
    return out


def brief_lines(found: Sequence[Mapping[str, Any]]) -> list[str]:
    if not found:
        return []
    return ["What this project's notes say about it (locks memory, kept from earlier work; check a note against "
            "the code before relying on it, and trust the code when they disagree):",
            *(f"- {line(n)}" for n in found)]


def index_text(store: Store, project: str, *, latest: int = 6) -> str | None:
    """The project's notes in a few lines: what topics hold, what is to review,
    the latest titles, and how to read the rest. What a session gets back after
    compacting, instead of the notes themselves."""
    counts = store.note_counts(project)
    live = {t: sum(n for s, n in by.items() if s in LIVE) for t, by in counts.items()}
    total = sum(live.values())
    if not total:
        return None
    parts = [f"{t} {n}" + (f" ({counts[t].get('review', 0)} to review)" if counts[t].get("review") else "")
             for t, n in sorted(live.items(), key=lambda kv: -kv[1]) if n]
    lines = [f"locks notes: this project keeps {total} note(s) of what it knows — " + ", ".join(parts) + ".",
             "Read them before rebuilding context from the code: `locks recall \"<question>\"` (add `--topic <t>` "
             "or `--path <file>`). Keep a durable fact the person tells you with `locks note \"<fact>\" --topic <t>`, "
             f"where <t> is one of: {', '.join(topics(store, project))}."]
    recent = store.notes(project, states=LIVE, limit=latest)
    if recent:
        lines.append("Latest: " + "; ".join(f"#{n['id']} [{n['topic']}] {n['title']}" for n in recent))
    if waiting := sum(by.get("review", 0) for by in counts.values()):
        lines.append(f"{waiting} note(s) wait for review: their files changed since they were written "
                     "(`locks notes list --review`). Check one against the code before relying on it, then keep it "
                     "(`locks notes ok N`) or drop it (`locks notes drop N`).")
    return "\n".join(lines)


# --- filing with Haiku ------------------------------------------------------

FILE_PROMPT = (
    "You file facts about one software project into its notes, a library kept by topic. You are given the "
    "project's topics, some of its existing notes, the files the work touched, and the new facts. For each fact "
    "answer: keep (false when it is trivial, only about this one task's progress, a guess, or plain from reading "
    "the code), topic (one of the listed topics, the one a person would look in), title (three to eight words, in"
    " the fact's own language, never translated), duplicate_of (the id of an existing note that already says it, "
    "else null), links (to existing notes it relates to, with a kind: depends_on, explains, replaces when the new"
    " fact makes that note wrong, contradicts, example_of), paths (only files from the given list the fact is "
    "about) and new_topic (a short name only when no topic fits; the fact is still filed under the closest one). "
    "Never invent ids."
)

EXTRACT_PROMPT = (
    "You read part of a conversation between a person and a coding agent working on one software project, and "
    "keep what is worth knowing the next time someone works on it, in the project's notes, a library kept by "
    "topic. Keep only durable facts the conversation established: business rules and domain words the person "
    "stated, decisions and why they were made, what was tried and dropped, conventions, how to run, build, test "
    "or deploy it, how a part of the code works. Never keep: the task's progress or plans, guesses, things later "
    "contradicted, anything about the agent itself, secrets, credentials, tokens or personal data. At most eight "
    "facts, often none. Each fact is one or two self-contained sentences, and it and its title are written in "
    "English, whatever language the conversation is in, so a project's notes read the same to every worker; a "
    "domain word, a name or a quote of the person stays exactly as they wrote it. For each give topic (one "
    "of the listed), title (three to eight words), duplicate_of (the id of an existing note that already says "
    "it, else null), links to existing notes (depends_on, explains, replaces, contradicts, example_of), paths "
    "(only files the conversation itself names as what the fact is about; usually none) and new_topic (a short "
    "name only when no topic fits). Never invent ids."
)


def _entry_schema(names: Sequence[str], *, with_text: bool) -> dict[str, Any]:
    props: dict[str, Any] = {
        "topic": {"type": "string", "enum": list(names)},
        "title": {"type": "string"},
        "duplicate_of": {"type": ["integer", "null"]},
        "links": {"type": "array", "items": {
            "type": "object",
            "properties": {"to": {"type": "integer"}, "kind": {"type": "string", "enum": list(LINKS)}},
            "required": ["to", "kind"], "additionalProperties": False}},
        "paths": {"type": "array", "items": {"type": "string"}},
        "new_topic": {"type": ["string", "null"]},
    }
    if with_text:
        props = {"text": {"type": "string"}, **props}
    else:
        props = {"fact": {"type": "integer"}, "keep": {"type": "boolean"}, **props}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _list_of(key: str, item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": {key: {"type": "array", "items": item}}, "required": [key],
            "additionalProperties": False}


def file_schema(names: Sequence[str]) -> dict[str, Any]:
    return _list_of("notes", _entry_schema(names, with_text=False))


def extract_schema(names: Sequence[str]) -> dict[str, Any]:
    return _list_of("facts", _entry_schema(names, with_text=True))


def _candidates(store: Store, project: str, texts: Sequence[str], limit: int = 30) -> list[dict]:
    ids: list[int] = []
    for text in texts:
        ids += [i for i in store.search_notes(project, text, limit=5) if i not in ids]
    found = {n["id"]: n for n in store.notes(project, states=LIVE, ids=ids)}
    out = [found[i] for i in ids if i in found]
    for n in store.notes(project, states=LIVE, limit=10):
        if n["id"] not in found:
            out.append(n)
    return out[:limit]


def _question(store: Store, project: str, candidates: Sequence[Mapping], paths: Sequence[str], body: str) -> str:
    lines = ["Topics:", *(f"- {t}: {d}" for t, d in topics(store, project).items()), "", "Existing notes:"]
    lines += [f"#{n['id']} [{n['topic']}] {n['title']}: {n['text'][:200]}" for n in candidates] or ["(none yet)"]
    if paths:
        lines += ["", "Files the work touched:", *(f"- {p}" for p in paths[:40])]
    return "\n".join([*lines, "", body])


def _ask(ask: Ask | None) -> Ask:
    if ask is not None:
        return ask
    from locks.classify import ask_haiku

    return ask_haiku


def file_facts(
    store: Store,
    project: str,
    facts: Sequence[str],
    *,
    author: str,
    source: str,
    task_id: int | None = None,
    session_id: str | None = None,
    repo_dir: Path | None = None,
    paths: Sequence[str] = (),
    commit: str | None = None,
    symbols_of: Callable[[str], list[tuple[str, str]]] | None = None,
    ask: Ask | None = None,
) -> list[dict]:
    """File what a worker learned: Haiku picks topic, title, links and the files
    each fact is about. Returns one entry per fact kept: {id, new, topic, title}."""
    kept = [f for f in (clean_fact(x) for x in facts[:MAX_FACTS]) if f]
    if not kept:
        return []
    names = list(topics(store, project))
    candidates = _candidates(store, project, kept)
    body = "Facts:\n" + "\n".join(f"{i}. {f}" for i, f in enumerate(kept))
    out, _cost, _why = _ask(ask)(FILE_PROMPT, file_schema(names), _question(store, project, candidates, paths, body),
                                 cwd=home())
    decided: dict[int, dict] = {}
    for entry in (out or {}).get("notes") or []:
        if isinstance(entry, dict) and isinstance(entry.get("fact"), int) and 0 <= entry["fact"] < len(kept):
            decided.setdefault(entry["fact"], entry)
    known = {int(n["id"]) for n in candidates}
    filed = []
    for i, fact in enumerate(kept):
        entry = decided.get(i)
        if out is not None and entry is not None and not entry.get("keep", True):
            continue
        filed.append(_file_one(store, project, fact, entry if out is not None else None, names, known,
                               author=author, source=source, task_id=task_id, session_id=session_id,
                               repo_dir=repo_dir, allowed_paths=paths, commit=commit, symbols_of=symbols_of))
    return [f for f in filed if f]


def _file_one(store: Store, project: str, fact: str, entry: Mapping[str, Any] | None, names: Sequence[str],
              known: set[int], *, author: str, source: str, task_id: int | None, session_id: str | None,
              repo_dir: Path | None, allowed_paths: Sequence[str] | None, commit: str | None,
              symbols_of: Callable[[str], list[tuple[str, str]]] | None) -> dict | None:
    if entry is not None and entry.get("topic") in names:
        topic, filed_by = str(entry["topic"]), "haiku"
        title = str(entry.get("title") or "").strip() or None
        duplicate = entry.get("duplicate_of") if entry.get("duplicate_of") in known else None
        links = [(int(x["to"]), str(x["kind"])) for x in entry.get("links") or []
                 if isinstance(x, dict) and x.get("to") in known and x.get("kind") in LINKS]
        paths = [str(p) for p in entry.get("paths") or [] if isinstance(p, str)]
        proposed = str(entry.get("new_topic") or "").strip()[:40] or None
    else:
        found = topics_in(fact)
        topic, filed_by, title, duplicate, links, paths, proposed = (
            found[0] if found else "code", "rule", None, None, [], [], None)
    if allowed_paths is not None and allowed_paths:
        paths = [p for p in paths if p in set(allowed_paths)]
    elif repo_dir is not None:
        top = repo.toplevel(repo_dir) or repo_dir
        paths = [p for p in paths if (top / p).is_file()]
    else:
        paths = []
    symbols = symbols_of(fact) if symbols_of else []
    if duplicate is not None:
        for to_id, kind in links:
            link(store, duplicate, to_id, kind)
        if repo_dir is not None:
            anchor(store, duplicate, repo_dir, paths, symbols, commit=commit)
        note = store.get_note(duplicate)
        return {"id": duplicate, "new": False, "topic": note["topic"], "title": note["title"]}
    try:
        note_id, new = add(store, project, fact, topic=topic, author=author, filed_by=filed_by, title=title,
                           source=source, task_id=task_id, session_id=session_id, links=links, paths=paths,
                           symbols=symbols, repo_dir=repo_dir, commit=commit,
                           proposed_topic=proposed if proposed and proposed not in names else None,
                           state=CANDIDATE if author == "worker" else "current")
    except ValueError:
        return None
    note = store.get_note(note_id)
    return {"id": note_id, "new": new, "topic": note["topic"], "title": note["title"]}


# --- what a session said --------------------------------------------------------

#: How much of a session's new conversation Haiku reads at once, the latest kept.
TRANSCRIPT_CHARS = 40_000
MESSAGE_CHARS = 3_000
#: Less new conversation than this is not worth a model call yet.
MIN_CONVERSATION = 80
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_COMMAND = re.compile(r"^\s*<(command-[a-z-]+|local-command-[a-z-]+|bash-[a-z-]+|task-notification)>")


def conversation(path: Path, offset: int) -> tuple[str, int]:
    """The person's and the agent's words in a transcript after `offset`, without
    tool calls, tool output or injected context; and the offset reached."""
    from locks.usage import _records

    said: list[str] = []
    end = offset
    try:
        for position, entry in _records(path, offset):
            end = position
            if not isinstance(entry, dict) or entry.get("isSidechain") or entry.get("isMeta") or (
                    entry.get("isCompactSummary")):
                continue  # a compaction's summary retells what was already read
            role = entry.get("type")
            if role not in ("user", "assistant"):
                continue
            content = (entry.get("message") or {}).get("content")
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            else:
                texts = []
            for text in texts:
                text = _REMINDER.sub("", str(text)).strip()
                if not text or _COMMAND.match(text):
                    continue
                said.append(f"{'Person' if role == 'user' else 'Agent'}: {text[:MESSAGE_CHARS]}")
    except OSError:
        return "", offset
    return "\n\n".join(said)[-TRANSCRIPT_CHARS:], end


def extract(
    store: Store,
    project: str,
    session_id: str,
    transcript: Path,
    *,
    repo_dir: Path | None = None,
    symbols_of: Callable[[str], list[tuple[str, str]]] | None = None,
    ask: Ask | None = None,
) -> list[dict]:
    """Keep what a session established since the last extraction. The offset only
    moves when Haiku answered: a failed call is tried again next time."""
    key = f"notes:{transcript}"
    text, end = conversation(transcript, store.transcript_offset(key))
    if len(text) < MIN_CONVERSATION:
        # Too little to ask about yet: it stays unread, and is read with what
        # comes after it. One short sentence can be a rule worth keeping.
        return []
    names = list(topics(store, project))
    candidates = _candidates(store, project, [text[-4000:]])
    out, _cost, _why = _ask(ask)(EXTRACT_PROMPT, extract_schema(names),
                                 _question(store, project, candidates, (), "Conversation:\n" + text), cwd=home())
    if out is None:
        return []
    store.set_transcript_offset(key, end)
    known = {int(n["id"]) for n in candidates}
    filed = []
    for entry in (out.get("facts") or [])[:MAX_FACTS]:
        if not isinstance(entry, dict):
            continue
        fact = clean_fact(str(entry.get("text") or ""))
        if fact is None:
            continue
        # A file the conversation never names is a guess, and a wrong anchor
        # sends a right note to review.
        entry = {**entry, "paths": [p for p in entry.get("paths") or []
                                    if isinstance(p, str) and p and (p in text or Path(p).name in text)]}
        filed.append(_file_one(store, project, fact, entry, names, known, author="session",
                               source=f"session {session_id}", task_id=None, session_id=session_id,
                               repo_dir=repo_dir, allowed_paths=None, commit=None, symbols_of=symbols_of))
    return [f for f in filed if f]


Symbols = Callable[[str], list[tuple[str, str]]]


def livespec_symbols(repo_dir: Path, env: Mapping[str, str] | None = None) -> Symbols | None:
    """A lookup of the symbols a fact names, from the livespec index, read-only.
    None when livespec is off or the repository is not indexed."""
    from locks import config

    if not config.enabled("livespec", env if env is not None else os.environ):
        return None
    from locks.adapters.livespec import Livespec

    adapter = Livespec(env)
    status = adapter.inspect(repo_dir)
    if not status.present:
        return None
    return lambda fact: adapter.symbols_named(fact, status)


def to_json(found: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(list(found), ensure_ascii=False, default=str)
