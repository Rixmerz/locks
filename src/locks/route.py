"""Which way a request is done: by the session itself, by one worker, or in parts side by side.

locks started by sending work to workers. A worker costs a plan, a worktree, a
cold start on the code and, for a risky change, a verifier: for a small edit
the session that already holds the code does it faster, and a single strong
session is what most work wants. A worker earns its cost when the work is long
enough to run while the session goes on, or when the session's context is worth
protecting; parts earn theirs when the work splits into pieces that run at
once. `choose` reads the request with no model (its kind by rules, its size and
risk by the plan's own estimate, its listed items, the files and areas it names)
and the session's context, and says which, with its reasons. It is advice to the
orchestrating session, which decides; each decision is kept (`routed`) so the
choice can be measured against how the work went.

Standard library only, no model: the prompt hook runs it.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from locks import criteria as criteria_mod
from locks import points as points_mod
from locks import risk as risk_mod
from locks.classify import asks_for_change, by_rules, opens_with_change
from locks.matrix import READ_ONLY_KINDS

MAIN, WORKER, PARALLEL = "main", "worker", "parallel"
#: At this size or under, the session does it: a worker's plan and cold start cost more than the work.
MAIN_POINTS = 2
#: At this size or over, or touching `PARALLEL_AREAS` areas or more, the work runs in parts.
PARALLEL_POINTS = 8
PARALLEL_AREAS = 2
#: Independent pieces a request lists, each naming its own top-level folder: parts.
PARALLEL_PIECES = 3
#: A session whose context is this full keeps it for what needs it: writing work goes to a worker.
CONTEXT_FULL = 0.6
#: The turns work of each size takes, at most the worker's measured median: a typo is not a feature.
TURNS_BY_POINTS = {1: 5, 2: 8, 3: 13, 5: 20, 8: 30, 13: 40}
#: A turn that read this much tool output into the session's own context (about 10k tokens, re-read by
#: every turn after it) is said at the next prompt, with what an explore worker here costs instead.
READ_HEAVY_CHARS = 40_000
#: A request this short is a turn of the conversation, never routed.
MIN_WORDS = 6
#: Words that point back into the conversation: the session holds what they mean, a worker does not.
_DEICTIC = re.compile(r"\b(esto|eso|aquello|lo (?:de|que) (?:arriba|antes|hablamos|dijimos)|"
                      r"como (?:dijimos|hablamos|antes)|this one|that one|as (?:we )?discussed|the above|"
                      r"as above|what we (?:said|discussed))\b", re.I)
#: A request that asks to understand rather than to change: its opening word, or a question.
_READS = re.compile(r"^\W*(explain|why|what|how|where|when|which|who|review|audit|investigate|analy[sz]e|compare|"
                    r"summari[sz]e|describe|explica|por qu[eé]|qu[eé]|c[oó]mo|d[oó]nde|cu[aá]l|revisa|audita|"
                    r"investiga|analiza|compara|resume|describe)\b", re.I)

#: A question about the work (whether, what if, is it worth it), asked mid-text as often as at its end:
#: a question mark after one of these openings is a turn of the conversation, never work to send.
_ASKS = re.compile(r"(?:^|[.!?¿]\s*)\W*(?:y |tambi[eé]n |pero |entonces )?(?:se )?(?:podr[ií]a\w*|"
                   r"deber[ií]a\w*|conviene|vale la pena|realmente|ser[ií]a|tendr[ií]a\w*|habr[ií]a|"
                   r"qu[eé] (?:pasa|tal|opinas|te parece)|y si|could|should|would|is it worth|what if|"
                   r"does it make sense|do we need|can we|hay (?:forma|manera)|is there a way)\b[^?]*\?", re.I)
#: A Spanish or English opening that asks for the session's preference, typed without its question
#: mark ("preferirias que en vez de trabajar en la nube…"): a question all the same, only at the
#: request's start.
_ASKS_UNMARKED = re.compile(r"^\W*(?:y |pero |entonces )?(?:preferir[ií]as|prefieres|qu[eé] prefieres|crees que|"
                            r"te parece(?: bien| mejor)? (?:que|si)|(?:no )?ser[ií]a mejor|valdr[ií]a la pena|"
                            r"vale la pena|conviene|would you (?:rather|prefer)|do you think|"
                            r"is it better)\b", re.I)
#: A request whose first sentence is itself a question ("¿lo arreglaste?", "did it pass?").
_FIRST_ASKS = re.compile(r"^\W*[^.!?\n]{3,}\?")

#: An opening that talks to the session itself: the answer is this conversation's to give.
#: Spanish informs with an imperative and a pronoun (indícame, muéstrame, explícame); "resume"
#: is Spanish only before what it sums up ("resume lo que hicimos"), never "resume the task".
_TALKS = re.compile(r"^\W*(?:habla\w*|h[aá]blame|dime|dinos|d[ií]game|cu[eé]nta(?:me|nos)|"
                    r"ind[ií]ca(?:me|nos)|mu[eé]stra(?:me|nos)|expl[ií]ca(?:me|nos)|ens[eé][ñn]a(?:me|nos)|"
                    r"aclara(?:me|nos)|res[uú]me(?:lo|me|nos)|resum[ií]\w*|"
                    r"resume\s+(?:lo|los|las|el|la|todo|qu[eé]|en|brevemente|c[oó]mo)\b|"
                    r"(?:lista|enumera)(?:me|nos)?\s+(?:los|las|todos|todas|qu[eé]|cu[aá]l\w*)\b|"
                    r"responde\w*|contesta\w*|qu[eé] (?:opinas|piensas|te parece)|tell (?:me|us)|"
                    r"what do you think|summari[sz]e|walk me through)\b", re.I)
#: A question about where the work stands: what is pending, what is left, how it ended.
_STATE = re.compile(r"^\W*(?:y |entonces |oye,? )?(?:(?:tienes|tenemos|tengo|hay|queda|quedan|quedo|quedaron)\s+"
                    r"(?:algo\s+|algunos?\s+|algunas?\s+|cosas\s+|tareas\s+)?(?:pendientes?|por hacer)|"
                    r"qu[eé] (?:falta|faltan|queda|quedan|qued[oó]|est[aá] pendiente|hay pendiente)|"
                    r"en qu[eé] (?:qued[oó]|qued[aá]|quedamos|va|vamos|estamos|est[aá])|"
                    r"c[oó]mo (?:va|vamos|qued[oó]|est[aá] (?:la|el|lo))|"
                    r"what'?s (?:left|pending|missing)|is (?:there )?anything (?:left|pending)|"
                    r"where (?:are we|did we leave|do we stand))\b", re.I)
#: A request for a report back, anywhere in the text ("Necesito que me des todos los errores…",
#: "dame la lista…"): the session that holds the code and the conversation writes it.
_REPORT = re.compile(r"(?:^|[.!?:;\n]\s*|,\s*|\b(?:y|and)\s+)\W*(?:"
                     r"(?:necesito|quiero|quisiera|me gustar[ií]a|podr[ií]as|puedes)\s+(?:que\s+)?"
                     r"(?:me\s+|nos\s+)?(?:des|digas|indiques|muestres|expliques|cuentes|listes|enumeres|"
                     r"reportes|resumas|entregues|dar(?:me|nos)|decir(?:me|nos)|indicar(?:me|nos)|"
                     r"mostrar(?:me|nos)|explicar(?:me|nos)|listar(?:me|nos)|resumir(?:me|nos))|"
                     r"d[aá](?:me|nos)\s+(?:un|una|el|la|los|las|todos|todas|tu)\b|"
                     r"(?:dime|ind[ií]ca(?:me|nos)|mu[eé]stra(?:me|nos)|expl[ií]ca(?:me|nos))\b|"
                     r"(?:i need|i want|i'd like) you to (?:tell|give|list|show|explain|report|summari[sz]e)|"
                     r"(?:give|tell|show) me (?:a list|all|every|the list|what|which|why|how)|report back)", re.I)
#: A sentence asking for a report that also asks for a change: the report is how the change is told.
_REPORT_CHANGES = re.compile(r"\b(?:arregl\w*|corrij\w*|corrig\w*|implement\w*|agreg\w*|a[ñn]ad\w*|"
                             r"cambi(?:es|ar|a)\b|modifiq\w*|modific\w*|elimin\w*|borr\w*|actualic\w*|"
                             r"cre(?:es|ar|a)\b|resuelv\w*|fix\w*|add|implement|change|update|remove|build|write)\b",
                             re.I)


#: Work that leans on what this conversation holds: a live incident, a hotfix, a diagnosis or a
#: decision just reached, a correction the person just made (issue #124). A worker sees none of it.
_CONVERSATION = re.compile(
    r"\b(hot ?fix\w*|incident\w*|incidente\w*|outage|ca[ií]d[oa] en producci[oó]n|prod(?:uction|ucci[oó]n)? "
    r"(?:is |est[aá] )?(?:down|ca[ií]d[oa])|urgent\w*|urgente\w*|right now|ahora mismo|ya mismo|"
    r"as (?:we|you) (?:said|discussed|found|agreed)|como (?:dijiste|dijimos|vimos|acordamos|hablamos)|"
    r"(?:the|esa|la|this|esta) (?:hypothesis|hip[oó]tesis|diagnosis|diagn[oó]stico)|"
    r"(?:what|lo que) (?:we|you|acabamos de|acabas de) (?:just )?(?:found|saw|encontr\w*|vi\w*|decid\w*))\b",
    re.IGNORECASE)


def _sentence(text: str, at: int) -> str:
    start = max(text.rfind(mark, 0, at) for mark in ".!?\n")
    ends = [i for i in (text.find(mark, at) for mark in ".!?\n") if i >= 0]
    return text[start + 1:min(ends) if ends else len(text)]


def reports(text: str) -> bool:
    """Whether a request asks for a report back (necesito que me des…, dime…, give me a list…)
    and no change in that same sentence: a pasted list of observations that asks what is wrong
    is answered here. A request that opens with a verb of change is an order."""
    if opens_with_change(text):
        return False
    return any(not _REPORT_CHANGES.search(_sentence(text, m.end()))
               for m in _REPORT.finditer(text))


def talks(text: str) -> bool:
    """Whether a request talks to the session: it opens by doing so (tell me, habla, dime,
    indícame, explícame, qué opinas…), asks where the work stands (tienes pendientes, qué falta,
    en qué quedó), or asks for a report back. What it asks is an answer, and the session holding
    the conversation gives it. A request that opens with a verb of change is an order."""
    if opens_with_change(text):
        return False
    return bool(_TALKS.search(text) or _STATE.search(text)) or reports(text)


def asks(text: str) -> bool:
    """Whether a request is a question about the work rather than the work: an opening
    of doubt (could, should, what if, vale la pena…) ending in a question mark, at the
    start or after a sentence, a first sentence that is a question, or an opening that asks for a
    preference with no mark at all (`_ASKS_UNMARKED`). A request that
    opens with a verb of change is an order, whatever it asks after it."""
    return bool(_ASKS.search(text) or _FIRST_ASKS.search(text) or _ASKS_UNMARKED.search(text)) \
        and not opens_with_change(text)


#: The bundled workflow a worker-bound request of a kind is also told of (`Route.workflow`): the
#: chain made for that kind of work. Said, and kept on the `routed` event; the session decides.
WORKFLOW_FOR = {"debug-repro": "bugfix", "debug-unclear": "bugfix"}


@dataclass(frozen=True)
class Route:
    mode: str
    reasons: tuple[str, ...]
    kind: str = ""
    points: int = 0
    areas: tuple[str, ...] = field(default=())

    @property
    def workflow(self) -> str:
        """The bundled workflow made for this request's kind, when it goes to a worker: said beside
        the queue line as the other way to send it, never chosen for the session."""
        return WORKFLOW_FOR.get(self.kind, "") if self.mode == WORKER else ""

    def advice(self) -> str:
        """What the orchestrating session is told; nothing when the session does it, the default."""
        why = "; ".join(self.reasons)
        if self.mode == WORKER:
            chain = (f" A bug has a chain of its own: `locks workflow run {self.workflow} \"<the bug>\"` reproduces "
                     "it with a failing test, fixes it and has the fix reviewed blind." if self.workflow else "")
            return (f"locks route: a worker ({why}). Queue it (locks' `queue` tool, or `locks queue add`) with "
                    "the request written in full and its check, and go on here; do it yourself instead if it turns "
                    "out to need this conversation." + chain)
        if self.mode == PARALLEL:
            return (f"locks route: in parts, side by side ({why}). Run `locks workflow run feature \"<the request>\"` "
                    "(it splits by itself), or queue one task per independent piece, each with its own `--surface`, "
                    "together; what depends on another piece goes `--after` it.")
        return ""

    def data(self) -> dict:
        return {"mode": self.mode, "reasons": list(self.reasons), "kind": self.kind, "points": self.points,
                "areas": list(self.areas), **({"workflow": self.workflow} if self.workflow else {})}


#: A build request listing this many items, in at least `SPLIT_WORDS` words, is several tasks:
#: `locks run` and `queue add` ask it split. A list of one-word items is one small task.
SPLIT_ITEMS, SPLIT_WORDS = 6, 40
#: The kinds a request is asked split in: work that builds, where one worker outgrows its turns.
SPLIT_KINDS = frozenset({"feature", "implement"})


#: An item that states a rule, a limit, a test for the other items or what not to do: context for the
#: pieces, never a piece of its own.
_RULE_ITEM = re.compile(
    r"\b(?:not|never|don'?t|without|untouched|only|must|should)\b|^\W*(?:tests?|checks?|verify|constraints?|rules?)\b",
    re.IGNORECASE)


def is_rule(item: str) -> bool:
    """A listed item that states a rule rather than a change: it has a rule's words and opens with no
    verb of change. "Add the endpoint, only for admins" is a piece; "Do not edit CHANGELOG.md",
    "Tests: cover each item" and "Standard library only" are rules."""
    return _RULE_ITEM.search(item) is not None and not opens_with_change(item)


def pieces_of(text: str) -> list[str]:
    """The request's listed items that can be a piece of their own: the ones that are no rule."""
    return [item for item in criteria_mod.items(text) if not is_rule(item)]


