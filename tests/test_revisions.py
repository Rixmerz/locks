"""A role's lessons in one repository, improved by measured trial (`locks.revisions`)."""
from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from locks import arms, doctor, revisions, roles
from locks.store import Store

LESSONS = ["Run pytest -q tests before claiming a pass; the suite is the check here.",
           "The exporters live in src/export/, not in src/cli.py."]


class Editor:
    """A fake Haiku: answers with what it was given and records each question."""

    def __init__(self, answer: object = None):
        self.answer = {"lessons": LESSONS} if answer is None else answer
        self.asked: list[str] = []

    def __call__(self, system_prompt, schema, text, **_):
        self.asked.append(text)
        return (self.answer if isinstance(self.answer, dict) else None), 0.002, ""


def _task(store: Store, *, role="implementer", repo="r", status="done", first: bool | None = True,
          attempts: int = 1, blockers: int = 0, **brief) -> int:
    task = store.create_task("t", status=status, source="locks", repo=repo)
    store.add_event(task["id"], "planned", kind="implement", role=role)
    store.add_event(task["id"], "briefed", role=role, role_version="abc", **brief)
    if first is not None:
        for n in range(attempts):
            passed = first if n == 0 else n == attempts - 1
            store.add_attempt(task["id"], cell="sonnet/medium", max_turns=80, passed=int(passed))
    if blockers:
        store.add_event(task["id"], "verified", passed=False, blocking=blockers,
                        blockers=[{"where": "src/a.py", "what": f"the change drops the header {i}"}
                                  for i in range(blockers)])
    store.add_event(task["id"], "finished", status=status)
    return task["id"]


def _evidence(store: Store, *, repo="r", role="implementer", n=3) -> None:
    for i in range(n):
        problem = store.open_problem(f"export breaks on empty rows {i}", repo=repo)
        store.add_fix(problem, f"patched the writer {i}", "failed", repo=repo, why="the reader was the cause",
                      task_id=None, role=role, areas=("code",))


# --- a role's version -----------------------------------------------------------------------


def test_a_role_s_version_is_its_text_and_skill_names_never_where_it_lives():
    role = roles.load().get("implementer")
    v = roles.version(role)
    assert len(v) == roles.VERSION_CHARS and v == roles.version(role)
    assert roles.version(dataclasses.replace(role, path="/elsewhere", scope="user")) == v
    assert roles.version(dataclasses.replace(role, prompt=role.prompt + "\nMore.")) != v
    assert roles.version(dataclasses.replace(role, skills=(*role.skills, "extra"))) != v
    assert revisions.lessons_hash(LESSONS) != revisions.lessons_hash(LESSONS[::-1])


# --- proposing -------------------------------------------------------------------------------


def test_a_revision_is_proposed_from_the_role_s_evidence_and_only_then(store: Store):
    editor = Editor()
    assert revisions.propose(store, "r", "implementer", ask=editor) is None and editor.asked == []
    _evidence(store, n=2)
    _task(store, blockers=1)
    assert revisions.due(store, "r", "implementer")
    made = revisions.propose(store, "r", "implementer", description="builds", role_version="v1", ask=editor)
    assert made["record"] == "proposed" and made["lessons"] == LESSONS and made["role_version"] == "v1"
    assert made["hash"] == revisions.lessons_hash(LESSONS)
    asked = editor.asked[0]
    assert "export breaks on empty rows 0" in asked and "the change drops the header 0 (src/a.py)" in asked
    assert "<evidence>" in asked and "Current lessons for this role here:\n(none)" in asked
    kept = store.role_revisions(repo="r", role="implementer")
    assert [r["record"] for r in kept] == ["proposed"] and kept[0]["evidence"]["dead_ends"] == 2
    # At most one on trial: nothing more is proposed, and the editor is not asked, until it is judged.
    _evidence(store, n=3)
    assert not revisions.due(store, "r", "implementer")
    assert revisions.propose(store, "r", "implementer", ask=editor) is None and len(editor.asked) == 1
    # Another role, and another repository, have none of this evidence.
    assert not revisions.due(store, "r", "debugger") and not revisions.due(store, "other", "implementer")


def test_the_verifier_and_the_surveyor_never_get_a_revision(store: Store):
    for role in roles.INTERNAL:
        _evidence(store, role=role, n=5)
        assert not revisions.due(store, "r", role)
        assert revisions.propose(store, "r", role, ask=Editor()) is None
        assert revisions.for_brief(store, "r", role, 1) == ("", {})


