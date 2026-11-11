"""How much a change can hurt, read from its request, its kind and what it
changed: `passive`, `medium` or `high`, with the reasons.

High is any of: it touches data (a migration, a schema, a delete), security
(auth, secrets, crypto, permissions), money (payments, billing), a contract
others consume (an API spec, a protocol, a public interface), concurrency,
delivery (CI, deploy, infrastructure), or it changes code with no test changed
beside it, so nothing would catch a mistake. A tier is only ever raised: the
request sets where it starts, and each attempt's files can raise it, never
lower it. A change that cannot be read (no checkout) is high: not knowing is
not safe.

A reason must be true, so a word is read in its context: "checkout" in git's
sense (`git checkout`, "this checkout", "a read-only checkout", a checkout
path, a worktree) is no money; a design "token", a chart library "migrated" or
a label "truncated" is no security and no data, unless a word of the area's own
sense is near (`_SENSES`); "lock" in a dependency lockfile (`package-lock`, "lock file")
is no concurrency; a "schema" of a config or a linter (`$schema`, a JSON schema, eslint's) is
no data; "deploy" said not to change ("no deployment changes") or about a linter's config is no
delivery, unless the change touches deploy files; a word inside a path or an environment variable's
name is no word of the request, and "payment" as one name in a list of names (an event, an enum) is no
money until the change touches payment code. "Parallel" is never read away: a
request that only mentions tasks running beside each other is still treated as
concurrency, since a missed one skips the verifier and an extra one only costs
a check.

No model is asked. The tier decides one thing: whether a passing change is
checked again by an independent verifier before it counts.
"""
from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

TIERS = ("passive", "medium", "high")

_TEXT = (
    ("data", re.compile(r"\b(migrat\w*|schema|drop\s+(table|column)|backfill|delete\s+from|truncate|data ?loss|"
                        r"base de datos|database)\b", re.I)),
    ("security", re.compile(r"\b(auth\w*|login|password|secret|token|credential|crypt\w*|oauth|jwt|"
                            r"session cookie|csrf|xss|injection|vulnerab\w*|segurid\w*|contraseñ\w*|"
                            # Enforcement: the first four say it alone, the last three only beside
                            # their own sense (`_SENSES`).
                            r"bypass\w*|sandbox\w*|allow-?list\w*|allow list\w*|deny-?list\w*|"
                            r"permissions?|polic(?:y|ies)|guards?)\b", re.I)),
    ("money", re.compile(r"\b(payment\w*|billing|invoice\w*|checkout|refund\w*|pago\w*|factur\w*|cobro\w*)\b", re.I)),
    ("concurrency", re.compile(r"\b(race|deadlock|concurren\w*|thread\w*|mutex|lock(ing)?|async|parallel\w*)\b",
                               re.I)),
    ("contract", re.compile(r"\b(public api|breaking change|endpoint\w*|protocol|webhook\w*|api contract|"
                            r"openapi|graphql schema)\b", re.I)),
    ("delivery", re.compile(r"\b(deploy\w*|ci pipeline|github actions|dockerfile|kubernetes|terraform|"
                            r"release|infra\w*)\b", re.I)),
)
_PATHS = (
    ("money", ("**/*payment*", "**/*billing*", "**/*invoice*", "**/*refund*")),
    ("data", ("**/migrations/**", "**/migrate/**", "*.sql", "**/schema.*", "**/schema/**", "**/alembic/**",
              "**/prisma/schema.prisma")),
    ("security", ("**/auth/**", "**/*auth*.*", "**/*security*", "**/*crypto*", "**/*permission*", "**/.env*",
                  "**/*secret*")),
    ("contract", ("**/openapi*.*", "**/swagger*.*", "*.proto", "**/*.proto", "**/*.graphql", "**/api/**/*.d.ts")),
    ("delivery", (".github/workflows/*", "**/Dockerfile", "Dockerfile", "**/docker-compose*.yml", "*.tf",
                  "**/*.tf", "**/k8s/**", "**/helm/**", ".gitlab-ci.yml", "**/deploy/**")),
)
#: The areas a path can be in, read by `locks.cells` too.
PATH_AREAS = _PATHS
#: What follows "checkout" in a shop, and what a request about a shop says elsewhere.
_SHOP_NOUN = re.compile(r"\s+(?:totals?|pages?|flow|forms?|buttons?|carts?|prices?|amounts?|steps?|screens?|"
                        r"summary|process|fees?|tax(?:es)?|discounts?|coupons?|items?|lines?|api|endpoints?)\b", re.I)