def one_module(items: list[str]) -> bool:
    """Whether the files the items name are all in one top folder of `src/`: one module, one task."""
    tops = set()
    for item in items:
        for path in points_mod.named(item):
            parts = PurePosixPath(path.strip("/")).parts
            if parts[:1] != ("src",) or len(parts) < 3:
                return False
            tops.add(parts[1])
    return len(tops) == 1


#: A document the request names that `points._PATH` does not read: an office file or a PDF.
_DOCUMENT = re.compile(r"(?<![\w@/.:-])[\w][\w .-]*?\.(?:docx?|xlsx?|pptx?|odt|ods|odp|pdf|rtf)\b", re.IGNORECASE)


def one_file(text: str, items: list[str]) -> bool:
    """Whether the request is the changes to one file or document: it names a single file, or a
    single document whose items name no paths in different folders. Items that each change one
    file are one task however many they are."""
    docs = {m.group(0).split("/")[-1].strip().lower() for m in _DOCUMENT.finditer(text)}
    files = {PurePosixPath(p).name.lower() for p in points_mod.named(text) if PurePosixPath(p.strip("/")).suffix}
    if len(docs | files) == 1:
        return True
    if len(docs) != 1:
        return False
    folders = {PurePosixPath(p.strip("/")).parent for item in items for p in points_mod.named(item)}
    return len(folders) <= 1


