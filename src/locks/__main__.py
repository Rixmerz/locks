from __future__ import annotations

import os
import sys

from locks.interpreter import OLD_ENOUGH, newer_python

if sys.version_info < OLD_ENOUGH:
    # Checked before the rest of locks is imported: on an older python3 the
    # first import fails with a traceback that does not say what is wrong.
    # A python3.1x beside it runs locks instead.
    newer = newer_python(os.environ)
    if newer:
        os.environ["LOCKS_REEXEC"] = "1"
        os.execv(newer, [newer, "-m", "locks", *sys.argv[1:]])  # noqa: S606 — the same argv, a newer interpreter
    sys.stderr.write(f"locks needs Python 3.11+, and {sys.executable} is {sys.version.split()[0]}; "
                     "install a newer one or set LOCKS_PYTHON to it.\n")
    sys.exit(1)
# Never inherited: a worker's hooks start on the old python3 again and must be
# free to look for a newer one themselves.
os.environ.pop("LOCKS_REEXEC", None)
os.environ.pop("CAUCE_REEXEC", None)  # legacy-name: an older parent's

# A command typed one plugin update later runs the newest install, not the
# version this session's PATH started with. Once: the newer copy never forwards,
# and keeps the version it was forwarded from (`link.LOADED`), never in its env.
os.environ.pop("CAUCE_FORWARDED", None)  # legacy-name: an older install forwards only to its own name
if _forwarded_from := os.environ.pop("LOCKS_FORWARDED", None):
    from locks import link

    link.LOADED = _forwarded_from
elif sys.argv[1:2] not in (["hook"], ["hook-trace"]):
    from locks import __version__, link

    newer_install = link.forward_to(sys.argv[1:], os.environ, __file__, __version__)
    if newer_install is not None:
        os.environ["LOCKS_FORWARDED"] = __version__
        os.execv(str(newer_install), [str(newer_install), *sys.argv[1:]])  # noqa: S606 — the same argv, a newer locks

if sys.argv[1:3] == ["hook", "PostToolUse"]:
    # The fastest path: every tool call lands here. One JSON line appended to
    # a file, no database; a delegation still takes the full hook below.
    import json

    from locks import signature

    try:
        event = json.loads(sys.stdin.read() or "{}")
        signature.append(event, os.environ)
        if isinstance(event, dict):
            from locks import trace

            if trace.enabled(os.environ):
                trace.hook("PostToolUse", event, os.environ)
    except Exception:  # a hook never takes the session down, and says when it failed
        signature.note_error(os.environ)
        event = None
    if not (isinstance(event, dict) and event.get("tool_name") in signature.DELEGATION_TOOLS):
        sys.exit(0)
    if os.environ.get("LOCKS_HOOKS_OFF") == "1":
        sys.exit(0)
    import io

    from locks.hooks import main as hook_main

    sys.exit(hook_main("PostToolUse", io.StringIO(json.dumps(event)), sys.stdout, os.environ))

if sys.argv[1:2] == ["hook-trace"]:
    # The opt-in trace of an event no other hook of locks runs on (every PreToolUse, a
    # subagent's stop, a notification): one line when `trace` is on, nothing otherwise.
    from locks import trace

    try:
        if len(sys.argv) >= 3 and os.environ.get("LOCKS_HOOKS_OFF") != "1" and trace.enabled(os.environ):
            import json

            event = json.loads(sys.stdin.read() or "{}")
            if isinstance(event, dict):
                trace.hook(sys.argv[2], event, os.environ)
    except Exception:  # a hook never takes the session down
        from locks import signature

        signature.note_error(os.environ)
    sys.exit(0)

if len(sys.argv) >= 3 and sys.argv[1] == "hook":
    # The fast path: a hook runs on every tool call, and the full CLI imports
    # the orchestrator, the launcher and argparse for nothing.
    from locks.hooks import main as hook_main

    sys.exit(hook_main(sys.argv[2], sys.stdin, sys.stdout, os.environ))

from locks.cli import main

sys.exit(main())
