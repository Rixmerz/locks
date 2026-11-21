---
description: Hand this session's thread to a fresh one — the next session here (after /clear) starts from your requests verbatim, what landed and what is open, instead of a compaction's summary
disable-model-invocation: true
---

Run this with Bash, from this session:

```bash
locks relay
```

When `locks` alone is not found (exit 127), call it by its full path:
`${CLAUDE_PLUGIN_ROOT}/bin/locks relay`.

Then tell the person, in one or two lines, what it printed: the relay is ready,
and typing `/clear` (or opening a new session in this folder) starts the fresh
session with this one's thread — their requests in their own words, the commits
made, the files written, and the locks tasks still open, which will report to
the new session. This session stays as it is.

Do nothing else. Do not summarize the conversation yourself: the relay is read
from the transcript and git, so it never paraphrases. If it says there is no
thread to hand over yet, say so.
