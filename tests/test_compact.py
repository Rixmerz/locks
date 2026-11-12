from __future__ import annotations

import json
from pathlib import Path

from locks import compact, hooks
from locks.store import Store


def user(text, **extra):
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def transcript(tmp_path: Path, entries) -> Path:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n")
    return path


ENTRIES = [
    user("fix the login redirect, keep `?next=`"),
    user("<command-name>/model</command-name>"),
    user("Caveat: harness text"),
    user("[Request interrupted by user]"),
    user("/compact"),
    user("The locks plugin sent a message: locks: work ended"),
    user("summary", isCompactSummary=True),
    user([{"type": "text", "text": "meta"}], isMeta=True),
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "a", "name": "Edit", "input": {"file_path": "/r/app.py"}},
        {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "npm test"}},
        {"type": "tool_use", "id": "c", "name": "Bash", "input": {"command": "rm -rf x"}},
    ]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "b", "is_error": True,
         "content": [{"type": "text", "text": "Exit code 1\nTypeError: x is undefined"}]},
        {"type": "tool_result", "tool_use_id": "c", "is_error": True,
         "content": "The user doesn't want to proceed with this tool use."},
    ]}},
    user([{"type": "text", "text": "and add a test"}]),
]


def test_read_keeps_what_the_person_typed_and_what_broke(tmp_path):
    facts = compact.read(transcript(tmp_path, ENTRIES))
    assert facts["requests"] == ["fix the login redirect, keep `?next=`", "and add a test"]
    assert facts["written"] == ["/r/app.py"]
    assert facts["failures"] == ["Bash `npm test`: Exit code 1 TypeError: x is undefined"]


def test_digest_fits_its_budget_and_keeps_the_newest_requests():
    facts = {"requests": [f"request {i} " + "x" * 300 for i in range(40)], "written": [], "failures": []}
    text = compact.digest(facts, changed=["a.py"], tasks=[{"id": 3, "status": "done", "title": "t"}], budget=3000)
    assert len(text) <= 3000
    assert "request 39" in text and "request 0 " not in text and "earlier left out" in text
    assert "- a.py" in text and "#3 [done] t" in text


def test_nothing_to_keep_is_no_digest():
    assert compact.digest({"requests": [], "written": [], "failures": []}, changed=[], tasks=[]) == ""


def test_an_id_that_is_no_file_name_is_refused(tmp_path):
    assert compact.path(tmp_path, "../x") is None
    assert compact.recall(tmp_path, "../x") is None
    assert compact.keep(tmp_path, "../x", tmp_path / "t.jsonl", None, []) is None


def test_precompact_keeps_and_the_session_after_it_gets_it_back(store: Store, tmp_path, git_repo):
    (git_repo / "new.py").write_text("x")
    event = {"session_id": "s1", "transcript_path": str(transcript(tmp_path, ENTRIES)), "cwd": str(git_repo)}
    hooks.handle("PreCompact", event, store, tmp_path, {})
    kept = compact.recall(tmp_path, "s1")
    assert kept and "fix the login redirect" in kept and "- new.py" in kept
    answer = hooks.session_start({"session_id": "s1", "source": "compact", "cwd": str(git_repo)}, store,
                                 tmp_path, {})
    assert "locks compact:" in answer["hookSpecificOutput"]["additionalContext"]
    fresh = hooks.session_start({"session_id": "s1", "source": "startup", "cwd": str(git_repo)}, store,
                                tmp_path, {})
    assert fresh is None or "locks compact:" not in fresh["hookSpecificOutput"]["additionalContext"]



def test_a_compaction_record_that_fails_costs_neither_the_digest_nor_its_restore(store: Store, tmp_path, git_repo,
                                                                                 monkeypatch):
    from locks import compaction

    def locked(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(compaction, "seen", locked)
    event = {"session_id": "s1", "transcript_path": str(transcript(tmp_path, ENTRIES)), "cwd": str(git_repo)}
    hooks.handle("PreCompact", event, store, tmp_path, {})
    assert "fix the login redirect" in (compact.recall(tmp_path, "s1") or "")
    answer = hooks.session_start({"session_id": "s1", "source": "compact", "cwd": str(git_repo)}, store,
                                 tmp_path, {})
    assert "locks compact:" in answer["hookSpecificOutput"]["additionalContext"]
    log = (tmp_path / hooks.ERROR_LOG).read_text()
    assert "PreCompact (compaction)" in log and "SessionStart (compaction)" in log

def test_no_transcript_keeps_nothing(store: Store, tmp_path):
    assert hooks.keep_digest({"session_id": "s1"}, store, tmp_path) is None
    assert hooks.keep_digest({"session_id": "s1", "transcript_path": str(tmp_path / "none")}, store,
                             tmp_path) is None


def test_git_that_cannot_run_is_no_change(tmp_path):
    assert compact._changed(tmp_path / "missing") == []


def test_a_secret_is_masked():
    facts = {"requests": ["use password=hunter2 for it"], "written": [], "failures": []}
    text = compact.digest(facts, changed=[], tasks=[])
    assert "hunter2" not in text and "[secret]" in text


def _call(call_id: str, command: str) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]}}


def _result(call_id: str, text: str, *, error: bool) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": call_id, "is_error": error, "content": text}]}}


def test_a_failure_the_same_command_later_passed_is_left_out(tmp_path):
    entries = [_call("a", "npm test"), _result("a", "boom", error=True),
               _call("b", "make lint"), _result("b", "lint boom", error=True),
               _call("c", "npm test"), _result("c", "ok", error=False)]
    assert compact.read(transcript(tmp_path, entries))["failures"] == ["Bash `make lint`: lint boom"]
    # a failure after the pass is still a failure
    again = [*entries, _call("d", "npm test"), _result("d", "boom again", error=True)]
    assert compact.read(transcript(tmp_path, again))["failures"][-1] == "Bash `npm test`: boom again"


def test_each_failure_is_clipped_and_the_section_takes_a_fifth_of_the_budget_saying_the_rest_by_count(tmp_path):
    entries = []
    for i in range(30):
        entries += [_call(f"x{i}", f"cmd {i}"), _result(f"x{i}", f"harness text {i} " + "long " * 200, error=True)]
    facts = compact.read(transcript(tmp_path, entries))
    assert all(len(f) <= len("Bash ``: ") + compact.COMMAND_CHARS + compact.ERROR_CHARS + 8 for f in facts["failures"])
    text = compact.digest({**facts, "requests": ["ask"]}, changed=[], tasks=[])
    section = text.split("Calls that failed")[1].split("\n\n")[0]
    assert len(section) <= compact.BUDGET // 5 and "earlier left out" in section
    assert "cmd 29" in section and "cmd 0 " not in section