def kept_whole(text: str, kind: str | None = None) -> str | None:
    """Why a build that lists enough items to be split is one task anyway, or None: its items
    are all changes to one file or document, or all in one module of `src/`. Pure."""
    if kind is None:
        ruled = by_rules(text)
        kind = asks_for_change(text, ruled).kind if ruled is not None else (
            "implement" if opens_with_change(text) else None)
    if kind not in SPLIT_KINDS:
        return None
    items = pieces_of(text)
    if len(items) < SPLIT_ITEMS:
        return None
    if one_file(text, items):
        return f"its {len(items)} listed items are all changes to one file or document"
    if one_module(items):
        return f"its {len(items)} listed items all touch one module of src/"
    return None


def split_first(text: str, kind: str | None = None) -> str | None:
    """Why a request should be sent as several tasks before one worker starts it, or None:
    a build (`kind`, else read by rules, with no model) that lists `SPLIT_ITEMS` pieces or
    more in `SPLIT_WORDS` words or more, is the largest size, or lists `PARALLEL_PIECES`
    pieces in folders of their own. A listed rule (a constraint, a test for the others, what
    not to do) is no piece, and items that all touch one module of `src/` are one task.
    One worker on all of it outgrows its turns, and the climb that follows (more turns,
    another model) costs more than the parts would. Pure."""
    if kind is None:
        ruled = by_rules(text)
        kind = asks_for_change(text, ruled).kind if ruled is not None else (
            "implement" if opens_with_change(text) else None)
    if kind not in SPLIT_KINDS:
        return None
    items = pieces_of(text)
    if one_module(items) or one_file(text, items):
        return None
    size = points_mod.of_request(text, kind, risk_mod.of_request(text, kind)).points
    pieces = listed_pieces(text)
    why = [f"{len(items)} listed items"] if len(items) >= SPLIT_ITEMS and len(text.split()) >= SPLIT_WORDS else []
    if size >= max(points_mod.SCALE):
        why.append(f"{size} points")
    if pieces >= PARALLEL_PIECES:
        why.append(f"{pieces} pieces in folders of their own")
    return ", ".join(why) or None


