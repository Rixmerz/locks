"""The `-nobaseline` arm of bench/speed/versus.py: the writing roles of a locks home without the
`engineering-baseline` skill, the other roles left as they are, and the prompt size every locks row
carries. No model runs: `bin/locks` is a fake that seeds the store the way a run's home holds it."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from locks import roles
from locks.store import Store

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("versus", ROOT / "bench" / "speed" / "versus.py")
versus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versus)

BASELINE = "engineering-baseline"
#: The bundled roles with a writing kind (every one of them carries the baseline) and the reading ones.
WRITING = {"debugger", "docs-writer", "feature-builder", "frontend", "frontier", "implementer", "investigator",
           "refactorer", "tester"}
READING = {"answerer", "architect", "researcher", "reviewer", "security-auditor"}


def baseline_section(text: str) -> str:
    """The baseline's skill section in a worker's prompt: its heading up to the next skill's, or the end."""
    start = text.index(f"## Skill `{BASELINE}`")
    end = text.find("\n\n## Skill `", start + 1)
    return text[start:end + 2] if end != -1 else text[start:]


def fake_locks(monkeypatch: pytest.MonkeyPatch, where: Path, briefed: list[int], seen: dict) -> None:
    """`bin/locks` as a run leaves it: one finished locks task whose first worker was briefed with each
    `briefed` size. What the fake saw of its home (the roles, the verifier switch) goes into `seen`."""
    real = subprocess.run

    def run(cmd, **kwargs):
        if cmd[0] != str(ROOT / "bin" / "locks"):
            return real(cmd, **kwargs)
        home = Path(kwargs["env"]["LOCKS_HOME"])
        seen["roles"] = sorted(p.name for p in (home / "roles").glob("*.md"))
        seen["verifier"] = kwargs["env"].get("LOCKS_VERIFIER")
        store = Store(home / "locks.db")
        task = store.create_task("implement the thing", status="done", source="locks", repo="r", cwd=str(where))
        for chars in briefed:
            store.add_event(task["id"], "briefed", system_chars=chars)
        store.close()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)


def test_the_bundled_implementer_carries_the_baseline():
    assert BASELINE in roles.brief(roles.load().get("implementer")).system_prompt


def test_nobaseline_shadows_the_writing_roles_and_changes_only_their_skills(_isolated_home):
    loaded = roles.load()
    bundled = {name: roles.brief(loaded.get(name)).system_prompt for name in WRITING | READING}
    bundled_skills = {name: loaded.get(name).skills for name in WRITING}

    assert set(versus.shadow_roles(_isolated_home)) == WRITING

    shadowed = roles.load()
    for name in WRITING:
        assert shadowed.get(name).scope == "user"
        assert shadowed.get(name).skills == tuple(s for s in bundled_skills[name] if s != BASELINE)
    implementer = roles.brief(shadowed.get("implementer")).system_prompt
    assert BASELINE not in implementer
    # The same prompt but for that skill: its section is cut out, every other word stays.
    assert implementer == bundled["implementer"].replace(baseline_section(bundled["implementer"]), "")
    for name in READING:
        assert shadowed.get(name).scope == "bundled"
        assert roles.brief(shadowed.get(name)).system_prompt == bundled[name]


def test_a_role_without_the_skill_on_its_one_line_is_an_error_never_a_guess():
    text = "---\nname: x\nkinds: [implement]\nskills: [ponytail]\n---\nBody.\n"
    with pytest.raises(ValueError):
        versus.without_skill(text, BASELINE)


@pytest.mark.parametrize("arm", ["locks-nobaseline", "locks-nobaseline-noverify", "locks-noverify-nobaseline"])
def test_nobaseline_arm_has_the_shadow_roles_in_its_home_before_the_run(monkeypatch, tmp_path, arm):
    monkeypatch.delenv("LOCKS_VERIFIER", raising=False)  # conftest sets it off: the switch must come from the arm
    seen: dict = {}
    fake_locks(monkeypatch, tmp_path, briefed=[4321, 99999], seen=seen)

    row = versus.locks(tmp_path, "add the thing", arm)

    assert "implementer.md" in seen["roles"]
    assert seen["verifier"] == ("off" if "noverify" in arm else None)
    # The first briefed event's prompt size, not the last one's.
    assert row["system_chars"] == 4321


def test_plain_locks_arm_runs_with_the_bundled_roles_and_no_briefed_worker_has_no_size(monkeypatch, tmp_path):
    seen: dict = {}
    fake_locks(monkeypatch, tmp_path, briefed=[], seen=seen)

    row = versus.locks(tmp_path, "add the thing", "locks")

    assert seen["roles"] == []
    assert row["system_chars"] is None
