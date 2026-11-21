# Loops: goals that run in rounds until evidence stops them

A loop (`locks loop`) is a goal with no end of its own: find and follow up
leads through the night, keep CI green, work a backlog. When a request is one,
ask the person to type `/locks:loop` (only they can start it): never run its rounds by hand from this session.

**How it runs.** Each round is a workflow run, its workers fresh, checked and
costed. A detached runner (`locks loop start`) starts a round, waits for its
end, measures the metric (the last number its command prints, run by locks,
never a worker's word) and starts the next one when it is due (`--every`). In a
checkout a round starts on what the last round kept when it passed. Each step
of a round is told the loop's goal, its mode, the person's rules and the diary
of the last rounds, quoted as data.

**When it stops:** its target reached, its window over (`--until`), `--stale`
rounds in a row that did not improve on the best before them, or a round that
waits on what only the person clears. Never for money: each round's cost is
kept and shown (`locks loop show`).

**Strategies.** Given `--variant`s, rounds alternate between them until each
has `--trial` measured rounds; the one that moved the metric most is adopted.

**What acts.** A loop starts as a draft: tools that send, post or reply (a
capability's `outbound.acts`) are refused by locks' guard, and the workers
write what they would send. `locks loop act` is the person's word only; then
the guard still refuses a call outside `--hours`, past `--per-hour`, or naming
an `--exclude`. What the rules do not allow is listed under `Needs the person:`
and shown when the loop stops.

**You hear of it once,** when it stops, with its summary. Tell the person what it
did, what it cost and what waits on them.
