"""`locks check`: the validation loop on work the session did itself, with no worker.

A session that changed code in its own checkout runs `locks check --request "<text>"`: locks records
a task for that work (`options.self_check`, a contract: work the session did itself), snapshots the
checkout's diff as a commit no ref holds (a temporary index, never the person's), and in a temporary
detached worktree at that commit runs locks' own check (`--verify`, compared with the base and rerun
for a flake as a worker's is, `orchestrate._verified`) and the independent verifier, read-only and
blind: the request verbatim and the diff (`orchestrate._check_spec`). The person's checkout is never
written; the worktree is always removed. No model of locks' own beyond the verifier.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from locks import classify, config, isolate, launch, matrix, orchestrate, repo, stops
from locks import criteria as criteria_mod
from locks import findings as findings_mod
from locks import risk as risk_mod
from locks.store import Store, home
from locks.text import clip

#: The task option that marks work the session did itself (read by other code: a contract).
OPTION = "self_check"
#: Exit codes: a pass, a check or verifier that found something, nothing to check.
EXIT_PASS, EXIT_BLOCKED, EXIT_NOTHING = 0, 1, 2
#: The branches a clean checkout's commits are measured from when no base is given.
TRUNKS = ("origin/HEAD", "origin/main", "origin/master", "main", "master")


class NothingToCheck(Exception):
    """The checkout holds no change against its base."""


@dataclass
class Outcome:
    task_id: int
    kind: str
    tier: str
    base: str
    changed: list[str]
    check: dict[str, Any] = field(default_factory=dict)
    verifier: dict[str, Any] = field(default_factory=dict)
    passed: bool = False

    def card(self) -> dict[str, Any]:
        return {"task": self.task_id, "self_check": True, "kind": self.kind, "risk": self.tier,
                "base": self.base, "changed": self.changed, "check": self.check, "verifier": self.verifier,
                "passed": self.passed}


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    proc = isolate._run_git(args, cwd, env=env, text=True)
    if check and proc.returncode != 0:
        raise isolate.IsolationError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def resolve_base(top: Path, base: str | None) -> str:
    """The commit the session's change is read against: the one given, else HEAD when the checkout
    holds uncommitted changes, else where HEAD left the trunk (its commits are the change)."""
    if base:
        return _git(top, "rev-parse", "--verify", f"{base}^{{commit}}")
    head = _git(top, "rev-parse", "HEAD")
    if _git(top, "status", "--porcelain"):
        return head
    for trunk in TRUNKS:
        found = _git(top, "merge-base", "HEAD", trunk, check=False)
        if found and found != head:
            return found
    return head


def snapshot(top: Path) -> str:
    """The checkout as it stands (tracked changes and untracked files git does not ignore) as a
    commit on HEAD that no ref holds, written through a temporary index: the person's index, refs
    and files are left as they were."""
    fd, index = tempfile.mkstemp(prefix="locks-check-index-")
    os.close(fd)
    os.unlink(index)
    env = {**os.environ, "GIT_INDEX_FILE": index}
    try:
        _git(top, "read-tree", "HEAD", env=env)
        _git(top, "add", "-A", env=env)
        tree = _git(top, "write-tree", env=env)
        return _git(top, "commit-tree", tree, "-p", "HEAD", "-m", "locks check: the session's own change", env=env)
    finally:
        if os.path.exists(index):
            os.unlink(index)


def _verifier_cell(kind: str, model: str | None) -> matrix.Cell:
    if model:
        return orchestrate.verify_cell(matrix.Cell(model, "high"))
    return orchestrate.verify_cell(matrix.ladder_for(kind)[0])


def run(store: Store, cwd: Path, request: str, *, verify: str | None = None, base: str | None = None,
        kind: str | None = None, model: str | None = None, launcher: orchestrate.Launcher | None = None,
        classifier: Callable[[str], classify.Classification] | None = None,
        say: Callable[[str], None] = lambda _: None, session_id: str | None = None) -> Outcome:
    """Record and check the session's own change in `cwd`'s checkout (module doc)."""
    started = time.monotonic()
    top = Path(_git(cwd, "rev-parse", "--show-toplevel"))
    based = resolve_base(top, base)
    commit = snapshot(top)
    changed = [p for p in _git(top, "diff", "--name-only", based, commit).splitlines() if p]
    if not changed:
        raise NothingToCheck(f"nothing changed against {based[:12]}")
    if kind is None:
        found = (classifier or (lambda t: classify.classify(t)))(request)
        kind = found.kind
    risk = risk_mod.of_request(request, kind)
    risk_mod.of_change(risk, changed)
    key = repo.key(top)
    options = {OPTION: True, "base": based, "commit": commit, **({"verify": verify} if verify else {})}
    task = store.create_task(request, status="running", source="check", author="orchestrator", repo=key,
                             cwd=str(cwd), kind=kind, session_id=session_id, options=json.dumps(options))
    task_id = int(task["id"])
    # An earlier self-check of the same request here that found something is answered by this one.
    for old in store._conn.execute(
            "SELECT * FROM tasks WHERE id != ? AND repo IS ? AND body = ? AND status = 'blocked' "
            "AND options LIKE '%\"self_check\": true%'", (task_id, key, request)).fetchall():
        stops.record(store, int(old["id"]), stops.Stop("superseded", f"checked again as #{task_id}"), result=False)
    store.add_event(task_id, "planned", kind=kind, self_check=True, risk=risk.data(), base=based, commit=commit,
                    changed=changed[:200], n_changed=len(changed), verify=verify or "")
    out = Outcome(task_id, kind, risk.tier, based, changed)
    say(f"locks: #{task_id} checking the session's own change ({len(changed)} file(s), {kind}, risk {risk.tier})")
    blockers: list[dict[str, str]] = []
    cost = 0.0
    with isolate.at_base(top, commit, home()) as where:
        if verify:
            baseline = orchestrate.Baseline(store, task_id, top, based, home(),
                                            must_fix=kind in orchestrate._REPRODUCES)
            mark = time.monotonic()
            result = orchestrate._verified(launch.WorkerResult(True, None, "the session's own change"),
                                           verify, where, baseline)
            check_s = round(time.monotonic() - mark, 2)
            out.check = {"command": verify, "passed": result.passed, "check_s": check_s,
                         **({"failure": result.failure.value} if result.failure else {}),
                         **({"tail": clip(result.check_tail or result.evidence, 2000)} if not result.passed else {}),
                         **({"preexisting": True} if baseline.found else {})}
            store.add_event(task_id, "self_checked", **out.check)
            say(f"locks: #{task_id} `{verify}` " + ("passed" if result.passed else "failed"))
        if out.check and not out.check["passed"]:
            out.verifier = {"ran": False, "why": "locks' own check failed"}
        elif not config.enabled("verifier", os.environ):
            out.verifier = {"ran": False, "why": "the verifier setting is off"}
        elif risk.tier == "passive":
            out.verifier = {"ran": False, "why": "a passive change (risk tier passive)"}
        else:
            cell = _verifier_cell(kind, model)
            spec = launch.LaunchSpec(prompt="", cell=cell, launch_dir=where,
                                     criteria=criteria_mod.ids(request),
                                     env={"LOCKS_WORKER_TASK": str(task_id), "LOCKS_WORKER_ATTEMPT": "0",
                                          "LOCKS_HOOKS_OFF": "1"})
            check, vcell = orchestrate._check_spec(text=request, kind=kind, cell=cell, spec=spec, workdir=where,
                                                   base=based, changed=changed, risk=risk, ledger={},
                                                   registry=None, rules=(), rows=())
            say(f"locks: #{task_id} an independent check at {vcell.label}")
            mark = time.monotonic()
            verdict = (launcher or launch.run)(check)
            shown = findings_mod.read({"findings": list(verdict.findings)})
            blockers = findings_mod.blocking(shown)
            if not verdict.passed and not blockers and verdict.verdict == "fail":
                blockers = [{"where": "", "what": verdict.summary or "the check failed without a finding",
                             "evidence_class": "inferential", "cause": "unknown", "proof": ""}]
            # As the late check: a finding only read from the code goes with a pass, never a blocker.
            blockers, carried = orchestrate._carry(blockers) if verdict.passed else (blockers, [])
            decided = verdict.passed or bool(blockers)
            cost = verdict.cost_usd
            out.verifier = {"ran": True, "cell": vcell.label, "passed": decided and not blockers,
                            "decided": decided, "summary": clip(verdict.summary, 500),
                            "criteria": list(verdict.criteria), "blockers": blockers[:20], "carried": carried[:20],
                            "cost_usd": cost}
            store.add_event(task_id, "verified", seq=0, self_check=True, cell=vcell.label, risk=risk.data(),
                            verdict=verdict.verdict, passed=decided and not blockers, findings=list(shown)[:20],
                            blocking=len(blockers), blockers=blockers[:20], carried=len(carried),
                            cost_usd=cost, summary=verdict.summary[:500],
                            wall_s=round(time.monotonic() - mark, 2), turns=verdict.turns)
    check_ok = not out.check or out.check["passed"]
    out.passed = check_ok and not blockers
    wall_s = round(time.monotonic() - started, 2)
    store.update_task(task_id, cost_usd=cost)
    timing = {"wall_s": wall_s, "check_s": out.check.get("check_s", 0.0), "verifier_cost_usd": cost}
    if out.passed:
        store.update_task(task_id, status="done", result="the session's own change passed locks' check")
        store.add_event(task_id, "finished", status="done", self_check=True, verdict=out.card(), **timing)
        store.add_event(task_id, "self_check_ended", status="done", **timing)
    else:
        why = (f"`{verify}` failed on the session's own change" if not check_ok
               else f"the independent check found {len(blockers)} problem(s) in the session's own change")
        stops.record(store, task_id, stops.Stop("review", why, account=clip(findings_mod.text(blockers), 2000)
                                                if blockers else out.check.get("tail", "")))
        # The stop's `finished` event carries the account; the card and the timing go beside it.
        store.add_event(task_id, "self_check_ended", status="review", verdict=out.card(), **timing)
    # The session ran it and read its card: no ending is handed to it again.
    store.mark_reported([task_id], via="check")
    return out


def text(out: Outcome) -> str:
    """The verdict card as `locks check` prints it."""
    lines = [f"#{out.task_id} {'PASS' if out.passed else 'REVIEW'}: the session's own change "
             f"({out.kind}, risk {out.tier}, {len(out.changed)} file(s) against {out.base[:12]})"]
    shown = out.changed[:10]
    lines.append("changed (from git): " + ", ".join(shown)
                 + (f" and {len(out.changed) - 10} more" if len(out.changed) > 10 else ""))
    if out.check:
        c = out.check
        lines.append(f"check `{c['command']}`: " + ("passed" if c["passed"] else "failed")
                     + (" (its failures were there at the base)" if c.get("preexisting") else "")
                     + f" in {c['check_s']}s")
        if not c["passed"] and c.get("tail"):
            lines.append(c["tail"])
    else:
        lines.append("check: none given (--verify)")
    v = out.verifier
    if not v.get("ran"):
        lines.append(f"independent check: not run ({v.get('why', '')})")
    else:
        state = "passed" if v["passed"] else ("could not decide" if not v["decided"] else "found blockers")
        lines.append(f"independent check at {v['cell']}: {state} (${v['cost_usd']:.2f})")
        for c in v.get("criteria") or []:
            lines.append(f"  {c.get('id', '?')}: {'met' if c.get('met') else 'NOT met'}"
                         + (f" — {clip(str(c.get('evidence') or ''), 200)}" if c.get("evidence") else ""))
        for b in v.get("blockers") or []:
            lines.append(f"  blocker {b.get('where') or ''}: {clip(str(b.get('what') or ''), 300)}")
        if v.get("carried"):
            lines.append(f"  {len(v['carried'])} finding(s) read from the code, not shown by a probe, go with it")
    lines.append("" if out.passed else f"Fix what it names and run `locks check` again (task #{out.task_id}).")
    return "\n".join(x for x in lines if x is not None).rstrip()