_SHOP = re.compile(r"\b(carts?|baskets?|pric(?:e|es|ing)|stripe|paypal|currenc(?:y|ies)|tax(?:es)?|discounts?|coupons?|"
                   r"purchas\w*|shop\w*|e-?commerce|skus?|merchants?|shipping|pay|paid)\b", re.I)
_DETERMINER = re.compile(r"\b(?:the|this|that|your|their|its|our|my|own|a|an|each|every|same|person's)\s+$", re.I)
_BUILD = re.compile(r"\b(?:add|build|implement|create|design|redesign|ship)\s+(?:a|an|the)\s+$", re.I)
#: Three names or more in a list (`client_approval`, `payment`, `access`): labels, one of them a word.
_NAME = r"[`'\"]?[^\W\d]\w*[`'\"]?"
_NAMES = re.compile(rf"{_NAME}(?:\s*(?:,|\||/|\bor\b|\band\b)\s*{_NAME}){{2,}}", re.I)


def _line(text: str, found: re.Match[str]) -> str:
    start = text.rfind("\n", 0, found.start()) + 1
    end = text.find("\n", found.end())
    return text[start:end if end >= 0 else len(text)]


def _git_checkout(text: str, found: re.Match[str]) -> bool:
    """Whether "checkout" is git's: `git checkout`, a path, a worktree, "this checkout" with no shop near it."""
    before, after = text[max(0, found.start() - 60):found.start()], text[found.end():found.end() + 40]
    if re.search(r"\bgit\s+$", before, re.I) or re.search(r"\bworktrees?\s+$", before, re.I):
        return True
    if before.endswith(("/", "\\")) or after.startswith(("/", "\\")):
        return True
    if re.match(r"(?:'s)?\s+(?:paths?|dir|directory|folders?|root)\b", after, re.I):
        return True
    if re.search(r"\b(?:read-only|local|clean|fresh|separate|sibling|second|other)\s+$", before, re.I) \
            and not _SHOP_NOUN.match(after):
        return True
    return bool(_DETERMINER.search(before)) and not _BUILD.search(before) and not _SHOP_NOUN.match(after) \
        and not _SHOP.search(text)


def _a_name(text: str, found: re.Match[str]) -> bool:
    """A word used as a label: one of a list of names, or quoted before "event"."""
    line, start = _line(text, found), found.start() - (text.rfind("\n", 0, found.start()) + 1)
    quoted = found.start() > 0 and text[found.start() - 1] in "`'\""
    if quoted and re.match(r"[`'\"]\s+events?\b", text[found.end():]):
        return True
    return any(m.start() <= start < m.end() and ("_" in m.group(0) or "`" in m.group(0))
               for m in _NAMES.finditer(line))


