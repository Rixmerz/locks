# Sending work: `locks run`, its flags, permissions, Node, worktrees, workspaces, the queue

**Dispatch** with Bash, from the repository the work is in:

```bash
locks run - --verify "<the repo's own check, if one exists: tests, lint, build>" <<'EOF'
<the task, written so a stranger could do it>
EOF
```

**Send the task on stdin through a quoted heredoc** (`-` as the text, `<<'EOF'`),
never inside double quotes. In a double-quoted argument the shell runs every
backtick and `$(…)` before locks sees the request: a task that quoted
`` `locks merge N` `` ran it, and started with those words missing. A quoted
heredoc passes the text as it is (`locks run - < request.md` reads a file the
same way); an empty stdin is refused (exit 2). `locks queue add -` takes it
the same way. Where a task id is wanted, a word that is not `N` or `#N` is
refused in one line (`locks merge`, `locks pr`), never a traceback.

`locks` is on the Bash tool's PATH while the plugin is enabled (it ships in
the plugin's `bin/`). Its full path is `${CLAUDE_PLUGIN_ROOT}/bin/locks`:
when `locks` alone is not found (exit 127 — an older Claude Code, or a
session opened before the plugin was installed), call it by that path. Never
do the work by hand because the command did not resolve.

Run it in the background when it may take a while, with the Bash tool's own
background run, or `--detach`: never with a trailing `&`, whose child dies with
the call's shell. It prints its task
number and each attempt on stderr as they happen, and its report when it
stops. A `warning:` line (no browser, servers that must be running) means
the task as written will not get far: say so to the person before waiting
on it. `locks run … --detach` queues it instead and starts its lane's
dispatcher: it prints the task number at once, and the ending reaches this
session as every queued task's does.

Write the task in full. The worker gets the person's own words from this
session that relate to it (locks' scribe puts them in its brief), but none
of your reasoning, what you read or what you decided: say in the task what
it needs of those. Add `--verify` whenever the repository has a command that proves the work — a
worker's own claim of success is not evidence, and locks runs the check
itself, so it needs no permission. The report says what the task cost;
nothing limits it for money: its room is its turns.
Pass `--kind` with the kind you read the request as, one of `explore`,
`review-routine`, `review-critical`, `plan`, `docs`, `test`, `ui`, `refactor`,
`implement`, `feature`, `debug-repro`, `debug-unclear` (`locks run --help`
lists them): you have read it already, and locks' classifier would read it
again in a model call of its own, a few seconds before the worker starts. Left
out, locks classifies it. A task that must change nothing says
"read-only" in its text: that is enough, whatever kind it gets.

Never pipe a run through `tail`, `head` or `grep`: the pipe hides its exit
code and holds back the lines that say it started. After launching one in
the background, read its output for `task #N started` before telling the
person it is running; a usage error there means nothing ran.

Workers run in Claude Code's auto mode (Haiku workers bypass permissions):
a classifier lets safe commands through, so most work needs no rules. What
it still refuses is named in the report with the rule that lets it through.
Keep rules for every task in this repository with `locks allow <rule>…` or a
preset (`locks allow --preset node`), instead of passing `--allow` to each
run; `locks run --dry-run` lists the rules workers here were refused before.
An absolute path in a rule takes two slashes: `Read(//abs/path)`. The
project's Node (`.nvmrc`, `.node-version`, `engines.node`) is put first on
the workers' PATH for them; when the project declares none and a
dependency needs a newer Node than the PATH has, an installed one that fits
is. A `warning: this task needs Node …` line means none installed fits:
tell the person before waiting on the task.

A worker works in its own worktree on `locks/task-<id>`: your checkout has
none of it until the branch is merged, and the worker does not see your
uncommitted changes. When the task builds on uncommitted work, or you will
go on in this checkout right after, add `--no-isolate`; a resume keeps it.

Work that spans two or more repositories (an API and its front end) is one
project when the folder that holds them is a workspace: one memory, one
board, and a task that spans both runs in the folder. When a report says
`this task spans N repositories` and they are tracked apart, offer the
person the command it gives (`locks workspace add <folder> --with …`); run
it only on their yes, since it moves what each repository kept to the
folder. `locks workspace list` shows the ones they have.

**livespec is a step of the work, not a tool the worker may ignore.** Before a worker
starts, locks indexes or refreshes the repository's livespec index (incrementally) and waits
for it up to `livespec_wait` seconds: 120 by default, `locks config livespec_wait 30` changes
it, 0 never waits. In a folder that holds several repositories it does each repository, never
the folder. Past the wait the task starts without the index: its plan says `still indexing …
this task runs without it` (`uses the index as it is` where a stale one exists), and the
index keeps building for the next task. With an index, the
worker's brief opens with how to find its way: the spec for the request (`list_specs`,
`search`), the code it names (`get_spec_implementation`), one symbol at a time (`quick_orient`,
`read_unit`), a whole file last; `search_similar` before it writes a helper and
`git_diff_impact` before it finishes. A task that writes or changes an OpenSpec spec must put
`@spec:<capability>-<requirement-name>` on the functions that implement it: the report and the
independent check name the specs it touched that no code names (a block for a spec the change
wrote, which also raises its risk to high so that check runs; a warning for one that was already
there). `locks stats` compares the tokens a run
spent, by kind, with livespec called against not called; `locks doctor` names repositories
whose index is missing or stale, and specs no code implements.

A worker that runs out of turns is checked with `--verify` by locks itself,
so a large change still gets its tests run; give one whenever a command can
tell whether the work is done.

For work that can wait, queue it instead — `locks queue add - <<'EOF'` … `EOF`. A
worker picks it up at once, in its own process that outlives this session;
its output says so. Work that needs another task's result first is queued
with `--after <task>`: it starts when that task passes, on the branch it
kept; never sleep or poll to start it yourself. When that task stops short,
its ending says what waits on it; dismissing it takes what waits off the
queue too, and each comes back to you as an ending (`locks resume N` starts
one on the base). Only when it says `locks work` runs the queue (the
person turned `autowork` off) start `locks work` in the background. Whether
a queued task runs beside the others or waits its turn is decided there,
not here.

## Where the check runs

A worker works in a worktree: a fresh copy with
only what git carries. A check that needs what the checkout holds and git
does not (a running Docker Compose or Sail stack named after the folder, a
gitignored `.env`, a local database) cannot run there: add `--no-isolate`,
and chain such tasks `--after` so one works in the checkout at a time. Not
sure? `locks run --dry-run` says what keeps the check from starting. Describe
the environment as the worker will find it, not as you see it ("the server
is up" is true in your checkout, not in a worktree). A surface covers every
file the request implies: deployment or proxy work holds its compose and CI
files too. To steer a resume, add to it: `locks resume N --unattended - <<'EOF'`.
