"""The work a request points to in a document's section (#200)."""

from __future__ import annotations

from locks import pointed, points, risk, route, split

PLAN = """# Plan

## Phase 1: setup
- create the module
- wire the config

## Phase 3: billing
- add invoices
- add credit notes
  - nested detail, not an item
- add refunds
- add tax lines
- add exports

```
- not an item: a fence
```

### Notes
- a sub-heading's item is the phase's too

## Phase 4: later
- not this phase
"""


def test_a_section_the_request_names_by_its_number_is_read(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "PLAN.md").write_text(PLAN)
    found = pointed.section("implement phase 3 of docs/PLAN.md", tmp_path)
    assert found and found.heading == "Phase 3: billing" and len(found.items) == 6
    assert "not this phase" not in found.items and "nested detail, not an item" not in found.items
    assert pointed.section("implementa la fase 3 de docs/PLAN.md", tmp_path) == found
    assert pointed.section('do "billing" from docs/PLAN.md', tmp_path) == found


def test_nothing_is_read_without_a_section_a_file_or_a_folder():
    assert pointed.section("implement phase 3 of docs/PLAN.md", None) is None


def test_nothing_is_read_for_a_whole_document_or_a_missing_one(tmp_path):
    (tmp_path / "PLAN.md").write_text(PLAN)
    assert pointed.section("implement PLAN.md", tmp_path) is None
    assert pointed.section("implement phase 3 of NOPE.md", tmp_path) is None
    assert pointed.section("implement phase 3 of ../PLAN.md", tmp_path / "sub") is None


def test_the_section_s_items_size_the_request_and_split_it_before_any_worker(tmp_path):
    (tmp_path / "PLAN.md").write_text(PLAN)
    text = "implement phase 3 of PLAN.md"
    found = risk.of_request(text, "feature")
    small = points.of_request(text, "feature", found)
    sized = points.of_request(text, "feature", found, where=tmp_path)
    assert sized.points > small.points and sized.reasons[0] == "6 items in PLAN.md › Phase 3: billing"
    assert route.split_first(text, "feature") is None
    assert route.split_first(text, "feature", where=tmp_path) == "6 items in PLAN.md › Phase 3: billing"
    proposal = split.by_items(text, where=tmp_path)
    assert len(proposal.parts) == 6 and "add refunds" in proposal.parts[2]["prompt"]
    assert "implement phase 3 of PLAN.md" in proposal.parts[0]["prompt"]
