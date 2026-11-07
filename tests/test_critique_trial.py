"""The plan's blind reader: its brief and launch, its event, and the trial that keeps it only where it paid."""
from __future__ import annotations

from pathlib import Path

import pytest

from locks import arms, critique, launch
from locks.escalate import Failure
from locks.matrix import Cell
from locks.store import Store

REQUEST = """Add an export command.

- writes the rows as CSV
- reports how many rows it wrote
"""
CONTRACT = "Every part calls `export.write_rows(rows, out)` and returns the row count as an int."
PARTS = (
    {"id": "writer", "prompt": "Build `export.write_rows` (S1).",
     "surfaces": ["src/export/**", "tests/test_export.py"]},
    {"id": "cli", "prompt": "Wire `locks export` to it; print the count.", "surfaces": ["src/cli.py"],
     "after": "writer"},
)


def plan_spec(**changes) -> launch.LaunchSpec:
    base = dict(
        prompt="plan it", cell=Cell("sonnet", "high"), launch_dir=Path("/work"), max_turns=60, timeout_s=3600,
        disallowed_tools=("WebFetch",), allowed_tools=("Bash(git status)", "mcp__livespec", "mcp__rastro__look"),
        mcp_servers={"livespec": {"command": "x"}}, append_system_prompt="you plan", criteria=("S1", "S2"),
        verdicts=("F1",), verdict_words=("fixed",), parts=True, report=True, persist_session=True,
        resume_session="sess-plan", resume_note="go on", fork_session=True,
        env={"LOCKS_WORKER_TASK": "4", "LOCKS_WORKER_ATTEMPT": "1"})
    return launch.LaunchSpec(**{**base, **changes})


class Reader:
    """A fake launcher: records every spec it is given and answers with the result it was built with.
    `clock` is a fake `time.monotonic` it advances by `takes` seconds on each launch."""

    def __init__(self, result: launch.WorkerResult | None = None, takes: float = 0.0) -> None:
        self.result = result or launch.WorkerResult(True, None, "ok", "read", verdict="pass")
        self.specs: list[launch.LaunchSpec] = []
        self.now, self.takes = 100.0, takes

    def clock(self) -> float:
        return self.now

    def __call__(self, spec: launch.LaunchSpec) -> launch.WorkerResult:
        self.specs.append(spec)
        self.now += self.takes
        return self.result


def found(where: str = "cli", what: str = "the output format is left to its worker") -> dict[str, str]:
    return {"where": where, "what": what, "evidence_class": "inferential", "cause": "unknown", "proof": ""}


# --- the reader's brief ---------------------------------------------------------------------------------------


def test_the_brief_carries_the_request_contract_and_every_part_verbatim():
    brief = critique.brief(REQUEST, PARTS, CONTRACT)
    assert REQUEST in brief and CONTRACT in brief
    for part in PARTS:
        assert f"Part `{part['id']}`" in brief and part["prompt"] in brief
        assert all(path in brief for path in part["surfaces"])
    # The request's criteria, in the person's words.
    assert "S1: writes the rows as CSV" in brief and "S2: reports how many rows it wrote" in brief
    # In the order it was told: what to do, then the request, its criteria, the contract, the parts.
    order = [brief.index(x) for x in (critique.TASK, "The request, verbatim", "S1: writes", "The contract every",
                                       "The parts:", "Part `writer`", "Part `cli`")]
    assert order == sorted(order)
    assert "after" not in brief.split("The parts:")[1]  # nothing but id, prompt and surfaces


def test_the_brief_tells_the_reader_to_paraphrase_list_decisions_name_criteria_and_report_findings():
    task = critique.TASK
    for asked in ("Paraphrase each part in two sentences", "design decisions its prompt leaves to its worker",
                  "a name, a schema, an endpoint, an error-handling strategy, where a file goes",
                  "criterion of the request", "`findings`", "`request`", "A part you could not paraphrase is a finding",
                  "read the repository"):
        assert asked in task


def test_a_request_without_listed_items_adds_no_criteria_block():
    assert "The request's criteria" not in critique.brief("Make exports faster.", PARTS, CONTRACT)
    assert "The request's criteria, in the person's words" in critique.brief(REQUEST, PARTS, CONTRACT)


