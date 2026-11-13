# Which bundled workflow for which work

Each bundled workflow is a chain of fresh, checked workers (`locks workflow run <name> "<goal>"`).
The cost column is one real run of each on a small repository, from `bench/speed/matrix.py`
(locks 0.104, November 2026): an order of magnitude, never a budget. Your repository's own
runs are in `locks stats`.

| Workflow | Use it for | Steps | One run measured |
|---|---|---|---|
| `bugfix` | a bug you can reproduce | reproduce with a failing test and fix, blind review, refute, fix, re-check | $0.21 |
| `feature` | one feature, one area | build, blind review, refute, fix, re-check | $0.33 |
| `feature-parallel` | a large feature over several areas (`feature` turns into it by itself) | plan in parts, parts side by side, join, then the review chain | $1.33 and $1.62 (two runs, 0.106, by the workers' own `/usage`) |
| `review-fix` | a change already made that needs a review and its fixes | review, refute, fix, re-check | $0.47 |
| `dual-review` | the same, where one reviewer is not enough | two blind reviews, refute, fix, re-check | $1.11, 146 s |
| `panel` | a change that touches correctness, security, speed and tests at once | four blind reviews (one lens each), refute, fix, re-check | $0.79 |
| `design-review` | a design document before any code | four blind readings (one lens each), refute; no fix | $0.76 |
| `drift` | whether a folder of documents still says what the code does | one read-only comparison | $0.01 |
| `tracer-slice` | one vertical slice in a repository that keeps the TRACER method (`docs/tracer/`) | build, critical verify against the flow's CORRECT lines, refute, fix, re-check | $1.13, 221 s |

A review chain costs nothing more when its review is clean: refute and fix run only on rows the
review found. Since 0.106 the fix of `feature`, `bugfix` and `tracer-slice` may go on in the
session of the worker that built the change (its `trunk`), on trial against a fresh worker.

A single task with no review chain is `locks run "<request>"`: cheaper, checked by locks' own
check and, where the risk calls for it, the independent verifier.
