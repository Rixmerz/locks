"""What locks reads from the outside (a worker's answer, a check's output, a request, a CI file, a
tool call) never breaks it: each reader is fed text built from the fragments that broke readers
before (fences, half JSON, brackets left open, ANSI, unicode, a secret-shaped token, a very long
line) and holds its contract: it answers, in its shape, and never fails open. Seeded, so a failure
is the same failure again; stdlib only."""
from __future__ import annotations

import contextlib
import json
import random
import re

import pytest

from locks import classify, criteria, launch, points, risk, route, signature, text
from locks.matrix import KINDS

FRAGMENTS = [
    "```locks-result\n", "```\n", "```json\n", '{"verdict": "pass"', '"summary": "done"}', "}", "{", "[", "]",
    '"criteria": [{"id": "S1", "met": true, "evidence": "ok"}]', '"parts": [{"id": "a", "prompt": "p", "surfaces":',
    '["src/**"]}]', '"findings": [{"where": "a.py:3", "what": "x"}]', "(line 197", "[claude-", "…", "\x1b[31m",
    "\x1b[0m", "FAILED tests/test_x.py::test_y - AssertionError", "Error: what", "Traceback (most recent call last):",
    "  File \"x.py\", line 1", "npm ERR! missing script: test", "exit 127", "1. first item\n", "- second item\n",
    "Out of scope:\n", "S1 ", "#fable ", "#plan ", "Fix ", "Implement ", "why does it fail?", "¿por qué falla?",
    "ñandú ", "日本語 ", "🙂", "\t", "\r\n", "\n\n", " ", "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789",
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "https://user:pw@host/x", "payment ", "checkout ", "migrate ",
    "src/locks/orchestrate.py ", "a" * 300, "\x00", "\\", '"', "'", "$(rm -rf /)", "`id`", "; ls", "| tee", "&&",
]

CASES = 400


def texts(seed: int, n: int = CASES):
    rng = random.Random(seed)
    for _ in range(n):
        yield "".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(0, 25)))


def test_a_worker_s_answer_is_a_block_or_nothing_and_never_a_crash():
    for said in texts(1):
        block = launch.result_block(said)
        assert block is None or isinstance(block, dict)
        if isinstance(block, dict):
            launch.read_parts(block)
            launch.notes_used(block)
            criteria.read(block)


def test_a_check_s_output_is_read_into_failures_and_a_bounded_tail():
    for said in texts(2):
        failing = launch.check_failures(said)
        assert isinstance(failing, frozenset) and all(isinstance(f, str) for f in failing)
        tail = launch.check_tail(said)
        assert len(tail) <= launch.CHECK_TAIL_CHARS + 200
        assert "\x1b[" not in tail
        assert "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789" not in tail
        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in tail


def test_a_request_is_read_for_its_kind_risk_size_way_and_criteria():
    for said in texts(3):
        found = classify.classify(said, use_model=False)
        assert found.kind in KINDS
        tier = risk.of_request(said, found.kind)
        assert tier.tier in ("passive", "medium", "high")
        score = points.of_request(said, found.kind, tier)
        assert score.points in (1, 2, 3, 5, 8, 13)
        assert route.choose(said).mode
        for item in criteria.items(said):
            assert item.strip() and item.strip() in said


def test_clipped_text_fits_its_limit_and_says_it_was_cut():
    rng = random.Random(4)
    for said in texts(4):
        limit = rng.randint(0, 200)
        cut = text.clip(said, limit)
        assert len(cut) <= max(limit, 0)
        if len(said) > limit >= 2:
            assert cut.endswith("…")
        one = text.clip(said, limit, one_line=True)
        assert "\n" not in one and len(one) <= max(limit, 0)


def test_a_tool_call_s_signature_never_holds_an_argument():
    allowed = re.compile(r"^[a-z0-9:._-]*$")
    for said in texts(5, 300):
        for tool, given in (("Bash", {"command": said}), ("Read", {"file_path": said}),
                            ("Edit", {"file_path": said, "old_string": said}), ("mcp__x__y", {"q": said}),
                            ("Grep", {"pattern": said}), ("WebFetch", {"url": said})):
            sig = signature.signature(tool, given)
            assert allowed.match(sig), sig
            for secret in ("sk-ant-", "ghp_", "user:pw", "rm -rf"):
                assert secret.replace(" ", "") not in sig
            entry = signature.entry({"tool_name": tool, "tool_input": given}, {})
            assert len(said.strip()) < 8 or said not in json.dumps(entry)


