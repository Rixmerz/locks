# Evals for `/locks:insight`

`evals.json` follows anthropics/skills' `skill-creator` format: each prompt, what a good answer
does, and the expectations a grader checks. Run them after any change to the skill's text,
before it ships, and grade every expectation against the answer.

Run each against a locks home with real tasks in it (the one `scripts/smoke.py --keep` leaves is
enough), with read-only tools and nothing that starts work:

```bash
LOCKS_HOME=<a home with tasks> LOCKS_AUTOWORK=off \
  claude -p --plugin-dir <this checkout> --allowedTools "Bash(locks:*)" Read Grep Glob \
  "/locks:insight <the eval's prompt>"
```

`LOCKS_AUTOWORK=off` matters: without it the session's own hooks start the dispatcher on that
home's queued tasks, which spends real money.

Last run (0.83.1 text, eval 1, $0.94, 28 turns): verdicts, counts and hypothesis labels held;
one causal claim was wrong (it took locks' check to have a test runner the task's `--verify` did
not), which the "A task's check is the command its `--verify` names" mechanism now addresses.
