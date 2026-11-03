"""One task attempt, run as a one-shot `claude -p` worker.

A one-shot worker instead of a subagent, for four reasons a subagent cannot
give:

- **Its tools are enforced, not requested.** `--tools`, `--disallowedTools`
  and `--strict-mcp-config` decide what exists in the worker. A subagent told
  "you may not edit" in its prompt still has the edit tool.
- **Its model and effort are per attempt.** Escalation needs a different cell
  on every retry; a subagent's model is fixed in its charter.
- **Its context is its own and dies with it.** A debugging session's dead ends,
  logs and tool output never reach the orchestrator. What comes back is the
  result block, nothing else.
- **It runs where it is told.** It is launched from one directory (whose
  settings and instructions it loads) and works in another (`--add-dir`).

Three things it never takes from the model. Changed files come from git, not
the worker's report — the party being checked does not supply the evidence. A
missing or ambiguous verdict is *inconclusive*, never a pass. And what the
permission settings refused comes from the CLI's own `permission_denials`: a
worker that could not run `npm run build` is blocked on a setting, whatever it
says about the code.
"""
from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from locks import allow, grants, legacy, models, surfaces
from locks import criteria as criteria_mod
from locks import findings as findings_mod
from locks import ledger as ledger_mod
from locks.escalate import REPLAN, Failure
from locks.isolate import is_artifact
from locks.matrix import Cell
from locks.notes import _PRIVATE, _SECRET
from locks.text import clip

RESULT_FENCE = "locks-result"
#: Where a block opens. Its JSON is decoded straight from there, not cut at the next
#: ```: evidence that quotes a fenced snippet, or a raw tab in a string, would
#: otherwise end or break the block and cost the attempt its verdict.
_OPEN_RE = re.compile(rf"```(?:{RESULT_FENCE}|{legacy.OLD_RESULT_FENCE})[ \t]*\r?\n", re.IGNORECASE)
#: A report a reading worker was asked for, beside its result block: findings too many
#: and too structured for `summary` and `evidence`. Kept on the task, never a verdict.
REPORT_FENCE = "locks-report"
_REPORT_OPEN_RE = re.compile(rf"```(?:{REPORT_FENCE}|{legacy.OLD_REPORT_FENCE})[ \t]*\r?\n", re.IGNORECASE)
_REPORT_RE = re.compile(rf"```(?:{REPORT_FENCE}|{legacy.OLD_REPORT_FENCE})\s*\n(?P<body>.*?)\n```",
                        re.DOTALL | re.IGNORECASE)
#: A JSON decoder that takes control characters (a raw tab) inside strings.
_DECODER = json.JSONDecoder(strict=False)
REPORT_CHARS = 60_000
#: A network that is down: a name that does not resolve, a host that cannot be
#: reached. Said by a tool (npm, git, curl) or by the session's own API call.
NETWORK_DOWN = re.compile(
    r"\b(ENOTFOUND|EAI_AGAIN|ENETUNREACH|EHOSTUNREACH|ERR_INTERNET_DISCONNECTED|getaddrinfo\b[^\n]{0,40}|"
    r"could not resolve host|temporary failure in name resolution|name or service not known|"
    r"network is unreachable|npm ERR! network)", re.IGNORECASE)
#: What else drops a session's own call to the API for a moment: a reset, a
#: timeout, an overload. Read only where the session itself errored, never in a
#: check's output, where a test may well print it.
NETWORK_FLAKY = re.compile(
    r"\b(ECONNRESET|ETIMEDOUT|socket hang up|fetch failed|connection error|connection reset|overloaded_error|"
    r"API Error: 5\d\d|bad gateway|service unavailable|gateway timeout|request timed out)", re.IGNORECASE)

#: The API turning a session's calls away for a while: a rate or usage limit (429), or an
#: overload (529). Read only from the session's own failed call (the CLI's result, or its
#: own error output when it gave none), never from a check's output, where a test may print it.
RATE_LIMITED = re.compile(r"\b(?:429|rate[_ ]limit(?:ed|_error)?|too many requests|usage limit reached)\b",
                          re.IGNORECASE)
OVERLOADED = re.compile(r"\b(?:529|overloaded(?:_error)?)\b", re.IGNORECASE)
_RETRY_AFTER = re.compile(r"retry[-_ ]after\W{0,3}(\d{1,6})", re.IGNORECASE)
_RESETS_AT = re.compile(r"usage limit reached\|(\d{10})", re.IGNORECASE)
#: The account's own limit (a session's, the week's, a model's), as the CLI says it: no retry
#: in seconds passes it, only its reset. Read only where `RATE_LIMITED` is.
USAGE_LIMIT = re.compile(
    r"usage limit reached|\b(?:session|weekly|daily|5-hour|opus|sonnet|fable)(?: usage)? limit(?: reached| hit)?\b|"
    r"hit your (?:\w+ )?limit|limit will reset|you(?:'ve| have) reached your (?:\w+ )?(?:usage )?limit|"
    r"out of (?:extra )?usage", re.IGNORECASE)
#: The reset a limit message names: `resets 5pm`, `resets at 3:30am (America/Santiago)`,
#: `resets Oct 27, 9am`.
_RESETS = re.compile(
    r"\breset(?:s| at| will be at)?\s+(?:at\s+)?(?:(?P<mon>[A-Z][a-z]{2})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+-]+)+|UTC)\))?",
    re.IGNORECASE)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def resets_in(text: str, *, now: datetime | None = None) -> float | None:
    """Seconds until the reset a limit message names, read in its time zone (the machine's
    when it names none), the next such moment after `now`; None when it names none."""
    if found := _RESETS_AT.search(text or ""):
        at = float(found.group(1))
        return max(at - (now.timestamp() if now else time.time()), 0.0)
    found = _RESETS.search(text or "")
    if found is None:
        return None
    hour, minute, ap = int(found["h"]), int(found["m"] or 0), (found["ap"] or "").lower().replace(".", "")
    if (ap and not 1 <= hour <= 12) or hour > 23 or minute > 59:
        return None
    hour = hour % 12 + (12 if ap == "pm" else 0) if ap else hour
    try:
        zone = ZoneInfo(found["tz"]) if found["tz"] else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    here = (now or datetime.now(UTC)).astimezone(zone)
    at = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if found["mon"] and found["mon"].lower()[:3] in _MONTHS:
        month = _MONTHS.index(found["mon"].lower()[:3]) + 1
        try:
            at = at.replace(month=month, day=int(found["day"]))
        except ValueError:
            return None
        if at <= here:
            at = at.replace(year=at.year + 1)
    elif at <= here:
        at += timedelta(days=1)
    return (at - here).total_seconds()
#: A disk with no room left, as the CLI or the OS says it.
NO_SPACE = re.compile(r"\bENOSPC\b|no space left on device", re.IGNORECASE)
#: The exit of a CLI the kernel killed (SIGKILL), as its out-of-memory killer does.
KILLED = frozenset({-9, 137})

DEFAULT_MAX_TURNS = 30
#: Past this a system prompt goes in a file: one argument is capped at 128 KiB
#: on Linux, and a role with its skills comes near.
INLINE_PROMPT_CHARS = 16000
DEFAULT_TIMEOUT_S = 1800
#: Wall-clock time per turn: an attempt given more turns is given the time to use them.
SECONDS_PER_TURN = 60


def timeout_for(turns: int) -> int:
    """How long an attempt of `turns` turns may run: never under the default."""
    return max(DEFAULT_TIMEOUT_S, turns * SECONDS_PER_TURN)

#: Tools a reading task never needs. Removing them is what makes "read-only"
#: true instead of requested.
WRITE_TOOLS: tuple[str, ...] = ("Edit", "Write", "NotebookEdit", "MultiEdit")