@pytest.mark.parametrize("answer", [
    "no answer",
    {"lessons": []},
    {"lessons": ["too short"]},
    {"lessons": [42]},
    {"lessons": "Run the suite before claiming a pass."},
    {"lessons": LESSONS, "note": "and also"},
    {"lessons": ["Run the suite before claiming a pass, step number " + str(i) for i in range(20)]},
    {"lessons": ["x" * (revisions.LESSON_CHARS + 1)]},
    {"lessons": ["Use the token sk-abcdefghijklmnop to call the API."]},
])
def test_no_answer_or_a_malformed_one_proposes_nothing(store: Store, answer):
    _evidence(store)
    assert revisions.propose(store, "r", "implementer", ask=Editor(answer)) is None
    assert store.role_revisions() == []


def test_private_text_and_secrets_never_reach_the_editor_or_the_lessons(store: Store):
    problem = store.open_problem("deploy fails <private>my home address</private> on push", repo="r")
    store.add_fix(problem, "set the key ghp_abcdefghijklmnop1234", "failed", repo="r", role="implementer")
    _evidence(store, n=3)
    editor = Editor({"lessons": [*LESSONS, "Never paste the token ghp_abcdefghijklmnop1234 anywhere."]})
    made = revisions.propose(store, "r", "implementer", ask=editor)
    assert "my home address" not in editor.asked[0] and "ghp_" not in editor.asked[0]
    assert made["lessons"] == LESSONS


def test_the_current_lessons_proposed_again_are_no_candidate(store: Store):
    _evidence(store)
    first = revisions.propose(store, "r", "implementer", ask=Editor(), now=datetime(2026, 1, 1, tzinfo=UTC))
    store.add_role_revision(at="2026-10-01T00:00:00+00:00", repo="r", role="implementer", record="adopted",
                            revision=first["id"], hash=first["hash"], lessons=LESSONS,
                            evidence={"current": {"pass_first": 0.5}}, version="0.32.0")
    _evidence(store, n=3)
    assert revisions.due(store, "r", "implementer")
    assert revisions.propose(store, "r", "implementer", ask=Editor()) is None
    made = revisions.propose(store, "r", "implementer", ask=Editor({"lessons": LESSONS[:1]}))
    assert made["evidence"]["against"] == first["id"] and "since revision #" in made["reason"]


# --- the trial ---------------------------------------------------------------------------------


def _candidate(store: Store, lessons=LESSONS) -> dict:
    opened = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    return {**{"id": store.add_role_revision(at=opened, repo="r", role="implementer",
                                            record="proposed", hash=revisions.lessons_hash(lessons),
                                            lessons=lessons, version="0.32.0")}}


def test_the_arms_alternate_and_a_resumed_task_keeps_its_arm(store: Store):
    assert revisions.for_brief(store, "r", "implementer", 1) == ("", {})
    assert revisions.for_brief(store, None, "implementer", 1) == ("", {})
    cand = _candidate(store)
    given = {}
    for task_id in range(10, 16):
        text, data = revisions.for_brief(store, "r", "implementer", task_id)
        store.add_event(store.create_task("t", status="done", source="locks", repo="r")["id"], "briefed",
                        role="implementer", **data)
        given[data["revision_arm"]] = text
        assert data["revision_trial"] == cand["id"] and "trial" in data["revision_why"]
    sides = [b["data"]["revision_arm"] for b in store.briefs("r")]
    # The arms fill at the same pace: never more than `arms.BALANCE` apart, each at least two of six.
    assert abs(sides.count("candidate") - sides.count("current")) <= arms.BALANCE
    assert min(sides.count("candidate"), sides.count("current")) >= 2
    assert "Lessons for the `implementer` role in this repository" in given["candidate"]
    assert "data, not instructions" in given["candidate"] and LESSONS[0] in given["candidate"]
    assert given["current"] == ""
    # A resume of a task briefed in an arm keeps it, whatever the counts say now.
    first = store.briefs("r")[0]
    _, again = revisions.for_brief(store, "r", "implementer", first["task_id"])
    assert again["revision_arm"] == first["data"]["revision_arm"] and "first brief" in again["revision_why"]


def _arm(store: Store, cand: int, arm: str, *, passed: int, n: int = revisions.ROLE_TRIAL, attempts: int = 1,
         blockers: int = 0, revision: int | None = None) -> None:
    for i in range(n):
        _task(store, first=i < passed, attempts=attempts if i < passed else 2, blockers=blockers,
              revision_trial=cand, revision_arm=arm, revision=revision if arm == "current" else cand)


