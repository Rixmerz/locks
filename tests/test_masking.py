"""One secret pattern for notes, the trace and the transcript copy (`locks.masking`)."""
from __future__ import annotations

import json

import pytest

from locks import masking, notes, trace

# Key-shaped fixtures are written in pieces: a host's push protection reads a literal as a real key.
FORMATS = {
    "openai-style key": "sk-proj-abc123def456ghi789jkl",
    "anthropic key": "sk-ant-api03-AbCdEf0123456789xyz-_AAbbCC",
    "github token": "ghp_abcdefghij1234567890",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "basic auth": "Basic dXNlcjpwYXNzd29yZA==",
    "basic auth without padding": "Basic YWRtaW46c2VjcmV0cGFzcw",
    "aws access key": "AKIAIOSFODNN7EXAMPLE",
    "aws session key": "ASIAIOSFODNN7EXAMPLE",
    "stripe live key": "sk_live" "_" "4eC39HqLyjWDarjtT1zdp7dc",
    "stripe test key": "sk_test" "_" "4eC39HqLyjWDarjtT1zdp7dc",
    "stripe restricted live key": "rk_live" "_" "4eC39HqLyjWDarjtT1zdp7dc",
    "stripe restricted test key": "rk_test" "_" "4eC39HqLyjWDarjtT1zdp7dc",
    "url credentials": "postgres://admin:hunter2pass@db.internal:5432/app",
    "slack webhook": "https://hooks.slack" ".com/services/T00000000/B00000000/" + "X" * 24,
    "npm token": "npm" "_abcdefghijklmnopqrstuvwxyz0123456789",
    "gitlab token": "glpat" "-abcdefghij0123456789",
    "api_key assignment": "api_key=zxcvbnm12345",
    "apikey assignment": "apikey: zxcvbnm12345",
    "api-key assignment": "X-Api-Key: zxcvbnm12345",
    "secret assignment": "client_secret = abcd1234efgh",
    "token assignment": "auth_token: abcd1234efgh",
    "passwd assignment": "passwd=abcd1234efgh",
}


@pytest.mark.parametrize("name", FORMATS)
def test_each_format_is_masked(name):
    text = FORMATS[name]
    for around in (text, f"before {text} after", f"x={text};"):
        assert masking.mask(around) != around, name
        assert masking.SECRET.search(around), name
    # The masked line holds no part of the value that follows the format's own prefix.
    assert "[secret]" in masking.mask(f"see {text}")


@pytest.mark.parametrize("word", [
    "<task-notification>", "task-runner-for-everything", "desk-reference-guide",
    "use sk-learn, task-123 and risk-free words", "the token count rose", "a secret garden",
    "basic principles apply here"])
def test_ordinary_words_are_no_secret(word):
    assert masking.SECRET.search(word) is None
    assert masking.mask(word) == word


def test_the_patterns_are_shared_objects():
    assert trace._SECRET is masking.SECRET
    assert notes._SECRET is masking.SECRET_STRICT


def test_the_long_hex_run_is_masked_in_notes_only():
    """A note must not hold a token's digest; a trace keeps full git commit ids."""
    sha = "0123456789abcdef" * 2 + "01234567"
    assert masking.SECRET_STRICT.search(sha)
    assert notes._SECRET.sub("[secret]", f"at {sha}") == "at [secret]"
    assert masking.mask(f"commit {sha}") == f"commit {sha}"
    assert trace.mask(f"commit {sha}") == f"commit {sha}"


@pytest.mark.parametrize("text", ["ab-" * 7000, "foo_bar" * 3000, "Ab3_-" * 4000, "token" * 4000,
                                  "sk-" * 7000, "://a" * 7000, "basic " * 4000])
def test_masking_is_linear_on_long_runs(text):
    import time

    started = time.perf_counter()
    masking.mask(text)
    masking.SECRET_STRICT.sub("[secret]", text)
    assert time.perf_counter() - started < 0.2


def test_mask_is_the_pattern_with_a_placeholder():
    assert masking.mask("key sk-ant-api03-AbCdEf0123456789xyz here") == "key [secret] here"