RESULT_INSTRUCTIONS = f"""
When you are done, end your reply with exactly one fenced block:

```{RESULT_FENCE}
{{
  "verdict": "pass" | "fail" | "inconclusive",
  "failure": "code_bug" | "test_bug" | "approach" | "spec_bug" | "architecture_bug" | "environment" | null,
  "summary": "one or two sentences: what you did, or what stopped you",
  "commit": "the change in one line for the project's history, imperative, under 72 characters",
  "evidence": "the command you ran to check it and its real output, verbatim",
  "learned": ["a durable fact about this project that you confirmed and the next person would want"],
  "orientation": ["what you had to find before your first change: a file, a command, a trap"],
  "notes_used": [12],
  "constraints": ["a rule a failing check showed this work must meet, that no file here states"],
  "criteria": [{{"id": "S1", "met": true, "evidence": "what shows it"}}],
  "findings": [{{"where": "path:line", "what": "the problem",
                "evidence_class": "deterministic" | "inferential" | "insufficient",
                "cause": "introduced" | "worsened" | "pre-existing" | "unknown",
                "proof": "the command or hunk that shows it", "fixed": false}}],
  "verdicts": [{{"id": "F1", "verdict": "one of the words the task gives", "proof": "what shows it"}}]
}}
```

- `pass` means the task is done and you can show it. If you could not run what
  would show it, the verdict is `inconclusive`, not `pass`.
- If the task contradicts itself or its check, or cannot be done as written,
  the verdict is `fail` with `spec_bug` — say what is contradictory. Do not
  bend the work to make a check pass.
- `evidence` is terminal output, not a description of it. A pass without it is
  refused.
- On a failure, `failure` says where the problem lives: `code_bug` (you left a
  bug or missed a case), `test_bug` (a test you relied on is wrong), `approach`
  (the direction was wrong), `spec_bug` (the task as written cannot be done),
  `architecture_bug` (the design prevents it), `environment` (a tool or service
  was missing or down).
- `summary` says what the files now show, nothing more. Never describe a change
  you did not make or could not save; if you changed nothing, say so.
- If a command you needed was refused, name it in `summary`; the verdict is
  `inconclusive`. The work you did stays: it is kept for a person to see.
- Nobody can approve a command while you run: what your settings do not allow
  is refused. Read and search with the Read, Grep and Glob tools, which need no
  approval, rather than with shell commands. Run one shell command per call
  instead of chaining them with `;`, `&&` or `|`: every part of a chain is
  checked, and one refused part refuses all of it.
- Run a project's tools by name (`npm test`, `npx tsc`), as its scripts do. Do
  not source a version manager or call a binary by its full path: the
  project's own Node, when it declares one, is already first on your PATH, and
  if the version is still wrong, answer `fail` with `environment` and say so.
- A command that never returns (a dev server, a watcher) blocks the call it
  runs in: start it in the background and stop it before you finish. You have
  no browser unless your tools include one; without it, answer `fail` with
  `environment` and say so, never describe a page you did not open.
- Run your check in the foreground and wait for it. Anything you start in the
  background tells you when it ends, after you answered, and your reply to that
  is the one read: every reply you end, that one too, ends with the whole
  result block again.
- A check that fails the same way without your change (tests, lint or coverage
  of code you did not touch, or were told not to) is not yours to fix and no
  reason to stop: when your own work is done, answer `pass`, and name those
  failures in `summary` as already failing before your change. locks runs the
  check at the base too and tells the two apart.
- Do not list the files you changed; they are read from git.
- `commit` is the subject of the commit that keeps your work, as the project's
  own history writes them (`Fix rounding of the checkout total`, or
  `fix(checkout): round the total once` where its commits use a type): what the
  change does, never the task's wording, a task number or a file list. `""`
  when you changed nothing.
- `learned` goes into the project's notes, read by whoever works here next. Put
  at most five facts you confirmed while working that outlive this task: a
  business rule, why the code is shaped this way, a convention, how to run or
  test something, a trap. Not what you changed, not guesses, not anything
  plain from reading the code, never a secret. `[]` when there is none.
- `orientation`: at most six short lines on what you had to find out before
  your first change, so the next worker on this kind of work here starts
  there: where the code for it lives (`path` and what it holds), the command
  that checks it, a trap you hit. Only what you used; never a secret. `[]`
  when you needed nothing.
- `notes_used`: the numbers of the notes from this project's memory in your brief
  (`#12`) that you relied on; `[]` when none helped or none were given.
- `constraints`: only when a check failed on this task (its output is above)
  and showed a rule the work must meet that nothing in the repository says (a
  policy, a limit, a case CI enforces): the rule in one line, and the check line
  that showed it. The next work here is told to meet it. `[]` otherwise.
- `criteria`: when the task lists criteria `S1..Sn`, one entry for each, met or
  not, with what shows it. A pass that leaves one out is not a pass, and nor is
  one that says your work does not meet one. `[]` when the task lists none.
- `findings`: when you review, audit or verify, each problem you found, as a
  record. `evidence_class` is `deterministic` when a command, a test or the
  diff shows it, `inferential` when you read it from the code, `insufficient`
  when you only suspect it. `cause` is `introduced` or `worsened` when the
  change being checked is to blame, `pre-existing` when it was there before
  (check the base commit), `unknown` when you cannot tell. `fixed` is true for
  a problem you found and fixed in this change (a bug your new test caught and
  you then fixed): it is reported, never offered as work left. `[]` when there
  is none, or when you were not asked to find problems.
- `verdicts`: when the task gives you ledger rows `F1..Fn` to decide, one entry
  for each, with one of the words it names. A pass that leaves one out is not a
  pass. `[]` otherwise.
""".strip()

#: Added to the contract of a step whose deliverable is a verdict about other work.
JUDGING_INSTRUCTIONS = """
You judge work someone else did; your deliverable is that judgment. A criterion
the work you judge does not meet, or a row you confirm, is your finding: report
it (`met: false`, `findings`, `verdicts`) and answer `pass` when you delivered
the judgment and can show it. `fail` is for a judgment you could not make, never
for what you found wrong.
""".strip()


#: The most parts a plan splits into: the machine's level, not this, bounds how many build
#: at once, and a part that builds on another waits for it. A part's prompt and the contract
#: have no length limit: they reach their workers on stdin, and a plan is never thrown away
#: for being thorough.
MAX_PARTS = 10
#: A part's id: short, as a branch, a title and a status line show it.
PART_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: Added to the contract of a plan step a workflow fans out over (`fanout`).
PARTS_INSTRUCTIONS = """
Your plan runs in parts, side by side, each built by a worker of its own and
joined after. Beside the fields above, your result block carries:

- `parts`: 2 to 10 parts (1 when splitting does not pay), in the order they are
  joined, each `{"id": "short-name", "prompt": "what this part builds",
  "surfaces": ["src/area/**", "tests/test_area.py"]}`. A part's surfaces are the
  only files its worker may change, and no other part's surfaces may reach them:
  never one file, folder or glob in two parts. A part's surfaces hold the
  existing tests its change will break or must adjust (the fixtures and
  assertions of the code it changes: the code index's `git_diff_impact` or
  the callers' tests name them), never a dependency folder such as
  `node_modules/` or `vendor/`. Its prompt stands on its own (its
  worker reads it, not your reasoning) and lists, verbatim, the request's
  criteria that part answers.
- A part that builds on another's work (the screens on the endpoints, the
  endpoints on the schema) says so with `"after": "<that part's id>"`, naming one
  part listed before it: it starts once that part passes, from what that part
  built. Parts with no `after` build side by side. Order the work in phases
  this way rather than leaving a part to guess what another will write.
- `contract`: what every part must keep the same (the names, types, interfaces,
  schemas or endpoints they share), as text; each part is given it verbatim.

Read only what the split needs: the layout, the entry points, and the schemas
and interfaces the parts will share. Each part's worker reads its own code, so
a plan that reads every file spends its turns on their work.
""".strip()


def instructions(spec: LaunchSpec) -> str:
    """The result contract a worker is given, with the judging note for a step that judges
    and what a plan a workflow fans out over must add."""
    return RESULT_INSTRUCTIONS + ("\n\n" + JUDGING_INSTRUCTIONS if spec.judging else "") + (
        "\n\n" + PARTS_INSTRUCTIONS if spec.parts else "")


#: What a session given more turns is told: its brief, its context and its prompt
#: cache are its own already, so only the contract is said again.
CONTINUE_PROMPT = ("You ran out of turns before you finished. Continue from where you are: your work so far is in "
                   "this directory, and the task is the one you were given. Check it before you answer.")
#: What a session given more effort on the same model is told: its approach stands
#: and its context is its own; what it lacked was depth, and what failed is said below.
DEEPER_PROMPT = ("Your last answer to this task did not pass; what failed it is below. Your work so far is in this "
                 "directory and the task is the one you were given. You now think at a higher effort: look deeper "
                 "than before, find why it failed and what else the same cause breaks, fix it, and check it "
                 "before you answer.")
#: What a session whose answer could not be read is told: its work stands, only the form
#: of its answer failed, and what failed it is said below.
RESHAPE_PROMPT = ("Your last answer to this task could not be read; why is below. Your work so far is in this "
                  "directory and stands: do not redo it. Answer again, in the result contract you were given, "
                  "with exactly one result block.")
