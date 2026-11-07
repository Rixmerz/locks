# Changelog

> The project was named cauce until it was renamed Locks; the entries of released versions below keep the old name. <!-- legacy-name: history -->

## [Unreleased]

## [0.90.0] - 2026-11-07

### Added
- A writing task's start is predicted from the repository's record of its shape (`locks.predict`, the
  `predict` setting, on by default): its kind, size, risk tier and whether it reaches several areas. Where
  tasks of that shape passed their first tries at the start cell under half the time over four or more,
  and the climbs from that start to the rung above rescued at least half of them over two or more, the
  task starts on the rung above and the plan says why. The backtest on the record is the rule: a rung that
  did not rescue the shape's failures predicts nothing. Only a raise, never a pinned or probation start.
  The `planned` event keeps the prediction with its evidence, and `locks stats` shows how the first tries
  of predicted starts went per shape.

### Changed
- An unclear bug (`debug-unclear`) starts at sonnet/high and climbs to opus/high: on `bench/speed/methods.py`
  sonnet/high with the diagnosis method fixed the planted bug 3 times in 3 at 38 s and $0.09 a run, as
  opus/high did at 69 s and $0.27 (W in `bench/speed/README.md`). The bench's judge now lets a worker add a
  test for the bug: a run that fixed it and added a regression test was judged wrong before.

### Fixed
- A task whose try won a race opened a problem on its pass: the other try, stopped because the pass came
  first, was read as a failure of the work (`_remember`'s "a pass after failures"). A try stopped so found
  nothing and is no failure (`escalate.Attempt.raced`); one that failed its own check before the pass
  still is. `locks doctor` says a problem a pass opened beside a stopped try (`problem-opened-by-a-race`).

## [0.89.0] - 2026-11-07

### Fixed
- `attempt_finished.tools_logged` was read before the hooks' log was loaded into the store when the worker
  was handed no capability (a withheld livespec, a reading kind with none): its calls read as none, `locks
  stats` counted the attempt as not recorded, and the evidence gate would have held such a pass back. The
  log is loaded once before anything of the attempt is read. `locks doctor` says one that still reads so
  while the store holds its calls (`tools-logged-off`).

### Changed
- A routine review starts at sonnet/high and climbs to opus/high: on `bench/review`'s change sonnet/medium
  caught 20 of 42 planted bugs where sonnet/high caught 27, 15 s apart (`effort.py`).
- A trial short of its arms in a repository takes the verdict of every repository once those arms are full
  (`arms.pick`'s `pooled`; the primer, the evidence gate, the trunk, livespec's handing, the methods and
  the recall order): a repository with few tasks no longer pays for both arms for months. Its own arms
  decide alone once full; an early loss is never pooled. The brief's `*_why` says when a verdict was pooled.

### Added
- `locks stats` shows every climb of a ladder by rung (`climbs by rung`: kind, from and to cell, the move,
  how many, and how many of the attempts they started passed), observed: the record a rung is judged by.
- A turn that read 40,000 characters or more of tool output with the session's own tools is said at the
  next prompt, beside the route's advice, with what an explore worker here passes at in time and cost
  (observed): what the session reads itself stays in the context every later turn re-reads. The scribe
  counts each turn's reading (`state["read"]`); the `routed` event keeps it (`read_chars`, `read_said`),
  and `locks stats` says how often locks work followed (`heavy reads said`).

## [0.88.0] - 2026-11-07

### Added
- **livespec handed to a worker, on trial** (`locks.handing`, the `livespec_trial` setting, on by
  default): six workers in a row were handed livespec and none called it, and what a handed worker
  carries (a server started with it, nine tools' descriptions in every turn, the orientation lines)
  costs every attempt. A repository's tasks of each nature (reading, writing) now alternate between
  `hand` and `withhold` (`briefed.livespec_arm`, `<nature>:<arm>`; a resume keeps its first brief's
  arm) until each arm has 6 first tries; then workers are handed it only where the handed ones passed
  their first tries no less often and no slower than `arms.SLOWER` times the other arm's median. A
  withheld worker gets no server, hint or orientation; the code map in its brief, the size estimate
  and what the change reaches for the independent check are locks' own reads of the index and stay,
  and the independent check keeps the index whatever the arm. `locks stats` shows both arms per
  nature and where each stands.

## [0.87.0] - 2026-11-07

### Changed
- **A trial's winner must not be slower**: the two-arm trials that share `arms.pick` (a method, the
  recall order) decided by pass-first alone, so an arm that passed as often but took longer won.
  Each arm's passing first tries are timed (`tally`'s `pass_p50_s`, from `TIME_SAMPLES` or more),
  and the change loses where its median is over `arms.SLOWER` times the other arm's; the verdict
  says both. A pre-mortem's steps are untimed (they come after the plan), so it decides as before.

## [0.86.0] - 2026-11-07