# --- its launch -----------------------------------------------------------------------------------------------


def test_the_reader_is_a_read_only_sonnet_low_session_of_its_own():
    plan = plan_spec()
    spec = critique.spec_for(plan, REQUEST, PARTS, CONTRACT)
    assert spec.prompt == critique.brief(REQUEST, PARTS, CONTRACT)
    assert spec.cell == critique.CELL == Cell("sonnet", "low") and spec.cell.label == "sonnet/low"
    assert spec.judging and not spec.report and not spec.parts
    assert spec.criteria == () and spec.verdicts == () and spec.verdict_words == ()
    assert set(launch.WRITE_TOOLS) <= set(spec.disallowed_tools) and "WebFetch" in spec.disallowed_tools
    assert spec.mcp_servers == {} and spec.allowed_tools == ("Bash(git status)",)
    assert spec.append_system_prompt == ""
    assert spec.max_turns == critique.TURNS and spec.timeout_s == launch.timeout_for(critique.TURNS)
    assert (spec.resume_session, spec.fork_session, spec.resume_note, spec.persist_session) == ("", False, "", False)
    assert spec.env == {"LOCKS_WORKER_TASK": "4", "LOCKS_WORKER_ATTEMPT": "1-critique"}
    assert spec.launch_dir == plan.launch_dir  # the task's directory, as the plan's


def test_the_reader_takes_the_model_a_person_pinned_and_its_permission_mode(monkeypatch):
    monkeypatch.setenv("LOCKS_MODEL_SONNET", "claude-sonnet-5-5")
    monkeypatch.setenv("LOCKS_MODE_SONNET", "dontAsk")
    spec = critique.spec_for(plan_spec(), REQUEST, PARTS, CONTRACT)
    assert spec.model_id == "claude-sonnet-5-5" and spec.permission_mode == "dontAsk"


def test_a_plan_with_no_attempt_marker_keeps_its_environment_as_it_was():
    spec = critique.spec_for(plan_spec(env={"LOCKS_WORKER_TASK": "4"}), REQUEST, PARTS, CONTRACT)
    assert spec.env == {"LOCKS_WORKER_TASK": "4"}


def test_a_plan_is_never_changed_by_the_reader_s_spec():
    plan = plan_spec()
    critique.spec_for(plan, REQUEST, PARTS, CONTRACT)
    assert plan.parts and plan.report and plan.mcp_servers and plan.resume_session == "sess-plan"
    assert plan.env["LOCKS_WORKER_ATTEMPT"] == "1"


# --- what it keeps --------------------------------------------------------------------------------------------


def test_a_reading_is_launched_once_and_kept_as_a_critiqued_event(store: Store, monkeypatch):
    task = store.create_task("plan it", status="running", source="locks", repo="r", kind="plan")
    reader = Reader(launch.WorkerResult(True, None, "ok", "read", cost_usd=0.123456, verdict="pass",
                                        findings=(found(), found("request", "S2 is answered by no part"))), takes=2.5)
    monkeypatch.setattr(critique.time, "monotonic", reader.clock)
    got = critique.read(reader, plan_spec(), REQUEST, PARTS, CONTRACT, store=store, task_id=task["id"], nth=2,
                        nth_round=1)
    assert len(reader.specs) == 1 and reader.specs[0].cell == critique.CELL
    assert got == critique.Critique(findings=got.findings, cost_usd=0.123456, seconds=2.5, verdict="pass")
    assert [f["where"] for f in got.findings] == ["cli", "request"]
    (event,) = store.events_of(task["id"], "critiqued")
    assert event["data"] == {"seq": 2, "round": 1, "cell": "sonnet/low", "findings": list(got.findings),
                             "cost_usd": 0.1235, "seconds": 2.5, "verdict": "pass", "held": True}
    assert set(event["data"]["findings"][0]) >= {"where", "what", "evidence_class", "cause", "proof"}


