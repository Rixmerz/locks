# A/B of two locks checkouts on trap tasks

`traps.py` runs the same trap tasks through two locks checkouts that differ only by the code
`locks run` imports (`PYTHONPATH=<checkout>/src`): the base (main) and the head (a branch).
Each run gets a fresh repository and its own `LOCKS_HOME`. The design comes from hermes-agent's
`evals/toolperf_abeval`: two arms, one variable, and each task a trap for one class of waste.

| Trap | Waste it provokes |
|---|---|
| `already-applied` | a fix asked for that the code already has |
| `ambiguous-edit` | an edit whose text matches in several places |
| `giant-output` | a check whose output is too large to read whole |
| `hidden-config` | the value to change lives in a hidden folder |
| `wrong-python` | a check that names `python3`, where a worker that runs `python` fails |

Results are judged by records, never by what the worker says: the task's status, attempts, turns
and dollars from the run's store, the failed tool calls from its tool-call log, and a hidden check
run after the task ends (`right`).

```bash
git worktree add ../locks-main origin/main
python bench/ab/traps.py run --base ../locks-main --head . --reps 2
python bench/ab/traps.py report
```

This spends real money. Run it on purpose, before a release that changes how workers are briefed or
checked. It has not been run here yet, so it measures nothing so far.