#: Words read in another sense when the words around them say so, and none of the
#: area's own: (area, the word's start, its other sense, its own sense nearby).
_ANYWHERE = re.compile(r"")
_SENSES = (
    ("security", "token",
     re.compile(r"\b(design|css|colou?rs?|themes?|styles?|styling|palettes?|tailwind|typograph\w*|spacing|"
                r"radius|shadows?|fonts?)\b|--[a-z]", re.I),
     re.compile(r"\b(auth\w*|sessions?|jwt|bearer|api|api[ -]?keys?|leak\w*|expir\w*|revok\w*|rotat\w*|"
                r"secrets?|oauth|login|refresh|access|csrf|"
                r"credentials?|passwords?|sign(?:ed|ing)?)\b", re.I)),
    ("data", "migrat",
     re.compile(r"\b(charts?|components?|svg|librar(?:y|ies)|frameworks?|echarts|d3|css|styles?|ui|views?|"
                r"islands?|react|preact|vue|svelte|widgets?|icons?|pages?|routes?|templates?|apps?)\b", re.I),
     re.compile(r"\b(databases?|schemas?|tables?|columns?|rows?|records?|sql|backfill\w*|postgres\w*|mysql|"
                r"sqlite|prisma|alembic|tenants?|users? data|data model)\b", re.I)),
    ("data", "schema",
     re.compile(r"\$schema|\b(json[ -]?schemas?|eslint\w*|lint\w*|config\w*|tsconfig|prettier|biome|ajv|zod|yaml|"
                r"settings|validat\w*|editor|vs ?code|intellisense)\b", re.I),
     re.compile(r"\b(databases?|tables?|columns?|rows?|records?|sql|backfill\w*|postgres\w*|mysql|sqlite|prisma|"
                r"alembic|migrat\w*|indexes|indices|foreign keys?|data model|tenants?)\b", re.I)),
    ("delivery", "deploy",
     re.compile(r"\b(eslint\w*|lint\w*|prettier|biome|formatt\w*|typecheck\w*|tsconfig|editorconfig|"
                r"code style|style guide)\b", re.I),
     re.compile(r"\b(pipelines?|ci|cd|docker\w*|kubernetes|k8s|helm|terraform|infra\w*|rollouts?|rollbacks?|"
                r"github actions|workflows?|render\.yaml|vercel|netlify|heroku|fly\.io|servers?|hosting)\b",
                re.I)),
    # Words of enforcement that are security only beside another: "permission to merge",
    # "the retry policy" and "a guard clause" are none. The other sense is anywhere.
    ("security", "permission", _ANYWHERE,
     re.compile(r"\b(grant\w*|refus\w*|den(?:y|ies|ied|ying)|polic(?:y|ies))\b", re.I)),
    ("security", "polic", _ANYWHERE,
     re.compile(r"\b(enforc\w*|guard\w*|refus\w*|bypass\w*)\b", re.I)),
    ("security", "guard", _ANYWHERE,
     re.compile(r"\b(polic(?:y|ies)|refus\w*|bypass\w*)\b", re.I)),
    ("data", "truncat",
     re.compile(r"\b(text|labels?|ellipsis|strings?|titles?|names?|characters?|chars|lines?|words?|axis|"
                r"captions?|overflow)\b", re.I),
     re.compile(r"\b(tables?|databases?|sql|rows|records|logs?|history|data)\b", re.I)),
)
#: How far around a word its sense is read.
_SENSE_WINDOW = 80
#: What is never a word of the request's own: a path, an environment variable's name.
_UNREAD = re.compile(r"(?:~|\.{1,2})?/[\w.\-/]+|\b[\w.-]+(?:/[\w.-]+)+\.\w+\b|\b[\w.-]+(?:/[\w.-]+){2,}"
                     r"|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


def _masked(text: str) -> str:
    """The text with paths and environment variable names blanked, positions kept: a word
    inside `MECHA_TEST_TRUNCATE` or `src/migrations/x.ts` is no word of the request."""
    return _UNREAD.sub(lambda m: " " * len(m.group(0)), text)


def _other_sense(area: str, text: str, found: re.Match[str]) -> bool:
    word = found.group(0).lower()
    for sense_area, start, other, own in _SENSES:
        if sense_area == area and word.startswith(start):
            near = text[max(0, found.start() - _SENSE_WINDOW):found.end() + _SENSE_WINDOW]
            return bool(other.search(near)) and not own.search(near)
    return False


#: "lock" as a dependency lockfile: `package-lock`, `yarn.lock`, "lock file", "lock-file".
_LOCKFILE_BEFORE = re.compile(r"(?:\b(?:package|pnpm|npm|yarn|bun|deno|cargo|poetry|composer|gemfile|pipfile|uv)"
                              r"[-.]|\.)$", re.I)
_LOCKFILE_AFTER = re.compile(r"[ -]?files?\b", re.I)
#: A word said not to change: "no deployment changes", "without deploying", "sin deploy".
_NEGATED = re.compile(r"\b(?:no|not|don.?t|without|never|nor|zero|sin|ni|nada de)\s+(?:\w+\s+){0,2}$", re.I)


def _lockfile(text: str, found: re.Match[str]) -> bool:
    before, after = text[max(0, found.start() - 12):found.start()], text[found.end():found.end() + 8]
    return bool(_LOCKFILE_BEFORE.search(before) or _LOCKFILE_AFTER.match(after))


def _own_sense(area: str, text: str, found: re.Match[str]) -> bool:
    word = found.group(0).lower()
    near = text[max(0, found.start() - _SENSE_WINDOW):found.end() + _SENSE_WINDOW]
    return any(sense_area == area and word.startswith(start) and own.search(near)
               for sense_area, start, _, own in _SENSES)


def _not_meant(area: str, text: str, found: re.Match[str]) -> bool:
    """Whether the word found is used in another sense than the one its area is about."""
    word = found.group(0).lower()
    if _other_sense(area, text, found):
        return True
    if area == "concurrency" and word == "lock":
        return _lockfile(text, found)
    if area == "delivery" and word.startswith("deploy"):
        # Said not to change, and no word of delivery's own near it: context, not the change.
        return bool(_NEGATED.search(text[max(0, found.start() - 40):found.start()])) \
            and not _own_sense(area, text, found)
    if area == "money" and word == "checkout":
        return _git_checkout(text, found)
    if area == "money":
        return _a_name(text, found)
    # "parallel" always counts: a request about tasks running beside each other is read
    # as concurrency too, because a missed one skips the verifier and an extra one only
    # costs a check.
    return False


#: Kinds whose work touches nothing a person ships: no verifier.
_PASSIVE_KINDS = frozenset({"chat", "explore", "docs", "plan", "classify"})
_HIGH_KINDS = frozenset({"review-critical", "frontier"})
_DOCS = frozenset({".md", ".rst", ".txt", ".adoc"})
_TEST = re.compile(r"(^|/)(tests?|spec|__tests__)(/|$)|(_test|\.test|\.spec|_spec)\.[a-z]+$|(^|/)test_[^/]+$")
_CODE = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".cs", ".rb", ".php",
                   ".swift", ".lua", ".c", ".cc", ".cpp", ".h", ".hpp", ".sh", ".sql", ".vue", ".svelte"})


