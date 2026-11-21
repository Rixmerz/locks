---
description: Open locks' board — what needs you, what is running and what it costs, the queue per repository — in a local web page
disable-model-invocation: true
---

Start the board in the background and tell the user where it is:

```bash
locks ui
```

It opens no browser tab: the user opens the address when they want it, and the
page shows every task of the session (its **Sessions** screen, one session's work
with each task opened inside the page), so no piece of work needs a tab of its
own. Add `--open` only when the user asks to see the board now, and only once;
never open a tab for each task you send. `--tabs` is the user's explicit
opt-in (`locks ui --open --tabs`): one tab for this session, at most, which
shows all its work inside and which a later run reuses instead of opening
another. Do not pass it on your own.

Run it with `run_in_background`, so the session stays usable; it serves until
the session ends or the user stops it, and it follows plugin updates by itself.
It listens on `127.0.0.1:8790` only. If it says this same version already
serves that port, the board is there: give the user that address. If it says
an older locks UI holds the port, tell the user: that server shows the old
version's board until they stop it. Meanwhile, run
`locks ui --port <another>`. If another program holds the port, use
another port too.
When `locks` alone is not found (exit 127), it is
`${CLAUDE_PLUGIN_ROOT}/bin/locks`; call it by that path.

Then say, in one line, the address it printed. Do not read the board yourself:
the page is for the user, and `locks board --json` gives the counts if one is
needed here.
