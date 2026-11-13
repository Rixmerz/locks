"""Findings as records, not prose: where, what, how it is known, and whether
the change caused it.

A reviewer, an auditor and the independent verifier report each finding with
`evidence_class` (deterministic: a command, a test or a diff shows it;
inferential: read from the code; insufficient: suspected, not shown) and
`cause` (introduced or worsened by the change, pre-existing before it, or
unknown). A finding blocks a change only when the change is to blame (or it
cannot be told: unknown counts as to blame) and it is more than suspected.
Pre-existing problems are reported and never block.

Two findings are the same when they name the same place and say the same
thing; `key` is that identity, so a re-check can tell a finding it already
reported from a new one.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

EVIDENCE = ("deterministic", "inferential", "insufficient")
CAUSES = ("introduced", "worsened", "pre-existing", "unknown")


def read(block: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """The findings a result block reports, each normalized; unknown values read as the cautious ones."""
    raw = block.get("findings")
    if not isinstance(raw, list):
        return ()
    out = []
    for f in raw:
        if not isinstance(f, dict) or not str(f.get("what") or "").strip():
            continue
        evidence = str(f.get("evidence_class") or "").lower()
        cause = str(f.get("cause") or "").lower()
        proof = str(f.get("proof") or "")[:500]
        if evidence == "deterministic" and not proof.strip():
            # Shown by a command, a test or a diff means its proof is given: a claim of
            # it without one is read from the code, and a refuter still decides it.
            evidence = "inferential"
        out.append({
            "where": str(f.get("where") or "")[:200].strip(),
            "what": " ".join(str(f["what"]).split())[:500],
            "evidence_class": evidence if evidence in EVIDENCE else "inferential",
            "cause": cause if cause in CAUSES else "unknown",
            "proof": proof,
            # The worker found it and fixed it in this change: reported, never offered as work left.
            **({"fixed": True} if f.get("fixed") is True else {}),
        })
    return tuple(out)


def blocking(found: Iterable[Mapping[str, str]]) -> list[Mapping[str, str]]:
    """The findings that stop a change: the change's own, and shown. One marked `fixed` is gone (a
    worker's own, or an earlier check's the next check says the correction fixed): it stops nothing."""
    return [f for f in found if f["cause"] in ("introduced", "worsened", "unknown")
            and f["evidence_class"] != "insufficient" and not f.get("fixed")]


def place(where: str) -> str:
    """A finding's file, without its line: a fix moves lines."""
    return re.sub(r"[:#]L?\d+(?:[-:]\d+)*$", "", where.strip().lower())


def key(f: Mapping[str, str]) -> str:
    words = " ".join(sorted(set(re.findall(r"[a-z0-9_]{4,}", f["what"].lower())))[:12])
    return hashlib.sha256(f"{place(f['where'])}|{words}".encode()).hexdigest()[:12]


def same(a: Mapping[str, str], b: Mapping[str, str]) -> bool:
    """The same finding: the same identity, or the same place named (a fix moves lines and rewords)."""
    return key(a) == key(b) or (bool(a["where"]) and place(a["where"]) == place(b["where"]))


def places(found: Iterable[Mapping[str, str]]) -> frozenset[str]:
    return frozenset(place(f["where"]) for f in found if f["where"])


def text(found: Iterable[Mapping[str, str]]) -> str:
    """Findings for a brief or a report, one per line."""
    return "\n".join(f"- [{f['cause']}, {f['evidence_class']}] {f['where'] or 'unplaced'}: {f['what']}"
                     + (f" — {f['proof']}" if f.get("proof") else "") for f in found)