#: The CLI's words when a session it was asked to resume is not there to resume.
_NO_SESSION = re.compile(r"no conversation found|session[^\n]{0,60}(?:not found|does not exist|could not be "
                         r"(?:found|loaded|resumed))|invalid session", re.IGNORECASE)


Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class LaunchSpec:
    prompt: str
    cell: Cell
    #: Where the worker starts. Its project settings and instructions load from
    #: here, and it is the directory its changes are read from unless `workdir`
    #: says otherwise.
    launch_dir: Path
    #: Where the work happens, when that is not the launch directory.
    workdir: Path | None = None
    #: Directories outside it the worker may read: the checkout's dependency
    #: folders a worktree links to, which otherwise resolve outside its reach.
    extra_dirs: tuple[Path, ...] = ()
    #: What `--model` gets, when a person pinned the cell's alias to a model id.
    model_id: str | None = None
    max_turns: int = DEFAULT_MAX_TURNS
    timeout_s: int = DEFAULT_TIMEOUT_S
    #: None leaves the built-in tool set alone; a tuple names the only ones.
    tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    #: Permission rules a person granted for this task, e.g. `Bash(npm run build)`:
    #: without them a one-shot worker is refused anything its settings do not allow.
    allowed_tools: tuple[str, ...] = ()
    #: The MCP servers this worker gets, as an `mcpServers` mapping. Always
    #: strict: a worker sees only what its task was given.
    mcp_servers: Mapping[str, Any] = field(default_factory=dict)
    permission_mode: str = "acceptEdits"
    append_system_prompt: str = ""
    #: The criteria `S#` the task lists: a pass must say each one is met.
    criteria: tuple[str, ...] = ()
    #: The step's deliverable is a verdict about other work (a review, a refute, a recheck, a reading
    #: kind): a criterion it says the work under judgment does not meet is its finding, carried
    #: forward, not a failure of the step. A step that builds or fixes keeps the rule.
    judging: bool = False
    #: The ledger rows the task decides, and the words a verdict may use (`locks.ledger`).
    verdicts: tuple[str, ...] = ()
    verdict_words: tuple[str, ...] = ()
    #: A plan a workflow fans out over: a pass carries its `parts` and their `contract` (`read_parts`).
    parts: bool = False
    persist_session: bool = False
    #: The worker session this attempt continues (`--resume`), told to go on:
    #: an attempt given more turns on the same cell, in the same directory. "" starts fresh.
    resume_session: str = ""
    #: What that session is told first: `CONTINUE_PROMPT` for more turns, `DEEPER_PROMPT` for more effort.
    resume_prompt: str = CONTINUE_PROMPT
    #: What it is told beside that: the output that failed its attempt (locks' own
    #: check after it stopped), or the independent check's blockers, which its context does not hold.
    resume_note: str = ""
    #: The continued session is a fork (`--fork-session`), the one it came from kept as it was:
    #: more effort on the same model, at the new effort the cell gives.
    fork_session: bool = False
    #: Extra environment for the worker: which task and attempt it is, so its
    #: tool calls are recorded against them.
    env: Mapping[str, str] = field(default_factory=dict)

    @property
    def target_dir(self) -> Path:
        return self.workdir or self.launch_dir


@dataclass(frozen=True)
class WorkerResult:
    passed: bool
    failure: Failure | None
    summary: str
    evidence: str = ""
    changed_paths: tuple[str, ...] = ()
    cost_usd: float = 0.0
    #: Every input token, the cached ones included: `cache_read_tokens` and
    #: `cache_write_tokens` say how much of it the prompt cache served or stored.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    turns: int = 0
    duration_s: float = 0.0
    raw: str = ""
    #: What the worker's own block said: pass, fail, inconclusive, or "" for none.
    verdict: str = ""
    #: The tool calls the permission settings refused, from the CLI, not the worker.
    denied: tuple[str, ...] = ()
    #: The `--allow` rules that would let them through, from the full calls.
    allow: tuple[str, ...] = ()
    #: The model the CLI says did the work, e.g. `claude-sonnet-5-5`; "" when unknown.
    served_model: str = ""
    #: The permission mode the worker ran in, after any fallback.
    permission_mode: str = ""
    #: Facts about the project the worker says it confirmed: filed in its notes on a pass.
    learned: tuple[str, ...] = ()
    #: What the worker had to find before its first change, for the next one of its cell.
    orientation: tuple[str, ...] = ()
    #: The project notes of its brief the worker says it relied on: observed, never a verdict.
    notes_used: tuple[int, ...] = ()
    #: Rules a failing check of this task showed the work must meet (`notes.CONSTRAINT`).
    constraints: tuple[str, ...] = ()
    #: The one line the worker proposes as the commit's subject; "" when it gave none.
    commit: str = ""
    #: The report its task asked for (a `locks-report` block), parsed; None when there is none.
    report: dict[str, Any] | None = None
    #: What the worker says of each criterion `S#` of the task: id, met, evidence.
    criteria: tuple[dict[str, Any], ...] = ()
    #: The problems it reports, as records (`locks.findings`).
    findings: tuple[dict[str, str], ...] = ()
    #: Its answer for each ledger row it was given.
    verdicts: tuple[dict[str, str], ...] = ()
    #: A plan's parts (id, prompt, surfaces) and the contract they keep: on a pass of a plan asked for them.
    parts: tuple[dict[str, Any], ...] = ()
    contract: str = ""
    #: What made its answer unreadable, when only the answer's form failed (no single result
    #: block, a plan's parts that cannot run): its work may stand, and it is asked again.
    shape: str = ""
    #: Refused writes to files the attempt changed anyway, per git: no block.
    overcome: tuple[str, ...] = ()
    #: What locks' own check said when it failed on this attempt (`check_tail`): the failing
    #: tests by name and the last lines of its output; "" when the check passed or never ran.
    check_tail: str = ""
    #: The launch broke (a timeout, no JSON, an errored session), not the work:
    #: the one failure a retry can help.
    transient: bool = False
    #: What says the network dropped under it (`ENOTFOUND`, a connection error):
    #: the same cell again after a wait, its work kept, never a block at once.
    network: str = ""
    #: The Claude Code session the attempt ran in, from the CLI's JSON; "" when it gave none.
    session_id: str = ""
    #: The API turned the session away: `rate_limit` or `overloaded`, and the wait it named
    #: (seconds); "" and None otherwise (`locks.capacity` halves the machine's level on it).
    limited: str = ""
    retry_after_s: float | None = None
    #: The machine starved the worker: `memory` (its CLI was killed), `disk` (no room left)
    #: or `load` (its tests timed out on a machine busier than its CPUs).
    starved: str = ""
    #: The line where locks' check says its tests timed out, when timeouts are most of what
    #: failed it after a claimed pass (`check_timed_out`): the machine's load, not the code.
    load: str = ""
    #: The failing lines of locks' own check that turned a claimed pass down (`check_failures`):
    #: two attempts that left the same ones did not change what fails.
    failing: frozenset[str] = frozenset()
    #: Why that failure could not be told from the base's, when the same check at the base could
    #: not say what fails there (a timeout, an error): nothing shows the work caused it.
    base_unknown: str = ""
    #: It continued an earlier attempt's session instead of starting from its brief.
    resumed: bool = False
    #: Why a session it was asked to continue started fresh instead; "" when it did not.
    fresh: str = ""


#: Tools a one-shot worker never needs: the subagent tool's description lists every agent the
#: repository and the person define, read on every turn (`lean` setting).
LEAN_DENIED: tuple[str, ...] = ("Agent", "Task")
#: CLI flags that keep a worker's context to what it needs: no skill listing (its role's skills
#: are in its brief), and the per-machine sections after the part workers share, for the cache.
LEAN_FLAGS: tuple[str, ...] = ("--disable-slash-commands", "--exclude-dynamic-system-prompt-sections")


