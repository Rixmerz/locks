from __future__ import annotations

from locks import cells_by_kind, matrix, standards


class _T:
    def __init__(self, kind, cell, passed):
        self.kind, self.pinned, self.attempts = kind, False, ({"cell": cell, "passed": passed, "move": None},)
        self.band, self.passed, self.cost_usd, self.wall_s = 2, passed, 0.2, 60.0

    @property
    def first(self):
        return self.attempts[0]["passed"]


def test_every_kind_has_a_row_and_a_proven_cell_is_named(monkeypatch, store):
    tasks = [_T("explore", "haiku/medium", True)] * 5 + [_T("implement", "sonnet/medium", False)] * 2
    monkeypatch.setattr(standards, "ended", lambda s, repo=None: tasks)
    rows = cells_by_kind.report(store, "r")
    assert [r["kind"] for r in rows] == list(matrix.KINDS)
    explore = next(r for r in rows if r["kind"] == "explore")
    assert cells_by_kind.best(explore) == "haiku/medium" and explore["pass_first"] == 1.0
    implement = next(r for r in rows if r["kind"] == "implement")
    assert cells_by_kind.best(implement) is None and implement["start"] == "sonnet/medium"
    text = "\n".join(cells_by_kind.text(rows, "r"))
    assert "proven here: haiku/medium" in text and "sonnet/medium 0/2" in text


def test_a_calibrated_start_is_said(store):
    store.set_standard("r", "implement", standards.ANY_SIZE, start_cell="sonnet/high")
    rows = {r["kind"]: r for r in cells_by_kind.report(store, "r")}
    assert rows["implement"]["start"] == "sonnet/high" and "calibrated" in rows["implement"]["why"]
    assert rows["feature"]["why"] == "the ladder's first rung"


def test_the_command_prints_and_dumps(store, capsys):
    from locks import cli

    assert cli.main(["cells", "--all"]) == 0 and "Where each kind starts" in capsys.readouterr().out
    assert cli.main(["cells", "--json"]) == 0 and '"kind"' in capsys.readouterr().out
