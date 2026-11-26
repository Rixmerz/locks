# Extensions

locks is closed enough to work on its own and open enough to take any tool into its flow. Its core
classifies a request, chooses a model and effort, runs a worker with exactly the tools its task
needs, checks the result, escalates on evidence and remembers what failed. Everything else (a code
index, a method, a design-system inspector, a memory server, a code graph) is an **extension**:
declared by the person, never coded into locks, never chosen by a repository.

## Why declared, and why per task

Every plugin a session loads lists its skills, agents and tools to the model on every turn: ten
tools cost every turn of every session, used or not. An extension is handed only to the worker
whose task it applies to (`when`), so a CSS change gets the layout inspector and a backend fix
does not. The person's own sessions are left as they are.

## The contract (format 1)

One JSON file per extension in the person's locks home, `extensions/<name>.json`. `locks extensions
add FILE` checks it and copies it there; `list`, `show NAME` and `rm NAME` read and remove.

```json
{"format": 1,
 "name": "graph",
 "description": "a code graph of the repository",
 "when": {"kinds": ["implement", "feature"], "touches": ["src/**"], "exists": "graph.json"},
 "tools": {"server": {"command": "graph-mcp", "args": ["{workdir}"]},
           "measures": true, "hint": "ask the graph who calls a symbol before you change it"},
 "brief": {"files": ["docs/architecture.md"], "chars": 4000},
 "criteria": {"files": ["docs/flows/*.md"], "pattern": "^\\s*-\\s*MUST:\\s*(.+)$"}}
```

| Point | Key | What locks does with it |
|---|---|---|
| when it applies | `when.kinds`, `when.touches` (patterns or `"ui"`), `when.exists` | every key given must hold; none given is always |
| tools | `tools.server`, `measures`, `hint` | an MCP server handed as a capability to the worker; to the independent check too only when it `measures` |
| brief | `brief.files`, `chars` | the repository's files, given to a writing worker as data, never instructions, bounded, the rest said by count |
| criteria | `criteria.files`, `pattern` | lines read by the pattern (its first group, or the line) become requirements of the independent check: one the change breaks is a finding |
| check | `check.command` | runs after the task's own check, and must pass for a writing task to pass: its failure is read as any check's, against the base |
| ending | `ending.offer` | the line a passing task's ending offers; nothing is started |

Each extension is tried per repository: a task that it applies to takes it or goes without it, in
turns, until each arm has six first tries; then it is kept only where its tasks passed no less
often (`briefed.ext_<name>_arm`, pooled from every repository while a repository's own arms are
short). The independent check's requirements stay whatever the arm, as livespec's index does.

An extension may also name `source` (an installed Claude Code plugin's server, its skill, the program on
PATH, extra arguments, the tools its hint names, whether it needs a browser, and a guard policy of
`locks.mcp_guard` by name) in place of `tools.server`, and `evidence` (what a passing writing attempt must
show of its tools, `ui_only` for a UI change): the gate `locks.evidence` holds a pass to.

What an extension cannot do: choose a cell, grant a permission, write outside a task's surfaces,
change a guard, or block a pass by its own word. A file of the wrong shape is refused whole, with
its reason, and costs only itself. Paths are inside the repository, never absolute or upward.

## Plan

| Phase | What | State |
|---|---|---|
| 1 | the contract above: `locks.extensions`, `locks extensions`, tools as capabilities, brief and criteria | done (0.116.0) |
| 1b | `check` and `ending` | done (0.116.0) |
| 1c | a trial per extension: with and without it, kept where its tasks pass no less often | done (0.116.0) |
| 2 | workers start with no MCP server of the session (`--strict-mcp-config`), no skill listing and no subagent tool (`lean`), and from 0.116.0 with every plugin the person turned on switched off but locks (`lean_plugins`, a `--settings` overlay): what applies comes from locks alone. For the person's own sessions `/locks:integrate` proposes what to hide, never writes their settings | done (0.116.0) |
| 3 | `/locks:integrate` and `locks extensions inspect`: reads an installed Claude Code plugin (skills, agents, MCP servers, hooks) without running it, drafts an extension per server, proposes roles from its agents (`locks roles import`), installs on the person's yes, in trial | done (0.116.0) |
| 4a | layout-inspector and rastro as bundled extensions (`src/locks/extensions_bundled/`): `plugins.KNOWN` and `evidence.REQUIRED` are read from them; any extension may take its server from an installed plugin (`source`, its guard a `locks.mcp_guard` policy, which only narrows) and owe evidence (`evidence`) | done (0.116.0) |
| 4b | TRACER's layout (its folder, files, flows, capability map, slices and the word that opens a criterion) is an extension's `method` (`extensions_bundled/tracer.json`); a person's `tracer` extension with its own `method` makes locks read an updated TRACER as it is | done (0.116.0) |
| 4c | livespec in the flow: each spec requirement the changed code implements is a criterion `L#` the independent check answers (`REQUIREMENT_PREFIX` lines of `assess`), done (0.116.0). Still to come, in livespec itself: a verification record per requirement and symbol (commit, evidence, body hash), `livespec check --stale`, and a tool locks records a verification through; then locks runs the stale check before a pass and records what its check verified | locks' side done; livespec's waits on push access to its repository |
| 4d | OpenSpec and engram as extensions | |
| 4e | the web board as an optional extension | |
| 5 | a test that the core names no tool; the core alone against the core with extensions on the speed bench; 1.0.0 | |

A bundled extension a person already uses is installed for them once, so nothing they rely on
stops when its code leaves the core.
