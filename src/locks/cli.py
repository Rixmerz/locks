"""`locks` — the command line the orchestrating session calls, and the hooks' entry point."""
from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from locks import (
    __version__,
    capabilities,
    capacity,
    config,
    dispatch,
    lazy,
    legacy,
    orchestrator,
    progress,
    project,
    repo,
    stops,
    usage,
)
from locks import findings as findings_mod
from locks.classify import classify
from locks.escalate import Attempt, Failure
from locks.matrix import EFFORTS, KINDS, LADDERS, MODEL_ORDER, Cell
from locks.store import FIX_OUTCOMES, Store, home
from locks.text import clip, title_of

# The run loop and what only some commands need, loaded when a command first reads them.
flow = lazy.module("locks.flow")
orchestrate = lazy.module("locks.orchestrate")
hooks = lazy.module("locks.hooks")
habits = lazy.module("locks.habits")
after = lazy.module("locks.after")


def _service(value: str) -> str:
    """A `--service`, checked where it is read: a command and the port it listens on."""
    from locks import services

    try:
        services.parse(value)
    except services.ServiceError as err:
        raise argparse.ArgumentTypeError(str(err)) from None
    return value


def _cell(value: str) -> str:
    """A `--start` cell, checked where it is read: a cell locks has no such dial
    for (`haiku/low`) is a usage error, never a traceback mid-run."""
    try:
        Cell.parse(value)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"{value!r}: {err}") from None
    return value


#: Words a caller reaches for that are not locks' kinds, read as the kind they mean.
KIND_ALIASES = {
    "research": "explore", "investigate": "explore", "investigation": "explore", "search": "explore",
    "audit": "review-critical", "review": "review-routine", "security": "review-critical",
    "fix": "debug-repro", "bug": "debug-repro", "bugfix": "debug-repro", "debug": "debug-unclear",
    "tests": "test", "testing": "test", "e2e": "test", "validate": "test", "validation": "test",
    "doc": "docs", "documentation": "docs", "frontend": "ui", "design": "ui",
    "impl": "implement", "code": "implement", "build": "implement", "planning": "plan", "question": "chat",
}


def _kind(value: str) -> str:
    """A `--kind`: one of locks', or a word that means one. Anything else is a
    usage error that names the kinds, before any task is recorded."""
    key = value.strip().lower()
    if key in KINDS:
        return key
    if key in KIND_ALIASES:
        print(f"locks: --kind {value!r} read as {KIND_ALIASES[key]!r}", file=sys.stderr)
        return KIND_ALIASES[key]
    raise argparse.ArgumentTypeError(f"{value!r} is not a kind; use one of: {', '.join(KINDS)} "
                                     "(or leave --kind out and locks classifies the task)")


#: A run's exit: 0 passed, 1 stopped short of it, 2 a usage error, 3 refused because only a
#: person can clear what stopped it, 4 the work is done and kept but one point needs a decision.
EXIT_DECISION = 4
#: `locks run` and `queue add` exit with it when a request is several tasks: nothing was started.
EXIT_SPLIT = 5


def _exit(report: orchestrate.Report) -> int:
    if report.status == "done":
        return 0
    return EXIT_DECISION if report.stop is not None and report.stop.cause == "decision" else 1