def test_the_second_reading_is_never_held(store: Store):
    task = store.create_task("plan it", status="running", source="locks", repo="r", kind="plan")
    reader = Reader(launch.WorkerResult(True, None, "ok", "read", findings=(found(),), verdict="pass"))
    again = critique.read(reader, plan_spec(), REQUEST, PARTS, CONTRACT, store=store, task_id=task["id"], nth=1,
                          nth_round=2)
    assert again.findings
    assert store.events_of(task["id"], "critiqued")[0]["data"]["held"] is False
    assert store.events_of(task["id"], "critiqued")[0]["data"]["round"] == 2


def test_a_clean_reading_holds_nothing_back(store: Store):
    task = store.create_task("plan it", status="running", source="locks", repo="r", kind="plan")
    clean = critique.read(Reader(), plan_spec(), REQUEST, PARTS, CONTRACT, store=store, task_id=task["id"], nth=1,
                          nth_round=1)
    assert clean.findings == () and clean.verdict == "pass"
    data = store.events_of(task["id"], "critiqued")[0]["data"]
    assert data["findings"] == [] and data["held"] is False


@pytest.mark.parametrize("broken", [
    launch.WorkerResult(False, Failure.ENVIRONMENT, "the session errored", transient=True, cost_usd=0.02,
                        findings=(found(),)),
    launch.WorkerResult(False, Failure.INCONCLUSIVE, "no block", shape="its answer did not hold exactly one "
                        "result block", findings=(found(),)),
], ids=["transient", "no block"])
def test_a_reader_that_broke_gives_no_findings_and_the_plan_goes_on(store: Store, broken):
    task = store.create_task("plan it", status="running", source="locks", repo="r", kind="plan")
    got = critique.read(Reader(broken), plan_spec(), REQUEST, PARTS, CONTRACT, store=store, task_id=task["id"],
                        nth=1, nth_round=1)
    assert got.findings == ()
    data = store.events_of(task["id"], "critiqued")[0]["data"]
    assert data["findings"] == [] and data["held"] is False and data["cost_usd"] == round(broken.cost_usd, 4)


def test_the_findings_are_read_as_findings_are(store: Store):
    task = store.create_task("plan it", status="running", source="locks", repo="r", kind="plan")
    raw = {"where": "writer", "what": "  the   file's place is left open ", "evidence_class": "deterministic"}
    got = critique.read(Reader(launch.WorkerResult(True, None, "ok", "read", findings=(raw, {"what": ""}))),
                        plan_spec(), REQUEST, PARTS, CONTRACT, store=store, task_id=task["id"], nth=1, nth_round=1)
    # No proof: a claim of "deterministic" without one is read from the code. A row with no `what` is none.
    assert got.findings == ({"where": "writer", "what": "the file's place is left open",
                             "evidence_class": "inferential", "cause": "unknown", "proof": ""},)


def test_the_plan_goes_back_with_the_findings_one_line_each():
    said = critique.shape([found("writer", "the file path is left to its worker"), found("request", "S2 is answered "
                                                                                                    "by no part")])
    assert said == ("critique: writer: the file path is left to its worker\n"
                    "request: S2 is answered by no part")
    assert said.startswith(critique.SHAPE) and "rewrite the parts" in critique.PROMPT


# --- when it runs ---------------------------------------------------------------------------------------------


def test_only_a_plan_of_two_parts_or_more_on_a_start_nobody_pinned_is_read(monkeypatch):
    monkeypatch.setenv("LOCKS_CRITIQUE", "on")
    assert critique.due(PARTS, pinned=False)
    assert not critique.due(PARTS[:1], pinned=False)  # a plan of one part is the step run whole
    assert not critique.due((), pinned=False)
    assert not critique.due(PARTS, pinned=True)


def test_the_setting_off_reads_nothing(monkeypatch):
    monkeypatch.setenv("LOCKS_CRITIQUE", "off")
    assert not critique.enabled() and not critique.due(PARTS, pinned=False)
    assert critique.due(PARTS, pinned=False, env={"LOCKS_CRITIQUE": "on"})  # an environment passed wins
    assert not critique.due(PARTS, pinned=False, env={"LOCKS_CRITIQUE": "0"})