@dataclass
class Risk:
    tier: str = "passive"
    reasons: list[str] = field(default_factory=list)

    def raise_to(self, tier: str, reason: str) -> None:
        if TIERS.index(tier) > TIERS.index(self.tier):
            self.tier = tier
        if reason not in self.reasons:
            self.reasons.append(reason)

    def data(self) -> dict[str, object]:
        return {"tier": self.tier, "reasons": list(self.reasons)}


def of_request(text: str, kind: str) -> Risk:
    """Where a task's risk starts, from its words and its kind."""
    risk = Risk("passive" if kind in _PASSIVE_KINDS else "medium")
    if kind in _HIGH_KINDS:
        risk.raise_to("high", f"a {kind} task")
    read = _masked(text)
    for area, pattern in _TEXT:
        found = next((m for m in pattern.finditer(read) if not _not_meant(area, read, m)), None)
        if found and kind not in _PASSIVE_KINDS:
            risk.raise_to("high", f"the request is about {area} ({found.group(0).strip()!r})")
    return risk


#: The reason `of_request` gives for an area, read back by `areas`.
_ABOUT = re.compile(r"^the request is about (\w+) \((['\"])(.*)\2\)$")


def areas(risk: Risk) -> list[tuple[str, str]]:
    """The areas the request is about, each with the word that showed it: what a task's
    size counts (`locks.points`)."""
    return [(m.group(1), m.group(3)) for m in map(_ABOUT.match, risk.reasons) if m]


def path_matches(path: str, patterns: Iterable[str]) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in patterns)


_matches = path_matches


#: Code files one change touches past which it is read as broad, whatever else it is: a change of 86
#: files that removed a feature passed as medium with no independent check (#3281 of a field report).
WIDE_FILES = 40


def of_change(risk: Risk, changed: Sequence[str] | None) -> Risk:
    """The tier raised by what an attempt changed. None means it could not be read."""
    if changed is None:
        risk.raise_to("high", "what changed could not be read")
        return risk
    for area, patterns in _PATHS:
        hit = next((p for p in changed if _matches(p, patterns)), None)
        if hit:
            risk.raise_to("high", f"it changed {area} ({hit})")
    code = [p for p in changed if PurePosixPath(p).suffix.lower() in _CODE and not _TEST.search(p)]
    tests = [p for p in changed if _TEST.search(p)]
    if code and not tests:
        risk.raise_to("high", f"it changed code ({code[0]}{' and more' if len(code) > 1 else ''}) "
                              "and no test beside it")
    elif code:
        risk.raise_to("medium", "it changed code and its tests")
    if len(code) >= WIDE_FILES:
        risk.raise_to("high", f"it changed {len(code)} files of code, a broad change")
    if changed and all(PurePosixPath(p).suffix.lower() in _DOCS for p in changed):
        risk.reasons.append("it changed documentation only")
    return risk


def needs_verifier(risk: Risk, *, cheap: bool) -> bool:
    """A change high in risk is always verified; a medium one when a cheap cell made it."""
    return risk.tier == "high" or (risk.tier == "medium" and cheap)