def _text(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


def _request_from_stdin(args: argparse.Namespace) -> bool:
    """Reads the request of `locks run -` / `locks queue add -` once, into `args.text`; False,
    said, when stdin held nothing or the request is empty or only tags. A quoted heredoc is the
    way to send text with backticks or `$(…)`
    in it: inside a double-quoted argument the shell runs them before locks sees the request."""
    if args.text == "-":
        args.text = sys.stdin.read()
        if not args.text.strip():
            print("locks: the request read from stdin is empty; send it as `locks run - <<'EOF'` … `EOF` "
                  "(a quoted heredoc keeps backticks and $(…) as text)", file=sys.stderr)
            return False
    if not _TAGS_ONLY.sub(" ", args.text).strip():
        # Nothing to do is no shallow attempt: more effort on an empty request only pays again for nothing.
        print("locks: the request holds no task (it is empty, or only tags); nothing was started. Send "
              "the task's full text — ask the person for it if it was cut off", file=sys.stderr)
        return False
    return True


#: Tags (`#review`, `#fable`) and whitespace: a request of only these asks for nothing.
_TAGS_ONLY = re.compile(r"#[A-Za-z][A-Za-z-]*\b|\s+")


def _task_numbers(command: str, words: list[str]) -> list[int] | None:
    """The task numbers (`N` or `#N`) a command was given; None, said in one line, when a word is
    none: a placeholder (`N`) or text the shell cut out of a request is no task."""
    numbers = [str(word).strip().removeprefix("#") for word in words]
    bad = [str(word) for word, number in zip(words, numbers, strict=True) if not re.fullmatch(r"\d+", number, re.ASCII)]
    if bad:
        print(f"locks {command}: {', '.join(repr(w) for w in bad)} is not a task number (N or #N)", file=sys.stderr)
        return None
    return [int(number) for number in numbers]


def _repo_key(args: argparse.Namespace) -> str | None:
    if getattr(args, "all", False):
        return None
    return repo.key(Path(args.repo or os.getcwd()))


def _behind(env: dict[str, str] | None = None) -> str | None:
    """What to tell a session whose locks is older than the newest installed one,
    or None. A session keeps the plugin it started with: its runs go on without
    every fix an update brought until it reloads."""
    from locks import link

    # The launcher on the session's PATH is the version it started with, even after a reload;
    # what its hooks last ran as is what it has loaded now.
    environ = os.environ if env is None else env
    session = environ.get("LOCKS_SESSION_ID")
    ran = link.session_loaded(home(env), session) if session else None
    said = ran or link.LOADED
    loaded = link.parse_version(said or "")
    mine = link.parse_version(__version__)
    if loaded and mine and loaded < mine:
        # Forwarded here: the command runs this version, the session's skills and hooks do not.
        return (f"locks: this session loaded locks {said}; the commands it types run {__version__}, but its "
                f"skills, hooks and /locks:* text are still {said}'s. Run /reload-plugins --force (a plain one can "
                "hold the load back), or `/locks:relay` then /clear, so its instructions are this version's too")
    newest = max((version for version, _ in link.installed(environ)), default=None)
    if not mine or not newest or newest <= mine:
        return None
    return (f"locks: this session runs locks {__version__}, but {'.'.join(map(str, newest))} is installed: "
            "it started before the update. Run /reload-plugins --force (or restart the session) so its runs get it")


def _warn_behind() -> None:
    if warning := _behind():
        _say(warning)


def _surfaces(values: list[str] | None) -> tuple[str, ...]:
    from locks import surfaces

    try:
        return surfaces.check(values or ())
    except surfaces.SurfaceError as exc:
        raise SystemExit(f"locks: {exc}") from exc


def _split_first(args: argparse.Namespace, text: str) -> int | None:
    """`EXIT_SPLIT`, said, when a build request is several tasks and `--whole` was not passed:
    one worker outgrows its turns on all of it, and the climb after costs more than the parts."""
    if getattr(args, "whole", False) or getattr(args, "dry_run", False):
        return None
    from locks import route

    why = route.split_first(text, args.kind)
    if why is None:
        return None
    if config.enabled("autosplit", os.environ) and not getattr(args, "after", None):
        queued = _split_and_queue(args, text, why)
        if queued is not None:
            return queued
    print(f"locks: not started: this request is several tasks ({why}). One worker on all of it outgrows its "
          "turns, then climbs to more turns and stronger models, which costs more than the parts would. Send "
          "one `locks run` per piece, each with its own `--surface` (pieces whose surfaces cannot touch run side "
          "by side; one that builds on another goes `--after` it), or `locks workflow run feature --goal "
          "'<the request>'`, whose plan splits it. `--whole` runs it as one task anyway.", file=sys.stderr)
    return EXIT_SPLIT


def _split_and_queue(args: argparse.Namespace, text: str, why: str) -> int | None:
    """`autosplit`: the pieces of a request that is several tasks, queued as `locks split --queue`
    queues them. None when it proposed no pieces: the caller says how to split it by hand."""
    print(f"locks: this request is several tasks ({why}): splitting it and queueing its pieces "
          "(`--whole` runs it as one; `locks config autosplit off` stops doing this)", file=sys.stderr)
    split_args = argparse.Namespace(target=text, repo=getattr(args, "repo", None), verify=getattr(args, "verify", None),
                                    no_model=False, json=False, queue=True)
    code = cmd_split(split_args)
    return code if code == 0 else None


def _notify(args: argparse.Namespace) -> None:
    """`--notify SESSION`: the task this command creates records who else hears its ending."""
    if getattr(args, "notify", None):
        from locks.store import NOTIFY_ENV

        os.environ[NOTIFY_ENV] = " ".join(args.notify)


def cmd_run(args: argparse.Namespace) -> int:
    _warn_behind()
    _notify(args)
    if not _request_from_stdin(args):  # read once: the split check and the run read the same text
        return 2
    if (split := _split_first(args, args.text)) is not None:
        return split
    if args.role:
        from locks import roles

        try:
            roles.load().get(args.role)
        except roles.RoleError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    options = orchestrate.Options(
        max_attempts=args.max_attempts,
        allow_approval=args.allow_approval,
        isolate=not args.no_isolate,
        verify=args.verify,
        launch_dir=Path(args.launch_dir) if args.launch_dir else None,
        max_turns=args.max_turns,
        use_model_classifier=not args.no_model,
        kind=args.kind,
        start=Cell.parse(args.start) if args.start else None,
        dry_run=args.dry_run,
        livespec=args.livespec,
        allow_tools=tuple(args.allow or ()),
        role=args.role,
        surfaces=_surfaces(args.surface),
        carry=args.carry,
        add_dirs=tuple(str(Path(d).expanduser().resolve()) for d in args.add_dir or ()),
        services=tuple(args.service or ()),
        whole=bool(getattr(args, "whole", False)),
        # What follows the ending (notes, the late independent check) goes on in a child: the command
        # returns at its ending.
        detach_after=True,
    )
    if (args.after is not None or getattr(args, "detach", False)) and not args.dry_run:
        return _queue_after(args, options)
    store = Store.open()
    previous = _cancel_on_sigterm()
    try:
        report = orchestrate.run(_text(args.text), Path(args.repo or os.getcwd()), store, options,
                                 session_id=os.environ.get("LOCKS_SESSION_ID"), progress=_say)
        if getattr(report, "task_id", None):
            store.mark_reported([report.task_id], via="cli")  # its caller reads the report below
            orchestrator.sent(store, Path(args.repo or os.getcwd()).resolve(), dict(os.environ))
    finally:
        signal.signal(signal.SIGTERM, previous)
        store.close()
    print(report.text())
    return _exit(report) if report.status != "dry run" else 0


def _queue_after(args: argparse.Namespace, options: orchestrate.Options) -> int:
    """`locks run … --after N` or `--detach`: the task is queued (held until #N passes,
    `locks.after`, with `--after`) and its lane's dispatcher started, as `locks queue
    add` does; its ending reaches the session that sent it, as every queued task's does."""
    flag = "--after" if args.after is not None else "--detach"
    if args.launch_dir:
        print(f"{flag} queues the task, and a queued task starts in its own directory: drop --launch-dir",
              file=sys.stderr)
        return 2
    where = Path(args.repo or os.getcwd()).resolve()
    store = Store.open()
    try:
        held = None
        if args.after is not None:
            try:
                held = after.target_of(store, args.after)
            except after.AfterError as exc:
                print(f"locks: not queued: {exc}", file=sys.stderr)
                return 2
        stored = {**options.stored(), **({after.KEY: held} if held else {}), **{k: v for k, v in {
            "start": args.start, "max_turns": args.max_turns, "carry": args.carry or None,
            "max_attempts": args.max_attempts if args.max_attempts != orchestrate.Options().max_attempts else None,
        }.items() if v is not None}}
        task = _enqueue_after(store, _text(args.text), where, stored)
        then = dispatch.kick(where, repo.key(where), os.environ)
        said = _held_line(store, task["id"])
        if held and (note := after.caveat(store, held, task["repo"])):
            said += f" ({note})"
        eta = progress.expected_line(store, task)
    finally:
        store.close()
    print(f"queued #{task['id']} — {task['title']}: {said}. {then}")
    if eta:
        print(eta)
    print(f"watch it: `locks show {task['id']}`, or `locks events --task {task['id']} --follow`; "
          "its ending reaches the session that sent it")
    return 0


def _held_line(store: Store, task_id: int) -> str:
    """What work just queued after other work does: waits for it, or starts on what it kept."""
    return dispatch.waiting(store, store.get_task(task_id)) or after.released_line(store, task_id) or "queued"


def _enqueue_after(store: Store, text: str, where: Path, options: dict) -> dict:
    """Queue held work, released at once when what it waits for passed already."""
    task = store.enqueue(text, repo=repo.key(where), cwd=str(where), options=options,
                         author="orchestrator" if os.environ.get("LOCKS_SESSION_ID") else "person",
                         session_id=os.environ.get("LOCKS_SESSION_ID"))
    after.settle(store, task)
    with contextlib.suppress(OSError, project.NotAProject):
        project.enroll(where)
    orchestrator.sent(store, where, dict(os.environ))
    return task


#: Endings a task can be resumed from: it stopped, and was not judged wrong.
RESUMABLE = ("blocked", "needs_approval", "failed", "cancelled", "interrupted", "dismissed")


def _resume_pressed(args: argparse.Namespace) -> int:
    """A person's press on a page that is not locks' own UI: the same resume the
    UI's button runs, read from locks' account of the stop (`stops.resume_args`).
    The caller names the task and nothing else: no rule or approval."""
    if args.allow or args.allow_approval or args.unattended or args.keep or any(
            v is not None for v in (args.verify, args.start, args.max_turns)):
        print("--pressed takes the resume from locks' account of the stop: give it the task id alone",
              file=sys.stderr)
        return 2
    store = Store.open()
    try:
        task = store.get_task(args.id)
        pressed = stops.resume_args(task["id"], stops.view(store, task)) if task else None
    finally:
        store.close()
    if pressed is None:
        print(f"task #{args.id} has no resume to press", file=sys.stderr)
        return 1
    if task["cwd"] and Path(task["cwd"]).is_dir():
        os.chdir(task["cwd"])
    return main(pressed)


def _not_resumable(task: dict) -> str | None:
    """Why a workflow step that runs no worker of its own is never resumed, and what goes on with
    it instead: a question is answered, a sub-workflow's step goes on by its run (or a retry)."""
    own = json.loads(task.get("options") or "{}")
    run, step = task.get("workflow_run"), task.get("workflow_step")
    if own.get("question"):
        options = "|".join(own["question"].get("options") or ())
        return f"is a question of workflow run #{run}: answer it with `locks workflow answer {run} {step} <{options}>`"
    if own.get("subflow"):
        return (f"runs workflow run #{own['subflow'].get('run')} as step {step!r} of run #{run}: that run goes on by "
                f"its own steps, or `locks workflow retry {run} {step}` starts it again")
    return None


def cmd_resume(args: argparse.Namespace) -> int:
    """A task that stopped, run again under its own id: on the branch its work was
    kept on, from the cell it stopped at, its earlier attempts in the brief."""
    if args.pressed:
        return _resume_pressed(args)
    _warn_behind()
    store = Store.open()
    previous = _cancel_on_sigterm()
    try:
        task = store.get_task(args.id)
        # A queued task cancelled before it started (taken off the queue, or behind work that
        # left the board) resumes too: it runs here, from the base, as it would have.
        if task is None or task["source"] not in ("locks", "queue") or not task["cwd"]:
            print(f"no task #{args.id} that locks ran", file=sys.stderr)
            return 1
        if (refused := _not_resumable(task)) is not None:
            print(f"locks: #{args.id} {refused}", file=sys.stderr)
            return 1
        if task["status"] == "running" and not flow.alive(task["pid"]):
            # A restart (or a crash) left it running under a process that is gone: the sweep
            # records what it would have, and the resume goes on from there.
            flow.sweep(store)
            task = store.get_task(args.id)
        # A dismissed task resumes as what it was before: a dismissal hides a
        # task, it never makes one that was found wrong resumable.
        was, hidden = task["status"], None
        if was == "dismissed" and (stop := stops.of(store, task)) is not None:
            was = str(stop.extra.get("was") or was)
            hidden = stop.extra.get("had_cause")
            task = {**task, "status": was}
        # What stopped it: a dismissal keeps the cause it took off the board.
        cause = hidden or ((stop := stops.of(store, task)) and stop.cause)
        # One split for its turns resumes as one that outgrew them: the session's word that it is one piece.
        outgrew = was == "replan" and cause in ("turns", "split")
        # One that stopped on its surfaces resumes with more of them, its diagnosis in the brief.
        widened = bool(args.surface) and cause in ("spec", "decision")
        if was not in RESUMABLE and not outgrew and not widened:
            print(f"task #{args.id} is {was}; only a task that stopped ({', '.join(RESUMABLE)}) resumes"
                  + ("; rewrite it as a new task" if was == "replan" else _instead(args.id, was)), file=sys.stderr)
            return 1
        if args.keep:
            # Kept for every task in this repository, so the next one is not
            # refused the same program: a person's press, never an unattended one.
            if args.unattended:
                print("--keep keeps rules for every task in the repository: only a person passes it, "
                      "never an unattended resume", file=sys.stderr)
                return 2
            from locks import grants

            rules = grants.expand(args.allow or ())
            if not rules or not task["repo"]:
                why = "name them with --allow" if not rules else f"task #{args.id} has no repository to key them to"
                print(f"--keep keeps the --allow rules for the task's repository: {why}", file=sys.stderr)
                return 2
            kept = grants.grant(task["repo"], rules)
            print(f"workers in {task['repo']} get: {', '.join(kept)}", file=sys.stderr)
        if args.unattended and (refusal := _needs_a_person(store, task, args)):
            print(refusal, file=sys.stderr)
            return 3
        if args.instruction not in (None, "-"):
            print(f"locks resume takes `-` (an instruction on stdin), not {args.instruction!r}", file=sys.stderr)
            return 2
        if args.instruction == "-":
            # What the session adds for this resume (`locks resume N -`), kept on the task so a
            # detached resume reads it too; its brief quotes it as context (issue #123).
            text = sys.stdin.read().strip()
            if text:
                store.add_event(args.id, orchestrate.RESUME_INSTRUCTION, text=text)
            args.instruction = None
        if args.detach:
            return _detach(args)
        if _under_a_session_shell():
            return _shielded(args)
        options = flow.queued_options(task, orchestrate.Options(use_model_classifier=not args.no_model))
        options.detach_after = True
        options.kind = options.kind or task["kind"]
        options.allow_tools = tuple(dict.fromkeys([*options.allow_tools, *(args.allow or ())]))
        options.verify = args.verify or options.verify
        if args.surface:
            options.surfaces = tuple(dict.fromkeys([*options.surfaces, *args.surface]))
        # An approval is a person's, for the run they approved: a resume nobody
        # approved (`--unattended`, a model's) never uses one an earlier run had,
        # and leaves it stored for the person's own next resume.
        inherited = options.allow_approval
        options.allow_approval = args.allow_approval or (inherited and not args.unattended)
        options.isolate = options.isolate and not args.no_isolate
        if args.max_turns or outgrew:
            # A task that outgrew its turns is not wrong: it goes on with more.
            options.max_turns = args.max_turns or stops.RESUME_TURNS
        attempts = store.attempts(args.id)
        moved = store.last_event(args.id, "moved")
        waiting = moved["data"].get("next_cell") if moved and task["status"] == "needs_approval" else None
        if waiting and not options.allow_approval:
            print(f"task #{args.id} waits for {waiting}, which needs your approval: resume it with --allow-approval",
                  file=sys.stderr)
            return 1
        start = args.start or waiting or (attempts[-1]["cell"] if attempts else None)
        options.start = Cell.parse(start) if start else None
        options.resume_why = resume_line(was, [a["cell"] for a in attempts], start,
                                         "asked" if args.start else "approval" if waiting else "last") + (
            f"; its surfaces now take {', '.join(args.surface)} too" if args.surface else "")
        denied, granted = store.denials(args.id), store.allowances(args.id)
        tails = store.check_tails(args.id)
        history = [Attempt(Cell.parse(a["cell"]), int(a["max_turns"] or 0), bool(a["passed"]),
                           _failure_of(a["failure"]), a["summary"] or "", tuple(denied.get(a["seq"], ())),
                           allow=tuple(granted.get(a["seq"], ())),
                           changed=tuple(json.loads(a["changed_paths"] or "[]")),
                           evidence=(a["evidence"] or "")[-1500:], check_tail=tails.get(a["seq"], ""))
                   for a in attempts]
        # A task the independent check stopped resumes with what it found: its first
        # check says for each whether it is fixed, instead of starting blind.
        checked = (store.last_event(args.id, "verified") or {}).get("data") or {} if cause == "review" else {}
        if "blockers" in checked:
            known = list(findings_mod.read({"findings": checked["blockers"]}))
        else:  # a check an older locks recorded kept only what it showed
            known = findings_mod.blocking(findings_mod.read({"findings": checked.get("findings") or []}))
        # What the task is beyond its run options stays on it: an investigator's mark, and the
        # part of a workflow step it runs (without it, its step reads as this task alone and
        # the integrate after it drops every other part's branch).
        prior = json.loads(task["options"] or "{}")
        kept = (options.stored() | {k: prior[k] for k in ("investigation", "part") if k in prior}
                | ({"allow_approval": True} if inherited else {}))
        store.update_task(args.id, options=json.dumps(kept), dispatched=0, cancel_requested=0)
        # It runs again: a lane paused on its stopping no longer waits on anyone, and
        # the queue behind it starts now, beside it where it may, not at its end.
        if store.release_lane(task["repo"], args.id):
            _go_on(store, task["repo"])
        try:
            report = orchestrate.run(task["body"], Path(task["cwd"]), store, options, task_id=args.id,
                                     history=history, progress=_say, known_findings=known)
        except Exception as exc:  # locks failed under the task, not the task: say so and keep the traceback
            return _could_not_run(args.id, exc)
        finally:
            # Work queued behind it has no dispatcher after a restart: its end starts one.
            _go_on(store, task["repo"])
        if not args.no_report:
            # Its report is printed to whoever ran it; a detached resume's is not,
            # and reaches the session as an ending instead.
            store.mark_reported([args.id], via="cli")
    finally:
        signal.signal(signal.SIGTERM, previous)
        store.close()
    print(report.text())
    return _exit(report)


def _rank(label: str) -> tuple[int, int]:
    """Where a cell stands in the matrix: its model's depth, then its effort."""
    cell = Cell.parse(label)
    model = MODEL_ORDER.index(cell.model) if cell.model in MODEL_ORDER else -1
    return model, EFFORTS.index(cell.effort) if cell.effort in EFFORTS else -1


def resume_line(was: str, cells: Sequence[str], start: str | None, why: str) -> str:
    """What a resume's plan says of where it starts: after what, at which cell and why (the
    cell its last attempt ran, the one that waited for an approval, or the one `--start`
    asked for), and when that is below the highest cell the task already ran."""
    if not cells or not start:
        return f"resumed after it {was}, with no attempt before: it starts where its plan does"
    because = {"asked": "as `--start` asked", "approval": "the cell that waited for an approval"}.get(
        why, "the cell its last attempt ran")
    line = f"resumed after it {was} ({len(cells)} attempt(s) before): it starts at {start}, {because}"
    top = max(cells, key=_rank)
    if _rank(start) < _rank(top):
        line += f"; that is below {top}, the highest cell it ran"
    return line


def _go_on(store: Store, key: str | None) -> bool:
    """Start the repository's dispatcher when work waits in its lane and `autowork` is on:
    a machine that restarted has queued tasks and no `locks work` to run them."""
    return config.enabled("autowork", os.environ) and dispatch.start_lane(store, home(), key)


def _could_not_run(task_id: int, exc: Exception) -> int:
    """locks failed under a task: one line with the cause and the command that continues
    it, the traceback in the log the hooks report from, and a non-zero exit. Called from
    the `except` that caught `exc`."""
    hooks.note_error(home(), f"resume #{task_id}")
    cause = " ".join(f"{type(exc).__name__}: {exc}".split())[:300]
    print(f"locks: task #{task_id} could not run: {cause}; `locks resume {task_id}` continues it "
          f"(traceback in {home() / hooks.ERROR_LOG})", file=sys.stderr)
    return 1


#: The attempts after which a resume nobody approved is refused: two runs' worth.
UNATTENDED_ATTEMPTS = stops.UNATTENDED_ATTEMPTS


def _needs_a_person(store: Store, task: dict, args: argparse.Namespace) -> str | None:
    """Why a resume nobody approved would only stop again, or None. A model may
    continue work; it may not grant what a person must: a refused command, or a
    cell that waits for approval. A rule the person kept since with `locks allow`
    counts as granted."""
    from locks import allow as allow_rules
    from locks import grants

    stop = stops.of(store, task)
    attempts = store.attempts(task["id"])
    # A try the network dropped under, or the load timed out, is no attempt at the work
    # (its `moved` event says so).
    tried = len(attempts) - stops.lost_tries(store, task["id"])
    if tried >= UNATTENDED_ATTEMPTS:
        # No dollar cap stops work any more: a model resuming a task that keeps
        # failing is stopped by the attempts it already had.
        return (f"task #{task['id']} already had {tried} attempts: only the person resumes it now "
                f"(`locks resume {task['id']}`), after reading what they found (`locks show {task['id']}`)")
    if task["status"] == "needs_approval" and not args.allow_approval:
        return (f"task #{task['id']} waits for a cell that needs the person's approval: they resume it "
                f"(`locks resume {task['id']} --allow-approval`)")
    start = args.start or (attempts[-1]["cell"] if attempts else None)
    tagged = LADDERS.get(task["kind"] or "", ())[:1]
    if (start and not args.allow_approval and Cell.parse(start).needs_approval
            and not (tagged and tagged[0].needs_approval)):
        # The approval an earlier run had was for that run: a resume nobody approved
        # does not start on the cell it opened. A `#fable` task's tag is its approval.
        return (f"task #{task['id']} would start again on {start}, which needs the person's approval: they "
                f"resume it (`locks resume {task['id']} --allow-approval`)")
    if stop is not None and stop.cause == "permission":
        needed = list(stop.allow or allow_rules.from_refusals(stop.denied))
        have = {*grants.granted(task["repo"]), *(args.allow or ())}
        missing = [rule for rule in needed if rule not in have]
        if missing:
            return (f"task #{task['id']} was refused {', '.join(stop.denied[:3]) or 'a command'}: only the person "
                    f"can allow {', '.join(missing)} (`{stops.next_step(task['id'], stop)}`, or keep it for this "
                    f"repository with `locks allow {' '.join(shlex.quote(r) for r in missing)}`)")
    return None


def _resume_argv(args: argparse.Namespace, *, report: bool) -> list[str]:
    """The same resume as a command of its own: what `--detach` and a shielded resume start."""
    argv = [sys.executable, "-m", "locks", "resume", str(args.id)] + ([] if report else ["--no-report"])
    for flag, value in (("--verify", args.verify), ("--start", args.start), ("--max-turns", args.max_turns)):
        if value is not None:
            argv += [flag, str(value)]
    for rule in args.allow or ():
        argv += ["--allow", rule]
    for path in args.surface or ():
        argv += ["--surface", path]
    argv += [flag for flag, on in (("--allow-approval", args.allow_approval), ("--no-isolate", args.no_isolate),
                                   ("--no-model", args.no_model), ("--unattended", args.unattended)) if on]
    return argv


#: Set on a resume a session's shell started in a process group of its own (`_shielded`).
SHIELDED = "LOCKS_SHIELDED"


def _under_a_session_shell() -> bool:
    """Whether this resume runs in a Claude Code session's shell, not a person's terminal: that
    shell's timeout sends SIGTERM to its process group, which a resume read as `locks cancel`
    and stopped the task it had just started."""
    return (os.environ.get("CLAUDECODE") == "1" and not os.environ.get(SHIELDED)
            and not os.environ.get("LOCKS_WORKER_TASK") and not sys.stdout.isatty())


def _shielded(args: argparse.Namespace) -> int:
    """Run the resume in a process group of its own and follow its output until it ends: a
    shell that times out or dies takes this follower with it, never the run, whose ending then
    reaches the session that sent the task as a detached resume's does. Only `locks cancel`
    stops it."""
    from locks import dispatch

    log = home() / "work" / f"resume-{args.id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)  # this follower dies; the run goes on
    start = log.stat().st_size if log.exists() else 0
    with open(log, "a") as out:
        child = subprocess.Popen(_resume_argv(args, report=True), cwd=os.getcwd(),
                                 env={**dispatch.child_env(), SHIELDED: "1"}, stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    _say(f"locks: resume #{args.id} runs in its own process group (pid {child.pid}); a shell that ends "
         f"leaves it running, and its log is {log}")
    with open(log, errors="replace") as follow:
        follow.seek(start)
        while True:
            done = child.poll() is not None
            if chunk := follow.read():
                sys.stdout.write(chunk)
                sys.stdout.flush()
            if done:
                return child.returncode if child.returncode >= 0 else 1
            time.sleep(0.5)


def _detach(args: argparse.Namespace) -> int:
    """Run the same resume in a process of its own and return at once: its ending
    reaches the session that sent the task, as every queued task's does."""
    from locks import dispatch

    argv = _resume_argv(args, report=False)
    log = home() / "work" / f"resume-{args.id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as out:
        subprocess.Popen(argv, cwd=os.getcwd(), env=dispatch.child_env(), stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    print(f"resuming #{args.id} in the background; its ending reaches the session that sent it")
    return 0


def _say(line: str) -> None:
    """Progress, to stderr and at once: a run's report waits for its end."""
    print(line, file=sys.stderr, flush=True)


def _failure_of(value: str | None) -> Failure | None:
    try:
        return Failure(value) if value else None
    except ValueError:
        return None


def _cancel_on_sigterm():
    """`locks cancel` sends SIGTERM. Turned into an exception, it unwinds the run:
    `subprocess.run` kills the worker, and the run's cleanup removes its worktree."""
    def handler(signum, frame):
        raise orchestrate.Cancelled

    return signal.signal(signal.SIGTERM, handler)


def _cancel_stop(via: str) -> stops.Stop:
    return stops.Stop("cancelled", f"you cancelled it {stops.CANCEL_VIA[via]} before it ran", by="you",
                      extra={"via": via})


def cmd_cancel(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        task = store.get_task(args.id)
        if task is None:
            print(f"no task #{args.id}", file=sys.stderr)
            return 1
        if task["status"] not in ("running", "queued"):
            print(f"task #{args.id} is {task['status']}; nothing to cancel")
            return 0
        store.request_cancel(args.id, via="cli")
        if task["pid"]:
            # Gone already: the flag still stops it at its next attempt.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(int(task["pid"]), signal.SIGTERM)
        elif task["status"] == "queued":
            stops.record(store, args.id, _cancel_stop("cli"))
    finally:
        store.close()
    print(f"cancel requested for task #{args.id}")
    return 0


def _dismiss_words(words: list[str]) -> tuple[list[int], str]:
    """`locks dismiss 12 13 not needed any more`: the leading numbers (`#12` too)
    are the tasks, the words after them the reason."""
    ids: list[int] = []
    for i, word in enumerate(words):
        number = word.strip().lstrip("#").rstrip(",")
        if not re.fullmatch(r"\d+", number, re.ASCII):
            return ids, " ".join(words[i:]).strip()
        ids.append(int(number))
    return ids, ""


def cmd_guide(args: argparse.Namespace) -> int:
    """The orchestration skill's detail, a topic at a time: the skill itself stays short."""
    folder = Path(__file__).parent / "guides"
    topics = {p.stem: p for p in sorted(folder.glob("*.md"))}
    if not args.topic:
        for name, path in topics.items():
            print(f"{name:<14} {path.read_text(encoding='utf-8').splitlines()[0].lstrip('# ')}")
        return 0
    if args.topic not in topics:
        print(f"no guide {args.topic!r}; one of {', '.join(topics)}", file=sys.stderr)
        return 2
    print(topics[args.topic].read_text(encoding="utf-8").rstrip())
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    """The branches tasks kept, merged into the branch checked out, on the person's word:
    in an order that puts the ones that touch no other first, the check after each."""
    from locks import isolate, merging

    ids = _task_numbers("merge", args.ids)
    if ids is None:
        return 2
    store = Store.open()
    try:
        tasks = {n: store.get_task(n) for n in ids}
        missing = [f"#{n}" for n, t in tasks.items() if t is None]
        if missing:
            print(f"no task {', '.join(missing)}", file=sys.stderr)
            return 1
        where = Path(args.repo or tasks[ids[0]]["cwd"] or os.getcwd())
        top = repo.toplevel(where) if where.is_dir() else None
        if top is None:
            print(f"{where} is no git checkout", file=sys.stderr)
            return 1
        finals = {n: ((store.final_event(n) or {}).get("data") or {}) for n in ids}
        changed = {n: list(finals[n].get("changed") or []) for n in ids}
        verify = args.verify or next((json.loads(tasks[n].get("options") or "{}").get("verify") for n in ids
                                      if json.loads(tasks[n].get("options") or "{}").get("verify")), None)
        branches = {n: isolate.branch_for(n) for n in ids}
        if args.dry_run:
            print("would merge, in this order: " + ", ".join(f"#{n} ({branches[n]})" for n in merging.order(changed))
                  + (f"; `{verify}` after each" if verify else "; no check after each (pass --verify)")
                  + ("; onto a new branch, pushed as one draft pull request" if args.pr else ""))
            return 0
        onto = ("locks/merge-" + "-".join(str(n) for n in ids))[:80] if args.pr else None
        try:
            done = merging.merge(top, branches, {n: tasks[n]["title"] for n in ids}, changed, verify=verify,
                                 say=print, onto=onto)
        except merging.MergeError as exc:
            print(f"locks: nothing merged: {exc}", file=sys.stderr)
            return 1
        if not onto:
            stops.landed(store, top)
    finally:
        store.close()
    if onto and not (done.conflict or done.failed):
        # Every branch went in and the check passes after each: pushed and offered as one draft.
        merging.back(top, done)
        opened, said = merging.open_pr(top, onto, f"Merge {len(done.merged)} locks branches"[:72],
                                       merging.merged_body(tasks, finals, done), base=done.base)
        print(said if opened else f"locks: merged onto {onto}, but the pull request did not open: {said}",
              file=sys.stdout if opened else sys.stderr)
        return 0 if opened else 1
    if onto:
        print(f"the merges stopped on {onto}, which stays checked out; `git checkout {done.base}` goes back",
              file=sys.stderr)
    if done.conflict:
        task_id, files = done.conflict
        print(f"#{task_id} ({branches[task_id]}) conflicts with what is in {done.into} in {', '.join(files[:8])}: its "
              "merge was aborted, nothing of it is in. Reconcile it by hand, or send it as a task:\n  "
              + merging.resolve_task(done.into, branches[task_id], files, verify), file=sys.stderr)
    if done.failed:
        task_id, out = done.failed
        print(f"`{verify}` fails after merging #{task_id}; the merge is in place (`git reset --merge ORIG_HEAD` "
              f"undoes it):\n{out}", file=sys.stderr)
    left = [f"#{n}" for n in done.order if n not in done.merged]
    if left:
        print("not merged: " + ", ".join(left))
    return 0 if not (done.conflict or done.failed) else 1


def cmd_ci(args: argparse.Namespace) -> int:
    """The repository's CI jobs as a plan reads them, and the ones quieted here: a job known not
    to apply to its checks (one that needs a license the fork lacks) is named in no plan."""
    from locks import ci

    where = Path(args.repo or os.getcwd()).resolve()
    key = repo.key(where)
    store = Store.open()
    try:
        if args.ci_command in ("quiet", "speak"):
            now = ci.set_quiet(store, key, args.jobs, on=args.ci_command == "quiet")
            print(f"quiet here: {', '.join(sorted(now)) or 'none'}")
            return 0
        quieted = ci.quiet(store, key)
    finally:
        store.close()
    found, unreadable = ci.jobs(where)
    for job in found:
        print(f"{job.file}/{job.name}" + ("  (quiet)" if job.name in quieted or f"{job.file}/{job.name}" in quieted
                                           else ""))
    for name in unreadable:
        print(f"{name}: could not be read")
    if not found and not unreadable:
        print("no CI jobs found in .github/workflows")
    return 0


def cmd_specs(args: argparse.Namespace) -> int:
    """How far a repository's OpenSpec specs are behind its code, and whether it is spec-first:
    a build that writes no spec there goes back to its worker once, then to the orchestrator."""
    from locks import specs

    where = Path(args.repo or os.getcwd()).resolve()
    key = repo.key(where)
    store = Store.open()
    try:
        if args.specs_command == "first" and args.state is not None:
            specs.set_first(store, key, args.state == "on")
        first = specs.first(store, key)
    finally:
        store.close()
    found = specs.drift(where)
    if found is None:
        if first:
            print(f"spec-first is on for this repository, but nothing is enforced: there is no `{specs.OPENSPEC}/` "
                  f"here. A build is held back for its spec only once `{specs.OPENSPEC}/` exists.")
        else:
            print(f"no `{specs.OPENSPEC}/` here: nothing to keep up")
        return 0
    print(f"specs: {found.text()}" + (" — behind" if found.behind else ""))
    print("spec-first: " + ("on (a build that writes no spec is not accepted)" if first
                            else "off (`locks specs first on` makes builds here owe their spec)"))
    for line in specs.structure(where):
        print(line)
    return 0


def cmd_pr(args: argparse.Namespace) -> int:
    """A draft pull request for the branch a task kept, its verdict as the description; on
    the person's word, since it pushes. `--dry-run` prints the description and pushes nothing."""
    import shutil

    from locks import isolate, merging

    numbers = _task_numbers("pr", [args.id])
    if numbers is None:
        return 2
    task_id = numbers[0]
    store = Store.open()
    try:
        task = store.get_task(task_id)
        final = ((store.final_event(task_id) or {}).get("data") or {}) if task else {}
    finally:
        store.close()
    if task is None:
        print(f"no task #{task_id}", file=sys.stderr)
        return 1
    branch = isolate.branch_for(task_id)
    where = Path(task["cwd"] or os.getcwd())
    top = repo.toplevel(where) if where.is_dir() else None
    if top is None or task_id not in isolate.kept_branches(top):
        print(f"#{task_id} keeps no branch here: nothing to open a pull request for", file=sys.stderr)
        return 1
    title = (isolate.last_commit(top, branch) or f"x {task['title']}").split(" ", 1)[1][:72]
    body = merging.pr_body(task, final)
    if args.dry_run or shutil.which("gh") is None:
        print(f"title: {title}\n\n{body}")
        if not args.dry_run:
            print(f"gh is not installed: push with `git push -u origin {branch}` and open the pull request with "
                  "this title and description", file=sys.stderr)
        return 0 if args.dry_run else 1
    opened, said = merging.open_pr(top, branch, title, body, base=args.base)
    print(said, file=sys.stdout if opened else sys.stderr)
    return 0 if opened else 1


#: What a piece keeps of the options of the task it splits: never its hold, start, turns or surfaces.
_SPLIT_KEEPS = ("verify", "kind", "allow_tools", "add_dirs", "services", "livespec", "role", "carry")


def cmd_split(args: argparse.Namespace) -> int:
    """The pieces a request, or a task that stopped as several pieces, splits into
    (`locks.split`); `--queue` starts them on what the task kept."""
    from locks import split

    target = args.target.lstrip("#")
    is_number = re.fullmatch(r"\d+", target, re.ASCII) is not None  # `²` is a digit to isdigit, and int() refuses it
    store = Store.open()
    try:
        task = store.get_task(int(target)) if is_number else None
        if is_number and task is None:
            print(f"no task #{target}", file=sys.stderr)
            return 1
        if task is not None:
            text, where = str(task["body"] or ""), Path(task["cwd"] or os.getcwd())
            final = (store.final_event(task["id"]) or {}).get("data") or {}
            done = list(final.get("changed") or [])
            base = final.get("commit") or final.get("tip")
            inherited = {k: v for k, v in json.loads(task.get("options") or "{}").items() if k in _SPLIT_KEEPS}
        else:
            text, where = _text(args.target), Path(args.repo or os.getcwd()).resolve()
            done, base, inherited = [], None, {}
        if args.verify:
            inherited["verify"] = args.verify
        proposal = split.by_items(text, done) if args.no_model else split.propose(text, where, done=done)
        if args.json and not args.queue:
            print(split.as_json(proposal))
            return 0 if proposal.parts else 1
        for line in split.lines(proposal):
            print(line)
        if not proposal.parts:
            return 1
        if not args.queue:
            again = f"`locks split {task['id']} --queue`" if task is not None else "the same command with `--queue`"
            print(f"nothing queued: {again} starts them" + (" on what it kept" if base else ""))
            return 0
        ids: dict[str, int] = {}
        for part in proposal.parts:
            options = {**inherited, **({"surfaces": list(part["surfaces"])} if part.get("surfaces") else {}),
                       **({"base": base} if base else {})}
            if part.get("after"):
                options[after.KEY] = {"task": ids[part["after"]]}
            queued = store.enqueue(split.brief(part, proposal.contract), repo=repo.key(where), cwd=str(where),
                                   options=options, session_id=os.environ.get("LOCKS_SESSION_ID"),
                                   author="orchestrator" if os.environ.get("LOCKS_SESSION_ID") else "person")
            ids[part["id"]] = queued["id"]
            store.add_event(queued["id"], "split_from", task=task["id"] if task else None, part=part["id"])
        numbers = ", ".join(f"#{n}" for n in ids.values())
        print(f"queued {numbers}" + (f" on what #{task['id']} kept ({str(base)[:12]})" if task and base else ""))
        if task is not None:
            last = list(ids.values())[-1]
            moved = after.reassign(store, task["id"], last, f"#{task['id']} was split into {numbers}")
            if moved:
                print(f"{', '.join(f'#{n}' for n in moved)} waited for #{task['id']}: now for #{last}, its last piece")
            via = "session" if os.environ.get("LOCKS_SESSION_ID") else "cli"
            if stops.dismiss(store, store.get_task(task["id"]), via=via, why=f"split into {numbers}") is not None:
                print(f"#{task['id']} leaves the board: split into {numbers}")
        print(dispatch.kick(where, repo.key(where), os.environ))
    finally:
        store.close()
    return 0


def _instead(task_id: int, status: str) -> str:
    """What a task in this state takes instead of a dismissal or a resume, as the refusal ends."""
    return {
        "running": f". It is running: its ending reaches you when it ends; `locks cancel {task_id}` stops it now, its "
                   "work kept",
        "queued": f". It waits in the queue: `locks cancel {task_id}` takes it off, `locks lanes --beside "
                  f"{task_id}` starts it beside the work ahead",
        "cancelled": f". It was cancelled: nothing waits on it; `locks resume {task_id}` starts it again",
        "done": ". It passed: nothing to take off the board or to resume",
        "dismissed": f". It is off the board already; `locks resume {task_id}` brings it back",
    }.get(status, "")


def cmd_dismiss(args: argparse.Namespace) -> int:
    """A task that waits on a person, taken off the board on their word; typed in a
    session (its Bash), it is the orchestrating session's call and says so."""
    via = "session" if args.via == "cli" and os.environ.get("LOCKS_SESSION_ID") else args.via
    ids, said = _dismiss_words(args.ids)
    if not ids:
        print("locks dismiss: name the tasks first: " + stops.dismiss_command("N"), file=sys.stderr)
        return 2
    why = " ".join(part for part in (args.why or "", said) if part.strip())
    store = Store.open()
    try:
        for task_id in ids:
            task = store.get_task(task_id)
            if task is None:
                print(f"no task #{task_id}", file=sys.stderr)
                return 1
            waited = dispatch.waiting(store, task) if after.held(task) else None
            if (stop := stops.dismiss(store, task, via=via, why=why)) is None:
                print(f"task #{task_id} is {task['status']}; only a task that waits "
                      f"({', '.join(stops.DISMISSABLE)}) or on other work (`--after`) is dismissed"
                      + _instead(task_id, task["status"]), file=sys.stderr)
                return 1
            print(f"took task #{task_id} off the queue (it {waited})" if waited
                  else f"dismissed task #{task_id}; `locks resume {task_id}` brings it back")
            if kept := _keep_left(store, task):
                print(f"kept what its worktree held on {kept}, unverified")
            if along := stop.extra.get("took_along"):
                print(f"and took {', '.join(f'#{n}' for n in along)} off the queue: queued after #{task_id}, "
                      "they would never start; each is reported as an ending, and `locks resume N` starts one "
                      "on the base")
            dispatch.reopened(task, os.environ)
    finally:
        store.close()
    return 0


def _keep_left(store: Store, task: dict) -> str | None:
    """A dismissed task's worktree that a run left behind (one that died) holds work no
    branch has: it is committed on the task's branch, unverified, as a blocked stop's is,
    so a dismissal never loses it and `locks resume N` goes on from it. Never fails the
    dismissal."""
    from locks import isolate, orchestrate

    where = Path(task["cwd"]) if task.get("cwd") else None
    if where is None or not where.is_dir():
        return None
    try:
        branch = isolate.keep_left(where, int(task["id"]), home(), message=isolate.message(
            int(task["id"]), title=orchestrate.commit_title(task), status="dismissed"))
    except (isolate.IsolationError, OSError):
        return None
    if branch:
        store.add_event(task["id"], "branch_kept", branch=branch, why="dismissed with its worktree left")
    return branch


def cmd_events(args: argparse.Namespace) -> int:
    store = Store.open()
    if args.last_id:
        print(store.last_event_id())
        store.close()
        return 0
    after = store.last_event_id() if args.new else args.after
    repos = {repo.key(Path(d).resolve()) for d in args.repo} if args.repo else None
    tasks: dict[int, dict | None] = {}

    def task_of(task_id: int) -> dict | None:
        if task_id not in tasks:
            tasks[task_id] = store.get_task(task_id)
        return tasks[task_id]

    try:
        while True:
            batch = store.events(after=after, task_id=args.task)
            for e in batch:
                after = e["id"]
                if args.kind and e["kind"] not in args.kind:
                    continue
                task = task_of(e["task_id"])
                if repos is not None and (task is None or task["repo"] not in repos):
                    continue
                if args.json:
                    # A watcher outside locks names the task without asking again.
                    e = {**e, "repo": task["repo"] if task else None, "title": task["title"] if task else None,
                         "session_id": task["session_id"] if task else None}
                print(json.dumps(e, ensure_ascii=False) if args.json else
                      f"{e['ts']} #{e['task_id']} {e['kind']} {json.dumps(e['data'], ensure_ascii=False)}")
            if batch:
                continue  # a page at a time, to the last event
            if not args.follow:
                return 0
            sys.stdout.flush()
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


def cmd_route(args: argparse.Namespace) -> int:
    args.dry_run, args.after = True, None
    return cmd_run(args)


def cmd_classify(args: argparse.Namespace) -> int:
    result = classify(_text(args.text), use_model=not args.no_model, cwd=home())
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_matrix(_: argparse.Namespace) -> int:
    for kind, ladder in LADDERS.items():
        print(f"{kind:16} {' → '.join(c.label for c in ladder)}")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        # locks' own work by default: a session's turns are no task to resume or count.
        sources = None if args.turns and not args.workers else ("locks", "queue")
        here = _repo_key(args)
        _notice_deaths(store)
        tasks = store.list_tasks(repo=here, status=args.status or None, limit=args.limit, source=sources)
        if args.workers:
            tried = {t["id"]: store.attempts(t["id"]) for t in tasks}
            tasks = sorted((t for t in tasks if tried[t["id"]]), key=lambda t: (str(t.get("repo") or ""), -t["id"]))
        seen: dict[tuple[str, str], list[float]] = {}
        now = {t["id"]: progress.of(store, t) for t in tasks if t["status"] == "running"}
        waits = {t["id"]: (progress.line(now[t["id"]]) if t["id"] in now else "")
                 or dispatch.waiting(store, t) or flow.slow(store, t, seen=seen) for t in tasks}
        files = {i: progress.files_line(p) for i, p in now.items()}
        # None here, but work elsewhere: say how much and where to see it.
        elsewhere = [] if tasks or here is None else store.list_tasks(
            status=args.status or None, limit=10_000, source=sources)
    finally:
        store.close()
    project = None
    for t in tasks:
        cell = t["final_cell"] or t["start_cell"] or ""
        mark = {"hook": " (turn)", "delegation": " (subagent)"}.get(t["source"], "")
        size = str(t.get("points") or "-")
        where = f"{_project_label(t.get('repo')):<18} " if here is None and not args.workers else ""
        if args.workers:
            if here is None and t.get("repo") != project:
                project = t.get("repo")
                print(f"{_project_label(project)} ({project or 'no repository'})")
            mark = f"  ({len(tried[t['id']])} attempt(s), ${float(t.get('cost_usd') or 0):.2f})"
        print(f"#{t['id']:<5} {where}{t['status']:<14} {t['kind'] or '-':<16} {cell:<14} {size:>2} "
              f"{t['title']}{mark}")
        if waits[t["id"]]:
            print(f"       {waits[t['id']]}")
        if files.get(t["id"]):
            print(f"       {files[t['id']]}")
    if elsewhere:
        projects = len({t.get("repo") for t in elsewhere})
        print(f"no tasks in this project ({here}); {len(elsewhere)} in {projects} other project(s): "
              "`locks tasks --all` lists them")
    elif not tasks:
        print("no tasks" + (f" in this project ({here})" if here else ""))
    return 0


def _project_label(key: str | None) -> str:
    """A project's key as a row names it: its folder's name."""
    return Path(key).name if key else "-"


def cmd_peek(args: argparse.Namespace) -> int:
    """A running worker's diff so far, read and never written; a task that ended has its kept
    branch instead, which `locks show` names."""
    store = Store.open()
    try:
        task = store.get_task(args.id)
        if task is None:
            print(f"no task #{args.id}", file=sys.stderr)
            return 1
        if task["status"] != "running":
            branch = ((store.final_event(args.id) or {}).get("data") or {}).get("branch")
            print(f"#{args.id} is {task['status']}, not running" + (
                f": its work is on {branch} (git -C {task['cwd']} diff HEAD...{branch})" if branch else ""),
                file=sys.stderr)
            return 1
        started = store.last_event(args.id, "attempt_started")
        base = ((started or {}).get("data") or {}).get("base")
        places = ((started or {}).get("data") or {}).get("places")
        links = tuple(((started or {}).get("data") or {}).get("links") or ())
        if args.stat:
            written = progress.changes(args.id, base, places=places, links=links)
            if written is None:
                print(f"#{args.id} works in no worktree of its own, or git cannot say", file=sys.stderr)
                return 1
            print(progress.line({"changes": written}) or "nothing written yet")
            if names := progress.files_line({"changes": written}):
                print(names)
            return 0
        found = progress.peek(args.id, base, places=places)
    finally:
        store.close()
    if found is None:
        print(f"#{args.id} works in no worktree of its own, or git cannot say", file=sys.stderr)
        return 1
    print(found["diff"] or "nothing written yet", end="" if found["diff"] else "\n")
    if found["cut"]:
        whole = "; ".join(f"git -C {p['path']} diff {p['head']}" for p in found["places"]) if "places" in found \
            else f"git -C {found['worktree']} diff {base or 'HEAD'}"
        print(f"… cut at {progress.PEEK_BYTES} bytes; the whole of it: {whole}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        task = store.get_task(args.id)
        if task is None:
            print(f"no task #{args.id}", file=sys.stderr)
            return 1
        if args.json:
            from locks.ui import api

            print(json.dumps(api.task_detail(store, args.id), ensure_ascii=False, default=str))
            return 0
        print(f"#{task['id']} [{task['status']}] {task['title']}")
        print(f"repo {task['repo']}  kind {task['kind']} ({task['class_source']}: {task['class_reason']})")
        spent = usage.so_far(store, task)
        # A running attempt's cost is read live (`usage.so_far`), never shown as $0.00.
        cost = f"${spent['cost_usd']:.2f}" + (" so far" if spent.get("cost_live") else
                                              " + the running attempt's, not read yet" if spent.get("cost_running")
                                              else "")
        final = task["final_cell"] or ("(running)" if task["status"] == "running" else "-")
        print(f"cost {cost}  start {task['start_cell']}  final {final}")
        if task.get("points"):
            print(f"points {task['points']}: {task.get('points_why') or 'nothing says it is bigger'}")
        ended = (store.final_event(args.id) or {}).get("data") or {}
        if ended.get("realized"):
            print(f"realized {ended['realized']}: {', '.join(ended.get('realized_why') or ()) or 'a small change'}")
        if waits := dispatch.waiting(store, task):
            print(waits)
        if task["status"] == "running":
            now = progress.of(store, task)
            for line in (progress.line(now), progress.files_line(now)):
                if line:
                    print(f"now: {line}")
            for c in now.get("criteria") or ():
                print(f"  asked {c['id']}: {c['text']}")
            print(f"  its diff so far: locks peek {args.id}")
        refused = store.denials(args.id)
        climbed = {e["data"]["seq"]: e["data"] for e in store.events_of(args.id, "moved") if "seq" in e["data"]}
        checks: dict[int, list[dict]] = {}
        for e in store.events_of(args.id, "verified"):
            checks.setdefault(e["data"].get("seq"), []).append(e["data"])
        tails = store.check_tails(args.id)
        for a in store.attempts(args.id):
            verdict = "pass" if a["passed"] else (a["failure"] or "fail")
            model = f" [{a['served_model']}]" if a.get("served_model") else ""
            print(f"  attempt {a['seq']} {a['cell']}{model} ({a['turns']} turns, ${a['cost_usd']:.2f}) {verdict}"
                  + (f" → {a['move']}: {a['move_reason']}" if a["move"] else ""))
            if a["summary"]:
                print(f"    {clip(a['summary'], 300)}")
            if refused.get(a["seq"]):
                print(f"    refused: {', '.join(refused[a['seq']])}")
            if not a["passed"] and tails.get(a["seq"]):
                print("    the check's own output:")
                for line in tails[a["seq"]].splitlines():
                    print(f"      {line}".rstrip())
            for check in checks.get(a["seq"], ()):
                for line in _check_lines(check):
                    print(f"    {line}")
            move = climbed.get(a["seq"])
            if move:
                if move.get("to_cell"):
                    print(f"    {move.get('axis')}: {move.get('from_cell')} → {move['to_cell']}")
                for line in move.get("because") or ():
                    print(f"      · {line}")
                for line in move.get("skipped") or ():
                    print(f"      · skipped {line}")
        for line in run_lines(store, args.id):
            print(line)
        for e in store.events_of(args.id, "report"):
            # A reading worker's answer, kept whole: its deliverable, never clipped here.
            print(f"--- report (attempt {e['data'].get('seq')})")
            print(report_text(e["data"].get("report")))
        stop = stops.view(store, task)
        if stop:
            print(f"stopped by {stop['who']}: {stop['reason']}")
            print(f"  {stop['todo']}" + (f": {stop['next']}" if stop["next"] else ""))
        for m in store.messages(args.id):
            print(f"--- {m['role']} {m['ts']}\n{m['text'][:2000]}")
    finally:
        store.close()
    return 0


def report_text(report: object) -> str:
    """A kept `locks-report` as text: the markdown a reading worker wrote (`unparsed`), else its JSON."""
    if isinstance(report, dict) and set(report) == {"unparsed"}:
        return str(report["unparsed"])
    return json.dumps(report, ensure_ascii=False, indent=1)


def run_lines(store: Store, task_id: int) -> list[str]:
    """Each run of a task that ran more than once, in order: how it ended, at which cell it
    started and why, and how many attempts it made. A task that failed and was resumed to a
    pass reads as both, never as one "done"."""
    planned, ended = store.events_of(task_id, "planned"), store.events_of(task_id, "finished")
    if len(ended) < 2:
        return []
    lines = ["runs:"]
    for n, finished in enumerate(ended, 1):
        data = finished["data"] or {}
        plan = (planned[n - 1]["data"] or {}) if n <= len(planned) else {}
        why = next((r for r in plan.get("reasons") or () if str(r).startswith("resumed after")), "")
        lines.append(f"  {n}. {data.get('status')} after {data.get('attempts') or 0} attempt(s), started at "
                     f"{plan.get('start') or '?'}" + (f" ({clip(why, 200)})" if why else ""))
    return lines


def cmd_memory_search(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        found = store.search(_text(args.text), repo=_repo_key(args), limit=args.limit)
    finally:
        store.close()
    if not found:
        print("nothing recorded matches")
    for p in found:
        print(f"problem #{p['id']} [{p['state']}] {p['title']}  ({p['repo']})")
        for f in p["fixes"]:
            why = f" — {clip(f['why'], 120)}" if f["why"] else ""
            print(f"  {f['outcome']:<8} {clip(f['description'], 200)}{why}")
    return 0


def _keys(dirs: list[str] | None) -> set[str] | None:
    return {repo.key(Path(d).resolve()) for d in dirs} if dirs else None


def cmd_memory_list(args: argparse.Namespace) -> int:
    """Problems and their fixes, as JSON or lines: everything, or only some repositories."""
    from locks.ui import api

    store = Store.open()
    try:
        found = api.memory(store, args.query or "", limit=args.limit, repos=_keys(args.repo))
    finally:
        store.close()
    if args.json:
        print(json.dumps(found, ensure_ascii=False, default=str))
        return 0
    for p in found:
        print(f"problem #{p['id']} [{p['state']}] {p['title']}  ({p['repo']})")
        for f in p["fixes"]:
            print(f"  {f['outcome']:<8} {clip(f['description'], 200)}")
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    from locks.ui import api

    store = Store.open()
    try:
        found = api.projects(store, enrolled_only=not args.all)
    finally:
        store.close()
    if args.json:
        print(json.dumps(found, ensure_ascii=False, default=str))
        return 0
    for p in found:
        mark = ("" if p["exists"] else "  (gone)") + ("" if p["enrolled"] else "  (not enrolled: locks init)")
        print(f"{p['dir'] or p['repo']}  {p['sessions']} session(s), last {p['last_seen']}{mark}")
    return 0


def _overview_task(t: dict) -> str:
    head = f"#{t['id']} [{t['status']}]" + (f" (workflow run #{t['workflow_run']}, step {t['workflow_step']})"
                                            if t.get("workflow_run") else "")
    if t["state"] == "needs_you":
        why = f" ({t['cause']})" if t.get("cause") else ""
        then = f" — {t['next']}" if t.get("next") else ""
        return f"{head}{why} {t['title']}\n      waits on the person: {t['asks']}{then}"
    if t["state"] == "running":
        alive = "" if t.get("alive") is not False else ", its worker is not alive"
        return (f"{head} {t['title']}\n      attempt {t.get('attempt') or '?'} on {t.get('cell') or '?'} "
                f"since {t.get('since') or '?'}{alive}")
    if t["state"] == "queued":
        lane = f" (lane paused: {t['reason']})" if t.get("paused") else ""
        return f"{head} {t['title']}{lane}"
    return f"{head} {t['title']} — {t.get('cell') or '-'}, ${t['cost_usd']:.2f}, {t['updated_at']}"


def cmd_prune(args: argparse.Namespace) -> int:
    from locks import config

    dry = args.dry_run or not config.enabled("prune", os.environ)
    if dry and not args.dry_run:
        print("pruning is off (`locks config prune on`); this is what it would delete:")
    dirs = None
    if args.repo:
        dirs = [top for top in (repo.toplevel(Path(d).resolve()) for d in args.repo) if top is not None]
    store = Store.open()
    try:
        gone = flow.prune(store, dirs, dry_run=dry)
    finally:
        store.close()
    for x in gone:
        print(f"{'would delete' if dry else 'deleted'} {x['branch']} ({x['why']}) in {x['repo']}; "
              f"back with: git branch {x['branch']} {x['tip']}")
    if not gone:
        print("no branch locks kept has landed")
    return 0


def cmd_overview(args: argparse.Namespace) -> int:
    """Every session seen lately, and its work: what waits, runs, is queued, ended.
    A read for a watcher; it claims no ending, so each session still hears its own."""
    from locks.ui import api

    store = Store.open()
    try:
        repos = {repo.key(Path(d).resolve()) for d in args.repo} if args.repo else None
        seen = api.overview(store, hours=args.hours, limit=args.limit, repos=repos)
    finally:
        store.close()
    if args.json:
        print(json.dumps(seen, ensure_ascii=False, default=str))
        return 0
    order = {"needs_you": 0, "running": 1, "queued": 2, "ended": 3}
    print(f"locks: sessions seen since {seen['since']}, and their work")
    for x in seen["sessions"]:
        state = "answering a prompt" if x["answering"] else "idle"
        print(f"\n{x['name'] or x['id']} — {x['cwd']} ({state}, last seen {x['last_seen_at']})")
        print(f"  resume: {x['resume']}")
        if x["last_prompt"]:
            print(f"  last prompt: {x['last_prompt']}")
        for t in sorted(x["tasks"], key=lambda t: (order[t["state"]], -t["id"])):
            print(f"  {_overview_task(t)}")
    if seen.get("workflows"):
        print("\nWorkflow runs (each goes on by itself; `locks workflow status <run>` for one):")
        for w in seen["workflows"]:
            steps = " → ".join(f"{s['id']}:{s['state']}" for s in w["steps"])
            print(f"  run #{w['id']} {w['name']} — {w['state']} · {w['cwd']}\n      {steps}")
    if seen["other"]:
        print("\nWork no listed session sent (the queue, a terminal, an older session):")
        for t in sorted(seen["other"], key=lambda t: (order[t["state"]], -t["id"])):
            print(f"  {_overview_task(t)}")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    from locks.ui import api

    store = Store.open()
    try:
        found = api.sessions(store, repos=_keys(args.repo), limit=args.limit)
    finally:
        store.close()
    if args.json:
        print(json.dumps(found, ensure_ascii=False, default=str))
        return 0
    for x in found:
        print(f"{x['id']}  {x['last_seen_at']}  {x['prompts']} prompt(s)  {x['last_prompt'] or ''}\n  {x['resume']}")
    return 0


def cmd_memory_record(args: argparse.Namespace) -> int:
    key = _repo_key(args)
    store = Store.open()
    try:
        problem = args.problem_id or store.open_problem(args.problem, repo=key, symptom=args.symptom or "")
        fix = store.add_fix(problem, args.fix, args.outcome, repo=key, why=args.why or "",
                            evidence=args.evidence or "", commit_sha=args.commit)
    finally:
        store.close()
    print(f"recorded fix #{fix} on problem #{problem}")
    return 0


def cmd_memory_invalidate(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        problem = store.invalidate_fix(args.fix_id, args.why)
    finally:
        store.close()
    if problem is None:
        print(f"no fix #{args.fix_id}", file=sys.stderr)
        return 1
    print(f"fix #{args.fix_id} disproved; problem #{problem['id']} is {problem['state']}")
    return 0


def cmd_memory_forget(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        for problem_id in args.problem_ids:
            problem = store.forget_problem(problem_id)
            if problem is None:
                print(f"no problem #{problem_id}", file=sys.stderr)
                return 1
            said = title_of(problem["title"], 100)
            print(f"forgot problem #{problem_id} and its {len(problem['fixes'])} fix(es): {said}")
    finally:
        store.close()
    return 0


def cmd_spend(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        report = usage.spend(store, days=args.days, repo=_repo_key(args))
    finally:
        store.close()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"last {args.days} day(s): ${report['total_usd']:.2f} at API rates")
    print("sessions, by model:")
    for row in report["sessions"]:
        tokens_in = row["input_tokens"] + row["cache_read"] + row["cache_write"]
        print(f"  {row['family']:<7} {row['model']:<24} {row['output_tokens']:>9} out  {tokens_in:>11} in  "
              f"${row['usd']:.2f}")
    print("workers, by cell:")
    for row in report["workers"]:
        print(f"  {row['cell']:<14} {row['attempts']:>3} attempts  {row['passed'] or 0:>3} passed  "
              f"${row['usd'] or 0:.2f}")
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    if args.example:
        print(json.dumps(capabilities.EXAMPLE, indent=2))
        return 0
    from locks import plugins

    path = home() / "capabilities.json"
    registry = capabilities.load(path)
    found = plugins.installed() if config.enabled("plugins", os.environ) else {}
    if not registry and not found:
        print(f"no capabilities registered in {path} (see `locks capabilities --example`), "
              "and no neighbour installed as a Claude Code plugin")
    every = capabilities.load_all(path)
    for c in every.values():
        when = c.when if c.when == "always" else ", ".join(c.when)
        extra = f"; after a failed {', '.join(c.after_failure)}" if c.after_failure else ""
        source = "your capabilities.json" if c.name in registry else f"installed: {found[c.name].source}"
        print(f"{c.name:<20} {when}{extra}  ({source})")
    if not config.enabled("plugins", os.environ):
        print("installed plugins are left out: `locks config plugins on` hands them to workers")
    for line in capabilities.notices(path):
        print(line.removeprefix("locks: "))
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    if args.key == "model":
        return _config_model(args)
    if args.key == "mode":
        return _config_mode(args)
    if args.key is None:
        for key, value in sorted(config.load().items()):
            print(f"{key} = {json.dumps(value)}")
        return 0
    if args.key not in config.DEFAULTS:
        print(f"unknown setting {args.key!r}; known: {', '.join(config.DEFAULTS)}", file=sys.stderr)
        return 1
    if args.value is None:
        print(json.dumps(config.load()[args.key]))
        return 0
    if args.key == "max_parallel":
        picked = config.ceiling_word(args.value)
        if picked is None:
            print("expected auto or a whole number from 1: the most workers this machine runs at once",
                  file=sys.stderr)
            return 1
        config.save({args.key: picked})
        print(f"{args.key} = {json.dumps(picked)}")
        if picked != "auto":
            # A number is only a ceiling: what runs under it already moves by itself.
            print("a ceiling only: the level under it already adapts to the API, memory, disk and load "
                  "(`locks lanes` says where it is and why); `auto` puts no ceiling over it")
        return 0
    if args.key == "livespec_wait":
        seconds = config.seconds_word(args.value)
        if seconds is None:
            print("expected whole seconds from 0: how long a task waits for livespec's index before its worker "
                  "starts (0 never waits)", file=sys.stderr)
            return 1
        config.save({args.key: seconds})
        print(f"{args.key} = {json.dumps(seconds)}")
        return 0
    if args.key in config.CHOICES:
        picked = config.word(args.key, args.value)
        if picked is None:
            print(f"expected one of {', '.join(config.CHOICES[args.key])}", file=sys.stderr)
            return 1
        config.save({args.key: picked})
        print(f"{args.key} = {json.dumps(picked)}")
        return 0
    value = config._bool(args.value)
    if value is None:
        print("expected on or off", file=sys.stderr)
        return 1
    config.save({args.key: value})
    print(f"{args.key} = {json.dumps(value)}")
    return 0


def _config_mode(args: argparse.Namespace) -> int:
    """`locks config mode [alias|default [mode|reset]]`: the permission mode workers run in."""
    from locks import grants
    from locks.matrix import MODELS

    names = (*MODELS, "default")
    if args.value is not None and args.value not in names:
        print(f"unknown model {args.value!r}; known: {', '.join(names)}", file=sys.stderr)
        return 1
    if args.value is not None and args.extra is not None:
        try:
            grants.set_mode(args.value, None if args.extra == "reset" else args.extra)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    for alias in [args.value] if args.value and args.value != "default" else MODELS:
        print(f"{alias:<7} {grants.mode_for(alias)}")
    return 0


def cmd_allow(args: argparse.Namespace) -> int:
    """Rules every worker in this repository gets, kept in the person's locks home."""
    from locks import grants

    where = Path(args.repo or os.getcwd()).resolve()
    key = repo.key(where)
    if not key:
        print(f"{where} is no repository locks can key rules to", file=sys.stderr)
        return 1
    try:
        rules = grants.expand(args.rules, args.preset or ())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.rm:
        kept = grants.revoke(key, rules)
    elif rules:
        kept = grants.grant(key, rules)
    else:
        kept = grants.granted(key)
    print(f"workers in {key} get: " + (", ".join(kept) if kept else "nothing beyond their mode"))
    return 0


def _config_model(args: argparse.Namespace) -> int:
    """`locks config model [alias [id|default]]`: what each alias runs as."""
    from locks import models
    from locks.matrix import MODELS

    if args.value is not None and args.value not in MODELS:
        print(f"unknown model {args.value!r}; known: {', '.join(MODELS)}", file=sys.stderr)
        return 1
    if args.value is not None and args.extra is not None:
        try:
            models.pin(args.value, None if args.extra == "default" else args.extra)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    store = Store.open()
    try:
        served, lag = store.worker_models(), {x["alias"]: x for x in models.lagging(store)}
    finally:
        store.close()
    for alias in [args.value] if args.value else MODELS:
        pin = models.pinned(alias)
        line = f"{alias:<7} {'pinned to ' + pin if pin else 'follows Claude Code'}"
        if served.get(alias):
            line += f"; workers last ran {served[alias]}"
        if alias in lag:
            line += f"; your sessions use {lag[alias]['sessions']}: `{lag[alias]['command']}` pins it"
        print(line)
    return 0


def cmd_neighbours(args: argparse.Namespace) -> int:
    where = Path(args.repo or os.getcwd())
    for key in ("livespec", "engram", "engram_write"):
        print(f"{key} setting: {'on' if config.enabled(key, os.environ) else 'off'}")
    from locks.adapters.engram import Engram
    from locks.adapters.livespec import Livespec

    for adapter in (Livespec(), Engram()):
        status = adapter.inspect(where)
        print(status.line())
        server = adapter.server()
        print(f"  runs as: {' '.join([server['command'], *server.get('args', [])]) if server else 'nothing found'}")
    from locks import plugins

    for neighbour in plugins.KNOWN:
        found = plugins.find(neighbour, os.environ)
        if found is None:
            print(f"{neighbour.plugin}: not installed (claude plugin install {neighbour.plugin}@rixmerz)")
            continue
        runs = " ".join([found.server["command"], *found.server.get("args", [])])
        skill = f"; skill {found.skill_dir}" if found.skill_dir else ""
        print(f"{neighbour.plugin}: {found.source}\n  runs as: {runs}{skill}")
    return 0


def _check_lines(check: dict) -> list[str]:
    """An independent check of an attempt, as its `verified` event kept it: its
    verdict, then each blocker and each other finding, clipped like the summary."""
    blockers = findings_mod.read({"findings": check.get("blockers") or []})
    # The blockers are records of the findings themselves: only the very same record
    # is left out, never another finding in a blocker's file (`findings.same` is that).
    others = [f for f in findings_mod.read({"findings": check.get("findings") or []}) if f not in blockers]
    verdict = "passed" if check.get("passed") else check.get("verdict") or "undecided"
    lines = [f"independent check at {check.get('cell') or '?'}: {verdict}"
             + (f", {len(blockers)} blocker(s)" if blockers else "")]
    for label, found in (("blocks", blockers), ("found", others)):
        # One line each: a proof is often a command's output, over several lines.
        lines += [clip(f"  {label}: {' '.join(findings_mod.text([f]).removeprefix('- ').split())}", 300) for f in found]
    return lines


def cmd_queue(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        if args.queue_command == "add":
            _notify(args)
            if not _request_from_stdin(args):
                return 2
            if (split := _split_first(args, args.text)) is not None:
                return split
            where = Path(args.repo or os.getcwd()).resolve()
            options = {k: v for k, v in {"verify": args.verify, "kind": args.kind,
                                         "start": args.start, "allow_tools": args.allow,
                                         "surfaces": list(_surfaces(args.surface)) or None,
                                         "carry": args.carry or None,
                                         "add_dirs": [str(Path(d).expanduser().resolve()) for d in args.add_dir]
                                         if args.add_dir else None,
                                         "services": args.service or None,
                                         "whole": True if args.whole else None}.items() if v is not None}
            if args.after is not None:
                try:
                    options[after.KEY] = after.target_of(store, args.after)
                except after.AfterError as exc:
                    print(f"locks: not queued: {exc}", file=sys.stderr)
                    return 2
            task = _enqueue_after(store, _text(args.text), where, options)
            then = dispatch.kick(where, repo.key(where), os.environ)
            said = _held_line(store, task["id"]) if args.after is not None else None
            if said and (note := after.caveat(store, options[after.KEY], task["repo"])):
                said += f" ({note})"
            eta = progress.expected_line(store, task)
            if args.json:
                print(json.dumps({**{k: task[k] for k in ("id", "title", "status", "repo", "cwd")}, "then": then,
                                  **({"waits": said} if said else {}), **({"expected": eta} if eta else {})},
                                 ensure_ascii=False))
            else:
                print(f"queued #{task['id']} — {task['title']}" + (f": {said}" if said else "") + f". {then}"
                      + (f"\n{eta}" if eta else ""))
            return 0
        if args.queue_command == "rm":
            task = store.get_task(args.id)
            if task is None or task["status"] != "queued":
                print(f"#{args.id} is not queued", file=sys.stderr)
                return 1
            stops.record(store, args.id, _cancel_stop("queue"))
            print(f"removed #{args.id}")
            return 0
        for t in store.list_tasks(repo=_repo_key(args), status=["queued"], limit=200)[::-1]:
            print(f"#{t['id']:<5} {t['repo'] or '-'}  {t['title']}")
            if waits := dispatch.waiting(store, t):
                print(f"       {waits}")
        return 0
    finally:
        store.close()


def cmd_lanes(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        if args.beside is not None:
            task = store.get_task(args.beside)
            if task is None or task["status"] != "queued":
                print(f"task #{args.beside} is {task['status'] if task else 'not there'}; only a queued task is let "
                      "start beside the work ahead", file=sys.stderr)
                return 1
            if after.held(task):
                print(f"task #{args.beside} {dispatch.waiting(store, task)}: `--after` is its order, not the "
                      f"dispatcher's call; `locks dismiss {args.beside}` takes it off the queue", file=sys.stderr)
                return 1
            reason = "the session's word: it does not touch the work ahead"
            store.set_parallel(task["id"], True, reason)
            store.add_event(task["id"], "dispatch_decided", parallel=True, reason=reason, ahead=[], by="session")
            print(f"task #{task['id']} starts beside the work ahead"
                  + (", its dispatcher started" if _go_on(store, task.get("repo")) else ""))
            return 0
        if args.unpause is not None:
            key = repo.key(Path(args.unpause).resolve()) if Path(args.unpause).is_dir() else args.unpause
            store.unpause_lane(key)
            print(f"unpaused {key}" + (", started its dispatcher" if _go_on(store, key) else ""))
            return 0
        # The workers this machine runs at once, and each lane's share of them (`locks.capacity`).
        ceiling = config.max_parallel(os.environ)
        level = capacity.current(home(), ceiling=ceiling)
        print(f"workers: {capacity.line(level)} · max_parallel {ceiling or 'auto'}")
        shared = flow.shares(store, level.value)
        rows = {lane["repo"] or None: lane for lane in store.lanes()}
        for key in [*rows, *(k for k in shared if k not in rows)]:
            lane = rows.get(key) or {"queued": 0, "paused": 0}
            state = f"paused: {lane['reason']}" if lane["paused"] else "open"
            share = f" · {shared[key].text()}" if key in shared else ""
            print(f"{key or '-'}  {lane['queued']} queued  {state}{share}")
        return 0
    finally:
        store.close()


def cmd_work(args: argparse.Namespace) -> int:
    from locks import dispatch

    key = None if args.all else repo.key(Path(args.repo or os.getcwd()).resolve())
    with dispatch.hold(home(), key or "*") as mine:
        if not mine:
            print(f"a dispatcher already runs {key or 'every repository'}; it picks this up")
            return 0
        store = Store.open()
        try:
            report = flow.work(store, repo=key, max_tasks=flow.DEFAULT_MAX_TASKS if args.max is None else args.max,
                               defaults=orchestrate.Options(use_model_classifier=not args.no_model))
        finally:
            store.close()
    # Work queued while this one was leaving found its lock held and was left to it (`dispatch.wake`):
    # it read its queue before that work was there, so a fresh dispatcher takes it now, not the next hook.
    unseen = dispatch.woken(home(), key or "*", report.looked_at)
    if unseen or (report.at_limit and getattr(args, "detached", False)):
        # The bound is per process: a detached dispatcher that reached it with work
        # still queued starts a fresh one after its lock is released, since nothing
        # else would (a step's parts all queue at once, and the step waits for them).
        store = Store.open()
        try:
            keys = [key] if key is not None else [
                lane["repo"] for lane in store.lanes() if lane["queued"] and not lane["paused"]]
            handed = [k for k in keys if dispatch.start_lane(store, home(), k)]
        finally:
            store.close()
        if handed:
            report.stopped_because += ("; a fresh dispatcher takes what was queued as this one left" if unseen
                                       and not report.at_limit else "; a fresh dispatcher takes the rest")
    print(report.text())
    return 0 if all(r.status == "done" for r in report.ran) else 1


def cmd_index_livespec(args: argparse.Namespace) -> int:
    """A background refresh, run detached by SessionStart under the index's lock."""
    from locks.adapters.livespec import Livespec

    print(Livespec().refresh(Path(args.dir)))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    where = Path(args.dir or os.getcwd()).resolve()
    if not where.is_dir():
        print(f"no such directory: {where}", file=sys.stderr)
        return 1
    try:
        folder = project.enroll(where)
    except project.NotAProject as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"enrolled: {folder} — this project and its sessions now show in the UI; in a session, /locks:init "
          "builds your team, workflows and memory from every session on this machine")
    return 0


def cmd_survey(args: argparse.Namespace) -> int:
    """What every local Claude Code session and its repository's history say: read only."""
    from locks import survey

    only = repo.key(Path(args.repo).resolve()) if args.repo else None
    store = Store.open()
    try:
        found = survey.gather(store, days=args.days, limit=args.sessions, per_project=args.per_project, only=only,
                              deep=args.deep)
    finally:
        store.close()
    print(survey.as_json(found) if args.json else survey.text(found))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Where locks' time and money go, read from the store: no model."""
    from datetime import UTC, datetime, timedelta

    from locks import analysis

    key = repo.key(Path(args.repo).resolve()) if args.repo else None
    store = Store.open()
    try:
        if args.attempts:
            since = (datetime.now(UTC) - timedelta(days=args.days)).isoformat(timespec="seconds") if args.days else ""
            rows = analysis.attempts(*store.runs_since(since, repo=key), since=since)
            sys.stdout.write(analysis.attempts_csv(rows) if args.csv else analysis.as_json(rows) + "\n")
            return 0
        report = analysis.compute(store, days=args.days, repo=key, keep_runs=None if args.runs else 20)
    finally:
        store.close()
    if args.expensive:
        print(analysis.as_json(report["expensive"]) if args.json else analysis.expensive_text(report["expensive"]))
    elif args.runs:
        sys.stdout.write(analysis.runs_csv(report["runs"]) if args.csv else analysis.as_json(report["runs"]) + "\n")
    else:
        print(analysis.as_json(report) if args.json else analysis.text(report))
    return 0


def cmd_trust(args: argparse.Namespace) -> int:
    """What the independent check's record says of each cell, read from the store: no model."""
    from locks import trust

    key = None if args.all else repo.key(Path(args.repo or ".").resolve())
    store = Store.open()
    try:
        rows = trust.report(store, key)
    finally:
        store.close()
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("No independent check yet" + (f" in {key}." if key else "."))
    else:
        print("\n".join(trust.text(rows)))
    return 0


def cmd_cells(args: argparse.Namespace) -> int:
    """Per kind, where locks starts it and how each cell's first tries went here: no model."""
    from locks import cells_by_kind

    key = None if args.all else repo.key(Path(args.repo or ".").resolve())
    store = Store.open()
    try:
        rows = cells_by_kind.report(store, key)
    finally:
        store.close()
    print(json.dumps(rows, indent=2) if args.json else "\n".join(cells_by_kind.text(rows, key)))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """States the store should never hold, read, never written: 1 when something is broken now."""
    from locks import doctor

    key = repo.key(Path(args.repo).resolve()) if args.repo else None
    store = Store.open()
    try:
        found = doctor.check(store, repo=key, days=args.days)
    finally:
        store.close()
    print(json.dumps([f.data() for f in found], indent=2) if args.json else doctor.text(found))
    return 1 if any(f.severity == doctor.BROKEN for f in found) else 0


def cmd_evolution(args: argparse.Namespace) -> int:
    """How locks evolves and why, read from the store: no model. `--snapshot` takes one now."""
    from locks import evolution

    key = repo.key(Path(args.repo).resolve()) if args.repo else None
    store = Store.open()
    try:
        if args.snapshot:
            taken = evolution.snapshot(store, until_now=True)
            print(f"snapshot: {len(taken)} row{'' if len(taken) == 1 else 's'}",
                  file=sys.stderr if args.json else sys.stdout)
        found = evolution.report(store, days=args.days, repo=key)
    finally:
        store.close()
    print(evolution.as_json(found) if args.json else evolution.text(found))
    return 0


def cmd_relay(args: argparse.Namespace) -> int:
    """Hand a session's thread to the next session that starts in its place."""
    from locks import compact, scribe
    from locks.store import OPEN_FOR_A_RELAY

    where = Path(os.getcwd()).resolve()
    store = Store.open()
    try:
        session = args.session or os.environ.get("LOCKS_SESSION_ID")
        if not session and (key := repo.key(where)):
            found = store.sessions(repos=[key], limit=1)
            session = found[0]["id"] if found else None
        if not session:
            print("locks: no session to relay from here: run it from the session itself (/locks:relay), or pass "
                  "--session", file=sys.stderr)
            return 1
        state = scribe.load(home(), session)
        transcript = (state or {}).get("transcript") or scribe.find_transcript(session)
        if transcript:
            state = scribe.update(home(), session, transcript) or state
        if not state or not state.get("requests"):
            print(f"locks: session {session} has no thread to hand over yet", file=sys.stderr)
            return 1
        place = state.get("cwd") or str(where)
        scribe.mark_relay(home(), session, place)
        tasks = store.list_tasks(session_id=session, status=OPEN_FOR_A_RELAY, source=("locks", "queue"), limit=15)
    finally:
        store.close()
    print(f"relay ready: the next session that starts in {place} continues {session} with its thread "
          f"({len(state['requests'])} request(s) verbatim, {len(state['written'])} file(s) written, {len(tasks)} "
          "open locks task(s), which will report there). Type /clear, or open a new session there, to start it; "
          "this session stays as it is.")
    if args.show:
        print()
        print(scribe.relay_text(state, tasks=tasks, changed=compact._changed(Path(place)),
                                commits=scribe.commits_since(place, state.get("started"))))
    return 0


def cmd_orchestrator(args: argparse.Namespace) -> int:
    """Show, claim or drop the session that orchestrates a project (`locks.orchestrator`)."""
    where = Path(args.repo or os.getcwd()).resolve()
    folder = project.find(where)
    if folder is None:
        print(f"locks: {where} is no locks project yet (it becomes one when work is sent there)", file=sys.stderr)
        return 1
    session = os.environ.get("LOCKS_SESSION_ID")
    moved = 0
    if args.claim or args.release:
        if not session and args.claim:
            print("locks: --claim runs in the session that takes the role (its Bash)", file=sys.stderr)
            return 1
        store = Store.open()
        try:
            if args.claim:
                _, moved = orchestrator.take(store, folder, session, str(where), via="claimed", force=True)
            elif not orchestrator.release(folder, session):
                print("locks: this session does not hold the role", file=sys.stderr)
                return 1
        finally:
            store.close()
    found = orchestrator.held(folder)
    if args.json:
        print(json.dumps({"orchestrator": found, "this_session": bool(found and found["session"] == session),
                          "moved": moved}))
        return 0
    if not found:
        print("no session orchestrates this project; the next one that sends work will")
        return 0
    state = (f", ended ({found.get('end_reason')}) — the next session in its place takes it"
             if found.get("ended") else "")
    mine = " (this session)" if found["session"] == session else ""
    print(f"orchestrator: {found['session']}{mine}, since {found.get('since')} ({found.get('via')}){state}")
    if moved:
        print(f"{moved} task(s) it sent report to this session now")
    return 0


def cmd_name_session(args: argparse.Namespace) -> int:
    """Run detached by the Stop hook: Haiku names one session."""
    from locks import dispatch, naming

    with dispatch.hold(home(), f"name:{args.id}") as mine:
        if not mine:
            return 0
        store = Store.open()
        try:
            name = naming.name_session(store, args.id, cwd=home())
        finally:
            store.close()
    print(name or "unchanged")
    return 0


# --- notes: what a project knows, by topic ------------------------------------


def _project(args: argparse.Namespace) -> tuple[str, Path]:
    where = Path(getattr(args, "repo", None) or os.getcwd()).resolve()
    key = repo.key(where)
    if key is None:
        raise SystemExit(f"locks: {where} is not a directory")
    return key, where


def _print_notes(found: list[dict], as_json: bool) -> None:
    from locks import notes

    if as_json:
        print(notes.to_json(found))
        return
    if not found:
        print("no notes match")
    for n in found:
        state = ("" if n["state"] == "current" else f" [{n['state']}]") + (
            " [constraint]" if n.get("kind") == notes.CONSTRAINT else "")
        why = f"  ({n['why']})" if n.get("why") else ""
        print(f"#{n['id']} [{n['topic']}]{state} {n['title']}{why}")
        if not n["text"].startswith(n["title"].rstrip("…")):
            print(f"  {n['text']}")
        if n["state"] == "review" and n.get("state_reason"):
            print(f"  to review: {n['state_reason']}")
        if n.get("anchors"):
            print("  about: " + ", ".join(f"{a['symbol']} ({a['path']})" if a["symbol"] else a["path"]
                                          for a in n["anchors"]))
        if n.get("links"):
            print("  links: " + ", ".join(f"{x['kind']} #{x['to']}" if x["out"] else f"#{x['to']} {x['kind']} this"
                                          for x in n["links"]))


def cmd_note(args: argparse.Namespace) -> int:
    """A person keeps a fact about this project."""
    from locks import notes

    key, where = _project(args)
    store = Store.open()
    try:
        try:
            topic = (notes.resolve_topic(store, key, args.topic) if args.topic
                     else (notes.topics_in(_text(args.text)) or ["code"])[0])
            links = []
            for spec in args.link or []:
                target, _, kind = spec.partition(":")
                links.append((int(target.lstrip("#")), kind or "depends_on"))
            note_id, new = notes.add(store, key, _text(args.text), topic=topic, title=args.title,
                                     author="session" if os.environ.get("LOCKS_SESSION_ID") else "person",
                                     filed_by="person" if args.topic else "rule",
                                     source="locks note", session_id=os.environ.get("LOCKS_SESSION_ID"),
                                     links=links, paths=args.anchor or [], repo_dir=where,
                                     kind=notes.CONSTRAINT if args.constraint else None)
        except ValueError as exc:
            print(f"locks: {exc}", file=sys.stderr)
            return 2
        note = store.get_note(note_id)
    finally:
        store.close()
    if args.json:
        print(json.dumps({"id": note_id, "new": new, "topic": note["topic"], "title": note["title"]},
                         ensure_ascii=False))
    else:
        print(f"{'kept' if new else 'already known as'} #{note_id} [{note['topic']}] {note['title']}"
              + (" as a constraint: every worker here is told its change must meet it" if args.constraint else ""))
    return 0


def cmd_workspace(args: argparse.Namespace) -> int:
    """A folder of repositories that is one project: declare it, list them, forget one."""
    from locks import grants, notes, teams, workspaces

    if args.workspace_command == "list":
        found = workspaces.declared()
        if args.json:
            print(json.dumps(found, ensure_ascii=False))
            return 0
        for w in found:
            print(f"{w['folder']}: " + ", ".join(f"{m['name']} ({m['path']})" for m in w["members"]))
        if not found:
            print("no workspace: `locks workspace add <folder> --with <repo> --with <repo>` makes one")
        return 0
    if args.workspace_command == "rm":
        if not workspaces.remove(args.folder):
            print(f"locks: no workspace at {Path(args.folder).expanduser().resolve()}", file=sys.stderr)
            return 1
        print("forgotten: its repositories are projects of their own again for new work; what the workspace "
              "kept (notes, tasks) stays under the folder")
        return 0
    folder = Path(args.folder).expanduser().resolve()
    tops = [t for t in (repo.toplevel(Path(r).expanduser()) for r in args.with_ or ()) if t is not None]
    inside = [c for c in sorted(folder.iterdir()) if (c / ".git").exists()] if folder.is_dir() else []
    joining = {str(t.resolve()): repo.own_key(t) for t in [*tops, *inside]}
    store = Store.open()
    try:
        if busy := store.busy([k for k in joining.values() if k]):
            print(f"locks: task(s) {', '.join(f'#{i}' for i in busy)} of these repositories are queued or running: "
                  "let them end (or cancel them), then make the workspace", file=sys.stderr)
            return 1
        try:
            entry = workspaces.add(folder, args.with_ or ())
        except workspaces.WorkspaceError as exc:
            print(f"locks: {exc}", file=sys.stderr)
            return 2
        new = entry["folder"]
        moved = {"notes": 0, "tasks": 0}
        merged_rules = False
        used = teams.assignments()
        picked = set()
        for m in entry["members"]:
            old = joining.get(m["path"]) or repo.own_key(Path(m["path"]))
            if old and old != new:
                for k, n in store.rekey(old, new, prefix=f"{m['name']}/", inside=m["path"]).items():
                    moved[k] += n
                kept_rules = grants.granted(old)
                if kept_rules:
                    grants.grant(new, kept_rules)
                    merged_rules = True
                if used.get(old):
                    picked.add(used[old])
        merged = notes.merge_same(store, new)
    finally:
        store.close()
    with contextlib.suppress(OSError, project.NotAProject):
        folder_state = project.enroll(new)
        for m in entry["members"]:
            # The session names a person wrote in a repository stay theirs in the folder.
            if (mine := legacy.project_folder(Path(m["path"]))) is not None and mine != folder_state:
                project.take_names(mine, folder_state)
    print(f"workspace {new}: " + ", ".join(f"{m['name']} ({m['path']})" for m in entry["members"]))
    print(f"  one project now: {moved['notes']} note(s) and {moved['tasks']} task(s) moved to it"
          + (f", {merged} repeated note(s) merged" if merged else "")
          + "; work that spans the repositories runs in the folder"
          + ("; the rules each kept with `locks allow` now hold for all of them" if merged_rules else ""))
    if new not in used and len(picked) == 1:
        teams.use(new, picked.pop())
    elif new not in used and picked:
        print(f"  its repositories used the teams {', '.join(sorted(picked))}: `locks team use <name> --repo {new}` "
              "picks the folder's")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    """The notes a question needs: by topic, by the code it is about, one link away."""
    from locks import notes

    key, where = _project(args)
    store = Store.open()
    try:
        notes.review(store, key, where)
        try:
            in_topics = [notes.resolve_topic(store, key, t) for t in args.topic] if args.topic else None
        except ValueError as exc:
            print(f"locks: {exc}", file=sys.stderr)
            return 2
        states = notes.STATES if args.all_states else notes.LIVE
        # A file of a workspace's member is named with the member in front, as its anchors are.
        from locks import workspaces

        pre = workspaces.prefix(repo.toplevel(where))
        found = notes.recall(store, key, _text(args.text or ""), in_topics=in_topics,
                             paths=[pre + p.removeprefix("./") for p in args.path or []],
                             limit=args.limit, hops=args.hops, states=states)
    finally:
        store.close()
    _print_notes(found, args.json)
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    from locks import notes

    key, where = _project(args)
    store = Store.open()
    try:
        command = args.notes_command or "list"
        try:
            if command == "list":
                notes.review(store, key, where)
                in_topics = [notes.resolve_topic(store, key, t) for t in args.topic] if args.topic else None
                states = ["review"] if args.review else (notes.STATES if args.all_states else notes.LIVE)
                rows = store.notes(key, topics=in_topics, states=states, limit=args.limit,
                                   kind=notes.CONSTRAINT if args.constraints else None)
                _print_notes(notes.expand(store, [r["id"] for r in rows]), args.json)
            elif command == "show":
                found = notes.expand(store, [args.id])
                if not found or found[0]["project"] != key:
                    print(f"locks: no note #{args.id} in this project", file=sys.stderr)
                    return 1
                _print_notes(found, args.json)
            elif command == "topics" and args.json:
                counts = store.note_counts(key)
                print(json.dumps([{"name": name, "description": about,
                                   "notes": sum(counts.get(name, {}).get(s, 0) for s in notes.LIVE)}
                                  for name, about in notes.topics(store, key).items()], ensure_ascii=False))
            elif command == "topics":
                counts = store.note_counts(key)
                for name, about in notes.topics(store, key).items():
                    by = counts.get(name, {})
                    print(f"{name:<14} {sum(by.get(s, 0) for s in notes.LIVE):>3} note(s)  {about}")
                proposed = [n for n in store.notes(key, states=notes.LIVE) if n["proposed_topic"]]
                for n in proposed:
                    print(f"  proposed topic {n['proposed_topic']!r} for #{n['id']} {n['title']} "
                          f"(add it with `locks notes topic add {n['proposed_topic']} \"<what it holds>\"`)")
            elif command == "topic":
                name = notes.add_topic(store, key, args.name, args.description)
                print(f"topic {name} added to this project")
            else:
                note = store.get_note(args.id)
                if note is None or note["project"] != key:
                    print(f"locks: no note #{args.id} in this project", file=sys.stderr)
                    return 1
                if command == "ok":
                    notes.confirm(store, args.id, where)
                    print(f"#{args.id} is current again, anchored to the code as it is now")
                elif command == "drop":
                    store.update_note(args.id, state="dropped", state_reason=args.why or "dropped by a person")
                    print(f"#{args.id} dropped")
                elif command == "move":
                    topic = notes.resolve_topic(store, key, args.topic)
                    store.update_note(args.id, topic=topic, proposed_topic=None)
                    print(f"#{args.id} moved to {topic}")
                elif command == "link":
                    notes.link(store, args.id, args.to, args.kind)
                    print(f"#{args.id} {args.kind} #{args.to}")
                elif command == "unlink":
                    gone = store.unlink_notes(args.id, args.to)
                    print(f"{gone} link(s) between #{args.id} and #{args.to} removed")
        except ValueError as exc:
            print(f"locks: {exc}", file=sys.stderr)
            return 2
    finally:
        store.close()
    return 0


def cmd_extract_notes(args: argparse.Namespace) -> int:
    """Run detached by PreCompact and SessionEnd: Haiku keeps what a session established."""
    from locks import dispatch, notes

    where = Path(args.cwd).resolve()
    key = repo.key(where)
    if key is None:
        return 0
    with dispatch.hold(home(), f"notes:{args.session}") as mine:
        if not mine:
            return 0
        store = Store.open()
        try:
            from locks import notes_fork

            ask, side, answered = None, None, []
            if notes_fork.enabled():
                side, _why = notes_fork.arm(store, key)
                base = notes_fork.ask_fork(args.session, where) if side == "fork" else notes._ask(None)

                def ask(*a, **kw):  # the cost of the one answer, for the trial's record
                    got = base(*a, **kw)
                    answered.append(got)
                    return got
            filed = notes.extract(store, key, args.session, Path(args.transcript), repo_dir=where,
                                  symbols_of=notes.livespec_symbols(where), ask=ask)
            if side and answered and answered[0][0] is not None:
                notes_fork.tally(store, key, side, kept=sum(1 for f in filed if f.get("new")),
                                 usd=float(answered[0][1]))
        finally:
            store.close()
    from locks import trace

    trace.action(os.environ, args.session, "notes_filed", repo=key, fork=side,
                 notes=[{"id": f["id"], "new": bool(f["new"]), "topic": f["topic"]} for f in filed])
    for f in filed:
        print(f"{'kept' if f['new'] else 'already known'} #{f['id']} [{f['topic']}] {f['title']}")
    return 0


def cmd_run_queued(args: argparse.Namespace) -> int:
    """One task the dispatcher claimed, in its own process."""
    store = Store.open()
    previous = _cancel_on_sigterm()
    try:
        task = store.get_task(args.id)
        if task is None or task["status"] != "running" or not task["dispatched"]:
            print(f"#{args.id} is not a task the dispatcher claimed", file=sys.stderr)
            return 1
        options = flow.queued_options(task, orchestrate.Options(use_model_classifier=not args.no_model))
        options.detach_after = True
        report = orchestrate.run(task["body"], Path(task["cwd"]), store, options, task_id=task["id"])
    finally:
        signal.signal(signal.SIGTERM, previous)
        store.close()
    print(report.text())
    return _exit(report)


def _candidates(store: Store, days: int) -> list[habits.Candidate]:
    habits.load_events(store)
    return habits.candidates(store.tool_events(days=days))


def _habit_repo(directory: str | None) -> str | None:
    return repo.key(Path(directory or os.getcwd()).resolve())


def cmd_habits(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        if args.habits_command == "install":
            where = Path(args.repo or os.getcwd())
            if not where.is_dir():
                # A habit writes the folder's own settings: a folder that is not there is a typo,
                # never one to make (it wrote `.claude/settings.local.json` into a new directory).
                print(f"locks: {where} is not a directory; install a habit in an existing repository",
                      file=sys.stderr)
                return 2
            # What `list` showed here, or a habit of several repositories offered to this one.
            found = {c.id: c for c in _candidates(store, args.days)}
            key = _habit_repo(args.repo)
            if key:
                here, _ = habits.for_repo(store.tool_events(days=args.days), key)
                found.update({c.id: c for c in here})
            if args.candidate not in found:
                print(f"no candidate {args.candidate} passes the gates now; see `locks habits`", file=sys.stderr)
                return 1
            try:
                habit_id, new = habits.install(store, found[args.candidate], command=args.command,
                                               repo_dir=Path(args.repo or os.getcwd()).resolve())
            except habits.HabitError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            if not new:
                print(f"already installed here as habit #{habit_id}: "
                      f"`{args.command}` after {found[args.candidate].steps[0]}")
                return 0
            print(f"installed habit #{habit_id}: after {found[args.candidate].steps[0]}, run `{args.command}`. "
                  f"`locks habits uninstall {habit_id}` removes it.")
            return 0
        if args.habits_command == "review":
            directory = str(Path(args.repo or os.getcwd()).resolve())
            key = _habit_repo(args.repo)
            if key is None:
                print("not in a repository", file=sys.stderr)
                return 1
            project.enroll(directory)
            task = habits.queue_review(store, repo=key, cwd=directory, session_id=os.environ.get("LOCKS_SESSION_ID"),
                                       kick=lambda d, k: dispatch.start(Path(d), home(), k) or dispatch.held(home(), k))
            if task is None:
                print("no habit here that a review has not seen; `locks habits` lists them")
                return 0
            print(f"queued #{task['id']}: {task['title']}")
            return 0
        if args.habits_command == "uninstall":
            if not habits.uninstall(store, args.habit_id):
                print(f"no habit #{args.habit_id}", file=sys.stderr)
                return 1
            print(f"removed habit #{args.habit_id}")
            return 0
        if args.habits_command == "status":
            installed = [{**h, "steps": json.loads(h["steps"]),
                          "repo": str(Path(h["settings_path"]).parent.parent)} for h in store.habits()]
            if args.json:
                print(json.dumps(installed, ensure_ascii=False))
                return 0
            for h in installed:
                state = f"off since {h['disabled_at']} after {h['failures']} breaks" if h["disabled_at"] else "on"
                print(f"#{h['id']} {' → '.join(h['steps'])}: `{h['command']}` ({h['runs']} runs, "
                      f"{h.get('caught') or 0} found something) {state}  in {h['repo']}")
            return 0
        key = None if args.all else _habit_repo(args.repo)
        if key is None:
            here, elsewhere = _candidates(store, args.days), []
        else:
            habits.load_events(store)
            here, elsewhere = habits.for_repo(store.tool_events(days=args.days), key,
                                              Path(args.repo or os.getcwd()).resolve())
        if args.json:
            print(json.dumps({"repo": key, "here": [c.data() for c in here[: args.limit]],
                              "elsewhere": [c.data() for c in elsewhere[: args.limit]]}))
            return 0
        if key is None:
            print("every repository:")
        else:
            print(f"in {key}:")
        if not here:
            print("  no repeated sequence passes the gates here yet")
        for c in here[: args.limit]:
            print(f"  {c.line()}")
        if elsewhere:
            print(f"in {habits.MIN_REPOS_ELSEWHERE} or more other repositories, not here yet "
                  "(install one only when this repository has its tool):")
            for c in elsewhere[: args.limit]:
                print(f"  {c.line()}")
        return 0
    finally:
        store.close()


def cmd_habit_run(args: argparse.Namespace) -> int:
    """The body of an installed habit's hook. Fails open; a check that failed is
    said to the session (exit 2, the output's end on stderr), a pass says nothing."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
        store = Store.open()
        try:
            said = habits.run_habit(store, args.habit_id, event)
        finally:
            store.close()
    except Exception:  # a hook never takes the session down, and says when it failed
        from locks import signature

        signature.note_error(os.environ)
        return 0
    if said:
        # PostToolUse exit 2: the edit stands, and the session reads why the check failed.
        print(said, file=sys.stderr)
        return 2
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from locks.ui import server

    try:
        ui = server.serve(args.port)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        found = server.occupant(args.port)
        # An older locks UI holds it: it serves that version's board, so this one takes its place.
        stopped = server.take_over(args.port) if server.older(found) else None
        if stopped is None:
            return _ui_port_taken(args)
        print(f"stopped the locks UI {found.get('version') or 'from before 0.4.1'} that held port {args.port} "
              f"(pid {stopped}): it served that version's board", flush=True)
        ui = server.serve(args.port)
    url = f"http://127.0.0.1:{ui.server_address[1]}/"
    idle = getattr(args, "idle_exit", None)
    print(f"locks UI at {url} (Ctrl-C stops it"
          + (f"; it stops {idle:g}s after its last tab closes" if idle else "") + ")", flush=True)
    if idle:
        import threading

        threading.Thread(target=ui.exit_when_idle, args=(idle,), daemon=True).start()
    sessions = _tabs_sessions(args)
    if args.open and not sessions:
        import webbrowser

        webbrowser.open(url)
    for session in sessions:
        # One tab per session, which is the board narrowed to its work: opened with --open, else
        # only claimed (a server handed over to a newer locks holds the claims; the tabs are open).
        if ui.claim_tab(session) and args.open:
            _open_session_tab(url, session)
    try:
        ui.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ui.stop_event.set()
        ui.server_close()
    return 0


def _tabs_sessions(args: argparse.Namespace) -> list[str]:
    """The sessions that get their one tab: `--session`, else the one this command was typed in."""
    if not args.tabs:
        return []
    sessions = args.session or [s for s in [os.environ.get("LOCKS_SESSION_ID")] if s]
    if not sessions:
        print("--tabs needs a session: run it in one, or pass --session <id>", file=sys.stderr)
    return sessions


def _open_session_tab(url: str, session: str) -> None:
    import webbrowser

    from locks.ui import api

    tab = api.session_tab(session)
    webbrowser.open_new_tab(f"{url}#{tab['fragment']}")
    print(f"  tab: {tab['title']} (its work shows inside it; no more tabs open for it)", flush=True)


def _ui_port_taken(args: argparse.Namespace) -> int:
    """The port is taken. By this same locks: it is the board, say where. By an
    older one: it shows what that version showed, and it is the person's to stop."""
    from locks.ui import server

    url = f"http://127.0.0.1:{args.port}/"
    found = server.occupant(args.port)
    if found and found["locks"] and found["version"] == __version__:
        print(f"locks UI {__version__} is already at {url}", flush=True)
        if args.open:
            import webbrowser

            sessions = _tabs_sessions(args)
            if not sessions:
                webbrowser.open(url)
            for session in sessions:
                claimed = server.claim_tab_there(args.port, session)
                if claimed:
                    _open_session_tab(url, session)
                elif claimed is None:
                    print("that server could not be asked for this session's tab, so none was opened",
                          file=sys.stderr)
                else:
                    print(f"session {session[:8]} has its tab already: it shows all its work", flush=True)
        return 0
    if found and found["locks"]:
        print(f"port {args.port} is held by a locks UI {found['version'] or 'from before 0.4.1'}, started before "
              f"locks {__version__} was installed: it still serves that version's board. Stop it (end the "
              f"session or background command that started it), then run this again; or use "
              f"`locks ui --port <another>`.", file=sys.stderr)
        return 1
    print(f"port {args.port} is taken by another program; use `locks ui --port <another>`", file=sys.stderr)
    return 1


def cmd_acp(args: argparse.Namespace) -> int:
    from locks import acp

    # stdout is the protocol's alone: whatever prints there by accident goes to stderr.
    protocol, sys.stdout = sys.stdout, sys.stderr
    sys.stdin.reconfigure(encoding="utf-8")
    protocol.reconfigure(encoding="utf-8")
    return acp.serve(sys.stdin, protocol)


def cmd_link(args: argparse.Namespace) -> int:
    from locks import link

    target_dir = Path(args.dir).expanduser() if args.dir else link.default_dir(os.environ)
    if args.remove:
        gone = link.remove(target_dir)
        print(f"removed {gone}" if gone else f"no `locks link` shim in {target_dir}")
        return 0
    launcher = Path(__file__).resolve().parents[2] / "bin" / "locks"
    try:
        target = link.write(target_dir, launcher)
    except FileExistsError as exc:
        print(f"locks: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {target}: it runs the newest locks Claude Code has installed")
    if old := link.write_old(target_dir, launcher):
        print(f"rewrote {old}: the old command runs the newest locks too")
    if not link.on_path(target_dir, os.environ):
        print(f"{target_dir} is not on PATH; add it to your shell profile: export PATH=\"{target_dir}:$PATH\"")
    return 0


def _notice_deaths(store: Store) -> None:
    """A worker whose process is gone is noticed by whoever looks next, not only at a turn's end:
    the mod's status line asks for the board every few seconds, so a death that came while the
    orchestrating session waited is swept (and resumed, `dispatch.recover`) within that poll.
    Only when one is gone: the poll stays a read otherwise. A command, never a GET."""
    from locks import dispatch  # light: it imports the orchestrator only when one is gone

    try:
        running = store.list_tasks(status=["running"], limit=500, source=("locks",))
        if any(t["pid"] and not dispatch._alive(t["pid"]) for t in running):
            from locks.store import home as locks_home

            dispatch.recover(store, locks_home(), os.environ)
    except Exception:  # noqa: S110 - looking never fails the command that looks
        pass


def cmd_board(args: argparse.Namespace) -> int:
    """The board as typed JSON: a status line or a mod reads fields, never a sentence.

    `--json` prints the counts; `--full` the whole board. `--repo DIR` (repeatable)
    keeps only those repositories, and the output names the key each directory has.
    """
    from locks.ui import api

    keys = {d: repo.key(Path(d).resolve()) for d in args.repo or []}
    store = Store.open()
    try:
        _notice_deaths(store)
        board = api.board(store, repos=set(keys.values()) if args.repo else None, session=args.session)
    finally:
        store.close()
    if args.full:
        print(json.dumps({**board, "repos": keys}, ensure_ascii=False, default=str))
    elif args.json:
        print(json.dumps(board["counts"]))
    else:
        c = board["counts"]
        print(f"locks ◆{c['needs_you'] + c['orchestrator']} ▶{c['running']} ⏸{c['queued']}")
    return 0


def cmd_endings(args: argparse.Namespace) -> int:
    """What ended since a session last heard: the same notice the Stop hook and the
    next prompt deliver. `--peek` reads it without taking it."""
    store = Store.open()
    try:
        if not args.peek:
            # A mechanical stop is resumed before it could wake the session (`dispatch.recover`).
            with contextlib.suppress(Exception):
                dispatch.recover(store, home(), os.environ)
        text, ids = hooks.endings(store, args.session, claim=None if args.peek else args.via)
        copied, copied_ids = hooks.copies(store, args.session, claim=None if args.peek else args.via)
        if copied:
            text, ids = (f"{text}\n\n{copied}" if text else copied), [*ids, *copied_ids]
        statuses = {str(i): (store.get_task(i) or {}).get("status") for i in ids}
        pulse = str(store.pulse_path)
    finally:
        store.close()
    if args.json:
        print(json.dumps({"notice": text, "ids": ids, "statuses": statuses, "pulse": pulse}, ensure_ascii=False))
    elif text:
        print(text)
    return 0


#: The longest a `limit-wait` sleeps: a week's limit resets within it.
LIMIT_WAIT_MAX_S = 8 * 24 * 3600


def cmd_limit_wait(args: argparse.Namespace, *, sleep: Callable[[float], None] = time.sleep) -> int:
    """Sleep until the account's usage limit resets, then resume what it stopped and start
    the dispatchers whose queue it held (`dispatch.recover`, `dispatch.revive`): started
    detached by `recover`, so the work goes on after the reset with nobody typing."""
    wait = min(max(float(args.until) - time.time(), 0.0), LIMIT_WAIT_MAX_S)
    if wait:
        sleep(wait)
    store = Store.open()
    try:
        resumed = dispatch.recover(store, home(), os.environ)
        revived = dispatch.revive(store, home(), os.environ)
    finally:
        store.close()
    print(f"locks limit-wait: resumed {', '.join(f'#{n}' for n in resumed) or 'nothing'}; started "
          f"{len(revived)} dispatcher(s)")
    return 0


def cmd_compaction(args: argparse.Namespace) -> int:
    """The mod's side of `locks.compaction`: what to do now that the session is
    idle (`next`), or what became of a decision it was to submit (`delivered`)."""
    from locks import compaction

    mode = config.choice("compact", os.environ)
    if args.compaction_command == "request" and (os.environ.get("LOCKS_WORKER_TASK") or mode == "off"):
        why = ("a locks worker never compacts" if os.environ.get("LOCKS_WORKER_TASK")
               else "the person turned locks' compaction off (`locks config compact`)")
        print(json.dumps({"error": why}) if args.json else f"locks: {why}", file=None if args.json else sys.stderr)
        return 0 if args.json else 1
    if os.environ.get("LOCKS_WORKER_TASK") or (args.compaction_command == "next" and mode == "off"):
        # A worker's session never compacts on locks' word, and with the setting
        # off there is nothing to take: neither opens the store.
        if args.json:
            print(json.dumps({"action": None}))
        return 0
    store = Store.open()
    try:
        answer: dict[str, object]
        if args.compaction_command == "delivered" and (args.ran or args.skipped is not None):
            answer = compaction.compacted(store, home(), args.session, args.decision, version=args.version,
                                          skip=args.skipped)
        elif args.compaction_command == "request":
            answer = compaction.request(store, home(), session_id=args.session, cwd=args.cwd, why=args.why or "",
                                        keep=args.keep or "", then=args.then or "")
        elif args.compaction_command == "delivered":
            why = (f"the engine rejected it: {args.rejected}" if args.rejected is not None
                   else "a turn started before it could be submitted")
            answer = {"kept": compaction.delivered(store, args.session, args.decision, how="suggest", why=why)}
        else:
            answer = compaction.next_step(store, home(), args.session, version=args.version, mode=mode)
    finally:
        store.close()
    if args.json:
        print(json.dumps(answer, ensure_ascii=False))
    else:
        for key in ("said", "line", "advice", "text"):
            if answer.get(key):
                print(answer[key])
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    """`locks trace`: the traced sessions, or what one session's trace shows."""
    from locks import trace

    on = trace.enabled(os.environ)
    if not args.session:
        rows = trace.sessions(os.environ)[: args.limit]
        if args.json:
            print(json.dumps({"enabled": on, "sessions": rows}, ensure_ascii=False))
            return 0
        if not on:
            print("tracing is off: locks config trace on (or the plugin's option) turns it on")
        if not rows:
            print("no session traced yet")
        for r in rows:
            print(f"{r['updated']}  {r['session_id']}  {r['bytes']} bytes"
                  + ("  +transcript" if r["transcript"] else ""))
        return 0
    if args.raw:
        for record in trace.records(os.environ, args.session):
            if not args.tag or str(record.get("tag", "")).startswith(args.tag):
                print(json.dumps(record, ensure_ascii=False))
        return 0
    info = trace.summary(os.environ, args.session)
    if args.json:
        print(json.dumps(info, ensure_ascii=False))
        return 0
    print(f"session {info['session_id']}: {info['records']} records"
          + (f", locks {', '.join(info['versions'])}" if info["versions"] else ""))
    launch = info["launch"]
    if launch is None:
        print("launch: not traced (a session traced before 0.92.0, or its SessionStart was not)")
    elif launch.get("read") is False:
        print("launch: its claude process could not be read; the hooks ran as for a person")
    else:
        flags = [f for f, on in (("-p", launch.get("print")), ("--json-schema", launch.get("json_schema"))) if on]
        flags += [f"--{k.replace('_', '-')} {launch[k]}" for k in ("output_format", "input_format") if launch.get(k)]
        tty = {True: "a terminal", False: "no terminal"}.get(launch.get("tty"), "input unknown")
        who = (f"a program's: {launch['scripted']}, so the hooks gave it nothing" if launch.get("scripted")
               else "a person's")
        print(f"launch: claude {' '.join(flags) or '(interactive)'}, {tty}; {who}")
    print("what the hooks did beyond injecting:" + ("" if info["actions"] else " nothing recorded"))
    for a in info["actions"]:
        rest = {k: v for k, v in a.items() if k not in ("ts", "action")}
        print(f"  {a.get('ts')}  {a['action']}  {clip(json.dumps(rest, ensure_ascii=False), 160)}")
    for title, key in (("by tag", "by_tag"), ("tools called", "tools"), ("livespec tools called", "livespec_calls"),
                       ("locks events", "locks_events")):
        print(f"{title}:" + ("" if info[key] else " none"))
        for name, count in info[key].items():
            print(f"  {count:>5}  {name}")
    print(f"context injected by hooks: {info['injected']}")
    print(f"livespec index events: {len(info['livespec_events'])}")
    for e in info["livespec_events"]:
        print(f"  task {e.get('task_id')}: {clip(json.dumps(e, ensure_ascii=False), 200)}")
    print(f"briefs that carried notes: {len(info['notes_briefed'])}")
    print("tokens:" + ("" if info["tokens"] else " no transcript kept yet"))
    for model, t in info["tokens"].items():
        print(f"  {model}: in {t['input']}, out {t['output']}, cache read {t['cache_read']}, "
              f"cache write {t['cache_write']} over {t['messages']} messages")
    return 0


def _store_copy(args: argparse.Namespace):
    """A read-only connection to a copy of the store: the one passed with --db, else a fresh
    backup of the live file (`forensics.snapshot`)."""
    from locks import forensics

    path = Path(args.db) if args.db else forensics.snapshot(legacy.database(home()))
    return forensics.open_copy(path), path


def cmd_forensics(args: argparse.Namespace) -> int:
    """`locks forensics`: where money, the cache and friction went, observed from a copy of the store."""
    from locks import forensics

    conn, path = _store_copy(args)
    try:
        info = forensics.report(conn, os.environ, days=args.days)
    finally:
        conn.close()
    info["db"] = str(path)
    if args.json:
        print(json.dumps(info, ensure_ascii=False))
        return 0
    s = info["spend"]
    print(f"OBSERVED over {args.days} days, read from a copy ({path})")
    share = f" ({s['unpassed_share']:.0%})" if s["unpassed_share"] is not None else ""
    print(f"spend: ${s['usd']:.2f}, of it ${s['unpassed_usd']:.2f} on attempts that did not pass{share}")
    for c in s["cells"][:args.limit]:
        print(f"  {c['kind']:<14} {c['cell']:<16} {c['attempts']:>4} attempts {c['passed']:>4} passed "
              f"${c['usd']:.2f} (unpassed ${c['unpassed_usd']:.2f})")
    print("cache, by model:" + ("" if info["cache"] else " no usage recorded")
          + (" (from the workers' attempts; a session's own usage needs its transcript)"
             if info["cache"] and info["cache"][0].get("source") == "attempts" else ""))
    for m in info["cache"]:
        read = f"{m['read_share']:.0%}" if m["read_share"] is not None else "n/a"
        unit = "attempts" if m.get("source") == "attempts" else "messages"
        print(f"  {m['model']}: {read} of input read from the cache over {m['messages']} {unit}")
    f = info["friction"]
    print(f"tool friction: {f['failed']} of {f['calls']} calls failed, {f['repeated']} repeated back to back")
    for t in f["tools"][:args.limit]:
        if t["failed"] or t["repeated"]:
            print(f"  {t['tool']}: {t['failed']} failed, {t['repeated']} repeated of {t['calls']}")
    for kind, row in info["footprint"].items():
        for g in row["grew"]:
            print(f"brief footprint: {kind} grew {g['ratio']}x from {g['from']} to {g['to']}")
    tr = info["trace"]
    if tr["sessions"]:
        print(f"trace: {tr['sessions']} sessions, {tr['injected']} injections, livespec calls "
              + (", ".join(f"{k} {v}" for k, v in tr["livespec_calls"].items()) or "none"))
    for place, arms in info["notes_fork"]["projects"].items():
        print(f"notes fork trial {place}: " + ", ".join(
            f"{a} {v.get('filings', 0)} filings, {v.get('kept', 0)} kept, ${v.get('usd', 0):.2f}"
            for a, v in arms.items()))
    return 0


def cmd_footprint(args: argparse.Namespace) -> int:
    """`locks footprint`: the median brief per kind and release, and the releases that grew it."""
    from locks import footprint

    conn, _path = _store_copy(args)
    try:
        info = footprint.report(conn, days=args.days)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(info, ensure_ascii=False))
        return 0
    if not info:
        print("no brief recorded its footprint yet")
    for kind, row in info.items():
        print(kind)
        for r in row["releases"]:
            print(f"  {r['version']:<10} {r['tasks']:>4} tasks  context {r['context_chars']:>6}  "
                  f"system {r['system_chars']:>6} chars")
        for g in row["grew"]:
            print(f"  grew {g['ratio']}x from {g['from']} to {g['to']}")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    """`locks skills`: how the skills roles carry are used, and their state; --pin keeps one active."""
    from locks import skill_use

    if args.pin or args.unpin:
        held = skill_use.pin(home(), args.pin or args.unpin, on=bool(args.pin))
        print("pinned: " + (", ".join(sorted(held)) or "none"))
        return 0
    conn, _path = _store_copy(args)
    try:
        rows = skill_use.report(conn, home())
    finally:
        conn.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print("no brief has carried a skill yet")
    for r in rows:
        rate = f"{r['pass_first']:.0%}" if r["pass_first"] is not None else "n/a"
        print(f"{r['state']:<8} {r['skill']:<28} {r['briefs']:>4} briefs  pass-first {rate:>4}  "
              f"last {r['last'] or 'never'}  ({r['source']})")
    dormant = [r["skill"] for r in rows if r["state"] == "dormant"]
    if dormant:
        print(f"{len(dormant)} dormant: carried by no brief in {skill_use.ARCHIVE_DAYS} days; locks moves "
              "nothing, a role's skills are changed by a person")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    return hooks.main(args.event, sys.stdin, sys.stdout, os.environ)


ALLOW_HELP = ("a permission rule the workers get, e.g. 'Bash(npm run build:*)' or 'Bash(node:*)', and "
              "'Read(//abs/path)' with two slashes for an absolute path; repeatable. A one-shot worker is "
              "refused whatever its settings do not allow; a blocked task's report prints the rules it needs")
AFTER_HELP = ("queue it held until task N (or task:N) or workflow run run:N passes, then start it on the branch "
              "that work kept (on the base when that branch is merged); nothing is merged or rebased")
NOTIFY_HELP = ("a session id that hears a copy of this work's ending beside the session that sends it "
               "(a supervising session); repeatable")
WHOLE_HELP = ("run a build that lists many items, or the largest size, as one task: without it, locks asks for it "
              "split (exit 5) and starts nothing")


def _after_ref(value: str) -> dict[str, int]:
    try:
        return after.ref(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="locks", description="route work to the right model, effort and tools")
    parser.add_argument("--version", action="version", version=f"locks {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def run_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("text", help="the task; - reads it from stdin")
        p.add_argument("--repo", help="the repository the work is in (default: the current directory)")
        p.add_argument("--launch-dir", help="start the worker here, and let it work in the repository")
        p.add_argument("--max-attempts", type=int, default=6)
        p.add_argument("--max-turns", type=int,
                       help="turns per attempt (default: 30; a feature, 60); raised once, then again while "
                            "the work moves")
        p.add_argument("--verify", help="command whose exit code decides a claimed pass")
        p.add_argument("--kind", type=_kind, metavar="KIND",
                       help=f"skip classification: {', '.join(KINDS)}")
        p.add_argument("--start", help="pin the first cell, e.g. sonnet/high", type=_cell)
        p.add_argument("--role", help="run as this role (`locks roles list`), on the kinds it takes")
        p.add_argument("--surface", action="append", metavar="PATH",
                       help="a path or glob the task may change (repeatable); a change outside is undone")
        p.add_argument("--allow-approval", action="store_true", help="allow cells that need approval (Fable)")
        p.add_argument("--allow", action="append", metavar="RULE", help=ALLOW_HELP)
        p.add_argument("--no-isolate", action="store_true", help="work in the checkout, not a worktree")
        p.add_argument("--carry", action="store_true",
                       help="bring your checkout's uncommitted changes into the task's worktree")
        p.add_argument("--add-dir", action="append", metavar="DIR",
                       help="a directory beside the work the workers may use, e.g. a sibling repository (repeatable)")
        p.add_argument("--service", action="append", metavar="CMD@PORT", type=_service,
                       help="a server the task needs up, started before the work and stopped after: "
                            "'npm run dev@4200' (repeatable)")
        p.add_argument("--no-model", action="store_true", help="classify with the rules only")
        switch = p.add_mutually_exclusive_group()
        switch.add_argument("--livespec", dest="livespec", action="store_true", default=None,
                            help="use livespec for this task even if the setting is off")
        switch.add_argument("--no-livespec", dest="livespec", action="store_false",
                            help="leave livespec out of this task")

    p = sub.add_parser("run", help="run a task: classify, attempt, escalate, remember")
    run_args(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--after", type=_after_ref, metavar="N", help=AFTER_HELP)
    p.add_argument("--detach", action="store_true",
                   help="queue it and start its lane's dispatcher instead of running it here; prints its "
                        "number, and its ending reaches the session that sent it")
    p.add_argument("--whole", action="store_true", help=WHOLE_HELP)
    p.add_argument("--notify", action="append", metavar="SESSION", help=NOTIFY_HELP)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("resume", help="run a task that stopped again: from its kept work and the cell it stopped at")
    p.add_argument("id", type=int)
    p.add_argument("instruction", nargs="?", metavar="-",
                   help="`-` reads what to add for this resume from stdin: quoted in its brief as context")
    p.add_argument("--allow", action="append", metavar="RULE", help=ALLOW_HELP)
    p.add_argument("--verify", help="command whose exit code decides a claimed pass (default: the task's own)")
    p.add_argument("--start", help="the cell to resume at (default: where it stopped)", type=_cell)
    p.add_argument("--surface", action="append", metavar="PATH",
                   help="add a path or glob the task may change (repeatable): a task that stopped on its "
                        "surfaces resumes with them, its diagnosis kept")
    p.add_argument("--allow-approval", action="store_true", help="allow cells that need approval (Fable)")
    p.add_argument("--no-isolate", action="store_true",
                   help="work in the checkout (a task started with --no-isolate already resumes there)")
    p.add_argument("--max-turns", type=int,
                   help="turns per attempt; a task that outgrew its turns resumes with 120 by default")
    p.add_argument("--no-model", action="store_true")
    p.add_argument("--detach", action="store_true", help="run it in the background; its ending reaches the session")
    p.add_argument("--pressed", action="store_true",
                   help="a person's press on another page: run the resume locks' account of the stop names")
    p.add_argument("--keep", action="store_true",
                   help="also keep the --allow rules for every task in the task's repository (locks allow)")
    p.add_argument("--unattended", action="store_true",
                   help="refuse (exit 3) when only a person can clear what stopped it: a refusal, an approval")
    p.add_argument("--no-report", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("route", help="show where a task would start and how it would climb")
    run_args(p)
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("classify", help="the kind of work a request is")
    p.add_argument("text")
    p.add_argument("--no-model", action="store_true")
    p.set_defaults(func=cmd_classify)

    sub.add_parser("matrix", help="every kind and its ladder").set_defaults(func=cmd_matrix)

    p = sub.add_parser("tasks", help="recent tasks of this repository (--all: every project's)")
    p.add_argument("--repo")
    p.add_argument("--all", action="store_true", help="every project's tasks, each row naming its project")
    p.add_argument("--status", action="append")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--turns", action="store_true", help="the sessions' own turns and subagents too")
    p.add_argument("--workers", action="store_true",
                   help="only work a worker ran (its attempts and cost on each row), by project with --all")
    p.set_defaults(func=cmd_tasks)

    queue = sub.add_parser("queue", help="tasks waiting for a worker; `++ task` in a session queues too")
    qsub = queue.add_subparsers(dest="queue_command")
    queue.set_defaults(func=cmd_queue, queue_command="list", repo=None, all=False)
    p = qsub.add_parser("add", help="queue a task for this repository's lane")
    p.add_argument("text")
    p.add_argument("--repo")
    p.add_argument("--verify")
    p.add_argument("--kind", type=_kind, metavar="KIND")
    p.add_argument("--start", type=_cell)
    p.add_argument("--allow", action="append", metavar="RULE", help=ALLOW_HELP)
    p.add_argument("--surface", action="append", metavar="PATH",
                   help="a path or glob the task may change (repeatable); tasks with surfaces that cannot touch "
                        "run side by side")
    p.add_argument("--carry", action="store_true",
                   help="bring your checkout's uncommitted changes into the task's worktree")
    p.add_argument("--add-dir", action="append", metavar="DIR",
                   help="a directory beside the work the workers may use (repeatable)")
    p.add_argument("--service", action="append", metavar="CMD@PORT", type=_service,
                   help="a server the task needs up, started before the work and stopped after (repeatable)")
    p.add_argument("--after", type=_after_ref, metavar="N", help=AFTER_HELP)
    p.add_argument("--whole", action="store_true", help=WHOLE_HELP)
    p.add_argument("--notify", action="append", metavar="SESSION", help=NOTIFY_HELP)
    p.add_argument("--json", action="store_true", help="print the queued task as JSON")
    p.set_defaults(func=cmd_queue)
    p = qsub.add_parser("list", help="what is waiting (this repository; --all for every one)")
    p.add_argument("--repo")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_queue)
    p = qsub.add_parser("rm", help="take a task off the queue")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_queue)

    from locks import investigate, loop_cli, team_cli, workflow_cli

    workflow_cli.add_parser(sub)
    loop_cli.add_parser(sub)
    team_cli.add_parser(sub)
    investigate.add_parser(sub)

    p = sub.add_parser("lanes", help="one serial lane per repository; a failed task pauses it")
    p.add_argument("--unpause", metavar="REPO", help="a repository path or key")
    p.add_argument("--beside", metavar="N", type=int,
                   help="let queued task N start beside the work ahead: the session's word that it is independent")
    p.set_defaults(func=cmd_lanes)

    p = sub.add_parser("work", help="run queued tasks in workers, lane by lane")
    p.add_argument("--repo")
    p.add_argument("--all", action="store_true", help="every repository's lane")
    p.add_argument("--max", type=int, default=None, help="stop after this many tasks (default: the dispatcher's)")
    p.add_argument("--no-model", action="store_true")
    p.add_argument("--detached", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_work)

    p = sub.add_parser("init", help="enroll a project: its .locks/ folder, so it and its sessions show in the UI")
    p.add_argument("dir", nargs="?", help="a directory in the project (default: here)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("survey", help="what every Claude Code session on this machine and its repository's "
                                       "history say about how you work; read only, no model. /locks:init builds "
                                       "your team, workflows and memory from it")
    p.add_argument("--days", type=int, default=180, help="sessions touched in the last N days (default 180)")
    p.add_argument("--sessions", type=int, default=400, help="the most sessions read, newest first (default 400)")
    p.add_argument("--per-project", type=int, default=60, help="the most sessions read per project (default 60)")
    p.add_argument("--repo", help="only this project")
    p.add_argument("--deep", action="store_true", help="every error reported and what followed it, every branch, "
                                                       "the chains of fixes and reverts, locks' stops")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_survey)
    p = sub.add_parser("stats", help="where locks' time and money go: runs by kind and cell, their phases, the "
                       "session's thread in briefs, and the session's own turns")
    p.add_argument("--days", type=int, default=30, help="the last N days (0: everything)")
    p.add_argument("--repo", help="only this repository (default: every one)")
    p.add_argument("--json", action="store_true", help="everything, as JSON")
    p.add_argument("--runs", action="store_true", help="one record per run instead of the summary (JSON)")
    p.add_argument("--attempts", action="store_true", help="one record per attempt with how its worker worked "
                   "(its tool calls: before the first write, checks, fix loops, rereads, repeats, gaps), as JSON")
    p.add_argument("--csv", action="store_true", help="with --runs or --attempts: CSV, for a spreadsheet")
    p.add_argument("--expensive", action="store_true", help="the runs that cost the most for their kind, and why")
    p.set_defaults(func=cmd_stats)
    p = sub.add_parser("trust", help="what the independent check's record says of each cell: clean checks, where "
                       "its sampling stands, repeated blockers, re-checks that found something new, and pass-first "
                       "and cost per point by the cell work started on")
    p.add_argument("--repo", help="the repository (default: here)")
    p.add_argument("--all", action="store_true", help="every repository")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_trust)
    p = sub.add_parser("cells", help="per kind of work, the model and effort locks starts it on here and how "
                       "each cell's first tries went: which models fit which work, measured")
    p.add_argument("--repo", help="the repository (default: here)")
    p.add_argument("--all", action="store_true", help="every repository")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_cells)
    p = sub.add_parser("doctor", help="states the store should never hold: work stuck now (broken) and what went "
                       "wrong over the last days (suspect); exits 1 when something is broken")
    p.add_argument("--days", type=int, default=2, help="how far back the suspect rules read (0: everything)")
    p.add_argument("--repo", help="only this repository (default: every one)")
    p.add_argument("--json", action="store_true", help="the findings as JSON")
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("evolution", help="how locks evolves and why: each metric's trend, and every change of a "
                                         "standard with its reason and verdict")
    p.add_argument("--days", type=int, default=30, help="the window (default 30; 0 for all time)")
    p.add_argument("--repo", help="one repository (default: every one)")
    p.add_argument("--json", action="store_true", help="the same, as data")
    p.add_argument("--snapshot", action="store_true", help="take a snapshot now, from where the last one ended")
    p.set_defaults(func=cmd_evolution)
    p = sub.add_parser("relay", help="hand this session's thread to a fresh one: the next session that starts "
                       "here (after /clear) begins from its requests verbatim, what landed and what is open")
    p.add_argument("--session", help="the session to hand over (default: the one this runs in)")
    p.add_argument("--show", action="store_true", help="also print what the fresh session will be told")
    p.set_defaults(func=cmd_relay)
    p = sub.add_parser("orchestrator", help="which session orchestrates this project; --claim makes it this one")
    p.add_argument("--repo", help="the project (default: here)")
    p.add_argument("--claim", action="store_true", help="this session takes the role, and the work the last one sent")
    p.add_argument("--release", action="store_true", help="drop the role")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_orchestrator)
    p = sub.add_parser("index-livespec", help=argparse.SUPPRESS)
    p.add_argument("dir")
    p.set_defaults(func=cmd_index_livespec)

    p = sub.add_parser("name-session", help=argparse.SUPPRESS)
    p.add_argument("id")
    p.set_defaults(func=cmd_name_session)

    p = sub.add_parser("run-queued", help=argparse.SUPPRESS)
    p.add_argument("id", type=int)
    p.add_argument("--no-model", action="store_true")
    p.set_defaults(func=cmd_run_queued)

    p = sub.add_parser("cancel", help="stop a running or queued task")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("dismiss", help="take tasks that wait on you off the board (a resume brings one back)",
                       description="locks dismiss N [N ...] --why TEXT: the reason is kept on the task's "
                                   "account; words after the ids are read as the reason too.")
    p.add_argument("ids", nargs="+", metavar="ID [WHY ...]",
                   help="the tasks' numbers, then optionally the reason in words")
    p.add_argument("--why", "--reason", dest="why", default="", help="why it is dismissed, kept on its account")
    p.add_argument("--via", choices=sorted(stops.DISMISS_VIA), default="cli", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_dismiss)

    p = sub.add_parser("events", help="what runs are doing, as they do it")
    p.add_argument("--task", type=int)
    p.add_argument("--after", type=int, default=0, help="only events after this id")
    p.add_argument("--follow", "-f", action="store_true", help="keep printing new events")
    p.add_argument("--kind", action="append", help="only events of this kind, e.g. finished (repeatable)")
    p.add_argument("--new", action="store_true", help="only events from now on (with --follow, a watch)")
    p.add_argument("--repo", action="append", help="only tasks of this repository (repeatable)")
    p.add_argument("--last-id", action="store_true", help="print the newest event id: where a watcher starts")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("peek", help="what a running task's worker wrote so far: its worktree's diff against its base")
    p.add_argument("id", type=int)
    p.add_argument("--stat", action="store_true", help="only the files and their lines")
    p.set_defaults(func=cmd_peek)

    p = sub.add_parser("show", help="one task: its attempts and its messages")
    p.add_argument("id", type=int)
    p.add_argument("--json", action="store_true", help="everything the UI's task view shows, as JSON")
    p.set_defaults(func=cmd_show)

    mem = sub.add_parser("memory", help="problems and the fixes tried against them").add_subparsers(
        dest="memory_command", required=True)
    p = mem.add_parser("search", help="search every repository, this one first")
    p.add_argument("text")
    p.add_argument("--repo")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_memory_search)
    p = mem.add_parser("list", help="problems and their fixes, latest first or matching --query")
    p.add_argument("--query")
    p.add_argument("--repo", action="append", help="only this repository (repeatable)")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_memory_list)
    p = mem.add_parser("record", help="record a fix and whether it worked")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--problem", help="the problem's title (opens it if new)")
    group.add_argument("--problem-id", type=int)
    p.add_argument("--fix", required=True, help="what was changed")
    p.add_argument("--outcome", required=True, choices=FIX_OUTCOMES)
    p.add_argument("--why", help="why it failed, if it did")
    p.add_argument("--evidence", help="error text or test output")
    p.add_argument("--symptom")
    p.add_argument("--commit", help="the commit the verdict is about")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_memory_record)
    p = mem.add_parser("invalidate", help="a fix believed to work, shown wrong")
    p.add_argument("fix_id", type=int)
    p.add_argument("--why", required=True)
    p.set_defaults(func=cmd_memory_invalidate)
    p = mem.add_parser("forget", help="problems that were never the work's (the environment, a clash): "
                                      "they and their fixes leave memory")
    p.add_argument("problem_ids", type=int, nargs="+")
    p.set_defaults(func=cmd_memory_forget)

    p = sub.add_parser("note", help="keep a fact about this project in its notes: "
                                     "locks note \"<fact>\" --topic business|code|decisions|conventions|environment")
    p.add_argument("text", help="the fact; - reads it from stdin")
    p.add_argument("--topic", help="where it is filed (default: the topic its words point to)")
    p.add_argument("--title")
    p.add_argument("--anchor", action="append", metavar="PATH",
                   help="a file the fact is about, relative to the checkout's top (repeatable)")
    p.add_argument("--link", action="append", metavar="ID[:KIND]",
                   help="a note it relates to: depends_on (default), explains, replaces, contradicts, example_of")
    p.add_argument("--constraint", action="store_true",
                   help="a rule every change here must meet (a policy, a limit): each worker's brief says it as one")
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("workspace", help="a folder of repositories that is one project: one memory, one board",
                       description="Work that spans two or more repositories is tracked in the folder that holds "
                                   "them: its notes, tasks and board are the folder's. Kept in your locks home.")
    wsub = p.add_subparsers(dest="workspace_command", required=True)
    q = wsub.add_parser("add", help="make a folder a workspace of the repositories in it, and of --with ones "
                                    "(linked in); made when it is not there")
    q.add_argument("folder")
    q.add_argument("--with", dest="with_", action="append", metavar="REPO", help="a repository to link in "
                                                                                 "(repeatable)")
    q = wsub.add_parser("list", help="every workspace and its repositories")
    q.add_argument("--json", action="store_true")
    q = wsub.add_parser("rm", help="forget a workspace: new work in its repositories is theirs again")
    q.add_argument("folder")
    p.set_defaults(func=cmd_workspace)

    p = sub.add_parser("recall", help="what this project's notes say: by topic, by file, one link away")
    p.add_argument("text", nargs="?", help="the question; empty for the latest notes")
    p.add_argument("--topic", action="append", help="only this topic (repeatable)")
    p.add_argument("--path", action="append", help="notes about this file (repeatable)")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--hops", type=int, default=1, help="how many links away to follow (default 1)")
    p.add_argument("--all-states", action="store_true", help="also notes replaced or dropped")
    p.add_argument("--json", action="store_true")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_recall)

    notes_p = sub.add_parser("notes", help="this project's notes: list, show, ok, drop, move, link, topics")
    notes_p.add_argument("--repo")
    notes_p.set_defaults(func=cmd_notes, notes_command=None, topic=None, review=False, all_states=False,
                         limit=100, json=False, constraints=False)
    nsub = notes_p.add_subparsers(dest="notes_command")
    p = nsub.add_parser("list", help="notes, latest first")
    p.add_argument("--topic", action="append")
    p.add_argument("--review", action="store_true", help="only notes whose code changed since they were written")
    p.add_argument("--constraints", action="store_true", help="only the rules every change here must meet")
    p.add_argument("--all-states", action="store_true")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--json", action="store_true")
    p = nsub.add_parser("show", help="one note, its anchors and links")
    p.add_argument("id", type=int)
    p.add_argument("--json", action="store_true")
    p = nsub.add_parser("ok", help="you checked a note to review: it is current again")
    p.add_argument("id", type=int)
    p = nsub.add_parser("drop", help="a note that is wrong or no longer matters")
    p.add_argument("id", type=int)
    p.add_argument("--why")
    p = nsub.add_parser("move", help="file a note under another topic")
    p.add_argument("id", type=int)
    p.add_argument("topic")
    p = nsub.add_parser("link", help="link two notes: locks notes link 12 9 explains")
    p.add_argument("id", type=int)
    p.add_argument("to", type=int)
    p.add_argument("kind", choices=["depends_on", "explains", "replaces", "contradicts", "example_of"])
    p = nsub.add_parser("unlink", help="remove the links between two notes")
    p.add_argument("id", type=int)
    p.add_argument("to", type=int)
    p = nsub.add_parser("topics", help="the topics, how many notes each, and topics Haiku proposed")
    p.add_argument("--json", action="store_true")
    p = nsub.add_parser("topic", help="add a topic to this project: locks notes topic add api \"<what it holds>\"")
    p.add_argument("action", choices=["add"])
    p.add_argument("name")
    p.add_argument("description")

    p = sub.add_parser("extract-notes", help=argparse.SUPPRESS)
    p.add_argument("session")
    p.add_argument("transcript")
    p.add_argument("cwd")
    p.set_defaults(func=cmd_extract_notes)

    p = sub.add_parser("forensics", help="where money, the cache and tool friction went, observed from a copy "
                                         "of the store (never the live file)")
    p.add_argument("--db", help="a copy of the store to read; default: a fresh backup of the live one")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--limit", type=int, default=12)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_forensics)

    p = sub.add_parser("footprint", help="the median brief per kind and release, and the releases that grew it")
    p.add_argument("--db")
    p.add_argument("--days", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_footprint)

    p = sub.add_parser("skills", help="how the skills roles carry are used: briefs, pass-first, state")
    p.add_argument("--db")
    p.add_argument("--pin", metavar="NAME", help="keep this skill active whatever its use")
    p.add_argument("--unpin", metavar="NAME")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("trace", help="the opt-in observability trace: traced sessions, or one session's "
                                     "hooks, tools, livespec calls, notes, injected context and tokens")
    p.add_argument("session", nargs="?", help="a traced session's id")
    p.add_argument("--raw", action="store_true", help="every record, one JSON line each")
    p.add_argument("--tag", help="with --raw, only records whose tag starts with this (hook:, tool:, locks:…)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("spend", help="tokens and dollars by model and by cell")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--repo")
    p.add_argument("--all", action="store_true", help="every repository (the default)")
    p.add_argument("--this", dest="all", action="store_false", help="only this repository")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_spend, all=True)

    p = sub.add_parser("capabilities", help="the MCP servers the core hands to workers")
    p.add_argument("--example", action="store_true")
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("allow", help="rules every worker in this repository gets: locks allow 'Bash(npm:*)', "
                                     "--preset read|node|python, --rm to take back")
    p.add_argument("rules", nargs="*", metavar="RULE")
    p.add_argument("--preset", action="append", help="a named set of rules: read, node, python")
    p.add_argument("--rm", action="store_true", help="take the named rules back (all of them when none is named)")
    p.add_argument("--repo", help="the repository (default: the current directory)")
    p.set_defaults(func=cmd_allow)

    p = sub.add_parser("config", help="show or change a setting: locks config livespec off; "
                                      "locks config compact suggest|auto|off; "
                                      "locks config model sonnet <model id>|default; "
                                      "locks config mode <model|default> <mode>|reset")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.add_argument("extra", nargs="?", help="for `model`: the model id to pin the alias to, or `default`")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("neighbours", help="what locks sees of the neighbours it adopts")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_neighbours)

    hb = sub.add_parser("habits", help="repeated tool sequences, and the hooks a person installs from them")
    hb.set_defaults(func=cmd_habits, habits_command="list", days=30, limit=20, json=False, repo=None, all=False)
    hsub = hb.add_subparsers(dest="habits_command")
    p = hsub.add_parser("list", help="this repository's sequences that pass the gates, best first, and those "
                                     "other repositories share")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--repo", help="the repository (default: the current directory)")
    p.add_argument("--all", action="store_true", help="every repository's events mined together")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_habits)
    p = hsub.add_parser("install", help="run a command after the edit that starts a habit (you approve it here)")
    p.add_argument("candidate")
    p.add_argument("--command", required=True, help="what to run, e.g. \"ruff format\"")
    p.add_argument("--repo")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_habits)
    p = hsub.add_parser("review", help="queue a read-only worker that reviews this repository's new habits; "
                                       "its ending gives the install lines")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_habits)
    p = hsub.add_parser("uninstall", help="the kill switch")
    p.add_argument("habit_id", type=int)
    p.set_defaults(func=cmd_habits)
    p = hsub.add_parser("status", help="installed habits, runs and failures, and where")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_habits)

    p = sub.add_parser("habit-run", help=argparse.SUPPRESS)
    p.add_argument("habit_id", type=int)
    p.set_defaults(func=cmd_habit_run)

    p = sub.add_parser("ui", help="the local web UI")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--open", action="store_true", help="open it in the browser")
    p.add_argument("--tabs", action="store_true",
                   help="with --open, one more tab for the session, showing all its work inside (at most one "
                        "per session while the board serves); off by default")
    p.add_argument("--session", action="append",
                   help="a session --tabs opens a tab for (default: the one this runs in); may repeat")
    p.add_argument("--idle-exit", type=float, metavar="SECONDS",
                   help="stop once no tab was open for SECONDS, counted from the first one (off by default: the "
                        "board stays up)")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("link", help="put `locks` on a terminal's PATH; survives plugin updates")
    p.add_argument("--dir", help="where to write it (default ~/.local/bin)")
    p.add_argument("--remove", action="store_true", help="take it away again")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("acp", help="locks as an Agent Client Protocol agent over stdio, for an editor "
                                   "(Zed's `agent_servers`): each prompt is one `locks run`")
    p.set_defaults(func=cmd_acp)

    cp = sub.add_parser("compaction", help="when a session should compact and what /compact keeps: the mod "
                                           "takes a decision once (`next`)")
    csub = cp.add_subparsers(dest="compaction_command", required=True)
    p = csub.add_parser("next", help="settle a submitted /compact and take the pending decision, once")
    p.add_argument("--session", required=True)
    p.add_argument("--version", default="unknown", help="the Claude Code version the session runs on")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_compaction)
    p = csub.add_parser("request", help="the orchestrating session decided to compact: it runs when its turn "
                                        "ends, with locks' instructions and what it asks to keep")
    p.add_argument("--session", required=True)
    p.add_argument("--cwd", help="where the session works (its open work and branches are read there)")
    p.add_argument("--why", help="why now, in a line")
    p.add_argument("--keep", help="what the summary must also keep, beyond what locks keeps itself")
    p.add_argument("--then", help="the session's prompt to itself once the compaction finished: what it goes on "
                                  "with (the mod submits it)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_compaction)
    p = csub.add_parser("delivered", help="what became of a decision the mod was to submit; with none of the "
                                          "flags, a turn started first and the line was shown instead")
    p.add_argument("decision", type=int)
    p.add_argument("--session", required=True)
    outcome = p.add_mutually_exclusive_group()
    outcome.add_argument("--ran", action="store_true", help="the engine compacted it")
    outcome.add_argument("--skipped", metavar="REASON", help="a hook vetoed the compaction, saying why")
    outcome.add_argument("--rejected", metavar="REASON", help="the call failed, saying why: the line was shown")
    p.add_argument("--version", default="unknown", help="the Claude Code version that answered")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_compaction)

    p = sub.add_parser("limit-wait", help=argparse.SUPPRESS)
    p.add_argument("--until", type=float, required=True, help="the epoch second the limit's resume is due")
    p.set_defaults(func=cmd_limit_wait)

    p = sub.add_parser("endings", help="what ended since a session last heard; delivered once, --peek to look")
    p.add_argument("--session", required=True)
    p.add_argument("--peek", action="store_true", help="read it without marking it delivered")
    p.add_argument("--via", default="mod", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_endings)

    p = sub.add_parser("guide", help="the orchestration skill's detail, a topic at a time (no topic: the list)")
    p.add_argument("topic", nargs="?")
    p.set_defaults(func=cmd_guide)

    p = sub.add_parser("merge", help="merge the branches tasks kept into the branch checked out, on your word: "
                       "the ones that touch no other first, the check after each; a conflict stops it")
    p.add_argument("ids", nargs="+", help="the tasks whose branches to merge (N or #N)")
    p.add_argument("--repo", help="the checkout (default: the first task's)")
    p.add_argument("--verify", help="the check run after each merge (default: the tasks' own)")
    p.add_argument("--dry-run", action="store_true", help="say the order, merge nothing")
    p.add_argument("--pr", action="store_true",
                   help="merge onto a new branch made from the one checked out, which stays as it is, then push it "
                        "and open one draft pull request (needs gh); a conflict or a failing check stops before")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("ci", help="the repository's CI jobs as a plan reads them; quiet one known not to apply here")
    p.add_argument("--repo", help="the checkout (default: here)")
    ci_sub = p.add_subparsers(dest="ci_command")
    for name, what in (("quiet", "name these jobs in no plan here"), ("speak", "name these jobs again")):
        q = ci_sub.add_parser(name, help=what)
        q.add_argument("jobs", nargs="+", help="a job's name, or file/name")
    p.set_defaults(func=cmd_ci)

    p = sub.add_parser("specs", help="how far this repository's OpenSpec specs are behind its code; "
                                     "`first on` makes every build here owe its spec")
    p.add_argument("--repo", help="the checkout (default: here)")
    specs_sub = p.add_subparsers(dest="specs_command")
    q = specs_sub.add_parser("first", help="spec-first here: on, off, or with neither, whether it is")
    q.add_argument("state", nargs="?", choices=("on", "off"))
    p.set_defaults(func=cmd_specs)

    p = sub.add_parser("pr", help="push the branch a task kept and open a draft pull request with its verdict, on "
                       "your word (needs gh)")
    p.add_argument("id", help="the task (N or #N)")
    p.add_argument("--base", help="the branch to merge into (default: the repository's)")
    p.add_argument("--dry-run", action="store_true", help="print the title and description, push nothing")
    p.set_defaults(func=cmd_pr)

    p = sub.add_parser("split", help="the pieces a request, or task N that stopped as several pieces, splits "
                       "into; --queue starts them on what the task kept")
    p.add_argument("target", help="a task number (N or #N), or the request's text ('-' reads stdin)")
    p.add_argument("--queue", action="store_true", help="queue the pieces; task N leaves the board, and what "
                   "waited for it waits for the last piece")
    p.add_argument("--repo", help="where a request given as text runs (default: here)")
    p.add_argument("--verify", help="the check every piece runs (default: the task's own)")
    p.add_argument("--no-model", action="store_true", help="split only by the request's listed items")
    p.add_argument("--json", action="store_true", help="the proposal as JSON")
    p.set_defaults(func=cmd_split)

    p = sub.add_parser("board", help="needs-you, running and queued counts, for a status line")
    p.add_argument("--json", action="store_true", help="the counts as JSON")
    p.add_argument("--full", action="store_true", help="the whole board as JSON")
    p.add_argument("--repo", action="append", help="only this repository (repeatable)")
    p.add_argument("--session", help="also count the waits that are this session's work (`mine`)")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("prune", help="delete the branches locks kept whose work landed: merged, squash-merged, "
                       "or tool artifacts only (sessions and the UI do it on their own)")
    p.add_argument("--repo", action="append", help="only this checkout (repeatable); every one locks used by default")
    p.add_argument("--dry-run", action="store_true", help="say what would go, delete nothing")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("overview", help="every session seen lately and how its work stands: for watching "
                       "sessions in several projects at once")
    p.add_argument("--hours", type=float, default=24, help="sessions seen and tasks ended this recently")
    p.add_argument("--limit", type=int, default=20, help="the most sessions shown")
    p.add_argument("--repo", action="append", help="only this repository's sessions and tasks (repeatable)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_overview)

    p = sub.add_parser("projects", help="the enrolled projects (a .locks/ folder) locks has worked in")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="also the ones not enrolled")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("sessions", help="the Claude Code sessions locks saw, and how to resume each")
    p.add_argument("--repo", action="append", help="only this repository (repeatable)")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("hook", help="Claude Code hook entry point (reads the event on stdin)")
    p.add_argument("event")
    p.set_defaults(func=cmd_hook)
    return parser