@functools.lru_cache(maxsize=8)
def _cli_help(claude_bin: str) -> str:
    try:
        return subprocess.run([claude_bin, "--help"], capture_output=True, text=True, timeout=20,
                              stdin=subprocess.DEVNULL, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def lean_flags(claude_bin: str, env: Mapping[str, str], *, help_text: Callable[[str], str] | None = None) -> tuple[
        str, ...]:
    """The lean flags the installed CLI lists in its `--help`, with the `lean` setting on; none
    when it is off or the CLI cannot say. A flag an older CLI does not know is never passed."""
    from locks import config

    if not config.enabled("lean", env):
        return ()
    said = (help_text or _cli_help)(claude_bin)
    return tuple(flag for flag in LEAN_FLAGS if flag in said)


def build_argv(spec: LaunchSpec, *, claude_bin: str = "claude", mcp_config_path: Path | None = None,
               prompt_path: Path | None = None, lean: Sequence[str] = ()) -> list[str]:
    argv = [
        claude_bin, "-p",
        "--model", spec.model_id or spec.cell.model,
        "--output-format", "json",
        "--max-turns", str(spec.max_turns),
        "--permission-mode", spec.permission_mode,
        "--strict-mcp-config",
    ]
    if spec.cell.effort:
        argv += ["--effort", spec.cell.effort]
    if spec.tools is not None:
        argv += ["--tools", ",".join(spec.tools)]
    denied = tuple(dict.fromkeys([*spec.disallowed_tools, *(LEAN_DENIED if lean else ())]))
    if denied:
        argv += ["--disallowedTools", ",".join(denied)]
    argv += list(lean)
    if spec.allowed_tools:
        argv += ["--allowedTools", *spec.allowed_tools]
    if spec.workdir is not None and spec.workdir != spec.launch_dir:
        argv += ["--add-dir", str(spec.workdir)]
    for extra in spec.extra_dirs:
        argv += ["--add-dir", str(extra)]
    if mcp_config_path is not None:
        argv += ["--mcp-config", str(mcp_config_path)]
    if prompt_path is not None:
        argv += ["--append-system-prompt-file", str(prompt_path)]
    elif spec.append_system_prompt:
        argv += ["--append-system-prompt", spec.append_system_prompt]
    if spec.resume_session:
        argv += ["--resume", spec.resume_session, *(["--fork-session"] if spec.fork_session else [])]
    if not spec.persist_session:
        argv.append("--no-session-persistence")
    return argv


class Stoppable:
    """A runner with `subprocess.run`'s contract whose worker a race can stop: the CLI runs
    in a process group of its own, so stopping it stops the servers it started too."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.stopped = False
        self._lock = threading.Lock()

    def __call__(self, argv: Sequence[str], *, input: str | None = None, cwd: str | None = None,
                 timeout: float | None = None, env: Mapping[str, str] | None = None,
                 **_: Any) -> subprocess.CompletedProcess[str]:
        with self._lock:
            if self.stopped:
                return subprocess.CompletedProcess(list(argv), -signal.SIGTERM, "", "stopped before it started")
            self.proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, cwd=cwd, env=dict(env) if env else None, text=True,
                                         start_new_session=True)
        try:
            out, err = self.proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            self.proc.communicate()
            raise
        return subprocess.CompletedProcess(list(argv), self.proc.returncode, out, err)

    def stop(self) -> None:
        with self._lock:
            self.stopped = True
            if self.proc is not None and self.proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.proc.pid, signal.SIGTERM)


def run(
    spec: LaunchSpec,
    *,
    runner: Runner = subprocess.run,
    claude_bin: str = "claude",
) -> WorkerResult:
    """Run one attempt. Raises only when the CLI itself cannot start."""
    mcp_path = None
    if spec.mcp_servers:
        # Outside the repository: a config file the worker could commit is a
        # config file that ends up in someone's history.
        fd, name = tempfile.mkstemp(prefix="locks-mcp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": dict(spec.mcp_servers)}, fh)
        mcp_path = Path(name)
    prompt_path = None
    if len(spec.append_system_prompt) > INLINE_PROMPT_CHARS:
        fd, name = tempfile.mkstemp(prefix="locks-role-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(spec.append_system_prompt)
        prompt_path = Path(name)
    argv = build_argv(spec, claude_bin=claude_bin, mcp_config_path=mcp_path, prompt_path=prompt_path,
                      lean=lean_flags(claude_bin, os.environ))
    before = tracked_state(spec.target_dir)
    started = time.monotonic()
    began = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        proc = runner(
            argv,
            input=(("\n\n".join(p for p in (spec.resume_prompt, spec.resume_note) if p) if spec.resume_session
                    else spec.prompt) + "\n\n" + instructions(spec)),
            cwd=str(spec.launch_dir),
            capture_output=True,
            text=True,
            timeout=spec.timeout_s,
            env=legacy.mirror({**worker_env(), **spec.env}),
            check=False,
        )
    except subprocess.TimeoutExpired:
        changed = changed_paths(before, tracked_state(spec.target_dir), spec.target_dir)
        if changed:
            # The work ran out of time, not the environment: it changed files, so
            # it goes on with more turns (and the time they bring), from what it did.
            return WorkerResult(
                False, Failure.TURNS_EXHAUSTED,
                f"the worker ran past its {spec.timeout_s}s time after changing {len(changed)} file(s), "
                "without a verdict", changed_paths=changed, duration_s=float(spec.timeout_s))
        return WorkerResult(
            False, Failure.ENVIRONMENT, f"the worker exceeded its {spec.timeout_s}s timeout",
            duration_s=float(spec.timeout_s), transient=True,
        )
    except FileNotFoundError as exc:
        raise LaunchError(f"{claude_bin!r} is not on PATH: a worker that cannot start is a "
                          "configuration problem, not a failed task") from exc
    finally:
        if mcp_path is not None:
            mcp_path.unlink(missing_ok=True)
        if prompt_path is not None:
            prompt_path.unlink(missing_ok=True)
    if _mode_refused(proc, spec):
        # This CLI does not take the mode (an older one, or auto mode not on for
        # the account): the attempt runs again, as it would have before.
        return run(dataclasses.replace(spec, permission_mode=grants.FALLBACK_MODE), runner=runner,
                   claude_bin=claude_bin)
    if spec.resume_session and not _json(proc.stdout) and (refused := _NO_SESSION.search(proc.stderr or "")):
        # The session is not there to continue: the attempt starts from its brief, as before, and says why.
        line = next((x.strip() for x in (proc.stderr or "").splitlines() if refused.group(0) in x), refused.group(0))
        fresh = run(dataclasses.replace(spec, resume_session=""), runner=runner, claude_bin=claude_bin)
        return dataclasses.replace(
            fresh, fresh=f"the CLI could not resume session {spec.resume_session}: {line[:200]}")
    changed = changed_paths(before, tracked_state(spec.target_dir), spec.target_dir)
    result = dataclasses.replace(parse(proc, spec, changed, duration_s=time.monotonic() - started),
                                 permission_mode=spec.permission_mode, resumed=bool(spec.resume_session))
    return counted_turns(result, began)


def counted_turns(result: WorkerResult, since: str, env: Mapping[str, str] | None = None) -> WorkerResult:
    """The attempt's turns as its worker's transcript counts them (each model call once, from
    `since`), when that is more than the CLI's `num_turns`: a session that compacted inside
    the worker counts its turns again from there, and a 48-minute attempt is never "2 turns"."""
    from locks import usage

    path = usage.transcript_of(result.session_id, env) if result.session_id else None
    counted = usage.transcript_turns(path, since=since) if path is not None else 0
    return dataclasses.replace(result, turns=counted) if counted > result.turns else result


def _mode_refused(proc: subprocess.CompletedProcess, spec: LaunchSpec) -> bool:
    if spec.permission_mode == grants.FALLBACK_MODE or proc.returncode == 0 or _json(proc.stdout):
        return False
    said = (proc.stderr or "").lower()
    return "permission-mode" in said or "permission mode" in said or "auto mode" in said


class LaunchError(Exception):
    """The CLI is missing or unusable. Fails loudly: escalating it would spend the
    whole ladder proving `claude` is still not installed."""


def parse(
    proc: subprocess.CompletedProcess,
    spec: LaunchSpec,
    changed: tuple[str, ...] = (),
    *,
    duration_s: float = 0.0,
) -> WorkerResult:
    envelope = _json(proc.stdout)
    usage = _usage(envelope)
    envelope, overcome = _overcome(envelope, changed, spec.target_dir)
    denied = denials(envelope)
    session = envelope.get("session_id") if envelope else None
    if session and session in {os.environ.get(name) for name in HOST_SESSION_VARS}:
        session = None  # the person's own session, never a worker's to continue
    common = dict(changed_paths=changed, duration_s=duration_s, denied=denied, allow=allow_rules(envelope),
                  overcome=overcome, session_id=session if isinstance(session, str) else "", **usage)
    if envelope is None:
        detail = (proc.stderr or "").strip()[:300]
        killed = proc.returncode in KILLED
        limited, wait = api_limit(proc.stderr or "")
        return WorkerResult(False, Failure.ENVIRONMENT,
                            f"the CLI produced no JSON (exit {proc.returncode}"
                            + (", killed: the kernel does so when memory runs out" if killed else "") + f"): {detail}",
                            transient=True, network=network_trouble(proc.stderr or "", proc.stdout or "", flaky=True),
                            limited=limited, retry_after_s=wait,
                            starved="memory" if killed else "disk" if NO_SPACE.search(proc.stderr or "") else "",
                            **common)
    text = str(envelope.get("result") or "")
    if envelope.get("is_error") and not _answered_anyway(envelope, text):
        subtype = str(envelope.get("subtype") or "")
        if "max_turns" in subtype:
            return WorkerResult(False, Failure.TURNS_EXHAUSTED,
                                f"used all {spec.max_turns} turns without a verdict", raw=text, **common)
        # An API error (a usage limit, an overload) arrives as `is_error` with
        # subtype "success": the reason is in the result text, never the subtype.
        why = text.strip()[:200] if subtype in ("", "success") and text.strip() else subtype or "no reason given"
        limited, wait = api_limit(text)
        return WorkerResult(False, Failure.ENVIRONMENT, f"the session errored: {why}", raw=text, transient=True,
                            network=network_trouble(text, flaky=True), limited=limited, retry_after_s=wait,
                            starved="disk" if NO_SPACE.search(text) else "", **common)
    block = result_block(text)
    if block is None and (earlier := answered_before_notices(common["session_id"])):
        text, block = earlier, result_block(earlier)
    if block is None:
        return WorkerResult(False, _refused(Failure.INCONCLUSIVE, denied),
                            "the worker did not answer with exactly one result block", raw=text,
                            shape="its answer did not hold exactly one result block", **common)
    verdict = str(block.get("verdict") or "").lower()
    summary = str(block.get("summary") or "")[:1000]
    evidence = str(block.get("evidence") or "")[:4000]
    common["learned"] = learned(block)
    common["orientation"] = orientation(block)
    common["notes_used"] = notes_used(block)
    common["constraints"] = learned({"learned": block.get("constraints")})
    common["commit"] = commit_subject(block)
    common["report"] = report_block(text)
    common["criteria"] = criteria_mod.read(block)
    common["findings"] = findings_mod.read(block)
    common["verdicts"] = ledger_mod.read(block)
    if verdict == "pass":
        if not evidence.strip():
            return WorkerResult(False, _refused(Failure.INCONCLUSIVE, denied),
                                "claimed a pass with no evidence: " + summary, raw=text, verdict=verdict, **common)
        left_out, not_met = criteria_mod.unmet(spec.criteria, common["criteria"])
        if not_met and not spec.judging:
            return WorkerResult(False, _refused(Failure.CODE_BUG, denied),
                                f"claimed a pass, but says {', '.join(not_met)} is not met: " + summary, evidence,
                                raw=text, verdict=verdict, **common)
        if left_out:
            return WorkerResult(False, _refused(Failure.INCONCLUSIVE, denied),
                                f"claimed a pass without saying whether {', '.join(left_out)} is met: " + summary,
                                evidence, raw=text, verdict=verdict, **common)
        undecided = ledger_mod.missing(spec.verdicts, spec.verdict_words, common["verdicts"])
        if undecided:
            return WorkerResult(False, _refused(Failure.INCONCLUSIVE, denied),
                                f"claimed a pass without a verdict on {', '.join(undecided)}: " + summary,
                                evidence, raw=text, verdict=verdict, **common)
        if spec.parts:
            parts, contract, problem = read_parts(block)
            if problem:
                return WorkerResult(False, _refused(Failure.INCONCLUSIVE, denied),
                                    f"claimed a pass, but its plan cannot run in parts: {problem}: " + summary,
                                    evidence, raw=text, verdict=verdict,
                                    shape=f"its plan cannot run in parts: {problem}", **common)
            common.update(parts=parts, contract=contract)
        return WorkerResult(True, None, summary, evidence, raw=text, verdict=verdict, **common)
    failure = _refused(_failure(block.get("failure"), verdict), denied)
    # The worker found the network down (a name that does not resolve): the
    # environment of a moment, not a wall to fix by hand. Its evidence is a
    # command's output, so the words a local server says when it is not up
    # (`fetch failed`, a connection error) are not read as the network.
    dropped = network_trouble(summary, evidence) if failure is Failure.ENVIRONMENT else ""
    return WorkerResult(False, failure, summary, evidence, raw=text, verdict=verdict, network=dropped, **common)


#: A check that could not reach the network itself: its package manager or git
#: says so on a line of its own. A log line a test prints is not this.
CHECK_OFFLINE = re.compile(
    r"^(?:npm (?:ERR!|error) (?:code (?:ENOTFOUND|EAI_AGAIN|ENETUNREACH)|network)\b.*|"
    r"(?:ERR_PNPM_\w+|error An unexpected error occurred).*\b(?:ENOTFOUND|EAI_AGAIN|getaddrinfo)\b.*|"
    r"fatal: unable to access .*(?:Could not resolve host|Failed to connect).*|"
    r"(?:WARNING: Retrying|ERROR: Could not install|ERROR: No matching distribution).*"
    r"(?:NewConnectionError|Temporary failure in name resolution|Name or service not known).*)$",
    re.MULTILINE)


def check_offline(output: str) -> str:
    """The line where a check says it could not reach the network, or ""."""
    found = CHECK_OFFLINE.search(output or "")
    return found.group(0).strip()[:120] if found else ""


#: A test runner's own line for a test that ran out of time: jest, vitest, mocha, pytest-timeout.
#: Only these lines count, never a word a test logs.
LOAD_TIMEOUT = re.compile(
    r"(?:\bExceeded timeout of \d+ ?ms for a (?:test|hook)\b|\bTimeout - Async callback was not invoked\b"
    r"|\bTest timed out in \d+ ?ms\b|\bHook timed out in \d+ ?ms\b|\bError: Timeout of \d+ ?ms exceeded\b"
    r"|^E?\s*Failed: Timeout >\s*[\d.]+s\b|^\+{5,} Timeout \+{5,}$).*$",
    re.MULTILINE)
#: A runner's line for one failed test: jest's `●` heading, vitest's `×`/`FAIL … >`, mocha's
#: numbered failure, pytest's `FAILED` summary line.
FAILED_TEST = re.compile(
    r"^\s*(?:● (?!Console\b).+|(?:×|✕) .+|FAIL .+ > .+|\d+\) .+|FAILED \S+.*)$", re.MULTILINE)
#: Share of the failed tests that must be timeouts for the failure to be the load's.
LOAD_SHARE = 2 / 3


def check_timed_out(output: str) -> str:
    """The first timeout line of a check whose failures are mostly tests that ran out of
    time (`LOAD_SHARE` of them; any when no failed test is listed), or "". On a loaded
    machine a suite that passes alone times out: that is the load, not the work."""
    timeouts = LOAD_TIMEOUT.findall(output or "")
    if not timeouts:
        return ""
    failed = len(FAILED_TEST.findall(output or ""))
    if failed and len(timeouts) < failed * LOAD_SHARE:
        return ""
    return LOAD_TIMEOUT.search(output).group(0).strip()[:120]


#: Escape sequences a runner colours its output with.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
#: How long something took, which differs between two runs of the same failure:
#: `(123 ms)`, `(0.01s)`, `[12ms]`, ` in 4.2s`, and a line's trailing `0.012s` or `3ms`.
_DURATION = re.compile(r"\s*[(\[]\d+(?:\.\d+)?\s*(?:ms|s|sec|m)[)\]]"
                       r"|\s+in\s+\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\b"
                       r"|(?<=\S)\s+\d+(?:\.\d+)?\s?(?:ms|s)$")
#: A word that names a file (`src/a.ts`, `app.py`), for another tool's `error` line.
_PATHISH = re.compile(r"[\w.@-]+/[\w./@-]+|\b[\w-]+\.(?:[jt]sx?|mjs|cjs|py|go|rs|rb|java|kt|vue|svelte|php|cs|"
                      r"css|scss|json|ya?ml)\b")
#: The lines a runner prints for one failure, each with how it is kept: a test file or a
#: test (jest, vitest, mocha, pytest, go test, deno), a type error (tsc), a coverage
#: threshold not met (jest, istanbul, nyc), a lint error (eslint). Counts and summaries
#: ("Tests: 2 failed") are no failure of their own: they change when one is fixed.
_FAILURE_SHAPES: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    # jest and vitest: `FAIL src/a.test.ts`, `FAIL src/a.test.ts > suite > test`; go: `FAIL pkg`.
    (re.compile(r"^FAIL\s+(\S.*)$"), lambda m: f"FAIL {m.group(1)}"),
    # go test: `--- FAIL: TestName (0.00s)`.
    (re.compile(r"^--- FAIL:\s+(\S+)"), lambda m: f"--- FAIL: {m.group(1)}"),
    # jest's failed test, vitest's: `✕ adds two`, `× adds two`.
    (re.compile(r"^(?:✕|×|✗)\s+(\S.*)$"), lambda m: f"✕ {m.group(1)}"),
    # jest's failure heading: `● suite › test`, `● Test suite failed to run` (never its console).
    (re.compile(r"^●\s+(?!Console\b)(\S.*)$"), lambda m: f"● {m.group(1)}"),
    # pytest's short summary: `FAILED tests/test_a.py::test_x - assert 1 == 2`, and its errors.
    (re.compile(r"^(FAILED|ERROR)\s+(\S+::\S+|\S+\.py)(?=\s|$)"), lambda m: f"{m.group(1)} {m.group(2)}"),
    # mocha's numbered failure: `1) suite adds two:` (the number moves when another is fixed).
    (re.compile(r"^\d+\)\s+(\S.*?):?$"), lambda m: f"mocha: {m.group(1)}"),
    # deno test: `adds two ... FAILED`, and the `name => ./a_test.ts:3:6` line under FAILURES.
    (re.compile(r"^(\S.*?) \.\.\. FAILED\b"), lambda m: f"{m.group(1)} ... FAILED"),
    (re.compile(r"^(\S.*?) => (\S+:\d+:\d+)$"), lambda m: f"{m.group(1)} => {m.group(2)}"),
    # tsc: `src/a.ts(12,5): error TS2322: ...` and `src/a.ts:12:5 - error TS2322: ...`.
    (re.compile(r"^(\S+?)(?:\((\d+),(\d+)\)|:(\d+):(\d+))\s*[:-]\s*error (TS\d+): (.*)$"),
     lambda m: f"{m.group(1)}:{m.group(2) or m.group(4)}:{m.group(3) or m.group(5)} error {m.group(6)}: "
               f"{m.group(7)}"),
    # jest: `Jest: "src/app/x.ts" coverage threshold for lines (80%) not met: 61.5%`.
    (re.compile(r"^(.*\bcoverage threshold for .*\bnot met\b.*)$", re.IGNORECASE), lambda m: m.group(1)),
    # istanbul / nyc: `ERROR: Coverage for lines (61.5%) does not meet global threshold (80%)`.
    (re.compile(r"^(?:ERROR:\s*)?(Coverage for .*\bdoes not meet\b.*threshold.*)$", re.IGNORECASE),
     lambda m: m.group(1)),
    # eslint's unix format: `src/a.ts:12:5: 'x' is unused [Error/no-unused-vars]`.
    (re.compile(r"^(\S+:\d+:\d+):?\s+(.*\b[Ee]rror\b.*)$"), lambda m: f"{m.group(1)} {m.group(2)}"),
    # eslint's compact format: `src/a.ts: line 12, col 5, Error - 'x' is unused (no-unused-vars)`.
    (re.compile(r"^(\S+): line (\d+), col (\d+), Error - (.*)$"),
     lambda m: f"{m.group(1)}:{m.group(2)}:{m.group(3)} error {m.group(4)}"),
)
#: eslint's stylish format names the file on a line of its own and each error under it.
_STYLISH_FILE = re.compile(r"^(?:/|\./|[\w@.-]+/)\S*\.\w+$")
_STYLISH_ERROR = re.compile(r"^(\d+:\d+)\s+error\s+(.*)$")
#: deno lint: `error[no-unused-vars]: ...`, its file on the `--> path:1:7` line after.
_DENO_LINT = re.compile(r"^error\[([\w-]+)\]:\s*(.*)$")
_DENO_AT = re.compile(r"^-->\s*(\S+:\d+:\d+)$")
#: Any other `error` line that names a file: a linter's or a compiler's. Never a package
#: manager's own (`error Command failed with exit code 1`).
_ERROR_LINE = re.compile(r"^(?:ERROR|error)\b[:\s]")
_MANAGER = re.compile(r"^error Command failed\b|^error An unexpected error\b", re.IGNORECASE)
#: The most failing lines kept from one output, and how long each may be.
FAILURES_KEPT, FAILURE_CHARS = 500, 300


def check_failures(output: str, roots: Sequence[str | Path] = ()) -> frozenset[str]:
    """The lines of a check's output that each report one failure, normalized so two runs
    of the same failure read the same: colour codes and durations dropped, the directories
    the check ran in (`roots`: a worktree, the base's copy) taken off the paths, whitespace
    collapsed. Empty when no line reads as a failure: the output cannot tell what failed."""
    text = _ANSI.sub("", output or "")
    for root in sorted({str(r).rstrip("/") for r in roots if str(r).strip("/")}, key=len, reverse=True):
        text = text.replace(root + "/", "").replace(root, ".")
    found: set[str] = set()
    current_file = pending_lint = ""
    for raw in text.splitlines():
        line = " ".join(_DURATION.sub("", raw).split())
        if not line:
            current_file = ""
            continue
        if pending_lint and (at := _DENO_AT.match(line)):
            found.add(f"{at.group(1)} {pending_lint}")
            pending_lint = ""
            continue
        if deno := _DENO_LINT.match(line):
            pending_lint = f"error[{deno.group(1)}]: {deno.group(2)}"
            continue
        if _STYLISH_FILE.match(line):
            current_file = line
            continue
        if current_file and (stylish := _STYLISH_ERROR.match(line)):
            found.add(f"{current_file}:{stylish.group(1)} error {stylish.group(2)}")
            continue
        for shape, keep in _FAILURE_SHAPES:
            if hit := shape.match(line):
                found.add(keep(hit))
                break
        else:
            if _ERROR_LINE.match(line) and not _MANAGER.match(line) and _PATHISH.search(line):
                found.add(line)
        if len(found) >= FAILURES_KEPT:
            break
    return frozenset(clip(" ".join(f.split()), FAILURE_CHARS) for f in found)


#: What an attempt keeps of its check's own output (`check_tail`): the failing lines, the last
#: lines, and locks' comparison with the base. About this many characters in all.
CHECK_TAIL_CHARS = 3500
_TAIL_LINES_CHARS, _TAIL_FAILING, _TAIL_FAILING_CHARS, _TAIL_NOTE_CHARS = 1400, 8, 140, 600


def check_tail(output: str, roots: Sequence[str | Path] = (), note: str = "") -> str:
    """What a failed check said, for whoever reads the attempt next: the failing tests and
    files by name, then the last lines of the output (where a runner prints its assertions),
    then `note` (locks' own comparison with the base). Colour codes and progress redraws
    are dropped, the directories the check ran in (`roots`) are taken off the paths, secrets
    are masked and `<private>` text left out, as a note is. The failing names come first
    only when the tail cannot hold the whole output. Empty when the check said nothing."""
    text = _ANSI.sub("", output or "")
    for root in sorted({str(r).rstrip("/") for r in roots if str(r).strip("/")}, key=len, reverse=True):
        text = text.replace(root + "/", "").replace(root, ".")
    text = _SECRET.sub("[secret]", _PRIVATE.sub(" ", text))
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):  # not splitlines(): it splits on "\r" too
        line = raw.rsplit("\r", 1)[-1].rstrip()  # a progress bar redrawn in place: its last state
        if line or (lines and lines[-1]):
            lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return ""
    kept: list[str] = []
    room = _TAIL_LINES_CHARS
    for line in reversed(lines):
        if len(line) + 1 > room:
            if not kept:  # the last line alone is longer than the tail: its end, clipped
                kept.append(clip(line, room - 1))
            break
        kept.append(line)
        room -= len(line) + 1
    kept.reverse()
    parts: list[str] = []
    cut = len(kept) < len(lines)
    if cut and (failing := sorted(check_failures("\n".join(lines)))):
        more = len(failing) - _TAIL_FAILING
        parts.append(f"failing ({len(failing)}):\n" + "\n".join(
            f"  {clip(f, _TAIL_FAILING_CHARS, one_line=True)}" for f in failing[:_TAIL_FAILING])
            + (f"\n  and {more} more" if more > 0 else ""))
    parts.append(("last lines of the output:\n…\n" if cut else "") + "\n".join(kept))
    if note.strip():
        parts.append(clip(_SECRET.sub("[secret]", note.strip()), _TAIL_NOTE_CHARS))
    return "\n\n".join(parts)


def preexisting(work: frozenset[str], base: frozenset[str] | None, *, must_fix: bool = False) -> bool:
    """Whether a red check's failures were all there before the work: the base's check failed
    too (`base` is its failing lines, None when it passed or could not tell), the work's run
    names at least one failing line, and every one is among the base's: the work fixed some,
    added none. Work whose job is to make a failure go away (`must_fix`) must also have fixed
    at least one of them: the same failures, untouched, are not its pass."""
    if not work or not base or not work <= base:
        return False
    return not must_fix or work < base


def api_limit(text: str) -> tuple[str, float | None]:
    """What a session's own failed call says of the API turning it away: (`usage` for the
    account's own limit, `rate_limit` or `overloaded`, the seconds it said to wait, or until
    the limit resets), or ("", None)."""
    if USAGE_LIMIT.search(text or ""):
        # The account's limit: nothing passes it before its reset, which the message names.
        return "usage", resets_in(text)
    if OVERLOADED.search(text or ""):
        kind = "overloaded"
    elif RATE_LIMITED.search(text or ""):
        kind = "rate_limit"
    else:
        return "", None
    if found := _RETRY_AFTER.search(text):
        return kind, float(found.group(1))
    if found := _RESETS_AT.search(text):
        return kind, max(float(found.group(1)) - time.time(), 0.0)
    return kind, None


def network_trouble(*texts: str, flaky: bool = False) -> str:
    """What in `texts` says the network dropped, or "". `flaky` also reads what
    only a session's own failed call says (a reset, an overload)."""
    for text in texts:
        found = NETWORK_DOWN.search(text or "") or (NETWORK_FLAKY.search(text or "") if flaky else None)
        if found:
            return found.group(0).strip()[:80]
    return ""


def _answered_anyway(envelope: Mapping[str, Any], text: str) -> bool:
    """A session the CLI flags `is_error` with subtype "success" whose result still holds the one
    result block, and no API limit or dropped network: the worker answered, and something after its
    answer (a hook, the last call's bookkeeping) set the flag. Its answer is read as any is; an
    errored session with no block, or one that names a limit, stays errored."""
    if str(envelope.get("subtype") or "") != "success" or result_block(text) is None:
        return False
    return not api_limit(text)[0] and not network_trouble(text, flaky=True)


def read_parts(block: Mapping[str, Any]) -> tuple[tuple[dict[str, Any], ...], str, str]:
    """A plan's parts and their contract, read strictly: (parts, contract, problem). A
    problem (no parts, more than `MAX_PARTS`, a part without its id, prompt or surfaces,
    surfaces two parts share, an `after` that names no earlier part) leaves nothing read: a
    plan is never guessed at. Only what
    `PARTS_INSTRUCTIONS` tells the plan is checked; length never is."""
    raw = block.get("parts")
    if not isinstance(raw, list) or not raw:
        return (), "", "it gave no parts"
    if len(raw) > MAX_PARTS:
        return (), "", f"it gave {len(raw)} parts; a plan splits into at most {MAX_PARTS}"
    parts: list[dict[str, Any]] = []
    for n, part in enumerate(raw, 1):
        if not isinstance(part, dict):
            return (), "", f"part {n} is not an object"
        pid = part.get("id")
        if not isinstance(pid, str) or not PART_ID.match(pid):
            return (), "", f"part {n} has no id (lowercase letters, digits, '-', '_')"
        if any(p["id"] == pid for p in parts):
            return (), "", f"two parts are called {pid!r}"
        prompt = part.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return (), "", f"part {pid!r} has no prompt"
        given = part.get("surfaces") or []
        try:
            if not isinstance(given, list):
                raise surfaces.SurfaceError("its surfaces are a list of paths or globs")
            bound = list(surfaces.check(given))
        except surfaces.SurfaceError as exc:
            return (), "", f"part {pid!r}: {exc}"
        if len(raw) > 1 and not bound:
            return (), "", f"part {pid!r} declares no surfaces: the parts run side by side only within their own"
        before = part.get("after")
        if before is not None and (not isinstance(before, str) or before not in {p["id"] for p in parts}):
            return (), "", (f"part {pid!r}: its after names {before!r}, which is no part listed before it "
                            "(`after` is the id of one earlier part)")
        parts.append({"id": pid, "prompt": prompt.strip(), "surfaces": bound, **({"after": before} if before else {})})
    contract = block.get("contract")
    if len(parts) > 1 and (not isinstance(contract, str) or not contract.strip()):
        return (), "", "it gave no contract: the names, types and interfaces its parts share"
    overlaps = surfaces.overlapping({p["id"]: p["surfaces"] for p in parts})
    if overlaps:
        return (), "", "its parts' surfaces overlap: " + "; ".join(overlaps)
    return tuple(parts), contract.strip() if isinstance(contract, str) else "", ""


#: How many facts one attempt may add to the notes, and how long each may be.
MAX_LEARNED, LEARNED_CHARS = 5, 600


def learned(block: Mapping[str, Any]) -> tuple[str, ...]:
    """The facts a result block says the worker learned: strings only, a few, short."""
    raw = block.get("learned")
    if not isinstance(raw, list):
        return ()
    found = [" ".join(x.split())[:LEARNED_CHARS] for x in raw if isinstance(x, str) and x.strip()]
    return tuple(dict.fromkeys(found))[:MAX_LEARNED]


#: How many orientation lines one attempt may leave for the next worker, and how long each may be.
MAX_ORIENTATION, ORIENTATION_CHARS = 6, 300


def orientation(block: Mapping[str, Any]) -> tuple[str, ...]:
    """What a result block says the worker had to find before its first change: strings
    only, a few, short."""
    raw = block.get("orientation")
    if not isinstance(raw, list):
        return ()
    found = [" ".join(x.split())[:ORIENTATION_CHARS] for x in raw if isinstance(x, str) and x.strip()]
    return tuple(dict.fromkeys(found))[:MAX_ORIENTATION]


def notes_used(block: Mapping[str, Any]) -> tuple[int, ...]:
    """The note numbers a result block says the worker relied on: whole numbers, `12` or `"#12"`."""
    raw = block.get("notes_used")
    if not isinstance(raw, list):
        return ()
    found = []
    for item in raw:
        text = str(item).strip().lstrip("#") if isinstance(item, int | str) and not isinstance(item, bool) else ""
        if text.isdigit():
            found.append(int(text))
    return tuple(dict.fromkeys(found))[:50]


#: The longest commit subject kept; a longer one is cut at a word.
SUBJECT_CHARS = 72


def commit_subject(block: Mapping[str, Any]) -> str:
    """The commit subject a result block proposes: one line, short, no trailing dot; "" for none."""
    raw = block.get("commit")
    if not isinstance(raw, str):
        return ""
    line = " ".join(raw.strip().splitlines()[0].split()) if raw.strip() else ""
    line = line.strip("`\"' ").rstrip(".")
    if len(line) > SUBJECT_CHARS:
        line = line[:SUBJECT_CHARS - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return line


def _refused(failure: Failure, denied: Sequence[str]) -> Failure:
    """A refusal decides where an unfinished attempt stands, unless the worker found
    the task itself wrong: no model gets past a setting, and a stronger one sent
    into the same refusal only costs more."""
    return Failure.PERMISSION if denied and failure not in REPLAN else failure


#: The tools that write a file: a refusal of one is overcome when git shows the file written.
_WRITES = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def _overcome(envelope: dict[str, Any] | None, changed: Sequence[str],
              workdir: Path) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """The envelope without the refused writes the attempt got past: a write to a
    file in its own work directory that git shows changed after all (a retry, an
    Edit after a refused Write). Such a refusal stopped nothing, and blocking on
    it asks a person to allow what is done. The refusals dropped, as `Tool(path)`."""
    entries = (envelope or {}).get("permission_denials") or ()
    if not entries or not changed:
        return envelope, ()
    done = set(changed)
    root = workdir.resolve()
    kept, dropped = [], []
    for entry in entries:
        given = entry.get("tool_input") if isinstance(entry, dict) else None
        path = str((given or {}).get("file_path") or (given or {}).get("notebook_path") or "")
        if isinstance(entry, dict) and entry.get("tool_name") in _WRITES and path:
            full = (root / path).resolve() if not path.startswith("/") else Path(path).resolve()
            if full.is_relative_to(root) and full.relative_to(root).as_posix() in done:
                dropped.append(f"{entry['tool_name']}({full.relative_to(root).as_posix()})")
                continue
        kept.append(entry)
    if not dropped:
        return envelope, ()
    return {**(envelope or {}), "permission_denials": kept}, tuple(dict.fromkeys(dropped))


def allow_rules(envelope: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The rules a person would pass to let every refused call through: one per
    program, read from the calls in full (see `locks.allow`)."""
    found: list[str] = []
    for entry in (envelope or {}).get("permission_denials") or ():
        if isinstance(entry, dict) and entry.get("tool_name"):
            found += allow.rules(str(entry["tool_name"]), entry.get("tool_input"))
    return tuple(dict.fromkeys(found))


def denials(envelope: Mapping[str, Any] | None) -> tuple[str, ...]:
    """What the permission settings refused, as the rules a person would allow:
    `Bash(npm run build)`, `Write(src/app.ts)`, or the bare tool name."""
    found: list[str] = []
    for entry in (envelope or {}).get("permission_denials") or ():
        if not isinstance(entry, dict) or not entry.get("tool_name"):
            continue
        name = str(entry["tool_name"])
        given = entry.get("tool_input") if isinstance(entry.get("tool_input"), dict) else {}
        target = given.get("command") or given.get("file_path") or given.get("url") or ""
        target = " ".join(str(target).split())
        rule = f"{name}({target[:117] + '...' if len(target) > 120 else target})" if target else name
        if rule not in found:
            found.append(rule)
    return tuple(found)


def _decoded(text: str, start: int, *, closed: bool = False) -> Any:
    """The JSON value that begins at `start` (after whitespace), or None. `closed`
    asks that nothing but the closing fence (or the end) follows it: a second value,
    or anything else, in the block makes it no value at all."""
    rest = text[start:]
    try:
        value, end = _DECODER.raw_decode(rest, len(rest) - len(rest.lstrip()))
    except ValueError:
        return None
    tail = rest[end:].lstrip()
    if closed and tail and not tail.startswith("```"):
        return None
    return value


def report_block(text: str) -> dict[str, Any] | None:
    """The last `locks-report` block: its JSON object, or its text when it is not one; None without one."""
    text = text or ""
    opened = list(_REPORT_OPEN_RE.finditer(text))
    if not opened:
        return None
    value = _decoded(text, opened[-1].end())
    if isinstance(value, dict):
        return value
    found = _REPORT_RE.findall(text[opened[-1].start():])
    body = (found[0] if found else text[opened[-1].end():])[:REPORT_CHARS]
    return {"unparsed": body}


#: What Claude Code puts in a session when a background command or monitor it started ends.
_NOTICE = "<task-notification>"


def answered_before_notices(session_id: str, env: Mapping[str, str] | None = None) -> str | None:
    """The worker's answer that held its one result block, when every turn after it only
    answered background notices: a monitor or a background command ended after the answer,
    and the worker's reply to that (often "the result stands") became the session's last
    message. None when a turn after it was opened by anything else (a prompt), when no turn
    held a readable block, or when the transcript is not there."""
    from locks import usage

    path = usage.transcript_of(session_id, env) if session_id else None
    if path is None:
        return None
    answers: list[tuple[str, bool]] = []  # (the turn's last text, whether a notice opened it)
    opened_by_notice, last = False, ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        content = (entry.get("message") or {}).get("content")
        if entry.get("type") == "user":
            parts = [content] if isinstance(content, str) else [
                c.get("text", "") for c in content or () if isinstance(c, dict) and c.get("type") == "text"]
            if not any(parts) or any(isinstance(c, dict) and c.get("type") == "tool_result" for c in
                                     (content if isinstance(content, list) else ())):
                continue  # a tool's result is no new turn
            answers.append((last, opened_by_notice))
            opened_by_notice, last = any(_NOTICE in part for part in parts), ""
        elif entry.get("type") == "assistant":
            for c in content or ():
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                    last = c["text"]
    answers.append((last, opened_by_notice))
    # From the last turn back: the newest block wins, as long as only notices opened the turns after it.
    for text, by_notice in reversed(answers):
        if result_block(text) is not None:
            return text
        if not by_notice:
            return None
    return None


def result_block(text: str) -> dict[str, Any] | None:
    """The one result block, or None — two verdicts are no verdict. A worker that repeated
    the very same block (the same JSON, every time) gave one verdict, and it is read; blocks
    that differ in anything, or one that cannot be read, are none (fail closed)."""
    opened = list(_OPEN_RE.finditer(text or ""))
    if not opened:
        return None
    values = [_decoded(text, at.end(), closed=True) for at in opened]
    first = values[0]
    if not isinstance(first, dict) or any(value != first for value in values[1:]):
        return None
    return first


#: Where a failure lives is worth reading even from an `inconclusive` verdict: a
#: worker that found the task contradictory often says "I could not finish"
#: rather than "I failed", and the place it names is still the place.
_DECISIVE = frozenset({Failure.SPEC_BUG, Failure.ARCHITECTURE_BUG, Failure.ENVIRONMENT})


def _failure(raw: Any, verdict: str) -> Failure:
    try:
        named = Failure(str(raw))
    except ValueError:
        named = None
    if verdict != "fail":
        return named if named in _DECISIVE else Failure.INCONCLUSIVE
    # A failure that says nothing about where it lives is treated as shallow:
    # the cheapest move that still changes something.
    return named or Failure.CODE_BUG


def _json(stdout: str | None) -> dict[str, Any] | None:
    try:
        value = json.loads(stdout or "")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _usage(envelope: Mapping[str, Any] | None) -> dict[str, Any]:
    if not envelope:
        return {}
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    cost = envelope.get("total_cost_usd")
    return {
        "cost_usd": float(cost) if isinstance(cost, (int, float)) else 0.0,
        "input_tokens": _int(usage.get("input_tokens")) + _int(usage.get("cache_read_input_tokens"))
        + _int(usage.get("cache_creation_input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")),
        "cache_read_tokens": _int(usage.get("cache_read_input_tokens")),
        "cache_write_tokens": _int(usage.get("cache_creation_input_tokens")),
        "turns": _int(envelope.get("num_turns")),
        "served_model": models.served(envelope),
    }


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


#: Where a host names the person's session; a worker inherits none of them.
HOST_SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID")


def worker_env() -> dict[str, str]:
    env = dict(os.environ)
    env["LOCKS_HOOKS_OFF"] = "1"
    env.pop("LOCKS_SESSION_ID", None)  # the worker's work is the task's, not that session's turn
    # A host that runs the person's session (the app's cloud sessions) names it in the
    # environment; a worker that inherits it writes its transcript under that session's
    # id, and every attempt reads it back as its own session.
    # The cloud host names it twice: a worker that keeps either one reports the host's id.
    for name in HOST_SESSION_VARS:
        env.pop(name, None)
    # A fork inside a worker re-reads its whole context outside locks' account: off,
    # whatever the person's own sessions set.
    env["CLAUDE_CODE_FORK_SUBAGENT"] = "0"
    return env


# --- what changed ---------------------------------------------------------


#: How deep below the work directory a nested git checkout is looked for, and how
#: many are read: a folder holding a few repositories, not a whole disk.
NESTED_DEPTH, NESTED_MAX = 2, 24
_SKIP_DIRS = frozenset({"node_modules", ".venv", "venv", ".git", "dist", "build", "target", "__pycache__"})


def tracked_state(directory: Path) -> dict[str, str] | None:
    """`git status` of a directory, path -> status code, and of every git checkout
    nested in it, their paths prefixed. A folder that holds several repositories
    is no checkout itself, and a checkout's status does not look inside a nested
    one: without these, work done in them reads as "changed nothing".
    None when neither the directory nor anything in it is under git."""
    own = _status(directory)
    nested = nested_checkouts(directory)
    if own is None and not nested:
        return None
    state = dict(own or {})
    for sub in nested:
        found = _status(sub)
        if found is None:
            continue
        rel = sub.relative_to(directory).as_posix()
        for path, code in found.items():
            state[f"\0HEAD:{rel}" if path == "\0HEAD" else f"{rel}/{path}"] = code
    # The parent's own status lists a nested checkout as one untracked folder.
    for sub in nested:
        state.pop(sub.relative_to(directory).as_posix() + "/", None)
    return state


def _status(directory: Path) -> dict[str, str] | None:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "-uall", "-z"],
            cwd=str(directory), capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    state: dict[str, str] = {}
    for entry in proc.stdout.split("\0"):
        if len(entry) > 3:
            state[entry[3:]] = entry[:2]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(directory),
                          capture_output=True, text=True, check=False)
    state["\0HEAD"] = head.stdout.strip()
    return state


def _linked_members(directory: Path) -> set[Path]:
    """The members of the workspace `directory` is the folder of that are linked
    into it: its checkouts as much as the ones in it. Other links are not followed."""
    from locks import workspaces

    folder = workspaces.of(directory)
    if folder is None or folder != directory.resolve():
        return set()
    entry = next((w for w in workspaces.declared() if Path(w["folder"]) == folder), None)
    return {directory / m["name"] for m in (entry or {}).get("members") or ()}


def nested_checkouts(directory: Path) -> list[Path]:
    """Git checkouts below `directory`, at most `NESTED_DEPTH` levels down (a
    workspace's members linked into its folder included)."""
    found: list[Path] = []
    linked = _linked_members(directory)
    level = [directory]
    for _ in range(NESTED_DEPTH):
        below: list[Path] = []
        for parent in level:
            try:
                children = sorted(p for p in parent.iterdir() if p.is_dir() and (not p.is_symlink() or p in linked))
            except OSError:
                continue
            for child in children:
                if child.name in _SKIP_DIRS or child.name.startswith("."):
                    continue
                if (child / ".git").exists():
                    found.append(child)
                    if len(found) >= NESTED_MAX:
                        return found
                else:
                    below.append(child)
        level = below
    return found


def changed_paths(
    before: dict[str, str] | None, after: dict[str, str] | None, directory: Path | None = None
) -> tuple[str, ...]:
    if before is None or after is None:
        return ()
    changed = {p for p in set(before) | set(after)
               if before.get(p) != after.get(p) and not p.startswith("\0HEAD") and not is_artifact(p)}
    if directory is not None:
        for key in {k for k in set(before) | set(after) if k.startswith("\0HEAD")}:
            old, new = before.get(key), after.get(key)
            if not (old and new and old != new):
                continue
            # The worker committed: what it committed is clean in `status` and would
            # otherwise read as "changed nothing".
            rel = key.removeprefix("\0HEAD").removeprefix(":")
            diff = subprocess.run(["git", "diff", "--name-only", old, new], cwd=str(directory / rel),
                                  capture_output=True, text=True, check=False)
            changed |= {f"{rel}/{line}" if rel else line for line in diff.stdout.splitlines()
                        if line and not is_artifact(line)}
    return tuple(sorted(changed))


def describe(argv: Sequence[str]) -> str:
    """The command line with the prompt-sized arguments shortened, for logs."""
    shown = []
    for arg in argv:
        shown.append(arg if len(arg) <= 80 else arg[:77] + "...")
    return " ".join(shown)
