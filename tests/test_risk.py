"""Words of enforcement in a request read as security, only where they mean it."""
from __future__ import annotations

import pytest

from locks import risk

HARDEN = ("Harden the MCP guard so no policy can be bypassed by a malformed message, "
          "and refuse what it cannot parse.")


def security(text: str) -> bool:
    found = risk.of_request(text, "implement")
    return any(area == "security" for area, _ in risk.areas(found))


@pytest.mark.parametrize("text", [
    "Stop the retry from bypassing the check",
    "Fix a bypass in the router",
    "A call bypasses the filter",
    "Run workers in a sandbox",
    "Keep an allow-list of tools",
    "Keep an allowlist of hosts",
    "Add a denylist for hosts",
    "Grant the permission to the worker",
    "Refuse the call when the permissions are missing",
    "Deny the permission when the policy says so",
    "Enforce the policy on every call",
    "The policy must refuse writes",
    "Make the guard refuse the call",
    "The guard enforces the policy",
])
def test_enforcement_words_are_security(text: str) -> None:
    assert security(text)


@pytest.mark.parametrize("text", [
    "Add a privacy policy page",
    "Change the retry policy of the queue",
    "Use a guard clause in the parser",
    "Ask for permission to merge was given",
    "Permission to merge was given",
    "Rename the helper",
])
def test_ordinary_senses_are_not_security(text: str) -> None:
    assert not security(text)


def test_a_word_inside_a_path_or_variable_is_never_read() -> None:
    assert not security("Rename src/locks/bypass.py")
    assert not security("Read LOCKS_BYPASS_ALL once")


def test_the_guard_request_is_high_with_a_security_reason() -> None:
    found = risk.of_request(HARDEN, "implement")
    assert found.tier == "high"
    assert any("about security" in reason for reason in found.reasons)