def test_it_is_on_by_default(monkeypatch):
    monkeypatch.delenv("LOCKS_CRITIQUE", raising=False)
    assert critique.enabled() and critique.due(PARTS, pinned=False)


# --- the trial ------------------------------------------------------------------------------------------------


def plan(store: Store, arm: str | None, later: list[bool], *, repo: str = "r", parts: int = 2,
         plan_passed: bool = True, before: list[bool] = ()) -> int:
    """A plan task in a workflow run: its brief's arm, its ending (with `parts` parts), a step before it for
    each entry of `before` and one after it for each entry of `later`, whose first try passed or not."""
    run = store.create_run(name="w", scope="project", source="x", definition="{}")
    for passed in before:
        early = store.create_task("explore", status="done", source="locks", repo=repo, kind="explore",
                                  workflow_run=run["id"])
        store.add_attempt(early["id"], cell="sonnet/high", max_turns=10, passed=int(passed), input_tokens=5)
    task = store.create_task("plan it", status="done", source="locks", repo=repo, kind="plan",
                             workflow_run=run["id"])
    store.add_attempt(task["id"], cell="sonnet/high", max_turns=10, passed=int(plan_passed), input_tokens=5)
    if arm is not None:
        store.add_event(task["id"], "briefed", critique_arm=arm, critique_why="on trial")
    store.add_event(task["id"], "finished", status="done",
                    parts=[{"id": f"p{i}", "prompt": "x", "surfaces": [f"d{i}/**"]} for i in range(parts)])
    for passed in later:
        step = store.create_task("build a part", status="done", source="locks", repo=repo, kind="implement",
                                 workflow_run=run["id"])
        store.add_attempt(step["id"], cell="sonnet/high", max_turns=10, passed=int(passed), input_tokens=5)
        if not passed:
            store.add_attempt(step["id"], cell="opus/high", max_turns=10, passed=1, input_tokens=5)
    return int(task["id"])


def test_the_trial_counts_the_first_tries_of_the_steps_after_each_plan(store: Store):
    plan(store, "with", [True, False])
    plan(store, "without", [True, True, True])
    plan(store, "with", [True])
    found = critique.trial(store, "r")
    assert found["with"] == {"tasks": 2, "tries": 3, "passed": 2, "rate": 0.667, "pass_p50_s": None}
    assert found["without"] == {"tasks": 1, "tries": 3, "passed": 3, "rate": 1.0, "pass_p50_s": None}
    assert set(found) == set(critique.ARMS)


def test_a_step_before_its_plan_a_queued_step_and_other_runs_are_no_evidence(store: Store):
    plan(store, "with", [], before=[True])  # a step before its plan is read by nothing after it
    task_id = plan(store, "with", [False], before=[True, True])
    run = store.get_task(task_id)["workflow_run"]
    store.create_task("still queued", status="queued", source="locks", repo="r", kind="implement", workflow_run=run)
    plan(store, "without", [True, True])  # another run's steps are no evidence for these
    found = critique.trial(store, "r")
    assert found["with"] == {"tasks": 2, "tries": 1, "passed": 0, "rate": 0.0, "pass_p50_s": None}
    assert found["without"]["tries"] == 2


def test_a_plan_of_one_part_or_with_no_ending_is_in_no_arm(store: Store):
    plan(store, "with", [True, True], parts=1)
    plan(store, "without", [True], parts=0)
    unfinished = store.create_task("plan it", status="running", source="locks", repo="r", kind="plan")
    store.add_attempt(unfinished["id"], cell="sonnet/high", max_turns=10, passed=1, input_tokens=5)
    store.add_event(unfinished["id"], "briefed", critique_arm="with")
    found = critique.trial(store, "r")
    assert found["with"]["tasks"] == 0 and found["without"]["tasks"] == 0


def test_a_task_of_another_trial_or_another_repository_is_not_this_one_s(store: Store):
    plan(store, "with", [True], repo="elsewhere")
    plan(store, None, [True])
    other = store.create_task("bug", status="done", source="locks", repo="r", kind="debug-unclear")
    store.add_attempt(other["id"], cell="opus/high", max_turns=10, passed=1, input_tokens=5)
    store.add_event(other["id"], "briefed", method_arm="diagnosis:with")
    assert critique.trial(store, "r")["with"]["tasks"] == 0
    assert critique.trial(store, None)["with"]["tasks"] == 1  # None: every repository


