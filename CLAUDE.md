# locks — project instructions

locks is the core that routes Claude Code work: classify, choose a model ×
effort cell, run a one-shot worker with exactly the tools and capabilities its
task needs, check the result, escalate on evidence, remember what failed. It is
installed as a plugin and its guidance runs in other people's repositories —
treat `commands/` and every prompt string in `src/` with the same care as code.

## Layout

| Path | What lives there |
|---|---|
| `src/locks/matrix.py` | models, the five efforts and their purpose, the ladder per kind, and the probation cell below a writing kind's ladder (`PROBATION`, `reach`); a model's default effort reads cells recorded before it had the dial |
| `src/locks/trust.py` | earned trust, read from the independent check's record per repository, kind and worker cell: skip-lot sampling of the verifier (`sampling`), the probation trial below a ladder (`probation_arm`), and what the record says of learning (`report`, `locks trust`, `locks stats`); no model |
| `src/locks/evidence.py` | what a neighbour a worker was handed must show before its pass counts (layout-inspector on a UI change), read from the tool-call log: a pass that owes it goes back once, then to the orchestrator; tried in arms per repository; every handed capability's calls counted per attempt; no model |
| `src/locks/classify.py` | rules first, Haiku for the rest; `frontier` only by tag |
| `src/locks/escalate.py` | failure kind → move (retry, more effort, next model, more turns, replan) |
| `src/locks/launch.py` | one attempt as `claude -p`: argv, the result contract, parsing, git-read changes |
| `src/locks/capabilities.py` | which MCP servers a worker gets, from the user-level registry |
| `src/locks/mcp_guard.py` | a JSON-RPC guard between a worker and an MCP server: the calls a policy refuses never reach the server, a line it cannot parse or whose keys collide by case is answered and never forwarded, and what it forwards is rebuilt from the protocol's keys; stdlib, imports nothing of locks |
| `src/locks/masking.py` | the one pattern locks masks secrets with: `SECRET` for the trace and kept transcripts (git commit ids stay), `SECRET_STRICT` for the notes (long hex runs too); linear on any input; stdlib, imports nothing of locks |
| `src/locks/plugins.py` | neighbours the person installed as Claude Code plugins (layout-inspector, rastro), read from the plugin cache or PATH and handed over as capabilities with their skill: layout-inspector measures rendered UI, rastro only browses (handed when the request asks to browse or the task has a service, `on_request`); `versions` is the one reader of the cache (folders only, never a `temp_*` clone `/plugin` left half done) |
| `src/locks/manifests.py` | what a neighbour says of itself: the `locks` block of its newest installed plugin manifest (or `locks.json` beside it), read at plan time and cached per file state; words only (hint, hints per kind, tool descriptions, what tools measure), ignored whole when malformed (`docs/neighbours.md`) |
| `src/locks/roles.py` | what a worker is: role files (bundled `roles/`, the person's in their locks home), `extends` and `match`, skills found by name and put into the worker's system prompt; a bundled skill with an upstream plugin (`UPSTREAM`) read from that installed plugin, the vendored copy the fallback |
| `src/locks/teams.py` | the person's teams of roles and which one each project uses (kept in their locks home); a project's team bounds the kinds it is classified into; `team_cli.py` is `locks roles …` and `locks team …` |
| `src/locks/survey.py` | what this machine says about how the person works (`locks survey`): every Claude Code session it kept (requests, shapes, friction, errors and the call that passed after them) and each repository's commit history; read only, no model |
| `src/locks/investigate.py` | `locks investigate`: one read-only worker per project (and per plugin to take on), each in its own lane, wearing the internal `surveyor` role, briefed with the deep survey; their `locks-report` blocks kept on the tasks, nothing filed |
| `src/locks/insights.py` | what the person's projects say about the teams they need (`locks team insights`): kinds asked and how each went, languages, checks, the team each uses; read only, no model |
| `src/locks/criteria.py` | a request's listed items as criteria `S1..Sn`, verbatim; a pass answers each |
| `src/locks/findings.py` | findings as records: where, what, `evidence_class`, `cause`; which block a change |
| `src/locks/ledger.py` | a review's findings as one frozen ledger `F1..Fn`: who decides each row (refuter, fix, re-check), two rounds, then the person |
| `src/locks/specs.py` | whether a repository's OpenSpec specs keep up with its code: days and code commits since `openspec/` changed, the writing brief's line and the ending's offer, the files of a change that owe a spec, and livespec's `validate_openspec` for the specs' structure (`locks specs`, on a temporary copy of `openspec/`); git and livespec, no model |
| `src/locks/tracer.py` | the TRACER method of a repository that keeps one (`docs/tracer/`): the method's files in a writing brief, and the CORRECT lines of the flows a request names, the anti-pattern catalogue and, with a measuring neighbour, each floor and anti-pattern beside the tool that measures it in the independent check's; read only |
| `src/locks/risk.py` | a change's tier (passive, medium, high) from its request, kind and files; only raised; no model |
| `src/locks/points.py` | a task's size on the Fibonacci scale, no model: the estimate when it is planned (criteria, kind, risk and its areas, files named, livespec's reach, length) and the realized size when it ends (files, lines, turns, wall time, attempts) |
| `src/locks/standards.py` | where a repository's tasks of each kind and size start, as measured: a standard per points band, the calibration's two rules, each change's verdict and its revert; recorded, never edited |
| `src/locks/evolution.py` | the evolution history: a snapshot a day per repository and kind, the report (`locks evolution`, the UI's Evolution), and `settle`, which the hooks and the dispatcher run after work ends |
| `src/locks/surfaces.py` | where a task may write (`--surface`, a step's `surfaces`): checked after each attempt, and whether two tasks can never touch the same file |
| `src/locks/phase.py` | what a running worker is doing (exploring, implementing, checking), read from the tail of the tool-call log |
| `src/locks/progress.py` | how far a running task got: time against its kind's usual here, its stage (worker, locks' check, the independent check), what its worker wrote so far (its worktree's git: counts, names, `peek`'s diff, `texts` before and after), the request's items as its subtasks, a worker gone quiet; read only, no model |
| `src/locks/roles_internal/` | roles locks runs itself and no team holds: the independent `verifier`, and the `surveyor` that investigates for `/locks:init` |
| `skills/insight/` | `/locks:insight`: what locks claims against what its trace, analysis, evolution, trust and doctor show it did; read only, proposals for the person |
| `skills/init/` | the plugin's one skill (`/locks:init`): scope and an estimate of its cost agreed with the person, `locks investigate` digs, and from the reports one team for every flow, one workflow per flow, and the memory of what was solved, through locks' own commands only |
| `docs/images/`, `scripts/demo.py` | the README's screenshots of `locks ui`, taken on demo data with generic project names: `scripts/demo.py DIR --shots` writes it with locks' own commands and the scenarios' fake `claude` (no money), spreads times and costs to a real worker's size, and takes them with Playwright |
| `src/locks/roles/`, `src/locks/skills/` | the bundled team and the skills it carries; every vendored skill keeps its license, and `skills/README.md` credits it and lists what locks changed |
| `src/locks/adapters/` | neighbours the core *adopts*: read their data, brief, route and assess with it. `livespec.py` is the first; `engram.py` reads the team's engram memory over its server and writes to it only opt-in |
| `src/locks/config.py` | locks' settings (`livespec`, `parallel`, `autowork`, `names`, `notes`, `scribe`, `prune`, `plugins`, `verifier`, `trust`, `probation`, `evidence`, `race`, `route`, `primer`, `trunk`, `overlap`, `late_verifier`, `specs`, `tracer`, `habit_review`, `habit_auto`, `role_trials`, `note_trials`, `recall_fusion`, `methods`, `dossier_trial`, `predict`, `critique`, `engram`, `engram_write`, `delegate`, `trace`, `notes_fork`, all on/off; `compact`, a word; `max_parallel`, `auto` or a number; `livespec_wait`, seconds, 0 never waits), copied from plugin options by SessionStart |
| `src/locks/ci.py` | what a repository's own checks run, read from its files: the scripts a command names (package.json, deno.json), whether its tests drive a headless browser, and its CI's jobs (`.github/workflows/*.yml`, a line reader, no YAML dependency) with the services and env each sets, and the jobs a person quieted per repository |
| `src/locks/orchestrate.py` | the loop: plan, attempt, verify, move, remember |
| `src/locks/trace.py` | the opt-in observability trace (`trace`, off by default): every hook event, tool call (input and output, secrets masked) and locks event of a session, tagged (`hook:`, `tool:`, `inject:`, `locks:`), each record with the release that wrote it, and what the hooks did to the session beyond injecting (`ACTIONS`: its launch, a hook skipped as a program's, the orchestrator role, a relay, notes filed from it, a dispatcher revived), in the locks home's `traces/<session>.jsonl`, the transcript copied beside it at each Stop; `locks trace` reads it, `skills/insight/` audits with it; stdlib, never fails what it watches |
| `src/locks/forensics.py` | `locks forensics`: where money, the cache and tool friction went, read from a copy of the store (a SQLite backup, or `--db`), never the live file; the cache share from the sessions' usage, else from the workers' attempts; every figure observed, none modeled; no model (`docs/observability.md`) |
| `src/locks/footprint.py` | `locks footprint`: each `briefed` event's footprint (context and system characters, skills, the release) as the median per kind and release, and a release that grew it `GROWTH` times named; said, never acted on |
| `src/locks/skill_use.py` | `locks skills`: per skill, the briefs that carried it, their pass-first and when last carried; `active`, `stale`, `dormant` or `pinned` (`--pin`, in the locks home); a dormant one is said, never moved or deleted |
| `src/locks/notes_fork.py` | `notes_fork` (off by default): a session's notes filed by a fork of the session instead of Haiku, tried in turns per project and kept where it kept no fewer facts a filing; its cost recorded, never the criterion |
| `bench/ab/` | the A/B of two locks checkouts on trap tasks (one waste class each), judged by the store's records and a hidden check (real money: run on purpose) |
| `src/locks/signature.py` | the tool-call signature and the append-only log the PostToolUse fast path writes; imports nothing heavy |
| `src/locks/habits.py` | mining sequences per repository (and the ones several repositories share), recipes for the brief, and approval-gated habit hooks |
| `src/locks/usage.py` | tokens per model from session transcripts and their subagents' (counted to the delegation that started each), spend by model and cell, and what a task's or a session's own turns cost |
| `src/locks/flow.py` | the queue's dispatcher (`locks work`): lanes per repository, each task its own `locks run-queued` process, as many at once as the machine's level allots each lane (`pick`), the stale sweep; a detached dispatcher at its task limit hands queued work to a fresh one |
| `src/locks/capacity.py` | how many workers this machine runs at once: the level, kept in the locks home, that starts at what the machine holds and rises or halves on the API's and the machine's own signals, and its allotment to the lanes by reserve and lending; stdlib |
| `src/locks/dispatch.py` | Haiku's call on whether a queued task runs beside running work or waits, and the one-dispatcher-per-repository lock the `++` hook starts it under; light, the hook imports it |
| `src/locks/split.py` | a request that is several tasks, as its pieces (`locks split`): by its listed items with no model, else Haiku read strictly as a plan's parts; `--queue` starts them on what the task kept |
| `src/locks/merging.py` | kept branches merged into the person's checkout on their word (`locks merge`: the ones that touch no other first, the check after each, a conflict aborted), and a draft pull request with the verdict (`locks pr`) |
| `src/locks/guides/` | the orchestration skill's detail, a topic at a time (`locks guide <topic>`): the skill itself stays short |
| `src/locks/after.py` | work queued after other work (`--after`): held in its options until that task or run passes, refused when it cannot pass, released onto the branch it kept (or the base, once merged), and the line every view says of why it waits; light, `stops` and `dispatch` read it |
| `src/locks/store.py` | SQLite memory: tasks, messages, attempts, problems and fixes, notes |
| `src/locks/revisions.py` | a role's lessons per repository, improved by measured trial: proposed by Haiku from the role's dead ends and verifier blockers there, tried in alternating arms, adopted, dropped or reverted, every record appended to `role_revisions`; `locks roles revisions` reads it |
| `src/locks/behavior.py` | how a worker worked, per attempt, from its own tool calls (never their arguments): before its first write, the checks it ran, fix loops, an unchecked end, rereads, repeats, silences, tool families; kept on `attempt_finished.behavior`, added up by kind, outcome and cell for `locks stats` and exported per attempt (`--attempts`); observed, no model |
| `src/locks/arms.py` | two arms tried in turns until each has enough first tries, then the change only where it passed first tries more often, or as often and faster (a tie goes to the arm without it), and never where it is slower than `SLOWER` times the other's median over `TIME_SAMPLES` timed passes each: the shape methods and the recall order share (`tally`, `pick`); which arm a task goes in is the hash of the trial, the repository and the task (`side`), the arm with fewer tasks only once they are `BALANCE` apart, so no two trials over the same tasks move in lockstep; while a repository's arms are short of the trial and every repository's are full, the pooled verdict decides there and says so (`pick`'s `pooled`, `verdict`, `pooled_why`; the primer, evidence and trunk trials the same way), its own arms deciding alone once full; no model |
| `src/locks/methods.py` | how a kind's workers are briefed to work, tried against its absence: a diagnosis protocol for `debug-unclear`, a pre-mortem for `plan` whose `Risks:` reach the independent check of the steps after it; arms per repository (`method_arm`); no model |
| `src/locks/critique.py` | a plan a workflow fans out over, read blind before its parts are built (the `critique` setting): a read-only sonnet/low reader given the request, the contract and each part's prompt and surfaces, nothing else, that names the design decisions a part leaves to its worker and the criterion no part answers; the plan goes back once with its findings, its second answer stands; tried in arms per repository (`critique_arm`), judged by the first tries of the parts after the plan as the pre-mortem is; no model of locks' own beyond the reader |
| `src/locks/predict.py` | where a writing task shaped like ones that failed their first try here starts: one rung up, only where the climbs from that start to the rung above rescued those failures (`shape`: kind, size, risk tier, areas; `start`, the backtest on the repository's record); never a lower, pinned or probation start; kept on `planned.predicted`, shown by `locks stats`; no model |
| `src/locks/racing.py` | whether racing a first try paid, per repository and kind: each race read from its attempts (rung, start, neither), the rung's wins and the time to pass against climbs, the judgement `race_cell` and `locks stats` read; no model |
| `src/locks/cells.py` | where a lesson belongs: the area a path or a change is in, the role that met it, or everyone (`scope`); the text a failure is kept and matched by (a workflow step's goal, never its template); the cell's primer (how its workers got oriented) and its trial; no model |
| `src/locks/cells_by_kind.py` | `locks cells`: per kind, the cell locks starts it on here and each cell's first tries, the cheapest proven one named; for the delegating session's own choices, never a rule; read only, no model |
| `src/locks/coverage.py` | `locks coverage`: which features this machine's records show ever ran (`FEATURES`: the events, fields and tables that mean each one did, every kind and bundled workflow at zero too); read from a copy of the store, no model |
| `bench/speed/matrix.py` | one real run of each bundled workflow the speed benches never start, each ended with `locks doctor` (real money: run on purpose) |
| `src/locks/doctor.py` | `locks doctor`: states the store should never hold, each a bug that shipped turned into a query (broken: stuck now; suspect: over the last days); read only, no model; SessionStart names what is broken |
| `bench/review/` | the review benchmark: real review chains (one review, two blind ones, two with different purposes, a classifier before a quality check) on a change with planted bugs, matched by `analyze.py`; what it measured is in its README |
| `bench/speed/kinds.py` | whether Haiku does `refactor` and `test` work as right as their first rung: real workers on the memory bench's pricing engine, a refactor judged by its layout and prices kept, tests judged by mutants killed (real money: run on purpose) |
| `bench/speed/methods.py` | whether the diagnosis method helps an unclear bug: real workers on the pricing engine with a planted bug told only by its symptom, with and without `methods`' lines (real money: run on purpose) |
| `bench/speed/plans.py` | which cell plans: real read-only planners on the harder pricing engine, scored by the traps their plan names (real money: run on purpose) |
| `bench/memory/` | the memory benchmark: real workers on a small pricing engine, judged by hidden tests, cold against briefed with a teacher's memory; what it measured is in its README (real money: run on purpose) |
| `src/locks/notes.py` | what a project knows, by topic: notes, typed links between them, anchors to code that send them to review; filed by Haiku, recalled by topic, file and one link away; constraints, the rules a change must meet, briefed apart as requirements |
| `src/locks/models.py` | what `--model` gets for an alias (the alias, or a person's pin), the model that served an attempt, and workers lagging the person's sessions |
| `src/locks/grants.py` | what workers may do: the permission mode per model (auto; Haiku bypasses) and the rules a person keeps per repository with `locks allow` |
| `src/locks/runtime.py` | what a worker and the check run with: the Node a project declares (`.nvmrc`, `.node-version`, `engines.node`), or its installed dependencies need when it declares none, found among nvm's and put first on PATH; and the project's own Python package here first on PYTHONPATH when its `.venv` imports it from elsewhere (an editable install read from a worktree, `package_elsewhere`) |
| `src/locks/preflight.py` | whether a task's check can start here, read before its first attempt: the programs it (and the scripts it names) starts on the PATH it runs with, the scripts and deno tasks it names, the Node the project declares, and a `.venv` importing the package from elsewhere (a warning); no model, fails open |
| `src/locks/allow.py` | a refused call → the `--allow` rules that let it through, one per program; suggestions only, nothing is granted |
| `src/locks/stops.py` | why a task stopped, who made the call, and the command that continues it; one account on its `finished` event; light, the hooks and the UI read it |
| `src/locks/orchestrator.py` | which session orchestrates a project (`.locks/orchestrator.json`): claimed by sending work, handed on by a relay, a clear in its place or going quiet, with the work the old one sent; stdlib |
| `src/locks/project.py` | a project's `.locks/` folder: enrollment (only enrolled projects show in the UI) and `sessions.json`, the session names; stdlib, no git call |
| `src/locks/workspaces.py` | a folder of repositories that is one project (`locks workspace`): declared in the person's locks home, read by `repo.key` and `project.find`; a member's files are named with it in front in the notes |
| `src/locks/naming.py` | Haiku names a session, started detached by the Stop hook; a name a person wrote is never replaced |
| `src/locks/ui/` | `locks ui`: `api.py` turns the store into JSON (testable without a socket), `server.py` is the envelope and the routes, `static/` one ES module per screen, no build step; `svg.js` draws the workflow graphs and the project map node by node; `workers.js` the board's pixel-art workshop, one scene per running task from its phase and stage |
| `src/locks/compact.py` | the facts of a session (requests verbatim, failed calls, changed files), written before a compaction and put back after it; no model call |
| `src/locks/scribe.py` | the scribe: at every turn's end, what a session's transcript gained (requests verbatim, corrections, files written, failed calls and the one that passed after), kept in the locks home; the thread a task the session sent gets in its brief, when the notes are due, and the relay to a fresh session; no model call |
| `src/locks/analysis.py` | where locks' time and money go (`locks stats`, the UI's Analysis): runs from `planned` to `finished`, their timed phases (and the gaps before them: advance, pickup, spawn), attempts by cell, the session's thread in briefs, a session's own turns; read only, no model |
| `src/locks/hooks.py` | Claude Code hooks: prompt ↔ task coupling, dead ends into context |
| `src/locks/scripted.py` | whether a hook's session is a program's `claude -p` (print mode with `--json-schema`, a program's output format, or no terminal on its input), read from the nearest `claude` process above the hook (/proc on Linux, `ps` elsewhere, its terminal standing for its input's); stdlib, imports nothing of locks |
| `src/locks/loops.py` | goals that run in rounds (`locks loop`): a definition, a diary and a state per loop in the person's locks home, each round a workflow run, the metric measured by locks, stops on evidence, strategies tried in arms; `loop_cli.py` is `locks loop …` and the detached `loop-runner` |
| `src/locks/trunks.py` | a workflow step whose first try forks the session of a reading step before it (`trunk`): what it forks, the note that marks the role it takes from there, and the trial (`fork` against `fresh`) that keeps forking only where it paid; no model |
| `src/locks/workflows.py` | saved chains of tasks: definitions in three scopes (project, user, bundled `workflow_templates/`), runs the engine advances when a step passes, each step on the commit the one before kept; a step that fans out over a plan's parts, one that joins their branches, a large change's review by area; the bundled templates (`feature`, `feature-parallel`, `bugfix`, `review-fix`, `dual-review`, `panel`, `tracer-slice`, `design-review`: four blind readings of a design document, one lens each, then a refuter, no fix; `drift`: one read-only step comparing a folder's documents with the code); steps that choose (`when`), ask the person (`ask`) or run another workflow (`workflow`); the graph as Mermaid (`render`); a change refused on a stale revision (`expect`); `workflow_cli.py` is `locks workflow …` |
| `hooks/locks.tsx` | the mod (function hooks), a thin layer over `bin/locks`: model tools, waking the session on endings, a status line, `/locks` opens the web board; `hooks/locks-cli.ts` its engine-free helpers, `types/index.d.ts` its state contract, `hooks/locks.test.tsx` its tests |
| `src/locks/services.py` | the servers a task needs up (`--service 'cmd@port'`): started in the task's directory, waited for, stopped with their process group when the run ends |
| `src/locks/isolate.py` | one git worktree per writing task, the checkout's ignored dependencies linked in; work that stopped for a person kept unverified; `at_base`, a temporary worktree at a task's base for comparing its check |
| `scripts/mutate.py`, `docs/workflows.md` | mutation testing of a module against the tests named for it (one AST change at a time, the module always put back, a suite that fails unmutated refused): the rules a suite runs but does not pin; and which bundled workflow fits which work, with one measured run of each |
| `scripts/bump.py` | a new version in every file that names it at once (`__init__.py`, `pyproject.toml`, `uv.lock`, the plugin and marketplace manifests), the CHANGELOG's `[Unreleased]` closed under it; nothing written when one file is off or the notes are empty |
| `scripts/livespec_check.py`, `.github/workflows/livespec-pin.yml` | the livespec locks runs kept current: every week a release on PyPI newer than `PINNED` is pinned, verified against a real index of this repository (the index read, a brief, a change's reach, every tool locks names served, `validate_openspec`), and proposed as a draft pull request once the suite passed with it; stdlib, no model |
| `scripts/release_notes.py`, `.github/workflows/release.yml` | a version that reaches main with CI green is tagged `v<version>` and released with its CHANGELOG section, by itself; one already tagged is left alone, and a person tags an older one by hand (`workflow_dispatch`); stdlib, no model |
| `commands/orchestration.md` | the one entry point a user types |
| `commands/habits.md` | review the habit candidates for this repository and install the ones the person picks |
| `commands/workflow.md` | make, change, copy, run and watch workflows |
| `commands/loop.md` | `/locks:loop`: a session proposes a loop with the person (goal, metric, rules, what acts), builds it only on their yes, and starts it as a draft |
| `commands/watch.md` | a session that watches every other one: `overview`, the endings as they happen, `SendMessage` to send work |
| `commands/relay.md` | `/locks:relay`: hand the session's thread to the next session started in the same place (`/clear`), instead of compacting |
| `bin/locks` | the launcher every hook and command goes through |
| `src/locks/legacy.py` | the old name, cauce, wherever an install from before the rename holds it: `CAUCE_*` read as `LOCKS_*` at import (and repeated to a worker), the home moved once with a link left (`cauce.db` renamed only while nothing holds it), `.cauce/` read and renamed when work is written, kept `cauce/task-N` branches and `refs/cauce/` renamed before use, the store's rows read into the new name once, and the words other readers accept (blocks, trailers, a neighbour's manifest, the UI's header, the bundled team); stdlib, imported first, never raises | <!-- legacy-name -->
| `scripts/rename_to_locks.py` | the rename as a script: ordered rules, the paths moved, an allow-list that says why each old name stays; `--check` fails while one is left outside it | <!-- legacy-name -->
| `src/locks/interpreter.py` | which Python runs locks; imported before the version check, so it must run on any python3 |
| `src/locks/link.py` | `locks link`: a shim on a terminal's PATH that survives plugin updates; `forward_to`, which sends a typed command from an old install to the newest (never a hook, never a checkout) |
| `src/locks/acp.py` | `locks acp`: locks as an Agent Client Protocol agent over stdio (Zed's `agent_servers`): JSON-RPC lines, one session per `session/new` and its folder, each prompt a `locks run` (or `locks tasks`/`stats` for `/tasks`/`/stats`) in its own process group, its stderr progress as one tool call, its report as the answer, a cancel ending the turn at once and the task after `CANCEL_WINDOW_S` unless a prompt follows (`locks cancel N` then a kill; `/cancel N` is the explicit one), `session/load` refused with what to do, the request's items and locks' stages as its `plan`, `locks tasks`' line as the tool call's title, the worker's last call, a failed check's output and the worker's files as content, the verdict and next step as the answer's lead; spawning, diffs and look injectable; stdlib |

## Verify before reporting done

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m coverage run -m pytest -n auto
.venv/bin/python -m coverage combine
.venv/bin/python -m coverage report
LOCKS_TEST_ALL_ON=1 .venv/bin/python -m pytest -n auto
claude plugin validate .
claude plugin test .
.venv/bin/python scripts/smoke.py   # before a release: one real task, real money
```

Coverage measures the processes the tests start too (`patch = ["subprocess"]`): `combine` gathers them.
`LOCKS_TEST_ALL_ON=1` is the profile where every adaptive setting stays as a person gets it, with the
fake `claude` on PATH (it answers Haiku's questions from their schema): the unit tests turn each
setting on alone, and the bugs live where they meet. A new setting goes in `conftest.SWITCHES`, and a test
that relies on one being off says so itself (`monkeypatch.setenv`, or the `no_verifier` fixture): both
profiles pass whole.

`claude plugin validate .` also reads the mod. In the hooks module, `$` stays
in the file: helpers that take it are declared at its top level, and imported
helpers never get it.

`tests/test_scenarios.py` runs the real commands as processes (the dispatcher,
`run-queued`, the automatic resume, workflows, worktrees) with
`tests/scenarios/fake_claude.py` as `claude`, and ends each scenario with
`locks doctor`: a bug that leaves the store in a state it should never hold
fails there. A doctor rule's `suspect` reads only records the release that fixes
its bug wrote (`doctor.FIXED_IN`, the `version` its events keep): a new rule
names that release. A way work moves that the unit tests drive in-process (a death, a
resume, a part, a lane) gets a scenario; a bug that shipped gets a doctor rule;
a request the router got wrong goes in `tests/corpus/requests.json`; a field
one place writes and another reads gets a round trip in `tests/test_contracts.py`.
Before a release, a small real task runs on the new version too: the CLI and the
model are the one thing no test here fakes faithfully.

The coverage floor lives in one place, `[tool.coverage.report] fail_under` in
`pyproject.toml`. Raise it when the real number rises; never lower it to pass.

## Conventions

- **A bound limits what one brief or view carries, never what is kept.** A
  worker's findings, a request's criteria and a ledger's rows are kept whole;
  what one brief or screen cannot carry waits or is said by count, never
  dropped with a bare slice. A bound that stops work is a rule with evidence,
  never a number picked once.
- **Shown text is clipped by `text.clip`**: at a sentence end near the limit,
  else a word boundary, never inside a bracket, marked with "…"; never a bare
  slice.
- **Everything committed is in English** — code, prompts, docs, commit
  messages, PRs. The Spanish in `classify.py` and `text.py` is different and
  stays: it matches what users type.
- **Standard library only** in `src/`. A hook runs on every prompt, and a
  dependency that fails to import there is a session that fails to start.
  Adding one needs a stated reason the stdlib failed.
- **No client or engagement names, ever** — in code, tests, examples or
  commit messages. Describe the shape of a system, never who it belongs to.
- **Tests use the `LOCKS_HOME` fixture** in `tests/conftest.py`. Never bypass
  it: a test that writes to the real database puts fake dead ends in front of
  real sessions.

- **The old name lives in one place.** Every reader of a name from before the rename (an
  environment variable, a path, a branch, a block, a trailer, a manifest key, a persisted word)
  takes it from `locks.legacy`; nothing an existing install holds is dropped or rewritten by
  guess. `scripts/rename_to_locks.py --check` passes, and a line that must name it says
  `legacy-name` in a comment, with why.

## Fail open, fail closed

**Hooks fail open.** A hook that raises takes the user's session down, so
`hooks.main` swallows everything — and writes it to `hook-errors.log`, which
the next `SessionStart` reports. Failing open is not failing silently.

**Verdicts fail closed.** A worker with no result block, two blocks that differ
(the same block repeated is one verdict, `launch.result_block`), or a pass
without evidence is `inconclusive`, never a pass. A pass the independent check
reads late (`late_verifier`) is a pass on locks' own check until that check shows a blocker; then
it is reopened as `review`, never left standing. Changed files come from git,
never from the worker, and every report prints them beside the worker's
summary — the work directory's checkout and every checkout nested in it. `--verify` overrides a claimed pass; it is the only thing that can
pass an attempt whose worker was refused its own check. Refusals come from the
CLI's `permission_denials`, never from the worker. A classifier that cannot run
yields the default kind, not a guess.

## Rules the router keeps

- Effort and model are different dials. Shallow work climbs effort; a wrong
  approach or a repeated answer changes model. Do not collapse them into one
  ladder walk. More effort on the same model keeps the approach, and so the
  session (below); another model never inherits it.
- A retry is for a launch that broke (a timeout before any change, no JSON,
  an errored session); it is never an escalation. A session that ended with no turn and nothing spent
  (`launch._never_reached`: its envelope says both) never got its brief, a hook of the machine turned it
  away: the environment at once, the hook named, never a shape asked again in that session nor a climb;
  an API error before the first turn stays an API error. An answer only its form
  failed (no single result block, a plan's parts that cannot run:
  `WorkerResult.shape`) is asked again of the same cell, once, with its turns
  and in its own session when one is kept (`Decision.reshape`); a second one
  climbs as any inconclusive does. An environment the worker
  named, or a check locks could not run, blocks at once. A refused
  command is neither: it blocks at once, because it is a setting. An
  environment failure on an attempt that changed files is not work that never
  ran: it blocks at once too, its work kept. A timeout after the work changed
  files is out of turns, not the environment: more turns, and an attempt's
  time grows with its turns (`launch.timeout_for`). A network that dropped is
  neither: read from a session's own failed call (`launch.NETWORK_DOWN` and
  `NETWORK_FLAKY`), from a worker that named the environment (`NETWORK_DOWN`
  only: its evidence is a command's output), or from locks' check only on a
  line where the check itself could not reach it (`launch.CHECK_OFFLINE`),
  never a word a test logs. The same cell goes again after each of
  `escalate.NETWORK_WAITS`, from the work it kept; such a try counts toward no
  attempt ceiling (nor `cli.UNATTENDED_ATTEMPTS`) and is no answer for a
  repeated pair; one that stays down stops the task as `network`, its work
  kept. The account's own usage limit (`launch.USAGE_LIMIT`, read where a rate
  limit is, its reset read by `launch.resets_in`) is neither: it stops the task
  at once as `limit`, its work kept, the try no attempt (`stops.lost_tries`); no
  queued work starts before the reset (`stops.LIMIT_META`, `flow.limited_until`),
  and `dispatch.recover` resumes it after the reset, from a detached `locks
  limit-wait` when nobody's turn ends then, at most `dispatch.LIMIT_RESUMES`.
- A build the plan sizes at `orchestrate.SPLIT_POINTS` or more, outside a workflow run and
  not sent `--whole`, that outgrows its turns is several pieces, not one that needs more
  room: it stops as `split` (`escalate.decide`'s `several`), its work kept, and the ending
  offers `locks split N` (`--queue` starts the pieces on that work, and what waited for it
  waits for the last piece). `dispatch.recover` never resumes it; a resume gives it more
  turns as one piece.
- Out of turns is room, never a bug: a finding of the independent check in
  work whose worker ran out of turns before its own check earns the same cell
  more turns, never more effort or another model, and is no dead end.
  More turns on the same cell continue the worker's own session
  (`claude -p --resume`, its id read from the attempt's JSON and kept on the
  attempt) in the same worktree, told only to go on under the same result
  contract and given the output that failed its attempt; a session is kept only in a worktree, never in a person's
  checkout. More effort on the same model continues a fork of it
  (`--resume <id> --fork-session --effort <new>`, the session it came from
  kept as it was), in the same worktree, told to look deeper and given what
  failed: the check's failing output, or the independent check's blockers in
  a correction round (`orchestrate.continuation_note`). When it cannot be
  continued (no id, the CLI refuses it, the worktree was reset, a different
  model, or for more turns a different cell) the attempt starts fresh and
  says why. The `moved` event says what the next attempt does (`continued`,
  with `forked_from` or `resumed`, else `fresh` with why) and its
  `attempt_finished` what it did, so `locks stats` compares continued and
  fresh attempts. Any other move starts fresh: a new model never inherits the
  old one's approach (it starts from the base, the work set aside), except work that only locks' own check failed on (`orchestrate.carries_work`: a `code_bug` with the check's output and files changed, no blockers of the independent check), which the next model finishes in the same worktree (`work_carried`), and a
  retry, a replan and a resume a person starts are fresh. The independent
  check never runs in a worker's session.
- A failed check's own output is kept per attempt (`launch.check_tail`, on the `attempt_finished`
  event, read by `Store.check_tails`): the failing names, the last lines and the base comparison,
  ANSI stripped, secrets masked, bounded by `CHECK_TAIL_CHARS`. `locks show`, the task page, the
  ending of a task that stopped short of a pass (this run's last attempt only) and the next
  brief (`failed_output`) read it; an attempt without one falls back to its evidence.
- Cost is measured and shown, never a brake: every attempt keeps what it cost
  (its own and its independent check's apart, and how much of its input the
  cache served; a continued or forked session's is what it added, `launch.own_cost`, since the CLI's figure
  for one is the whole session's), and the report, the board, `locks spend` and `locks stats`
  show it; no task stops, skips a cell or gets a shorter attempt for money,
  and no worker is given a dollar cap. The one place money moves anything is a
  probation start (below), and only back up the ladder. Turns are the one room dial: an attempt
  is bounded by its turns, never above `escalate.MAX_TURNS_CEILING` however
  many are asked for, and the time they earn (`launch.timeout_for`); a run by
  its attempts. A resume nobody approved is refused after
  `cli.UNATTENDED_ATTEMPTS`.
- A check that could not run at all (exit 127, npm's missing script or
  manifest, a tool's minimum Node) is `environment`, never a `code_bug`: its
  exit says nothing about the work, and it is no dead end. npm's EBADENGINE
  warning is not such a line. Dead ends reach a brief only on a strict match.
- A check that cannot start is found before a worker is spent on it (`locks.preflight`, before
  every run's first attempt): a program missing from the PATH the check runs with, a script or
  deno task its files lack, a `python -m X` whose interpreter lacks `X` (`preflight._modules`), a declared Node nothing installed fits stops the task as `preflight`
  (blocked, the environment's) with the fix line, no attempt counted, nothing ran. It fails open:
  what it cannot tell (a substitution, a `cd`, a workspace flag, a path or script the request may
  add (named outside an invocation: a request that quotes its check's `npm run build` adds no `build`), dependencies the worker installs, a dependency's tool in a script) is said, never a stop,
  and a `.venv` importing the package from elsewhere is a warning that says the package here goes first
  on PYTHONPATH (`runtime.env_for`). Ports stay `locks.services`'.
- A request that opens with a verb of change is never read as a read-only
  kind unless a tag says so, and is read as `test` only when its
  first change is about tests: a fix that asks for its own tests is the fix. A clause may open
  with where the change goes before its verb ("In src/x.py add …", `classify._WHERE`).
  A reading kind a caller passes with `--kind` (`chat`, `explore`, a review) on
  such a request is widened to `implement` and the plan says so
  (`orchestrate._caller_kind`), unless the text says it changes nothing, is a
  question, or the task is a workflow step, whose definition chose its kind.
- The plan says a task needs servers when it tells the worker to start them,
  or mentions one and its check talks to one (or it has no check), and names
  the words that set it off: a mention is no need. A task that mentions a
  browser is told workers have none unless a capability gives one, its check
  runs one (Playwright through the scripts it names), or the project's own
  tests drive a headless one (a Playwright dependency, an `e2e` script that
  runs it): then the plan says the check drives it.
- A check that covers less than the repository's CI is said, never a verdict
  and never a blocker: the plan reads each job of `.github/workflows/*.yml`
  (its `run:` commands, the services and the env the code reads) and names in
  one warning the jobs `--verify` leaves out, on the report too. A workflow
  file the line reader cannot read is skipped and said once; one that runs on no push, pull request
  or merge group (a schedule, by hand, after another workflow) is no job of CI (`ci.checks_changes`). A job the person
  quieted for a repository (`locks ci quiet`, kept in their locks home) is named
  no more there.
- Work that stopped for a person (blocked, needs approval) is kept on its
  branch, unverified, and so is work that outgrew its turns (a `replan` for
  turns resumes with more); so is what a cancelled task changed (a cancel
  stops work, never throws it away). Only `failed` and a `replan` for a wrong
  task drop it, and never what an earlier run of the task kept: a resume that
  ends short leaves its branch as it found it.
- A `spec_bug` or `architecture_bug` on work that stands (it changed files and
  locks' check passed, or with no check the worker's own evidence and at most
  one criterion open) is no replan: it stops as `decision` (blocked, the
  worker's call, the orchestrator's to decide), its work kept, the point
  quoted, and `locks run` exits 4. Never "failed".
- A run in the checkout itself (no worktree, and it writes or has a check)
  holds it: a second one there waits, says for which task, and a cancel
  still stops it. A run in a folder that is no checkout holds the folder and
  every checkout right inside it, in sorted order. A run in a worktree never
  waits.
- A task that waits on a person leaves the board by a resume, by their
  dismissal (`locks dismiss`, a button they press), or by a later task with
  the same text that passed (`superseded`); each records its `stops.Stop` and
  is marked reported, never handed to a session as an ending. A dismissed
  task resumes as what it was: one found wrong is still rewritten. A dismissal
  never loses work: a worktree a dead run left behind is committed on the
  task's branch, unverified, and removed (`isolate.keep_left`, a `branch_kept`
  event), as a blocked stop keeps its work.
- Work a session sent is reported to it once when it ends: by the report a
  `locks run` prints, else by the Stop hook (which blocks the turn's end once
  with the endings) or the next prompt.
- Every ending short of a pass records a `stops.Stop` on its `finished`
  event: a new way for a task to stop adds its cause there, and a status set
  without one is read back and marked `recovered`, never presented as recorded.
  A cause no run records any more (`budget`) keeps its words for the records
  that hold it, and resumes plainly.
  A cancel says where it came from; "a person did it" is never a guess.
- The model's stated confidence about its own work is not an input.
- A request's criteria are its own words: a pass answers every `S#`, one left
  out is `inconclusive` and one not met is a `code_bug`, unless the step judges
  other work (a workflow's review, refute or recheck, a reading kind:
  `LaunchSpec.judging`): a criterion it reports the work under judgment misses
  is its finding, carried forward, and the step passes when it answered what it
  was asked. A step that builds or fixes keeps the rule. Prose has no criteria, and neither are the lines a
  brief quotes (`>`: the request a workflow step carries, what the steps before said): `criteria._split` skips
  them. A pass that leaves an `S#` or a ledger row unanswered is a form failure, asked again once of the same
  cell with what it left out (`criteria-unanswered`, `verdicts-missing`), never a climb on its own.
  Items the request puts out of scope (under a line ending in `:` that says
  so, or marked so themselves) are no criteria, and the brief names them.
- A finding blocks only when the change caused or worsened it (or it cannot be
  told) and it is shown, not suspected; a pre-existing one is reported, never
  a blocker. A finding is `deterministic` only with its proof (the command or
  hunk that shows it); one claimed without it is read as `inferential`.
  One marked `fixed` (a worker's own, or an earlier check's the re-check says is gone) blocks nothing (`doctor`: `blocked-on-a-fixed-finding`).
- A red check is compared with the base before it is the work's failure: one
  that ran and failed (not a timeout, not `cannot_run`, not load or offline)
  runs again at the task's base in a temporary detached worktree
  (`isolate.at_base`, dependencies linked as a task's, stale ones left out,
  always removed), run in the copy of the folder the task was sent from; a run
  in a folder of several repositories has each of them at its own commit,
  laid out as the folder is (`isolate.at_bases`), whatever the launch shape
  (worktree, `--no-isolate`, no checkout). Its result is kept in the store's meta for a day per
  repository, base and command, never in a GET. Failures are compared, never
  exit codes (`launch.check_failures`): the pass stands only when the work's
  failing lines are a non-empty subset of the base's (`launch.preexisting`; a
  `_REPRODUCES` kind must clear at least one), recorded as `check_preexisting`,
  said in the report and on the verdict card (`failing at base, unchanged`),
  and the ending offers its bugfix without starting it. A base that passes, an
  unreadable output, or a new failing line keeps it a `code_bug`, fail closed,
  and the evidence names which lines are new. The base's run is given
  `orchestrate.base_timeout_s` (the longest run of the work's own check seen,
  `Baseline.check_s`, times `BASE_TIMEOUT_FACTOR` plus `BASE_TIMEOUT_MARGIN_S`,
  never under `VERIFY_TIMEOUT_S`), and one that timed out is never kept: it
  runs again at the next try, since a kept timeout read as the base's answer to
  every task on that commit for a day. A base that cannot tell (it timed out,
  could not run, no worktree) is no `code_bug` either: the failing tests are
  read against the last base run of that command here that could tell, at any
  commit (`Baseline.known`, kept apart and never expired); when every one is in
  it they were there before (`check_preexisting` with `known_at`), else the
  attempt is `environment` with `base_unknown` and the task stops as
  `base_unknown`, its work kept, the reason saying how many failing tests the
  worker's own account names, and `locks resume N` as the line. A worker
  that stopped on a point (`spec_bug`, `architecture_bug`) whose work stands
  only because the check's failures were all at the base, that names the check
  or one of those failures, and that leaves no criterion unmet passes
  (`orchestrate._only_the_base`); any other point stays a `decision`. A report
  that did not pass and says its check's failure was there before its work
  (a `pre-existing` finding, or its words) is checked, never taken or dropped
  (`orchestrate._weigh_base_claim`): locks runs the check and compares it with
  the base; it passes only on that comparison with every `S#` of the request
  answered and met, else
  the report stands with the comparison added. An answer with no single result
  block is never weighed.
- Two attempts that leave exactly the same failing tests of locks' check
  (`WorkerResult.failing`, `Attempt.failing`) climb no further: a second climb
  would repeat the first and re-run the whole check. The task stops as
  `same_failures` (`escalate._same_failing`, `Decision.stuck`), its work kept.
  A try the network dropped under or the load timed out is not one of the two.
- A check whose exit is a pipeline filter's without `pipefail`, or that always
  exits 0, is said in the plan (`ci.weak`): a warning, never a refusal. So is one
  that runs a tool a project script runs with more (`ng test` against `npm test`'s
  `ng test --code-coverage`, `ci.bypassed`): what the script adds is not checked.
- A red check with at most `orchestrate.FLAKY_MAX` failing tests runs once more
  (`_steady_check`); one that passes then is flaky: the pass stands, a `check_flaky`
  event names the tests, they are kept per repository in the store's meta, and the
  ending says so; every later writing brief there names them (`orchestrate.known_flaky`),
  as data. Anything else is the first run's failure, unchanged.
- Work that fixes a flake (writing work whose request names a flaky, intermittent or
  "1 of N" failure, `orchestrate.flake_fix_runs`) runs its check `FLAKE_FIX_RUNS` times
  (`_repeated_check`, a `check_repeated` event) and passes only when every run does; runs
  that disagree are the flake still there, never the one-rerun pass and never excused by the
  base; every run failing the same tests is one steady failure, compared with the base as any
  is. The plan says so and the verdict card keeps the runs.
- A writing worker's brief names the command that runs only the tests near its change
  (`ci.related`: jest, vitest, pytest), to run as it goes; the full check runs at the end, by
  locks, and the worker is told not to run it before it reports (`bench/speed/handoff.py`).
- Reviewers never vote: their findings are added together, so one is enough
  and there is no tie. A row only one saw and could only read from the code is
  decided by a refuter with its own proof, never by a count; a row it cannot
  decide goes to the person. The engine builds the ledger from the steps'
  `finished` findings, never from a worker's words: a fix gets only its rows,
  a re-check answers only for them against the fix's diff, and a pass that
  leaves a row without a verdict is `inconclusive`. A row is fixed in code: a
  fix whose diff only touches documentation or instruction files
  (`ledger.written_down`) fixes no row but one about those files, whatever the
  re-check says (`noted_only`). Open rows earn one more
  round, up to `ledger.ROUNDS`; then the run stops as `review`. The ledger keeps
  every row a worker reported: a role's brief carries the rows that fit in
  `ledger.BRIEF_CHARS` (shown and agreed ones first, `ledger.fit`), a fix's rows that
  did not fit wait for its next round and a refute's go to the person, each said by
  count. `panel` is this chain after four blind reviews, one lens each.
  What each blind review beside another added (rows no other saw, and of those the ones
  kept) is measured from the runs (`workflows.reviewer_yield`, `locks stats`), never a reason
  the engine changes a definition.
- A review that reports no finding and read nothing is no review: a blind review step (`behavior.REVIEW_KINDS`,
  or a step that judges) that passes clean (`orchestrate._clean_answer`) while its own calls read no code
  (`behavior.read_calls`, from the log's signatures: a file read, `grep`, `glob`, `git diff|show|log`, a shell
  read, a reading livespec tool; a chained or unknown shell call counts as a read, since unknown is not none)
  is held back once (`behavior.UNREAD_SHAPE`): the same worker goes on in its own session told to read the diff
  against its base (`UNREAD_PROMPT`), and a second unread clean answer climbs as any inconclusive does. An
  independent check that passed clean and read nothing runs once more (`orchestrate._reread`); unread twice, it
  decided nothing: the pass stands on locks' own check and the verdict card says so (`verified.unread`). Calls
  that never reached the log (`tools_logged` false) hold nothing back. `review_unread` events, `locks stats`
  (`analysis.reviews_unread`) and `locks doctor` (`clean-review-read-nothing`) keep the count.
  A fix's independent verifier answers its rows in the re-check's words (`verifier_brief`'s
  `rows`): a row it calls open is a blocker of the fix, a correction like any, and when its
  passing check called every row `fixed` (and the fix wrote none only down) the re-check after
  it is decided from those answers with no worker (`workflows._ledger`'s `answered`, a
  `ledger_decided` event `by: verifier`): one reader of one diff, not two in a row. A fix that
  ran no verifier, or whose check left a row unanswered, is re-checked by a worker as before.
- A risk tier is only raised, never lowered; a change that cannot be read is
  high. Its reasons are true: a word is read in its sense ("checkout" in git's,
  `payment` as one name among others is no money until the change touches that
  code; a design `token`, a chart library `migrated` or a label `truncated` is
  none unless a word of the area's own sense is near, `risk._SENSES`; a word
  inside a path or an environment variable's name is never read); "parallel" is never read away, since a missed concurrency request skips
  the verifier and an extra one only costs a check. A high change, or a medium one a cheap cell made, is checked by the
  independent verifier before it passes: another cell, read-only, given the
  request verbatim and the diff, never the worker's summary; it runs at high
  effort on the work's model (never Haiku), and keeps only the capabilities
  that measure (`plugins.MEASURES`, `Capability.measures`), plus any its change touches, and the
  code index where one is present (`orchestrate._index_servers`): livespec behind `mcp_guard`'s
  `livespec` policy, an allow-list of reading tools (`LIVESPEC_READ`; a tool's own `readOnlyHint`
  is not trusted, `export_openspec` carries it and writes files), the late check's too. A correction's re-check in
  a worktree is told what the correction changed since the snapshot the round before read (`isolate.snapshot`,
  `read_at` on its `verified` event, `orchestrate._read_before`, the event's `delta`) and to read that first, the
  rest of the change being read already; after a new model, outside a worktree or with no snapshot it reads the
  whole change. It reads and never writes: what it wrote in the worktree (a shell writes with no write tool) is put
  back as a snapshot taken before it ran held it (`orchestrate._read_only`, a `verifier_wrote` event). A correction
  round is earned by a blocker that
  persists or by progress (every blocker of the round before fixed and a new
  one a probe shows), at most `MAX_CORRECTIONS`; a new finding after progress
  that is only read from the code goes with the pass, recorded, never a
  blocker; what is left at the cap stops the task as `review`, and so does a ladder that runs
  out (or the attempt limit) while only the verifier's blockers stand on work that
  passed its own check: kept on its branch, never `failed`. A verifier that
  cannot decide keeps the pass and says so. It is told why it runs (the
  risk's reasons) and, for a fix, to show the failure at the base and gone; it
  has its own turns (`VERIFIER_TURNS`), never the worker's raised ones, and is
  never told to stash the work it checks. A resume after a `review` stop (a
  dismissed one too) gives its first check the blockers the `verified` event
  kept, and its worker too (`orchestrate.review_context`, `REVIEW_FINDINGS`): a resumed worker never told what
  was wrong verified its earlier work and changed nothing. When the change will need it, the verifier starts beside locks' own check, not after
  it (`orchestrate.Ahead`, the `overlap` setting): a passing writing attempt in a worktree, a
  check to run and no services (their ports would be shared); it reads the diff the check runs
  on and is told the check is running and not to run it itself. A check that fails stops it, its
  verdict dropped as one that never ran and its cost kept (`verifier_dropped`); a change the check
  itself did not write since it started is read again. Its `verified` event says `beside_check`.
  A cheap cell is Haiku or a model below Opus at low effort (`orchestrate._cheap`): Opus at low
  effort passed every hidden test of `bench/speed/versus.py` on its own, and checked as cheap its
  medium changes climbed to opus/high for nothing.
  The check may run after the ending (`late_verifier`, on by default): a pass that locks' own check
  settled ends at once (`verifier_deferred`), and the verifier reads its kept branch afterwards in a
  temporary worktree at its tip (`orchestrate._late_check`, a `verified` event with `late`, its cost
  the task's). A blocker it shows reopens the task as `review` with a `stops.Stop` and a new ending
  its session hears of; one that cannot decide leaves the pass. Never late for a workflow step, a
  fix of ledger rows, or work something queued `--after` waits on (`orchestrate._awaited`): what
  those release could not be taken back. `locks run` and the resumes the dispatcher starts do what
  follows an ending (notes, the late check) in a fresh detached process with its own store
  (`Options.detach_after`, `orchestrate._after_ending`), so the command exits with the ending: never
  a fork, since a forked child's SQLite writes were lost when it closed.
  The check runs with `--setting-sources user` when the installed CLI lists the flag
  (`launch.setting_sources_flag`, `LaunchSpec.setting_sources`, `orchestrate.VERIFIER_SETTING_SOURCES`; the first
  check, a correction's re-check and the late check alike, kept on the `verified` event): the repository it
  judges cannot choose, by a committed `.claude/settings*.json`, the hooks and rules it runs under. Workers and
  blind review steps keep the repository's settings.
  Tests run with `LOCKS_VERIFIER=off`, `LOCKS_OVERLAP=off` and `LOCKS_LATE_VERIFIER=off`.
- A pass the sampling let through unchecked (`verifier_sampled`) is a first try like any, but
  moves no start cell cheaper (`Ended.unread`, `standards.rates(read=True)`, `start_cell`'s `read`):
  a cheap cell checked less would otherwise look better for it, and the calibration would choose
  it sooner; a move up reads every try.
- The independent check's record earns trust (`locks.trust`, the `trust` setting), never a worker's
  word: a cell is a repository, a kind and the worker's cell, and each task's first decided check
  is one inspection (a correction's re-check is no new one). Skip-lot sampling (ISO 2859-3): once
  `trust.QUALIFY` inspections in a row came clean, a medium change a cheap cell made is checked one
  pass in two, after another `QUALIFY` one in `trust.EVERY[-1]`, never fewer; one blocker returns
  the cell to every pass. A high risk, a correction's re-check, a resume carrying blockers and a fix
  of ledger rows are checked every time (`orchestrate._sampling`); locks' own check runs on every
  pass. Which passes are checked is a hash of the repository and the task (`trust.due`), never the
  clock; a pass let through is a `verifier_sampled` event with its cell's level, said in the run
  and on the verdict card, and neither qualifies nor resets a cell. `locks trust` and `locks stats`
  show each cell's record: clean checks, its sampling, the blockers that repeat an earlier task's
  here, the re-checks that found something new, and pass-first and cost per point by the cell work
  started on. Tests run with `LOCKS_TRUST=off`.
- A writing kind may have a cheaper cell below its ladder (`matrix.PROBATION`: Haiku for
  `implement`, from `bench/memory`, and haiku/medium for `refactor` and `test`, from
  `bench/speed/kinds.py`; no other kind was measured). It is tried only on trial
  (`trust.probation_arm`, the `probation` setting): where the ladder's first rung passes
  `standards.PASS_FIRST` of `MIN_SAMPLES` or more first tries of the kind here, on a start nothing
  raised or pinned, never a high risk, a resume, a ledger, parts or a run in the checkout, and only
  with the verifier on, since a cheap cell's medium pass is what it reads. The arms alternate
  (the one with fewer tasks next, `planned.probation`) until the cheaper cell was planned
  `trust.PROBATION_TRIAL` times in `PROBATION_DAYS`; it stops early, lost, once its first tries pass
  worse than `PASS_FIRST` by more than `REVERT_MARGIN` over `PROBATION_EARLY`. Its tries are first
  tries like any, and only the start-cell rule makes it the start (`standards.rungs_of`); a start
  there climbs into the ladder (`Plan.ladder` is `matrix.reach`). It is judged as any change, and
  also reverted when a passed point costs more on it than before it (`standards.PROBATION_COSTLIER`):
  a cheaper cell that climbs is cheaper only in name; and when a passed point takes more than
  `standards.PROBATION_SLOWER` times as long (`bench/speed/kinds.py`: Haiku five times slower). Tests run with `LOCKS_PROBATION=off`.
- A task that stops on a point (`spec`, `decision`) because its surfaces kept it from the
  cause (it changed a path outside them, or its worker says so and names one,
  `orchestrate._widen`) keeps those paths on its stop (`widen`), and its next step is
  `locks resume N --surface …` with them: the resume adds them and its brief keeps the
  diagnosis. Nothing widens a task's surfaces but that resume.
- A check that fails only in tests outside the task's surfaces (its work changed files, the failures are not
  the base's) stops the task at once as a `decision` with those files on its `widen`, no climb; and where the
  index knows them the plan warns of tests that exercise the surfaces and are left out of them.
- Two surfaces under one folder are read apart segment by segment (`surfaces._may_touch`): `*` and `?` stay in a
  segment, a literal path holds what is under it, `**` and a character class are never read apart; in doubt they
  touch. A plan's `tests/test_rule_*.py` beside `tests/test_receipt.py` had been refused as overlapping.
- Surfaces narrow and grant nothing: a change outside them fails the attempt
  and is undone in its worktree, never in a person's checkout. Two tasks whose
  surfaces are provably disjoint run side by side without Haiku; a task whose
  surfaces may touch another's, or hold a file a running task already changed,
  waits without it, whatever the others declare; in doubt, the dispatcher
  decides as before, and Haiku is shown each task's coordination lines, its
  surfaces and what running work changed so far, never asked to guess them.
- A move explains itself: `Decision.because` is the evidence chain (what the
  attempt ended with, the rule, where it goes) and `skipped` the rungs passed
  over. A new branch in `decide` gives both; the `moved` event keeps them.
- A plan starts at sonnet/high (`bench/speed/plans.py`: every trap named at a sixth of opus/high's
  cost), and a plan for parts at opus/high (`feature-parallel`'s step, where it was measured). A routine
  review starts at sonnet/high and climbs to opus/high (`bench/review`'s `effort.py`: on the same change
  sonnet/medium caught 20 of 42 planted bugs, sonnet/high 27, 15 s apart; a reading kind's repeated
  answer converges, so no third reading by the same model). Every climb of a ladder is counted by rung
  (`analysis.climbs_by_rung`, from `moved` events with the attempt after each: `locks stats`, "climbs by
  rung"), observed: the record a rung is kept or dropped by, never a rule's input. An unclear bug starts
  at sonnet/high with the diagnosis method and climbs to opus/high (`bench/speed/methods.py`, W: with the
  method sonnet/high fixed the planted bug 3 of 3 at 38 s and $0.09 a run, opus/high 3 of 3 at 69 s and
  $0.27; without it each missed once); the bench's judge lets a worker add a test for the bug and refuses
  a test line taken away.
- A first try that fails is the slowest thing locks does, so a writing task's start is predicted from the
  repository's record of its shape (`locks.predict`, the `predict` setting): its kind, size, risk tier and
  whether it reaches several areas (`predict.shape`, read from every task's `planned` event). Where tasks
  of that shape passed their first tries at the start cell under `predict.BELOW` over `SAMPLES` or more,
  and the climbs from that start to the rung above passed at least `RESCUED` of them over `RESCUE_SAMPLES`
  or more, the task starts on the rung above and the plan says why (`predict.line`); the backtest is the
  rule, and a rung that did not rescue the shape's failures predicts nothing however often the start
  failed. Only a raise, after the standards and history, before the race; never a pinned start, a
  probation start, a reading kind or a rung that needs an approval. The `planned` event keeps the
  prediction with its evidence (`predicted`), a predicted task teaches the shape nothing of the start it
  left, and `locks stats` shows the first tries of predicted starts per shape (`predict.report`),
  observed. Tests run with `LOCKS_PREDICT=off`.
- A plan a workflow fans out over is read blind before its parts are built (`locks.critique`, the `critique`
  setting; after Google's Elephant-Goldfish Model: a reader with none of the author's context). A read-only
  sonnet/low reader gets the request, the contract and each part's id, prompt and surfaces, nothing else, and
  reports as findings the parts it cannot paraphrase, the design decisions a part leaves to its worker and
  the criterion no part answers. Any finding sends the plan back once to its own cell, in its own session,
  with those lines (`critique.SHAPE`, `PROMPT`, a reshape `escalate.reshape_streak` never counts); its second
  answer stands whatever the reader says. Never a plan of one part, a pinned start (a start a workflow step's
  definition names is no pin), or a climb. `critiqued`
  events keep the findings, cell, cost and seconds, and `critique_cost_usd` on `attempt_finished` is kept
  apart from the worker's and the check's. A repository's plans with parts alternate `with` and `without`
  (`briefed.critique_arm`, pooled while short) until each arm has `critique.TRIAL` first tries of the steps
  after its plans; then it reads only where those passed no less often (`locks stats`). The contract tells a
  plan the zero-decision rule (`PARTS_INSTRUCTIONS`, the `architect` role): a part leaves no name, schema,
  endpoint, error strategy or file placement to its worker, and a decision the plan cannot make is a question
  in its summary, never a part; `locks stats` counts the parts that stopped on a point
  (`analysis.parts_on_a_point`). Tests run with `LOCKS_CRITIQUE=off`.
- A high complexity raises a reading kind's start one rung, never a writing kind's: a build
  classified high passed as often at its ladder's first cell, in less than half the time
  (`bench/speed/versus.py`). livespec's code map raises a reading kind's start one rung, never a writing kind's (the same
  bench, Z: a build raised for a 21-caller symbol passed the same hidden tests in four times the time), and
  only for a symbol the request names outright (widely used, or implementing a critical spec), never for one
  its words merely match (`Livespec.brief`, `_matching_symbols`' `named`). A symbol's callers are the
  distinct calling symbols outside tests, one number for the plan, the size, the rule and the brief. History may raise where a task starts, never lower it below its ladder, and only on
  climbs: a writing task that passed where it started is no evidence a lower cell fails
  (`Store.landings`, `""`), or a start history raised keeps every later task as high; a
  reading kind's pass is its answer, so its landings count where they passed. So may it
  raise the turns it starts with: when at least
  `orchestrate.MIN_TURN_SAMPLES` recent tasks of its kind here (one, for a task of
  `orchestrate.LARGE_POINTS` or more) ran out at the kind's turns and passed with more, it starts at the median those passes
  needed (capped at `escalate.MAX_TURNS_CEILING`), and the plan says so. An
  explicit `--max-turns` wins. A calibrated standard (below) takes the place of
  these rules for its band, and may lower the start cell, never below the
  ladder (its probation cell aside, below), and never the start turns below the kind's (`matrix.turns_for`): a
  cap is room, an attempt that is done stops on its own, and a lower cap only
  cuts the slow passes.
- A task's points are its size on the Fibonacci scale (1, 2, 3, 5, 8, 13),
  estimated when it is planned by a pure function of what the plan knows
  (`points.estimate`: its criteria, kind, risk tier and the areas that raised
  it, the files and surfaces it names, the symbols livespec says it reaches when
  the index is readable, its length, which weighs one at most, and words that ask for the change
  everywhere, `points.sweeping`), kept with
  its reasons on the task and its `planned` event. Its realized size, on the
  same scale, is read when it ends from git and its attempts (files, the lines
  its kept branch holds, turns, wall time, attempts), never from the worker,
  and kept on its `finished` event; what was not measured is left out. A
  workflow run's points are its steps', each part counted.
- A kind's sizes whose own band holds fewer than `standards.MIN_SAMPLES` tasks, two or more
  of them, are measured together as the kind at any size (`standards.ANY_SIZE`, band 0): its
  start cell moves by the same rule (never its turns), is judged by the kind's tasks of every
  size, and a band with no start cell of its own starts there, and the plan says so. A kind's
  estimate is raised one step of the scale when its tasks here turned out `points.UNDER_RATIO`
  or more times their estimate at the median of `UNDER_SAMPLES` (`points.corrected`,
  `standards.size_ratio`); never lowered.
- A standard is per repository, kind and points band, and only a calibration
  moves it: after every `standards.CALIBRATE_EVERY` endings in a repository,
  in the hooks or the dispatcher (`evolution.settle`), never in a GET, by two
  rules and no other. The start cell is the cheapest rung of the kind's ladder
  whose first tries pass at least `standards.PASS_FIRST` over at least
  `standards.MIN_SAMPLES` (a first try is a task's first attempt or the first
  after a new model; more turns or a retry on the same cell goes on with it; a
  pinned task teaches none), one rung per calibration, never below the
  ladder's first rung (but onto the kind's probation cell, by the same rule) and never a cell that
  needs an approval. The start turns
  move toward the 75th percentile of what the band's passes needed, one
  `standards.TURNS_STEP` per calibration, capped at
  `escalate.MAX_TURNS_CEILING` and never under the kind's own. A band moves one field at a time, and not
  again before that change is judged. Money moves no standard: a point's cost
  is measured and shown, never a rule's input, but for the revert of a probation start. `--start` and `--max-turns`
  always win, and the plan says which standard it used and why (its `planned`
  event keeps the changes it started on).
- Every change of a standard is appended with when, the repository, the kind
  and band, the field, before and after, the rule, its evidence (counts and
  rates) and locks' version. Once `standards.JUDGE_AFTER` tasks ran under it,
  it is judged once against the window before it (pass-first, attempts, time
  and cost per point); one that made pass-first worse by more than
  `standards.REVERT_MARGIN` is reverted one step back, as a change of its own
  that names it, and a revert is never judged. The first settle on a store
  sizes the tasks that ended without points (`standards.backfill`: the plan's
  pure estimate from their text, kind and recorded risk, marked as sized after
  the fact) and calibrates every repository with sized endings at once; a task
  that ends with no size is sized when its ending is settled. Changes, verdicts and the daily
  snapshots are append-only: the database refuses an update or a delete.
- A calibration may also skip a rung of a kind's ladder (field `standards.SKIP`, band
  `ANY_SIZE`, value `from>to`): when the climbs from one rung to the next of one move
  (`SKIP_MOVES`: more effort, next model) passed their next try under `SKIP_PASS_BELOW` over at
  least `SKIP_MIN_MOVES`, later climbs from it go to the rung after (`escalate.decide`'s
  `skips`, `Decision.skipped` and `standard`, kept on the `moved` event). Read in the
  repository, and from every repository only when it has fewer (the ladder is code, one per
  kind), the evidence saying which (`pooled`). Never the last rung, never onto or past a rung
  that needs an approval, never a retry, more turns or a wait, never by cost, and never more
  effort onto another model (the dials stay apart: such a climb is left as it was); one per kind per
  calibration, and none while another of the kind waits for its verdict; it holds no start
  cell back. It is appended and judged once like any change, by its climbs: after
  `JUDGE_AFTER` climbs past the rung against those into it before, reverted as its own change
  when they pass less by more than `REVERT_MARGIN` or their tasks took `SKIP_SLOWER` times as
  long to pass, at the median.
- The evolution history takes one snapshot a day, at the first ending of a new
  day in the person's time zone or with `locks evolution --snapshot`, of what
  ended since the last one. `locks evolution` and the UI's Evolution read it
  beside every change and verdict, never write. Tests run on fake clocks
  (`now=`, `tz=`) and fake finished tasks (`tests/test_standards.finished`).
- Fable needs a person: `#fable` or `--allow-approval`. An approval is the
  person's for the run they approved: a resume nobody approved (`--unattended`)
  never uses one an earlier run stored, nor starts on a cell that needs one (a
  `#fable` task's tag is its approval). A resume a session starts on its own
  carries `--unattended` (`commands/orchestration.md`); one that grants
  (`--allow`, `--allow-approval`, `--keep`) is the person's to type, and the
  guidance gives them the line instead of running it.
- A worker gets a model alias unless a person pinned it (`locks config model`).
  locks never hard-codes a model id; the model that served an attempt is read
  from the CLI's `modelUsage`, never assumed from the alias.
- A plan names the rules workers here were refused only from the repository's latest
  `orchestrate.REFUSALS_WINDOW` attempts, and only one refused in `REFUSED_IN_TASKS` tasks or
  more: one task's refusal is not repeated to every task after it.
- A worker is lean (`lean`, `launch.lean_flags`): no subagent tool (its description lists
  every agent defined, read each turn), no skill listing (its role's skills are in its
  brief), and the per-machine prompt sections after the shared part; only the flags the
  installed CLI lists in its `--help` are passed. Tests run with `LOCKS_LEAN=off`.
- Merging is the person's: `locks merge` merges kept branches into the branch checked out
  only on their command, one merge commit each, aborts a conflict (never resolves it by
  guess) and stops where the check fails, the merge in place; `locks pr` pushes and opens a
  draft pull request only on their command. `locks merge --pr` merges onto a new branch,
  leaves the one checked out as it was, and pushes and opens one draft only when every
  branch merged clean and the check passed after each. A worker runs neither, and no one
  merges a pull request but the person.
- Where a task declared no surfaces, the files its text names (`dispatch.named_surfaces`)
  hold it back from work ahead that declares or names files they may touch, or already
  changed one; never a reason to start beside it, and never enforced on the worker. A path in a clause
  that negates it ("do not edit X", "never touch Y", `dispatch._negated`) names nothing.
  A folder named beside files inside it names those files alone, and a bare file name may be the file another
  task names with its folder (`dispatch.names_touch`).
  A task held so starts on the branch the work ahead kept once that passed, when it changed one of those files
  and is not merged yet (`flow.chain`, a `chained` event, read outside the dispatcher's lock): never when the
  work did not pass, changed none of them, or its late check is still to come. Queueing says, never acts on, work
  with the same text queued or running and a declared surface that is a whole folder (`cli._queue_notes`).
- `locks roles import` reads Claude Code agents (a file or a folder, read only) into the
  person's roles: their instructions kept, `model` dropped, their tool list as the built-in
  tools taken away, their kinds read from their name and description; a role of the same
  name is never replaced, and an imported role reaches no worker until a team names it.
- An ending's notice is compact (`hooks.endings`): its instructions, the session's cost and the do-not-re-read
  line are said in a session's first notice of a day and by one short line after it (`locks guide endings`), a
  pass is one verdict line with its criteria shown only when one is not met, the worker's answer is clipped at
  300 characters and its files at three; what is left out is in `locks show N` and said by count. A stop only
  the person clears always carries its own instruction.
- An ending of four or more at once tells each pass in one line (`hooks.verdict_summary`).
  `locks stats --expensive` names the runs over their kind's p90 cost and why, from what
  they recorded. A running task past `flow.SLOW_MIN_S` and its kind's p90 wall time in its
  repository (`flow.slow`, over `SLOW_SAMPLES` passes) is said slow on the board, in `locks
  tasks` and in the mod's `tasks` tool; said, never stopped.
- A running task shows how far it got (`locks.progress`), read and never written: its time against the
  median and the 90th percentile of its kind's passes here (of its size when `progress.ESTIMATE_SAMPLES` of
  them are, never a guess with fewer), its stage from the `stage` events the run writes as its worker hands
  over to locks' check and the independent check, what its worktree holds against its base (files, lines
  added and removed, a tool's artifacts and the checkout's linked dependencies left out), and a worker with no tool call for `progress.IDLE_S`
  said quiet ("a long command, or stuck"), never stopped. The status line tells each of up to
  `SHOWN_RUNNING` tasks so, and the board, the task page, `locks tasks` and the mod's `tasks` tool too,
  with the files it wrote by name; queueing says how long tasks like it took here. Its phase is
  implementing once any call of the attempt wrote, or its worktree holds changes (a shell writes with no
  write call). `locks peek N` and the task page show its diff so far, read only. Its subtasks are its
  request's items (`progress.asked`), never a list its worker keeps: a worker that kept one spent ~5x the
  model calls and ~19% more time on a four-item task.
- A run with no worktree (`--no-isolate`, or a folder that holds several repositories and is no
  checkout itself) records where each checkout it may change stands when the run starts, once, at
  its first attempt (`isolate.stand`, `places` on `attempt_started`, repeated on each later attempt:
  commit and the files already modified), so a later attempt reads its earlier ones' work as the
  task's. Progress, `peek`, the phase and `isolate.changed_so_far` read each of them against that
  record, files named from the folder; a file the person already had modified is the worker's only
  when its content changed since. They read with `git diff-index`, never `git diff`, which rewrites
  the index of a checkout the person (or the worker) may be running `git add` in. Its ending names the repositories changed in place (`finished.in_place`): there is
  no branch per repository, and it says so.
- A resume's plan says what it resumes after, the cell it starts at and why, and when that
  is below the highest cell the task ran (`cli.resume_line`); `locks show` lists every run of
  a task that ran more than once, so a fail resumed to a pass reads as both.
- A plan line a repository hears the same on every task (`orchestrate.repeats`: a
  neighbour's state, the CI jobs a check leaves out, a capability not in the registry) is
  shown once a day there (`said_today`, the store's meta); the `planned` event keeps every
  line and names the ones left out (`quiet`), and the ending leaves them out too.
- An attempt's turns are the larger of the CLI's `num_turns` and its worker transcript's
  model calls since it began (`launch.counted_turns`): a worker that compacted restarts the
  CLI's count.
- A pass whose worker never ran its own check (out of turns, refused) and that locks'
  check settled says so on its verdict card (`unchecked`), and the ending then allows the
  diff to be opened.
- A plan the request asks to be written to a file ("write it in docs/PLAN.md") is
  classified `implement`, never a read-only plan (`classify.asks_for_change`); a `#plan`
  tag keeps it a plan.
- Older locks installs in Claude Code's plugin cache are named once per set at
  SessionStart (`hooks._older_installs`), with the command that removes them; the person
  removes them, locks never deletes from Claude Code's folder.
- A session keeps the plugin it loaded: a command it types runs the newest install
  (`link.forward_to`, which keeps the version it came from in `link.LOADED`) and says the
  session's skills and hooks are still that version's; the prompt hook says a newer install
  once per session and version. `locks ui` stops an older locks UI on its port, found by
  its command line, never another program.
- locks suggests permission rules and never grants one: `allow.py` only
  writes text a person passes with `--allow` or keeps with `locks allow` or
  `locks resume --keep` (stored in their locks home, never in a repository;
  `--keep` is refused with `--unattended`). A rule for an absolute
  path has two slashes (`Read(//abs)`). Three things are allowed with the
  worker itself: the tools of the MCP servers locks hands it, reading the
  skill folders of its role, and the permission mode its model runs in
  (auto; Haiku bypasses), which a person changes with `locks config mode`.
- Two corrections in a row on one model, each leaving the independent check's
  blockers, go to the next model, not the next effort.
- A refused attempt blocks; it never climbs, whatever else it ended with,
  unless the worker found the task wrong. One refused by locks' own worker guard (`escalate.WORKER_GUARD`:
  the task asks a worker to start locks work) is a `decision`, its work kept. A refused write to a file in the
  worker's own directory that git shows changed anyway is no refusal: it
  stopped nothing.
- A reading task's answer is its deliverable: findings, broken code included,
  are a pass, and two cells reporting the same findings stop it as
  `converged` instead of climbing. The answer is kept whole: a reading worker
  (an internal role aside) is told to put it in a `locks-report` block
  (`launch.REPORT_INSTRUCTIONS`, `LaunchSpec.report`), kept as a `report` event, printed whole by
  `locks show`, named by the ending and carried by the UI; `summary` stays short, and prose
  outside the blocks is not kept. A task whose own text says it changes
  nothing (words about the task, never about what it builds) is read as
  reading while it changes nothing, and holds no checkout; a `feature`,
  `implement`, `refactor` or `docs` task never is.
- A kept branch is never deleted while its workflow run is active, and as a
  copy only of a twin whose task passed. Every ending keeps its branch's tip
  (`finished.tip`); a step whose run builds on a branch that is gone puts it
  back there (`branch_restored`), or stops the run with a `workflow_error`;
  a worktree never starts on HEAD in place of a base it cannot read.
- A task that never ran owns no branch: a `locks/task-N` already in the checkout when it
  first starts (a store started over, another locks home) is another's, renamed aside
  (`isolate.FOREIGN`, its tip in the name, a `branch_set_aside` event) and never built on;
  the task starts on its base and is no resume.
- A kept branch is deleted only when nothing is lost (`isolate.prunable`):
  its commits are on another branch, HEAD holds every file it changed, it
  holds only artifacts, or another kept branch holds the same tree; never one
  checked out, never one of a task that may resume. Its tip is kept on a
  `branch_pruned` event. It runs in hooks and the server thread, never in a
  GET. A task asked again while one with the same text keeps its work points
  at `locks resume`.
- A dependency folder whose lockfile differs between the worktree and the
  checkout is not linked (`isolate.Workspace.stale`): the worker installs it.
- A worktree gets what the task needs from the person's checkout and nothing
  more: ignored paths the task names are copied (never linked, never
  committed, up to `isolate.BRING_BYTES`); uncommitted changes come only with
  `--carry`, and a task that names them without it is warned. A new model
  starts from the base (unless `carries_work`); what the last one did is kept on
  `refs/locks/aside/…`, the brief says so, and the refs go with the branch
  when the run keeps none. `--add-dir` is the person's, for a sibling
  repository.
- A service (`--service 'cmd@port'`) is the person's flag, never a
  definition's or a model tool's: locks starts it in the task's directory,
  waits for its port, and stops its process group when the run ends; a port
  already up is left alone, and one that never listens blocks the task as
  `environment` before any attempt.
- A session's turn is recorded for coupling, never as work: `locks tasks`
  and the board leave it out (`--turns` shows it), a turn that ended without
  its answer is a `turn` stop with nothing to resume, and so is a subagent
  whose ending never reached locks after `flow.DELEGATION_TIMEOUT`; neither is
  listed as unfinished work. A notice locks put
  in a session (the endings, a plugin's message, its compaction advice and continuation,
  `compaction.NOTICE`, read back by `NOTICE_RE`) is no turn and gets nothing injected.
- A kept commit's subject is the change's: the line the worker proposed
  (`commit`), else the task's title, never a task number; the task and an
  unverified state are trailers (`Locks-Task`, `Locks-Verified: no`).
- Files a worker's tools write for themselves (`isolate.ARTIFACT_DIRS`, a dev-server
  plugin's `.fresh-dev/` among them) are
  never a changed file and never committed (one a repository tracks stays as HEAD has it, never deleted
  or rewritten in a commit), and a branch holding only them is
  never a review.
- A worker that stopped before its own check (refused, or out of turns) is
  checked with `--verify` by locks when the task changed something;
  a red check is evidence for the next brief, never a pass.
- What locks' own check writes in a worktree (`isolate.fingerprint` before
  and after it) is committed with the work and said: a `check_rewrote` event,
  the report's line beside `changed (from git)`, and a `Locks-Check-Output`
  trailer. It is never a verdict, and the independent check is told which files
  they are (`verifier_brief`'s `generated`, the late check's too): a tracked
  coverage report the check rewrote was blocked as the worker's change in every round.
- A committed build folder (`isolate.BUILT_DIRS`: `_fresh/`, `dist/`, …) that locks' own check wrote into is put
  back as at the task's base after the check (`isolate.restore_built`, a `built_restored` event, `finished.rebuilt`):
  the check passed on the build it made, the branch keeps the source alone, and the report and the ending say to
  rebuild after merging. Only a folder tracked at the base and written by the check; one the task's words or
  surfaces name is its work and is kept, and one never committed is kept as any file.
- A repository's specs are kept alive, never written by locks (`locks.specs`): where `openspec/` exists, a
  writing worker is told a change of behaviour updates the spec it reaches in the same work, the plan says how
  far the specs are behind (said once a day), and an ending whose change wrote no spec, where they are
  `specs.BEHIND_COMMITS` or more code commits behind, offers the docs task that catches them up. Offered,
  never started: the orchestrator decides.
- A repository is spec-first only on the person's word (`locks specs first on`, kept in the store's meta, never in a
  repository): there a build (`specs.FIRST_KINDS`) is told to write the spec before the code, and a pass whose
  change touches code (`specs.coded`: not tests, lockfiles, config or generated files, `holds_behaviour`) and nothing under `openspec/` is held back (`spec_owed`): the first time it
  goes back to the same cell, in its own session, told to write the spec (`specs.FIRST_PROMPT`, as a reshape); the
  second it stands as work with one point, a `decision` for the orchestrator, kept on its branch. Never a climb: a
  stronger model writes no spec a weaker one would not.
- The TRACER method of a repository (`docs/tracer/`) is data in a writing brief and requirements in the
  independent check's (`locks.tracer`), never instructions of locks' own: a flow reaches a brief only when
  the request holds every word of its file name, of a title of its own of two words or more, or of a
  capability of `capabilities.md` the flow belongs to; its CORRECT lines are read as the template writes
  them (the success path's items with the lines under them, the failure table's CORRECT column found by its
  header, never the header row), and the method's files are carried up to
  `tracer.BRIEF_CHARS`; what does not fit is named by section with its lines (`text.outline`,
  `path:start-end`, up to `tracer.OUTLINE_CHARS`), the rest said by count. Its phases that need the person (contract, empathy, scope)
  stay in the session; `tracer-slice` is the bundled workflow for one slice.
- What livespec's index says a change reaches (`adapter.assess`: specs touched, callers outside it) goes in
  the independent check's brief as where to probe, data, never findings (`orchestrate._impact`); whether
  workers handed livespec call it is counted from the tool-call log (`Store.capability_use`), observed, never
  a trial.
- livespec is a step before the worker, never only a tool it may ignore: a plan whose repository has no
  index or a stale one builds it (`Livespec.ensure_fresh`, incremental) and waits up to `livespec_wait`
  seconds (`config.livespec_wait`, 120; 0 keeps the background refresh), each repository of a folder on
  its own and never the folder (`Livespec.roots`; `_index_argv` refuses a folder that is no checkout).
  Past the wait the refresh is left running, never killed, and the task starts without the index (or on
  the stale one a repository already has, as it is) and says which; repositories sharing a `group_db`
  are indexed one after another under its one lock; an index run locks waits on leads its own process
  group (`adapters.livespec._run_in_group`), so its timeout ends `uvx` and the `livespec index` under
  it together; a refresh that failed at this commit is not tried
  again until the checkout moves. With an
  index locks asks it itself and briefs the answers (`Livespec.dossier`, no model and no MCP call, the index
  opened read-only): the symbols the request names and those of the files it names, each with where it is,
  its signature, what it calls, who calls it, the tests that cover it and the specs it implements; the specs
  the request's words find strictly, with their requirement and implementing symbols; for a writing kind the
  helpers named close to what the request says to add; and what the named files reach. It is data in the
  brief, bounded by `DOSSIER_CHARS` with the rest said by count, and the `briefed` event keeps its size
  (`livespec_chars`) and what it carried. A worker is handed nothing of livespec (no server, no hint, no
  orientation): handed to 46 attempts its tools were called in 3. The independent check keeps the index
  behind the read-only guard (`_index_servers`), and what a change reaches is locks' own `assess`, briefed
  to it. Where the repository keeps `openspec/`, a
  writing worker is told to put `@spec:<capability>-<requirement-name>` on the code that implements a
  spec it writes (`spec_links_brief`), and what the change touched that no code names is a line of the
  report and of the independent check's brief (`specs.unlinked`, `LINK_PREFIX`): a spec the change
  wrote blocks, one that was already there is a warning, data and never a verdict of locks' own. A spec
  the change wrote with none raises its tier to high (`orchestrate._risk_of_change`), so the independent
  check that blocks always runs, a docs task's too; a warning raises nothing. Whether the dossier helps is a
  trial (`dossier_trial`, `briefed.dossier_arm`): a repository's first briefs draw `dossier` or `none` per
  nature of work (a resume keeps its arm), and once each arm has its first tries it is briefed only where
  its arm passed no less often and no slower (`arms.pick`, `locks stats`); what locks reads for itself and
  the independent check's index stay whatever the arm. Tests run with `LOCKS_DOSSIER_TRIAL=off`. `LOCKS_LIVESPEC=off` reads no index and briefs none.
  Tests run with `LOCKS_LIVESPEC_WAIT=0`.
- A trial's samples are the tasks that drew an arm while it ran, never the ones its verdict placed
  (`Store.arm_trial`, `_PLACED`: the words `won|lost its trial` in the reason a brief keeps beside its arm): counted,
  they fed only the winning arm and turned the verdict over (`doctor`: `trial-verdict-turned`). A new trial keeps
  its reason under `<name>_why` beside `<name>_arm`, and its verdict says `won its trial` or `lost its trial`.
- A trial is per repository, and a repository with few tasks would pay for both arms for months (the
  primer needs 24 tasks, a role's lessons 20, the trunk 20 workflow steps, the evidence gate 16 the
  check read, a method 12 of its kind): while its own arms are short of the trial
  and every repository's are full, the verdict of every repository decides there (`arms.pick`'s `pooled`,
  `arms.pooled_why`; the primer, the evidence gate and the trunk through their `_verdict`), said in the
  brief's `*_why` as "pooled from every repository while the trial here has …"; once the repository's own
  arms are full they decide alone. An early loss is never pooled: only a full trial is. A role's lessons
  are the repository's own and pool nothing.
- An adopted neighbour is read, never written: the livespec index is opened
  `mode=ro`, and the index is built only through livespec's own CLI. Absent
  and unreadable are different states and neither stops a run. The one
  exception is engram, and only with `engram_write`, off by default: what a
  passing worker learned, filtered as a note is (never a secret, never a
  failed attempt's, never `<private>` text), saved through engram's own
  server.
- engram is used only where the person turned it on (`engram`, off by default): with it off nothing of it is
  probed, read, briefed or handed. Its store is never opened by locks: only its server, once `/health`
  says engram and `/compatibility` says the running store is verified. What it
  holds goes in a brief as data, never instructions. A worker gets
  `engram mcp` behind the `engram` guard: reading tools only, held to the
  project locks resolved. Tests run with `LOCKS_ENGRAM=off`; a test that wants
  it passes a fake `http`.
- An index livespec failed to build is not tried again until the checkout
  moves (`Livespec.last_failure`); the plan says so once per task. A release
  a plugin names that uv reports missing from the registry is remembered
  (`Livespec.unpublished`) and the pinned one runs instead.
- Every livespec tool a hint names is in `adapters/livespec.TOOLS`, checked
  against the release in `MINIMUM_VERSION`. Check a name against livespec
  before adding it, and bump `PINNED` only after checking the new release.
  livespec's `validate_openspec` has no command line, so `locks specs` asks it over MCP on stdio
  (`Livespec.call_tools`, a minimal client cut at its timeout) after `sync_openspec`, both on a
  temporary copy of `openspec/` alone, indexed there by livespec's CLI, with its own `LIVESPEC_HOME`
  and no `.livespec.toml` (`Livespec.validate_openspec`): nothing is written in the person's
  checkout, their index or livespec's registry of projects.
  What `uvx` runs when no plugin entry and no `livespec` on PATH is used is `Livespec.pin()`: the
  release in the newest installed livespec plugin's own manifest (`.claude-plugin/plugin.json`,
  never one `unpublished()` holds), else `PINNED`. A release newer than `PINNED` (a plugin's, or one
  on PATH) is taken, and checked: its own `tools/list` is read once per release and kept in the locks
  home (`Livespec.missing_tools`); an orientation line naming a tool it does not serve is not told,
  the hint names it as not served, and the plan says so (`_release_note`, never a raised rung). Tools
  that cannot be read brief as for `PINNED` and are asked again next time. `PINNED` itself moves by a
  reviewed pull request the weekly `livespec-pin` workflow opens only after `scripts/livespec_check.py
  verify` and the suite passed on it: never an unpinned fetch.
- Tests run with `LOCKS_LIVESPEC=off` and an empty `CLAUDE_CONFIG_DIR`; a
  test that wants livespec builds the fixture index in `tests/livespec_fixture.py`.
- A dispatch decision fails closed: no answer from Haiku means the task
  waits. A wait Haiku gives names a collision (a change the task needs, the same code or
  behaviour changed), never a guess at a shared helper (`bench/speed/dispatch.py`). It is
  made once and kept on the task with its reason, until none of the
  work it was judged against is ahead any more: then it is made again
  (`flow._judged_against_gone`, the event's `again`); and a wait Haiku gave is asked again,
  at most every `flow.REJUDGE_S`, once the work ahead changed files it had not when it was
  judged (the event's `seen` and `moved`). `locks lanes --beside N` is
  the session's word that a queued task starts beside the work ahead; it never
  moves work held `--after`. A task that will
  not write (`matrix.READ_ONLY_KINDS`, or text `orchestrate.declares_read_only`
  reads as read-only; with no kind yet, the one `classify` gives it without the
  model, a build when no rule reads it) is never put to Haiku: it runs beside
  running work, and is no conflict for a writing task, which is judged against
  the writing work ahead of it alone. A reason is kept as plain text on one
  line. Tests run with
  `LOCKS_PARALLEL=off`, `LOCKS_AUTOWORK=off` and `LOCKS_NAMES=off`; a test
  that wants them passes a fake `decide`, `start` or `popen`.
- Scaling has two axes, and money moves neither. Horizontal scaling is the
  number of workers this machine runs at once: the dispatcher's level
  (`locks.capacity`), kept in the person's locks home, never a fixed number.
  It starts at what the machine can hold (its CPUs, free memory and the free
  disk where worktrees live, each worker counted at `WORKER_MEMORY_MB` and at what a worktree
  here holds as measured (`capacity.worktree_mb`, never under `WORKTREE_DISK_MB`), at least 1), rises by one when work waits for a worker
  and a round of the tasks it started (as many as the level) ended with no
  limit, and halves, never below 1, on a rate limit or an overloaded API
  (read from the CLI's own result, `launch.api_limit`, never from a check's
  output), on a worker the machine starved (a CLI or a `run-queued` process
  killed, a full disk), on free memory or disk under its constant, or on a
  load average over `LOAD_PER_CPU_HIGH` per CPU; after a rate limit it does
  not rise before the retry-after. A level the machine lowered (memory or
  disk readings, load, a full disk) goes back once the machine is `clear()`
  for `RECOVER_AFTER_S` and work waits, without waiting for a round. `max_parallel` (`auto`
  or a number) is the person's ceiling, and the level adapts under it. Each
  change is a `level_changed` event, on the task that caused it or on task 0
  when the machine did. The lanes share the level by reserve and lending: with
  level L and k lanes with work to run, each is reserved max(1, L // k),
  granted in the order its oldest work was queued while L lasts; what a lane
  does not use is lent, and a lent worker goes back to its lane as soon as that
  lane has work and a borrowed task ends. Nothing running is stopped to give
  it back, and a lane paused on a person reserves nothing. Every dispatcher
  settles, allots and claims under one lock (`capacity.held`), and judges only
  its own lane's tasks. Vertical scaling is the cell and the turns each task
  starts with and climbs by evidence (`matrix`, `escalate`), as above. Neither
  reads a cost. Tests run on a fake machine of three CPUs (`capacity.machine`,
  in `tests/conftest.py`); a test that wants another passes `machine`.
- A session a program runs is no person's (`locks.scripted`): where the nearest `claude` above a hook runs in
  print mode and asks for structured output (`--json-schema`), writes an output a program reads
  (`--output-format json|stream-json`) or has no terminal on its input, `hooks.main` answers nothing, opens
  no store and claims nothing (no injection, no notes filed, no relay, no orchestrator role); the person's
  trace still keeps the event. Environment variables cannot tell it (a `claude -p` from a session's shell
  inherits that session's); a person's `claude -p` in a terminal and an app over `--input-format stream-json`
  keep the hooks, and what cannot be read is no program's. `LOCKS_HOOKS_OFF=1` turns every hook off (locks'
  workers run so). Tests run with `LOCKS_SCRIPTED_CHECK=off`.
- A session belongs to the `.locks/` in its own directory or up to the top of
  its checkout, never above it; outside a checkout, its own directory only.
  The home directory, `/`, Claude Code's own folder (a plugin's cached copy)
  and locks' home are never a project, a dotfiles checkout at home
  included. The one exception is a workspace the person declared.
- A workspace (`locks workspace add`) is a folder of two or more repositories
  that is one project: `repo.key` answers with the folder for each of them and
  their worktrees, and `project.find` for each of them, so notes, tasks, lanes,
  sessions, the team and the rules kept with `locks allow` are the folder's (a
  repository keeps its own project-scope workflows). It lives in the person's
  locks home, never in a repository, and never inside a checkout. Its folder
  holds only its own repositories, since every one under it belongs to it: the
  ones named, linked in when elsewhere, or with none named the ones right
  inside it. Nothing is made before everything is checked. Making one moves
  what each repository kept where it was done in that checkout (`Store.rekey`;
  a clone elsewhere with the same key keeps its own, and its notes are copied,
  not moved), merges a fact two of them kept (a current copy over one to
  review, with both one's anchors and links), and waits while one of them has
  work queued or running. A member's file is named with the member in front in
  the notes (`api/src/x.py`); a member and its worktrees review only its own
  anchors, and the folder each member's in that member. A task in the folder
  holds every checkout in it, a linked one included. locks offers a workspace
  (a task that reaches a sibling with `--add-dir`: their own folder only when
  it holds exactly them, else a new one beside them) and never makes one for
  the person.
- `.locks/sessions.json` belongs to the person as much as to Haiku: a `name`
  that differs from `haiku_name` is theirs, and nothing in locks overwrites it.
- The UI shows one project at a time and takes no task: work is asked for in
  a session. Claude Code's own task files are read, never written.
- A lane pauses only on a task the dispatcher started, and never on one a
  person cancelled or on an investigator's; it opens again by itself when that task leaves the board
  (dismissed, superseded, or resumed to a pass), and the queue starts again. A resume that reopens it, or `locks lanes
  --unpause`, starts its dispatcher at once (under the one-dispatcher lock), not at the resumed task's end; a person's own `locks run` never blocks the queue. A GET in the UI never starts work or
  sweeps — housekeeping runs in hooks, the dispatcher and the server thread.
- Color in the UI carries meaning and never alone: a state, a kind's family
  and a model's depth each have one tone (`util.js`: `statusBadge`,
  `kindChip`, `cellChip`), always beside their word. The kind hues keep their
  fixed order; a change to them is checked for colour-blind separation in
  both themes. The mod's text carries the same tones as emoji markers.
- The UI opens no browser tab by itself, and never one per piece of work (`/locks:ui`
  runs `locks ui`; `--open` once, only when the person asks): a session's tasks are all in
  its page (`#session=<id>`, the Sessions screen, each task opened inside it). `--tabs` is
  the person's opt-in and gives a session one tab at most: the server holds the sessions
  that have it (`UIServer.claim_tab`), a later run asks with the token (`/api/tab`) and is
  told `claimed: false`, and a server handed over to a newer locks passes the claims on.
  A GET opens nothing. `locks ui --idle-exit N` is the person's opt-in to stop the board once no
  tab was open for N seconds (`UIServer.exit_when_idle`: an open stream is a viewer), counted only
  after one came; without it the board stays up.
- A task page polls only while it shows a running task (`/api/tasks/N/live`,
  every 5 s; tool calls write no events), and what it shows of a worker's calls
  is what `phase.calls` keeps: when, the tool, its signature, whether it went
  through; never an argument nor its hash. The one exception is the person's own trace: with `trace` on, the board's
  terminal also shows each call's input and output from it (`trace.worker_calls`, masked as written,
  clipped by `text.clip`), read from their locks home only. A desktop notification is asked for
  only from a person's click, kept per viewer in the browser, and never fires
  for the state a tab opened with.
- The UI builds every node with `textContent` (`h()` in `static/util.js`,
  `s()` in `static/svg.js` for SVG), never `innerHTML`, and sets no inline
  style attribute (the page's policy refuses it; use the CSSOM): task text and worker summaries are untrusted. The token
  never goes in a URL, and every mutation needs it in a header.
- `plan()` never waits on a neighbour's index: a refresh starts in the
  background and the task uses the index as it is. A run says its task number
  and each attempt on stderr as they happen; stdout stays the report.
- `PostToolUse` runs on every tool call: its fast path in `__main__.py`
  imports only `locks.signature` and appends one line. Do not add a database
  write, a git call or an orchestrator import to it.
- The trace (`locks.trace`) is the one record that keeps a tool's arguments and output: the person's
  opt-in (`trace`, off by default), in their locks home only, secrets masked, a worker's own transcript kept
  beside it when its attempt ends (`launch._keep_worker_transcript`: a `claude -p` worker fires no Stop of the person's), written by its own
  `hook-trace` entry or the existing fast path, never read by a rule; a trace never fails a hook or a write.
  The fast path traces a session with the hooks off only when it is a locks worker (`LOCKS_WORKER_TASK`):
  locks' own Haiku calls are no session of the person's. A call that failed (`PostToolUseFailure`) or a
  permission refused before it ran (`PermissionDenied`) takes the fast path too (`signature.FAILED`): a failed
  call in the tool-call log and a `failed` tool record with its error, so friction counts what never reached
  PostToolUse. `sk-` is a key only at a word's start, or glued with a digit in it: `<task-notification>` is none.
  Every record names the release that wrote it (`version`), and what a hook does to a session beyond what it
  injects is a `locks:<action>` record with no task (`trace.action`, `trace.ACTIONS`), written where it
  happens: the launch at SessionStart (flags only, never a prompt, `scripted.shape`) and each hook skipped as
  a program's (`hooks._trace_launch`), the orchestrator role taken, lost (in the old holder's trace too) or
  ended (`orchestrator.claim`, `ended`), a relay taken and given, the notes filed from a conversation
  (`cli.cmd_extract_notes`), a dispatcher revived or a task resumed by a hook (`hooks._revive`). A new act of
  a hook on a session gets one, so an audit reads it from the session's own trace.
- A `git status --porcelain -z` is read through `isolate.status_paths` (and `launch._status`): a rename or copy
  is `XY new\0old`, and its old name, an entry with no status before it, is a path whole, never cut as one with.
- A stray path outside a task's surfaces is a finding of its own: it never rewrites an `environment` failure, nor a `code_bug` with the check's output, as `approach`, and never empties `check_tail`. Surfaces are read from inside the repository as well as from the folder, and a dependency folder is never offered as one. A container stack a worker started in its worktree is stopped when its task ends (`isolate.stop_stacks`).
- A resume's brief carries the notes filed since its last attempt and the instruction a session passed with `locks resume N -` (`orchestrate.resume_context`), quoted as context, never the task. The independent check gets that instruction too, as the session's later decision: following it is no finding. The late check blocks by the first check's rule (a finding only read from the code goes with the pass). A lockfile that differs only in the project's own version is not stale (`isolate._stale`), and an overlap notice never names a kept branch HEAD already holds (`isolate.already_in_head`).
- Forensics, the footprint and skill use read a copy of the store, never the live file, and say what
  they observed, never a modeled figure; a dormant skill is said, never archived by locks. The notes
  fork is judged by facts kept per filing, never by its cost, which is shown beside it.
- The locks home is the person's alone: the folder 0700, the store and what the scribe and the trace
  keep 0600. Secrets are masked by `locks.masking` only; a new format goes there, with a timing test.
- A git call locks makes has a timeout (`isolate.GIT_TIMEOUT_S`) and never prompts; a write
  transaction of the store never spans a git call or a model call (`tests/test_workflow_lock.py`).
- A claim is never left without a process: `flow.work` puts it back when `start` raises, and the sweep
  after `flow.CLAIM_STALE_S` (`claim_released`). A habit's command sets only `habits.ENV_ALLOWED`.
- The store's schema and migrations run only when the file's `user_version` differs from
  `store.SCHEMA_VERSION` (bumped by hand with `SCHEMA_HASH`, `tests/test_store_open.py`); the old name's
  rows are read on every open. The trace is bounded by `trace.prune` (`KEEP_DAYS`, `KEEP_MB`), run by
  SessionStart once a day with the trace on, never the session's own; `hook-errors.log` keeps its tail.
- The dispatcher's model call runs outside `capacity.held`: candidates are read under it, judged
  without it, then allotted and claimed under it again, each checked as still queued.
- A hook's order is its contract: usage ingest, the scribe and the endings run before anything whose time grows
  with the session; the trace's transcript copy is last, incremental (`trace.keep_transcript`, its offset and the
  hashes of the source's head and of the bytes before the offset kept beside it) and bounded by `COPY_BUDGET_S`.
  A count older than its constant says when it was counted, and `doctor` names `transcript-ahead-of-usage`.
- A signature never holds an argument. The final `[a-z0-9:._-]` filter stays
  last, after the semantic scrubbing.
- A compaction digest is read from the transcript and git, never written by
  a model: it may leave things out to fit, never paraphrase. A person's
  words are kept as typed; harness text (commands, reminders) is not theirs.
- Compacting is the orchestrating session's call (`compact orchestrator`, the
  default): locks' moment reaches it as advice with its reasons, never as a
  compaction, and it compacts with the mod's `compact` tool
  (`compaction.request`), on that advice or on its own, run when its turn ends
  with no check of the cache, the size or the cooldown. What it asks to keep is
  quoted apart and marked as the session's (`kept_by_session`), never mixed
  into locks' instructions. What it asks to go on with (`then`) is never told to
  the summarizer: once the compaction finished (a hook saw it end, the engine
  answered the mod, or it never ran) and within `compaction.THEN_WITHIN_S`, the
  mod submits it once (`compaction.continuation`, a `compaction_continued`
  event) as the session's next prompt, quoted and marked as its own words; a
  worker never compacts, and with `compact off`
  the tool is refused. The tool is delegated like `queue` and `resume`. `queue` waits `QUEUE_MS` for `locks queue add`, which splits
  a request that is several tasks first (Haiku may propose the pieces), and reads the pieces it queued from
  its one JSON line (`--json` keeps prose on stderr).
- The scribe reads a session's transcript, never its context, and never with a
  model: the same rules as a compaction digest, kept as the session goes, in the
  locks home, secrets masked. Its failure never costs a turn its endings. A
  task a session sent gets the part of the thread it needs in its brief,
  verbatim and marked as context, never instructions (the task's text is what
  to do); an investigation gets none, and reading it never fails a run.
- A relay is the person's (`locks relay`, `/locks:relay`): taken once, by the
  next session that starts (or is cleared) in the same place, never by a
  resume, and dropped after `scribe.RELAY_HOURS`. The work the old session
  sent that is open, waits on the person or ended unheard reports to the new
  session from then on, its active workflow runs too; the old session is left
  as it was.
- Habits are per project: mined from one repository's events and gated on
  them alone, and the ones its own tool files declare (`habits.DECLARED`, one per program and
  tracked file type) offered before any session repeats them, reviewed and vetted as the mined ones; a habit of other repositories is offered apart, never counted as
  this one's, and only to a checkout that has its tool (`habits.has_tool`). An
  event's repository is read from its task or session, never written by the
  PostToolUse fast path. A write is an edit (one hook matcher takes both), a
  worker's task is a place of its own, the home directory and temporary folders
  are no repository (`habits._project`), and a command that does not check the
  edit's file type is no habit (`habits.ECOSYSTEMS`).
- A signature reads through a wrapper to the program it runs (`signature._WRAPPERS`, and `timeout` with its
  duration and flags). A habit's review stands until the repository's tool files change in more than the project's
  own version line (`habits.tooling`), and a candidate whose tool a hook here already runs, a sub-command aside,
  is no new habit (`habits.same_tool`). `locks habits` says what is installed first, with its runs and what it
  caught (`habits.installed_in`, `record_line`: one that caught nothing in `QUIET_RUNS` is said so, never removed),
  and offers no candidate a hook here already runs.
- `locks evolution` also says what the record decided beyond the standards (`evolution.learned`): where each trial
  stands, from the last reason a brief kept beside its arm, a role's lessons, the notes their trial moved and how
  many still wait, and each installed habit's runs and catches. Read from records already kept, never computed again.
- Nothing in locks writes to a settings file but a habit: `locks habits install`, run by a
  person, or locks itself installing a reviewed habit (`habits.apply_reviews`, the
  `habit_auto` setting), and only the repository's `.claude/settings.local.json`. A
  project's new habits are reviewed once each by a read-only worker (again when that review
  answered nothing, or once the repository's tool files changed: `habits.reviewed`, `TOOL_FILES`), queued at
  SessionStart at most once a day (`habits.queue_review`, the `habit_review` setting,
  with `autowork`): it finds the tool, a command scoped to the edited file
  (`$LOCKS_EDITED`), runs and times it. Its proposal is never trusted: in
  `evolution.settle` and in the review's ending before it is told (whichever way it is
  delivered), never a GET, locks installs only what passes its own check
  (`habits.vet`): one simple command that runs the habit's own program (`shaped`: no
  second command, pipe, redirection, substitution or other variable), passing on a
  tracked file of its type at HEAD in a temporary worktree, within
  `habits.REVIEW_SECONDS`, writing nothing. Each install, refusal and skip the review chose is an event on the
  review (`habit_installed`, `habit_refused`, `habit_skipped`) and its ending says it with the line that
  removes it; with `habit_auto` off the ending gives the install lines for the person. A
  worker never runs `locks habits install` or `review`. An installed habit whose command breaks
  three times in a row (cannot start, hangs, or runs past `habits.SLOW_SECONDS`) turns itself off; a
  check that exits non-zero with output found something (`habits.caught`, shown to the session), which
  is its job and never a break. Tests run
  with `LOCKS_HABIT_REVIEW=off` and `LOCKS_HABIT_AUTO=off`.
- A check whose failures are mostly the test runners' own timeout lines
  (`launch.check_timed_out`) after a claimed pass, or that ran past its own
  time as a whole (`VERIFY_TIMEOUT_S`), is `environment` with
  `load`: the level halves on it, the same cell goes again after each of
  `escalate.LOAD_WAITS`, and such a try counts toward no attempt ceiling and
  is no dead end; one that persists stops as `load`, its work kept.
- A dispatcher never dies on a full disk (`flow.disk_full`): it waits and
  tries again with its lock and work kept, and first frees what idle worktrees rebuild anyway
  (`flow.shed`, `isolate.REBUILT_DIRS`: a real folder at a worktree's top that git ignores, never
  a link, only of a task neither running nor queued; a `shed` event per task). SessionStart does
  the same under `capacity.LOW_DISK_MB` free and says how much came back, and so does a dispatcher's
  pass on a disk with no room for one more worktree (`flow.low_disk`, every `SHED_EVERY_S`), before a
  worker dies of it; a `locks run` beside running work there says so and goes on (`cli._warn_disk`).
  Nothing git keeps is touched: a resume builds them again.
- A `locks resume` a Claude Code session's shell runs (`CLAUDECODE`, no terminal) runs in a
  process group of its own and the command follows its log (`cli._shielded`, `LOCKS_SHIELDED`):
  that shell's timeout sends SIGTERM to its group, which a resume read as `locks cancel` and
  stopped the task it had just started. Only `locks cancel` stops it now; its ending reaches the
  session as a detached one's does. The hooks (SessionStart, Stop)
  start a dispatcher for a lane with startable queued work and none holding
  its lock (`dispatch.revive`, autowork on), never a GET. A start leaves word
  before it asks whether a dispatcher runs (`dispatch.wake`), and a dispatcher
  that read its queue before that word was left starts a fresh one once its lock
  is let go (`cli.cmd_work`, `WorkReport.looked_at`): work queued while the last
  one was leaving never waits for the next hook (`doctor`: `pickup-nobody-saw`).
- A project names one orchestrator (`locks.orchestrator`), in its `.locks/`,
  never in a repository's files: the first session that sends work, one that
  claims it, a relay from it, the next session that starts where it ended, or
  one that sends work after it went quiet (`STALE_HOURS`). The role brings
  the work the old session sent (`Store.hand_over`), and the orchestrator
  adopts what quiet sessions left in its own project only. A worker and a
  terminal never take it. A stop waits on the person only for what only they
  clear (`stops.PERSON_ONLY`: a refused command's rule, an approval, a gone
  directory, a workflow's question; or past `stops.UNATTENDED_ATTEMPTS`) and a branch to merge;
  every other stop is the orchestrator's to decide. Nothing shows as waiting on
  the person: the status line counts every wait as the orchestrator's (the
  session's own first, `board --session`, and the rest of the project's apart), the
  board lists the ones it must ask the person for apart, and the `tasks` tool
  and the endings tell it to ask in its conversation and run the line on a yes. The orchestrator
  gains no power: it still resumes `--unattended`, and a dismissal it types is
  recorded as its call, never as the person's.
- A start that rarely passes its first try here (under `orchestrate.RACE_BELOW` over at
  least `RACE_SAMPLES` first tries of its kind in the repository, `standards.rates`)
  races the next rung that needs no approval on that try (`race_cell`, the `race`
  setting): each in its own worktree (`isolate.race_lane`), each checked there, the
  first to pass kept and the other stopped (`launch.Stoppable`, its process group). A start whose failed first
  tries here mostly ran out of turns (`orchestrate._outgrew`, at least half) never races: another cell gets no more
  room, and the plan says so.
  A rung that wins has its work carried into the task's worktree (`isolate.adopt`);
  when neither passes, the climb goes on from the rung. Both tries are attempts with
  their cost; one stopped because the other passed first (`RACED_OUT`) teaches its cell
  nothing, and the rung's is a first try of its own (`standards.samples`); a try so stopped
  is no failure of the work (`escalate.Attempt.raced`): a pass beside it opens no problem
  (`doctor`: `problem-opened-by-a-race`), where one that failed its own check first does. Never a
  pinned start, a reading kind, a ledger, integrate or plan-parts step, a resume, a run
  in the checkout, or a worktree that brought files in from it. Racing judges itself
  (`locks.racing`): once `racing.JUDGE_AFTER` races ran among the window of the kind
  here, each read from its attempts (the rung won, the start won, neither), it stops
  where the rung won under `racing.WINS_BELOW` of them, or where the races the start did
  not win took no less wall time to pass, at the median, than the kind's climbs without
  one (each side over at least `racing.TIME_SAMPLES`); the failed first try then climbs
  deep, the plan says why, and races that fall out of the window let it be tried again.
  Money is no brake and never an input: the second worker is the price of the climb's
  wait, and whether it paid is read from wins and time alone.
- A workflow step a blind review of its change follows (`workflows.reviewed_next`: a
  review with no ledger that needs it, or an integrate step that joins its parts and is
  followed by one) runs no independent verifier of its own (`Options.reviewed_next`):
  the review reads the request and the diff as the verifier would, and the plan says so.
- A build (`feature`, `implement`) that lists `route.SPLIT_ITEMS` items or more (in `SPLIT_WORDS`
  words or more), is the
  largest size, or names `PARALLEL_PIECES` pieces in folders of their own is several
  tasks (`route.split_first`, pure): with `autosplit` (on by default) `locks run` and `locks queue
  add` split it and queue its pieces as `locks split --queue` does (`cli._split_and_queue`); off,
  or when no piece is proposed, they start nothing and exit `EXIT_SPLIT` with how to split it.
  `--whole` is the session's word that it is one. A listed item that states a rule (a constraint, a test for
  the others, what not to do: `route.is_rule`) is no piece and is carried to every piece as context
  (`split.by_items`), and items that all touch one module of `src/` are one task (`route.one_module`), as are
  items that are all changes to one named file or document and pieces identical in their own text; a piece
  never gets a derived surface whose path is absent from the repository. `locks cancel --split N` undoes a
  split in one command.
  Tests run with `LOCKS_AUTOSPLIT=off`.
- An ending names other work in its repository (running, or a kept branch of the last
  `hooks.OVERLAP_DAYS`) that changed the same files: merging both conflicts there. A file is named only when
  its content at that work's tip differs from HEAD's, and never work that landed and was edited since. Said,
  never acted on; steps of one run are never named.
- A goal of `workflows.MANY_FEATURES` items or more at the largest size is told,
  before its run starts, that runs per feature or per phase (`--after run:<N>`)
  cost less than one plan for all; advice, and the run goes on as asked.
- `locks workflow run feature` runs as `feature-parallel` when its goal is
  `workflows.FAN_OUT_POINTS` or more, or touches `FAN_OUT_AREAS` areas, by the plan's
  pure estimate (`workflows.fanned`), and says so; never a `feature` the person saved,
  and `--whole` keeps one build.
- A worker that died is noticed at a turn's end, not at the next session: the hooks
  sweep a running task whose process is gone, and so do `locks board` and `locks tasks` when one is gone
  (`cli._notice_deaths`: the mod's status line polls the board, so a death while the orchestrating session
  waits is swept and resumed within that poll). An automatic resume goes on from its own kept work, never
  rebased; its `auto_resumed` event says how far that work is behind the trunk (`dispatch._behind_trunk`). A task that stopped for a mechanical
  cause (`dispatch.MECHANICAL`: died, outgrew its turns, a network or a load that
  outlasted locks' waits) is resumed once on its own before any session hears of it
  (`dispatch.recover`, from the hooks and `locks endings`: `locks resume --unattended`
  in the worktree it left, with more turns for one that outgrew them, within
  `AUTO_RESUME_WITHIN_S`, or `AUTO_RESUME_ABSENT_S` when no worker started anywhere since (a restart nobody
  saw), at most `AUTO_RESUMES` that reached an attempt, a resume that died before its first one not counted, up to
  `AUTO_RESUME_TRIES`, with `autowork` on). Its stop is no
  ending handed to a session; a second one is the orchestrator's. A death names its crash: the last
  exception line its run wrote to its log (`flow.died`, `flow.crash_line`, from where that run began in
  `work/task-N.log`, which the dispatcher's runs and an automatic resume both write, the offset kept in
  the store's meta), read by the dispatcher and by the sweep alike, kept on its stop with the log's path; one that repeats the crash of its last
  death says so and is never resumed on its own (`repeated`): it is locks' failure, not the work's,
  and a resume repeats it. A stop that needs
  judgment or a person (review, permission, approval, a wrong task) is never resumed so. The analysis reads
  the time from an attempt that never finished to its run's end as `lost_s`, never
  as other. What that attempt spent is read from its worker's transcript by the sweep (an
  `attempt_lost` event, added to the task's cost; unreadable is kept as unknown, never $0), and
  the next attempt takes a number past every one started (`Store.next_attempt`), so a dead
  attempt's tool calls are never read as the next one's.
  A resume after a death goes on in the dead worker's own session (`orchestrate._dead_session`, the session the
  sweep kept on `attempt_lost`, `launch.INTERRUPTED_PROMPT`, `attempt_started.after_death`): only at the same cell
  and model, in its worktree, its transcript still there; else it starts fresh and says why.
  The dispatcher keeps that record too when it sees the death first (`flow._Process.poll`; `doctor`:
  `death-left-no-record`), and an attempt is kept under the number its worker ran as, row and event alike.
- Each request is read for its way (`locks.route`, the `route` setting), with no model:
  the session itself (a turn, reading, what points back into the conversation), parts
  side by side (`PARALLEL_POINTS`, `PARALLEL_AREAS` or `PARALLEL_PIECES`), else the
  cheaper reading: the worker's measured turns times the session's context against a
  worker attempt's measured input (`Store.worker_footprint`); with no measure, size and
  a full context decide. The prompt hook says it only when it is not the session, keeps
  every decision as a `routed` event to be measured (`locks stats` says how often the session sent
  locks work after each advice, `Store.routed_followed`), and never costs the turn. A bug sent to a
  worker is also told of the `bugfix` workflow (`route.WORKFLOW_FOR`, the event's `workflow`). It is
  advice: the session decides. A turn that read `route.READ_HEAVY_CHARS` or more of tool output
  with the session's own tools (the scribe's count per turn, `scribe.READ_TOOLS`, `state["read"]`)
  is said at the next prompt beside the route's advice (`route.read_advice`): what it read stays in
  the context every later turn re-reads, and what an explore worker here passes at, in time and
  cost (`Store.kind_record`, observed); kept on the `routed` event (`read_chars`, `read_said`) and
  measured the same way (`locks stats`, `heavy reads said`). The session's own reading is where
  its money goes (this session: locks' injections 0.2% of its text, its own tool output 57%). A question about the work (an opening of doubt,
  `route.asks`: could, should, what if, vale la pena… with a question mark,
  wherever it falls; an opening that asks for a preference needs none, `_ASKS_UNMARKED`)
  stays with the session, and so does a question mark that
  ends a request; neither when the request opens with a verb of change
  (`classify.opens_with_change`): that is an order, whatever it asks after.
- A cell keeps how its workers got oriented (`cells.primer`): a passing worker's
  `orientation` (what it had to find before its first change, filtered as a
  note is) is kept as an `oriented` event with its role and the areas of its
  change, and the next worker of that role, or of a task reaching one of those
  areas, gets the recent lines in its brief, as data, never instructions; the
  verifier never does. Whether it earns its place is measured, never assumed:
  a repository's briefs alternate with and without it until each arm has
  `cells.PRIMER_TRIAL` first tries, then it stays only where its workers reached
  their first change in fewer calls and passed their first tries no worse than
  `PRIMER_MARGIN` below (`primer_arm`); the `briefed` event keeps the arm and
  why, and `locks stats` shows both arms. The `primer` setting turns it off;
  tests run with `LOCKS_PRIMER=off`. The trial is tallied per nature of work
  (`primer_nature`: reading kinds never edit, and arms that mixed them were not comparable), a resume
  keeps the arm its first brief drew, and calls before the first change decide a tie only once each arm
  has `cells.PRIMER_TIMED` timed samples, else it goes to the arm without the primer.
- A role's candidate whose trial has not filled its arms in `revisions.TRIAL_DAYS` is dropped
  for too few tasks, recorded with the counts it reached. The primer's arms fill by count (the
  arm with fewer tasks goes next) and its trial ends early, lost, once each arm has
  `cells.PRIMER_EARLY` first tries and the primed ones pass worse by more than
  `PRIMER_EARLY_MARGIN`; `locks stats` for a repository says where it stands now.
- A role's instructions improve by measured trial, never by edit
  (`locks.revisions`, the `role_trials` setting). Every brief records the role
  and the version of its text (`roles.version`: a hash of its resolved
  instructions and its skills' names, plus the plugin and version of a skill
  read from an installed upstream plugin). Once a role's workers in a repository
  met `revisions.MIN_EVIDENCE` dead ends or verifier blockers since its last
  revision, Haiku, as an editor, proposes a short list of lessons for that role
  there, read strictly (`read_lessons`): no answer or a malformed one proposes
  nothing, and a line that looks like a secret or is `<private>` is never kept.
  It runs in `evolution.settle`, never in a GET, outside the write lock and
  under a claim, with at most one candidate per role and repository on trial.
  The lessons reach a worker in its brief, marked as this role's lessons here,
  as data, never instructions; the role's file is never written, and the
  verifier and the surveyor never get one. The role's tasks alternate between
  the candidate and the current lessons (a resume keeps its first arm) until
  each arm has `ROLE_TRIAL` first tries; the candidate is adopted only when its
  pass-first is no worse than `ROLE_MARGIN` below the current arm's and it is
  better on pass-first, attempts to pass or verifier blockers by more than one task of the
  smaller arm (`ADOPT_BY_TASKS`), else dropped.
  Once `REVERT_AFTER` first tries ran under an adopted one outside its trial,
  it is judged once and reverted, as its own record, when its pass-first is
  more than `REVERT_MARGIN` worse than its trial's current arm. Every proposal
  and verdict is appended to `role_revisions`, which refuses an update or a
  delete. Tests run with `LOCKS_ROLE_TRIALS=off`; a test that wants them
  passes a fake `ask`.
- A passing ending keeps its verdict (`orchestrate.verdict_card`): each criterion the
  worker answered with its evidence, the check locks ran (never the worker's word), the
  independent check's outcome or why it did not run, and the attempts. The ending notice
  gives it (`hooks.verdict_lines`) and tells the session not to re-read the code, re-run
  the check or diff the branch to confirm it: the delegated work is never done twice.
  The diff is opened only on the person's word, or where the verdict says something was
  not checked or not met.
- An ending names the problems the work found that were already there, shown with
  proof (`hooks._found_before`), and the `bugfix` that would take one on `--after` the
  work; nothing is started: the orchestrator decides. One its worker marks `fixed`, or in
  a file the work changed, is counted and never offered: the work most likely fixed it.
  So is one in a file changed since the work started (`hooks._moved_since`: the checkout's
  branch since the base its `finished` event keeps, or other work here that passed after it
  started): it may be fixed already.
- A worker never inherits the host's session id (`CLAUDE_CODE_SESSION_ID`),
  and an attempt never keeps the host's session as its own.
- A problem is solved when the work it is about passes: a task whose attempts left a failed
  fix, in this run or an earlier one, closes its own problem when it passes, and its pass
  closes the open problems of its repository its own text matches strictly
  (`orchestrate._solve_matched`). A failure on work that strictly matches a solved problem
  of its repository makes that problem `recurring` and disproves its fixes (`Store.regressed`).
  A problem is one request's text, not its clipped title (`Store.open_problem`). `locks doctor`
  says a problem still open after its task passed (`problem-open-after-pass`), a flake fix
  that passed on one run (`flake-fix-checked-once`) and a long attempt that kept few turns
  (`turns-under-the-wall`).
- Problems and fixes are kept with their cell (`locks.cells`): the role the
  worker wore and the areas of what it changed (of its request when it changed
  nothing; tests and docs name an area only when the change holds nothing
  else). A worker's brief gets the dead ends of its task's areas, of its role,
  and the shared ones near its cell (a person wrote it, or one of its fixes was met in the task's areas or by its
  role, `cells._near`): a shared one met only in other cells stays with the session; a problem is shared when two tasks or more met it with
  no area in common, or a person wrote it down. The orchestrating session (the
  prompt hook, SessionStart) hears only the shared ones. A failure is kept and
  matched by the work's own text, a workflow step's goal and never its
  template, and a strict match counts only the words fewer than
  `store.COMMON_SHARE` of the problems hold, at least `STRICT_MIN_TERMS` of
  them. Fixes recorded before scopes get theirs once, at the first settle
  (`cells.backfill`). Notes belong to one project and are never
  read from another (a workspace is one project). A note is written by a passing worker (`learned`), by
  Haiku from a session's conversation (every few turns, when the scribe finds
  them due, before a compaction, at its end), or by a person — never by a failed
  attempt, whose facts no pass confirmed. A worker's note starts as a
  `candidate`: recalled, marked unproven in a brief, and adopted only on
  evidence. The `briefed` event keeps the notes each brief carried
  (`notes.BRIEFED`) and the candidates its trial left out (`notes.WITHHELD`): the tasks
  a candidate is recalled for alternate between carrying it and going without it
  (`notes.arms`, the `note_trials` setting; a constraint is never left out) until each
  side has `notes.NOTE_TRIAL` first tries; then `notes.weigh`, in `evolution.settle` and
  never in a GET, makes it current where the tasks that carried it passed their first try
  no less often than the ones without it, and drops it, with both rates, where they did
  worse; one with no pass after `notes.DISPROVE_AFTER` failed is dropped at once. A verdict one
  task would turn is none: where the carried arm lost by a single failed first try the arms go on to
  `NOTE_TRIAL_LONG` each; and a candidate recalled too seldom to fill them in `NOTE_TRIAL_DAYS` is left
  out no more and settled by the passes of the tasks that carried it. With
  the trial off, `notes.PROVE_AFTER` passes make it current. A later attempt's brief
  also carries the notes anchored to the files the work changed so far
  (`orchestrate._later_notes`). When the code a task touches and its words find different
  notes, the brief's order is on trial (`notes.recall_arm`, the `recall_fusion` setting,
  `briefed.recall_arm`): reciprocal rank fusion of the two lists (`notes.fused`) against
  anchored first, kept where its arm passed first tries no less often. A worker names the
  notes it relied on (`notes_used`, always asked; `[]` kept as none relied on, a block that left it
  out kept as `notes_unsaid`); only notes its brief carried are kept, on
  `attempt_finished`, and `locks stats` shows them, observed, never a verdict or a trial.
  A current note a worker or a session filed that the first briefs of `notes.IDLE_AFTER` ended
  tasks carried since it last changed, none of whose workers named it, is a candidate again
  (`notes.retry_idle`, `RETRIED`, in `evolution.settle`, only with the trial on): the trial weighs
  it by the briefs after that alone. Never dropped on those words, never a person's note or a
  constraint.
  How each attempt's worker worked is read from its own tool calls (`locks.behavior`, kept on
  `attempt_finished.behavior`): observed, never a rule's input. The independent check runs with its
  attempt marked as the check's (`orchestrate._verifier_env`), so the log keeps its calls under no
  attempt and nothing read as the worker's use (`evidence.owed`, `capability_calls`) holds them. A person's note and
  Haiku's from a conversation are current as written. Haiku files under topics that
  exist and never invents a link; without Haiku a fact is kept by its words.
  A fact that looks like a secret is never kept.
- A constraint (`notes.CONSTRAINT`) is a note of its own kind: a rule a change must meet (a
  policy, a limit a check enforces), briefed apart from the notes and as a requirement
  (`notes.constraint_lines`), every live one of the project that fits in
  `notes.CONSTRAINT_CHARS` and the rest by count, to every worker and to the independent
  verifier, where breaking one is a finding. Framing is measured, not taste: a cheap model
  ignored a policy briefed as a note and met it briefed as a constraint (`bench/memory`). It
  bounds what the work must be, never what a worker may run or change. It comes from a
  person (`locks note … --constraint`, current as written) or from a passing worker's
  `constraints`, kept only when a check of that task (locks' own, or the verifier's) failed
  before the pass, else filed as a fact; a worker's starts as a candidate and is weighed as
  any note. It has no anchor: a change to the code it was learned on does not send it to review.
- A worker's brief recalls notes by the code the task touches (the notes anchored to the files its
  request names, its surfaces, a step's diff: `touched_paths`) and by its words strictly (several in
  common, `notes.recall(strict=True)`): one word in common puts no unrelated note in a brief.
- What a worker must use is made to happen, in this order: locks does it itself where it can (the
  livespec index built, asked by locks and its answers briefed as data, `Livespec.dossier`); else the pass is gated on
  evidence locks reads (`locks.evidence`); a hint alone is the weakest. A requirement (`evidence.REQUIRED`)
  names a neighbour and when its use is owed: layout-inspector on writing work whose change touches
  rendered UI (`capabilities.renders`: a `UI_FILES` path that is no test, fixture, example, documentation
  or tool output such as a coverage report); rastro browses and owes nothing. A worker held to
  it is told so in its brief as a requirement (`brief_line`); a pass with no call of that
  neighbour's tools that went through in the task's attempts (`tool_events.ok`, never the worker's
  words) is held back (`evidence_owed`): the first time the same cell goes on in its own session told
  what to measure (`evidence.PROMPT`, as a reshape), the second it stands as a `decision`, its work
  kept. Never a climb. Whether holding work to it pays is measured: tasks handed a required
  neighbour alternate `gate` and `free` (`briefed.evidence_arm`) until each arm has `evidence.TRIAL`
  tasks the independent check read; then the gate stays only where its passes drew the check's
  blockers less often (`evidence.arm`). Every handed capability's calls are counted per attempt
  (`attempt_finished.capability_calls`), and `locks stats` shows each one handed, called and never
  called, observed. An attempt none of whose calls reached the log (`attempt_finished.tools_logged`
  false: its worker's hooks did not run) is not recorded, never silent, and `locks stats` counts it
  apart; the hooks' log is loaded into the store once before anything of the attempt is read from it
  (`habits.load_events` before `attempt_finished`), whatever the worker was handed: read before that
  load, a withheld worker's calls read as none (`doctor`: `tools-logged-off`). Tests run with
  `LOCKS_EVIDENCE=off`.
- Each CORRECT line of the TRACER flows a request names is a criterion of the independent check,
  `C1..Cn` (`tracer.criteria`; the lines that fit in half of `tracer.BRIEF_CHARS`, the rest said by
  count), added to its `criteria`: one it says is not met is a blocker (`orchestrate._correct_lines`),
  a correction like any, the late check's too; one it leaves unanswered is kept on its `verified`
  event (`tracer_unanswered`), said on the verdict card and the ending (`unchecked`), and lets the
  diff be opened: never taken as met. Before that the check is asked once more for exactly those
  lines (`orchestrate.reask_lines`, `tracer_reasked`), its two answers merged; what stays
  unanswered is what is kept.
- A method is how a kind's workers are told to work (`locks.methods`, the `methods` setting): a
  diagnosis for `debug-unclear` (reproduce, where it fails and where not, a cause shown before a
  fix), a pre-mortem for `plan` (a `Risks:` section). It is data in the brief, tried in arms per
  repository (`briefed.method_arm`) until each has `methods.TRIAL` first tries, and kept only where
  its arm passed no less often; a pre-mortem is judged by the first tries of the steps after its
  plan. A plan's risks reach the independent check of the steps after it as where to probe
  (`Plan.seen_risks`), data, never findings. Tests run with `LOCKS_METHODS=off` and
  `LOCKS_NOTE_TRIALS=off`.
- A note whose anchors were all written at commits outside the history a task starts from
  (its base, else HEAD: a worker's note about its own change, filed while its branch waits
  to be merged) is marked so in that task's brief (`notes.unmerged`), never dropped: what it
  says of the code may not be there yet.
- A note's anchor is reviewed only against this checkout's history: a change
  on a branch not merged yet is not a change. A run files what its worker learned after its
  `finished` event and the workflow's advance (a `notes_filed` event times it): no ending and no
  next step waits on that model call. Filing notes never fails a run,
  and a GET in the UI never reviews them. Tests run with `LOCKS_NOTES=off`; a
  test that wants notes turns them on and passes a fake `ask`.
- The ACP agent (`locks acp`, `locks.acp`) is a way in, never a power, like the mod: each prompt is a
  `locks run` in the session's folder with no `--allow`, `--allow-approval` or `--keep`, so a stop that
  needs a rule or an approval is the person's as anywhere, and the `mcpServers` a client sends are never
  handed to a worker (the registry is the person's locks home only; the first answer says so). stdout
  carries protocol messages alone. A `session/cancel` ends the turn (answers its prompt `cancelled`, says
  nothing more of its run) and never the task by itself: an editor sends it before the next prompt of a
  thread and waits for its answer, so the task runs on, in `/tasks` and on the board, while a prompt may still
  follow within `acp.CANCEL_WINDOW_S`; one that follows keeps it and starts its own task, one that does not
  (or a closed input) runs `locks cancel N` and kills the process group only if the run outlasts
  `acp.CANCEL_GRACE_S`. `/cancel N` is the explicit stop; a bare `/cancel` is refused, never run as a request.
  `session/load` is answered with an error that says to open a new thread (`loadSession` stays false).
  The editor gets the request's items as `plan` entries (completed only on a pass), followed by locks'
  stages (`progress.STAGES`: worker, check, independent check), each completed once a later one began,
  and the worker's files as `diff` content, read from its worktree every `acp.DIFF_EVERY_S` and from its
  kept branch once it ends. The tool call's title is `progress.line`, its content the worker's last call
  in words (`progress.words`, from the signature alone: a signature holds no argument, so it cannot name
  the file), the files written, `progress.warnings`, the stderr tail and the end of a failed check's
  output (`acp.failed_check`), all read every `acp.PROGRESS_EVERY_S` (`acp.task_look`, read only) and said
  only when they differ from what the editor was last told, never as a flood; its ending leads the
  answer with the verdict and next step (`acp.ending_text`). Reading any of it never fails a prompt.
- The mod adds a way in, never a power. Every tool runs `bin/locks`. `resume`
  runs with `--unattended`, which refuses what only a person may clear, and
  no model input ever reaches `--allow`, `--allow-approval` or `--keep`: only
  a button the person presses in the web board, or on a neighbour's page
  through `locks resume <id> --pressed`, does, and its rules are read
  from locks' account of the stop (`stops.resume_args`), never from the
  request; `--pressed` refuses any rule or approval passed with it. The board lives in the web UI only; the mod draws no copy of it. Tool schemas carry no `enum`: the handler
  reads a value and says what locks takes. The reading tools (`recall`,
  `tasks`, `note`, `overview`) turn an `ask` into an allow; `queue` and
  `resume` do only while the person delegates them (`locks config delegate`,
  on by default, read at each check; a locks that cannot say leaves the ask
  with the person). A deny stays a deny, an organization's ceiling still caps
  it, and a delegated `resume` still runs `--unattended`. Without the mod, everything still works through
  the classic hooks.
- A watcher reads, never claims: `overview` and `events` take nothing from a
  session, and an ending is delivered only to the session that sent the work. A
  supervising session hears a copy of the work it asked for when that work was sent
  with `--notify <session>` (`LOCKS_NOTIFY`, a `notify` event on the task): delivered once
  per session (`Store.claim_copies`, a `copied` event), in a notice of its own
  (`hooks.copies`), never taking the owner's ending, and giving no claim on the task.
  locks has no channel of its own to another session; sending one work is
  Claude Code's `SendMessage`.
- An ending is claimed, not just read, when it is delivered
  (`Store.claim_unreported`). The Stop hook, the next prompt and the mod's
  wake may run at the same moment, and each ending is still handed over once.
  The mod wakes a session only while no turn runs, and looks every `PULSE_MS` at the
  pulse a task's ending touches (`Store.pulse`, a stat, no process) before its full poll.
- A workflow advances on the engine, never on a session: `workflows.advance`
  runs at the end of every run (`orchestrate.run`), queues what waited on a
  step that passed, inside one write transaction, and starts the dispatcher;
  advancing never fails the run that ended, and an error is a
  `workflow_error` event. Work queued after other work (`locks run|queue add
  … --after <task>`, `locks workflow run … --after <run>`) is held in its
  options (`after.KEY`): the dispatcher never starts it, and every view that
  says why work waits says "waits for #N to pass" (`dispatch.waiting`). A
  task or run that does not exist, or whose work was dropped (failed,
  cancelled, found wrong), is refused when queued. `advance` releases it when
  that work passes (a run, when its last step does), in one write
  transaction, onto the branch it kept (a run's last step's), or the base when
  that branch is merged into it; its plan says which, and nothing is merged or
  rebased. Work that waited on work in another repository (numbers are global)
  starts on its own base, is given that checkout to read (`add_dirs`), and its
  plan names the branch kept there (`after.reads`). Work that stops short releases nothing: what waits stays held and
  says how it stopped (its ending names what waits on it), until a resume of it passes; `locks dismiss` or a
  cancel takes held work off the queue, each with its `stops.Stop`. Work that
  leaves the board unpassed takes what waits on it along (`after.abandon`): a
  dismissed task or a cancelled run cancels it, cause `after`, by locks, never
  marked reported, so its session hears of it and `locks resume N` starts it
  on the base; a superseded task hands it to the one that passed
  (`after.hand_on`), and a landed one releases it onto the base. A chain
  is released one link at a time. `--after N` is a task (`task:N`), `run:N` a
  run; work in another repository is named with it and its title. A step's
  try that `locks workflow retry` queued again is superseded by the retry
  (`stops.retried`): reported, nothing to resume. The session that started a run hears of it once —
  at its end or when a step waits on the person — and steps the engine went
  past are marked reported. A step that stops stops the run there; it goes on
  by a resume to a pass or `locks workflow retry`.
- A workflow step says how far its work is behind the branch its run started
  from, as that branch is now (`isolate.behind`): the commits it moved and the
  files both changed, kept as a `behind_base` event when the step starts and
  when the run ends, and shown in `locks workflow status`, the ending notice
  and the board. It is said, never acted on: nothing is merged or rebased.
- A workflow definition grants nothing and caps nothing: no rule, no
  approval, no capability, no dollar limit; rules, approvals, a run's check
  (`--verify`, for every step that writes and names none) and `--add-dir` are
  the person's flags on `locks workflow run`, so a definition a repository commits
  runs with what the person gives, and a `budget_usd` an older one holds is
  dropped. A bundled template is never written: a change copies it to the user
  scope.
- Work is cut where context changes hands, never by role: the phases of one
  task are one worker's (the one that builds writes its tests; the one that
  reproduces fixes). A review step of a change reads blind, as the verifier
  does: the request and the diff, never what earlier steps said of their work;
  one with no diff to read (a plan's critique) gets what came before. Other
  steps get the findings of the steps they wait on as records, beside their
  summaries. The bundled templates follow this; their review chain fixes only
  what the ledger confirms, and costs nothing more when the review is clean.
  A refuter is told whether one review or two found its rows. What the steps
  before said reaches a step quoted, as context, never as its criteria.
- A fix's later round starts from the tip the round before it kept, so the
  run's last branch holds every round (`doctor`: `fix-round-off-base`).
- A step may start as a fork of a reading step's session (`trunk`: an `explore` or `plan`
  step before it that it waits on), and only its first try: what follows goes on from its own
  attempts. A ledger's fix may fork the step that wrote the change (`trunks.WRITING_TRUNKS`:
  `feature`, `bugfix` and `tracer-slice` do), told it is its own session, and only on that step's
  model (`trunks.same_model`): another model never inherits an approach. A reading trunk reads and decides nothing for its forks; a review forks only an
  `explore`, a refute, recheck or integrate step forks none, and the independent verifier
  never runs in a fork. Claude Code records a session's system prompt on its first request
  and every resume and fork sends that record (`--system-prompt-snapshot`), so a trunk runs
  with its role and capability hints in its brief, never in its system prompt, and a fork is
  given its role, its hints, where its directory is and that it is a new worker as its first
  message (`trunks.note`), its brief after it. A trunk is queued with a worktree, where its
  session is kept; one with no session, one that did not pass, or one whose context is over
  `trunks.MAX_TOKENS` is not forked, and the step says why. Whether forking pays is
  measured, never assumed: a repository's steps alternate forked and fresh first tries until
  each arm has `trunks.TRIAL`, then forking stays only where its first tries passed no worse
  than `MARGIN` below and reached their first change in fewer calls or finished sooner
  (`trunks.arm`, the `briefed` event's `trunk_arm`, `locks stats`); the `trunk` setting
  turns it off, and tests run with `LOCKS_TRUNK=off`. `LOCKS_TRUNK_ARM=fork|fresh` forces the arm for one
  process (a benchmark's), and a forced step is left out of the trial.
- A worker's answer is refused for its form only by what its result contract
  told it (`launch.RESULT_INSTRUCTIONS`, `PARTS_INSTRUCTIONS`): a check the
  worker was never told of is a lost attempt, never a shape. Length is no form:
  a prompt reaches its worker on stdin.
- A step can run in parts. A `fanout` step names a plan step before it that it
  waits on; that plan (`kind plan`, read-only) is asked for `parts` (1 to
  `launch.MAX_PARTS`, each an id, a prompt and surfaces no other part reaches)
  and their `contract`, read strictly (`launch.read_parts`: no parts, more than
  `MAX_PARTS`, a part without surfaces beside another, surfaces that may touch,
  or an `after` that names no earlier part are `inconclusive`, never a guess).
  A part that builds on another names it (`after`, one part listed before it):
  it is queued with the others, held behind that part's task (`locks.after`),
  and starts from what that part kept once it passes; a retry that runs that
  part again moves the hold to the retry, and a retried part whose earlier part
  passed starts from its kept tip. The plan is told to read the layout and the
  interfaces the parts share, never every file: each part's worker reads its own. When the plan passes, `advance` queues one
  task per part in its transaction: the step's prompt, its inputs quoted as
  context (a part's criteria are its own prompt's; the review judges the whole
  request), the part's prompt, the contract verbatim and quoted, the part's
  surfaces, and the step's other fields. A plan of one part is the step run
  whole, as without one. Parts whose surfaces may touch (`surfaces.disjoint`,
  checked again by the engine) take the plan's pass back as `parts`: the run
  stops at the plan with the paths named. What needs a step run in parts waits
  for every part; a part that stops stops the run there, and `locks workflow
  retry` queues only the parts that did not pass. `status` and the board show
  each part under its step.
- An `integrate` step joins the branches the parts kept, on the commit they
  started from, in the plan's order, before any worker (`isolate.join`): a
  clean join whose check (the run's `--verify`) passes, or that has none,
  passes with no worker; a conflict, or a check that fails on the joined work,
  goes to a worker told which files, whose branch to merge, and the contract;
  one branch or none passes at once, and what follows starts from it. The
  review chain after it reads the combined diff, blind, as the verifier does.
  Only an integrate step waits on a step run in parts: a step starts from one
  branch, and the parts keep one each, so a definition where another step
  waits on it directly is refused.
- A blind review of a change over `workflows.AREA_REVIEW_LINES` changed lines
  that touches two or more areas (the surfaces the parts declared; a change with no
  parts is reviewed whole, since split by its top-level folders it found the same
  bugs slower and dearer, `bench/review/areas.py`) runs as one blind review per area, side by side, each
  given the whole request, its area's diff and the names of the other files
  changed. Their findings add up in the one ledger; refute, fix and recheck
  run once on it. Below the threshold it is one review. Generated files (marked
  `linguist-generated` or `-diff` in `.gitattributes`, or written by a step's
  own check) are no area and count no lines; the review is told they are
  generated, never asked to read them.
- A loop (`locks.loops`) is the person's: its definition, diary, state and the log of
  what its tools sent live in their locks home, never in a repository, and only
  `locks loop add` makes one. Each round is a workflow run started by the loop's detached
  runner (`loop-runner`), on what the round before kept when it passed; each step is told
  the goal, the mode, the person's rules and the diary's last `loops.DIARY_SHOWN` rounds
  (the rest counted), quoted as data. The metric is the last number the person's command
  prints, run by locks before the first round and after each, never a worker's word. A
  loop stops only on evidence: its target, its window, `stale` rounds that did not improve
  on the best before them (an unmeasured round improves nothing; a passed round that changed
  no file and moved nothing is an empty round, a valid outcome said in the diary, and the
  next round starts from the last one that changed files), or a round that waits on
  what only the person clears; never on money, and its rounds' cost is kept and shown.
  Variants alternate, the one with fewer measured rounds first, until each has `trial`;
  the one with the larger mean gain is adopted and recorded. A loop starts as a draft:
  a capability's `outbound.acts` tools run behind the `outbound` guard of `mcp_guard`,
  which refuses them outside a loop, in a draft, outside its `hours`, past its `per_hour`
  (counted across its workers in its log, which keeps no argument) or when they name an
  `exclude`; `locks loop act` is the person's word only. What the rules do not allow is
  listed under `loops.NEEDS` and shown when the loop stops; the session that made the
  loop hears of its stop once, with the endings (`loops.endings`). `/locks:loop` proposes
  a loop and writes nothing, and starts no worker, before the person agrees; it never
  writes the registry, a settings file or a credential.
- A worker never starts locks work (`run`, `resume`, `work`, `queue add`,
  `workflow run|retry|answer`, `investigate start`, `loop add|start|act|rm` and the
  `loop-runner` refuse under `LOCKS_WORKER_TASK`),
  and forks are off inside it (`CLAUDE_CODE_FORK_SUBAGENT=0`): what is left
  goes in its result. The main session does what fits in what it holds, sends
  to locks what earns a worker, and a judgment that needs the conversation to
  Claude Code's own fork, never to locks (`commands/orchestration.md`).
- A step chooses, asks or delegates only on records, never on a worker's words. `when` maps a
  step it needs to an outcome its ending records (`workflows.OUTCOMES`: `findings` or `clean`,
  from the blocking findings it passed with; `changed` or `unchanged`, from its changed files; or a
  question's option, `outcome`); one that does not hold skips the step (`_skip`): it passes with
  no worker, a `finished` event with `skipped` and `when_unmet`, reported, and what waits on it
  starts from the work before it. A question step (`ask`, `options`) runs no worker: its task is
  blocked from the moment it exists, its stop is `question` (`stops.PERSON_ONLY`, by `workflow`),
  its line `locks workflow answer <run> <step> <option>`; it is answered, never resumed (`locks
  resume` refuses it, a board's press offers none), and the answer (`workflows.answer`, an
  `answered` event, `finished.answer`) passes it and advances the run. A sub-workflow step
  (`workflow`, `with`) starts a run of the workflow it names on the commit the step would start
  from, with this run's flags, and waits for it as work held `--after` that run does: the
  dispatcher never starts it, the child's end passes it with the branch its last step kept and
  its cost (`_finish_parent`, before anything held is released), and the child's endings are
  reported, so the session hears of the run that holds it. A child that stops stops the run
  there; cancelling the run cancels the child, and a cancelled child cancels the step. The
  workflows a run calls are read and frozen in its options when it starts (`subflows`): one
  missing, lacking an input a check does not fill, calling itself, or nesting past
  `SUBFLOW_DEPTH` refuses the run before anything is queued.
- `locks workflow render` draws a definition (or a run's own copy, each step's state beside its
  tone) as a Mermaid flowchart, text only; `--markdown` fences it for a README or a pull request.
  `locks workflow show` prints a definition's revision (`workflows.revision`: what a change builds
  on, the pending revision while one waits), and a change passed `--expect` it is refused once
  another landed since (`_expected`); the editor's change carries the revision it opened.
- A run runs the copy of its definition it started with, never the file. A
  definition an active run uses (in any project) is held: a change becomes a
  pending revision that applies when the last such run ends or is cancelled,
  unless a person passes `--now`; removing it needs `--force`.
- A definition may name paths its repository must hold (`requires`, from the checkout's top, never absolute or
  upward): `workflows.prepare` refuses the run, `--dry-run` included, while one is absent. `tracer-slice` requires
  `docs/tracer`. A feature that writes a new event kind gets its line in `coverage.FEATURES`.
- A step's `verify` gets the run's inputs as its prompt does (never a brace
  after `$`); one left holding a `{name}` refuses the run before it starts,
  `--dry-run` included.
- A step's `memory` is read when the step is queued, never written. A note
  comes only from the run's own project; a run with a note or problem that
  cannot be read there does not start, a topic with no note yet is said when
  it starts (the step goes without it), and a note gone since says so in the
  brief. `locks workflow run --dry-run` checks all of it and queues nothing.
- A capability comes by kind (`when`), after a failure (`after_failure`), or
  by what the work touches (`touches`: file patterns or `"ui"`): the files the
  request names, its surfaces, what its work changed so far, and a workflow
  reading step's diff (`Options.touches`). Reading work gets by touch only a
  capability that `measures` (never changes anything), and the independent
  verifier gets every measuring one its change touches, even when the work had
  none. The plan's hint says when it came by touch.
- The capability registry is read from the user's locks home only. A
  repository must never be able to add an MCP server by committing a file.
  Installed neighbours (`plugins.KNOWN`) are read from the person's Claude
  Code plugin cache or PATH, never a repository; a registry entry of the same
  name wins, and `locks config plugins off` leaves them out. Every tool a
  neighbour's hint names is in its `tools`. A registry entry that shadows a
  neighbour and sets neither `touches` nor `measures` takes the neighbour's
  (`capabilities._inherit`); what a registry shadows or fetches unpinned is
  said once at SessionStart, never refused. A neighbour that drives Playwright's Chromium
  (`Neighbour.browser`: layout-inspector, rastro) is not handed on a machine with none
  (`plugins.browser_found`: `LAYOUT_INSPECTOR_CHROMIUM`, `PLAYWRIGHT_BROWSERS_PATH` or the platform's
  cache), and the plan says why with the install line; `LOCKS_BROWSER_CHECK=off` skips the look,
  as the tests do. rastro runs with `--routines=off`
  and behind `mcp_guard`, which enforces what its hint asks: no browser
  profile, no saved routine, writes and uploads to loopback hosts only, every
  call in its own session. A policy that matters is enforced there, never only
  asked of the worker in its prompt.
- A neighbour's own manifest (`locks.manifests`) brings words, never policy: its hint, livespec's
  hints per kind, its tools' descriptions and what they measure override the built-in text
  (`plugins.KNOWN`, `livespec._HINTS`, `tracer.MEASURED_BY`, which stay as the fallback); every
  `mcp_guard` policy, rastro's `--routines=off`, `Neighbour.measures`, `when`, `touches`,
  `extra_args` and the server command are locks' code alone. It is read from the person's plugin
  cache only (newest version through `plugins.versions`), never from a repository; a block of
  another format or shape is ignored whole, and a hint naming a tool its own `tools` lacks is
  dropped.
- A role is not a subagent: it adds identity and instructions, never a dial.
  It names no model, no effort and no tool to add; it may take tools away,
  carry or point at skills, and name capabilities, which come only from the
  person's registry and adopted neighbours. The bundled writing roles carry no
  `engineering-baseline` (the reading roles and the verifier do): the same hidden tests passed 26% and 39%
  sooner without it (`bench/speed/versus.py`, AA; its `-baseline` arm puts it back). Its text goes in
  `--append-system-prompt`, never `--agent`, which would replace Claude
  Code's system prompt and locks' tool list.
- Roles and teams live in the person's locks home and in locks itself, never
  in a repository, and so does which team a project uses: a repository must
  not choose what its workers are told by committing a file. A role or team
  that fails its checks is refused with its reason and costs only itself; a
  role of a bundled name shadows it and says so.
- A project's team bounds its kinds: a task is classified only into the kinds
  the team covers, the nearest kind of the same nature (reading or writing)
  when it would be another. A tag or `--kind` is narrowed the same way and
  says so. A team left with no role serves as the bundled one, and says so.
- A pickup is split by what it waited on, from the task's own records
  (`orchestrate.pickup_causes`, the `pickup_*_s` of its `finished` event): the spans a dispatch
  decision said wait, the spans the usage limit held the queue, and the rest a worker: the
  person's ceiling when the level stood at it (`dispatched.ceiling`, a choice), else the
  machine's level. The parts add up to the pickup; `locks stats` shows them, and a pickup from
  before they were kept as not told. Compaction advice the orchestrating session chose not to
  take is counted apart from a compaction that did not run.
- A request about a README in a repository holding several is warned (`orchestrate.readmes`):
  which one is the session's to say. Notes filed from a conversation are written in English,
  domain words and quotes kept as the person wrote them; the notes' index says how many wait
  for review.
- The analysis is read, never written, and never by a model. A run's phases
  are kept on its events as it runs (`plan_s`, `queued_s`, `since_start_s`,
  each attempt's `wall_s`, `check_s`, `verifier_s`, the waits for a network
  `wait_s`, the run's `wall_s`), and on its ending what came before it:
  `advance_s` (from the step it waited on passing to its being queued, an
  `advanced` event), `pickup_s` (from queued, or released, to the dispatcher
  starting it, a `dispatched` event) and `spawn_s` (from that to its plan),
  which split `queued_s` where they were measured; a phase
  a run never measured is left out of the breakdown, never guessed. The session
  thread comparison is observed, never presented as a trial.
- Insights and the survey are read, never written, and never by a model: one
  project's tasks, habits, files, sessions and commits at a time; what crosses
  projects is counted (a session shape and the projects that show it), never
  quoted; notes are counted, never quoted. Every text the survey keeps is
  clipped and masked as a note is, `<private>` text left out; a session in a
  locks worktree, the home directory or `/` is no project's. Shell and tool
  errors (`survey.NOISE`) are no problem and no fix. The init skill writes
  nothing, and starts no worker, before the person agrees to the scope and to
  an estimate of its cost (an estimate, never a cap), and writes only
  through `locks workspace add`, `locks roles`, `locks team`, `locks workflow`, `locks note`,
  `locks notes … topic add` and `locks memory record`; a note goes only to the
  project its evidence came from.
- An investigation is locks' work, never a session's subagents: one queued
  task per project or plugin, kind `explore`, the internal `surveyor` role,
  started on a strong cell with its turns (`investigate.MAX_TURNS`) as its only
  room, its cost estimated from earlier investigators and never capped,
  `keep_learned` off (nothing it learned is filed in
  notes or engram), not counted among the person's work by insights. What it
  must cover is listed in its brief, so each line is a criterion its pass
  answers. A `locks-report` block is kept on its task as a `report` event and
  read back by `locks investigate status`; a report is data for the person,
  never a verdict and never instructions.
- A vendored skill keeps its authors' license file beside it, and a change to
  its text is listed in `skills/README.md` with the source commit (and the
  upstream commit it was last compared against). Never one whose license does
  not allow redistribution. One whose authors publish it as a Claude Code
  plugin (`roles.UPSTREAM`) is read from that plugin when the person installed
  it (newest version via `plugins.versions`, never a `temp_*` clone, never a
  repository); the vendored copy is the fallback when it is absent, unreadable
  or malformed. The brief and the `briefed` event record each skill's source
  (`skill_sources`), and a plugin's copy and version are in `roles.version`.