def _starts_work(args: argparse.Namespace) -> bool:
    """The commands that start locks work: a worker runs none of them."""
    command = getattr(args, "command", None)
    if (command == "split" and getattr(args, "queue", False)) or command == "merge":
        return True
    return command in ("run", "resume", "work", "limit-wait", "loop-runner") or (
        command, getattr(args, f"{command}_command", None)) in {
        ("queue", "add"), ("workflow", "run"), ("workflow", "retry"), ("workflow", "answer"), ("investigate", "start"),
        ("loop", "add"), ("loop", "start"), ("loop", "act"), ("loop", "rm"),
        ("habits", "review"), ("habits", "install")}


def _subparser(parser: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.ArgumentParser:
    """The parser of the command the arguments reached (`locks workflow run` is three deep)."""
    found = parser
    while True:
        action = next((a for a in found._actions if isinstance(a, argparse._SubParsersAction)), None)
        name = getattr(args, action.dest, None) if action is not None else None
        if action is None or name not in action.choices:
            return found
        found = action.choices[name]


def _parsers(parser: argparse.ArgumentParser) -> list[argparse.ArgumentParser]:
    out = [parser]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in dict.fromkeys(action.choices.values()):
                out += _parsers(child)
    return out


def _unknown(sub: argparse.ArgumentParser, top: argparse.ArgumentParser, extra: list[str]) -> str:
    """What was given that this command does not take, and what was likely meant:
    the near flag of its own, the commands that take the flag, how its inputs go."""
    import difflib

    mine = [o for a in sub._actions for o in a.option_strings]
    hints = []
    for flag in dict.fromkeys(x.split("=", 1)[0] for x in extra if x.startswith("-")):
        near = difflib.get_close_matches(flag, mine, n=1, cutoff=0.7)
        if near:
            hints.append(f"{flag}: did you mean {near[0]}?")
            continue
        elsewhere = [p.prog for p in _parsers(top) if p is not sub and flag in
                     (o for a in p._actions for o in a.option_strings)]
        if elsewhere:
            hints.append(f"{flag} is taken by {', '.join(elsewhere[:3])}, not by `{sub.prog}`")
    # A word right after an unknown flag is taken as its value, not as one too many.
    words = [x for n, x in enumerate(extra) if not x.startswith("-")
             and not (n and extra[n - 1].startswith("-") and "=" not in extra[n - 1])]
    if words and "--input" in mine:
        hints.append("inputs go by name: --input name=value (`locks workflow list` shows what each workflow takes)")
    elif words:
        hints.append("more words than it takes: quote a text that has spaces")
    return (f"unrecognized arguments: {' '.join(extra)}" + "".join(f"\n  {h}" for h in hints)
            + f"\n  `{sub.prog} --help` lists what it takes")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if extra:
        sub = _subparser(parser, args)
        sub.error(_unknown(sub, parser, extra))
    if os.environ.get("LOCKS_WORKER_TASK") and _starts_work(args):
        # A worker is one attempt of one task: work it starts would run outside its
        # task's checks, its turns and its account. What is left goes in its result.
        print(f"locks: this is a worker of task #{os.environ['LOCKS_WORKER_TASK']}: it does not start locks work. "
              "Say what is left in your result block instead.", file=sys.stderr)
        return 2
    with contextlib.suppress(OSError):
        if renamed := legacy.say_renamed(home()):
            print(renamed, file=sys.stderr)
    os.environ.pop("LOCKS_INVOKED_AS", None)
    return int(args.func(args))