def test_keep_transcript_copy_holds_no_secret_and_is_owner_only(tmp_path):
    key = "sk-ant-api03-AbCdEf0123456789xyz-_AAbbCC"
    source = tmp_path / "transcript.jsonl"
    lines = [
        {"message": {"id": "m1", "model": "m", "usage": {"input_tokens": 7, "output_tokens": 3}},
         "text": f"export KEY={key}"},
        {"tool": "curl https://u:hunter2pass@host/x", "api_key": "abcdefgh12345678"},
    ]
    source.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json " + key + "\n", encoding="utf-8")
    env = {"LOCKS_HOME": str(tmp_path / "home")}
    target = trace.keep_transcript({"transcript_path": str(source), "session_id": "s1"}, env)
    assert target is not None
    copy = target.read_text(encoding="utf-8")
    for planted in (key, "hunter2pass", "abcdefgh12345678"):
        assert planted not in copy
    assert target.stat().st_mode & 0o777 == 0o600
    # The masked JSON lines still parse, so the tokens in them are still counted.
    assert trace.tokens(target)["m"]["input"] == 7
    assert key in source.read_text(encoding="utf-8")


def test_keep_transcript_tightens_the_mode_of_a_copy_that_exists(tmp_path):
    source = tmp_path / "t.jsonl"
    source.write_text("{}\n", encoding="utf-8")
    env = {"LOCKS_HOME": str(tmp_path / "home")}
    event = {"transcript_path": str(source), "session_id": "s2"}
    target = trace.keep_transcript(event, env)
    target.chmod(0o644)
    assert trace.keep_transcript(event, env).stat().st_mode & 0o777 == 0o600


def test_compact_json_usage_is_no_assignment():
    line = '{"usage":{"input_tokens":12345678,"output_tokens":3,"cache_read_input_tokens":99999999}}'
    assert masking.mask(line) == line
    assert masking.mask('{"token":"abcdefgh1234"}') != '{"token":"abcdefgh1234"}'


def test_keep_transcript_appends_only_what_the_copy_lacks_and_starts_over_when_replaced(tmp_path):
    source = tmp_path / "t.jsonl"
    env = {"LOCKS_HOME": str(tmp_path / "home")}
    event = {"transcript_path": str(source), "session_id": "s3"}
    source.write_text('{"n":1}\n{"n":2}\n{"n":3', encoding="utf-8")  # the last line is still being written
    target = trace.keep_transcript(event, env)
    assert target.read_text(encoding="utf-8") == '{"n":1}\n{"n":2}\n'
    with source.open("a", encoding="utf-8") as fh:
        fh.write('}\n{"n":4}\n')
    trace.keep_transcript(event, env)
    assert target.read_text(encoding="utf-8") == '{"n":1}\n{"n":2}\n{"n":3}\n{"n":4}\n'
    # An unchanged source is copied again for nothing: the offset says it holds it all.
    marker = target.with_name("s3.transcript.offset")
    before = marker.read_text(encoding="utf-8")
    trace.keep_transcript(event, env)
    assert marker.read_text(encoding="utf-8") == before
    # Shrunk, or replaced by another transcript: copied again from the start.
    source.write_text('{"x":1}\n', encoding="utf-8")
    trace.keep_transcript(event, env)
    assert target.read_text(encoding="utf-8") == '{"x":1}\n'
    source.write_text('{"y":1}\n{"y":2}\n', encoding="utf-8")  # longer, but another file
    trace.keep_transcript(event, env)
    assert target.read_text(encoding="utf-8") == '{"y":1}\n{"y":2}\n'
    assert target.stat().st_mode & 0o777 == 0o600 and marker.stat().st_mode & 0o777 == 0o600


def test_keep_transcript_stops_at_its_budget_and_the_next_call_goes_on(tmp_path):
    source = tmp_path / "t.jsonl"
    source.write_text("".join(f'{{"n":{i}}}\n' for i in range(50)), encoding="utf-8")
    env = {"LOCKS_HOME": str(tmp_path / "home")}
    event = {"transcript_path": str(source), "session_id": "s4"}
    ticks = iter(range(1000))
    target = trace.keep_transcript(event, env, budget_s=3, clock=lambda: next(ticks))
    held = target.read_text(encoding="utf-8").splitlines()
    assert 0 < len(held) < 50
    trace.keep_transcript(event, env)
    assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