@pytest.mark.parametrize("seed", [6, 7])
def test_a_ci_file_the_line_reader_cannot_read_is_skipped_never_a_crash(tmp_path, seed):
    from locks import ci

    flows = tmp_path / ".github" / "workflows"
    flows.mkdir(parents=True)
    rng = random.Random(seed)
    pieces = ["jobs:\n", "  test:\n", "    runs-on: x\n", "    steps:\n", "      - run: npm test\n",
              "      - run: |\n", "          pytest -q\n", "    services:\n", "      db:\n", "        image: pg\n",
              "    env:\n", "      A: b\n", "  - \n", ": : :\n", "\t\n", "'unclosed\n", "{[\n", "on: push\n"]
    for n, said in enumerate(texts(seed, 60)):
        body = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 30))) + said
        (flows / f"w{n}.yml").write_text(body)
        with contextlib.suppress(ci.Unreadable):  # its contract: said, never a guess
            ci.read(body)
    found, skipped = ci.jobs(tmp_path)
    assert isinstance(found, list) and isinstance(skipped, list)
    for said in texts(seed, 100):
        ci.weak_line(said)


@pytest.mark.parametrize("key", [
    "sk-ant-api03-AbCdEf0123456789xyz-_AAbbCC", "sk-proj-abc123def456ghi789jkl", "gho_abcdefghij1234567890",
    "ghs_abcdefghij1234567890", "github_pat_11ABCDEFG0123456789_abcdef", "AKIAIOSFODNN7EXAMPLE",
    "AIzaSyA1234567890abcdefghijklmnopqrstu", "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc"])
def test_a_real_shaped_key_is_masked_wherever_text_is_kept(key):
    """Keys are written with dashes inside: `sk-[A-Za-z0-9]{8,}` let every Anthropic and OpenAI key
    through to check tails, notes, briefs and the UI. Each shape here is masked, glued or not."""
    from locks import notes

    for around in (f"{key}", f"x={key};", f"export KEY={key}\n", f"¿{key}¿"):
        assert key not in notes._SECRET.sub("[secret]", around)
        assert key not in launch.check_tail(f"FAILED tests/t.py::t - {around}")
    assert notes._SECRET.search("use sk-learn, task-123 and risk-free words") is None


def test_a_word_that_holds_sk_dash_is_no_key():
    """`(?<![A-Za-z0-9])`: `<task-notification>` was traced as `<ta[secret]>` 63 times in one session, and
    a note that quoted it was dropped as a secret. A key stands at a word's start; inside a word it is
    one."""
    from locks import notes, trace

    for word in ("<task-notification>", "task-runner-for-everything", "desk-reference-guide"):
        assert notes._SECRET.search(word) is None and trace._SECRET.search(word) is None
    for key in ("sk-abcdefghijklmnop", "(sk-ant-api03-AbCd0123456789xyz)", "KEY=sk-proj-abc123def456"):
        assert notes._SECRET.search(key) and trace._SECRET.search(key)


def test_a_signature_keeps_a_file_s_type_only_when_it_is_one_and_a_quoted_token_never():
    """A path's text after a dot is no extension (`notes.final draft`): kept, the signature held the
    argument. A token whose quotes `shlex` could not split showed a secret once cleaned."""
    assert signature.signature("Read", {"file_path": "/a/b/app.TSX"}) == "read:.tsx"
    assert signature.signature("Read", {"file_path": "/a/Makefile"}) == "read:noext"
    assert signature.signature("Edit", {"file_path": "/a/notes.final draft sk-ant-abcdefghijklmnop"}) == "edit:other"
    assert signature.signature("Read", {"file_path": "/a/x.ghp_abcdefghij"}) == "read:other"
    assert signature.signature("Bash", {"command": "\"…'ghp_ABCDEFGHIJKLMNOPQRSTUVW $(rm -rf /)"}) == "bash:unknown"
    assert signature.signature("Bash", {"command": "pytest -q tests"}) == "bash:pytest"
    assert signature.signature("Bash", {"command": "npm test"}) == "bash:npm-test"
