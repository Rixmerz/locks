"""The one pattern locks masks secrets with: notes, the trace and the transcript copy all use it.

Standard library only and nothing of locks imported: the trace runs on every tool call.
"""
from __future__ import annotations

import re

#: Keys written with dashes inside (`sk-ant-api03-…`, `sk-proj-…`) must not slip through a pattern
#: of letters and digits alone. A key starts a word, or holds a digit when glued to one: inside a
#: word (`<task-notification>`, `desk-reference`) `sk-` is no key.
_FORMATS = (
    # Provider keys.
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}|(?<![A-Za-z0-9])sk-[A-Za-z0-9]{8,}|"
    r"sk-(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{8,}|github_pat_\w{8,}|xox[abposr]-[\w-]{8,}|"
    r"AKIA[0-9A-Z]{12,}|ASIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|"
    r"(?:sk|rk)_(?:live|test)_\w{10,}|npm_\w{30,}|glpat-[\w-]{20,}|"
    # Private keys and password assignments.
    r"-----BEGIN [A-Z ]*PRIVATE KEY|password\s*[:=]\s*\S+|"
    # Bearer tokens and JWTs (three base64url parts).
    r"bearer\s+[A-Za-z0-9._~+/-]{20,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    # `Basic <base64>`: the value must look encoded (a digit, `+`, `/`, `=` or a camelCase seam) so
    # that prose such as "basic principles" is no credential. ponytail: base64 of all-lowercase
    # letters is let through; widen if such a header shows up.
    r"\bbasic\s+(?-i:(?=[A-Za-z0-9+/]*(?:[0-9+/=]|[a-z][A-Z])))[A-Za-z0-9+/]{8,}={0,2}|"
    # Credentials in a URL and Slack webhooks.
    r"://[^\s/:@]+:[^\s/@]+@|hooks\.slack\.com/services/[\w/-]+|"
    # An assignment to a name that says it holds a secret. It starts at the keyword, with no run of
    # word characters before it and a bounded one after it, so no input makes the match quadratic.
    # The value is a run of non-space characters that stops at a quote, a comma or a closing
    # bracket and is no bare number, so compact JSON usage (`"input_tokens":12345678,"output_tokens":3`)
    # is no assignment. ponytail: a purely numeric secret or one with a comma in it is let through.
    r"(?:api[_-]?key|secret|token|passwd)[\w-]{0,40}[\"']?[ \t]*[:=][ \t]*[\"']?"
    r"(?!\d+(?![\w.-]))[^\s\"',}\]]{8,}"
)

#: What the trace and the transcript copy mask. It leaves long hex runs alone: a trace keeps full
#: git commit ids.
SECRET = re.compile("(" + "".join(_FORMATS) + ")", re.IGNORECASE)

#: `SECRET` and a run of 40 or more hex digits (a token, or the digest of one): what a note must not hold.
SECRET_STRICT = re.compile("(" + "".join(_FORMATS) + r"|\b[0-9a-f]{40,}\b)", re.IGNORECASE)


def mask(text: str) -> str:
    """`text` with every secret replaced by `[secret]`."""
    return SECRET.sub("[secret]", text)
