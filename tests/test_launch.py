from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from locks import launch
from locks.escalate import Failure
from locks.launch import LaunchSpec, build_argv, parse, result_block
from locks.matrix import Cell

from .conftest import git


def spec(tmp_path: Path, **kw) -> LaunchSpec:
    return LaunchSpec(prompt="do it", cell=kw.pop("cell", Cell("sonnet", "high")), launch_dir=tmp_path, **kw)


def block(**fields) -> str:
    return "done.\n```locks-result\n" + json.dumps(fields) + "\n```"


def envelope(text: str = "", **extra) -> str:
    return json.dumps({"is_error": False, "result": text, "total_cost_usd": 0.12, "num_turns": 4,
                       "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 7}, **extra})


def proc(stdout: str, code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["claude"], code, stdout, stderr)


def test_a_pinned_model_id_replaces_the_alias_and_the_served_model_is_read_from_the_cli(tmp_path):
    assert build_argv(spec(tmp_path))[build_argv(spec(tmp_path)).index("--model") + 1] == "sonnet"
    pinned = build_argv(spec(tmp_path, model_id="claude-sonnet-5-5"))
    assert pinned[pinned.index("--model") + 1] == "claude-sonnet-5-5"
    usage = {"claude-sonnet-5-5": {"outputTokens": 700}, "claude-haiku-4-5": {"outputTokens": 30}}
    out = parse(proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed"), modelUsage=usage)),
                spec(tmp_path))
    assert out.served_model == "claude-sonnet-5-5"
    assert parse(proc(envelope(block(verdict="pass", summary="ok", evidence="1"))), spec(tmp_path)).served_model == ""


def test_argv_carries_every_ceiling_and_restriction(tmp_path):
    work = tmp_path / "work"
    s = spec(tmp_path, workdir=work, max_turns=12, tools=("Read", "Bash"),
             disallowed_tools=("Edit", "Write"), append_system_prompt="hint")
    argv = build_argv(s, mcp_config_path=tmp_path / "m.json")
    def val(flag):
        return argv[argv.index(flag) + 1]
    assert val("--model") == "sonnet" and val("--effort") == "high"
    assert val("--max-turns") == "12" and "--max-budget-usd" not in argv
    assert val("--tools") == "Read,Bash" and val("--disallowedTools") == "Edit,Write"
    assert val("--add-dir") == str(work) and val("--mcp-config") == str(tmp_path / "m.json")
    assert val("--append-system-prompt") == "hint"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv


def test_haiku_gets_its_effort_flag(tmp_path):
    """Haiku 5.5 has the dial; a cell recorded without one runs at its default."""
    argv = build_argv(spec(tmp_path, cell=Cell("haiku")))
    assert argv[argv.index("--effort") + 1] == "medium"
    assert "--add-dir" not in argv
    assert "--tools" not in argv


def test_a_pass_needs_evidence(tmp_path):
    s = spec(tmp_path)
    ok = parse(proc(envelope(block(verdict="pass", summary="fixed", evidence="3 passed"))), s)
    assert ok.passed and ok.evidence == "3 passed"
    assert (ok.cost_usd, ok.input_tokens, ok.output_tokens, ok.turns) == (0.12, 100, 7, 4)
    bare = parse(proc(envelope(block(verdict="pass", summary="fixed"))), s)
    assert not bare.passed and bare.failure is Failure.INCONCLUSIVE


@pytest.mark.parametrize(
    ("stdout", "failure"),
    [
        (envelope(block(verdict="fail", failure="approach", summary="wrong way")), Failure.APPROACH),
        (envelope(block(verdict="fail", failure="nonsense", summary="x")), Failure.CODE_BUG),
        (envelope(block(verdict="inconclusive", summary="x")), Failure.INCONCLUSIVE),
        (envelope(block(verdict="inconclusive", failure="spec_bug", summary="contradicts")), Failure.SPEC_BUG),
        (envelope(block(verdict="inconclusive", failure="code_bug", summary="x")), Failure.INCONCLUSIVE),
        (envelope("no block at all"), Failure.INCONCLUSIVE),
        (envelope(block(verdict="pass") + block(verdict="fail")), Failure.INCONCLUSIVE),
        (json.dumps({"is_error": True, "subtype": "error_max_turns"}), Failure.TURNS_EXHAUSTED),
        # locks sets no dollar cap; one set elsewhere reads as the session erroring.
        (json.dumps({"is_error": True, "subtype": "error_max_budget_usd"}), Failure.ENVIRONMENT),
        (json.dumps({"is_error": True, "subtype": "error_during_execution"}), Failure.ENVIRONMENT),
        ("not json", Failure.ENVIRONMENT),
    ],
)
def test_every_failure_is_typed(tmp_path, stdout, failure):
    result = parse(proc(stdout), spec(tmp_path))
    assert not result.passed and result.failure is failure


def test_only_an_answer_of_the_wrong_form_says_what_was_wrong_with_it(tmp_path):
    for unreadable in (envelope("no block at all"), envelope(block(verdict="pass") + block(verdict="fail"))):
        assert parse(proc(unreadable), spec(tmp_path)).shape == "its answer did not hold exactly one result block"
    planned = parse(proc(envelope(block(verdict="pass", summary="planned", evidence="read it", parts=[{"id": "x"}],
                                        contract="c"))), spec(tmp_path, parts=True))
    assert planned.shape.startswith("its plan cannot run in parts: ")
    # A pass with no evidence, or a failure the worker named, is about the work: no form to fix.
    for work in (envelope(block(verdict="pass", summary="fixed")),
                 envelope(block(verdict="inconclusive", summary="x"))):
        assert parse(proc(work), spec(tmp_path)).shape == ""


def test_an_errored_session_says_why_not_its_subtype(tmp_path):
    limited = json.dumps({"is_error": True, "subtype": "success", "result": "Claude AI usage limit reached"})
    assert parse(proc(limited), spec(tmp_path)).summary == "the session errored: Claude AI usage limit reached"
    bare = json.dumps({"is_error": True, "subtype": "success"})
    assert parse(proc(bare), spec(tmp_path)).summary == "the session errored: success"
    crashed = json.dumps({"is_error": True, "subtype": "error_during_execution", "result": ""})
    assert parse(proc(crashed), spec(tmp_path)).summary == "the session errored: error_during_execution"


def test_a_session_flagged_errored_that_answered_with_its_block_is_read(tmp_path):
    """The CLI's JSON said `is_error` with subtype "success" over a worker's full answer: four
    sessions were recorded as errored and retried although each had passed."""
    answered = json.loads(envelope(block(verdict="pass", summary="done", evidence="pytest: 12 passed")))
    answered.update({"is_error": True, "subtype": "success"})
    result = parse(proc(json.dumps(answered)), spec(tmp_path))
    assert result.passed and result.summary == "done"
    # A limit named beside a block is still the limit, and no block is still an errored session.
    limited = dict(answered, result=answered["result"] + "\nClaude AI usage limit reached")
    assert parse(proc(json.dumps(limited)), spec(tmp_path)).failure is Failure.ENVIRONMENT
    bare = dict(answered, result="API Error: 500")
    assert parse(proc(json.dumps(bare)), spec(tmp_path)).failure is Failure.ENVIRONMENT
    crashed = dict(answered, subtype="error_during_execution")
    assert parse(proc(json.dumps(crashed)), spec(tmp_path)).failure is Failure.ENVIRONMENT


def test_result_block_rejects_garbage():
    assert result_block("```locks-result\n[1]\n```") is None
    assert result_block("```locks-result\n{bad\n```") is None


def test_a_block_whose_strings_hold_a_fence_or_a_tab_is_still_read():
    evidence = "pytest -q:\n```\n3 passed\n```\nand\ta raw tab"
    raw = '{"verdict": "pass", "summary": "done", "evidence": ' + json.dumps(evidence).replace("\\t", "\t") + "}"
    block = result_block(f"Done.\n```locks-result\n{raw}\n```\n")
    assert block["verdict"] == "pass" and "3 passed" in block["evidence"] and "\t" in block["evidence"]
    # The same block repeated is one verdict; two that differ in anything are none, and a block that
    # never closes is read as far as its JSON goes.
    twice = result_block(f"```locks-result\n{raw}\n```\nAgain:\n```locks-result\n{raw}\n```")
    assert twice is not None and twice["summary"] == "done"
    other = raw.replace('"done"', '"done."')
    assert result_block(f"```locks-result\n{raw}\n```\n```locks-result\n{other}\n```") is None
    assert result_block(f"```locks-result\n{raw}\n```\n```locks-result\n{{bad\n```") is None
    assert result_block(f"```locks-result\n{raw}")["summary"] == "done"
    # Two verdicts in one block, or anything after its JSON, are no verdict either.
    failing = '{"verdict": "fail", "failure": "code_bug", "summary": "no"}'
    assert result_block(f"```locks-result\n{raw}\n{failing}\n```") is None
    assert result_block(f"```locks-result\n{raw}\n}}\n```") is None
    report ='```locks-report\n{"project": "x", "note": "a ```fenced``` word"}\n```'
    assert launch.report_block(report) == {"project": "x", "note": "a ```fenced``` word"}
    assert launch.report_block("```locks-report\nnot json") == {"unparsed": "not json"}


def test_run_reads_changes_from_git_and_cleans_its_mcp_file(git_repo):
    seen = {}

    def runner(argv, **kwargs):
        cfg = Path(argv[argv.index("--mcp-config") + 1])
        seen["mcp"] = json.loads(cfg.read_text())
        seen["cfg"] = cfg
        seen["input"] = kwargs["input"]
        seen["env"] = kwargs["env"]
        (git_repo / "new.py").write_text("x = 1\n")
        (git_repo / "app.py").write_text("print('changed')\n")
        git(git_repo, "add", "app.py")
        git(git_repo, "commit", "-q", "-m", "worker commit")
        return proc(envelope(block(verdict="pass", summary="ok", evidence="ok")))

    s = LaunchSpec("task", Cell("sonnet", "low"), git_repo, mcp_servers={"idx": {"command": "idx"}})
    result = launch.run(s, runner=runner)
    assert result.passed
    assert result.changed_paths == ("app.py", "new.py")
    assert seen["mcp"] == {"mcpServers": {"idx": {"command": "idx"}}}
    assert not seen["cfg"].exists() and git_repo not in seen["cfg"].parents
    assert "locks-result" in seen["input"] and seen["env"]["LOCKS_HOOKS_OFF"] == "1"


def test_run_timeout_and_missing_cli(tmp_path):
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    result = launch.run(spec(tmp_path, timeout_s=1), runner=slow)
    assert result.failure is Failure.ENVIRONMENT

    def missing(argv, **kwargs):
        raise FileNotFoundError("claude")

    with pytest.raises(launch.LaunchError):
        launch.run(spec(tmp_path), runner=missing)


def test_changes_outside_git_are_unknown(tmp_path):
    assert launch.tracked_state(tmp_path) is None
    assert launch.changed_paths(None, {}) == ()
    assert launch.describe(["a", "b" * 100]).endswith("...")


def test_granted_rules_reach_the_worker_one_argument_each(tmp_path):
    argv = build_argv(spec(tmp_path, allowed_tools=("Bash(npm run build)", "Bash(node:*)")))
    at = argv.index("--allowedTools")
    assert argv[at + 1:at + 3] == ["Bash(npm run build)", "Bash(node:*)"]
    assert "--allowedTools" not in build_argv(spec(tmp_path))
    argv = build_argv(spec(tmp_path, extra_dirs=(tmp_path / "node_modules",)))
    assert argv[argv.index("--add-dir") + 1] == str(tmp_path / "node_modules")


def test_a_refused_command_is_read_from_the_cli_not_the_worker(tmp_path):
    s = spec(tmp_path)
    refusals = [
        {"tool_name": "Bash", "tool_input": {"command": "npm   run build", "description": "build"}},
        {"tool_name": "Bash", "tool_input": {"command": "npm run build"}},
        {"tool_name": "Write", "tool_input": {"file_path": "src/app.ts"}},
        {"tool_name": "WebFetch", "tool_input": {}},
        {"tool_name": "Bash", "tool_input": {"command": "x" * 200}},
        "garbage", {"tool_input": {"command": "ls"}},
    ]
    said_done = block(verdict="inconclusive", failure="environment", summary="created the routes; build refused")
    result = parse(proc(envelope(said_done, permission_denials=refusals)), s)
    assert not result.passed and result.failure is Failure.PERMISSION and result.verdict == "inconclusive"
    assert result.denied[:3] == ("Bash(npm run build)", "Write(src/app.ts)", "WebFetch")
    assert len(result.denied) == 4 and result.denied[3].endswith("...)") and len(result.denied[3]) == 126
    assert result.allow == ("Bash(npm run build:*)", "Write(src/app.ts)", "WebFetch", f"Bash({'x' * 200}:*)")
    assert launch.allow_rules(None) == ()
    # no block at all, or a pass it could not show: the refusal still decides
    assert parse(proc(envelope("stopped", permission_denials=refusals[:1])), s).failure is Failure.PERMISSION
    bare = parse(proc(envelope(block(verdict="pass", summary="done"), permission_denials=refusals[:1])), s)
    assert bare.failure is Failure.PERMISSION and bare.verdict == "pass"
    # a task the worker found wrong is wrong whatever was refused
    wrong = block(verdict="fail", failure="spec_bug", summary="read-only task asks for files")
    assert parse(proc(envelope(wrong, permission_denials=refusals[:1])), s).failure is Failure.SPEC_BUG
    # a pass it could show stands: it found another way
    shown = block(verdict="pass", summary="done", evidence="ok")
    assert parse(proc(envelope(shown, permission_denials=refusals[:1])), s).passed
    assert launch.denials(None) == () and launch.denials({"permission_denials": None}) == ()


def test_work_in_checkouts_nested_in_the_work_directory_is_seen(tmp_path):
    """A task run in a folder that holds several repositories edited two of them;
    the folder is no checkout itself, and the report said it changed nothing."""
    from .conftest import git

    def checkout(path):
        path.mkdir(parents=True)
        git(path, "init", "-q")
        git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
        return path

    holder = tmp_path / "workspace"
    api, web = checkout(holder / "api"), checkout(holder / "apps" / "web")
    (holder / "node_modules" / "dep").mkdir(parents=True)
    (holder / "node_modules" / "dep" / ".git").mkdir()  # never looked into
    assert launch.nested_checkouts(holder) == [api, web]
    before = launch.tracked_state(holder)
    (api / "routes.js").write_text("x\n")
    (web / "app.ts").write_text("y\n")
    git(web, "add", "app.ts")
    git(web, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "worker committed")
    after = launch.tracked_state(holder)
    assert launch.changed_paths(before, after, holder) == ("api/routes.js", "apps/web/app.ts")

    # a checkout holding another one: the nested one is read, not listed as one untracked folder
    outer = checkout(tmp_path / "outer")
    inner = checkout(outer / "vendor" / "lib")
    before = launch.tracked_state(outer)
    (inner / "fix.py").write_text("z\n")
    assert launch.changed_paths(before, launch.tracked_state(outer), outer) == ("vendor/lib/fix.py",)
    assert launch.tracked_state(tmp_path / "outer" / "vendor") is not None
    empty = tmp_path / "plain"
    empty.mkdir()
    assert launch.tracked_state(empty) is None


def test_a_cli_that_does_not_take_the_mode_runs_the_attempt_in_the_old_one(tmp_path):
    calls = []

    def runner(argv, **kw):
        calls.append(argv[argv.index("--permission-mode") + 1])
        if len(calls) == 1:
            return proc("", 1, "error: option '--permission-mode <mode>' argument 'auto' is invalid.")
        return proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed")))

    out = launch.run(spec(tmp_path, permission_mode="auto"), runner=runner)
    assert calls == ["auto", "acceptEdits"] and out.passed and out.permission_mode == "acceptEdits"
    # any other failure is the attempt's own, not a reason to change mode
    calls.clear()
    failing = lambda argv, **kw: calls.append(1) or proc("", 1, "network down")  # noqa: E731
    assert launch.run(spec(tmp_path, permission_mode="auto"), runner=failing).failure is Failure.ENVIRONMENT
    assert calls == [1]


def test_what_the_worker_learned_is_read_from_its_block_and_kept_short(tmp_path):
    facts = ["Prices are stored in cents", "  Prices are stored in cents ", 7, "", *(f"fact {i}" for i in range(9)),
             "x" * 2000]
    out = parse(proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed", learned=facts))),
                spec(tmp_path))
    assert out.learned[0] == "Prices are stored in cents" and len(out.learned) == launch.MAX_LEARNED
    assert "fact 0" in out.learned and 7 not in out.learned
    assert launch.learned({"learned": ["y" * 2000]}) == ("y" * launch.LEARNED_CHARS,)
    assert launch.learned({"learned": "not a list"}) == () and launch.learned({}) == ()
    failed = parse(proc(envelope(block(verdict="fail", failure="code_bug", summary="no", learned=["A fact"]))),
                   spec(tmp_path))
    assert failed.learned == ("A fact",)  # read on any verdict; only a pass files it
    assert '"learned"' in launch.RESULT_INSTRUCTIONS


def test_a_refused_write_the_attempt_got_past_blocks_nothing(tmp_path):
    def refused(*paths):
        return json.dumps({"is_error": True, "subtype": "error_max_turns", "result": "", "permission_denials": [
            {"tool_name": "Write", "tool_input": {"file_path": p}} for p in paths]})

    # written after all, per git: the attempt ran out of turns, nothing more
    got_past = parse(proc(refused(str(tmp_path / "src/routes/a.ts"))), spec(tmp_path), changed=("src/routes/a.ts",))
    assert got_past.failure is Failure.TURNS_EXHAUSTED
    assert got_past.denied == ()
    assert got_past.allow == ()
    assert got_past.overcome == ("Write(src/routes/a.ts)",)
    # one written, one not: the one not written still blocks
    half = parse(proc(refused(str(tmp_path / "src/a.ts"), str(tmp_path / "src/b.ts"))), spec(tmp_path),
                 changed=("src/a.ts",))
    assert len(half.denied) == 1
    assert half.overcome == ("Write(src/a.ts)",)
    # outside the work directory, a changed path of the same name is no evidence
    outside = parse(proc(refused("/elsewhere/src/a.ts")), spec(tmp_path), changed=("src/a.ts",))
    assert outside.denied == ("Write(/elsewhere/src/a.ts)",)
    assert outside.overcome == ()


def test_only_a_launch_that_broke_is_transient(tmp_path):
    assert parse(proc("not json"), spec(tmp_path)).transient
    errored = json.dumps({"is_error": True, "subtype": "success", "result": "limit"})
    assert parse(proc(errored), spec(tmp_path)).transient
    named = parse(proc(envelope(block(verdict="fail", failure="environment", summary="old Node"))), spec(tmp_path))
    assert named.failure is Failure.ENVIRONMENT
    assert not named.transient


def test_changed_paths_leave_out_tool_artifacts():
    before = {"a.py": " M"}
    after = {"a.py": "M ", ".playwright-mcp/page.yml": "??", "web/.playwright-mcp/c.log": "??"}
    assert launch.changed_paths(before, after) == ("a.py",)


def test_the_commit_subject_a_worker_proposes_is_one_short_line():
    from locks.launch import commit_subject

    assert commit_subject({"commit": "  `Fix rounding of the total.`\nmore"}) == "Fix rounding of the total"
    assert commit_subject({"commit": 3}) == "" and commit_subject({}) == "" and commit_subject({"commit": " "}) == ""
    cut = commit_subject({"commit": "word, " * 30})
    assert len(cut) <= 72 and cut.endswith("…")


def test_a_timeout_after_the_work_changed_files_is_out_of_turns_not_the_environment(git_repo):
    def slow_but_working(argv, **kwargs):
        (git_repo / "app.py").write_text("changed\n")
        raise subprocess.TimeoutExpired(argv, 1)

    result = launch.run(spec(git_repo, timeout_s=1), runner=slow_but_working)
    assert result.failure is Failure.TURNS_EXHAUSTED and not result.transient
    assert result.changed_paths == ("app.py",) and "after changing 1 file(s)" in result.summary
    assert launch.timeout_for(30) == launch.DEFAULT_TIMEOUT_S and launch.timeout_for(120) == 7200


def test_a_worker_s_cache_is_counted_apart_and_forks_are_off_in_it(monkeypatch):
    usage = launch._usage({"total_cost_usd": 0.3, "num_turns": 4, "usage": {
        "input_tokens": 100, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 900,
        "output_tokens": 50}})
    assert usage["input_tokens"] == 10000 and usage["cache_read_tokens"] == 9000
    assert usage["cache_write_tokens"] == 900
    monkeypatch.setenv("CLAUDE_CODE_FORK_SUBAGENT", "1")
    assert launch.worker_env()["CLAUDE_CODE_FORK_SUBAGENT"] == "0"


def test_a_network_that_dropped_is_named_wherever_it_shows(tmp_path):
    lost = json.dumps({"is_error": True, "subtype": "success", "result": "API Error: Connection error."})
    assert parse(proc(lost), spec(tmp_path)).network == "Connection error"
    silent = parse(proc("", code=1, stderr="Error: getaddrinfo ENOTFOUND api.anthropic.com"), spec(tmp_path))
    assert silent.network.startswith("getaddrinfo ENOTFOUND") and silent.transient
    named = '{"verdict": "fail", "failure": "environment", "summary": "npm install failed", ' \
            '"evidence": "npm ERR! code ENOTFOUND registry.npmjs.org"}'
    worker = parse(proc(json.dumps({"result": f"```locks-result\n{named}\n```"})), spec(tmp_path))
    assert worker.failure is Failure.ENVIRONMENT and worker.network == "ENOTFOUND"
    # A usage limit is no network, and a test that prints a reset is no network either.
    limited = json.dumps({"is_error": True, "subtype": "success", "result": "Claude AI usage limit reached"})
    assert parse(proc(limited), spec(tmp_path)).network == ""
    assert launch.network_trouble("expected ECONNRESET to be retried") == ""
    # A worker's evidence is a command's output: a local server that is not up is no network.
    local = '{"verdict": "fail", "failure": "environment", "summary": "the e2e check could not reach the app", ' \
            '"evidence": "TypeError: fetch failed (connect ECONNREFUSED 127.0.0.1:3000)"}'
    assert parse(proc(json.dumps({"result": f"```locks-result\n{local}\n```"})), spec(tmp_path)).network == ""


# --- more turns continue the worker's own session ---------------------------------------


def test_a_resume_continues_the_session_with_a_short_prompt_and_keeps_the_contract(tmp_path):
    s = spec(tmp_path, resume_session="sess-1", persist_session=True)
    argv = build_argv(s)
    assert argv[argv.index("--resume") + 1] == "sess-1" and "--no-session-persistence" not in argv
    seen = []

    def runner(argv, **kw):
        seen.append((argv, kw["input"]))
        return proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed"), session_id="sess-1"))

    out = launch.run(s, runner=runner)
    assert out.passed and out.session_id == "sess-1" and out.resumed and out.fresh == ""
    said = seen[0][1]
    assert said.startswith("You ran out of turns") and "do it" not in said and launch.RESULT_FENCE in said
    # a fresh attempt is given its brief and keeps no session unless asked to
    fresh = build_argv(spec(tmp_path))
    assert "--resume" not in fresh and "--no-session-persistence" in fresh


def test_a_resume_is_told_the_output_that_failed_its_attempt(tmp_path):
    seen = []

    def runner(argv, **kw):
        seen.append(kw["input"])
        return proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed"), session_id="sess-1"))

    note = "The output that failed the last attempt, last lines:\nFAIL total is 3"
    launch.run(spec(tmp_path, resume_session="sess-1", persist_session=True, resume_note=note), runner=runner)
    said = seen[0]
    assert said.startswith(launch.CONTINUE_PROMPT) and note in said and launch.RESULT_FENCE in said
    assert said.index(note) < said.index(launch.RESULT_FENCE) and "do it" not in said


def test_a_session_the_cli_cannot_resume_starts_fresh_and_says_why(tmp_path):
    seen = []

    def runner(argv, **kw):
        seen.append(("--resume" in argv, kw["input"]))
        if "--resume" in argv:
            return proc("", 1, "No conversation found with session ID: sess-1")
        return proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed"), session_id="sess-2"))

    out = launch.run(spec(tmp_path, resume_session="sess-1", persist_session=True), runner=runner)
    assert [r for r, _ in seen] == [True, False] and seen[1][1].startswith("do it")
    assert out.passed and not out.resumed and out.session_id == "sess-2"
    assert out.fresh.startswith("the CLI could not resume session sess-1: No conversation found")


def test_more_effort_forks_the_session_at_its_new_effort_and_is_told_to_look_deeper(tmp_path):
    note = "The independent check found:\n- [introduced, deterministic] app.py:3: drops the last item"
    s = spec(tmp_path, resume_session="sess-1", persist_session=True, fork_session=True,
             resume_prompt=launch.DEEPER_PROMPT, resume_note=note, cell=Cell("sonnet", "xhigh"))
    argv = build_argv(s)
    assert argv[argv.index("--resume") + 1] == "sess-1" and "--fork-session" in argv
    assert argv[argv.index("--effort") + 1] == "xhigh"
    seen = []

    def runner(argv, **kw):
        seen.append((argv, kw["input"]))
        if "--resume" in argv:
            return proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed"), session_id="fork-1"))
        return proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed"), session_id="fresh-1"))

    out = launch.run(s, runner=runner)
    said = seen[0][1]
    assert said.startswith(launch.DEEPER_PROMPT) and note in said and launch.RESULT_FENCE in said
    assert "do it" not in said and out.resumed and out.session_id == "fork-1"
    # without --fork-session a resume continues the session itself (more turns)
    assert "--fork-session" not in build_argv(spec(tmp_path, resume_session="sess-1", persist_session=True))
    assert "--fork-session" not in build_argv(spec(tmp_path, fork_session=True))  # nothing to fork


def test_a_fork_the_cli_cannot_resume_starts_fresh_and_says_why(tmp_path):
    seen = []

    def runner(argv, **kw):
        seen.append("--fork-session" in argv)
        if "--resume" in argv:
            return proc("", 1, "No conversation found with session ID: sess-1")
        return proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed"), session_id="sess-2"))

    out = launch.run(spec(tmp_path, resume_session="sess-1", persist_session=True, fork_session=True,
                          resume_prompt=launch.DEEPER_PROMPT), runner=runner)
    assert seen == [True, False] and not out.resumed and out.session_id == "sess-2"
    assert out.fresh.startswith("the CLI could not resume session sess-1: No conversation found")


PARTS = [{"id": "api", "prompt": "Build the endpoint", "surfaces": ["src/api/**"]},
         {"id": "web", "prompt": "Build the page", "surfaces": ["src/web/**"]}]


def test_a_plan_that_runs_in_parts_is_read_strictly(tmp_path):
    asked = spec(tmp_path, parts=True)
    # The contract a plan step is given asks for its parts and their contract.
    assert "`parts`" in launch.instructions(asked) and "`contract`" in launch.instructions(asked)
    assert "`parts`" not in launch.instructions(spec(tmp_path))
    out = parse(proc(envelope(block(verdict="pass", summary="two parts", evidence="read the code", parts=PARTS,
                                    contract="GET /orders returns {id, total}"))), asked)
    assert out.passed and [p["id"] for p in out.parts] == ["api", "web"]
    assert out.parts[1]["surfaces"] == ["src/web/**"] and out.contract == "GET /orders returns {id, total}"
    # One part, when splitting does not pay: no surfaces or contract needed.
    one = parse(proc(envelope(block(verdict="pass", summary="one", evidence="e",
                                    parts=[{"id": "all", "prompt": "Build it"}]))), asked)
    assert one.passed and one.parts == ({"id": "all", "prompt": "Build it", "surfaces": []},) and one.contract == ""
    # A part that builds on an earlier one says so: it is kept with the part.
    ordered = parse(proc(envelope(block(verdict="pass", summary="planned", evidence="e", contract="c",
                                        parts=[PARTS[0], {**PARTS[1], "after": "api"}]))), asked)
    assert ordered.passed and ordered.parts[1]["after"] == "api" and "after" not in ordered.parts[0]
    assert '"after"' in launch.PARTS_INSTRUCTIONS and "Read only what the split needs" in launch.PARTS_INSTRUCTIONS
    # A thorough plan is never thrown away for its length: the plan is not told of one, and its
    # parts reach their workers on stdin.
    long = parse(proc(envelope(block(verdict="pass", summary="planned", evidence="e", contract="c" * 20_000,
                                     parts=[PARTS[0], {**PARTS[1], "prompt": "x" * 9_000}]))), asked)
    assert long.passed and len(long.parts[1]["prompt"]) == 9_000 and len(long.contract) == 20_000
    # A step not asked for parts ignores them.
    assert parse(proc(envelope(block(verdict="pass", summary="s", evidence="e", parts=PARTS))),
                 spec(tmp_path)).parts == ()


@pytest.mark.parametrize("parts, contract, said", [
    (None, "c", "it gave no parts"),
    ([], "c", "it gave no parts"),
    ([*PARTS, *({"id": f"p{i}", "prompt": "x", "surfaces": [f"p{i}/**"]} for i in range(9))], "c",
     "11 parts; a plan splits into at most 10"),
    ([PARTS[0], {**PARTS[1], "after": "nope"}], "c", "part 'web': its after names 'nope', which is no part listed"),
    ([{**PARTS[0], "after": "web"}, PARTS[1]], "c", "part 'api': its after names 'web'"),
    ([PARTS[0], {**PARTS[1], "surfaces": ["src/**"]}], "c",
     "its parts' surfaces overlap: api's src/api/** and web's src/**"),
    ([PARTS[0], {**PARTS[1], "surfaces": []}], "c", "part 'web' declares no surfaces"),
    ([PARTS[0], {**PARTS[1], "id": "api"}], "c", "two parts are called 'api'"),
    ([PARTS[0], {**PARTS[1], "prompt": " "}], "c", "part 'web' has no prompt"),
    ([PARTS[0], {**PARTS[1], "id": "Web Page"}], "c", "part 2 has no id"),
    ([PARTS[0], {**PARTS[1], "surfaces": ["../up"]}], "c", "part 'web': a surface stays inside the project"),
    ([PARTS[0], "web"], "c", "part 2 is not an object"),
    (PARTS, None, "it gave no contract"),
])
def test_a_plan_with_no_parts_overlapping_ones_or_too_many_is_inconclusive(tmp_path, parts, contract, said):
    fields = {k: v for k, v in {"parts": parts, "contract": contract}.items() if v is not None}
    out = parse(proc(envelope(block(verdict="pass", summary="planned", evidence="read it", **fields))),
                spec(tmp_path, parts=True))
    assert not out.passed and out.failure is Failure.INCONCLUSIVE and out.parts == ()
    assert said in out.summary and out.summary.startswith("claimed a pass, but its plan cannot run in parts")


def test_what_the_worker_needed_to_get_oriented_is_read_from_its_block_and_kept_short(tmp_path):
    lines = ["src/export.py holds the writer", " src/export.py  holds the writer", 3, "",
             *(f"line {i}" for i in range(9)), "z" * 900]
    out = parse(proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed", orientation=lines))),
                spec(tmp_path))
    assert out.orientation[0] == "src/export.py holds the writer" and len(out.orientation) == launch.MAX_ORIENTATION
    assert launch.orientation({"orientation": ["w" * 900]}) == ("w" * launch.ORIENTATION_CHARS,)
    assert launch.orientation({"orientation": "no"}) == () and launch.orientation({}) == ()
    assert '"orientation"' in launch.RESULT_INSTRUCTIONS


def test_a_lean_worker_gets_no_subagent_tool_nor_skill_listing_and_only_flags_its_cli_knows(tmp_path, monkeypatch):
    from locks import launch

    monkeypatch.setenv("LOCKS_LEAN", "on")
    knows_all = "  --disable-slash-commands\n  --exclude-dynamic-system-prompt-sections\n"
    lean = launch.lean_flags("claude", os.environ, help_text=lambda b: knows_all)
    assert lean == launch.LEAN_FLAGS
    older = launch.lean_flags("claude", os.environ, help_text=lambda b: "  --disable-slash-commands\n")
    assert older == ("--disable-slash-commands",)
    assert launch.lean_flags("claude", os.environ, help_text=lambda b: "") == ()
    argv = build_argv(spec(tmp_path), lean=lean)
    assert argv[argv.index("--disallowedTools") + 1] == "Agent,Task"
    assert "--disable-slash-commands" in argv and "--exclude-dynamic-system-prompt-sections" in argv
    assert "--disallowedTools" not in build_argv(spec(tmp_path))
    monkeypatch.setenv("LOCKS_LEAN", "off")
    assert launch.lean_flags("claude", os.environ, help_text=lambda b: knows_all) == ()


def test_setting_sources_reach_the_argv_only_when_asked_and_only_for_a_cli_that_lists_the_flag(tmp_path):
    assert "--setting-sources" not in build_argv(spec(tmp_path))
    argv = build_argv(spec(tmp_path, setting_sources=("user",)))
    assert argv[argv.index("--setting-sources") + 1] == "user"
    listed = "  --setting-sources <sources>  Comma-separated list of setting sources\n"
    assert launch.setting_sources_flag("claude", os.environ, help_text=lambda b: listed) == ("--setting-sources",)
    assert launch.setting_sources_flag("claude", os.environ, help_text=lambda b: "  --model <model>\n") == ()
    assert launch.setting_sources_flag("claude", os.environ, help_text=lambda b: "") == ()


def test_run_passes_setting_sources_only_to_a_cli_whose_help_lists_it(tmp_path, monkeypatch):
    seen = []

    def runner(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    for said, expected in (("--setting-sources <s>", True), ("--model", False)):
        seen.clear()
        monkeypatch.setattr(launch, "_cli_help", lambda b, said=said: said)
        launch.run(spec(tmp_path, setting_sources=("user",)), runner=runner)
        assert ("--setting-sources" in seen[0]) is expected


def test_the_account_s_usage_limit_is_read_with_when_it_resets():
    from datetime import UTC, datetime

    now = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)  # 12:00 in Santiago
    kind, wait = launch.api_limit("Claude AI usage limit reached|1792771200")
    assert kind == "usage" and wait is not None
    assert launch.api_limit("Session limit reached ∙ resets 5pm")[0] == "usage"
    assert launch.api_limit("You've hit your limit · resets 3am (America/Santiago)")[0] == "usage"
    assert launch.resets_in("5-hour limit reached ∙ resets 5pm (America/Santiago)", now=now) == 5 * 3600
    assert launch.resets_in("resets at 11:30am (UTC)", now=now) == 20.5 * 3600  # tomorrow's
    assert launch.resets_in("Weekly limit reached ∙ resets Oct 27, 9am (UTC)", now=now) == (3 * 24 + 18) * 3600
    assert launch.resets_in("resets 25pm", now=now) is None and launch.resets_in("no reset named") is None
    assert launch.resets_in("resets 9am (Mars/Olympus)", now=now) is not None  # an unknown zone: the machine's
    # A rate limit is no usage limit, and keeps its retry-after.
    assert launch.api_limit("API Error: 429 rate_limit_error retry-after: 30") == ("rate_limit", 30.0)


def test_the_rules_a_failing_check_taught_are_read_from_the_block(tmp_path):
    rule = "Gift card lines are never discounted (CI: POL-7)"
    rules = [rule, "", 4, rule]
    out = parse(proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed", constraints=rules))),
                spec(tmp_path))
    assert out.constraints == ("Gift card lines are never discounted (CI: POL-7)",)
    assert parse(proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed"))), spec(tmp_path)).constraints \
        == ()
    assert '"constraints"' in launch.RESULT_INSTRUCTIONS


def _transcript(tmp_path: Path, turns: list[tuple[str, str]]) -> dict[str, str]:
    """A worker's session as Claude Code keeps it: each (prompt, answer) a turn."""
    folder = tmp_path / "cfg" / "projects" / "worktree"
    folder.mkdir(parents=True)
    lines = []
    for prompt, answer in turns:
        lines.append({"type": "user", "message": {"content": prompt}})
        lines.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}})
        lines.append({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}})
        lines.append({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}})
    (folder / "s1.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}


def _answer(verdict: str) -> str:
    return f"Done.\n\n```{launch.RESULT_FENCE}\n" + json.dumps({"verdict": verdict, "summary": "s"}) + "\n```"


NOTICE = "<task-notification> <summary>Monitor event: suite finished</summary></task-notification>"


def test_an_answer_followed_only_by_background_notices_is_read(tmp_path):
    """7 of 7 unreadable answers here: the block was given, then a monitor's notice came and
    the worker's reply to it ("the result stands") became the last message."""
    env = _transcript(tmp_path, [("the task", _answer("pass")), (NOTICE, "The result stands."),
                                 (NOTICE, "Still the same.")])
    assert launch.result_block(launch.answered_before_notices("s1", env))["verdict"] == "pass"


def test_a_worker_that_changed_its_answer_on_a_notice_is_read_by_its_latest_block(tmp_path):
    env = _transcript(tmp_path, [("the task", _answer("pass")), (NOTICE, _answer("fail")), (NOTICE, "done")])
    assert launch.result_block(launch.answered_before_notices("s1", env))["verdict"] == "fail"


@pytest.mark.parametrize("turns", [
    [("the task", "Waiting for the monitor."), (NOTICE, "Still waiting.")],  # no block was ever given
    [("the task", _answer("fail")), ("Look deeper.", "Fixed it.")],  # a prompt came after it
])
def test_anything_but_notices_after_the_answer_reads_nothing(tmp_path, turns):
    assert launch.answered_before_notices("s1", _transcript(tmp_path, turns)) is None
    assert launch.answered_before_notices("missing", {"CLAUDE_CONFIG_DIR": str(tmp_path / "none")}) is None


def test_parse_reads_the_answer_a_background_notice_pushed_off_the_end(tmp_path, monkeypatch):
    env = _transcript(tmp_path, [("the task", _answer("inconclusive")), (NOTICE, "The result stands.")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", env["CLAUDE_CONFIG_DIR"])
    out = parse(proc(envelope("The result stands.", session_id="s1")), spec(tmp_path))
    assert out.summary == "s" and not out.shape