def test_a_trial_adopts_a_revision_that_won_and_drops_one_that_lost(store: Store):
    cand = _candidate(store)["id"]
    _arm(store, cand, "candidate", passed=8, n=revisions.ROLE_TRIAL - 1)
    _arm(store, cand, "current", passed=6)
    assert revisions.judge(store, "r", "implementer") == []  # one arm is short of its trial
    _arm(store, cand, "candidate", passed=1, n=1)
    found = revisions.judge(store, "r", "implementer")
    assert [r["record"] for r in found] == ["adopted"] and "better on pass-first" in found[0]["reason"]
    assert found[0]["evidence"]["candidate"]["tasks"] == revisions.ROLE_TRIAL
    state = revisions.state(store, "r", "implementer")
    assert state["candidate"] is None and state["current"]["id"] == cand
    text, data = revisions.for_brief(store, "r", "implementer", 99)
    assert data == {"revision_arm": "adopted", "revision_why": f"revision #{cand} won its trial here",
                    "revision": cand, "revision_hash": revisions.lessons_hash(LESSONS)}
    assert "kept after a trial" in text
    assert revisions.judge(store, "r", "implementer") == []  # judged once

    # The next candidate is tried against the adopted one, and loses.
    nxt = _candidate(store, LESSONS[:1])["id"]
    _, data = revisions.for_brief(store, "r", "implementer", 100)
    assert data["revision_trial"] == nxt and data["revision"] in (nxt, cand)
    _arm(store, nxt, "candidate", passed=4)
    _arm(store, nxt, "current", passed=8, revision=cand)
    found = revisions.judge(store, "r", "implementer")
    assert [r["record"] for r in found] == ["dropped", "kept"] and "worse by more than" in found[0]["reason"]
    # The trial's current arm ran under the adopted revision: its first tries judged it, once, and it held.
    assert found[1]["revision"] == cand and found[1]["evidence"]["after"]["tasks"] == revisions.ROLE_TRIAL
    assert revisions.state(store, "r", "implementer")["current"]["id"] == cand


def test_a_verdict_reads_every_measure():
    base = {"pass_first": 0.6, "attempts_to_pass": 2.0, "blockers": 1.0}
    assert revisions.verdict({**base, "blockers": 0.5}, base)[0] == "adopted"
    assert revisions.verdict({**base, "attempts_to_pass": 1.5}, base)[0] == "adopted"
    # Fewer blockers, but first tries far worse: dropped.
    said, why = revisions.verdict({**base, "pass_first": 0.4, "blockers": 0.0}, base)
    assert said == "dropped" and "worse" in why
    said, why = revisions.verdict(dict(base), base)
    assert said == "dropped" and "no measure" in why


def test_an_adopted_revision_that_made_first_tries_worse_is_reverted_as_its_own_record(store: Store):
    cand = _candidate(store)["id"]
    _arm(store, cand, "candidate", passed=10)
    _arm(store, cand, "current", passed=8)
    assert [r["record"] for r in revisions.judge(store, "r", "implementer")] == ["adopted"]
    for i in range(revisions.REVERT_AFTER):
        _task(store, first=i < 3, revision=cand, revision_arm="adopted")
    found = revisions.judge(store, "r", "implementer")
    assert [r["record"] for r in found] == ["reverted"] and found[0]["revision"] == cand
    assert "worse by more than" in found[0]["reason"]
    assert revisions.state(store, "r", "implementer")["current"] is None
    assert revisions.for_brief(store, "r", "implementer", 7) == ("", {})
    assert [r["record"] for r in store.role_revisions()] == ["proposed", "adopted", "reverted"]


def test_an_adopted_revision_that_held_is_kept(store: Store):
    cand = _candidate(store)["id"]
    _arm(store, cand, "candidate", passed=10)
    _arm(store, cand, "current", passed=8)
    revisions.judge(store, "r", "implementer")
    for _ in range(revisions.REVERT_AFTER):
        _task(store, first=True, revision=cand, revision_arm="adopted")
    assert [r["record"] for r in revisions.judge(store, "r", "implementer")] == ["kept"]
    assert revisions.state(store, "r", "implementer")["current"]["id"] == cand


def test_the_records_are_append_only(store: Store):
    _candidate(store)
    with pytest.raises(Exception, match="append-only"):
        store._conn.execute("UPDATE role_revisions SET lessons = '[]'")
    with pytest.raises(Exception, match="append-only"):
        store._conn.execute("DELETE FROM role_revisions")


# --- settling ----------------------------------------------------------------------------------


