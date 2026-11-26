"""A change that renders UI is checked rendered (`ui_check`): its tier is raised so the independent
check runs, that check is told what to render and measure, and a UI worker is told what done means."""
from __future__ import annotations

from pathlib import Path

from locks import evidence, orchestrate, risk


class _Plan:
    def __init__(self):
        self.risk = risk.Risk()


def test_a_change_that_renders_ui_is_high_only_with_the_setting(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(orchestrate, "_unlinked", lambda *a: [])
    plan = _Plan()
    monkeypatch.setenv("LOCKS_UI_CHECK", "off")
    orchestrate._risk_of_change(plan, ["src/components/Card.tsx"], tmp_path, None)
    assert not any("rendered UI" in r for r in plan.risk.reasons)
    monkeypatch.setenv("LOCKS_UI_CHECK", "on")
    orchestrate._risk_of_change(plan, ["src/components/Card.tsx"], tmp_path, None)
    assert plan.risk.tier == "high" and any("rendered UI" in r for r in plan.risk.reasons)
    other = _Plan()
    orchestrate._risk_of_change(other, ["src/components/Card.test.tsx", "README.md"], tmp_path, None)
    assert not any("rendered UI" in r for r in other.risk.reasons)


def test_the_check_of_rendered_ui_measures_it_or_says_what_it_could_not_render():
    assert evidence.ui_check_lines(["src/x.py"], ()) == []
    measured = evidence.ui_check_lines(["src/components/Card.tsx"], ("layout-inspector",))[0]
    assert "Card.tsx" in measured and "compare_viewports" in measured and "375 px" in measured
    assert "never a blocker" in measured
    blind = evidence.ui_check_lines(["src/components/Card.tsx"], ())[0]
    assert "No layout tool" in blind and "unchecked, never assumed fine" in blind


def test_the_verifier_brief_carries_the_ui_lines(tmp_path: Path):
    lines = evidence.ui_check_lines(["src/components/Card.tsx"], ())
    brief = orchestrate.verifier_brief("Add a card", "implement", tmp_path, "abc", ["src/components/Card.tsx"],
                                       required=lines)
    assert lines[0] in brief
