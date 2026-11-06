# Which model fits which work

locks picks the model and effort of every task it runs: a ladder per kind, its start moved
only by what first tries measured here, its climbs by what each attempt ended with. Do not pass
`--start` to second-guess it; pass it only when the person asks for a cell.

`locks cells` shows, per kind, where locks starts it in this repository and why, and each
cell's first tries here (`--all` for every repository, `--json` for the rows). Read it before
choosing a model yourself, which happens in two places:

- **Your own subagents** (the Agent tool's `model`): a search or a read that settles a fact is
  `explore` (haiku); a review of a change is `review-routine` (sonnet) or `review-critical`
  (opus); a plan is `plan` (sonnet/high). Take the cell `locks cells` says is proven here, else
  the start it shows: never cheaper than the ladder's first rung.
- **Whether to send work at all**: a kind whose first tries here rarely pass on its start costs
  climbs; a kind that passes cheap is worth sending even when small.

What the table says was measured: a first try is a task's first attempt, or the first after a
new model. A cell with under five tries is shown as a count, never as a rate.