def test_the_arms_alternate_during_the_trial(store: Store):
    ids = {critique.ARMS[arms.side("critique", "r", n, 0, 0)]: n for n in range(1, 30)}
    assert critique.arm(store, "r", ids["with"])[0] == "with"  # balanced: the hash of the trial and the task
    assert critique.arm(store, "r", ids["without"])[0] == "without"
    plan(store, "with", [True])
    plan(store, "with", [True])
    side, why = critique.arm(store, "r", ids["with"])
    assert side == "without" and why == "the critique trial here (2 with, 0 without, 6 each)"  # two apart: fewer next
    for _ in range(4):
        plan(store, "without", [True])
    assert critique.arm(store, "r", ids["without"])[0] == "with"


def test_after_the_trial_the_reader_stays_only_where_the_steps_after_it_passed_no_less_often(store: Store):
    for n in range(critique.TRIAL):
        plan(store, "with", [True])
        plan(store, "without", [n < 3])
    side, why = critique.arm(store, "r", 99)
    assert side == "with" and why == "the critique won its trial here: first tries passed 100% with and 50% without"
    for _ in range(3):  # 6 of 9 with it against 3 of 6 without: still no worse
        plan(store, "with", [False])
    assert critique.arm(store, "r", 99)[0] == "with"
    for _ in range(5):  # 6 of 14 with it against 3 of 6 without: it lost
        plan(store, "with", [False])
    side, why = critique.arm(store, "r", 99)
    assert side == "without" and why.startswith("the critique lost its trial here: first tries passed")


def test_it_is_judged_by_the_steps_after_the_plan_never_by_the_plan_s_own_pass(store: Store):
    for _ in range(critique.TRIAL):
        plan(store, "with", [False], plan_passed=True)
        plan(store, "without", [True], plan_passed=False)
    assert critique.arm(store, "r", 99)[0] == "without"


def test_a_repository_short_of_the_trial_takes_the_pooled_verdict_and_says_so(store: Store):
    for n in range(critique.TRIAL):
        plan(store, "with", [True], repo="other")
        plan(store, "without", [n < 2], repo="other")
    plan(store, "with", [True], repo="r")
    side, why = critique.arm(store, "r", 99)
    assert side == "with" and "won its trial everywhere" in why
    assert "pooled from every repository while the trial here has 1 with, 0 without, 6 each" in why
    # Once its own arms are full they decide alone.
    for _ in range(critique.TRIAL):
        plan(store, "with", [False], repo="r")
        plan(store, "without", [True], repo="r")
    side, why = critique.arm(store, "r", 99)
    assert side == "without" and "lost its trial here" in why and "pooled" not in why


def test_pooled_arms_that_are_short_too_leave_the_draw_to_the_trial(store: Store):
    plan(store, "with", [True], repo="other")
    side, why = critique.arm(store, "r", 99)
    assert side in critique.ARMS and why == "the critique trial here (0 with, 0 without, 6 each)"


def test_no_repository_runs_no_trial(store: Store):
    assert critique.arm(store, None, 1) == ("with", "no repository to run a trial in")


def test_a_resume_keeps_the_arm_its_first_brief_drew(store: Store):
    drawn = plan(store, "without", [])
    store.add_event(drawn, "briefed", critique_arm="with")  # a later brief of the same task
    assert critique.arm_for(store, "r", drawn) == ("without", "kept from its first brief")
    assert critique.arm_for(store, None, drawn) == ("without", "kept from its first brief")
    fresh = store.create_task("plan it", status="queued", source="locks", repo="r", kind="plan")
    store.add_event(fresh["id"], "briefed", method_arm="premortem:with")  # a brief that drew no critique arm
    assert critique.arm_for(store, "r", fresh["id"]) == critique.arm(store, "r", fresh["id"])
    assert critique.arm_for(store, None, fresh["id"])[0] == "with"