def listed_pieces(text: str) -> int:
    """How many of the request's listed items name a file in a top-level folder of their own."""
    tops = set()
    for item in pieces_of(text):
        for path in points_mod.named(item):
            parts = PurePosixPath(path.strip("/")).parts
            if len(parts) > 1:
                tops.add(parts[0])
    return len(tops)


def _tokens(n: float) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def read_advice(chars: int, calls: int, explore: Mapping[str, Any] | None = None) -> str | None:
    """What the last turn's own reading cost, when it was heavy: the output it read with its tools
    stays in this context and every later turn re-reads it, where an explore worker reads in its own.
    `explore` is the kind's record here (`Store.kind_record`: passes, their median time and cost), said
    as observed; advice, never a rule."""
    if chars < READ_HEAVY_CHARS:
        return None
    said = (f"locks route: last turn this session read about {_tokens(chars // 4)} tokens of tool output into its "
            f"own context over {calls} call(s), and every turn after re-reads them.")
    if explore and explore.get("passes"):
        took = f" at {explore['p50_s']:.0f}s" if explore.get("p50_s") is not None else ""
        cost = f" and ${explore['cost_per_pass']:.2f} a pass" if explore.get("cost_per_pass") is not None else ""
        said += (f" An explore worker here passed {explore['passes']} task(s){took}{cost} (observed) and leaves "
                 "none of what it read in this context.")
    else:
        said += " An explore worker reads in its own context and leaves none of it here."
    return said + (" Reading that long is a worker's: `locks run --kind explore \"<question>\"` (or the `queue` tool), "
                   "and this session reads its report.")