### Changed
- **livespec raises a start only on a symbol the request names**: the code map's best full-text
  matches (the request's words against every symbol's text) could name a widely used or critical
  symbol the work never touches, and a rung was added for it (a test task on `arms.py` started at
  sonnet/high because "state" matched a 27-caller function elsewhere). The map still shows what the
  words found; only a symbol the request names outright (`_matching_symbols`' `named`) raises the
  start or marks a critical spec.
- **A clause may open with where the change goes**: "In src/x.py add a function … Add a test for it"
  read as `test` work, since no verb of change opened a clause before the tests were mentioned
  (`classify._WHERE`); the first change is the function, and the tests are how it proves itself.

## [0.85.1] - 2026-11-07
- **`locks forensics` reads the cache share from the workers' attempts** when no session usage was
  recorded (a session's usage comes from its transcript, kept only with the trace on): every
  `attempt_finished` keeps its input, cache-read and cache-write tokens, so the share is shown per
  model from them (`source: attempts`) instead of "no usage recorded".

### Fixed
- **A check in a worktree tests the work, not the checkout**: a Python project installed editable
  (`uv pip install -e .`) has its `.venv` import the project's own package from the checkout, and a
  worker's worktree links that `.venv`, so the worker's tests and locks' check imported the
  checkout's code and never the worker's change (preflight only warned). `runtime.env_for` now puts
  the directory that holds the package here first on `PYTHONPATH` for the worker, the check and the
  base's run (`runtime.package_elsewhere`), and the preflight line says so.
- **A linked dependency is not a written file**: the board, `locks tasks`, `locks peek --stat` and the
  ACP diff listed `.venv` as written by every worker (a `.venv/` ignore rule matches a directory, not
  the link the worktree holds). The links the attempt recorded are left out (`progress.changes`).
- **A workflow that runs on no change is no job of CI**: the plan's "CI also runs" warning named the
  jobs of scheduled, manual and chained workflows (a weekly pin, a release) as checks the task's
  check leaves out. Only a workflow triggered by a push, a pull request or a merge group is read
  (`ci.checks_changes`, `CHANGE_TRIGGERS`).

## [0.85.0] - 2026-11-07

### Changed
- **Trials no longer move in lockstep**: every two-arm trial (a method, the primer, a trunk, the
  evidence gate, a role's lessons, the recall order) broke a tie by the task's parity and then kept
  the arms level, so trials that ran over the same tasks put the same tasks in every "with" arm and
  read each other's effect as their own. The side is now the hash of the trial, the repository and
  the task (`arms.side`), and the arm with fewer tasks goes next only once they are `arms.BALANCE`
  apart. Deterministic, no clock, no model; the `briefed` event's arm fields show it.
- **A pass nobody read moves no start cell cheaper**: a first try the independent check's sampling
  let through (`verifier_sampled`) counted as a first-try pass, so a cheap cell looked better the less
  it was checked and the calibration chose it sooner. `Ended.unread` marks it, `standards.rates(read=True)`
  leaves it out, and `start_cell` moves to a cheaper rung only on first tries that were read (the
  change's evidence keeps `read` and `held`); a move up reads every try as before.
- **A linter's findings no longer turn its habit off**: an installed habit counted every non-zero
  exit as a failure of the hook, so three edits in a row with something to fix (the very runs it
  is for) disabled it. A command that exits non-zero with output has *found* something: the session
  hears it as before, the run is counted as `caught` (`locks habits status` shows it beside the
  runs), and the streak resets. Only a command that cannot start (exit 126 or 127), hangs, or runs
  past `habits.SLOW_SECONDS` breaks the hook, and three breaks in a row turn it off.

### Added
- **A reading worker's answer is kept whole**: a worker of a reading kind (explore, a review, a plan,
  chat) is told to put its whole answer in a `locks-report` block (`launch.REPORT_INSTRUCTIONS`),
  which was already parsed and kept as a `report` event and shown only for investigations. The
  result contract's `summary` is one or two sentences, and the tables and lists a reading task was
  asked for were dropped with the rest of the prose. `locks show N` prints the report whole, the
  ending notice says it is kept, and the UI's task detail carries it (`report`). The surveyor keeps
  its own JSON report.
- **`attempt_finished.tools_logged`**: whether any tool call of the attempt reached the log. A worker
  whose hooks did not run (no locks plugin installed where it ran) logs none, and its
  `capability_calls` zeros, its missing `behavior` and `livespec_calls: {}` were read as "never
  called". `locks stats` counts such attempts apart ("not recorded") instead of as silent.
- **Habits a repository's tool files declare**: a linter, formatter or type checker a repository
  declares (`[tool.ruff]` or `[tool.mypy]` in pyproject.toml, eslint, prettier, typescript or biome
  in package.json, deno.json, Cargo.toml, go.mod: `habits.DECLARED`) is offered as a habit for each
  file type the repository tracks, before any session repeats the sequence (a mined habit needed
  three runs in two sessions). It is listed by `locks habits list` as "declared by <file>", reviewed
  by the same read-only worker (which finds the command and times it) and vetted by locks before it
  is installed, as a mined one is; a habit a session already showed is the mined one.

### Fixed
- **A habits review the dispatcher ran was never applied**: `habits.apply_reviews` looked for reviews
  by the `queue` source they were created with, and a task's source becomes `locks` once it runs, so
  the vetted install from `evolution.settle` never happened (only an ending delivered to a session
  applied it). Reviews are found by their options now. A review whose verdict locks cannot read
  (`install <id>: `<command>`` or `skip <id>: <why>`) is a `habit_refused` event that says so, never
  silence; a review of one habit has no criteria (a list is two items or more), so the verdicts are
  read from its `locks-report` block and its evidence too, and the brief asks for them there.
  `locks doctor` says a passed review nothing applied (`habit-review-unapplied`).
- **A runner named by its path is the habit's program**: `.venv/bin/python -m ruff check` (how a
  project's own docs run it) was refused as not running `ruff`; a runner is read by its name wherever
  it is.
- **An older install a live session still runs from is no longer offered for removal**: SessionStart
  named every older install with an `rm -rf` line, and removing one a session started before the
  update still ran its hooks from made that session's next hook fail to start (after a compaction,
  the prompt it carried was lost). Each session's last hook time is kept (`link.LOADED_AT`); a
  version one ran from within `link.LIVE_S` is named as still loaded, with "restart or
  `/reload-plugins` first", and left out of the removal line.

### Docs
- **Role polymorphism** in the README: the phases a worker takes on beside its role (its tests, the
  fix after its reproduction, a spec, a neighbour's measure, its own continuation, a trunk's fork),
  and the ones that always go to another worker (judging the work, a new model).

## [0.84.0] - 2026-11-06

### Added
- **`locks cells`**: per kind of work, the model and effort locks starts it on in this repository
  (a calibrated standard, else the ladder's first rung), each cell's first tries here, and the
  cheapest cell proven by them. For the session that delegates: which model fits which work,
  measured, for its own subagents and for knowing what a kind costs. `locks guide models` says how
  to read it; locks still chooses the cell of every task it runs.

### Fixed
- **Habit reviews that answered nothing are tried again**: a habit was marked reviewed when its
  review was queued, so a review that died or was cancelled left it unreviewed forever. A mark now
  keeps its review task and stands only while that task is queued, running or done.
- **A habit is reviewed again when the repository's tools change**: a review records a fingerprint of
  the files that say which tools a repository has (`habits.TOOL_FILES`); a habit not installed is
  reviewed again once they change (a linter added later). An installed habit never is.

## [0.83.3] - 2026-11-06

### Fixed
- **Preflight reads `python -m X`**: a check whose interpreter cannot import its module (`No module
  named pytest`) stops before a worker is spent, with the line that fixes it (the `.venv`'s python when
  it has one). A module the request names, or the worker installs, is said and never a stop.
- **Autosplit pieces run side by side when they can**: items naming different files of one folder no
  longer all got that folder as surfaces, which held each behind the other. Pieces whose folders
  collide go without surfaces and the dispatcher judges them by the files each names.

## [0.83.2] - 2026-11-06

- `/locks:insight`'s frontmatter is YAML a strict reader parses (its description held an unquoted
  `: `, which the Agent Skills spec's validator refuses), and `tests/test_plugin.py` checks every
  skill locks ships against the spec as anthropics/skills' validator does.
- Evals for `/locks:insight` and `/locks:init` in `skill-creator`'s format (`evals/evals.json`),
  with how to run them in `skills/insight/evals/README.md`. Run on the insight skill: its report
  now leads with what blocks, then a verdict per release claim; one false causal claim found in
  the first run (that locks' check had a test runner the task's `--verify` lacked) is addressed by
  a known mechanism the skill now names, and the second run held it.
- Tests: the `kicks` fixture also stops `dispatch.start`, so no real dispatcher starts under
  `LOCKS_TEST_ALL_ON=1` (autowork on). `locks queue add` started one, which took queued tasks the
  test was reading: `test_after.py` failed in about 1 of 4 parallel all-on runs, 0 of 12 after.

## [0.83.1] - 2026-11-06

- With the trace on, a worker that died keeps its transcript beside the trace, as one whose
  attempt ended does: the sweep that reads what a dead attempt cost copies it
  (`flow._keep_lost_transcript`). Before, a worker killed by a restart left only its tool-call
  lines, so why it never called livespec could not be read.
- `scripts/smoke.py`: one real task on the checkout's locks (a throwaway repository, a real
  worker, its own locks home with the trace on), checked for a first-try pass, the kept branch
  and the worker's transcript. Run before every release; real money (cents).
- `/locks:insight` first judges the last releases' claims against their records (worked, no
  effect, regressed, not exercised, too few), cites a record and a count for every claim,
  labels a cause without code or a quoted brief as a hypothesis, and searches locks' code and
  rules before proposing what may already exist.

## [0.83.0] - 2026-11-05

- A request that asks for its change everywhere ("everywhere", "all screens", "en todas las
  pantallas") is estimated larger (`points.sweeping`): two builds that asked for it ran out of
  turns on their first try.
- A task of 8 points or more starts on the turns an earlier task of its kind here needed after it
  outgrew them, from one such task instead of two.
- `autosplit` (on by default): a build `locks run` or `locks queue add` reads as several tasks is
  split and its pieces queued, instead of stopping with `EXIT_SPLIT`; `--whole` still runs it as one.
- layout-inspector and rastro are not handed to a worker on a machine with no Playwright Chromium:
  every call would fail and cost an attempt. The plan says so with the install line.

## [0.82.0] - 2026-11-05

- A compaction the orchestrating session asked for is no longer buried by
  locks' own later decisions: it is the one delivered, and while it runs
  locks advises nothing over it. locks' own advice reaches a session at most
  once per prompt of the person's, even from 90% of the context, where it used
  to repeat with every notice (three in 30 seconds in a field report). A
  doctor rule, `advised-again-in-a-prompt`, finds it if it comes back.
- A change of `risk.WIDE_FILES` (40) or more code files is high risk, so the
  independent check reads it: a change of 86 files that removed a feature
  passed as medium with none.
- The `briefed` event says whether livespec's index describes the worker's
  directory or the person's checkout (`livespec_index_of`), so a worker that
  never calls livespec can be read against it.
- The orchestration skill and the dispatch guide say never to background
  `locks` with a trailing `&`: its child dies with the call's shell.

## [0.81.0] - 2026-11-05

- A supervising session hears a copy of the endings of work it asked other
  sessions to send: `locks run` and `locks queue add` take `--notify SESSION`
  (repeatable); the Stop hook, the next prompt and `locks endings` deliver the
  copy once, apart from the owner's ending, which the owner still claims.
  `/locks:watch` says how to ask for it.
- locks knows layout-inspector's `check_design_system` (0.6) without reading
  its manifest, and the independent check is told it measures contrast and
  design-system tokens; the plan line for an interface change with nothing
  that measures it gives the install command.

## [0.80.0] - 2026-11-05

### Fixed

- **An attempt whose worker died keeps its number**: the next attempt was numbered from the
  attempts kept, so it took the dead one's number and read its tool calls as its own. A field
  task counted 133 calls in 8 turns, the calls of three dead runs and the one that passed. The
  next attempt now takes a number past every attempt started (`Store.next_attempt`).
- **What a dead worker spent is kept**: the sweep reads the dead attempt's cost from its
  worker's transcript, keeps it as an `attempt_lost` event and adds it to the task's cost, and
  `locks stats` counts it. A cost it cannot read is kept as unknown, never as $0.

### Changed

- **`notes_used` is recorded as said**: `[]` when the worker said no note helped, and
  `notes_unsaid` when its result left the field out, so `locks stats` no longer reads a missing
  answer as "relied on none". The result contract asks for the field always.

## [0.79.0] - 2026-11-05

### Fixed

- **A resume a session's shell started is no longer cancelled by that shell's timeout**: the
  timeout's SIGTERM reached the resume, which read it as `locks cancel` and stopped the task it
  had just started. In a Claude Code session's shell, `locks resume` now runs in a process group
  of its own and follows its log; only `locks cancel` stops it.
- **A full disk gives back what idle worktrees rebuild anyway**: build output and installed
  dependencies (`target/`, `node_modules/`, `.venv/`, …) that git ignores, in the worktrees of
  tasks neither running nor queued, go first when the dispatcher meets a full disk and at
  SessionStart under 2 GB free. A field run lost a worker to 17 GB of such folders.

### Added

- **`locks stats` sets each livespec tool offered beside called** (`briefed.livespec_tools`
  against the calls): it says whether workers leave the index alone because they were offered
  nothing or because they were offered tools and did not call them.
- **A current note every brief carries and no worker names goes back on trial**: after 8 ended
  tasks carried it with none naming it as relied on, it is a candidate again and the trial weighs
  it against its absence on the briefs after that. A person's note and a constraint never are.

## [0.78.0] - 2026-11-05

### Changed

- **A worker gets only the livespec tools its brief names**: its server runs behind `mcp_guard`, whose
  `tools/list` shows those alone and refuses a call to any other, so the descriptions of tools it never
  calls are no longer context on every turn; the `briefed` event keeps the list (`livespec_tools`).
- **No `git_diff_impact` for a worker or a review**: it read the diff of the checkout the index describes,
  not the worktree the worker edits, so it measured a change that was not the work. What a change reaches
  is asked by locks itself and briefed to the independent check, as before. The hint says the index is the
  base commit, not the worker's edits.

- **`/locks:insight` looks for causes, not counts**: each gap is traced to what the worker was really
  given and the locks code that built it (or marked a hypothesis), a missing field is told apart from a
  zero, and every proposal is checked against locks' own rules before it is made.

## [0.77.0] - 2026-11-05

### Fixed
- A language's rules skill a role carries whole reaches only a repository that holds that language's files, as an on-demand one already did: a Rust repository's briefs no longer carry eight other languages' rules in their system prompt.
- A worker that died while nobody could see it (a container restart: no worker started since) is resumed on its own past the six-hour window, up to three days; an automatic resume that died before its first attempt no longer spends the one resume a death gets.

### Added
- The `briefed` event keeps each adopted neighbour's state (livespec present, absent, stale, with why), so a brief without livespec's orientation says why.

## [0.76.0] - 2026-11-04

### Fixed
- A worker whose process died was noticed only at the next turn's end, so a task could show running for close to an hour with nothing behind it. `locks board` and `locks tasks` now sweep it when its process is gone, and resume it as a turn's end would. The mod's status line polls the board, so this happens within seconds.
- A worker's brief no longer carries shared dead ends that only other cells met (another phase's docs and security fixes on an implement task about queries). Those still reach the orchestrating session.

### Added
- With the trace on, a worker's own transcript is kept beside the traces when its attempt ends, so its tools, the notes it read and its livespec calls can be audited.
- An automatic resume's `auto_resumed` event says how far the work it goes on from is behind the trunk. The work itself is not rebased.

## [0.75.0] - 2026-11-04

### Changed
- The UI's look is a canal lock's: harbour navy and brass over warm paper (deep navy and brass in dark mode), a serif wordmark, models as shades of navy; the state tones keep Radix Colors' scales and the kind hues stay as they were.
- A new logo, a lock chamber (two levels of water and the gate between them), in the header, the favicon and the README; the README says what the name means and its screenshots show the new look.

## [0.74.0] - 2026-11-04

### Added
- The board opens with a workshop: one pixel-art scene per running task, drawn from its phase, its stage and a quiet worker (reading, writing, locks' check, the independent check, no tool call for a while), with the same words beside it; still under reduced motion. Its terminal shows the chosen worker's tool calls as they happen (tool, signature, whether it went through) and the diff its worktree holds so far, read every 5 s while open, with the request's items and the newest call in view. Each scene says the worker's last call in words, a bar of its time against tasks like it here, and its own colour; a line above counts the workers by state. With `trace` on, the terminal shows each call's command and output; an office view puts every worker in one room with the queue at the door; and desktop notices (on a click) say when a worker goes quiet or ends.

### Changed
- The UI's base tones are Radix Colors' sage, teal, amber, tomato, grass and slate scales, light and dark; the kind hues stay as they were.

## [0.73.1] - 2026-11-04

### Fixed
- A prompt's strict note recall counts only words fewer than a fifth of the project's notes hold, so its common vocabulary no longer brings unrelated notes (#127).
- `locks footprint` names briefs written before 0.71 as such instead of `?` (#127).

## [0.73.0] - 2026-11-04

### Changed

- **A new model finishes work that only locks' check failed on** (#124): an attempt that changed files and failed locks' own check (a `code_bug` with the check's output, no blockers of the independent check) is kept in the worktree when the climb goes to another model, and its brief says to start from it (`work_carried`). A wrong approach, a repeated answer or the independent check's blockers still start from the base.
- **Routing advice is measured** (#124): `locks stats` says, per route, how often the session sent locks work after it, the evidence the advice is weighed by.
- **Notes workers relied on are recalled first** (#123): when recall has more notes than a brief carries, one that workers here named as relied on at least twice takes the place of one never named.

### Fixed

- Tests for a stray path that keeps an environment failure and the check's output, and for stopping a worker's container stack (#119).

## [0.72.0] - 2026-11-03

### Fixed

- **A check whose container runtime is not up could not run** (#119): `Sail is not running.`, `Docker or Podman is not running.`, `Cannot connect to the Docker daemon`, `service "…" is not running` and `no configuration file provided` are `environment`, never a `code_bug`.
- **A stray path no longer hides how the check failed** (#119): an attempt that changed a file outside its surfaces keeps its `environment` class, or its `code_bug` and the check's output, with the stray paths added to its evidence. Only a pass or a failure with nothing to show reads as `approach`.
- **Composer's `vendor/`** is a dependency folder, linked into a worktree and checked against `composer.lock` as `node_modules` is (#119).
- **Container stacks a worker starts are stopped** when its task ends (the Compose project named after the worktree, a `stacks_stopped` event), and a writing brief in a worktree with a compose file says not to start one (#119).
- **Surfaces are read from inside the repository** (#123): in a folder of several repositories, `db/**` allows `api/db/…`, and a leading `./` is ignored. A dependency folder (`node_modules/`, `vendor/`) is never offered as a surface to widen.
- **A capability about some files comes after a failure only to work that touches them** (#123): an API task no longer gets `layout-inspector` on its second attempt.
- **An unreadable livespec index is rebuilt** before the worker starts, as a missing one is (#123).
- **A resume hears what changed since its last attempt** (#123): the project's notes filed since then, and an instruction passed with `locks resume N -` on stdin, quoted as context.
- **The reset to the base before a new model** is told as locks' own move, never a spec point to report (#124).

### Changed

- **Compaction advice** (#124): from 90% of the context it is `now` whatever else holds it back, and a "not now" is recorded at most once in ten minutes per session.
- **Routing** (#124): a small request that leans on the conversation (a hotfix, an incident, a diagnosis or a decision just reached) stays with the session.
- **Briefs offer only the rules skills of languages the repository tracks** (#123), so a JavaScript repository's workers no longer read a list of C++, Lua or Ruby rules.
- **A plan's parts** are told to hold in their surfaces the existing tests their change will break, never a dependency folder (#123).
- **The plan says when a request's length alone adds to its size**, and `locks run --dry-run` runs the check's preflight (#118).
- **An ending says what the session cost so far**, its own turns apart from the work it sent locks (#118).
- **The orchestration skill** says where a check runs: a check bound to the checkout needs `--no-isolate`, and the environment is described as the worker finds it (#119).

## [0.71.0] - 2026-11-03

### Added

- **`locks forensics`**: where the money, the cache and the friction went, read from a copy of the store (a SQLite backup, or `--db`). It reports spend by kind and cell, with the dollars of attempts that did not pass; the share of input the cache served, by model; tool calls that failed or were repeated back to back with the same arguments; brief growth; the notes-fork trial; and what the trace shows. Every figure is observed. Idea taken from hermes-agent's post-mortem harness.
- **`locks footprint`**: every `briefed` event records its footprint (context and system characters, skills carried, the release). The report gives the median per kind and release, and names a release that made a kind's brief 1.2 times heavier.
- **`locks skills`**: how the skills roles carry are used (briefs, pass-first, last carried), and their state: `active`, `stale`, `dormant`, or `pinned` with `--pin`. A dormant skill is only reported; locks never moves or deletes one.
- **`notes_fork`** (off by default): a session's notes are filed by a fork of the session, which already holds the conversation and may read it from the cache, tried in turns against Haiku per project. It is kept where it keeps no fewer facts per filing; its cost is recorded beside it.
- **`bench/ab/traps.py`**: an A/B of two locks checkouts on five trap tasks, one waste class each, judged by the store's records and a hidden check. Real money: run it on purpose.
- `docs/observability.md` describes all of this; the `insight` skill reads it.

## [0.70.0] - 2026-11-03

### Added

- **An observability trace, opt-in** (the `trace` option, off by default). Every hook event of a session (`hook:<Event>`, PreToolUse on every tool, SubagentStop and Notification included), every tool call with its input and output (`tool:<name>`, livespec's MCP tools marked `livespec_tool`), the context a locks hook injected (`inject:<Event>`) and every event locks recorded on the session's tasks (`locks:<event>`: the plan, the brief and the notes it carried, each attempt and its livespec calls) go to `traces/<session>.jsonl` in the locks home, with the session's transcript copied beside it at each Stop for its tokens. Secrets are masked. `locks trace [session] [--raw --tag …] [--json]` reads it, and the new `insight` skill audits locks against it and against `stats`, `evolution`, `trust` and `doctor`.

## [0.69.2] - 2026-11-02

### Fixed

- **The board loads again** ([#117](https://github.com/Rixmerz/locks/issues/117)). `analysis.js` closed one parenthesis too many in `behaviorSection`, so the browser refused the module (`Unexpected token ')'`) and, with it, every module `main.js` imports: the page stayed blank with no project and no task, though the API answered. A test now runs `node --check` on every script of the UI, so a syntax error there fails the suite instead of shipping.

## [0.69.1] - 2026-11-01

### Changed

- **The repository is `Rixmerz/locks`.** The plugin manifest points there; GitHub redirects the old
  address. `scripts/rename_to_locks.py` rewrites the repository's name too, so `--check` no longer
  keeps it.

### Fixed

- **A change with nothing to render owes no layout-inspector measurement** ([#115](https://github.com/Rixmerz/locks/issues/115)). A test fixture of type `.tsx`, a coverage report's HTML, an example, documentation or build output matched the UI patterns, so a feature in a repository with no UI was held back and stopped as a decision. Only a path that is rendered UI (`capabilities.renders`) makes the evidence owed now.

## [0.69.0] - 2026-11-01

### Changed

- **Renamed to Locks.** The project was cauce; it is Locks now, after a canal lock: work climbs <!-- legacy-name -->
  a ladder of model cells one level at a time, each gate opening only on evidence. The plugin is
  `locks` (`claude plugin install locks@rixmerz`), the command is `locks`, the slash commands are
  `/locks:…`, variables are `LOCKS_*`, data lives in `~/.local/share/locks` (XDG respected), a
  project's folder is `.locks/`, kept branches are `locks/task-N`, workers write a `locks-result`
  block, commits carry `Locks-Task`, `Locks-Verified` and `Locks-Check-Output`, and the board's
  header is `X-Locks-Token`. The rename was made by `scripts/rename_to_locks.py` (`--check` lists
  what is left).
  What keeps working for a person who had it installed, with nothing to do: <!-- legacy-name -->
  - every `CAUCE_*` variable they or an older process set reads as its `LOCKS_*`, and a worker <!-- legacy-name -->
    gets both;
  - the old home moves to the new one the first time Locks runs, under a lock, with a link left
    where it was; its database becomes `locks.db` (a link at the old name) only while no other
    process has it open, and is read under its old name otherwise. A home that cannot move is
    used where it is, and Locks says so once;
  - a project's old folder is read where it is and becomes `.locks/` the first time work is
    written there, its line in the checkout's `.gitignore` rewritten; never two folders;
  - a task's kept branch of the old name (and its set-aside refs, foreign and merge branches) is
    renamed, its tip and reflog kept, the first time Locks reads that checkout's branches, so a
    resume starts from its work; a worktree the old home held is found through the link;
  - the store's rows of the old name (a task's origin, a stop's author, a verdict's check, a
    branch named in an ending) are read into the new name once, and again if an old version
    still running writes more;
  - old result and report blocks, compaction markers, the old unverified trailer, the board's
    old token header, the bundled team's old name and the edited-file variable of habits
    installed before are all still read;
  - a neighbour's `cauce` block in its `plugin.json` (or a `cauce.json` beside it) is read after <!-- legacy-name -->
    a `locks` one: livespec, rastro and layout-inspector need no release for this;
  - `cauce` still runs Locks (the console script, `bin/cauce`, and the shim an older link wrote, <!-- legacy-name -->
    which SessionStart rewrites) and says once that it is `locks` now.
  What does not carry over: the board's per-browser choices (its period, scope, last project and
  notifications are kept under new keys, so each is asked again once); values set in Claude
  Code's settings screen for the old plugin, which Claude Code keeps per plugin, so set them again
  for `locks`; permission rules naming the old mod tools (`mcp__cauce__…`), which now are <!-- legacy-name -->
  `mcp__locks__…`; habits installed by the old plugin run from its folder until it is removed, then
  install them again with `locks habits`. SessionStart names an old install still present and the
  line that removes it; Locks never deletes from Claude Code's folder.

### Added

- **A newer livespec is taken and checked, not trusted.** When the livespec locks runs (a plugin's,
  or one on PATH) is newer than the release it was checked against, its own tools are read once per
  release and kept. A tool locks names that it does not serve is left out of the orientation, named
  in the hint as not served, and said in the plan; tools that cannot be read brief as for the checked
  release. Before, a release that renamed a tool left workers told to call one that was not there.
- **The pinned livespec keeps itself current** (`.github/workflows/livespec-pin.yml`,
  `scripts/livespec_check.py`). Every week, a release on PyPI newer than the pin is pinned and
  verified against a real index of this repository: the index read, a brief, a change's reach, every
  tool named served, and `validate_openspec`. Once the suite passes with it, the workflow opens a
  draft pull request. It is never merged or fetched unpinned by itself.

## [0.68.0] - 2026-11-01

### Added

- **A newer livespec is taken and checked, not trusted.** When the livespec cauce runs (a plugin's,
  or one on PATH) is newer than the release it was checked against, its own tools are read once per
  release and kept. A tool cauce names that it does not serve is left out of the orientation, named
  in the hint as not served, and said in the plan; tools that cannot be read brief as for the checked
  release. Before, a release that renamed a tool left workers told to call one that was not there.
- **The pinned livespec keeps itself current** (`.github/workflows/livespec-pin.yml`,
  `scripts/livespec_check.py`). Every week, a release on PyPI newer than the pin is pinned and
  verified against a real index of this repository: the index read, a brief, a change's reach, every
  tool named served, and `validate_openspec`. Once the suite passes with it, the workflow opens a
  draft pull request. It is never merged or fetched unpinned by itself.

## [0.67.0] - 2026-11-01

### Changed

- **livespec pinned at 0.36.0**, which carries three fixes found from cauce:
  - an attribute call no longer links to a project function of the same name, so the briefs' code map stops naming false callers (`isolate.join` read 457 callers, now 2);
  - indexing is 35% faster;
  - spec impact now sees specs.

  The adapter's reads, brief, reach, impact and `validate_openspec` were checked against a real 0.36.0 index, and every tool cauce names is there.

### Fixed

- **A race neither side won was never counted as one.** Its mark sat on the start's attempt,
  which the next move then overwrote. The rung's try was not read as the first try of its own
  that it is, either. The rung's attempt is now marked `raced_beside`: `racing.outcome` reads it
  as neither, and `standards.samples` reads it as a first try while the start's try goes on.

## [0.66.0] - 2026-11-01

### Fixed

- **A sweep could stop a run that had just started.** A run was marked running and given its pid
  in two writes, and a sweep between them (a turn's end, the dispatcher) read a running task with no
  pid as dead. An automatic resume stopped so went on unseen and, when it died for real, nobody
  noticed: the task stayed interrupted with no crash named. Running and the pid are now one write.
  New doctor rule: `swept-before-its-pid`. Found by the all-on CI run.
- **The independent check could write into the work it read.** Its write tools are taken away,
  but a shell writes too, and what it wrote was committed on the kept branch as the work. Its
  worktree is now snapshotted before it runs, and anything it changed is put back afterwards
  (`orchestrate._read_only`, a `verifier_wrote` event), leaving the worker's own uncommitted work
  untouched.
- **SessionStart resumed a dead task whose work had already landed.** It ran the automatic resume
  before reading what landed, so a worker ran on merged work while the task was dismissed. It now
  reads what landed first, as the Stop hook already did. New doctor rule: `resumed-after-it-landed`.
- **`cauce ui` did not prune for its first hour after a machine booted.** Its first prune waited
  until `time.monotonic()` passed `PRUNE_EVERY_S`, and that clock counts from the boot. The first
  pass now prunes. Found by the new coverage tests on a fresh CI runner.
- **Work held `--after` a task in another worktree of the same local repository started on the
  base.** With no `origin`, a checkout's key is its path, so two worktrees of one repository read
  as two repositories; the work got the other checkout as an `--add-dir` instead of starting on the
  kept branch. They now count as one when they share a git store (`after._store_of`). Found by the
  coverage pass.
- **`cauce habits install --repo` with a folder that does not exist made it**, and wrote
  `.claude/settings.local.json` inside. A missing folder is a typo: it is now refused.
- **The recipes a brief carries changed from one process to the next**: tied sequences were
  ordered by a set, whose order follows each process's hash seed (`habits.recipes`). Found by the
  coverage pass.
- **A CI service written `db: {}` was named `{}`**; an empty flow mapping or list is now read as
  one (`ci.read`).
- **A checkout with no commit read `HEAD` as its commit** when notes were reviewed (`git rev-parse`
  prints it and fails); it now has none.
- **A race whose rung only met the environment was decided by the rung's broken launch.** Its
  attempt was appended after the start's, and the next move reads the last attempt: the task
  retried at the rung's cell and stopped as "the environment failed twice" instead of climbing from
  the start's failure. The rung's attempt now goes before the start's. Found by the coverage pass.
- **API keys were not masked** (`notes._SECRET`, used for check tails, notes, briefs, the scribe, the
  survey and compaction digests). Anthropic (`sk-ant-api03-…`) and OpenAI (`sk-proj-…`) keys carry
  dashes, and the pattern took letters and digits only, so they went through whole. It now also
  covers GitHub's `gho_`/`ghs_`/`ghu_`/`ghr_` tokens, Google API keys and `Bearer` tokens. Found by
  the new fuzz tests.
- **A tool call's signature could hold its argument.** A file tool kept whatever followed the
  last dot of its path as the file's type, and a shell token whose quotes could not be split showed
  a secret's shape once cleaned. A type is now a real extension (short, plain) or `other`, and a
  cleaned shell signature with a secret's shape is `bash:unknown`.
- **`cauce doctor` took 33 s, and so did SessionStart, on a store of 3000 runs**: two index rules
  read every run's folder with git, one process per run. Each folder is now read once, about 1 s.

### Changed

- **Commands and hooks start faster.** A command imported the whole run loop before doing anything.
  The run loop now loads only where a command runs work (`cauce.lazy`), and git's answers about a
  folder are kept per process, checked with `stat` before reuse. `cauce tasks` went from 251 to
  ~165 ms; `board`, which the mod runs every 15 s per session, from 236 to ~190 ms; SessionStart
  went from 25 git processes to 14.
- **A plan finds a task's kind and brings its neighbours' indexes current side by side.** A real
  plan took 13.7 s doing Haiku's classification (~4 s) and livespec's index (9 s) one after the
  other; it now waits for the longer of the two.
- **livespec pinned at 0.35.0**, checked against a real index of this repository.
- **Tests:**
  - `CAUCE_TEST_ALL_ON=1` runs the whole suite with every adaptive setting as a person gets it,
    with a fake `claude` that also answers Haiku's schema questions; CI runs it on every push.
    82 tests that silently relied on a setting being off now declare it (the `no_verifier`
    fixture and single switches); no assertion was weakened.
  - Coverage now counts the processes tests start.
  - `tests/test_fuzz.py` feeds every reader of outside text the fragments that broke readers before.
  - Coverage went from 95% to 99% of lines and branches (every module at 100% but a few lines
    nothing can reach), with tests that assert behaviour; the floor is now 99.

## [0.65.0] - 2026-11-01

### Added

- **How a worker worked, per attempt** (`cauce.behavior`, `attempt_finished.behavior`): read from
  its own tool calls, never their arguments. It keeps:
  - calls, and the calls that failed;
  - calls per phase;
  - calls and seconds before the first write;
  - the checks it ran itself, and its fix loops;
  - whether it ended on a write no check followed;
  - rereads of a file, the longest run of the same call, the longest silence, active time;
  - calls by tool family.

  `cauce stats` and the Analysis screen add the profiles up by kind and outcome (passing workers
  beside failing ones) and by cell. Each run gains `calls`, `failed_calls`, `check_runs`,
  `fix_cycles` and `calls_before_edit` (in `--runs --csv` too). `cauce stats --attempts [--csv]`
  exports one record per attempt with its cell, outcome, time, tokens, cost and profile, for an
  analysis outside cauce. Observed, never a rule's input.

### Fixed

- **The independent check's tool calls were counted as its worker's.** The check ran with the
  worker's attempt number, so the calls it made with a measuring tool counted toward the attempt's
  `capability_calls`. They also counted toward the evidence gate of that task's later attempts.
  The check's calls are now logged under no attempt (`orchestrate._verifier_env`).
- **A malformed line in the tool-call log stopped reading the log** (a line without a time
  raised an integrity error). Such a line is now skipped.

## [0.64.0] - 2026-11-01

### Changed

- **A plan starts at sonnet/high** (`bench/speed/plans.py`): sonnet named every trap of a plan whose
  traps are found only by reading the code, 3 of 3, at a sixth of opus/high's cost and half its time.
  A plan classified high starts on opus/high, and `feature-parallel`'s plan for parts keeps opus/high,
  where its own bench measured it.
- **The probation cell below `refactor` and `test` is haiku/medium**: as right as haiku/high, about
  twice as fast and cheaper (`bench/speed/kinds.py`).

### Fixed

- **An automatic resume that dies names its crash too**: it wrote to a shared log and its death was
  found by the sweep, which read no crash, so a repeated crash went unseen there. Each run of a task
  now writes to the task's own log, and both the dispatcher and the sweep read the crash from where
  that run began.

### Added

- **`scripts/bump.py`**: one command moves the version in every file that names it and closes the
  CHANGELOG's `[Unreleased]` section, refusing a half-bumped tree or empty notes.
- **A scenario test of a worker that dies of the same crash twice**: resumed once, then left to the
  orchestrator, read through the sweep as the hooks do.

## [0.63.0] - 2026-10-31

### Added

- **The order of a brief's notes is on trial** (the `recall_fusion` setting): when the code a task
  touches and its words find different notes, the brief orders them by reciprocal rank fusion (a
  note both find comes first, and the words' best match is no longer cut by every anchored note)
  or anchored first, in turns per repository, and keeps fusion only where its first tries passed
  no less often. `cauce stats` shows the trial.
- **Workers name the notes they relied on** (`notes_used` in the result block): only notes their
  brief carried are kept, and `cauce stats` shows how many of the notes briefed were relied on and
  how first tries went where one was named. Observed, never a verdict.
- **Long TRACER files go by section**: what does not fit in the brief is named by heading with its
  lines (`docs/tracer/floors.md:40-88`), so the worker reads only the part it needs, instead of
  a count of files left out.
- **`cauce doctor` names a run that died after its attempt passed** (`died-after-a-pass`): the
  ending failed, not the work, as with the empty commit fixed in 0.62.0.
- **Releases tag themselves**: a version that reaches `main` with CI green is tagged `v<version>` and
  released with its CHANGELOG section (`.github/workflows/release.yml`, `scripts/release_notes.py`);
  an older version is tagged by hand from the Actions tab, on the commit a person names.
- **A worker that died names its crash**: the last exception line its run wrote to its log is in the
  stop's reason, beside the log's path, where it said only "exited with 1". One that dies of the
  same crash again says so and is never resumed on its own: it is cauce's failure, and a resume
  repeats it.
- **The Analysis screen shows the order of a brief's notes and the notes workers relied on.**
- **`bench/speed/methods.py`**: on a planted bug told only by its symptom, Haiku with the diagnosis
  method was right 4 of 4 times, without it 2 of 4 (both misses claimed a pass).

### Changed

- **A probation start is also reverted when a passed point takes more than twice as long** on the
  cheaper cell (`standards.PROBATION_SLOWER`): Haiku was as right as the first rung of `refactor`
  and `test`, but about five times slower.
- **A request that opens by asking for a plan is a plan** ("Plan how to migrate…", "Planifica…"):
  it fell to the default build.

## [0.62.0] - 2026-10-31

### Fixed

- **A workflow `plan` step no longer stops as `spec_bug` because its task names a change**: a real
  run's plan worker, told that a read-only task asking for a change is a `spec_bug`, refused to
  plan "Add a function"; a planning worker is now told the change is for the work after it.
- **A worker that committed its own work no longer kills its run at the ending**: when only its
  tools' artifacts (`__pycache__`) were left uncommitted, cauce's commit had nothing staged once
  they were dropped, and the run died with exit 1 (twice in a real workflow, the automatic resume
  included). cauce commits only what is still staged, and keeps the worker's commit as the branch.
- **`cauce workflow status` gives a question step the line that answers it**
  (`cauce workflow answer <run> <step> <option>`), not `cauce resume`, which a question refuses.
- **A workflow graph's `when` label sits over its step's box**, where the gap between two boxes
  cut it off.

### Added

- **A worker's candidate note is weighed against its absence** (the `note_trials` setting): the
  tasks it is recalled for alternate between carrying it and going without it, and it becomes
  current only where the ones that carried it passed their first try no less often; two passes in a
  row no longer prove a note that every task would have passed without. The `briefed` event keeps
  the candidates left out (`notes_withheld`).
- **A later attempt reads the notes of the files the work changed**: the notes anchored to what the
  earlier attempts wrote go in its brief, where the first brief could only read the files the
  request named.
- **The independent check is asked once more for the TRACER CORRECT lines it left unanswered**, its
  two answers merged; only what stays unanswered is said on the verdict card.
- **Methods tried in arms** (`cauce.methods`, the `methods` setting): a diagnosis protocol for
  `debug-unclear` (reproduce, where it fails and where not, a cause shown before the fix) and a
  pre-mortem for `plan` (a `Risks:` section). Each repository tries a method in turns with its
  absence and keeps it where its arm passed first tries no less often; a plan's risks reach the
  independent check of the steps after it as where to probe. `cauce stats` shows each trial.
- **A loop's round that changes nothing is a valid outcome**: its steps are told so, the diary
  marks a passed round that changed no file and moved nothing as empty, the next round starts from
  the last one that changed files, and the summary counts them.
- **Haiku is tried below `refactor` and `test` too** (`matrix.PROBATION`): on `bench/speed/kinds.py`
  it was as right as their first rung (every check held, every mutant killed) at a fifth of the
  cost per right answer, though about five times slower; each repository's probation trial decides.
- **The web board shows what cauce learned**: the Analysis screen lists each trust cell's record and
  sampling, the capabilities handed and called, and the evidence and method arms; a workflow's graph
  draws question steps, sub-workflow steps, skipped steps and each edge's `when`.

## [0.61.0] - 2026-10-31

### Added

- **Workers made to use what they were given, not only told of it.** A worker's notes are found by
  the code its task touches (the notes anchored to the files it names) as well as by its words, now
  strictly, so one word in common briefs no unrelated note. cauce asks the livespec index what the
  files a request names reach (the specs they implement, who calls them, the tests) and puts it in
  the brief before the worker starts. A worker handed layout-inspector for a change to rendered UI,
  or rastro, is held to evidence (`cauce.evidence`, the `evidence` setting): a pass with no call of
  that tool that went through, read from cauce's log of its calls, goes back to the same worker once,
  then waits on the orchestrator with its work kept; each repository tries it in turns with free work
  and keeps it only where its passes drew fewer blockers from the independent check. Every capability
  a worker is handed has its calls counted, and `cauce stats` shows the ones handed and never called.
- **Each TRACER CORRECT line is a criterion of the independent check** (`C1..Cn`): one it says is not
  met blocks the change; one it leaves unanswered is said on the verdict card and the ending, never
  taken as met.

- **A workflow step can choose by what the step before it ended with** (`when`): `findings` or
  `clean` (the blocking findings it passed with), `changed` or `unchanged`, or a question's
  option. A step whose condition does not hold is skipped with no worker, and what follows starts
  from the work before it, so two steps with opposite conditions are an if/else.
- **A workflow step can ask the person** (`ask`, `options`): no worker runs, the step waits on the
  person (`stops.PERSON_ONLY`, cause `question`), the session hears of it with the line that
  answers it, and `cauce workflow answer <run> <step> <option>` passes it and goes on. A question
  is answered, never resumed.
- **A workflow step can run another workflow** (`workflow`, `with`): a run of it starts on the
  work before the step, and its end passes the step with the branch it kept. The workflows a run
  calls are checked and frozen when it starts; one that is missing, lacks an input, calls itself
  or nests past three levels is refused before anything is queued. Cancelling the run cancels the
  run it started.
- **`cauce workflow render`**: a workflow, or a run with each step's state, as a Mermaid
  flowchart (`--markdown` fences it for a README or a pull request).
- **`--expect <revision>`** on `cauce workflow save`, `step add|set|rm` and `edit`: a change made on
  a revision that is no longer the one it builds on is refused; `cauce workflow show` prints it.
- **`cauce ui --idle-exit SECONDS`**: the board stops once no tab was open for that long, counted
  after the first one came.

- **Earned trust: the verifier reads less where its record says it may** (`cauce.trust`, the
  `trust` setting). A cell is a repository, a kind and the worker's cell, and each task's first
  decided independent check is one inspection. Once ten in a row came clean (ISO 2859-3 skip-lot
  sampling), a medium change a cheap cell made is checked one pass in two, after ten more one in
  four, never fewer; one blocker returns the cell to every pass. A high risk, a correction's
  re-check, a resume carrying blockers and a fix of ledger rows are always checked, and cauce's own
  check runs on every pass. Which passes are checked is a hash of the repository and the task. A
  pass let through is a `verifier_sampled` event, said in the run and on the verdict card.
- **A correction's re-check reads what the correction changed:** the independent check keeps a
  snapshot commit of what it read (`isolate.snapshot`, `read_at` on `verified`), and the next round
  is told the files changed since and to read those first, the rest of the change being read
  already (`delta` on its event). After a new model, or outside a worktree, it reads the whole change.
- **A cheaper cell below the ladder, on trial** (`matrix.PROBATION`, the `probation` setting): where
  `implement`'s first rung already passes its first tries in a repository, Haiku is tried in turns
  with it, every pass read by the independent check. The calibration makes it the start only by its
  own start-cell rule, and reverts it when pass-first falls or a passed point costs more on it than
  before: the one place money moves a standard, and only back up the ladder. From `bench/memory`,
  where Haiku briefed with the project's constraints passed every hidden test at a sixth of
  Sonnet's cost.
- **`cauce trust`** (and a section of `cauce stats`): per cell, its clean checks, where its sampling
  stands, the blockers that repeat an earlier task's here, the re-checks that found something new,
  and pass-first and cost per point by the cell the work started on.

## [0.60.0] - 2026-10-29

### Fixed

- **A paused lane is told to the session whose work waits in it:** every ending and
  prompt checks the lanes paused with queued work this session sent, and says each once
  per pause (a sweep pausing it again on the same stop is no new one) with the command that
  opens it. Pausing a lane touches the pulse, so the mod wakes an idle session with it too
  (its toast: "a lane holding your queued work is paused"). Work queued into a lane after
  the ending that paused it (a workflow's next step, another run) no longer waits unseen.
  `Store.lane_paused_on` also reads a pause written as `task #N: …` (a process that
  exited, a directory gone), so that ending names the work that waits too.
- **A task folder a cancel left behind no longer breaks its resume:** what
  `git worktree remove` leaves (a dev server's cache written while it went) is removed
  with it, and a task's folder git no longer knows is moved aside under
  `worktrees/stray/`, never deleted, before its worktree is added again; the run says
  where it went and keeps a `stray_moved` event.
- **An answer a background notice pushed off the end is read:** a worker that gave its
  result block and then answered a monitor's or a background command's notice ("the
  result stands") ended on that reply, and the attempt was read as inconclusive and asked
  again; 7 of 7 unreadable answers on one machine were this. `launch.answered_before_notices`
  reads the newest block from the worker's transcript when only notices opened the turns
  after it; anything else after it (a prompt) still reads nothing. The result contract also
  tells workers to run their check in the foreground and to end every reply with the block.
- **`cauce show` never says a running task cost $0.00:** it shows what the running attempt
  cost so far, read live (`usage.so_far`, as the board and the `tasks` tool already did), or
  says it is not read yet; `final (running)` replaces `final None`.
- **Past the usual time, the range is said:** a running task past its kind's median reads
  `9m, past the usual ≈7m (9 in 10 end within 10m)` instead of `9m of ≈7m`, since half
  the tasks like it go past a median.
- **Fewer repeated notices:** "this task spans N repositories" is said once a day per
  repository, not on every step of a workflow; a step whose run changed a spec is no
  longer told its specs are behind.

### Added

- **A starting point far behind the trunk is said:** a writing task that starts from a
  branch `isolate.TRUNK_BEHIND` (10) or more commits behind the branch work lands on
  (the remote's HEAD, else main, master or develop) warns with both names and the
  count. Nothing is rebased or switched: which base to build on is the person's.

## [0.59.1] - 2026-10-29

### Changed

- livespec 0.35.0 and layout-inspector 0.5.0 ship the `cauce` manifest
  block, so cauce now takes their tools, hints and measures from the
  installed plugin; `docs/neighbours.md` says so.

## [0.59.0] - 2026-10-28

### Added

- **Installed upstream skills come first:** a bundled skill whose authors publish it as a Claude
  Code plugin (`roles.UPSTREAM`: `ponytail`) is read from that plugin's own `SKILL.md` when the
  person installed it (newest version, never a half-done `temp_*` clone, never a repository); the
  vendored copy is the fallback when it is absent, unreadable or malformed. The `briefed` event
  keeps each skill's source (`skill_sources`), and a plugin's copy and version change
  `roles.version`, so a role's trial compares like with like.

### Changed

- **ponytail:** cauce's short text gained the ideas ponytail v5 adds that fit (scope listed before
  writing, the codebase's own helper first, a bug's root cause fixed once, edge-case correctness
  between equal options, the report ends with what was skipped). Compared against upstream
  `09c434a` (v5.3.2); `security-audit` was compared again and upstream is still at `c1c8a8c`.
- **Environment preflight:** a task with `--verify` has its check read before the first attempt
  (`cauce.preflight`, no model, no network): every program it starts, and every program the
  scripts it names start, must resolve on the PATH the check runs with (`runtime.env_for`, with
  `node_modules/.bin` for script bodies); every `npm|pnpm|yarn|bun run <script>`, `npm test` and
  `deno task <name>` must exist in package.json or deno.json; and a Node the project declares must
  be installed or on the PATH. One that fails stops the task as `preflight` (blocked, the
  environment's) with the exact fix line, before any worker: nothing ran and no attempt is
  counted. It fails open: a substitution, a `cd`, a workspace flag, a path or script the request
  itself may add, dependencies the worker installs, or a preflight that breaks is said and the task
  goes on. A `.venv` that imports the project's own package from another directory is a warning.
  A `preflight` event keeps what it found. Ports stay `cauce.services`' (a port that answers is
  left alone, one that never listens stops the task).

### Fixed

- **A request with no task in it is refused, never climbed.** An empty request given as an argument,
  or one of only whitespace or tags (`#review`), was planned and its inconclusive answer earned more
  effort. `cauce run` and `cauce queue add` now refuse it before anything is recorded, exit 2, and say
  to send the task's full text.
- **A reading `--kind` no longer sends an order to a worker that cannot carry it out.** A session that
  passed `--kind review-routine` or `--kind explore` with "Implement X" got a read-only worker, which
  stopped on a `spec_bug`. A reading kind given to a request that opens with a verb of change is now
  widened to `implement`, and the plan says so; a tag, a request that says it changes nothing, a
  question and a workflow step keep the kind they were given.
- **The orchestration skill lists the kinds `--kind` takes.** Only `cauce guide dispatch` did, and a
  session that did not read it passed a kind cauce has no such name for. Any other word is still
  refused with the list (a few common words, `research` among them, are read as the kind they mean).
- **A worker that answered is no longer recorded as an errored session.** The CLI's JSON can flag
  `is_error` with subtype `success` over a full answer; cauce read every such session as errored
  and retried it. A session flagged so whose result holds the one result block, and names no usage
  limit or dropped network, is now read as its answer; one with no block, a limit, or another
  subtype stays errored.
- **A ladder can skip a rung that rarely helps:** a calibration now records a `skip` change per
  repository and kind (band 0, any size) when the climbs from one rung to the next, of the same
  move (`more_effort` or `next_model`), passed their next try under `standards.SKIP_PASS_BELOW`
  (25%) over at least `SKIP_MIN_MOVES` (4) of them. Later climbs from that rung go to the rung
  after (`escalate.decide`'s `skips`); `Decision.skipped` names the rung and `Decision.standard`
  the change, kept on the `moved` event. The climbs are read in the repository, or from every
  repository when it has too few, and the evidence says which. Never the last rung, a rung that
  needs an approval, a retry, more turns, a network or load wait, and never by cost. Judged once
  after `JUDGE_AFTER` climbs past the rung against the climbs into it before (pass rate, median
  time to pass), and reverted as its own change when they pass less by more than
  `REVERT_MARGIN` or take more than `SKIP_SLOWER` (1.25×) as long. `cauce evolution` and the
  Evolution screen show it with the other changes.

## [0.58.0] - 2026-10-28

### Added

- **Neighbours bring their own words:** a plugin may publish a `cauce` block (format 1: `tools`,
  `hint`, `hints` per kind, `measures`) in its installed `.claude-plugin/plugin.json`, or a
  `cauce.json` beside it, and cauce reads it when it plans (`cauce.manifests`), newest installed
  version only, cached per file state. It replaces only descriptive text: the worker's hint,
  livespec's hint per kind, and what each tool measures in the TRACER check. Policy (guards,
  `--routines=off`, who measures, `when`, `touches`, arguments, the server command) stays
  cauce's. A block of another format or shape is ignored whole; a hint naming a tool the block
  does not list is dropped. The plan says once a day whose words came from a manifest. Schema
  and drafts for livespec and layout-inspector in `docs/neighbours.md` and `docs/neighbours/`.

### Fixed

- **A task whose time was never measured no longer makes a kind look fast.** Its attempts kept no
  wall time, and it counted as 0 s: Evolution showed "time per point 0s" beside a real cost, a
  velocity in the hundreds of points an hour, and the calibration judged a standard on those
  numbers. The time per point and the velocity are now over the tasks whose time was measured, and
  say nothing when none was.
- **Routing no longer says a kind passed at `null`.** A workflow step that passes with no worker (a
  clean join of its parts, a ledger the verifier decided) has no cell; it is now counted as "no
  worker".

## [0.57.0] - 2026-10-28

### Added

- **`cauce specs` reports the specs' structure:** where livespec runs, it asks livespec's
  `validate_openspec` (a tool with no command line, so over MCP on stdio) after `sync_openspec`,
  and prints the problems (a requirement with no scenario, no normative keyword, no title) after
  the drift, or says why it could not. Both run on a temporary copy of `openspec/` with its own
  index and its own `LIVESPEC_HOME`, removed afterwards: nothing is written in the checkout, its
  index or livespec's registry of projects.
- **The independent check gets livespec's tools:** where the repository has an index, the check
  (the late one too) is handed livespec's MCP server behind a new `livespec` policy of
  `mcp_guard`, an allow-list of reading tools; anything that could change the index or the specs
  is refused, whatever a tool's own read-only hint says (`export_openspec` carries it and writes
  files). Before, it only read the reach as text.
- **The independent check is told which tool measures each floor and anti-pattern.** 0.52.0 said
  so, but `cauce.tracer` added one generic paragraph. Now, where `docs/tracer/` exists and the check
  has layout-inspector, each line of `floors.md` and `anti-patterns.md` is listed beside the tool
  that measures it (a target size: `accessibility_spatial`; overflow, clipping, truncated text,
  overlap: `detect_issues`; breakpoints: `compare_viewports`; `element_context` for the cause), and
  a line with no measurement behind it is said to be judged from the rendered text. Contrast,
  focus and keyboard order are in that group: layout-inspector 0.4 measures none of them, and the
  old paragraph claimed `accessibility_spatial` did. Lines no rule reaches are counted, never dropped.
- **A change to the interface says why it is checked without layout-inspector.** The plan says
  once a day per repository, when the request names UI files (or is a `ui` task) and nothing in the
  registry measures a rendered page, whether the `plugins` setting is off or the plugin is not
  installed. The check already got layout-inspector for any change that touches UI files, TRACER or
  not (end to end test of both briefs); only the silence is new.


- **`cauce acp` shows a running task alive, not only its first minute:** in Zed the diffs of the
  files the worker wrote in its first minutes were the last thing to change for minutes, while
  `cauce tasks` showed the task running. The tool call's title is now the line `cauce tasks` says
  (`implementing · 3m of ≈5m · 5 files +126 −2`), refreshed every `acp.PROGRESS_EVERY_S` (5 s) and
  with its warnings (past what its kind usually takes here; no tool call for five minutes). Its
  content says what the worker is doing now (`running npm test`, `editing a .py file`: read from
  the tool-call signature, which holds no argument, so it names no file), the files written, the
  stderr tail and, when cauce's check turned an attempt down, the end of its output. The plan
  gains cauce's stages after the request's items (worker, cauce's check, independent check),
  each pending, in progress or completed, so the check phase is no longer silence. When the task
  ends, the answer opens with its verdict and next step (the branch and how it is merged, or why
  it stopped and the command that goes on) before the worker's report. Updates are sent only when
  something changed, so a quiet task sends nothing. `progress.words` and `progress.warnings` are
  new beside `progress.line`, which says the same words as before.

### Changed

- **`cauce specs first on` with no `openspec/` says so plainly:** spec-first is on but nothing is
  enforced until `openspec/` exists, on the command that turns it on and on each look after it.
- **livespec is pinned to 0.34.0, and follows the installed plugin:** 0.34.0 fixes a `group_db`
  bug. When cauce runs livespec through `uvx` it now uses the release in the newest installed
  livespec plugin's own manifest (`.claude-plugin/plugin.json`, skipping a release uv found
  unpublished), else the one it was built against.
- **A resume that grants is the person's to run.** `cauce resume N --allow …` started by the
  orchestrating agent was refused by Claude Code's own permission check as the agent changing its
  own permissions. `commands/orchestration.md` and `cauce guide endings` now say the person runs a
  resume with `--allow`, `--allow-approval` or `--keep` (in a terminal, or after `!` in the prompt),
  and the agent gives them the line.
- **A worker that repeats its result block word for word is read.** Attempts failed with "did not
  answer with exactly one result block" when the worker had written the same `cauce-result` block
  twice. Blocks whose JSON is identical are now one verdict; blocks that differ in anything, or one
  that cannot be read, are still `inconclusive` (fail closed).
- **The board no longer opens a browser tab for each task:** `/cauce:ui` now runs `cauce ui`
  (no tab; `--open` once, only when the person asks to see it), `/cauce` opens the board once,
  and the page shows every task of a session in itself: the Sessions screen lists the work a
  session sent and opens each task inside the same page, live. `cauce ui --tabs` stays as an
  explicit opt-in, capped: with `--open` it opens one tab for the session
  (`#session=<id>`), the server remembers the sessions that have theirs, and a later
  `--tabs` for the same session says it has one instead of opening another. The thread that
  followed a session and opened a tab for each piece of work it sent, `POST /api/follow` and
  the eight-tab cap are gone (`POST /api/tab` takes their place).

### Fixed

- **An index cut at its timeout no longer goes on indexing beside the next one:** `uvx` starts the
  `livespec index` that does the work as its child, and the timeout killed only `uvx`; the child
  went on with nobody holding the index lock, so the next refresh started a second index on the
  same file. The run now leads its own process group and the timeout ends all of it.
- **A change of tests, lockfiles, config or generated files no longer "owes a spec":** in a spec-first
  repository, any file outside `docs/` that was not a document counted as code that owed its spec,
  so a build that only touched `package-lock.json`, a CI file, a test or `dist/` was held back for a
  spec it had nothing to write. Those files are left out. `build/`, `out/`, `target/` and
  `generated/` count as generated only at the repository root: `src/build/compiler.py` and
  `src/generated/x.py` are code and owe their spec.
- **`cauce dismiss` never loses a task's work.** A task whose run died (`interrupted`) left
  its worktree with changes no commit held; dismissing it left them there, outside any branch, for
  the next cleanup to lose. The dismissal now commits them on the task's branch, unverified, and
  removes the worktree, as a blocked stop does (`isolate.keep_left`, a `branch_kept` event); `cauce
  resume N` goes on from that branch.
- **A whole check that timed out after a claimed pass no longer climbs the model.** Only a check
  whose failures were the test runners' own timeout lines was read as the machine's load; one that
  ran past its own time as a whole (`VERIFY_TIMEOUT_S`) was `inconclusive`, so a busy machine
  climbed sonnet to opus and ended in `replan` or `failed`. It is now `environment` with `load`:
  the same cell again after each of `escalate.LOAD_WAITS`, no attempt counted, and a stop as
  `load` with the work kept when it persists.
- **A check slower than its limit no longer burns a night of climbs** (tasks that ran 4+ hours
  with the repository's 15-minute check). The check failed on three tests that also fail at the
  base, but the base's run timed out, so each attempt was charged `code_bug` and climbed
  sonnet/high, sonnet/xhigh, opus/high, re-running the whole check each time:
  - **The base's run is sized from the check's own duration.** It gets the longest run of the
    work's own check times two plus five minutes, never less than the 30 minutes the work's own
    run has (`orchestrate.base_timeout_s`). A base run that timed out is no longer kept for a
    day: it was read as the base's answer by every task on the same commit.
  - **A base that cannot tell is not the work's failure.** The failing tests are read against the
    last base run of that command in the repository that could tell, at any commit. When every
    one is in it they were there before and the pass stands (the report names that commit). When
    not, the task ends blocked as `base_unknown`, its work kept, saying how many of the failing
    tests the worker's own account names, with `cauce resume N` as the line.
  - **The same failing tests never earn a second climb.** When two attempts leave exactly the
    same failing tests, the task ends blocked as `same_failures`, its work kept, instead of
    climbing again.
- **The independent check got the wrong CORRECT lines from a real TRACER flow:** it kept every line
  that contained the word "CORRECT". TRACER's critical-flow template writes the failure paths as a
  table whose header row (`| Failure | WORKS (…) | CORRECT (…) |`) is the only one that says it, so the
  check was handed that header as a criterion and every failure path's CORRECT cell was dropped; a
  `CORRECT:` item that goes on over several lines (or lists its lines under it) was cut at its first.
  The flow is now read as the template defines it: the `CORRECT:` items of the success path with the
  lines under them, and the CORRECT column of each table found by its header, led by the row's first
  cell. The test fixture is now the template's own shape (it had written `CORRECT:` inside a cell,
  which the template never does).
- **A request that did not repeat a flow's file name reached none of its flows:** flows were matched
  by the words of the file name alone. A flow is now also reached by its own title (its `Flow:` and
  `Capability:` lines and its first heading, two words or more, all in the request), and by a
  capability named in `docs/tracer/capabilities.md` (its headings, list entries and table rows) whose
  words all lie in the flow's name or title. A title of one word, or a generic one, names nothing.
- **The "this session loaded cauce X" hint went on after `/reload-plugins`:** it took the version
  the session loaded from the `cauce` launcher a command was forwarded from, and a session's PATH
  keeps the launcher it started with however many times its plugins reload, so the hint named the
  old version even once the session's skills and hooks were the new one's. Every session's hooks
  (SessionStart and each prompt) now keep the version they run (`work/loaded-plugin.json` in the
  cauce home, the newest 200 sessions), Claude Code re-reads the hooks on a reload, and `cauce run`
  reads that for its session before the launcher's: the hint shows only while the session's hooks
  still run the old plugin, and from the first prompt after a reload it is gone. The launcher's
  version stays the answer for a session with no hook record yet. The hint (and the once-a-session
  note a prompt gives) now says `/reload-plugins --force`: Claude Code 2.1 holds back a plain reload
  that would rewrite the prompt cache and prints "Run /reload-plugins --force to apply".
- **A failed check's own output is kept and shown:** when a claimed pass failed cauce's check,
  `cauce show N` gave one line (`claimed a pass, but <cmd> exited 1: <the worker's summary>`) and
  the orchestrator re-ran the whole check by hand to learn which test failed. Each attempt now
  keeps what the check said (`check_tail` on its `attempt_finished` event): the failing tests and
  files by name, then the last lines of the output, where runners print their assertions, then
  cauce's comparison with the base. Colour codes and progress redraws are dropped, the work's
  directories are taken off the paths, and secrets are masked and `<private>` text left out as in
  notes. About 3500 characters at most per attempt. `cauce show N` prints it under the attempt,
  the task page shows it open beside the evidence (and in the attempt comparison), the ending of a
  task that stopped short of a pass shows its last attempt's (clipped; `cauce show` has all), and
  the next attempt's brief, a continued session's note and a resume start from it instead of the
  last 1500 characters of the raw output. Attempts recorded before this keep showing their
  evidence as they did. No schema change: the tail lives on the event.
- **A check that already failed at the base was charged to the work in a folder of several
  repositories:** 0.34.0 promised that a check failing at the base fails no work, but the
  comparison needs the commit the work started from, and a folder that holds two repositories
  has none, so the run had no comparison at all. One failing test the request never touched
  cost three attempts (sonnet/medium, sonnet/high, opus/high) and a `replan`. The base of such a
  run is now each repository inside the folder at its own commit, laid out as the folder is
  (`cd web && npm test` reads the same paths), and the check runs there; the work ends as passed
  with the failure it shares with the base, and the ending offers the `bugfix --after`. A run
  sent from a folder inside a checkout runs the base's check in that folder's copy, not at the
  checkout's root. What the folder holds outside those repositories is not copied to the base.
- **A worker's word that the failure was already there is checked, not dropped:** a report that did
  not pass (`code_bug`, `test_bug`, `inconclusive`, `environment`) and says its check's failure was
  there before its work (a finding marked `pre-existing`, or words like "already fails at the base")
  makes cauce run the check and compare it with the base. Every failing line at the base too, and
  every criterion the request lists answered and met: the work passes. Otherwise the report stands,
  with the comparison added for
  the next attempt (the lines that are new). Tests cover a single checkout, a checkout with no
  worktree, a folder inside a checkout and a folder of several repositories.

## [0.56.0] - 2026-10-27

### Added

- **livespec is a step before the worker, not a tool it may ignore.** A task now builds or
  refreshes its repository's livespec index (incrementally) before its worker starts and waits
  for it up to `livespec_wait` seconds (120 by default; `cauce config livespec_wait`,
  `CAUCE_LIVESPEC_WAIT`, a plugin option; 0 never waits). In a folder that holds several
  repositories each one is indexed on its own, honouring `.livespec.toml` `group_db` (repositories
  that share one index file are indexed one after another, each once the one before lets the file's
  lock go), and the folder never is. Past the wait the task starts without the index, or on the
  stale one a repository already has, and its plan says which; the index keeps building (it is never
  killed). Before, the index started in the background and the task ran without it, so workers
  oriented by opening files.
- **Spec-first orientation in the worker's brief** wherever livespec has an index: find the
  spec for the request (`list_specs`, `search`), reach its code (`get_spec_implementation`), read
  one symbol at a time (`quick_orient`, `read_unit`), open a whole file last; `search_similar`
  before a new helper and `git_diff_impact` before finishing, for the kinds that write. An
  index with no specs starts from `search` and `find_symbol` instead.
- **Spec-code links are part of done.** A worker that writes or changes an OpenSpec spec is
  told to put `@spec:<capability>-<requirement-name>` on the functions that implement it and
  to index again (not from a shared `group_db` index). The report, the independent check's
  brief and a workflow's review step name the specs a change touched that no code names: a
  spec the change wrote is a blocker, one that was already there a warning. A spec written with
  no such comment raises the change's risk to high, so the independent check that blocks runs
  even for a docs task or a medium change made by a strong cell; a warning raises nothing.
- **`cauce stats` keeps which livespec calls a worker made and compares tokens.** Each attempt's
  `attempt_finished` event holds `livespec_calls` (by tool) when livespec was handed; a run's row
  has `livespec_handed`, `livespec_calls` and `tokens`; the report adds tokens per run by kind with
  livespec called against not called (observed, not a trial); the UI's Analysis shows the same
  in a "livespec and tokens" section.
- **`cauce doctor` flags the livespec index:** `index-missing`, `index-unreadable` and
  `index-stale` for repositories recent runs were in, and `specs-with-no-code` for specs in the
  index that no symbol implements. Suspect, never broken.
- **`/cancel N` in `cauce acp`:** stops task N from the editor (`cauce cancel N`), the one way to
  cancel a task on purpose now that stopping a turn does not. `/cancel` without a task's number is
  refused, since read as a request it would start a worker on that word.

### Fixed

- **A task's check gets 30 minutes, not 15:** on a loaded machine (about 24 suites at once) the
  full suite took 19:25, and `VERIFY_TIMEOUT_S = 900` cut it every time after the worker's own
  pass, so eight finished tasks read as failed and climbed the ladder for nothing. The limit is now
  1800 s.
- **A new request in the same Zed thread no longer cancels the task still running:** Zed sends
  `session/cancel` before the `session/prompt` of a follow-up, so `cauce acp` cancelled the first
  request's task ("you cancelled it with cauce cancel before it finished") though the person only
  wanted to ask for more. A cancel now ends the turn at once (the editor waits for its answer before
  sending the next prompt) and stops showing that run's updates, but the task goes on, in `/tasks`
  and on the board; the prompt that follows within `acp.CANCEL_WINDOW_S` (3 s) leaves it running,
  says so, and starts a task of its own. A cancel nothing follows (or the input closes first)
  cancels the task as before.
- **Restoring a saved thread in Zed:** `cauce acp` advertises `loadSession: false`, yet Zed asks to
  load a saved thread and showed "no thread found with ID". `session/load` is now answered with why
  and what to do: cauce keeps no saved threads, open a new thread.
- **A request's backticks no longer reach the shell, and a bad task id is one line, not a
  traceback:** a dispatch whose text sat in a double-quoted argument had its backticks run by the
  shell (it ran `cauce merge N` and `cauce guide dispatch`) and the task started with those words
  missing; the `N` it left behind then crashed `cauce merge` with `ValueError: invalid literal for
  int()`. The orchestration skill and `cauce guide dispatch` now send the request on stdin through
  a quoted heredoc (`cauce run - <<'EOF'`, the same for `cauce queue add -`), so the shell never
  interpolates it; an empty stdin is refused (exit 2) before a task is made. `cauce merge` and
  `cauce pr` refuse a word that is not `N` or `#N` in one line (exit 2), and `cauce split` and
  `cauce dismiss` read a non-ASCII digit (`²`) as text, not as a task number; so do `--after ²` and
  `cauce config max_parallel ²`, which no longer reach `int()`.
- **A stopped task is resumed once when several things notice it at the same time:** the
  dispatcher's loop, a `limit-wait` waiter and a turn's end all call `dispatch.recover`, and the
  first resume had not flipped its task to running yet, so a second one started a second worker on
  it (the usage-limit scenario failed with three worker calls instead of two under load). The resume
  is now recorded in one write transaction before it starts, and one started since the task's last
  ending, in the last two minutes, is left to take hold; one that took hold and stopped again is not.
- **The test suite no longer depends on what the machine has on its PATH:** `rastro` in
  `~/.local/bin` failed the three capability tests; the tests now hide the programs cauce adopts
  from the PATH unless a test puts its own there.
- **A task sent from a folder that holds several repositories showed nothing of its work:** such
  a folder is no checkout, so the task runs with no worktree, and its progress line, `cauce peek`
  and phase all diffed a worktree that was not there ("works in no worktree of its own, or git
  cannot say", no files or lines written, the phase stuck on "exploring"). A run now
  records, as its first attempt starts, the commit each repository inside the folder (or the
  checkout itself, for a run with `--no-isolate`) was at and the files already modified there; a
  retry or a climb reads from that same point, so the work of its earlier attempts still shows.
  Progress, `cauce
  peek` (`--stat` too), the task page's diff, the phase and the files a running task is said to
  have changed read each of those repositories against that record, files named from the folder
  (`api/src/x.js`), so what the person already had uncommitted is not the worker's. They are read
  without touching the repositories' indexes (`git diff-index`; `git diff` rewrites one to refresh
  its stat data, under a lock the person's own `git add` may be waiting for).
- **The ending says which repositories were changed in place:** a run with no worktree in a
  folder of several repositories ended with "these changes are in your checkout". It now names
  each repository and its file count, and says none of them has a branch to review or merge.
- **Still in place, said so:** such a task works directly in the person's checkouts, with no
  branch per repository to merge; one worktree per repository is a larger change (a branch, a
  commit and a merge for each). The lines of a file the person had already modified before the
  attempt include what they wrote.
- **A folder that is no repository could be indexed.** livespec was asked to index the folder a
  task ran in when it had no git root, which for a folder of several repositories indexes the
  parent of many. It is refused now (`cauce index-livespec` says why) and its repositories are
  indexed instead.
- A worker's own livespec index (`.mcp-docs/`) in its worktree is a tool artifact: never a changed
  file, never committed.

## [0.55.0] - 2026-10-27

### Added

- **`cauce peek N`:** a running worker's diff so far, its worktree against the commit it
  started on, new files included and a tool's artifacts left out. `--stat` gives the files
  alone. The task page of a running task shows the same diff with a button. Read only: no
  index, no stash.
- **The files a worker wrote, by name:** shown under its progress line on the board, the task
  page, `cauce tasks` and the mod's `tasks` tool (`wrote calc.py, test_calc.py`).
- **A running task's subtasks are its request's items:** `cauce show N` and the task page list
  `S1..Sn` as asked, answered when the task ends. A worker is not asked to keep a task list.
  `claude -p` gives a worker none by default (`TaskCreate` can be enabled). A worker that kept
  one spent 14–15 model calls instead of 3, a median of 31 s instead of 26 s, and $0.295
  instead of $0.25, on a four-item task (3 runs each).
- **`cauce acp` shows the plan and the diffs:**
  - The request's items go to the editor as `plan` entries: pending, then in progress once the
    task starts, completed when it passes.
  - The worker's files go as `diff` content: read again from its worktree every ten seconds
    while it runs, and from the branch it kept once it ends.

### Fixed

- **A worker that wrote said "exploring":** the phase read only the attempt's last six calls,
  and a worker reads again after it writes. Now a write anywhere in the attempt makes it
  implementing. So does a worktree with changes, since a worker that writes through its shell
  (`cat >`, `sed -i`) makes no write call.

## [0.54.0] - 2026-10-27

### Added

- **Delegated work shows how far it got** (`cauce.progress`). Until now, sending work to a
  worker left a person with a count until it ended. A running task now says:
  - **Where it is:** its worker's phase, or cauce's check or the independent check once the
    worker is done. These stages come from new `stage` events the run writes.
  - **How long against the usual:** its elapsed time against the median and the 90th percentile
    of its kind's passes in the repository, of its size when three or more were. With fewer
    passes there is no guess. Past the 90th percentile, it says so.
  - **What its worker wrote so far:** files, and lines added and removed, read from its
    worktree against its base. A tool's artifacts are left out.
  - **A worker gone quiet:** no tool call for five minutes, said as "a long command, or stuck".
    Nothing is stopped.

  The status line tells each of up to two running tasks this way
  (`🔵 1 running (#389 implementing · 4m of ≈7m · 3 files +120 −8)`). So do the board, the
  task page, `cauce tasks` and the mod's `tasks` tool. Queueing a task says how long tasks like
  it took here. In a real run, `cauce tasks` showed a worker go from nothing written to three
  files and 63 lines in 16 seconds, then pass.

## [0.53.0] - 2026-10-27

### Added

- **Spec-first repositories** (`cauce specs first on`, kept in the cauce home, never in the
  repository). A build (`feature`, `implement`) there is told to write the spec of the behaviour
  before its code. A pass whose change touches code and nothing under `openspec/` is held back
  (`spec_owed`). The first time, it goes back to the same cell, in its own session, told to write
  the spec. The second time, it stands as work with one point for the orchestrator to decide,
  kept on its branch. It never climbs: a stronger model writes no spec a weaker one would not.
  `cauce specs` says how far the specs are behind and whether the rule is on. In a real run on a
  spec-first repository, the worker wrote the spec on its first try.
- **`cauce acp`**: cauce as an Agent Client Protocol agent over stdio, so Zed (`agent_servers`)
  and other ACP editors use it as their agent.
  - Each prompt is one `cauce run` in the project's folder. Its progress shows as a tool call
    and its report is the answer.
  - `/tasks` and `/stats` read the board.
  - A cancel asks cauce to cancel the task, then stops its process group.
  - A way in, never a power: no `--allow`, `--allow-approval` or `--keep`. A client's
    `mcpServers` never reach a worker.

### Fixed

- A pass whose independent check was deferred until after its ending (`late_verifier`) said
  that its risk called for no check. Its verdict now says the check runs after the ending and
  that a blocker it shows reopens the task.

## [0.52.0] - 2026-10-27

### Added

- **Specs that keep up with the code** (`cauce.specs`, the `specs` setting, on by default). cauce
  read a repository's OpenSpec specs and never said they were falling behind; a repository could
  build for weeks with none of it specified. Now, where `openspec/` exists:
  - a writing worker is told a change of behaviour updates the spec that describes it in the same
    work, or proposes one under `openspec/changes/`, and how far the specs are behind;
  - the plan says it, days and code commits since `openspec/` last changed, once a day;
  - an ending whose change touched no spec, where the specs are five or more code commits behind,
    offers the docs task that catches them up, in the report `cauce run` prints and in the
    session's ending. Nothing is started. In a real run the worker, told so, still added a
    behaviour without a spec: the instruction alone does not keep specs alive, and the offer is
    what catches it.
- **The TRACER method carried into the work** (`cauce.tracer`, the `tracer` setting). In a
  repository with `docs/tracer/`, a writing worker reads the audience profiles, identity,
  component floors, anti-pattern catalogue and the critical flows its request names before any
  code, and the independent check holds the change to those flows' CORRECT lines and the
  catalogue. A flow is named only by a strict match of its file name's words. When the check has
  layout-inspector (it measures, so it comes with a change that touches the interface), it is told
  which of its tools measures each floor and anti-pattern, and a measurement is the finding's proof.
- **`tracer-slice`**, a bundled workflow: one slice checked ready and built test-first, verified
  blind as its audience would, then refute, fix (an escaped expectation also extends the flow,
  floors or catalogue) and recheck.

### Changed

- **The independent check is told what livespec says the change reaches**: the specs it touches
  and the callers outside it, as where to probe. Before, only the report a person reads had them.
- **`cauce stats` says whether workers use livespec**: how many attempts handed it called it, and
  how those attempts went, observed, never a trial.

## [0.51.0] - 2026-10-27

### Changed

- **The independent check runs after the ending (`late_verifier`, on by default).** A pass that
  cauce's own check settled ends at once; the verifier reads the branch it kept afterwards, in a
  temporary worktree at its tip, and its cost is the task's. A blocker it shows reopens the task as
  `review`, its work kept, and its session hears of it as a new ending; a check that cannot decide
  leaves the pass. It still runs before the ending with no check of cauce's, for a workflow step, a
  fix of ledger rows, and work something queued `--after` waits on: what those release could not be
  taken back. On `bench/speed/versus.py`'s harder build the verifier was 50 s of cauce's 213 s.
  On the harder build cauce now ends in 122 s at the median (114 to 136 s) against 203 to 213 s,
  every run right on the hidden tests, and the late check spoke 66 to 108 s after the ending. In one
  of three it reopened work the hidden tests passed: a false alarm a resume answers. The medium
  build, which needs no verifier, took 82 s at the median against 88 to 91 s.
- **`cauce run` exits with the ending.** Filing what the worker learned (about 7 s) and the late
  check run in a fresh detached process with its own store, for `cauce run` and the runs the
  dispatcher starts. Never a fork: a forked child's SQLite writes were lost when it closed.
- **A plan classified high starts at opus/high, its ladder's first cell.** It started at opus/xhigh.
  `bench/speed/plan.py` drew the same parts at opus/high in half the time, and in real use plans at
  opus/xhigh passed their first try 47% of the time at a 10-minute median.
- **The board's median wait in the queue reads the pickup and spawn** where a run measured them
  apart (it showed "—"), and the board says what the pickup waited on, as `cauce stats` does.
- **Opus at low effort is no cheap cell.** A medium-risk change it made no longer calls the
  verifier by that alone: it passed every hidden test of `versus.py`'s builds on its own, and
  checked as cheap, two of three medium builds climbed to opus/high for nothing (209 s against
  87 s).

### Added

- **`bench/speed/README.md` opens with a summary**: what was measured and its numbers, what was
  built, what was measured and not built and why, what not to change and why, and what was never
  measured.
- `versus.py` waits for a late check and keeps it apart from the foreground wall time
  (`late_s`, `late_passed`).

## [0.50.0] - 2026-10-27

### Changed

- **A writing task classified as high complexity starts at its ladder's first cell.** It started a
  rung up. `bench/speed/versus.py` ran cauce against a single Claude Code session on the same build
  and hidden tests. On the harder build, classified high, cauce started at sonnet/xhigh and took
  468 s at the median, against 86 s for one Opus session at low effort; every arm was right every
  time. Starting at sonnet/high, its ladder's first cell, cauce took 213 s and $0.54 against $1.43,
  as right. A reading kind still starts a rung up for a high complexity, and history still raises a
  writing kind's start where its tasks had to climb. On the medium build cauce took 91 s against 87 s
  for the single session, at half its cost.

### Added

- **`bench/speed/versus.py`**: cauce end to end against one plain `claude -p` session at a cell
  given, on the same build, judged by hidden tests, with the lines each kept. Pulled apart, cauce's
  own machinery was about 5 s of the harder build; its independent verifier (a pricing change, high
  risk) was the rest of the gap to one Opus session at low effort. A guidelines CLAUDE.md
  (`andrej-karpathy-skills`) kept diffs 4 to 15% smaller and made no arm but opus/low faster.

### Added

- **`bench/review` measures cauce's independent verifier at medium effort.** It was 30 to 40%
  faster and caught every slip of a small fix, but caught 20 of 42 planted bugs on the large
  change where high caught 27, so the verifier stays at high. `bench/speed/README.md` also
  records the classifier's model call (4 to 9 s, about one task in ten) as left as it is.

## [0.49.0] - 2026-10-26

### Changed

- **A step's ending and the next step no longer wait for its notes.** A passing run filed what its
  worker learned (a Haiku call) before it recorded its ending and advanced its workflow: 5.3 s at
  the median and 7.5 s at p90 over 107 runs in this store, on every step. The run now files them
  after its ending and the advance, and a `notes_filed` event keeps the time.

### Added

- **`bench/speed/build.py` takes a `+batch` arm** that asks workers to make independent tool calls
  in the same turn. It was slower on both builds (80 s against 76 s, 111 s against 100 s at the
  median, all right), so workers are not told so.

## [0.48.0] - 2026-10-26

### Fixed

- **A start history raised comes back down.** A task's first cell rose to where half of the last
  tasks of its kind had passed, counting a task that started high and passed there. So a start
  that history raised kept every later task as high, and the cells below were never tried again.
  In this machine's store, features started at opus/high though 11 of the last 14 passed where they
  started, and implement tasks at sonnet/high with 19 of 20. Now only a climb raises a writing
  kind's start: a task that passed where it started is no evidence that a lower cell fails. In
  `bench/speed/build.py` the cells this leaves them at were the fastest and as right: implement at
  sonnet/medium passed both builds 3 of 3 times in 48 s and 69 s. opus/high, where features had
  risen, took 118 s and 189 s and missed a hidden test once. A reading kind keeps the old count,
  since its pass is its answer and a review at lower effort missed the hardest bug in
  `bench/review`.

### Changed

- **An ending reaches an idle session within seconds.** The mod looked for endings every 15 s,
  running `cauce endings` and the board each time. A task's ending now touches a pulse file beside
  the store (`Store.pulse`), and the mod checks it every 2 s with a stat, no process, and polls at
  once when it changed. In this store the mod's deliveries had taken 9 s at the median and 16 s
  at p90 after the ending. `cauce endings --json` names the pulse.

## [0.47.0] - 2026-10-26

### Changed

- **A worker no longer runs the full check before it reports; cauce runs it right after.** The
  brief told a writing worker to run the tests near its change as it goes and the whole check
  "yourself once, at the end". cauce then ran the same check again. Worker transcripts on this
  machine spent 29% of their time in tests they ran themselves, often the whole suite three or
  four times in one task. The brief now says cauce runs the check as soon as the worker reports
  and hands it the failures. In `bench/speed/handoff.py` (a 60 s suite, sonnet/high, hidden
  tests as the judge), a medium build went from 216 s to 131 s at the median, and a harder one
  from 208 s to 152 s. Both arms were as right (7 of 7 builds against 6 of 7), and no run needed
  a second round.

- **The dispatcher starts independent work beside running work instead of holding it back.** Its
  prompt said "when unsure, answer false". It held clearly separate tasks (an invoice export
  beside a profile page) because neither declared its surfaces, or because the two "might share"
  a helper. A wait now needs a collision it can name: a change the task needs from the other, the
  same code or behaviour changed by both, or results that rest on what the other changes. In
  `bench/speed/dispatch.py` (real Haiku calls, 3 runs per pair), independent pairs started 42 of
  42 times, against 29 of 42 before, and colliding pairs 0 of 48, as before. On pairs written
  after the prompt was chosen, independent pairs started 18 of 18 times against 14 of 18, and
  colliding pairs 0 of 18 in both.

- **A wait the dispatcher's judgment gave is asked again once the work ahead has changed
  files.** The judgment was made once and kept for the whole run of the work ahead. It is often
  made when that work has just started and changed nothing, so it could only guess. Now, at most
  every two minutes (`flow.REJUDGE_S`), a wait Haiku gave is asked again when the running work
  changed files it had not changed then. The `dispatch_decided` event keeps what each running task
  had changed (`seen`) and which work moved (`moved`). A wait that surfaces or named files gave
  stands.

## [0.46.0] - 2026-10-26

### Changed

- **Reviewed habits are installed by cauce, as the standards settle, once its own check
  passes** (the `habit_auto` setting). The review worker proposes; its word is never
  trusted. When the evolution settles, or when the review's ending is about to be told,
  cauce checks each proposal itself. It must be one
  simple command that runs the habit's own program: no second command, pipe, redirection,
  substitution or other variable. It must pass on a tracked file of its type at HEAD in a
  temporary worktree, within 30 s, and write nothing. What passes is installed in the
  repository's `.claude/settings.local.json`. What fails is refused with why. The ending
  says each, with the line that removes an install. An installed habit that runs past 60 s
  now counts toward turning itself off, as three failures in a row already did. Off: the
  ending gives the install lines for the person, as in 0.45.0.

## [0.45.0] - 2026-10-25

### Fixed

- **Habits offer far fewer false positives.** Over five repositories, 19 habits were offered and
  about one in four was worth a hook. The same habit was offered twice, once as a write and once
  as an edit, though one hook takes both. A linter was offered after any Markdown edit, and
  Python checks were offered to Deno projects. Temporary folders and the home directory counted
  as repositories, and every worker one session sent counted as one place. Now a write is an
  edit. A command must check the edit's file type. A habit of other repositories is offered
  only to a checkout that has its tool (the UI included). Each worker's task is its own place.
  On the same five repositories, 13 habits are offered, each one's tool in its checkout.

- **Work queued while the last dispatcher was leaving no longer waits for the next hook.** A
  step queued by a run that had just ended kicked its lane while that lane's dispatcher, which
  had read the queue before the step was there, still held its lock. The kick was dropped, and
  in one recorded run a review step waited 30 minutes on an idle machine. A start now leaves
  word before it asks whether a dispatcher runs. A dispatcher that read its queue before that
  word starts a fresh one once it releases its lock.
  `cauce doctor` names a pickup that waited with nothing running in its lane
  (`pickup-nobody-saw`).

### Changed

- **A fix whose own check showed every row fixed is not re-checked by a second worker.** The
  fix's independent verifier now answers each ledger row it fixes (`fixed`, `not_fixed`,
  `regression`). A row it calls open is a blocker, and the fix gets a correction round in its
  own task instead of a stop later. When its passing check called every row fixed, the
  re-check after it is decided from those answers with no worker. The two had read the same
  diff one after the other: in this repository's runs they took 2.6 and 3.9 minutes per fix
  round. On `bench/review`'s fix with planted slips, each caught all three slips alone. The
  pair took 94 s against 49 s for one reader.

- **The independent check runs beside cauce's own check, not after it** (the `overlap` setting).
  When a writing attempt in a worktree passes and its change needs the verifier, the verifier
  starts at once. It reads the same diff, and it is told the check is running and not to run it
  itself. A check that fails stops it: its verdict is dropped and its cost kept
  (`verifier_dropped`). It never overlaps for a task that starts services. In
  `bench/speed/overlap.py` (a cauce worktree, its full suite as the check, a real verifier, 3 reps),
  check plus verifier went from a median of 822 s to 478 s. The check was not slowed (260 to 263 s),
  and the verifier passed with the same findings in every run.

- **A feature starts at sonnet/high, a rung below where it started.** The feature ladder now
  runs sonnet/high, sonnet/xhigh, opus/high, opus/xhigh. A feature classified as high complexity
  starts a rung up, at sonnet/xhigh as before, and a repository's history and its calibrated
  standard still raise the start where its features needed more. `bench/speed/build.py` ran two
  builds, three runs per cell:
  - **Medium build** (three rules, a receipt and a CSV export): every cell passed all seven hidden
    tests. sonnet/high took 57 s, opus/high 118 s, sonnet/xhigh 170 s and opus/xhigh 192 s.
  - **Harder build** (the same, plus moving 24 rules and renaming their methods everywhere):
    sonnet/high passed all ten hidden tests 3 times in 3, in 84 s; sonnet/xhigh did too, in 276 s.
    opus/high (189 s) and opus/xhigh (382 s) missed one test once each.

- **A plan starts at opus/high, not opus/xhigh.** The plan ladder now runs opus/high, opus/xhigh,
  opus/max and then fable/high, which still needs a person. `bench/speed/plan.py` asked for the
  plan of `feature-parallel`, two runs per cell. Every plan was valid, read strictly by cauce's own
  parts reader. opus/high, opus/xhigh and opus/max all drew the same three parts, in 93 s, 189 s
  and 510 s at the median. sonnet/high, at 59 s, drew four other parts; whether they build as well
  was not measured, so it is not the start.

- **A large change with no parts is reviewed whole, not split by its top-level folders.** On
  `bench/review`'s 1,221-line change, `bench/review/areas.py` ran three blind reviews per arm. Four
  area reviews side by side (`top`, `docs`, `src`, `tests`) caught the same 40 of 42 planted bugs
  as one review, but slower (142 s against 122 s at the median, since `src` held almost every
  change) and at three times the cost ($1.84 against $0.64). They also left four times the rows
  that were no planted bug (32 against 8) for the refuter and the fix. A change whose parts
  declared their surfaces is still reviewed by those areas.

### Added

- **New habits are reviewed by a worker, and installed on the person's yes.** A habit newly
  found in a project goes, once, to a read-only worker queued at SessionStart (at most once a
  day per repository, the `habit_review` setting, with `autowork`). The worker checks the tool
  is there, finds a command scoped to the edited file, and runs and times it. Its ending gives
  the session one `cauce habits install … --command '…'` line per habit worth it, and a reason
  for each skip. The session asks the person and runs the line only on their yes. A habit's
  command now reads the edited file from `$CAUCE_EDITED`.

- **`cauce stats` says what each blind review added beside another**: for each workflow step
  that reviewed beside another, its cell, the rows it put on the ledger, the rows no other
  review saw, and how many of those the refuter or a proof kept. It changes no definition.
- **`bench/review`: two blind reviewers with one purpose against a classifier before a quality
  check.** These are real review chains on a change with 14 planted bugs in 1,221 diff lines.
  Every arm caught 40 to 41 of 42. In all six two-review chains, the second (sonnet) reviewer
  found no bug the opus one missed, for about 60% more cost than one review. The classifier and
  quality-check funnel cost about as much as a blind review and was the slowest arm, so it was
  not built.


## [0.44.0] - 2026-10-25

### Added

- **Constraints: rules a change must meet, briefed as requirements.** A constraint is a note
  of its own kind. It comes from a person (`cauce note "<rule>" --constraint`) or from a worker
  whose task passed after one of its checks failed and that names the rule the failure showed
  (the result contract's new `constraints`). Every worker's brief carries the project's
  constraints apart from its notes, as requirements, and so does the independent verifier's,
  where breaking one is a finding. A worker's starts as a candidate and is weighed like any
  note; a claimed rule with no failing check behind it is kept as a fact.
  `cauce notes list --constraints` lists them.
- **A note written on work not merged is said to be.** When every anchor of a note was written
  at a commit outside the history a task starts from (a worker's note about its own change,
  filed while its branch waits), that task's brief marks it so.
- **`bench/memory`: the benchmark behind both.** Real workers on a small pricing engine,
  judged by hidden tests. Across 177 runs:
  - Opus starting cold was no better than Haiku at what the repository does not say: both
    failed every such task, silently.
  - Whose memory a student got hardly mattered.
  - Haiku ignored a policy briefed as a note and met it every time once it was briefed as a
    constraint (0/6 → 6/6, and 6/6 on this release's own brief text).
## [0.43.0] - 2026-10-25

### Added

- **A workflow step can start where a reading step left off (`trunk`).** A step that names
  an `explore` or `plan` step before it as its `trunk` starts its first try as a fork of that
  step's session (`claude -p --resume <trunk> --fork-session`), so what the trunk read is
  already in its context instead of being read again, from another worktree. Claude Code
  records a session's system prompt on its first request and every resume and fork sends that
  record, so a new role given as a system prompt never reaches a fork: a trunk now runs with
  its role in its brief, and a fork is given its role, its capability hints, its directory and
  that it is a new worker (what was read is evidence, never a decision made for it) as its first
  message, its brief after it. A review forks only an exploration, which decided nothing;
  refute, recheck and integrate steps fork none. A trunk with no session, one that did not pass
  or one whose context is over 120,000 tokens is not forked, and the step says why.
  `feature-parallel`'s parts now fork its plan; `cauce workflow step add|set --trunk` gives
  any step one.
- **Forking is a trial, not a belief.** Each repository's steps alternate forked and fresh
  first tries until each side has 10, then keep forking only where its first tries passed no
  worse and reached their first change in fewer calls or finished sooner; it ends early where
  forked first tries pass much worse. `cauce stats` shows both arms and where the trial stands;
  the `trunk` setting turns it off.
## [0.42.0] - 2026-10-25

### Added

- **Loops: goals that run in rounds (`cauce loop`).** For work with no end of its own
  (find and follow up leads through the night, keep CI green, work a backlog). A loop has
  a goal, a workflow each round runs, a metric, a window and the person's rules, all kept
  in their cauce home. A detached runner (`cauce loop start`) starts each round as an
  ordinary workflow run, on what the round before kept when it passed, and tells every step
  the goal, the mode, the rules and the diary of the last rounds, quoted as data. The metric
  is the last number the person's command prints, run by cauce before the first round and
  after each. A loop stops on evidence only: its target, its window (`--until 07:00`),
  `--stale` rounds that did not improve on the best before them, or a round that waits on
  what only the person clears; never on money, and each round's cost is kept. Given
  `--variant`s, rounds alternate until each has `--trial` measured rounds, and the one that
  moved the metric most is adopted. The session that made the loop hears of its stop once,
  with its summary (`cauce loop show`): rounds, metric, cost, the strategy adopted and what
  waits on the person.
- **Tools that act on the world answer to a loop.** A capability names its sending tools
  (`"outbound": {"acts": [...]}` in `capabilities.json`) and runs behind a new `outbound`
  guard: those calls are refused outside a loop and while the loop is a draft (every loop
  starts as one, and its workers write what they would send); after the person's `cauce
  loop act`, a call is still refused outside the loop's `--hours`, past its `--per-hour`
  (counted across its workers in a log that keeps no argument) or when it names an
  `--exclude`. What the rules do not allow is listed under `Needs the person:` and shown
  when the loop stops.
- **`/cauce:loop`**: a session proposes a loop with the person (whether it is one, its
  metric, what a round does, the rules in their words, what may act and what waits, the
  window, strategies to compare, the tools the registry lacks), builds it with cauce's own
  commands only on their yes, and starts it as a draft. `cauce guide loops` is the detail.

## [0.41.0] - 2026-10-25

### Fixed

- **Nothing a worker reports is dropped by a count.** Three bounds picked once in 0.12 and
  0.13 dropped data without saying so: a result's findings past 30, a review ledger's rows
  past 30, and a request's listed items past 20, which stopped being criteria a pass must
  answer. All three now keep everything. What is bounded is one brief: a ledger role gets
  the rows that fit in `ledger.BRIEF_CHARS` (shown and agreed rows first), a fix's rows
  that did not fit wait for the next fix round, a refute's go to the person, and the brief
  says how many wait. A re-check may answer for any number of rows.

### Added

- **`panel`, a bundled workflow of four lenses.** Four blind reviews side by side, each
  asking one question (correctness, security, performance, tests), then the same refute,
  fix and re-check as `dual-review`. The refuter is told how many reviewers there were.
- **`cauce guide panels`.** When a session's own Workflow script fits (an exhaustive
  read-only audit, a comparison of designs, a critique of a plan) and when the work goes
  to cauce (anything that writes, needs a check or should be remembered, and every review
  of a change). Inside a panel, readers never vote and a later round never sees what was
  found; what it finds becomes cauce work as one `bugfix` each, after the person sees the
  list, and is never filed as a note.

## [0.40.0] - 2026-10-25

### Fixed

- **The memory of problems follows the work.** A task that failed and then passed on a
  resume that started fresh, or a later task that finished what an earlier one diagnosed,
  left its problem "open", and workers kept seeing it as a dead end. A pass now solves the
  problem its task's attempts opened, in any run, and the open problems of its repository
  its text matches strictly. Work that fails on a problem solved under another title marks
  it recurring and disproves its fix.
- **Two long requests are two problems.** A long request's title is clipped in its middle,
  and problems were told apart by that title alone, so different requests shared one.
  The 30-task scenario found it.

### Changed

- **Sizes with too few tasks learn from the kind.** Where several sizes of a kind each hold
  too few tasks to calibrate, they are measured together. A size with no start of its own
  starts where the kind's first tries pass, and the plan says so. Where sonnet/high passed
  1 in 6, ui and debug work now moves its start up.
- **An estimate is raised where the kind turns out bigger.** When a kind's tasks here turned
  out 1.5 times their estimate or more (the median of 5 or more), the next estimate goes one
  step up the scale, and says why.
- **A role's trial ends.** A candidate that has not filled its arms in 14 days is dropped,
  with the counts it reached.
- **The primer's trial is balanced and can end early.** Its arms fill by count, not by the
  task number's parity. A primer whose first tries pass more than 20 points worse over 6
  tasks each loses at once. `cauce stats` for a repository says where it stands.
- **Notes filed from a conversation are in English.** The person's domain words and quotes
  stay as they wrote them.

### Added

- **Why the pickup waited.** `cauce stats` splits the wait for a worker into:
  - work ahead it was judged to wait for;
  - the usage limit;
  - your ceiling of workers, a choice;
  - the machine's level.
- **`cauce doctor` reads the memory and the checks.** It reports:
  - a problem still open after its task passed;
  - a flake fix that passed on one run;
  - a long attempt that kept few turns.
- **A README among several is named.** A request about a README in a repository holding
  more than one is warned that the one at the top is the one its host shows.
- **The notes' index names what waits for review**, and how to keep or drop each note.
- **Compaction advice the session chose not to take** is counted apart in `cauce stats`.
- **A scenario of 30 tasks in one repository** checks that reports stay short, lines heard
  once a day are not said again, and every problem a failure opened is solved by its pass.

## [0.39.0] - 2026-10-25

### Changed

- **Work that fixes a flaky test is checked three times.** A request that names a flaky,
  intermittent or sporadic failure ("fails 1 of 4 runs") gets its check run three times,
  and it passes only when every run does. Runs that disagree mean the flake is still there:
  that is never the one-rerun pass and never excused by the base. The plan says so, and
  the verdict says how many runs passed. Before, one green run closed a flake fix that
  failed again on CI.
- **Problems that were already there are not offered when their file changed since.** An
  ending used to offer a bugfix for each problem the work found that was there before,
  even when the checkout's branch, or other work that passed after this one started, had
  changed its file since. Those are now counted and not offered: they may be fixed
  already.
- **Lines a repository hears on every task are said once a day.** A neighbour's state
  ("engram: absent", "livespec: … stale"), the CI jobs a check leaves out and a role's
  capability that is not in the registry are said on the first task of the day there.
  The plan's record keeps them all, and the report says how many it left out.
- **A plan asked to be written to a file is work that writes.** "Write the plan in
  docs/PLAN.md" is no longer a read-only plan that a second task has to write down. It
  runs as `implement`.

### Added

- **A resume says where it starts.** Its plan names what it resumes after, the cell it
  starts at and why: the last attempt's cell, the one that waited for an approval, or
  `--start`. It also says when that is below the highest cell the task already ran.
  `cauce show` lists each run of a task that ran more than once, so "failed, resumed,
  done" reads as both.
- **`cauce resume N --surface PATH`.** A task that stopped on a point because its surfaces
  kept it from the cause (it named a file outside them, or changed one that was undone)
  offers a resume with those paths added. Its diagnosis goes on in its brief, instead of a
  dismissal and a new task.
- **A pass whose worker never ran its own check says so.** When cauce's check passed in
  place of a worker that ran out of turns or was refused, the verdict says so. The ending
  then allows opening the diff.
- **Slow work is said.** A running task that has gone past 20 minutes and past 9 in 10 of
  its kind's passes in the repository is named slow, with that 90th percentile. It shows
  in `cauce tasks`, on the board and in the mod's `tasks` tool. Nothing is stopped.
- **`cauce tasks --workers`.** Only work a worker ran, with each row's attempts and cost,
  grouped by project with `--all`.

### Fixed

- **An attempt's turns are its transcript's model calls when the CLI counts fewer.** A
  worker session that compacts restarts the CLI's `num_turns`, so a 48-minute attempt
  could read "2 turns".

## [0.38.1] - 2026-10-24

### Fixed

- **A new task no longer builds on an old branch that has its number.** A checkout can
  hold a `cauce/task-N` branch that is not the work of the task now numbered N: the
  cauce home was started over, or another one used the same checkout. The new task used
  to take that branch as its own kept work, start on top of it and be briefed as a
  resume. Now a task that never ran renames such a branch to
  `cauce/foreign/task-N-<tip>`, so nothing is lost and no list reads it as a kept branch.
  It says so and starts on its base.

## [0.38.0] - 2026-10-24

### Added

- **A session that compacts can leave itself its next prompt.** The `compact` tool takes
  `then`: what the session goes on with once the compaction finished. Before, a session
  that compacted with nothing of its own running stopped there, since no ending would
  start its next turn. Now the mod submits `then` as the session's next prompt, once,
  quoted and marked as its own words, as soon as the compaction ends (a hook saw it end,
  the engine answered, or it never ran; a hook's veto too). It is never told to the
  summarizer. `cauce compaction request --then` is the same from the command line, and
  `cauce stats` shows which compactions carried one and were continued.

## [0.37.0] - 2026-10-24

### Changed

- **Problems that were already there, but that the work fixed, are not offered as work.**
  A worker that writes tests often finds old bugs and fixes them on the same branch. An
  ending used to offer a bugfix for each one. Now it leaves out a finding the worker
  marks `fixed` (a new field of the result contract), and one in a file the work changed.
  It says how many it left out.
- **A queued task's wait is judged again once the work it was judged against ends.**
  Before, a "wait" decision was kept for good. A task held because it might touch one
  task stayed behind an unrelated long build after that task merged. Now the dispatcher
  makes the decision again against the work ahead at that moment.
- **The account's usage limit is its own stop.** Messages such as "Session limit reached ∙
  resets 5pm" are read with their reset time. The task stops as `limit` with its work
  kept, the try counts as no attempt, and no queued work starts before the reset. A
  detached `cauce limit-wait` resumes it after the reset with nobody typing, up to 3
  times. Before, it stopped as "the environment stopped work" and waited for a person.
- **`cauce pr` names the repository and the head in full** (`--repo owner/name --head
  owner:branch`). Right after a push, `gh` used to say "No commits between".
- **`.fresh-dev/` is a tool's own folder.** It is never a changed file and never
  committed to a kept branch, and `cauce merge` does not count it as uncommitted
  changes.
- **Workers are told the repository's known flaky tests.** Every writing brief names
  the tests cauce saw flake there, so each worker no longer rediscovers them.

### Added

- **`cauce merge N M … --pr`.** Merges the kept branches onto a new branch made from the
  one checked out, which is left as it was. The check runs after each merge. When all
  merge clean, it pushes the branch and opens one draft pull request with each task's
  verdict. A conflict or a failing check stops it before any push.
- **`cauce lanes --beside N`.** The session's word that a queued task is independent of
  the work ahead: it starts beside that work.
- **`cauce ci`, `cauce ci quiet <job>`, `cauce ci speak <job>`.** List the repository's CI
  jobs as a plan reads them, and stop naming one that is known not to apply here. Kept
  per repository in the person's cauce home.
- **Clearer refusals.** A dismissal or a resume of a task that is running, queued,
  cancelled or done says what that task takes instead.
- **Older installs are named.** SessionStart names the older cauce installs left in
  Claude Code's plugin cache once, with the command that removes them. After a
  compaction, a hook can still run from one of them.

## [0.36.0] - 2026-10-23

### Changed

- **A large build that outgrows its turns stops for a split instead of climbing.** A
  task the plan sizes at 8 points or more, outside a workflow run and not sent `--whole`,
  that runs out of turns stops as `split` with its work kept, instead of being given
  200 turns on the same worker. Its ending offers `cauce split N`.
- **The orchestration skill is short.** It keeps the core and a decision table for
  every ending; the detail moved to `cauce guide <topic>` (`when`, `dispatch`,
  `endings`, `orchestrator`, `workflows`, `notes`, `memory`, `relay`).
- **Workers are lean.** No subagent tool (whose description lists every agent the
  repository defines, read on every turn), no skill listing (a role's skills are
  already in its brief), and per-machine prompt sections after the shared part for the
  cache; only flags the installed CLI lists. The `lean` setting turns it off.

### Added

- **`cauce split`.** The pieces a request, or a task that stopped as `split`,
  splits into: by its listed items with no model, else by Haiku, read strictly. `--queue`
  starts them on what the task kept, moves what waited for it to the last piece, and
  takes the task off the board.
- **`cauce merge`.** Kept branches merged into the branch checked out, on the person's
  word: the ones that touch no other first, the check after each; a conflict is aborted
  and names the task that reconciles it.
- **`cauce pr`.** Pushes a kept branch and opens a draft pull request with the verdict as
  its description (needs `gh`; `--dry-run` prints it).
- **A check that bypasses the repository's script is said.** `--verify 'ng test'` when
  `npm test` runs `ng test --code-coverage …` warns that what the script adds is not
  checked.
- **Flaky tests.** A red check with three failing tests or fewer runs once more; tests
  that pass then are flaky: the pass stands, the ending names them, and they are kept per
  repository.
- **The tests near the change.** A writing worker is told the command that runs only the
  tests near its change (jest, vitest, pytest) to run as it goes.
- **`cauce stats --expensive`.** The runs that cost the most for their kind, and why:
  turns given more, models climbed, a race, the independent check, a slow check.
- **Many endings at once in short.** Four or more endings tell each pass in one line.
- **Files a request names hold it back.** A task with no surfaces waits when the files
  its text names may touch what work ahead names or already changed.
- **`cauce roles import`.** Claude Code agents as roles of yours: instructions kept,
  model dropped, their tool list as tools taken away; used only where a team names them.

## [0.35.0] - 2026-10-22

### Fixed

- **Work queued after a task that is dismissed no longer waits forever.**
  Dismissing a task takes what was queued `--after` it off the queue too:
  each is cancelled by cauce with why, and reaches the session that queued it
  as an ending whose line (`cauce resume N`) starts it on the base. So does
  cancelling a workflow run for what waits on it. Work held behind a task that
  a later one with the same text superseded waits on that one instead and
  starts on what it kept; work held behind a task whose change already landed
  on the checkout's branch starts on the base. The ending of a task that
  stopped short names what waits on it. `cauce doctor` gains
  `held-behind-gone`, and a queued task cancelled before it started now
  resumes, as its line always said.
- **A plan no longer repeats one old refusal to every task.** The rules
  workers were refused are read from the repository's latest 40 attempts, and
  only a rule refused in two tasks or more is named.
- **A first try no longer races a second worker where the start's failures
  ran out of turns.** When at least half of the failed first tries at the
  start cell here outgrew their turns, the start does not race the next rung:
  another cell gets no more room. The plan says why.

### Added

- **A build that is several tasks is asked split before any worker starts.**
  `cauce run` and `cauce queue add` (and the mod's `queue`) refuse a `feature`
  or `implement` request that lists six items or more in 40 words or more, is the largest size,
  or names three pieces in folders of their own, with exit 5 and how to split
  it: one task per piece with its own `--surface`, or `cauce workflow run
  feature`, whose plan splits it. `--whole` runs it as one task.
- **Two pieces of work that change the same file are said.** An ending names
  the other tasks in the repository, running or with a kept branch from the
  last three days, that changed the same files, and says merging both
  conflicts there.
- **A session hears when it runs an older cauce than the one installed.** A
  command forwarded from an older plugin says that the session's skills,
  hooks and `/cauce:*` text are still that version's; the prompt hook says it
  once per session and version.
- **`cauce ui` takes its port from an older cauce UI.** One it finds by its
  command line (`lsof`, `ps`) is stopped and replaced; anything else on the
  port is left alone, as before.
- **The status line counts this session's waits first.** `cauce board
  --session` counts the waits that are that session's work (`mine`), and the
  mod's status line shows the rest of the project's apart.

## [0.34.0] - 2026-10-22

### Fixed

- **A check that already failed at the base no longer fails the work.** A red
  `--verify` check is compared with the same check at the commit the task
  started from. When every failing line of the work's run (tests, type errors,
  lint errors, coverage thresholds, read from jest, vitest, pytest, mocha, go
  test, deno, tsc, eslint and istanbul output) also failed at the base, the
  pass stands: recorded as a `check_preexisting` event, said in the report,
  marked `failing at base, unchanged` on the verdict card, and the ending
  offers a bugfix for those failures without starting one. Before, coverage
  thresholds that already failed on files the task never touched made it
  climb and stop as replan. A base that passes or cannot tell, or a failing
  line the work added, keeps it a `code_bug`, and the next attempt is told
  which lines are new. A `debug-repro`/`debug-unclear` task must clear at least
  one. The base's run is cached for a day per repository, base and command.
  `cauce doctor` gains `preexisting-that-was-not`. A worker that stops on a
  point (`spec_bug`, `architecture_bug`) whose point is only those failures
  passes instead of waiting on a decision, and workers are told that a check
  failing the same way without their change is no reason to stop.

### Added

- **A check whose exit says nothing is said.** The plan warns when a check is a
  pipeline whose last command is `grep`, `grep -v`, `tail`, `head`, `tee`,
  `sed`, `awk`, `cat`, `sort`, `uniq` or `wc` without `set -o pipefail`, or
  ends in `|| true`, `|| :`, `; true` or `; exit 0`, reading the npm/deno
  scripts it runs too. A warning, never a refusal.

## [0.33.0] - 2026-10-21

### Added

- **Parts that build on each other.** A plan's part may name one part listed
  before it (`after`): it is queued with the others, waits for that part to
  pass and starts from what it kept, so a large feature runs in phases (data
  and API, then the screens on them) instead of being forced into parts that
  all build side by side. A retry that runs the earlier part again takes the
  parts held behind it along. A plan may give up to 10 parts (it was 6): the
  machine's level, not the plan, bounds how many build at once.
- **A plan reads what the split needs.** Plans are told to read the layout and
  the interfaces the parts share, not every file: each part's worker reads its
  own code.
- **A goal that is several features is told so.** `cauce workflow run` says,
  before it starts, that a goal listing six items or more at the largest size
  costs less as runs per feature or per phase (`--after run:<N>`); the run
  goes on as asked. The orchestration guidance says the same.

## [0.32.0] - 2026-10-21

### Added

- **A tab for each piece of a session's work.** `cauce ui --open --tabs` (and
  `/cauce`, `/cauce:ui`) opens the board and one tab per workflow run and per
  task the session has queued or running outside a run (at most eight; the
  board lists the rest), then one more for each piece of work the session sends
  while the board serves. A tab opens straight on its work (`#task=N`, the task
  as the page; `#run=N&repo=…`, the run marked among its project's workflows),
  follows it live, and names its state in its title (`● running · #12 …`).
  Escape takes a task's tab back to the board. A second session that finds the
  board already serving asks it to follow that session too (`POST /api/follow`,
  with the token), and a server handed over to a newer cauce goes on following.
- **A tab's work at a glance.** A task's or run's tab shows its state in its
  favicon as well as its title (a mark per state, colours that follow the
  theme; the board keeps its own). "Notify me when it ends", in that tab's
  header, asks for the browser's permission on its click and then shows a
  desktop notification when the work stops running, saying how it ended. A
  running task's page shows its worker's phase and last eight tool calls
  (signatures only), refreshed every 5 s through `GET /api/tasks/N/live`, and a
  task with two or more attempts compares any two side by side: cell, served
  model, turns, cost, wall time, how each ended, the move after it, the files
  it changed, its summary and its check's evidence.
- **Roles' lessons improve by measured trial.** Every brief records the role a
  worker wore and the version of its text (`role`, `role_version` on
  `briefed`). Once a role's workers in a repository met 3 dead ends or verifier
  blockers, Haiku proposes a short list of lessons for that role there (the
  role's file is never touched). The role's tasks alternate with and without it
  until each arm has 10 first tries; it is adopted only when it passed first no
  worse than 10 points below and did better on pass-first, attempts to pass or
  verifier blockers, and it is reverted if, over its next 10 first tries, it
  makes pass-first more than 20 points worse. Every proposal and verdict is
  appended to `role_revisions`. `cauce roles revisions` and `cauce stats` show
  them; the new `role_trials` setting (on by default) turns it off. `cauce
  doctor` gains `candidates-on-trial-at-once`, `revision-judged-twice` and
  `trial-never-sampled`.
- **A first try races the next rung only where racing paid.** Once 5 races ran
  among a repository's last tasks of a kind, racing stops there when the rung
  won under a quarter of them, or when races took no less wall time to pass
  than comparable climbs; the failed first try then climbs deep instead, and
  the plan says why (`cauce.racing`). `cauce stats` lists, per repository and
  kind, the races run, rung wins, start wins, both failed, and whether racing
  is on there and why.
- **What a worker learned is adopted only with evidence.** A passing worker's
  note starts as a `candidate`: recalled, and marked unproven in a brief. It
  becomes current once 2 later tasks whose brief carried it passed their first
  try, and is dropped after 3 failed theirs with none passing. The `briefed`
  event keeps the notes a brief carried (`notes`). A person's notes and those
  filed from a conversation are unchanged. The Notes view (with a filter) and
  `cauce notes` show the state; `cauce doctor` gains `note-proven-unbriefed`.
- The Analysis screen shows racing's record per project and kind, and each
  role's lessons on trial with both arms' first tries.

### Fixed

- **A plan is never thrown away for its length.** A plan whose part prompt
  ran over 6,000 characters (or whose contract ran over 8,000) was read as
  unfinished and asked again, though the plan was never told of either
  limit and the parts reach their workers on stdin, which has no such cap.
  Both limits are gone: a part is checked for its id, its prompt and its
  surfaces, never for how much it says.

## [0.31.0] - 2026-10-20

### Fixed

- **A workflow never builds on the wrong commit.** A step's kept branch deleted
  while its run still built on it made the next step start on the run's base
  in silence: a review read no change, a refute blocked on a change it could
  not find, and the run reported a branch without the work. Each ending now
  keeps its branch's tip (`finished.tip`); a branch gone while its run is active
  is put back there (`branch_restored`), and one that cannot be stops the run
  with a `workflow_error`. `isolate.prepare` refuses a base it cannot read
  instead of using HEAD.
- **A fix's later round starts from the round before it**, never from the
  build: the run's last branch holds every round's fixes. `cauce doctor` flags
  a round that did not (`fix-round-off-base`).
- **Nothing deletes committed work.** Prune leaves the branches of an active
  run, and deletes a copy only for a twin whose task passed; a cancel (a
  workflow's too) keeps what the task changed on its branch; a resume that ends
  short keeps what the run before it kept.
- **A worktree never gets another commit's dependencies.** A dependency folder
  whose lockfile differs from the checkout's is not linked; the worker is told
  to install it, and the plan names the `--allow` that lets the install run.
- **A second correction in a row on one model goes to the next model**, not the
  next effort: more thinking from the model that missed defects twice is the
  bet that already lost.
- **Work done with one open question is not a failure.** A worker that changed
  files whose check passed, and reports `spec_bug`/`architecture_bug` for one
  point that needs a design decision, stops as `decision`: work kept on its
  branch, the point quoted, the orchestrator's call; the ending says "work done,
  one decision open" and `cauce run` exits 4.
- **Reports say what checked the work.** "In your checkout" only without a
  worktree; "unverified" only when no check cauce ran passed; "the worker's own
  check, not re-run by cauce" when that is the only evidence; a kept branch is
  named with its commit.
- **Security problems a worker found are an alert**, a `⚠ security:` line in the
  report and the ending, secrets masked, not a line among the notes. Problems
  quoted from before the work are masked too.
- **Stale stops leave the board.** A stopped task whose kept branch already
  landed on HEAD is taken off as `landed`; a prompt that is only a CLI flag or a
  bare slash command (`--continue`, `/clear`) is no turn.
- **Risk words read in their sense:** a lockfile is no concurrency, a config's
  or linter's schema is no data, a deployment said not to change is no
  delivery.
- **The route leaves conversation with the session:** informing imperatives and
  questions about where the work stands (indícame, explícame, qué falta, tienes
  pendientes), and a request for a report back.
- **Merges, cherry-picks, rebases and conflict resolution are changes**, never a
  read-only review, whatever the text says of an audit.
- **No `--service` advice that does not apply:** a task that starts its own
  servers, or names a host only as context with a check that talks to none.
- **livespec:** uv's resolver errors ("No solution found … livespec==X") read as
  an unpublished release, recorded ones too, so the pinned release runs; an
  index failure is said in one plan, not every plan.
- **The workspace offer names no folder that does not exist**, and says making
  one is the person's choice.
- **A running attempt's cost is read from its live transcript**; when it cannot
  be, views say running, never $0.00.
- **Claude Code's own folder (a plugin's cached copy) and cauce's home are never
  a project.**
- **Compaction no longer waits for a natural break near a full context:** from
  80% of the window, the harness's own compaction is near.
- **A dead worker's lost time stops at its time limit** in the analysis; what
  passed until a sweep noticed is `unnoticed_s`.

### Added

- `cauce dismiss` keeps a reason: `--why TEXT` (`--reason`), or the words after
  the ids; the UI's dismiss takes one too.
- `cauce tasks --all` names each row's project; an empty folder points at it.
- `cauce run --detach` queues the task and starts its lane.
- Workflow inputs are listed by `workflow list`, `show` and `run <name> --help`;
  an input only a step's `verify` uses is filled by `--verify` (`--check`); a
  positional text fills the one required input left.
- `cauce workflow status` is a line per step (state, cell, attempt, cost,
  branch, why it stopped); `--verbose` shows the inputs whole.
- Shown text is clipped at a sentence or a word (`text.clip`), never mid-word.

## [0.30.1] - 2026-10-20

### Fixed

- **An answer of the wrong form no longer climbs.** A worker whose answer could
  not be read (no single result block, a plan whose parts cannot run) was read
  as shallow work and sent to more effort: a plan climbed opus/xhigh → opus/max
  for a part without a prompt. Now the same cell is asked again, once, in its
  own session when one is kept (`launch.RESHAPE_PROMPT`, told what was wrong
  and not to redo its work), with the same turns; a second unreadable answer
  climbs as any inconclusive does. The `moved` event says `retry`.
- **`cauce doctor` reports a bug coming back, not its history.** Its suspect
  rules read every record of the window against today's rules, so tasks run
  before 0.29.2 kept showing as sessions never kept and turns under the kind.
  The events it reads (`planned`, `attempt_started`, `moved`, `finished`) now
  keep the version that wrote them; each suspect rule names the release that
  fixes it (`doctor.FIXED_IN`) and reads it only from that release on. What
  came before is counted apart, under "fixed since, history only".

## [0.30.0] - 2026-10-20

### Added

- **Scenarios with real processes and a fake `claude`.** The unit tests drive
  the engine in-process with the adaptive settings off, and never saw a worker
  die under the dispatcher, a resume drop a workflow part, or a worker inherit
  the host's session. `tests/test_scenarios.py` runs the real commands as
  processes with a scripted `claude` on PATH and ends each with `cauce doctor`;
  run against 0.29.1, all three fail on the bugs 0.29.2 fixed.
- **A corpus of real requests for the router** (`tests/corpus/requests.json`):
  a request it ever got wrong is kept with where it belongs.
- **Round trips for what one place writes and another reads**
  (`tests/test_contracts.py`): a task's stored options and every lane pause
  reason.
- **CI runs the plugin as Claude Code loads it**: `claude plugin validate` and
  the mod's tests.

- **`cauce doctor`: what the store says is broken.** Every rule is a bug cauce
  shipped and found late in real work, turned into a query: a lane paused on
  a task that passed, a task running with its process gone, queued work no
  dispatcher took, an automatic resume that started nothing (broken: stuck
  now), and over the last days a workflow part that lost its mark, an
  integrate that skipped while parts kept branches, worktree attempts that
  kept no session id, first attempts under their kind's turns, and a format
  failure that climbed effort (suspect). Read only, no model; it exits 1 when
  something is broken, and the session start names what is broken in its
  repository.

### Fixed

- **A question in a request's first sentence stays with the session**
  ("¿arreglaste los fallos…?", "hay forma de…?"): only an opening of doubt was
  read as one.
- **A resume keeps everything the task was queued with.** Its stored options
  left out the start cell, the turns, the base commit, `--carry`, the attempt
  limit and an explicit `--no-livespec`, so the resume after it ran without
  them.

## [0.29.2] - 2026-10-20

### Fixed

- **Workers in a cloud session can be continued again.** A cloud host names
  the person's session in `CLAUDE_CODE_REMOTE_SESSION_ID` too, and a worker
  that inherited it reported the host's session as its own, so cauce kept none
  and every "more turns" or "more effort" attempt started fresh. Workers no
  longer inherit it, and an id that is either host's is still never kept.
- **A standard set before a kind's turns rose no longer cuts them.** An
  `implement` task started with the 40 turns a band's old standard held, under
  the kind's 80; a standard's turns under the kind's own now give way to them,
  and the plan says so.
- **"Render" is no page.** The words that put a request in the `ui` area read
  `render…` as rendering, so work on the Render host was kept as UI work.
- **A worker killed under the dispatcher is resumed by it.** Its workflow
  waited for some session's next turn to resume it; the dispatcher that sees
  one of its own runs stop now runs the mechanical resume at once.
- **A worker never spends another task's resume.** The mod also runs inside
  a worker's session and asks for endings; that ran the mechanical resume
  with the worker's identity, whose `cauce resume` refused, and the task's one
  automatic try was spent. Inside a worker nothing is resumed, and a process
  cauce starts on its own never carries a worker's identity.
- **A lane paused by a dead worker opens when that task passes.** The
  dispatcher wrote its pause as `task #N: …` and the release looked for
  `task #N …`, so a resume that passed left the lane paused and the next
  workflow step queued behind it for good.
- **A resumed part stays a part.** `cauce resume` rewrote a task's options
  without the workflow part it ran, so its step read as that task alone and the
  integrate after it joined nothing: every other part's branch was dropped
  and the review read half the change.
- **A plan's over-long part says so.** A part whose prompt ran past 6000
  characters was reported as having no prompt; the plan's next attempt is now
  told its length and to leave the how to the part's worker.
- **A request that talks to the session stays with it.** "Habla en español y
  dime…", "tell me…", "resúmeme…" were routed as worker tasks; an opening that
  addresses the session is now its to answer.

## [0.29.1] - 2026-10-20

### Fixed

- **A subagent whose ending never arrived no longer stays "running".** A
  background subagent's notice can be lost (a compaction, a closed session),
  and cauce never swept a delegation, so it was listed as unfinished at every
  session start, for days. After a day with no sign of life it is now closed as
  a `turn` stop, nothing to resume, and the session start lists neither it nor
  an interrupted turn as unfinished work.

## [0.29.0] - 2026-10-20

### Added

- **Each cell primes its next worker.** A fresh worker spent a third of its
  calls (a median of 24) finding its way before its first change. A passing
  worker now says what it had to find (`orientation`: where the code lives,
  the command that checks it, a trap), and the next worker of its role, or of
  a task reaching the same area, starts from those lines. It is a trial, not
  a belief: each repository alternates briefs with and without the primer
  until each side has 12 first tries, then keeps it only where it got workers
  to their first change sooner without failing more first tries. `cauce stats`
  shows both sides; the `primer` setting turns it off.

### Fixed

- **A question is no longer sent to a worker.** The router read a question
  whose question mark fell mid-text ("¿Se podría…? Al menos…") as work to
  queue. A question about the work (could, should, what if, vale la pena…)
  now stays with the session, and a request that opens with a verb of change
  stays an order even when it ends with a question.

## [0.28.0] - 2026-10-20

### Changed

- **Each cell keeps its own lessons.** A failure is kept with the role the
  worker wore and the area of what it changed (ui, data, security, money,
  contract, delivery, code; tests and docs only when nothing else changed).
  A worker is shown the dead ends of its task's areas and of its role; a
  failure two tasks met in areas with nothing in common is shared, and only
  shared ones reach the orchestrating session. Fixes recorded before this get
  their role and areas once, from their task's plan and changed files.

### Fixed

- **Dead ends no longer match on a workflow's template.** A step's failure
  was kept under its template ("Build it, with its tests") and its brief text,
  so every run of the workflow matched every other: measured here, 270 of the
  306 dead ends shown to workers came from unrelated runs. A failure is now
  kept and matched by the run's goal, and a strict match ignores the words a
  fifth of the problems hold. Replayed on the same history, workers see 101
  instead of 305, each of their own area or role.

## [0.27.0] - 2026-10-20

### Added

- **A hard start races the next rung.** Where a kind's start passes its first
  try under half the time in a repository (over at least four), that first
  try runs beside the next rung, each in its own worktree and each checked
  there; the first to pass is kept, the other stopped with its process
  group. A rung that wins has its work carried into the task's worktree;
  when neither passes, the climb goes on from the rung. Measured here,
  features passed their first try 29% of the time and each climb was a
  whole attempt more of waiting. The `race` setting (on) turns it off.
- **Large features run in parts by default.** `cauce workflow run feature`
  runs as `feature-parallel` when the goal is 8 points or more, or touches
  two areas or more, and says so; `--whole` keeps one build.
- **A dead worker is noticed at once and resumed.** The hooks look at every
  turn's end: a running task whose process is gone is swept and resumed once
  from the worktree it left (`autowork` on). Measured here, five dead workers
  cost 3.7 hours (17% of all run time), one of them noticed 93 minutes late.
- **What was already broken is offered as a bugfix.** An ending lists the
  problems the work found that were not its change's, shown with proof, and
  the `cauce workflow run bugfix … --after` that would take each on; nothing
  starts unless the orchestrator says so.
- **`cauce stats` names the lost time.** The time of attempts whose worker
  died is `lost`, and filing what a worker learned is `notes`; "other" went
  from 18% to 1% of the measured time.

- **Each request is told its way.** With no model, cauce reads a request for
  the way it is best done and says it when that is not the session itself:
  one worker, or parts side by side. Work done in a session re-reads its
  whole context every turn, so the choice compares the session's context
  times a worker's measured turns against what a worker attempt reads here
  (measured: about 2.3M tokens over 26 turns; the session in which this was
  built read about 600k each turn); reading, small work and what points back
  into the conversation stay with the session. Every decision is kept to be
  measured. The `route` setting (on) turns it off.
- **Mechanical stops resume without waking the session.** A task that
  outgrew its turns (with more), lost the network or ran under load through
  cauce's waits is resumed once on its own, as a dead worker is, before any
  session hears of it; review, permission and approval stops still reach the
  orchestrator.

### Changed

- **Attempts start with room to finish.** A first attempt gets 80 turns, and
  a feature, UI or test task 120 (were 30 and 60). Measured here, 20 of 110
  attempts ran out of turns and 15 of those passed on the next one, paying a
  fresh orientation again (157 minutes and $58); passing features needed 104
  turns at p90. A cap costs nothing when the work is done early, so a
  calibration never moves a band's start turns under its kind's.

- **The session reads the verdict instead of redoing the work.** A passing
  ending keeps what the work was held to (`verdict`): each criterion `S#` the
  worker answered with its evidence, the repository's check cauce ran in its
  worktree, and the independent check's outcome or why it did not run. The
  ending notice gives it whole and tells the orchestrator not to re-read the
  code, re-run the check or diff the branch to confirm it; it no longer asks
  it to show `git diff`. The diff is opened only when the person asks, or
  where the verdict says something was not checked or not met. The
  orchestration guide says the same: the workers are a team that checks
  itself, and the session that delegated never repeats their work.
- **No verifier twice.** A workflow step a blind review of its change follows
  (a feature's build, an integrate step and the parts it joins) runs no
  independent verifier of its own: the review reads the same request and diff.
  Measured here, the verifier was 12% of all run time.
- The orchestration guide tells the orchestrator to distribute the load:
  independent pieces as tasks with their own surfaces, queued together, and
  what depends on other work queued `--after` it at once.

## [0.26.0] - 2026-10-20

### Changed

- **The orchestrating session decides when to compact.** A new `compact`
  mode, `orchestrator`, is the default. cauce still sees the moment (a large
  context, a break between tasks, the cache warm), but tells the session with
  its reasons instead of compacting or handing the person a line; the session
  compacts by calling the new `compact` tool (`why`, and `keep` for what must
  survive beyond the requests, open work and branches cauce keeps itself),
  on that advice or on its own judgment. It runs when its turn ends, with
  cauce's instructions and what the session asked to keep quoted apart as its
  own (`cauce compaction request`). `suggest`, `auto` and `off` work as before.

## [0.25.0] - 2026-10-19

### Added

- **A project knows its orchestrator.** `.cauce/orchestrator.json` names the
  session that orchestrates the project: the first one that sends work, one
  that claims it (`cauce orchestrator --claim`), a relay from it, the next
  session that starts where it ended (a `/clear`, a terminal opened again),
  or one that sends work once it has not been seen for 6 hours. The role
  brings the work the old session sent, open or ended unheard, and the
  orchestrator adopts what sessions gone quiet left in its project, so their
  endings reach it. `cauce orchestrator` shows who holds it.
- **Nothing waits on the person: every stop is the orchestrator's to carry.**
  The status line no longer says "waits on you": it says `🟣 N for the
  orchestrator`. What only the person can grant (a refused command's rule, a
  cell's approval, a directory that is gone, a task past the unattended
  attempts, a branch to merge) the orchestrator asks them for in its
  conversation and, on a yes, runs; every other stop it resumes, rewrites or
  dismisses itself. The board's "For the orchestrator" column lists what it
  asks apart from what it decides, the `tasks` tool and the endings notice
  say which, and `cauce dismiss` typed in a session is recorded as the
  orchestrator's call.
- **The standards calibrate from the work already done.** Tasks that ended
  before cauce kept points counted for nothing, so a store full of history
  calibrated nothing. The first settle sizes them with the same pure estimate
  a plan makes (from their text, kind and the risk their plan recorded,
  marked as sized after the fact) and calibrates every repository with sized
  endings at once; later, a task that ends with no size is sized when its
  ending is settled.

### Fixed

- **Workers no longer take the host's session as their own.** In the app's
  cloud sessions every worker inherited `CLAUDE_CODE_SESSION_ID`, wrote its
  transcript under the person's session id and recorded it on each attempt;
  the variable is dropped from a worker's environment, and a session id equal
  to the host's is never kept.
- **A workflow step's own check gets the run's inputs.** `verify: "{check}"`
  ran as `/bin/sh: {check}` (exit 127) and stopped work that had passed. The
  inputs are filled in as in the prompt; `${VAR}`, `{a,b}` and `{}` stay the
  shell's. A check still holding a `{name}` refuses the run at start and in
  `--dry-run`, naming the input to pass.
- **The dispatcher survives a full disk.** A full store or disk makes it wait
  and try again with its work and lock kept; the level halves once it can
  write. The SessionStart and Stop hooks start a dispatcher for any lane whose
  queued work has none (`dispatch.revive`, with autowork on), so a queue no
  longer sits still until someone runs `cauce work`.
- **The workers come back when the machine does.** A level lowered for
  memory, disk or load goes back to where it was once the machine has been
  clear for a minute and work waits, without waiting for a round of tasks to
  end. A load average over 2 per CPU halves the level like low memory.
- **Tests that time out under load are not a code bug.** When most of what
  failed in cauce's check, after the worker's own run passed, are the test
  runner's timeout lines, the attempt is the machine's: the level halves, the
  same cell goes again after 60 s and 180 s with no attempt spent, and it
  stops as `load` with its work kept if the timeouts persist.
- **A capabilities.json entry named after a neighbour no longer hides 0.24's
  capabilities by touch.** An entry that sets neither `touches` nor
  `measures` takes the neighbour's. SessionStart says once what the registry
  shadows and which servers it fetches with no fixed version
  (`npx pkg@latest`); the example pins `@playwright/mcp`.
- **A livespec plugin naming an unpublished release no longer fails every
  index.** cauce remembers the release uv cannot find, and the index and
  workers run the pinned release; the refresh says so.
- **A resume after a full disk mends its branch.** A kept branch whose newest
  commit cannot be read goes back to the newest readable commit in its
  reflog, the lost commit's files staying in the worktree. With none, the
  resume stops as `environment` with the cause in one line, the branch kept.
- `cauce config max_parallel N` says it is a ceiling over a level that adapts
  by itself.

## [0.24.1] - 2026-10-19

### Fixed

- **`compact auto` compacts in a headless session too.** The engine has no
  compaction between turns in an SDK host or the app's cloud sessions, so
  `$.session.compact` was refused and the person got the toast with the line
  to paste. The mod now runs `/compact` with cauce's instructions there
  (`$.command.run`), and cauce settles it from the hooks that see its marker.

## [0.24.0] - 2026-10-19

### Added

- **A capability follows what the work touches, not only its kind** (#65).
  A registry entry (or an installed neighbour) may declare `touches` (file
  patterns, or `"ui"` for rendered UI) and `measures` (read-only). It joins
  any work whose request names, whose work changed, or whose reviewed diff
  holds such a file, from the first attempt; reading work (a workflow's
  review, refuter and re-check, given the diff's files as `touches`) gets
  only the ones that measure, and the independent verifier gets every
  measuring capability its change touches, even when the work had none.
  layout-inspector declares rendered UI and measures, and its hint says how
  to render a page to measure (a started server, the build, a fixture).

## [0.23.0] - 2026-10-19

### Fixed

- **A confirmed finding is no longer closed by writing it down** (#58). The
  fix is told a row is fixed in the code it is about, never by a note or a
  line in CLAUDE.md / AGENTS.md, and to leave a row it will not fix for the
  person. When a fix's diff only touches documentation or instruction files,
  the engine keeps every row not about those files open, whatever the re-check
  says (`noted_only`), so after its rounds the run stops for the person.
- **Words in another sense no longer raise the risk tier** (#59): a design
  `token`, a chart library `migrated`, a label `truncated` or "a read-only
  checkout" raise nothing unless a word of the area's own sense is near, and a
  word inside a path or an environment variable's name is never read.
  "parallel" is still never read away.
- **A fix that asks for its own tests is a fix** (#60): "asks for tests" fires
  only when the request's first change is about tests.
- **A workflow step that was retried offers nothing to resume** (#61): its
  stopped try is superseded by the retry and never handed to a session.
- **`--after` names what it waits on** (#62): work in another repository is
  said with its repository and title, the confirmation warns when the number
  is a run of this repository, and `--after task:N` / `run:N` say which.

### Added

- **`cauce workflow run --add-dir DIR`** (#63), repeatable, kept on the run:
  every step and every retry gets it.

## [0.22.0] - 2026-10-19

### Fixed

- **A step split into more parts than the dispatcher's limit no longer
  stalls** (#52). A detached dispatcher that stops after its 10 starts with
  work still queued in its lane hands that work to a fresh one once its lock
  is released; a person's own `cauce work --max N` keeps its bound.
- **A workflow step's task is named by its run's goal** (#55): `[feature#3
  build] Migrate the MCP apps…` in `cauce tasks`, the board and the endings,
  instead of the template step's title on every run.
- **What cauce's own check writes is said and marked** (#54). The files a
  `--verify` run writes in the worktree (compared before and after it) are a
  `check_rewrote` event, a line in the report beside `changed (from git)` and
  a `Cauce-Check-Output: N files` trailer on the kept commit.
- **Generated files get no review of their own** (#53). A blind review leaves
  out the files `.gitattributes` marks `linguist-generated` or `-diff` and the
  ones a step's own check wrote: they are no area of a review by area, count
  no lines toward its threshold, and the review is told they are generated.

### Added

- **`--after` work in another repository** (#56). Task and run numbers are
  global, so `--after N` already held work behind a task or run of another
  repository; released, it now starts on its own repository's base (nothing
  crosses), is given the other checkout to read (`add_dirs`, kept for every
  step of a held run), and its plan names the branch and the git commands
  that read it there.

## [0.21.0] - 2026-10-19

### Fixed

- **A base that took in a branch's own work under another commit is not
  ahead of it.** A run started on another run's kept branch said "behind by 1
  commit; both changed: 33 files" once that run was squashed into the base.
  `isolate.behind` now measures from where the branch and the base last held
  the same tree, so a squash or a re-commit of work the branch already has
  brings nothing, and what the base gains after it is still counted.
- **A workflow step's kept commit is named by its goal.** A build that ran out
  of turns with no `commit` line kept "[feature#19 build] Build it, with its
  tests": the step's title and the run's tag. The fallback for a step is now
  the first line of its goal, without the step's lead-in.

### Added

- **Every task has points.** When a task is planned, a pure function scores
  it on the Fibonacci scale (1, 2, 3, 5, 8, 13) from what the plan already
  knows: its criteria, its kind, its risk tier and the areas that raised it,
  the files and surfaces it names, the symbols livespec says it reaches when
  the index is readable, and its length, the weakest signal. The score and its
  reasons ("5: 4 criteria, touches schema, risk high") are kept on the task and
  its `planned` event and shown on the board, the task's page, `cauce show`,
  `cauce tasks` and `cauce route`. When it ends, the size it turned out to be,
  on the same scale, is read from git and its attempts (files and lines
  changed, turns, wall time, attempts) and kept on its `finished` event. A
  workflow run's points are its steps', each part of a step run in parts
  counted.
- **Standards, calibrated from what was measured.** For each repository, kind
  and points band cauce keeps a standard: the start cell, the start turns, and
  what the last 20 finished tasks of the band measured (how often each cell
  passed its first try, cost and time per point). After every 5 endings in a
  repository, from the hooks or the dispatcher and never from a page, a
  calibration applies two rules: the start cell is the cheapest rung of the
  ladder whose first tries pass at least 70% over at least 5 (one rung per
  calibration, never below the ladder, never a cell that needs an approval),
  and the start turns move toward the 75th percentile of what passes needed
  (10 turns per calibration, at most 200). Money moves none of it. `--start`
  and `--max-turns` always win, and the plan says which standard it used and
  why.
- **Every change is recorded and judged.** A change is appended with its
  repository, kind, band, field, before and after, the rule that fired, its
  evidence and cauce's version. After 5 tasks under it, the window before and
  after are compared (pass-first, attempts, time and cost per point), the
  verdict is recorded, and a change that made pass-first worse by more than 20
  percentage points is reverted, one step back, as a change of its own. The store refuses
  an edit or a deletion of any of these records.
- **`cauce evolution`.** A snapshot a day (at the first ending of a new day in
  your time zone, or `cauce evolution --snapshot`) per repository and kind:
  tasks and points done, velocity in points per hour, cost and time per point,
  pass-first and climb rates, points estimated against realized, and cauce's
  version. `cauce evolution [--days 30] [--repo DIR] [--json]` prints each
  metric's trend and every calibration change with its reason and verdict in
  plain sentences ("feature in mecha, 5 points: starts at opus/high since Oct
  20 because 2 of 8 passed first at sonnet/xhigh and 4 of 5 at opus/high;
  pass-first since then 80 %, kept"). The UI gets an **Evolution** screen that
  reads the same, for one project or every one.
- **A workflow step can run in parts.** A step with `"fanout": "<plan step>"`
  runs one task per part of that plan, side by side as the dispatcher allows:
  a read-only `plan` step is asked for `parts` (2 to 6, or 1 when splitting
  does not pay), each with surfaces no other part reaches, and the `contract`
  they all keep, read strictly (no parts, overlapping surfaces or more than six
  is `inconclusive`). Each part gets the step's prompt (the request quoted as
  context: its criteria are its own prompt's), its own prompt, the contract
  verbatim and quoted, and its surfaces. What needs the step waits for every
  part; a part that stops stops the run there, and `cauce workflow retry`
  queues only the parts that did not pass. Parts whose surfaces may touch stop
  the run at the plan with the paths named. A plan of one part is the step run
  whole. `cauce workflow status` and the board show the parts under their step;
  `cauce workflow step add|set` takes `--fanout STEP` and `--integrate`.
- **An integrate step joins the parts.** A step with `"integrate": true` merges
  each part's branch, in the plan's order, on the commit they started from,
  with no worker when they merge cleanly and the run's `--verify` passes; a
  conflict, or a check that fails on the joined work, goes to a worker told
  which files conflict, whose branch to merge and the contract. The review
  chain after it reads the combined diff. Only an integrate step may wait on a
  step run in parts; a definition where another one does is refused.
- **`feature-parallel`**, a bundled template for a feature that touches several
  areas: plan → build (fanout over the plan's parts) → integrate → review →
  refute → fix → recheck. The plan starts where plans pass in the repository
  (never Haiku); a plan of one part is today's `feature`.
- **A large change is reviewed by area.** A blind review of a change over 600
  changed lines that touches two or more areas (the parts' surfaces, else the
  top-level folders) runs as one review per area, side by side, each with the
  whole request, its area's diff and the other files' names; their findings go
  into the one ledger, and refute, fix and recheck run once.
- **The workers at once adapt instead of being fixed.** The dispatcher's three
  per repository became a level per machine, every repository's together
  (`cauce.capacity`): it starts at what the machine holds (CPUs, free memory,
  free disk where worktrees live), rises by one when work waits and a round of
  tasks ended with no limit, and halves on a rate limit or an overloaded API
  (read from the CLI's own result, waiting out its retry-after), on a worker
  the machine starved, or on low memory or disk. `cauce config max_parallel
  auto|N` (a plugin option, `auto` by default) sets a ceiling over it. The
  repositories with work share it by reserve and lending: each is reserved
  max(1, level / lanes) in the order its oldest work was queued, lends what it
  does not use, and gets a lent worker back when its borrowed task ends.
  `cauce lanes` and the board show the level and its reason, and each lane's
  reserve, use and loans; every change is a `level_changed` event. Cost never
  moves it.
- **The time between steps is measured.** A run's `finished` event gains
  `advance_s` (from the step it waited on passing to its being queued),
  `pickup_s` (from queued to the dispatcher starting it) and `spawn_s` (from
  that to its plan), and `cauce stats` and the board's Analysis show them as
  phases. A run that never measured one leaves it out.
- **More effort continues the same model's session.** A `more_effort` move
  whose next cell has the same model resumes the last attempt's Claude Code
  session as a fork at the new effort (`claude -p --resume <id> --fork-session
  --effort <new>`), in the same worktree, with a short prompt that says what
  failed (the check's failing output, or the independent check's blockers in a
  correction round) and to look deeper, and the result contract. Measured
  here, five of six such moves passed, and each second attempt repeated
  nearly the whole first one from a cold start. Another model, a replan, a
  retry of a launch that broke and a resume a person starts still start
  fresh. When the session cannot be continued (no id, the CLI refuses it, the
  worktree was reset) it starts fresh and the `moved` event says why; it says
  `continued` and the session it forked from otherwise, and `cauce stats`
  compares continued and fresh attempts by pass, turns and cost.
- **Work queued after other work.** `cauce run … --after <task>`, `cauce queue
  add … --after <task>` and `cauce workflow run … --after <run>` queue work
  held behind that task or run: the dispatcher never starts it, and `cauce
  queue`, `cauce tasks`, `cauce show`, `cauce workflow status` and the board
  say "waits for #N to pass". When that work passes, the engine releases it
  where it advances a workflow, onto the branch it kept (a run's last step's),
  or the base when that branch is merged; its plan says which, and nothing is
  merged or rebased. Work that stops short releases nothing: what waits stays
  held and says how it stopped ("waits for #17, which stopped as review")
  until a resume of it passes; `cauce dismiss` or a cancel takes it off the
  queue. A missing task or run, or one that failed, is refused when queued.
- **More turns continue the same session.** A `more_turns` move on the same
  cell resumes the worker's own Claude Code session (`claude -p --resume`) in
  its worktree with a short continuation prompt, so its context and prompt
  cache carry over instead of a cold start that re-reads the code and the
  brief; it is told the output that failed its attempt (cauce's own check
  after it stopped), which its context does not hold. The session id is read from the attempt's JSON and kept on the
  attempt (`attempts.session_id`); a session is kept only in a worktree. When
  it cannot be continued it starts fresh and says why; any other move starts
  fresh. Where a task's turns start may be raised by history: when recent
  tasks of its kind here ran out at the kind's turns and passed with more, it
  starts at the median they needed, and the plan says so.
- **`delegate` setting** (`cauce config delegate on|off`, a plugin option, on
  by default): the mod turns Claude Code's `ask` for `queue` and `resume` into
  an allow, so the orchestrator hands out work without waiting on the person.
  A deny stays a deny, and a resume still runs `--unattended`.
- **A check that covers less than CI is said.** The plan reads the jobs of
  `.github/workflows/*.yml` (commands, services, env the code reads) and names
  the ones `--verify` leaves out, in one warning and on the report. A job is
  covered only when the check runs every command of it that checks the work
  (each one a line chains with `&&` or `;`; installs and the shell's
  bookkeeping aside); the warning names the ones left out.
- **A workflow step says how far it is behind its base.** When a step starts
  and when the run ends (done, or stopped at a step), its work is compared with the branch the run started
  from: commits it moved and files both changed, in `cauce workflow status`,
  the ending notice and the board. Nothing is merged.

### Fixed

- What the steps before a workflow step said reached its brief as list items,
  so a step after two others was asked to answer each one as a criterion of its
  own; it is quoted as context now.
- The independent check after an attempt that continued a session ran in the
  worker's session instead of its own; it never inherits one now.
- A task whose check runs Playwright, or whose project's tests drive a
  headless browser, is no longer warned that workers have no browser.
- Resuming a task that reopens a paused lane starts its dispatcher at once,
  so queued work that may run beside it starts now.
- Risk reasons read words in context: "checkout" in git's sense and `payment`
  as one event name among others no longer raise a change to money.
  "Parallel" still always counts as concurrency: reading it away dropped real
  concurrency requests to medium and skipped the verifier.

## [0.20.0] - 2026-10-18

### Added

- **Compaction at the right moment.** At the end of every turn cauce decides,
  with no model, whether now is a good moment to compact: a context at least
  half the window, a natural break (work the session sent ended and was
  reported, a branch merged), no run of this session holding the checkout,
  the prompt cache still warm, nothing compacted or suggested in 30 minutes.
  Each decision is a `compaction_decided` event with its reasons. The
  `/compact` line it builds keeps the person's requests and corrections
  verbatim, the open work with its next command and the branches waiting to
  merge, on one bounded line, secrets masked. The new `compact` setting
  (`suggest` by default, `auto`, `off`) has the mod show the line in a toast,
  or have the engine compact with its instructions between turns
  (`$.session.compact`: the engine refuses a `/` text a mod submits); what the
  engine answered, or the hooks saw (PreCompact's marker, a manual compaction,
  a SessionStart after one; a PostCompact hook is new), is kept per version in
  the cauce home, and a build never seen to run it falls back to suggest and
  says so once. A call that only failed shows the line and is no verdict on
  the build. The mod asks cauce after a turn ends, not on every idle poll.
  `cauce stats` lists each decision, what the
  summary costs warm beside cold, and whether it ran. Claude Code's own
  auto-compaction is left as it is; the README says how to move it.

### Changed

- **The dispatcher's parallel call reads what the tasks declare.** Haiku saw
  the first 600 characters of each task, so four builds queued with their own
  migration numbers ("Migration version: 3", "other tasks running in parallel
  take the other numbers", said at the end of a long goal) were read as
  depending on each other and ran one after another. The question now gives
  each task's opening and the lines further on that name a migration, a
  version, a file, another task or work in parallel (migrations and parallel
  work first, then versions and other tasks, then files; within a bound), the surfaces each task declared, and, for a running task,
  the files git shows it changed so far in its worktree. Declared surfaces
  that may touch another task's, or hold a file a running task already
  changed, wait without asking the model, whatever the others declare. No
  answer still means the task waits, and when unsure, Haiku answers false.
- **cauce says why work waits and why it stopped.** A queued task the
  dispatcher held back says which task it waits for, and why ("waits for #46:
  <reason>"), in `cauce queue`, `cauce tasks`, `cauce show`, `cauce workflow
  status` and the web board's queue; each decision is a `dispatch_decided`
  event, with the task it waits for and what was ahead of it. The decision is
  still made once, but the task the line names is read from the lane as it
  is: when the one it was judged against has ended, it names the one it is
  behind now, and says whom the reason was about. `cauce show` prints what the
  independent check found under the attempt it checked: each blocker, then
  each other finding (one in a blocker's file included), with its place, its
  cause and evidence and its proof, clipped like the rest of the report.
  `cauce workflow run` prints the warnings the plan of each step it starts
  with will raise (a browser the workers lack, a server the task names; a plan
  that cannot be foreseen is a note, never a start that failed); an
  ending repeats the warnings its task ran with, kept on its `planned` event
  as `warnings`, and a run's ending those of the steps the engine went past
  since the session last heard of the run.

### Fixed

- **A judging step is told that what it finds wrong is its finding.** A refute
  confirmed both rows it was given, then answered `fail` because "S1 is not met
  because of F4": the result contract said a pass reporting an unmet criterion
  is not a pass, and nothing told the step that rule is about its own work. A
  step that judges (a review, refute, recheck or reading kind) now gets a note
  after the contract: a criterion the judged work misses, or a row it
  confirms, is reported, and the verdict is `pass` when the judgment is made
  and shown; `fail` is for a judgment it could not make.
- **A judging step is no longer failed for what it found in the work it
  judges.** A review, refute or recheck step of a workflow (and any task of a
  reading kind) that reported "S1, S4 is not met" about the change it judged was
  ended `failed` with "claimed a pass, but says S1, S4 is not met", climbed the
  ladder and paused the lane, although its ledger rows were decided. For a step
  whose deliverable is a verdict (`LaunchSpec.judging`), a criterion it says the
  work under judgment does not meet is a finding carried forward, kept on the
  attempt; the step passes when it answered what it was asked, and a criterion
  it left out is still `inconclusive`. A step that builds or fixes keeps the
  rule: an unmet criterion is a `code_bug`.
- **A move taken for a correction says the work passed its own check.** For an
  attempt whose only failure is the independent check's blockers, `more_effort`
  and `next_model` (the shallow climb and the repeated-answer climb) explained
  themselves as "the work happened and was shallow". Each now says the work
  passed its own check, how many blockers the independent check found, and why
  this rung.
- **A task is no longer read as changing nothing for what it quotes.** A fix
  that quoted what the observed step did ("read-only", "changed no file", a
  parenthesized "(a read-only review)") was briefed as read-only and given no
  check. Quoted text and parentheses are evidence about something else and are
  not read; a parenthesis that opens with the declaration (`(read-only)`,
  `(read-only: report the cause)`), the task's own, still is.

- **Work that passed its own check is no longer discarded when the independent
  check's corrections use up the ladder.** A feature started at opus/high
  passed the repository's `--verify` twice; the verifier found a different
  blocker each round, each correction took a rung, and at the top of the ladder
  the task ended `failed`: branch and worktree deleted, 25 files and the money
  gone, before `MAX_CORRECTIONS` could stop it as `review`. When the last
  attempt passed its own check and only the verifier's blockers stand, running
  out of ladder (no stronger cell, two cells agreeing with no model above, or
  the attempt limit) now stops the task as `review`: its work kept on its
  branch, unverified (`Cauce-Verified: no`), the stop saying the ladder ran out
  with the verifier's blockers open and its account naming them, resumed like
  any `review` stop (its first check is given those blockers). A correction
  still climbs the ladder as it did: a blocker the check shows in work that
  passed is a missed defect, which is the shallow kind (more effort, or a
  stronger model when a cheap cell cannot find it); it is not evidence that the
  task is wrong, so running out of rungs is not a failure (`Move.REVIEW`,
  `Decision.because` says so). A `failed` whose attempts never passed a check
  still drops its work.
- **A task resumed after its worker died is reported by its last ending.**
  The notice a session got for a task that died twice and then passed after
  `cauce resume` could say "it changed nothing" with no branch: while the run
  filed its notes, the latest `finished` event on record was an earlier death's.
  The endings, the board, the workflow status and the stop account now read
  the task's final ending (`Store.final_event`: the last `finished` whose
  status is the task's own), and a task with none yet is not reported until
  it has one.
- **One count of a task's changed files.** The `finished` event kept only the
  first 50 paths, so `cauce workflow status` said "50 file(s)" where the
  report and `git diff --stat` said 106. The event now keeps up to 500 paths
  and the whole count (`changed_n`), and the endings notice, the report and
  the workflow status all read it.
- **A kept commit's subject no longer ends in "…".** A long worker line or task
  title was cut with an ellipsis. It is now cut at the last whole word that
  fits (not on a dangling "and", "to", "of"…), and the full text goes first
  in the commit body.
- **Reading work no longer waits behind writing work.** The review, refute and
  recheck steps of a `feature` or `bugfix` run sat queued behind other runs'
  builds for an hour or more: the dispatcher asked Haiku about them like any
  task, and Haiku read the goal their text carries ("adds migration 3") as what
  the step does. A queued task that will not write (a reading kind from
  `matrix.READ_ONLY_KINDS`, or text `orchestrate` reads as read-only when its
  kind is no build; a task with no kind has the one `classify` gives it without
  the model, a build when no rule reads it) now starts beside running work with
  no question, up to `MAX_PARALLEL`, recorded as "reads only: runs beside
  writing work"; one a "no" had already held starts too. A writing task is
  judged against the writing work ahead of it only, never against a reading
  task, and with none ahead it starts without a call, a "no" given against
  work that has ended included.
- **A dispatch reason is plain text.** A reason the model closed with markup
  ("…as #55.</reason> </invoke>") reached the board as typed; `dispatch.decide`
  now strips tags and keeps one line of at most 200 characters. An answer
  whose `parallel` is not a boolean still makes the task wait.
- **A resume after its worker died continues where it was.** A run whose
  process died mid-attempt (a machine restart, a killed session) left its
  worktree registered with the attempt's uncommitted work, and `cauce resume`
  failed on it with `already exists`. The resume now continues in that
  worktree, keeping what the dead attempt wrote.
- **A resume that cannot start no longer strands its task.** `cauce resume`
  whose worktree could not be made (`IsolationError` from `isolate.prepare`)
  raised before the first attempt and left the task `running` under the pid of
  the process that had just exited, so every later resume refused it and only
  the stale sweep freed it. An error before the first attempt now stops the
  task as `environment` with the error's one-line cause, resumable, and a
  resume of a `running` task whose process is gone sweeps it first instead of
  refusing. A failure of cauce itself under a resume reaches the person as one
  line (the cause and the command that continues it) with a non-zero exit; the
  traceback goes to the log the hooks already report at the next session.
- **After a restart, queued work starts again.** Neither `cauce lanes
  --unpause` nor the end of a `cauce resume` started a dispatcher, so a task
  queued behind a stopped one stayed `queued` until a person ran `cauce work`
  by hand. Both now start the repository's dispatcher when work waits in its
  lane and none holds its lock (with `autowork` on, like the web board's
  unpause).
- **A lane paused on a task opens when that task is resumed.** A lane paused
  by "task #N stopped with no sign of life" stayed paused after #N was resumed
  and running again. A resume that starts the task now reopens a lane paused
  only on it; a pause on another task stays.

## [0.19.0] - 2026-10-16

### Added

- **Workspaces: a folder of repositories is one project.** Work that spans an
  API and the front end that shares its spec was tracked twice: two sets of
  notes (the same ones written twice), two boards, two lanes. `cauce workspace
  add <folder> --with <repo> --with <repo>` makes the folder that holds them
  (made when missing; a repository elsewhere is linked in; with no `--with`,
  the repositories right inside it) one project, its folder holding only its
  own repositories: every
  repository in it, and every worker's worktree of one, answers to the folder
  for its notes, tasks, lanes, sessions, team and kept rules, and a task that
  spans them runs in the folder, which holds every checkout in it (a linked
  one included). What each repository kept there moves to the folder (a clone
  elsewhere keeps its own, its notes copied), with the session names a person
  wrote, and a fact two of them kept is merged into one note with both one's
  anchors and links; a repository keeps its own project-scope workflows. A note about a file names its
  repository in front (`api/src/cart.ts`), so two files of the same name stay
  two, and a repository reviews only its own anchors. Declared in the cauce
  home, never in a repository. `cauce workspace list` and `rm` (new work is
  each repository's again). A task that reaches a sibling repository with
  `--add-dir` while they are tracked apart says so, with the command; cauce
  never makes a workspace for the person.
- **`cauce workflow run --dry-run`** checks the inputs and the memory, shows
  each step with the check it would run, and queues nothing.
- **`cauce workflow run --verify`**: the check of every step that writes and
  names none of its own (a reading step never gets it; a step with no kind
  gets it once it is classified into one that writes).

### Changed

- **A network that drops is waited for, not handed to the person.** A task
  whose session lost the API (`ENOTFOUND`, a connection error, an overload),
  whose worker found a name that does not resolve, or whose check's package
  manager or git could not reach the network, was blocked like a refused
  command. Now the same cell tries again after 30s, 90s and 270s, from the
  work it kept; those tries count toward no attempt ceiling (an unattended
  resume's included), and the waits are their own phase in `cauce stats`.
  Only a network that stays down stops it, as `network` (by the network, its
  work kept, resumed unattended once it is back). A local server that is not
  up, or a test that logs an unresolved name, is not the network.
- **A workflow's first start is the one that runs.** `cauce workflow list`
  shows each workflow's inputs and memory; text given without a name fills the
  one input not named with `--input`, and an error for several names them all
  in a command to copy. A memory topic with no note yet (a new project) no
  longer stops the run: it is said, and the step goes without it. A note or
  problem that cannot be read still stops it, with every problem at once.
- **A flag a command does not take says what was meant**: the near flag of its
  own (`--allw: did you mean --allow?`), the commands that take it (`--input
  is taken by cauce workflow run`), and how a workflow's inputs go.
- **`cauce workflow status` says how far each run got, in a line**: the step
  it is at, its attempt, what its worker is doing, its last commit and the
  files it touched; one run shows the same per step, its inputs on one line
  (`--full` shows them whole).
- **The servers notice only when it applies.** A task that mentions a dev
  server, and whose check needs none (lint, tests, a build), is no longer told
  to start one; one that tells the worker to start its servers still is. The
  notice names the words that set it off.

## [0.18.0] - 2026-10-16

### Changed

- **Cost is measured, never a brake.** A dollar cap stopped more good work than
  it saved: a worker cut off at its cap left half a change, and the next
  attempt started short. There is no `--budget` any more (on `cauce run`,
  `cauce resume`, `cauce queue add` and `cauce workflow`), no
  `--max-budget-usd` handed to a worker, and no attempt refused for the money
  left. What every attempt, check and run cost is still recorded and shown in
  the report, the board, `cauce stats` and the Analysis screen. Turns are the
  one dial of room: a task's are clamped to 200 (a workflow step's too: one an
  older cauce saved with more reads as 200), and attempts stay the ceiling of
  a run. A task an older cauce stopped on a budget reads back as such and
  resumes plainly; a workflow step's old `budget_usd` is dropped when the
  definition is read.
- **An unattended resume has a bound of its own** (`--unattended`: the mod's
  `resume` tool, and the resumes the orchestration guide now has a session
  start on its own): twice the attempts a run takes, beyond which it needs a
  person. An approval for Fable a person gave stays on the task for their own
  next resume; an unattended one never uses it, nor starts on a cell that
  needs it.
- **`cauce investigate` gives an estimate, not a cap**: the average cost of the
  investigators run before (when there are any) times the number of projects,
  in the dry run and when it starts. The init skill says so.
- **The bundled workflows are cut where context changes hands, never by
  role.** `feature`: build (the change with its tests) → review → refute → fix
  → recheck. `bugfix`: repair (a failing test first, then the fix) → review →
  refute → fix → recheck. `review-fix`: review → refute → fix → recheck, its
  review told that code with no change under review has nothing to excuse as
  pre-existing. A clean review ends `feature` and `bugfix` after two workers
  and `review-fix` after one; the ledger steps run only for the findings there
  are, and the refuter is told whether one review or two found them.
- **A review step of a change reads blind**: the change itself, never what the
  steps before said of it (a review with no change to read, such as a plan's
  critique, still gets what came before). A step that needs another gets its
  findings as records (where, what, evidence, cause, proof) beside its
  summary.
- **One agent first.** The orchestration guide starts from the session doing
  the work itself, and names what earns a worker (a context to keep clean, work
  to run beside it, another cell, an independent check). A judgment that needs
  the conversation goes to Claude Code's native fork (`/subtask`, the Agent
  `fork` type), never the check of the session's own work. The init skill, the
  `surveyor` and the investigation brief cut workflows by context and fold
  personas into roles.

### Added

- The independent verifier is told why it runs (the risk's reasons, to probe
  first), reproduces a bug at the base for a debugging task (in a worktree of
  its own, never by stashing the work it checks), and gets its own room (40
  turns at most, with a timeout to match).
- A task the check stopped for review resumes with what the check found, also
  after a dismissal: the blockers it stopped on (now kept on its `verified`
  event) seed the next round's ledger.
- What an attempt cost is split: the worker's own cost and the independent
  check's, and the prompt cache's reads and writes. `cauce stats` and the
  Analysis screen show what the checks cost and how much of the input the
  cache served, per cell and in all; an older run's checks are read from its
  `verified` events.
- How long filing notes takes is timed with the run's other phases.

### Fixed

- **A result block that quoted a fenced snippet, or held a raw tab in a
  string, lost the attempt its verdict.** The block's JSON is now decoded from
  where it opens, not cut at the next fence; two openers, or anything but the
  closing fence after its JSON (a second verdict), are still no verdict.
- A finding claimed as deterministic without its proof is read as inferential,
  so a refuter decides it instead of the claim.
- A reading worker is told where its answer and findings go in the result
  block.
- A worker no longer starts cauce work of its own: `cauce run`, `resume`,
  `work`, `queue add`, `workflow run`, `workflow retry` and `investigate start`
  refuse inside a worker (every reading command still answers), and a worker's
  Claude Code runs with forks off.
- An attempt's record keeps the start and the end of long evidence, so a
  resume's brief quotes the real last lines.
- A resumed investigator stays one: it still counts toward the estimate and
  stays out of insights.

## [0.17.0] - 2026-10-16

### Added

- **The scribe.** A worker started from the task's text alone, and a session
  that never compacted (to keep its thread) fed its project's notes only when
  it ended. At the end of every turn the Stop hook now reads what the session's
  transcript gained since the last time and keeps the facts of the session in
  the cauce home: the person's requests verbatim and which of them corrected
  the agent, the files it wrote, the calls that failed and the call that passed
  after them. No model reads or writes it and the session's context is never
  touched: it can leave things out to fit, never paraphrase. A failure of the
  scribe is noted and never costs the turn its endings.
- **Work a session sends gets its thread.** A task's brief carries the part of
  the sending session's thread it needs: its newest requests, the older ones
  that share the task's words, the files it wrote and the calls still failing
  there, verbatim and marked as context, not instructions. It is read up to the
  moment the task starts, so the turn that sent it is in. An investigation gets
  none. A `briefed` event records what went in.
- **Notes as the session goes.** The project's notes are filed every few turns
  (8 requests, or about 16k characters of conversation) by the detached Haiku
  that used to wait for a compaction or the session's end.
- **`cauce relay` and `/cauce:relay`**, the alternative to compacting: the next
  session that starts in the same place (`/clear`, or a new one) begins from the
  old session's thread — the requests in the person's words, the commits made,
  the files written, the calls still failing, the cauce tasks still open — and
  that open work, and the endings the old session never heard, report to the
  new session from then on. Taken once, never by a resume, dropped after a day.
- **Every run's phases are timed**, on its events: the wait in its lane, the
  planning, the setting up (worktree, waiting for the checkout, servers), each
  attempt's wall time, tokens and turns, cauce's `--verify`, the independent
  check (now with its duration and turns), and the run's wall time.
- **`cauce stats`** (`--repo`, `--days`, `--json`, `--runs [--csv]`) and the UI's
  **Analysis** screen: pass rate and first-try rate, time and cost per pass, by
  kind, cell, workflow and day; where the time went, phase by phase; how runs
  stopped and moved; a session's own turns and its subagents' delegations; runs
  with the session's thread in their brief beside runs without it; the slowest
  and costliest runs, and a JSON export. Read only, no model; a run recorded
  before the phases were timed counts by its timestamps and is left out of the
  breakdown rather than guessed.
- The `scribe` setting (plugin option, `cauce config scribe off`).

### Changed

- The orchestration guide says what a worker now sees of the session (the
  person's own words that relate to its task, never the session's reasoning),
  and offers the relay when a person wants a fresh start.
- Scribe states of sessions not seen for 30 days are forgotten at session start.

## [0.16.1] - 2026-10-16

### Fixed

- **`cauce survey` (and with it `/cauce:init` and `cauce investigate`) stopped
  on a plugin cache with a file in it.** `/plugin` clones a marketplace whole
  under `plugins/cache/temp_*` before it moves it in place, and a clone it
  never finished stays there: a repository's files, a `.gitignore` among them,
  at the level cauce reads plugins from. Listing the installed plugins opened
  that file as a folder and the survey exited before it read any session. The
  plugin cache is now read in one place (`plugins.versions`): only folders, at
  every level, and never a `temp_*` clone, for the plugin list, the neighbours
  cauce hands a worker and the skills a role names as `plugin:skill`. A clone
  of a neighbour left half done is no longer read as that neighbour.

## [0.16.0] - 2026-10-15

### Added

- **`cauce investigate`.** `/cauce:init` stopped at counts: two minutes of
  reading and a plan, with no branch opened, no chain of fixes followed and no
  pasted error matched to its fix. The deep reading is now cauce's own work:
  `cauce investigate start` queues one read-only worker per project (and one
  per plugin named with `--plugin`), each in its own lane so they run side by
  side, on `opus/high`, wearing the new internal `surveyor` role. Its brief
  carries the deep survey and lists what it must cover as criteria its pass
  has to answer: every chain of fixes and reverts explained from the commits,
  every unmerged branch accounted for, every reported error matched to its fix
  or marked unsolved, corrections turned into worker instructions, flows with
  their exact checks, every problem and fact with its evidence. A plugin's
  worker writes its phases and gates as cauce workflow definitions and its
  personas as roles. `--dry-run` shows what it would read and the most it can
  cost; `cauce investigate status --wait` says each ending, and `--json` prints
  every report. Nothing an investigator learned is filed: it waits on the person.
- **`cauce survey --deep`**: per project, every error the person pasted or
  quoted and what the session did after it (its answer, the files changed, a
  check run, a commit), a sample of their requests, every branch against the
  default one (merged, squash-merged or unmerged) with the commits that never
  landed and the fixes and security work among them, the chains of fixes and
  reverts that followed a change on the same files (a fix of a fix joins its
  chain: that fix did not hold), the reverts with what they undid, every fix
  with its files across all branches, cauce's kept branches that hold the same
  tree, and cauce's tasks that stopped with their reason.
- **Installed plugins** in `survey` (`have.plugins`): each one's skills,
  commands and agents, newest version, read from the plugin cache.
- **A worker's report.** A reading worker may return a `cauce-report` block
  beside its result; it is kept on the task as a `report` event.
- **Screenshots in the README**, taken from `cauce ui` on demo data, in light
  and dark, and a table of what cauce does at a glance.

### Changed

- **`/cauce:init` investigates before it proposes**: overview, then scope and
  cost agreed with the person, then `cauce investigate`, a check of every
  report (a stopped worker is resumed, never filled in by guess), then flows
  and failure families across projects, a plan with its evidence, and the
  build — memory first, so workflow steps can carry the problems it recorded.
  Plugins whose workflow the person relies on are taken into the team and the
  workflows. The README recommends running it on Opus at high effort.
- **Shell and tool noise is no problem.** A zsh parse error, a blocked
  `sleep`, an edit of text not found, a file not read first: `survey` counts
  them as `noise` and keeps them out of the errors and fixes it reports.
- `Options.keep_learned`: off, a passing worker's `learned` is filed neither
  in notes nor in engram.
- An investigator that stops never pauses its project's lane: the person's own
  queue there goes on.

- **`--service 'cmd@port'`** on `cauce run` and `cauce queue add`: cauce starts
  the server in the task's directory, waits for its port, runs the attempts and
  stops it after. 17 tasks of one machine had stopped on "this task needs
  running servers", most of them cancelled.
- **`--carry`** brings the person's uncommitted changes into the task's
  worktree; without it, a task that names an uncommitted file is warned before
  it starts. **`--add-dir`** gives the workers a sibling repository.
- **Ignored paths a task names are copied into its worktree** (up to 50 MB;
  never committed): a task about `scripts/slides/` found an empty folder.
- **`cauce tasks --turns`** shows the sessions' own turns and subagents; by
  default `cauce tasks` lists cauce's work only.

### Fixed

From a compendium of 1,207 real tasks:

- **A session's turns showed up as interrupted tasks to resume.** 67 chat
  messages ended `interrupted: no sign of life` with "resume it", and the
  endings notice the mod submits to wake a session became a task of its own
  (one even `failed`). A turn that ends without its answer is now a `turn`
  stop with nothing to resume, cauce's own notices are no turn, and
  `cauce tasks` leaves turns out.
- **Running out of turns read as a bug in the code.** When the worker ran out
  of turns, its check passed in its place and the independent check then found
  a problem, the next attempt climbed effort and model (sonnet/high → xhigh →
  opus, about $6.50 on one task), and "two cells gave the same answer" fired on
  the wrong cause. It now gets the same cell with twice the turns, and is no
  dead end in memory.
- **A timeout after real work was "the environment".** A feature that changed
  24 files and ran past 1800 s was retried once and then blocked as
  "environment failed twice". A timeout after changing files is now out of
  turns: it goes on with more turns from what it did, and an attempt's time
  grows with its turns (60 s per turn, never under 1800 s).
- **UI and test work start with 60 turns**, as features do: at 30 they ran out
  before a verdict more than any other kind.
- **A new model looked for work that had been reset away.** After a wrong
  approach the worktree goes back to the base; the next brief now says so, and
  what the last attempt wrote is kept on `refs/cauce/aside/task-<id>-<n>`.
- **Workers sourced nvm and were refused.** The brief now names the Node already
  on the worker's PATH, forbids `source ~/.nvm/nvm.sh`, `nvm use` and
  `bash -c`, and lists the shell commands the person's rules let through.
- **An attempt was launched with $0.03 left on opus/xhigh.** After the first,
  no attempt starts with less than its model can use (haiku $0.03, sonnet
  $0.20, opus $0.50, fable $1): the task stops as `budget` with the resume
  command.
- **Criteria the request put out of scope failed the pass.** Items under a line
  like "Part B (do not touch):" became S5 and S6, and a pass that left them
  alone was a `spec_bug`. They are now no criteria, and the brief names them as
  out of scope.
- **A livespec index that cannot be built was retried by every task.** A
  failure is kept and not tried again until the checkout moves.
- **Duplicate `cauce/task-*` branches.** `cauce prune` deletes a finished
  task's branch when another kept branch holds the same tree, and a task asked
  again while one with the same text keeps its work points at `cauce resume`.
- **The task drawer's attempts were squeezed into 140 px.** The map's
  milestones list shared the `.timeline` class, and its grid clipped every
  attempt's cell, turns and cost. The milestones have their own class now.

## [0.15.0] - 2026-10-15

### Added

- **`/cauce:init`**, which replaces `/cauce:personal-team`. It reads every
  Claude Code session on the machine and the history of every repository they
  ran in, finds the flows the person's work follows, and — once they agree —
  builds one team covering every flow, one workflow per flow with measurable
  criteria, and the memory of what was already solved: project notes and the
  problems with the fixes that worked. It writes only through `cauce roles`,
  `cauce team`, `cauce workflow`, `cauce note` and `cauce memory record`.
- **`cauce survey`** (`--json`, `--repo`, `--days`, `--sessions`,
  `--per-project`): read only, no model. Per project: what the person asks for
  and the shape each session takes (`plan → implement → check → commit → pr`);
  friction (corrections with what the agent said before them, interrupts,
  refused calls, bare "go on"s, pasted logs); errors solved in-session, with
  the files changed between and what the agent said before the pass, and the
  ones never solved; the commands and checks run, skills, subagents and slash
  commands used; and the repository's history: cadence, commit types, fix
  share, reverts, merges, branch prefixes, agent share, hot and fragile files,
  recent fix subjects. Across projects, each session shape is counted with the
  projects that show it, never quoted. A cauce worker's worktree, the home
  directory and `/` are no project's.

### Changed

- **Commits read as the change.** A worker proposes the commit's subject
  (`commit` in its result block); the kept commit is that line (else the task's
  title), the worker's summary under it, and the task in trailers:
  `Cauce-Task: #12`, `Cauce-Verified: no (blocked)` while unverified. A
  Before, a merged branch read as `cauce task #1144: Repo wapp-admin…` in the history.
- **The independent check no longer stops work that is getting better.**
  Fixing every problem the round before found is progress and earns the next
  round (two corrections at most, as before) when a probe shows a new problem;
  a new problem after progress that is only read from the code goes with the
  pass, recorded on the `verified` event and in the task's findings. Before,
  any new finding after the first correction stopped the task as `review`:
  a palette change was blocked with each round fixing what the last found.
- **The verifier measures, and is told to.** It keeps the neighbours that only
  measure (layout-inspector) when the work had them, and its instructions say
  to report everything on the first look, to measure UI findings, and that
  taste is no finding.
- **The verifier runs at high effort** on the work's model (never Haiku):
  work that climbed to xhigh or max was checked at that effort too, doubling
  the wait on the slowest tasks. Its independence is the fresh session given
  the request verbatim.

## [0.14.2] - 2026-10-13

### Fixed

- **Subagents' turns are counted.** A subagent writes its own transcript
  under `<session>/subagents/`, and only the session's file was read: in a
  real store not one of 13,832 usage records was a subagent's, and a session
  that delegated its building showed about 60% of what it cost. `Stop` now
  reads those transcripts too, each counted to the delegation its tool call
  opened (from the `.meta.json` beside it).
- **A subagent's report is no task.** Claude Code hands a background
  subagent's final report back as a turn (`<agent-message …> [Subagent
  hand-back]`), and each became a task of its own: four of fifteen in one
  session. It now joins the task that delegated the work, which records the
  answer the session gives on it.

### Added

- The task and session views price a session's own turns (`turns`, its
  subagents included) apart from cauce's workers (`workers`). A prompt
  answered in a session showed $0.00 whatever it cost.

## [0.14.1] - 2026-10-13

### Changed

- **A prototype is functional work.** The `frontend` role and the `ponytail`
  skill tell a prototype (a mockup, a wireframe, "just make it work") from a
  product screen:
  - a prototype gets every state and semantic markup, with the fewest neutral
    styles, each marked `ponytail: prototype styles`, and never an invented
    palette;
  - a design system or brief that exists is applied in full;
  - only a product screen with neither still stops to ask.

## [0.14.0] - 2026-10-13

### Added

- **engram, adopted** (`adapters/engram.py`). For teams whose agents already
  keep their memory in engram:
  - the brief recalls the project's engram memories as data;
  - workers get `engram mcp` read-only, behind `mcp_guard`'s new `engram`
    policy (reading tools only, held to the project);
  - with `engram_write` on (off by default), what a passing worker learned is
    saved as engram observations, one session per task, each save with an
    operation id so a retry never saves twice.

  cauce talks only to engram's server (HTTP or Unix socket, bearer token
  honoured), after `/health` and `/compatibility` verify it, and never opens
  its store. New settings: `engram` and `engram_write`.
- **`<private>…</private>`**: text a person marks private never becomes a
  note and never reaches engram.

## [0.13.0] - 2026-10-13

### Added

- **A review ledger with a refuter.** Workflow steps take `"ledger"`:
  `refute`, `fix` or `recheck` (`--ledger` on `cauce workflow step`). The
  engine builds the ledger `F1..Fn` from the blocking findings of the reviews
  before, frozen with a sha256. Reviewers do not vote: one is enough to put a
  finding on it.
  - A row only one reviewer saw and could only read from the code goes to a
    refuter. It answers `confirmed`, `refuted` or `unsure` with its own proof;
    `unsure` comes to the person.
  - The fix gets only the confirmed rows. The re-check answers `fixed`,
    `not_fixed` or `regression` for each row against the fix's own diff.
  - A row still open gets a second round; after two, the run stops as
    `review`. A ledger step with nothing to decide passes without a worker.
- **`verdicts`** in the result contract: a pass that leaves a given row
  without a valid verdict is `inconclusive`.

### Changed

- **`dual-review`** now runs refute → fix → recheck, with up to two rounds.
- A workflow step that passes while a sibling still runs is no longer handed
  to the session as an ending.

## [0.12.0] - 2026-10-13

### Added

- **Criteria.** A request that lists what it wants (bullets, numbered lines)
  becomes criteria `S1..Sn`, kept verbatim in every brief. A pass answers each
  one with its evidence; one left out is `inconclusive`, one not met a
  `code_bug`.
- **Findings as records.** Reviewers, auditors and the verifier report
  `where`, `what`, `evidence_class` (deterministic, inferential, insufficient)
  and `cause` (introduced, worsened, pre-existing, unknown). Only the change's
  own, shown findings block; two attempts naming the same places converge.
- **Risk tiers.** Each task has a tier (passive, medium, high) read from its
  request, kind and changed files, with its reasons, only ever raised; shown in
  the report and the UI.
- **An independent verifier.** A high-risk pass, or a medium one from a cheap
  cell, is checked by another cell, read-only, given the request and the diff
  but never the worker's summary. A blocker that persists earns at most two
  correction rounds; new findings alone stop the task as `review` for a
  person. `cauce config verifier off` turns it off.
- **Edit surfaces.** `--surface <glob>` (run, route, queue add) and a workflow
  step's `surfaces` bound where a task writes. A change outside them fails the
  attempt and is undone in its worktree. Tasks with disjoint surfaces run side
  by side without asking Haiku.
- **`dual-review` workflow template**: two blind reviews on different models,
  a fix of only the findings the change caused, and a re-check.
- **Phase.** A running task shows whether it is exploring, implementing or
  checking, on the board and in the mod's status line.
- **The change in the drawer.** A task's drawer shows its risk, its
  independent checks and, on demand, its diff (`GET /api/tasks/<id>/diff`).

Ideas adapted from gentle-shell (MIT).

## [0.11.0] - 2026-10-12

### Added

- **`/cauce:personal-team`**, the plugin's first skill. It reads how the person
  works and proposes one team per purpose: several sessions on several
  projects usually need several teams. Once the person agrees, it builds the
  roles on cauce's own, creates the teams and gives each project its team.
- **`cauce team insights`** (`--json`): every project cauce worked in, with:
  - the kinds asked for and how each went (pass rate, where it passed, how it
    stopped, what it cost);
  - the failures of attempts that did not pass;
  - its languages by share of files, its manifests, whether it has a UI;
  - the checks its sessions repeat after an edit, and the team it uses;
  - suggestions (a language specialization, a kind that passes badly, kinds
    never asked for).

  Beside it: the person's roles, teams and Claude Code subagents, by name and
  description. Read from what cauce kept and from the checkouts, never by a
  model.
- **`cauce roles new <name> --extends <role>`**: a role built on another, with
  `--match`, `--skill`, `--on-demand`, `--capability`, `--disallow`, `--kind`
  and `--prompt` (`-` reads stdin). It keeps the parent's instructions and
  follows them; its lists are the parent's plus the ones given.

- **A workflow step names its role** (`"role": "security-auditor"`,
  `cauce workflow step add … --role`). It is used only when the project's
  team has it: a definition a repository commits never chooses beyond the
  person's team.
- **rastro behind a guard.** `cauce.mcp_guard` sits between a worker and
  rastro and refuses, before they reach it, a browser profile, a saved routine
  or the profile list, and writes or uploads to any host but localhost; every
  call runs in rastro's session `cauce-worker`. The hint asked for this; the
  guard enforces it.
- **`plugins` on the plugin's settings screen**, beside the other settings;
  a test holds every setting to an option there.
- **Roles that add to their parent's lists.** `add_skills`, `add_on_demand`,
  `add_capabilities`, `add_disallowed_tools` and `add_match` extend the
  parent's list instead of replacing it, so a derived role follows its parent
  as it changes. `cauce roles new` writes them, and a role it makes no longer
  freezes its parent's kinds.

### Fixed

- A skill a role carries whole is no longer also offered by path.

## [0.10.0] - 2026-10-12

### Added

- **Installed neighbours as capabilities.** layout-inspector and rastro,
  installed as Claude Code plugins (or on PATH), reach workers without a
  `capabilities.json` entry:
  - layout-inspector for `ui` work, and after a failed `implement` or
    `feature`;
  - rastro for `test` work, and after a failed `ui`, run with its saved
    routines off.

  The server entry is read from the newest installed version, from the
  manifest, the file it names or `.mcp.json`, with `${CLAUDE_PLUGIN_ROOT}`
  filled in. Each comes with its plugin's skill to read first and a read rule
  for its folder. A registry entry of the same name wins; `cauce config
  plugins off` leaves them out. `cauce capabilities` and `cauce neighbours`
  say where each comes from.
- The `frontend` role names rastro beside layout-inspector, and `tester`
  names rastro.

## [0.9.0] - 2026-10-12

### Added

- **Roles.** Every worker runs as a role: a name, what it is for, the kinds it
  takes, its instructions and the skills it carries, all in the worker's
  system prompt beside Claude Code's own. cauce still picks the model, effort
  and tools of every attempt: a role names no model and adds no tool, it may
  only take tools away, carry skills (`skills`), point at them (`on_demand`)
  and name the person's capabilities. `extends` builds a role on another;
  `match` makes it a specialization picked where the project holds its files.
- **The bundled team**, one role for every kind: `answerer`, `researcher`,
  `docs-writer`, `tester`, `refactorer`, `implementer`, `frontend`,
  `feature-builder`, `debugger`, `investigator`, `reviewer`,
  `security-auditor`, `architect`, `frontier`. Most adapt vise's agent
  charters; `security-auditor` carries Cloudflare's security-audit skill.
- **Bundled skills**, credited with their licenses in
  `src/cauce/skills/README.md`: vise's engineering-baseline, ponytail,
  security-baseline, architecture, ui-critique, web-ui-rules, design-brief and
  fourteen language rules (MIT), and Cloudflare's security-audit (MIT).
- **Teams.** A person keeps several teams and gives each project one (`cauce
  team create`, `cauce team use`); a project's workers run only as its team's
  roles, and its tasks are classified only into the kinds that team covers.
  Teams, roles and which project uses which live in the person's cauce home.
- **`cauce roles`** (`list`, `show --full`, `copy`, `rm`, `check`, `skills`)
  and **`cauce team`** (`list`, `show`, `create`, `change`, `delete`, `use`);
  `cauce run --role <name>` runs a task as one role.
- **Teams screen** in `cauce ui`: every team, the roles in it with their
  kinds, skills, capabilities, tools taken away and instructions, and the
  projects using it (`GET /api/teams`).

### Changed

- **Color you read without reading.** The web UI gives every piece of state
  one tone, always beside its word:
  - a task's state is a badge with an icon (✓ done, ● running, ○ queued,
    ! blocked, ◆ needs approval, ✕ failed);
  - the kind of work is a colored dot by family (read, debug, test, docs, ui,
    build, plan, review): eight hues in a fixed order, checked for colour-blind
    separation and contrast in both themes;
  - a cell shows its model as depth (haiku light to fable dark) and its effort
    as five pips;
  - board columns, cards that wait on a person or failed, ladders, the routing
    pass rates and role cards wear the same tones.
- The mod's status entry and its toast carry the same tones as markers
  (🟠 waits on you, 🔵 running, ⚪ queued; ✅ passed, ❌ failed), and the toast
  says how each task ended: `cauce endings --json` gives each one's status.
- A system prompt over 16,000 characters goes to the worker in a file
  (`--append-system-prompt-file`).

## [0.8.0] - 2026-10-11

### Added

- **Workflows screen.** `cauce ui` draws each workflow run of the project as a
  graph of its steps:
  - columns by what each step waits on;
  - every step with its state, cell and tries, opening its task;
  - the run's progress and the branch holding its work so far, and what to do
    when a step waits.

  Below are the workflows the project can run, each with its version, steps,
  step memory, the runs holding it and any change waiting for them.
- **Map screen.** A mind map of the project, built from what cauce keeps
  without a model: topics → live notes → the code each is about, and problems
  → every fix tried. The typed links between notes are dashed cross-edges. A
  topic or problem folds, a filter prunes the tree, and a picked note shows
  its text and links. **Milestones** lists, newest first, the workflow runs
  that finished, the decisions the notes keep, and the features, refactors and
  plans that passed.
- **Attach a note to a step from the map.** A person's press carries a note
  into a workflow step's memory (`POST /api/workflows/attach`, token
  required). A workflow in use gets the change as a pending revision.
- `GET /api/workflows`, `/api/map` and `/api/milestones` read one project.
  SVG is drawn node by node with `createElementNS` and `textContent`, never
  markup, and the map keeps its folds and filter when live events arrive.

## [0.7.0] - 2026-10-09

### Added

- **Workflows.** A workflow is a saved chain of tasks that goes on by itself.
  A run queues its first steps. When a step passes, the engine queues the
  steps that waited on it and starts the dispatcher, with no session relaying
  anything:
  - a writing step starts its worktree from the commit the step before it
    kept;
  - a reading step (a review, a plan) reads that work in a worktree its tools
    cannot write to;
  - every brief says what the earlier steps found.

  Each step is an ordinary cauce task with its own cell, check, escalation
  and stops. A step that stops stops the run there, and a resume to a pass or
  `cauce workflow retry` goes on. The session that started the run hears of
  it once, at its end or when a step waits on the person.
- **Definitions** are JSON in three scopes, the nearest winning: the project
  (`.cauce/workflows/`), the user (the cauce home) and the bundled templates
  `feature`, `bugfix` and `review-fix`. `cauce workflow list|show|new
  [--from]|copy|save|edit|rm|validate` and `step add|set|rm` make and change
  them, and every change is checked before it is written. Changing a bundled
  template copies it to the user scope first. A definition holds no
  permission rule, approval or capability: those are flags of `cauce workflow
  run`.
- **Global workflows are held while in use.** A user-scope workflow is one
  definition reused by several projects, and each run keeps its state with
  its own project. Every definition has a version, and a run runs the copy
  and version it started with. While any active run uses a definition:
  - a change becomes a pending revision, and later changes build on it;
  - the revision applies by itself when the last such run ends or is
    cancelled (`cauce workflow pending [--apply|--drop]`, `--now` to write
    at once);
  - removing it needs `--force`.
- **Memory per step.** A step can carry cauce memory into its brief: `note:<id>`
  (a replaced note is read as the one that replaced it), `topic:<name>` (every
  live note in it) and `problem:<id>` (with every fix tried on it). Notes are
  read only from the run's own project. A run whose memory cannot be read
  there does not start. Set it with `cauce workflow step add|set --memory`.
- **Watching:** `cauce workflow status [<run>]` shows each step's state, task
  and cell, what runs now, what waits on the person and the branch so far.
  `cauce overview` lists the runs and names each step's run. `cauce workflow
  cancel` stops a run.
- **Guidance:** `/cauce:workflow` takes a session through all of it, and
  `/cauce:orchestration` now sends a sequence of phases to a workflow instead
  of relaying it by hand.

## [0.6.21] - 2026-10-09

### Added

- **A watcher outside cauce reads only its repositories.** `cauce overview
  --repo DIR` and `cauce events --repo DIR` (both repeatable) keep those
  repositories' sessions, tasks and events; `cauce events --last-id` prints
  where a watcher starts, and each event `--json` prints carries its task's
  title, repository and session. A page that shows some repositories (Tanka's)
  no longer reads every one to filter them itself.
- **`cauce resume <id> --pressed`.** A person's press on another page runs the
  same resume the UI's button does, read from cauce's account of the stop
  (`stops.resume_args`); a rule, approval, budget or `--keep` passed with it
  is refused. `cauce dismiss --via tanka` says the dismissal came from Tanka's
  page.

## [0.6.20] - 2026-10-09

### Changed

- **Habits are per project.** Every repository's tool events were mined
  together, so a Python repository was offered twenty TypeScript habits and
  none of its own, and a count summed over unrelated projects passed the gates
  for each of them. `cauce habits` (and the UI's habits screen, and
  `/cauce:habits`) now mine the current repository's own sessions and workers,
  gated on them alone, and list apart the habits two or more other
  repositories share that this one has not shown yet. `--all` mines
  everything together, as before. An event's repository comes from its task,
  else its session (99% of a real store's events have one); the PostToolUse
  fast path still writes no path.
- A bare interpreter run (`bash:python3`, `bash:node`: a script or a `-c`
  one-off) is no habit: `python -m pytest` is already `bash:pytest`. After a
  `.ts` write it was the model's scratch work, and the top candidate.

## [0.6.19] - 2026-10-09

### Added

- **`/cauce:habits`.** One skill reviews the habit candidates for the
  repository it runs in and installs the ones the person picks: it keeps those
  whose file type the repository has, whose last step is a fast check it
  really has (the exact command read from its own files), that pass now and
  are not installed yet; it offers to remove habits that turned themselves off
  or are installed twice. `cauce habits list --json` and
  `cauce habits status --json` (with where each habit lives) are what it reads.

### Fixed

- **A failed habit is said to the session.** A habit ran its check after each
  edit and kept the result to itself, so the session ran the check again by
  hand and the habit saved nothing. A failure now exits 2 with the end of the
  output, which Claude Code shows the session; a pass says nothing.
- **A habit is installed once.** The same command on the same file type in one
  settings file was installed twice, and ran twice after every edit.
  `cauce habits install` now says it is already there.

## [0.6.18] - 2026-10-09

### Fixed

- `cauce events` without `--follow` printed the first 200 events and stopped,
  so `--kind branch_pruned` found nothing in a store with more. It reads every
  page now.

## [0.6.17] - 2026-10-09

### Fixed

- **A typed command runs the newest cauce installed.** A session's PATH keeps
  the plugin version it started with, and `/reload-plugins` does not change
  it: one update later, `cauce prune` was an invalid choice. A command now
  forwards, once, to the newest install in Claude Code's plugin cache. Hooks
  stay on the version that registered them, and a copy outside the cache (a
  checkout) never forwards.

## [0.6.16] - 2026-10-09

### Added

- **Branches whose work landed are deleted.** A `cauce/task-N` branch of a
  task that is done (or dismissed) goes once nothing would be lost: its
  commits are on another branch (a merge), HEAD holds every file it changed as
  it changed them (a squash or a cherry-pick), or all it changed is tool
  artifacts. A branch checked out in a worktree, or of a task that may still
  resume, stays. Each deletion is a `branch_pruned` event on the task with its
  tip, so `git branch cauce/task-N <tip>` brings it back. It runs at a
  session's start for its checkout and every ten minutes in the UI server;
  `cauce prune [--dry-run] [--repo DIR]` runs it by hand, and the `prune`
  setting (on by default) turns it off.

## [0.6.15] - 2026-10-09

### Added

- **One session watches the others.** `/cauce:watch` makes a session the
  watcher of every session that uses cauce, one per project: `cauce overview`
  (and the mod's `overview` tool) lists every session seen lately with its
  directory, last prompt and work: what waits on the person and why, what runs
  on which cell and since when, what is queued, what ended. It reads and
  claims nothing, so each session still hears of its own endings. Sending a
  session work is Claude Code's `ListAgents` and `SendMessage`.
- `cauce events --kind <kind>` keeps one kind of event, and `--new` starts
  from now: `cauce events --follow --new --kind finished` is every project's
  endings as they happen.

### Fixed

- The README described the mod's band and pane, gone since 0.6.12.

## [0.6.14] - 2026-10-09

### Fixed

- **A task that builds something read-only still writes.** Since 0.6.10, a
  task whose text said "read-only" anywhere was briefed as one: a feature
  asking for a read-only version history ("historial de versiones solo
  lectura") was told it could change nothing, three times, and each stopped as
  a task found wrong; a docs task saying "do not change code" could not edit
  its README. The words must now be about the task ("READ-ONLY VALIDATION",
  "(read-only)", "do not edit any file"), a bound like "do not edit any file
  under src/admin" is no such declaration, and `feature`, `implement`,
  `refactor` and `docs` tasks are never read as read-only.
- **No review card for a branch of tool artifacts.** Validations committed
  before 0.6.10 left branches holding only a browser's logs and snapshots;
  the board asked for each to be reviewed and merged. A kept branch whose
  changed files are all `isolate.ARTIFACT_DIRS` waits on nobody.

## [0.6.13] - 2026-10-09

### Changed

- **Haiku 5.5.** Haiku has the effort dial now (low to max, default medium)
  and costs a tenth of what Haiku 4.5 did ($0.10 / $0.50 per MTok), so the
  matrix gives it its dial and its rates: `classify` climbs haiku/low →
  haiku/medium → sonnet/low, `explore` and `docs` haiku/medium → haiku/high →
  sonnet/medium. A cell older runs recorded as plain `haiku` reads as
  haiku/medium, the model's default. cauce's own questions to Haiku
  (classifying, dispatch, notes, session names) ask at `--effort low`.
  Spend is priced at the new rates. A real `explore` task in a sandbox
  passed at haiku/medium on claude-haiku-5-5 for $0.01.
- Pin Haiku with `cauce config model haiku claude-haiku-5-5` to keep workers
  on it whatever the CLI's alias says.

## [0.6.12] - 2026-10-09

### Fixed

- **A lane paused on a task opens when that task leaves the board.** A queued
  task waited 16 hours behind a lane paused on a task the person had already
  dismissed. Dismissing, superseding, or resuming the stopped task to a pass
  unpauses the lane and starts the queue again. A session's ending says when
  a lane is paused on it and how many tasks wait, and the guide tells the
  orchestrator to unpause it when the queued work does not depend on it.

### Changed

- **The board lives in the web UI only.** The mod's band above the prompt
  and its `/cauce` pane were a poorer copy of the web board: they are gone.
  The mod keeps its tools, waking the session on endings, and a status line
  in words; `/cauce` starts the web board. The task drawer gains Resume,
  Allow & resume, Approve & resume and Always allow here; the rules a press
  resumes with come from cauce's account of the stop, never from the page.

## [0.6.11] - 2026-10-09

### Fixed

- `--kind research` (and `audit`, `fix`, `validation`…) is read as the kind it
  means, said on stderr; a word that means none is a usage error that lists
  the kinds and says cauce classifies when `--kind` is left out. A session
  guessed a kind, the run never started, and the background pipe hid it.
- The orchestration guide names the kinds, says to leave `--kind` out, never
  to pipe a run through `tail`, and to read `task #N started` before telling
  the person a run is going.

## [0.6.10] - 2026-10-08

### Fixed

- **A validation that says it is read-only is read as one.** A task whose own
  text says "read-only", "solo lectura" or "do not edit files" gets the
  reading brief (its findings are the answer, broken code included), and two
  cells that changed nothing and found the same thing stop as `converged`
  instead of climbing to a stronger model. It does not hold the checkout.
- **Tool artifacts are no work.** Files a worker's tools write for themselves
  (`.playwright-mcp/` page snapshots and console logs, `__pycache__/`) are never a changed
  file and never committed to a kept branch: they made read-only tasks look
  like writing ones.
- **"needs running servers" is said only when it is true.** A task that names
  its servers by `localhost:<port>`, all of them answering, is not warned.
- A run waiting for the checkout says how to work beside it (a worktree) and
  that a wait is not a hang.
- The compaction digest leaves out what the person never typed: a prompt a
  plugin submitted, a bare slash command, "[Request interrupted…]".

### Changed

- The orchestration guide: never send copies of one task to compare
  answers; parallel runs that share state a run uses up (a demo user, its
  attempts) spoil each other; never cancel a run because it waits.

## [0.6.9] - 2026-10-07

### Added

- **cauce compact.** Before a compaction (`/compact` or automatic), cauce reads
  the session's transcript and git and keeps a digest of facts: the person's
  requests verbatim, the cauce tasks of the session, the calls that failed
  with their exact error, the files git shows changed and the files the
  session wrote. The SessionStart after the compaction puts it back beside
  Claude Code's own summary, which paraphrases from memory. No model call,
  so it invents nothing; it fits a budget, newest first, and requests never
  take more than half of it. It runs in any directory, notes setting or not.

## [0.6.8] - 2026-10-07

### Changed

- **A new look for `cauce ui`.** The default blue and grey gave way to the
  palette of a riverbed: wet stone for the ground, deep water for what
  moves, ochre for what waits on you, iron oxide for what stopped. Every
  text colour meets 4.5:1 against the surfaces it sits on, in both themes.
  One typeface (Avenir Next, Segoe UI Variable, then the system's);
  monospace only for real code. Cards carry a stripe in their status's
  colour; "Needs you" is the one lane with an ochre count; tabs are
  underlined; labels are sentence case; the brand mark is the channel
  itself. Task text and worker accounts read as prose, command output as
  code. Destructive buttons stay quiet until hovered.
- Switching to a view says "Loading…" at once: Spend reads every session's
  transcript and could take seconds with no sign the click landed.

## [0.6.7] - 2026-10-07

### Changed

- **An environment the worker named blocks at once.** Of 18 retries after an
  `environment` failure, none passed: 16 were the worker's own diagnosis (an
  old Node, a server it could not start, a missing CLI), which the same cell
  meets again at once. The one retry is now kept for a launch that broke (a
  timeout, no JSON from the CLI, an errored session); a worker's own
  `environment` verdict, or cauce's check that could not run, blocks with its
  account.
- **A feature starts with 60 turns.** Its attempts used 29.7 of 30 turns on
  average and a quarter ran out, each costing a fresh worker's orientation;
  the raised ones passed at 31 to 45. Other kinds keep 30, and `--max-turns`
  still wins. The plan says so in its reasons.
- **Routing has a period** (last 7 days by default, 30, or everything, kept
  per viewer): a rule fixed last week no longer shows as a rung that fails.
  A move that goes on but has no next attempt says why (`running`,
  `cancelled`) instead of "stopped".

## [0.6.6] - 2026-10-07

### Fixed

- **A refused write the worker got past blocked the task anyway.** Auto mode
  refused two `Write` calls in a task's own worktree; the worker wrote both
  files afterwards, and git shows them on its branch. cauce still read the
  refusal as a setting, blocked the task, and asked a person to allow two
  absolute paths in a temporary worktree, where the same attempt, out of
  turns, would otherwise have gone on with more. A refused write to a file
  in the worker's own directory that git shows changed after all is now no
  refusal: the attempt moves as its failure says, and the `attempt_finished`
  event lists it as `refused_but_written`. A refusal of anything else, or of
  a file the attempt never wrote, still blocks.
- A test of the checkout lock released the lock on a timer and could pass
  without the run ever waiting; it now releases when the run says it waits.

## [0.6.5] - 2026-10-07

### Fixed

- **A dismissal reached the session as an ending.** Dismissing (or a pass
  superseding) wrote a new `finished` event, so the Stop hook handed the
  session that sent the task "#192 ended dismissed" and blocked its turn's
  end. The person did it, or the pass's report already names it: both are
  now marked reported as they are written.
- **A run in a folder that is no checkout did not hold the checkouts inside
  it.** A run in a parent folder and one in a repository inside it worked at
  once. A run in such a folder now holds the folder and every checkout right
  inside it, in one sorted order.
- **A dismissed task that had been replanned offered `cauce resume`,** which
  refuses it. It now offers nothing, unless it had only outgrown its turns.

## [0.6.4] - 2026-10-07

### Added

- **`cauce dismiss <id>…`** takes a task that waits on you (blocked, replan,
  failed, needs approval, interrupted) off the board. Until now only a resume
  did, and a stale audit stayed in "Needs you" for good. Its attempts and
  result stay; the account says you did it and what it had stopped on, and
  `cauce resume` brings it back as what it was (a task found wrong is still
  rewritten, never resumed). A **Dismiss** button in the UI's task view, the
  session's cauce band and the `/cauce` pane does the same.
- **A pass takes its stopped copies off the board.** Earlier tasks with the
  same text in the same repository that stopped short of a pass are dismissed
  as `superseded`, naming the task that passed; its report lists them.
- **`cauce memory forget <id>…`** and a **Forget** button in the UI's Memory
  view: a problem that was never the work's (the environment, two runs in one
  checkout) leaves memory with its fixes, and no brief shows it again.

### Fixed

- **Hide in the session's band brought every card back** as soon as any one
  changed: it remembered the whole list at once. It now puts each card away
  on its own; a card comes back only when its own status changes.
- **"Done" grouped by day but listed by id**, so a task resumed today showed
  under an older day. It is now ordered by when each task ended.
- **`--start haiku/low` crashed with a traceback** mid-run. A cell with no
  such dial is now a usage error, said before anything runs.

## [0.6.3] - 2026-10-07

### Fixed

- **A session kept running the cauce it started with after an update.**
  Its Bash put the old version's `bin/` first on the PATH, so its runs went
  on without the update's fixes: an environment failure that had changed ten
  files was retried, a rule 0.6.2 had already fixed. `cauce run` and
  `cauce resume` now say at once when a newer cauce is installed, and ask for
  `/reload-plugins`.
- **Two runs without a worktree worked in one checkout at once.** One
  reinstalled `node_modules` while the other's check ran, and the check
  failed with `Cannot find module 'vite'`: a `code_bug` that was neither
  task's work, and a climb to more effort. A run in the checkout itself (no
  worktree, and it writes or has a check) now holds it: a second one waits,
  says which task it waits for, and still stops on a cancel.
- **An errored session said "the session errored: success".** An API error,
  such as a usage limit, arrives with subtype `success`; the reason is in the
  result text, and that is what the summary now shows.

## [0.6.2] - 2026-10-06

### Fixed

- **A worker ran an older Node than the project's tools need.** The project
  declared no Node, but one of its dependencies did: a CLI with
  `engines.node >=22.22.3`. The worker got the PATH's 22.16, and every `npm
  test` failed before running. When a project declares no Node, cauce now
  reads the `engines.node` of its installed direct dependencies. When the Node
  on the PATH fails them, it puts first an installed Node that fits, of the
  same major when there is one, so native modules still load. When none fits,
  the plan warns at once, naming the dependency and the version. A spec cauce
  cannot read, a dependency that is not installed, or an unknown current Node
  changes nothing.
- **An environment failure was retried after the work had already run.** The
  retry met the same wall. An environment failure on an attempt that changed
  files now blocks at once, and the work is kept on its branch. Work that
  never ran is still retried once.
- **A check that could not run counted as a bug in the work.** Examples: a
  missing npm script, a missing `package.json`, a tool's minimum Node, or
  exit 127. The task climbed to stronger models against a wall none of them
  could pass, and the failure was kept as a dead end. Such a check is now
  `environment`. A failing test that only quotes such text, or that runs
  after npm's EBADENGINE warning, is still a bug in the work.
- **Unrelated failures reached workers as dead ends.** The brief matched any
  word of the task, so tasks in the same repository that shared a word or two
  showed up as if they were this problem's. It now matches as strictly as the
  prompt hook does.
- **A resumed task numbered its attempts from 1 again** in its progress lines
  and its `attempt_started` events. They now go on from the task's last
  attempt.

## [0.6.1] - 2026-10-06

### Fixed

- **A run in a background shell said nothing for minutes, and had no task to
  show.** A stale or missing livespec index was rebuilt before the task was
  created, and on a large repository that took minutes. The index now
  builds in the background, under the same lock as every other refresh. The
  task starts at once: it uses the index as it is, or runs without one. A
  repository livespec cannot index says so, instead of claiming a refresh is
  running.
- **`cauce run` and `cauce resume` printed nothing until the end.** They now
  print progress to stderr as it happens: the task number, warnings, each
  attempt and how it ended. The report on stdout stays the same. A warning
  that a task needs a browser or running servers now shows before any money
  is spent, not only in the final report.
- **The mod's tools answered "Invalid tool parameters" to a value outside a
  list.** This happened with a kind cauce does not have, a topic alias, or an
  id written as `"42"`. The schemas no longer constrain these values; the
  handler checks them instead and says what it accepts:
  - an unknown kind is dropped and cauce classifies the task;
  - cauce resolves a topic, aliases included, and an unknown one comes back
    with the project's topics;
  - `note` without a topic is filed by its words;
  - links it cannot read are named in the answer.

### Added

- **Fewer approvals for the same thing.**
  - `cauce resume <id> --allow <rule> --keep` also keeps the rules for every
    task in the task's repository, so the next task there is not refused the
    same program. It is refused together with `--unattended`.
  - The mod's band and pane offer **Always allow here** next to *Allow &
    resume*.
  - The mod's reading tools (`recall`, `tasks`, `note`) are never put to the
    person. A rule or setting that denies them still does. `queue` and
    `resume` spend money and stay under the person's rules.

## [0.6.0] - 2026-10-06

### Added

- **cauce as a Claude Code mod.** The plugin also loads `hooks/cauce.tsx`, a
  module of function hooks over the same core. The classic hooks stay, so
  nothing depends on it.
  - **Tools the model calls by name**: `recall`, `note`, `queue`, `tasks` and
    `resume`. They need no PATH and no permission rule (none was asked in
    default mode), and `note` takes only the project's topics.
    - `resume` runs with `--unattended`. It refuses what only a person may
      clear, a refused command or an approval, and says so. No model input
      reaches `--allow`.
  - **The session wakes when work it queued ends.** When a queued task ends
    while the session is idle, cauce starts a turn with the notice, and the
    session acts on it without waiting for the next message. Work that ends
    during a turn is still delivered by the Stop hook. The `wake` setting
    turns it off.
  - **A status entry, a band above the prompt and a `/cauce` pane.**
    - The band lists what waits on you, with a button that continues it with
      the rule cauce suggests: *Allow & resume*, *Approve & resume* or
      *Resume*.
    - The pane cancels running work and confirms or drops notes to review.
- `cauce endings --session <id> [--peek] [--json]`: what ended since a
  session last heard.
- `cauce resume --detach`: run a resume in the background; its ending reaches
  the session that sent the task.
- `cauce resume --unattended`: refuse a resume that only a person can clear.
- `cauce note --json` and `cauce notes topics --json`.

### Fixed

- **An ending could be delivered twice.** The Stop hook and the next prompt
  each read and then marked it, so two at once could both deliver it. Now the
  ending is claimed in one transaction.
- **The board asked to review branches that had already landed.** A merged,
  squash-merged or deleted branch no longer waits on anyone.

## [0.5.0] - 2026-10-06

### Added

- **Project notes: what a project knows, by topic.** Problems and fixes stay
  global. Each project now also keeps notes of what it knows, so a session
  that compacted, or a worker that just started, reads it back instead of
  rebuilding it from the code.
  - **Topics.** `business`, `code`, `decisions`, `conventions` and
    `environment`, plus any a person adds to a project (`cauce notes topic
    add`). Haiku files under the topics that exist. When none fits it
    proposes one, and a person decides.
  - **Links between notes.** `depends_on`, `explains`, `replaces` (retires the
    old note), `contradicts`, `example_of`. Recall follows them one step.
  - **Anchors.** A note is tied to a file, or to a symbol from the livespec
    index (read-only). When a commit that changes it reaches the checkout,
    the note goes to review. A branch not merged yet changes nothing, and a
    squash merge moves the anchor along. `cauce notes ok`, `cauce notes drop`.
  - **Written at three moments.**
    - A passing worker's result block now has `learned`. Haiku files each
      fact, anchored to the files the task changed as its branch has them.
    - Before a session compacts and when it ends (`PreCompact`,
      `SessionEnd`), a detached Haiku reads what was said since the last
      time and keeps the durable facts.
    - A person keeps one with `cauce note "<fact>" --topic <t>`.
    - A failed attempt's facts are never filed, and nothing that looks like
      a secret is kept. Without Haiku, a fact is kept under the topic its
      words point to.
  - **Read back in four places.**
    - Every worker's brief gets the notes that match its task.
    - A prompt that matches notes gets them in context.
    - A session that starts, resumes or compacts gets the project's index of
      notes.
    - `cauce recall "<question>" [--topic] [--path]` answers on demand.
  - **The UI's new Notes tab.** One project's notes by topic, with their
    links and anchors. A note to review can be confirmed or dropped there.
  - **The `notes` setting.** On by default: plugin option, `cauce config
    notes off`, `CAUCE_NOTES`.

## [0.4.10] - 2026-10-06

From running the installed plugin end to end: real `claude -p` workers, a
session queueing with `++` and with `cauce queue add`, a cancel, the UI.

### Fixed

- **An audit was not read as a review.** "Audit calc.py for bugs" matched no
  rule. Haiku read the request as its own task and answered with what it would
  do first ("looking for calc.py"), which is always a search. Its confidence
  swung, so the same audit became `explore` one time and `implement` the next.
  Now:
  - A rule reads an audit or a hunt for bugs (`audit`, `audita`, `busca bugs`,
    `find bugs`, `qué problemas tiene`) as a review, critical when it touches
    auth or money. A verb of change still makes it `implement`.
  - The classifier gets the request as data to classify (`<request>`), told
    not to carry it out.
- **A task queued with `cauce queue add` sat in the queue.** Only `++` started
  the dispatcher. The main session queues through Bash, so its tasks waited for
  a `cauce work` nobody ran. `cauce queue add` now starts the dispatcher too,
  under the same `autowork` setting, and says what happens next.
- **Cancelling a task paused its repository's queue.** The next task waited
  for `cauce lanes --unpause`. A person's cancel is their call on that task,
  not a broken state, so the queue goes on.
- **Haiku named sessions "Unable to determine - incomplete prompts provided".**
  It read titles cut at a few words. It now reads the prompts themselves and
  must always answer with a name.
- **Every repository a session opened got a `.mcp-docs/` folder.** The
  livespec index was built at session start in any git checkout. Now it is
  built only in enrolled projects.
- **The plan warned about refusals that no longer apply.** It listed commands
  refused under `acceptEdits` before auto mode. Now it counts only refusals in
  the mode the next worker runs in.
- **The drawer listed dead ends a task was never shown.** It searched the
  memory as it is now. It now shows what the run and the prompt recorded when
  they showed them.
- **The notice of a finished task left out its answer.** A pass now carries
  the worker's summary and the changed files. A pass that changed nothing says
  so, instead of "its changes are in the checkout".
- **Every Haiku call cauce made was logged as a tool call.** The classifier,
  the dispatcher and the namer answer through `StructuredOutput`, and each
  answer landed in the log that habits are mined from. It is not a step of
  anyone's work, so it is no longer logged.
- **A pass outside a worktree left out where its changes were.** This happens
  in a folder of several repositories. The report now says the changes are in
  your checkout.

## [0.4.9] - 2026-10-06

### Fixed

- **An audit that found problems was read as failed work.** A read-only task
  (review, explore, plan) whose worker found the audited code broken answered
  `fail` / `code_bug`, about the code it read. cauce reads `code_bug` as
  shallow work, so it climbed effort and model. One audit gave the same
  findings seven times, from opus/high to opus/max, for $6.60, and then
  suggested resuming it. Now:
  - A reading worker is told that the answer is the deliverable. Findings,
    broken code included, are a `pass`, with each finding in the summary and
    what shows it in the evidence. `code_bug` is never about the code it
    read.
  - Two cells of a reading task that report the same findings stop the run
    as `converged`. The findings are confirmed and the answer is in the
    worker's account. The task suggests acting on them, not resuming, which
    would repeat them.
  - The notice that tells the main session a task ended now carries what the
    worker found.

## [0.4.8] - 2026-10-06

From a session where one read-only audit went five attempts deep, each refused
different commands, and a person ran it by hand in thirty seconds.

### Changed

- **Workers run in Claude Code's auto mode.** Under `acceptEdits`, every
  command not named by a rule was refused. A worker that needed `node`
  reached for it five ways (`nvm`, `~/.nvm/.../node`, `/opt/homebrew/bin/node`,
  …) and met five refusals, one new `--allow` each. In auto mode a classifier
  lets safe commands through with no rule per command. Haiku is not served by
  auto mode, so Haiku workers run with `bypassPermissions`. This is a
  deliberate choice: Bash in a Haiku worker is unchecked, and deny rules still
  hold, so a reading task's write tools stay removed. `cauce config mode
  <model|default> <mode>` changes either. A CLI that does not take the mode
  runs the attempt again in `acceptEdits` instead of failing.
- **A refused attempt never climbs.** A worker refused its tools, then its
  check went red. That read as shallow work and moved to more effort
  (opus/high → opus/xhigh), which met the same refusals. Any attempt with a
  refusal now blocks, unless the worker found the task itself wrong.

### Fixed

- **Workers were refused the MCP servers cauce handed them.** A capability,
  livespec included, was configured for the worker but its tools were never
  allowed. The tools of every server cauce hands a worker are now allowed
  with it (`mcp__<name>`).

### Added

- **`cauce allow`: rules kept for a repository.** `--allow` grants a rule to
  one run. A new task in the same repository started with none and met the
  same refusals again. `cauce allow 'Bash(npm:*)'` keeps rules for every run
  and resume in the repository, `--preset read|node|python` adds the usual
  sets, and `--rm` takes rules back. They live in the person's cauce home,
  never in the repository. A blocked report also prints the `cauce allow`
  command that keeps its rules.
- **The project's Node goes first on the worker's PATH.** It is read from
  `.nvmrc`, `.node-version` or `engines.node` (for a folder of projects, the
  version that fits them all), found among the versions nvm installed, and
  put first on the PATH of the worker and of its `--verify`. The plan says
  which one. Workers are told to run tools by name, never through a version
  manager or a full path.
- `/orchestration` answers quick read-only lookups itself: a worker's setup
  costs more than a grep.

## [0.4.7] - 2026-10-06

### Fixed

- **Tasks from before 0.4.6 said "you" for text the main session wrote.**
  Back then every task's text was recorded as `user`, and 0.4.6 read `user`
  as the person. Now:
  - text a person typed (`++`, or `cauce run` / `cauce queue add` outside a
    session) is recorded as `person`, so `user` only means a session's own
    prompt, or an older record;
  - on an older task sent from a session, the drawer shows
    "main session (orchestrator, inferred …)", marked as inferred, since
    cauce did not record who wrote it;
  - a session's own prompts, and older tasks run from a terminal, still say
    "you".

### Added

- **The main session hears when the work it sent ends.** A queued task, or a
  run in the background, could end while the main session waited on nothing,
  so it never told the person, continued or relaunched. Now each ending is
  reported to the session that sent it, once:
  - when the session is about to end its turn, the Stop hook blocks it once
    with what ended, why, what changed so far and the command that goes on;
  - when the session is idle, it hears with the person's next message;
  - a `cauce run` or `cauce resume` whose report the caller read already
    counts as reported.

  Only endings of the last 24 hours are reported, so old work does not come
  back.
- **A task that outgrew its turns can be continued.** A `replan` because a
  task did not fit the raised turn budget is not a wrong task. It now resumes
  with `cauce resume <id> --max-turns 120` (or any `--max-turns`), and in a
  worktree its work is kept on its branch instead of dropped.

## [0.4.6] - 2026-10-06

### Fixed

- **The UI listed sessions from every folder, not just enrolled projects.**
  To find a session's `.cauce/`, cauce climbed every parent folder, and
  outside a git checkout it went all the way to `/`. So one `.cauce/` high up
  enrolled every session below it: in a folder that holds several projects,
  in the home directory, or at the top of a home that is itself a git
  checkout (dotfiles). Now a session belongs to the `.cauce/` in its own
  directory, or in a parent up to the top of its checkout, and never above it.
  Outside a checkout only its own directory counts. The home directory and `/`
  are never a project, and `cauce init` refuses them. A stray `.cauce/` left
  in a high folder no longer lists anything below it, with no cleanup needed.
- **Work in nested repositories read as "changed nothing".** A task run in a
  folder that holds several git repositories reported `changed (from git):
  nothing` while its worker had edited two of them. The folder is not a
  checkout itself, and a checkout's own status never looks inside a nested
  one. Each attempt now also reads every git checkout up to two levels below
  its work directory (dependency folders aside), with paths prefixed by
  where each one lives, including what a worker committed in them.
- **Rules read back from older refusals missed programs.** A refusal cut at
  120 characters lost its last command even when only that command's
  arguments were cut, so `~/.local/bin/x -n "…" src/...` gave no rule.
  A refusal cut inside a quote lost everything after the quote opened. Now
  the program is kept whenever it is whole, and a cut quote is closed before
  the line is read.
- **The task's text was always labeled "user".** When the main session sends
  work through its own Bash (`cauce run` or `cauce queue add` with
  `CAUCE_SESSION_ID` set), or delegates to a subagent, the text is the main
  session's, not something a person typed. It is now recorded as
  `orchestrator`. The task drawer labels each message by who wrote it: you,
  the main session (orchestrator), the main session's answer, or cauce's
  report. Older tasks keep `user`.
- The task drawer no longer shows an empty "evidence" block for an attempt
  whose evidence was blank.

### Added

- **The plan says when a task needs what a worker does not have.**
  - **A browser:** words like "browser", "navigate" or "screenshot", with no
    browser capability registered.
  - **Running servers:** "dev servers", `npm start`, `ng serve`,
    `localhost:<port>`. A server a worker starts stops when the worker
    finishes.

  The note appears before any money is spent, in the report and in
  `--dry-run`.
- `cauce capabilities --example` includes a browser (Playwright MCP) for
  `test` tasks, and for `ui` tasks after a failure.
- Workers are told that a command that never returns blocks the call it runs
  in. They should start it in the background and stop it before they finish.
  Without a browser they answer `environment`, never a description of a page
  they did not open.

## [0.4.5] - 2026-10-06

### Added

- **Which model actually served each attempt.** A worker is started with an
  alias (`--model sonnet`), and the installed Claude Code decides which model
  that is. An older Claude Code can still map `sonnet` to an earlier Sonnet
  while a person's own sessions already run the newer one. Each attempt now
  records the model id the CLI reports in its `modelUsage` (the one that did
  most of the work). The report, `cauce show` and the task drawer show it.
- **Pin an alias to a model id:** `cauce config model sonnet claude-sonnet-5-5`
  (or `CAUCE_MODEL_SONNET`). `cauce config model sonnet default` unpins it, and
  `cauce config model` lists every alias with its pin and the model that last
  served its workers. Unpinned aliases go through as before, so cauce keeps
  following Claude Code and any provider's model names. The classifier's
  Haiku honours the pin too.
- **The plan warns when workers run an older model than your sessions.** If
  the newest model of a family in the person's sessions is a newer version
  than the one that last served the workers, the plan (and `--dry-run`) says
  so, with the command that pins it.

### Fixed

- `store.home(env)` took the home directory from the running process instead
  of the environment it was given. With that, a test leaked livespec index
  locks into the real `~/.local/share/cauce`.

## [0.4.4] - 2026-10-06

From a review of real sessions where most blocked tasks were finished by
hand.

### Fixed

- **The suggested `--allow` rules could not be used.** A refusal was suggested
  as the whole call, chained and cut at 120 characters, e.g.
  `Bash(search -n "x" src; echo "exit $?"; g...)`. Allowing that allowed
  nothing. Refusals now become one rule per program, read from the full call
  (`src/cauce/allow.py`):
  - a shell line is split into the commands it chains, and each one gets a
    prefix rule: `Bash(npx tsc:*)`, `Bash(npm run build:*)`,
    `Bash(git status:*)`. `bash -c` is read for its script;
  - an absolute file path gets two slashes (`Read(//abs/path)`). With one,
    Claude Code reads it relative to the settings file;
  - a URL becomes its domain.

  The report, the `blocked` reason, `cauce show`, the board and the resume
  command use these rules. Tasks recorded before get them read back from what
  was refused.
- **A task started with `--no-isolate` resumed in a worktree.** There it could
  not see the uncommitted work it was continuing. The choice is now kept with
  the task's options; `cauce resume --no-isolate` also sets it.
- **One click on Cancel in the task drawer stopped a run.** Cancel now takes
  a second click within four seconds.

### Changed

- **A worker that runs out of turns or money is checked by cauce.** Before,
  such a worker never reached `--verify`, and a large change ended without
  its tests ever running. Now cauce runs `--verify` itself whenever the task
  changed something: a green check passes the attempt, and a red one gives
  the next attempt the failing output, at the end of its brief. Every failing
  attempt's output now reaches the next brief this way.
- **Turns are raised while the work keeps moving.** A run that hit its turn
  ceiling twice used to be sent back to be split, even when the raised
  attempt had changed dozens of files. Now a raised attempt that changed files
  is raised again (30 → 60 → 120 → 200). A raised attempt that changed
  nothing, or the 200 cap, still means split it.
- **The plan names the rules workers in this repository lacked before**, with
  counts, leaving out those already granted. `cauce run --dry-run` shows them,
  so they can be passed with `--allow` from the start.
- **Workers are told how refusals work.** Nobody can approve a command while
  they run: read and search with the Read, Grep and Glob tools, and run one
  shell command per call, since one refused part refuses the whole chain.

## [0.4.3] - 2026-10-06

### Added

- **Every climb says where it went and why.** A move after a failed attempt
  used to keep only its name and a short reason ("code_bug: same model, more
  thorough"). Its destination could only be read off the next attempt. Each
  decision (`escalate.decide`) now carries the full account:
  - the dial it turned: effort, model, turns, retry, or stop;
  - from which cell to which, and the turns before and after;
  - the evidence chain: what the attempt ended with and what that failure
    means, the rule it was read against (shallow work climbs effort; a wrong
    approach or two cells agreeing changes model; a refusal is a setting),
    and the cell it goes to with that effort's purpose;
  - the ladder cells it skipped, and why;
  - the budget left.

  The `moved` event keeps all of it (with its attempt's `seq`).
- **Where it shows:**
  - The run's report and `cauce show` print the account under each attempt.
  - The task drawer shows each move as from → to, with the numbered evidence
    and the skipped cells.
  - The task drawer's plan draws the ladder with each attempt on the cell it
    ran at: red where it failed, green where it passed.
- **Routing** gains *How each ladder was climbed*: per kind, every move with
  the cell it left, the failure behind it, where the next attempt ran, how
  many times, and how often that next attempt passed. This is the evidence
  for raising a start or dropping a rung. It is built from the attempts, so
  tasks from before 0.4.3 count too.
- A task from before 0.4.3 shows its moves from what its attempts recorded,
  marked as such.

## [0.4.2] - 2026-10-06

### Added

- **Every task that stops says why, who made the call, and what continues it.**
  A *Needs you* card used to say the same sentence for every `blocked` task,
  and a cancelled task did not say whether a person cancelled it. Each place
  that ends a task without a pass now records one account on its `finished`
  event (`src/cauce/stops.py`): the cause (`permission`, `budget`,
  `environment`, `approval`, `spec`, `turns`, `ladder`, `exhausted`,
  `attempts`, `cancelled`, `signal`, `died`, `missing_dir`, `crashed`), who
  made the call, the reason in full, the refused rules, the last worker's own
  account, and the command that continues it, such as
  `cauce resume 12 --allow 'Bash(npm run build)'`, `--allow-approval` or a
  doubled `--budget`. Those places are the run's own loop, `cauce cancel`,
  `cauce queue rm`, the UI's cancel, Ctrl-C, the dispatcher and the sweep.
  A cancel records where it came from (the UI, `cauce cancel`,
  `cauce queue rm`, Ctrl-C). A SIGTERM nobody asked cauce for is told apart
  from a person's cancel, and so is an error in cauce itself.
- The board's cards and the task drawer show that account, with the command
  ready to copy. The drawer also shows what each attempt was refused and the
  evidence of each failure. A task that stopped before 0.4.2 gets an account
  read back from its attempts and events, marked as read back.
- `cauce show <id>` prints each attempt's refused rules and the account. When
  a session resumes or is compacted, it is told which of its tasks stopped for
  a person, why, and the command, so it can tell them instead of guessing.
- The session drawer shows what each open item of Claude Code's own task list
  waits on (`blockedBy`).

### Fixed

- `cauce resume` lost what earlier attempts were refused: the resumed brief
  no longer said `refused: …`. It is read back from the attempts' events.

## [0.4.1] - 2026-10-05

### Fixed

- **The board kept showing every prompt in Done after the update that
  stopped it (0.1.2).** A `cauce ui` server keeps the code it started with,
  and a plugin update never restarted it: a board started in a session before
  an update went on serving that version's board. Now the server checks
  every 30 seconds what Claude Code has installed. When a newer cauce is
  there, it runs that version's UI in its own place, on the same port. The
  open page reads the new `GET /api/version` and reloads when the version
  changes. A UI from before 0.4.1 cannot do this itself: stop it once.
- **`cauce ui` on a taken port** failed with a Python traceback. It now says
  who holds the port. If it is this same version, it prints the board's
  address and opens it with `--open`. If it is an older cauce UI, it explains
  that this server shows the old board until it is stopped. If it is another
  program, it suggests another port.

## [0.4.0] - 2026-10-05

From an audit of a session where five tasks ended `blocked` and their reports
said "I created the routes" while the repository had none of them.

### Fixed

- **Work a worker did was thrown away when its task stopped.** A task that
  ended `blocked` or `needs_approval` had its worktree and branch deleted, so a
  worker's true account of what it wrote read as a lie. That work is now kept
  on `cauce/task-<id>`, committed as unverified, and the report says so.
  `failed`, `replan` and `cancelled` still leave nothing.
- **A refused command was retried, then reported as "the environment failed
  twice".** A one-shot worker is refused whatever its settings do not allow
  (`npm run build`, `node`). cauce now reads the CLI's own
  `permission_denials`. A refusal is the new `permission` failure, and it
  blocks at once: retrying it, or sending a stronger model into it, gets the
  same refusal. The report names each refused call as the rule to allow.
- **A fresh worktree could not build.** It has none of the checkout's ignored
  folders. The checkout's ignored `node_modules`, `.venv` and `venv` are now
  linked in, and the worker may read them. The person's
  `.claude/settings.local.json` is copied in, so a worker is allowed (and
  refused) what the person set. Neither is ever committed.
- **A pre-commit hook could lose a task's work.** cauce's own snapshot commit
  on the task branch runs with `--no-verify`. A hook that needs approval, or
  an install the worktree lacks, no longer stops the work being kept. The
  person's hooks run when they commit or merge.
- **A request to create files could get a read-only worker.** "Crear 5
  módulos…" read as `explore` gets a worker with no write tool, which can only
  describe changes it did not make. A request that opens a clause with a verb
  of change (create, add, register, fix, update…, in English and Spanish) is
  never read as `chat`, `explore` or a review, unless a `#tag` or `--kind`
  says so. A read-only worker asked for a change answers `spec_bug`, so the
  run ends `replan`.
- **"Fix it, then resume" named no command**, and the orchestrating session
  tried `cauce continue`, which never existed.

### Added

- `cauce resume <id>` runs a task that stopped (`blocked`, `needs_approval`,
  `failed`, `cancelled`, `interrupted`) again under its own id. It continues
  on its kept branch from the cell it stopped at, with its earlier attempts in
  the brief. It reuses the task's verify command and grants; `--allow`,
  `--verify`, `--budget`, `--start` and `--allow-approval` add to them or
  replace them.
- `--allow "<rule>"` on `run`, `route`, `queue add` and `resume` (repeatable),
  e.g. `--allow "Bash(npm run build)"`. It is passed to workers as
  `--allowedTools` and kept with the task.
- Every report of a writing task prints `changed (from git): …` next to the
  worker's summary. The summary is labelled as the worker's own account, and
  a kept branch says whether it is ready to merge or unverified.
- `--verify` can settle an attempt whose worker was refused the command that
  would check its work, as long as the task has written something and the
  worker did not call it a failure. A green check is a pass; a red one stays
  blocked, with the check's output as evidence. The worker's claim alone is
  never enough.
- `/orchestration` tells the session to grant commands up front with
  `--allow`, to say a file was created only when `changed (from git)` lists
  it, to resume a blocked task instead of redoing it, and to use only the
  commands `cauce --help` lists.

## [0.3.2] - 2026-10-05

### Fixed

- A task failed with livespec's "database is locked". Several refreshes of one
  livespec index could run at once — the session-start refresh, and since
  0.2.0 every parallel task planning in the same repository — and livespec
  lets only one through. Refreshes of one index file are now serialized under
  a lock in cauce's home: a refresh that finds another running waits for it and
  then indexes only if the index is still behind; the session-start refresh
  runs as `cauce index-livespec` under the same lock and is not started while
  one runs. Reproduced with three real refreshes on a fresh repository: one
  failed before, none after.

## [0.3.1] - 2026-10-05

### Fixed

- Habit candidates listed false positives: `find → ls`, `mkdir → cd`, `cat →
  cd → cd` repeat in every session, but no hook could take them over and
  installing one would save nothing. A candidate now has to start with an
  edit or a write (what a hook is triggered by) and end in a command that does
  something — not a program that only looks, moves around or prints.

## [0.3.0] - 2026-10-05

### Added

- **Enrolled projects.** A project is cauce's when it has a `.cauce/` folder,
  at the top of its checkout or where the session runs. Only enrolled projects
  and their sessions show in the UI and in `cauce projects` / `cauce sessions`
  (`--all` lists the rest). cauce enrolls a project the first time work is
  queued or run there; `cauce init` does it by hand. The folder ignores itself
  in git.
- **Session names.** Haiku names each session of an enrolled project in
  `.cauce/sessions.json`, after its first prompt and every ten after, started
  detached from the Stop hook (`names`, on by default, a plugin option). A
  name a person edits there is theirs and is never replaced. The UIs show the
  name, with the last prompt under it.

### Changed

- The board no longer carries a line explaining where work is asked for.

## [0.2.0] - 2026-10-05

### Changed — who decides what waits

- **Haiku decides whether a queued task runs in parallel or waits its turn**
  (`parallel`, on by default). The first task of an idle repository starts;
  one queued behind running work starts beside it only when Haiku, reading it
  against everything running and queued ahead, finds it independent. The
  decision is made once, kept on the task with its reason, and fails closed:
  no answer means waiting. Up to three tasks run at once per repository.
  `parallel off` keeps one serial lane per repository.
- **The dispatcher runs each task as its own process** (`cauce run-queued`),
  so a task's pid, its cancel and the sweep are its own, and it watches several
  at once. One dispatcher per repository, held by a lock file; a second
  `cauce work` for the same repository says so and exits.
- **`++` starts the dispatcher** (`autowork`, on by default): queued work runs
  without anyone typing `cauce work`. Unpausing a lane in the UI resumes it.
- **Work typed in a session is that session's.** SessionStart exports
  `CAUCE_SESSION_ID` to the session's commands, so `cauce run` and `cauce queue
  add` record it; workers and the dispatcher never inherit it.
- Both new settings are plugin options, like `livespec`.

### Changed — the UI

- **One project at a time, always.** A project picker at the top; the board and
  the sessions are that project's, and `/api/board` and `/api/sessions` refuse
  a request without one. The board narrows further to one session.
- **Sessions** screen: every session cauce saw in the project with its resume
  command; opening one shows its own task list as Claude Code keeps it (read
  from `<config>/tasks/<session>/`, or the older `todos/` file — never
  written; absent and unreadable told apart), the work it gave cauce, and its
  recent turns.
- **No task is typed into the page.** The queue form and `POST /api/queue` are
  gone: work is asked for in a session, where it has the session's context and
  record. Queued cards show Haiku's call and its reason.

## [0.1.2] - 2026-10-05

### Fixed

- The board showed every prompt typed in a session as a card, so Done filled
  with turns and pushed real tasks out of its 60 slots. The board now holds
  only work cauce runs or queued (`source` cauce or queue), filtered in SQL;
  the sessions answering a prompt right now are listed apart as `answering`
  (a count under Running in the UI, and `counts.answering`). Prompts are still
  recorded: they couple follow-ups and results to a turn, list unfinished
  work after a resume or compaction, and feed the Sessions view.

## [0.1.1] - 2026-10-05

### Added — the core

- **Model × effort routing.** Fifteen kinds of work, each with a ladder of
  cells; all five effort levels used, each for its own purpose; Haiku without
  an effort; Fable only on a `#fable` tag or `--allow-approval`.
- **Classification:** rules that are right when they fire, then Haiku with a
  JSON schema and no tools; below 0.6 confidence, or on any failure, the
  default kind.
- **Escalation on evidence:** shallow work climbs effort, a wrong approach or a
  repeated answer changes model, a timeout retries once, a turn ceiling is
  raised once, a wrong task replans. A worker's pass needs verbatim evidence;
  `--verify` has the last word.
- **One-shot workers** via `claude -p`: per-attempt model, effort, turn and
  dollar ceilings; read-only kinds get no edit tools; exactly the selected MCP
  servers; launched in one directory and working in another; no session kept.
- **Isolation:** one git worktree per writing task; a passed task leaves one
  branch, a failed one leaves nothing.
- **Capabilities** from a user-level registry: always, per kind, or after a
  failure; a hint per capability goes into the worker's system prompt.
- **Memory** in one SQLite file: tasks, the messages coupled to them, every
  attempt (the reroute trace), and problems with the fixes tried — searchable
  across repositories, this one first. History raises where a kind starts.
- **Hooks:** a prompt is a task, a follow-up joins its turn, a bare "continue"
  folds into the turn it pushes, a matching dead end reaches the model before
  it starts. Hooks fail open and report what they swallowed.
- **`/orchestration`**, the one entry point; the `cauce` CLI for everything else.

### Added — habits

- Every tool call, in sessions and workers, is logged as a signature plus an
  argument hash — never arguments, contents, output or prompts — by a hook
  fast path that appends one line to a file (about 50 ms with the launcher).
- `cauce habits`: repeated sequences past boolean gates, ranked by score.
- Recipes: the steps before passing worker attempts of a kind go into the
  next worker's brief.
- `cauce habits install|uninstall|status`: a habit becomes a `PostToolUse`
  hook only by that command; three failures in a row turn it off.
- The classifier on a local Laya model was not taken: rules and Haiku already
  route, and Laya needs a 1–3 GB install.

### Added — memory that remembers being wrong, and real spend

- Fixes carry `believed_from` and `invalidated_on`; a solved problem that
  turns up again is `recurring` and its fix disproved; `cauce memory
  invalidate`. Disproved fixes are dead ends, with what worked instead.
- A fix that worked is anchored to the commit on the task's branch.
- Token usage per model is read from the session transcript at `Stop`
  (incrementally, one record per message, a runaway line skipped);
  `cauce spend` joins it with worker spend by cell. Plans state what
  finished tasks of the same kind cost on average.

### Added — live state, the queue and its lanes

- An event trail per run (`cauce events --follow`), the cell in flight, and
  `cauce cancel`.
- `++ <task>` in a session, or `cauce queue add`, queues work at zero tokens;
  `cauce work` drains it in workers. One serial lane per repository, paused by
  a task that does not pass; `cauce lanes` shows and reopens them.
- A sweep marks runs whose process died, and session prompts abandoned for 12
  hours, as interrupted.
- Subagent delegations are child tasks; a turn is not done while one runs,
  background ones included (closed by their task notification).
- Pinned cells are recorded and never teach the router.

### Added — the UI

- `cauce ui`: a local page with a board (needs you, running, queued per lane,
  done by day), a task drawer with the attempt timeline, and spend, routing,
  memory and habits screens. Live through server-sent events from the event
  trail, with a polling fallback.
- Queue, cancel, reopen a lane and start `cauce work` from the page. Loopback
  only, `Host` allowlist, token header on every mutation, strict CSP, 1 MiB
  body cap; housekeeping in a server thread, never in a GET.
- `cauce board --json` for status lines.
- `/cauce:ui` opens the page from a session. `cauce board --full [--repo DIR]…`,
  `cauce show <id> --json` and `cauce queue add … --json` give another program
  the board scoped to some repositories, a task, and what it queued.
- The board carries each task's way through the matrix (`flow`: kind, ladder,
  start, every attempt with its cell, outcome and move) and, for a running task,
  the worker out now (`worker`: the `claude -p` of this attempt, its cell, turn
  and dollar limits, MCP servers, start time, and whether its process lives).
  `counts.workers` says how many are out.
- `cauce projects --json`: every repository cauce has worked in, from the
  directory it was last used from. `cauce sessions --json [--repo DIR]`: the
  Claude Code sessions it saw, with their prompts and the command that resumes
  each. `cauce memory list --json [--query Q] [--repo DIR]`: problems and every
  fix tried, everything or some repositories'. Board items carry the task's
  text (`body`, up to 800 characters) and its `session_id`.

### Fixed

- Two processes opening an old database at once no longer fail on a column
  the other one just added.
- Opening the database is serialized across processes (a lock file beside it)
  and a "database is locked" during setup is retried: switching to WAL and
  altering a table could be refused at once instead of waiting, when a hook,
  the dispatcher and the UI opened the file in the same instant.
- `cauce` not found (exit 127) in a session of an older Claude Code, or one
  opened before the plugin was installed: `SessionStart` puts the plugin's
  `bin/` on the session's PATH through `CLAUDE_ENV_FILE`, or names the full
  path when it cannot, and the commands fall back to the full path too. The
  launcher follows symlinks and says so when no Python is found.
- A `python3` older than 3.11 hands over to a `python3.11`–`3.14` beside it,
  with no extra process when `python3` is new enough; with none, or with an old
  `CAUCE_PYTHON`, one line says so instead of an import traceback. The
  hand-over marker is never inherited by a worker's hooks.
- `cauce link`: `cauce` in a terminal. It writes a shim to `~/.local/bin` that
  runs the newest installed cauce, so a plugin update does not break it, and
  never replaces a `cauce` it did not write.

### Added — livespec is built in

- **Adapters**: neighbours the core adopts rather than lists. The livespec
  adapter reads the code index read-only with `sqlite3` and takes part in
  routing (critical specs and widely called symbols raise the start; a review
  of critical code is reviewed as critical), briefing (a code map at the top of
  every worker's prompt), the work (livespec's server with a per-kind hint and
  the `workspace`) and the report (specs touched, callers outside the change).
- An unindexed or stale repository is indexed through livespec's own CLI before
  a run, and in the background at session start.
- livespec needs no separate install: an installed plugin, a `livespec` on
  `PATH`, or the pinned `livespec@0.32.0` through `uvx`.
- **The `livespec` switch**, on by default: plugin option, `cauce config
  livespec off`, `CAUCE_LIVESPEC`, `--no-livespec`. `cauce neighbours` shows
  what cauce sees.