def test_settle_judges_and_proposes_where_work_ended_and_nothing_when_off(store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_ROLE_TRIALS", "off")
    _evidence(store)
    _task(store)
    editor = Editor()
    assert revisions.settle(store, ask=editor) == {"judged": [], "proposed": []}  # off
    monkeypatch.setenv("LOCKS_ROLE_TRIALS", "on")
    found = revisions.settle(store, ask=editor)
    assert [r["record"] for r in found["proposed"]] == ["proposed"] and len(editor.asked) == 1
    assert found["proposed"][0]["role_version"] == roles.version(roles.load().get("implementer"))
    assert revisions.settle(store, ask=editor) == {"judged": [], "proposed": []}  # nothing ended since
    _evidence(store, n=4)
    _task(store)
    assert revisions.settle(store, ask=editor)["proposed"] == [] and len(editor.asked) == 1  # one on trial


def test_settle_runs_from_evolution_and_never_for_an_investigator(store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_ROLE_TRIALS", "off")
    from locks import evolution, standards

    monkeypatch.setattr(standards, "settle", lambda store, now=None: {"ended": 0, "calibrated": [], "judged": []})
    assert "roles" not in evolution.settle(store)
    monkeypatch.setenv("LOCKS_ROLE_TRIALS", "on")
    called = []
    monkeypatch.setattr(revisions, "settle", lambda store, now=None: called.append(1) or {"judged": []})
    assert evolution.settle(store)["roles"] == {"judged": []} and called == [1]


def test_a_claim_keeps_two_settles_from_asking_twice(store: Store):
    now = datetime(2026, 10, 21, tzinfo=UTC)
    assert revisions._claim(store, "r", "implementer", now)
    assert not revisions._claim(store, "r", "implementer", now + timedelta(minutes=1))
    assert revisions._claim(store, "r", "implementer", now + revisions.CLAIM + timedelta(seconds=1))
    store.set_meta("role_trials:asking:r:debugger", "not a time")
    assert revisions._claim(store, "r", "debugger", now)


# --- seen ---------------------------------------------------------------------------------------


def test_the_overview_and_the_command_show_current_candidate_and_history(store: Store, capsys, monkeypatch):
    monkeypatch.setenv("LOCKS_ROLE_TRIALS", "off")  # the command says so
    from locks import analysis, cli

    cand = _candidate(store)["id"]
    _arm(store, cand, "candidate", passed=10)
    _arm(store, cand, "current", passed=8)
    revisions.judge(store, "r", "implementer")
    nxt = _candidate(store, LESSONS[:1])["id"]
    _task(store, revision_trial=nxt, revision_arm="candidate", revision=nxt)
    rows = revisions.overview(store)
    assert rows[0]["current"]["id"] == cand and rows[0]["candidate"]["id"] == nxt
    assert rows[0]["candidate"]["arms"]["candidate"]["tasks"] == 1
    assert [h["record"] for h in rows[0]["history"]] == ["proposed", "adopted", "proposed"]
    said = "\n".join(revisions.text(rows))
    assert f"implementer in r: revision #{cand}" in said and f"on trial: revision #{nxt}" in said
    assert f"    - {LESSONS[0]}" in said and f"    + {LESSONS[0]}" in said

    store.add_event(_task(store), "planned", kind="implement", start="sonnet/medium", plan_s=1.0)
    report = analysis.compute(store, days=0)
    assert report["role_revisions"][0]["role"] == "implementer"
    assert "the roles' lessons here" in analysis.text(report)

    monkeypatch.setattr(Store, "open", classmethod(lambda cls, env=None: store))
    monkeypatch.setattr(store, "close", lambda: None)
    assert cli.main(["roles", "revisions", "--role", "implementer"]) == 0
    out = capsys.readouterr().out
    assert "role trials are off" in out and f"revision #{cand}" in out
    assert cli.main(["roles", "revisions", "--json", "--role", "nobody"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main(["roles", "revisions", "--role", "nobody"]) == 0
    assert "no role has a revision yet" in capsys.readouterr().out


# --- doctor ------------------------------------------------------------------------------------


def test_doctor_names_two_candidates_a_double_verdict_and_a_trial_nobody_got(store: Store):
    first = _candidate(store)["id"]
    second = _candidate(store, LESSONS[:1])["id"]
    found = {f.rule: f for f in doctor.check(store, days=0)}
    assert found["candidates-on-trial-at-once"].severity == doctor.BROKEN
    assert f"#{first}, #{second}" in found["candidates-on-trial-at-once"].what
    for record in ("adopted", "dropped"):
        store.add_role_revision(at="2026-10-02T00:00:00+00:00", repo="r", role="implementer", record=record,
                                revision=first, hash="h", version="0.32.0")
    store.add_role_revision(at="2026-10-02T00:00:00+00:00", repo="r", role="implementer", record="kept",
                            revision=first, hash="h", version="0.31.0")
    store.add_role_revision(at="2026-10-02T00:00:00+00:00", repo="r", role="implementer", record="reverted",
                            revision=first, hash="h", version="0.31.0")
    # The second candidate has been on trial for days and the role's tasks here got no arm.
    _task(store)
    found = doctor.check(store, days=0, now=datetime.now(UTC) + timedelta(days=4))
    rules = {(f.rule, f.severity) for f in found}
    assert ("revision-judged-twice", doctor.SUSPECT) in rules and ("revision-judged-twice", doctor.FIXED) in rules
    assert ("trial-never-sampled", doctor.SUSPECT) in rules
    assert "candidates-on-trial-at-once" not in {f.rule for f in found}
    _task(store, revision_trial=second, revision_arm="current")
    assert "trial-never-sampled" not in {f.rule for f in doctor.check(store, days=0,
                                                                      now=datetime.now(UTC) + timedelta(days=4))}


# --- a run's brief -------------------------------------------------------------------------------


@pytest.mark.usefixtures("no_verifier")
def test_a_run_s_brief_records_the_role_s_version_and_follows_the_trial_s_arm(git_repo, store, monkeypatch):
    from locks import repo
    from locks.orchestrate import run

    from .test_orchestrate import Script, kind, ok

    report = run("add the export", git_repo, store, registry={}, launcher=Script(ok(), write="a.py"),
                 classifier=kind("implement"))
    briefed = store.last_event(report.task_id, "briefed")["data"]
    role = store.last_event(report.task_id, "planned")["data"]["role"]
    assert briefed["role"] == role and briefed["role_version"] == roles.version(roles.load().get(role))
    assert "revision_arm" not in briefed
    key = repo.key(git_repo)
    store.add_role_revision(at="2026-10-01T00:00:00+00:00", repo=key, role=role, record="proposed",
                            hash=revisions.lessons_hash(LESSONS), lessons=LESSONS, version="0.32.0")
    monkeypatch.setenv("LOCKS_ROLE_TRIALS", "on")
    seen = {}
    # The arm is the task's hash until the arms drift two apart (`arms.side`): a few runs see both.
    for name in ("b.py", "c.py", "d.py", "e.py")[:2 + arms.BALANCE]:
        script = Script(ok(), write=name)
        again = run(f"add the {name}", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
        data = store.last_event(again.task_id, "briefed")["data"]
        seen[data["revision_arm"]] = LESSONS[0] in script.specs[0].prompt
        if len(seen) == 2:
            break
    assert seen == {"candidate": True, "current": False}
    monkeypatch.setenv("LOCKS_ROLE_TRIALS", "off")
    off = run("add the d", git_repo, store, registry={}, launcher=Script(ok(), write="d.py"),
              classifier=kind("implement"))
    assert "revision_arm" not in store.last_event(off.task_id, "briefed")["data"]


def test_a_trial_that_cannot_fill_its_arms_in_its_days_is_dropped_for_too_few_tasks(store: Store):
    cand = _candidate(store)["id"]
    _arm(store, cand, "candidate", passed=1, n=1)
    assert revisions.judge(store, "r", "implementer") == []
    later = datetime.now(UTC) + timedelta(days=revisions.TRIAL_DAYS)
    [found] = revisions.judge(store, "r", "implementer", now=later)
    assert found["record"] == "dropped" and found["reason"].startswith("too few tasks here to judge it: in 14 days")
    assert revisions.state(store, "r", "implementer")["candidate"] is None


def test_a_difference_one_task_would_turn_adopts_nothing():
    """82% against 80% over ten first tries adopted a revision that was reverted the day after."""
    arm = {"pass_first": 0.8, "attempts_to_pass": 1.2, "blockers": 0.2, "tasks": 10}
    said, why = revisions.verdict({**arm, "pass_first": 0.82}, arm)
    assert said == "dropped" and "by more than one task of the arm" in why
    assert revisions.verdict({**arm, "pass_first": 0.9}, arm)[0] == "dropped"  # exactly one task
    assert revisions.verdict({**arm, "pass_first": 1.0}, arm)[0] == "adopted"
    assert revisions.verdict({**arm, "blockers": 0.15}, arm)[0] == "dropped"
    assert revisions.verdict({**arm, "blockers": 0.0}, arm)[0] == "adopted"