def choose(text: str, *, context_tokens: int | None = None, context_share: float | None = None,
           footprint: tuple[int, int] | None = None) -> Route:
    """The way to do a request, with its reasons. Pure: everything it weighs is the request's
    words, the session's context (its tokens and how full it is) and what a worker attempt
    reads here as measured (`footprint`: median tokens and turns, `Store.worker_footprint`).

    Every turn a session takes re-reads its whole context, so work done here reads about
    the worker's turns times the session's context; a worker reads its own measured
    footprint, cold start included. The cheaper of the two is the way, once the request is
    work at all: reading, and what points back into the conversation, stay here; what splits
    runs in parts."""
    words = len(text.split())
    if words < MIN_WORDS:
        return Route(MAIN, ("a turn of the conversation",))
    if talks(text):
        return Route(MAIN, ("it talks to this session, which answers it",))
    if asks(text):
        return Route(MAIN, ("a question about the work, for this conversation to answer",))
    ruled = by_rules(text)
    # With no rule and no model, a question or an opening of reading is reading; else a build.
    kind = asks_for_change(text, ruled).kind if ruled is not None else (
        "explore" if (_READS.search(text) or text.rstrip().endswith("?")) and not opens_with_change(text)
        else "implement")
    risk = risk_mod.of_request(text, kind)
    size = points_mod.of_request(text, kind, risk)
    areas = tuple(sorted({area for area, _ in risk_mod.areas(risk)}))
    pieces = listed_pieces(text)
    base = dict(kind=kind, points=size.points, areas=areas)
    if kind in READ_ONLY_KINDS:
        return Route(MAIN, (f"{kind}: reading, which the session holding the code does best",), **base)
    if size.points >= PARALLEL_POINTS or len(areas) >= PARALLEL_AREAS or pieces >= PARALLEL_PIECES:
        why = [f"{size.points} points"] if size.points >= PARALLEL_POINTS else []
        if len(areas) >= PARALLEL_AREAS:
            why.append(f"{len(areas)} areas ({', '.join(areas)})")
        if pieces >= PARALLEL_PIECES:
            why.append(f"{pieces} listed pieces in separate folders")
        return Route(PARALLEL, tuple(why), **base)
    if (leans := _CONVERSATION.search(text)) and size.points <= MAIN_POINTS + 1:
        return Route(MAIN, (f"it leans on this conversation (\"{leans.group(0)}\"), which a worker does not see",),
                     **base)
    if _DEICTIC.search(text):
        return Route(MAIN, ("it points back into this conversation, which a worker does not see",), **base)
    if footprint and context_tokens:
        measured_tokens, measured_turns = footprint
        # Each turn re-reads its context: a worker's measured reading per turn against this session's.
        turns = min(TURNS_BY_POINTS.get(size.points, measured_turns), measured_turns) or 1
        here, there = turns * context_tokens, turns * measured_tokens // max(measured_turns, 1)
        said = (f"done here it reads about {_tokens(here)} tokens ({turns} turns over this session's "
                f"{_tokens(context_tokens)} context), a worker about {_tokens(there)} at its measured "
                f"{_tokens(measured_tokens // max(measured_turns, 1))} a turn")
        if there < here:
            return Route(WORKER, (said, f"{size.points} points of {kind}"), **base)
        return Route(MAIN, (said,), **base)
    full = context_share is not None and context_share >= CONTEXT_FULL
    if size.points <= MAIN_POINTS and not full:
        return Route(MAIN, (f"{size.points} points: smaller than a worker's start-up",), **base)
    if full:
        return Route(WORKER, (f"this session's context is {context_share:.0%} full: keep it for what needs it",
                              f"{size.points} points of {kind}"), **base)
    return Route(WORKER, (f"{size.points} points of {kind}: long enough to run while the session goes on",), **base)
