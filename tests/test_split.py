"""A request that is several tasks, split into its pieces (`locks split`)."""
from __future__ import annotations

import json

from locks import after, cli, split, stops
from locks.store import Store

LISTED = ("Build the student side of the platform:\n- the opinion form in src/app/opinion/form.ts\n"
          "- the quiz per module in src/app/quiz/\n- the forum in src/app/forum/forum.ts")


def test_a_request_that_lists_its_items_is_split_by_them_with_no_model():
    got = split.propose(LISTED, None, ask=lambda *a: (_ for _ in ()).throw(AssertionError("no model")))
    assert got.source == "items" and [p["id"] for p in got.parts] == ["p1", "p2", "p3"]
    assert got.parts[0]["prompt"].startswith("Build the student side of the platform:\n\nThis task is one piece")
    assert got.parts[0]["surfaces"] == ["src/app/opinion/**"] and got.parts[1]["surfaces"] == ["src/app/quiz/**"]
    assert "already worked on" in split.by_items(LISTED, ["x.ts"]).parts[0]["prompt"]
    assert split.by_items("one thing only").problem
    many = "Do:\n" + "\n".join(f"- item {n} of the work" for n in range(9))
    assert "more than" in split.propose(many, None).problem


def test_prose_is_split_by_the_model_and_read_strictly():
    asked = []

    def ask(system, schema, question):
        asked.append(question)
        return ({"parts": [{"id": "control", "prompt": "Build the control", "surfaces": ["src/control/**"]},
                           {"id": "pdf", "prompt": "Add PDF notes", "surfaces": ["src/pdf/**"], "after": "control"}],
                 "contract": "NotesService.export(id) -> Blob"}, 0.002, "")

    got = split.propose("Add the control, the PDF notes and fix the overlaps", None, done=["src/a.ts"], ask=ask)
    assert [p["id"] for p in got.parts] == ["control", "pdf"] and got.parts[1]["after"] == "control"
    assert "Files the work already changed:\nsrc/a.ts" in asked[0]
    assert "> NotesService.export(id) -> Blob" in split.brief(got.parts[0], got.contract)
    overlapping = lambda *a: ({"parts": [{"id": "a", "prompt": "x", "surfaces": ["src/**"]},  # noqa: E731
                                         {"id": "b", "prompt": "y", "surfaces": ["src/b/**"]}], "contract": "c"}, 0, "")
    assert "does not read" in split.by_model("prose", None, ask=overlapping).problem
    told = []
    fixed = iter([overlapping()[0], {"parts": [{"id": "a", "prompt": "x", "surfaces": ["src/a/**"]},
                                               {"id": "b", "prompt": "y", "surfaces": ["src/b/**"]}], "contract": "c"}])
    again = split.by_model("prose", None, ask=lambda s, sc, q: (told.append(q) or next(fixed), 0.001, ""))
    assert [p["id"] for p in again.parts] == ["a", "b"] and "could not be used: its parts' surfaces overlap" in told[1]
    assert "no split" in split.by_model("prose", None, ask=lambda *a: (None, 0, "down")).problem
    one = lambda *a: ({"parts": [{"id": "a", "prompt": "x", "surfaces": []}]}, 0, "")  # noqa: E731
    assert "one task" in split.by_model("prose", None, ask=one).problem
    assert split.lines(got)[0].startswith("2 pieces (by Haiku")


def test_split_queue_starts_the_pieces_on_what_the_task_kept_and_moves_what_waited(git_repo, capsys, monkeypatch):
    store = Store.open()
    try:
        big = store.create_task(LISTED, status="running", source="locks", repo="r", cwd=str(git_repo),
                                options=json.dumps({"verify": "npm test", "max_turns": 200}))
        stops.record(store, big["id"], stops.Stop("split", "it outgrew 120 turns as several pieces"))
        store.add_event(big["id"], "finished", status="replan", changed=["src/app/quiz/q.ts"], commit="abc123",
                        stop={"cause": "split", "reason": "x"})
        later = store.enqueue("small fixes", repo="r", cwd=str(git_repo), options={"after": {"task": big["id"]}})
        view = stops.view(store, store.get_task(big["id"]))
        assert view["next"] == f"locks split {big['id']}" and "--queue" in view["todo"]
    finally:
        store.close()
    assert cli.main(["split", str(big["id"])]) == 0
    out = capsys.readouterr().out
    assert out.startswith("3 pieces (by its listed items):") and "nothing queued" in out
    assert cli.main(["split", f"#{big['id']}", "--queue"]) == 0
    out = capsys.readouterr().out
    store = Store.open()
    try:
        pieces = [t for t in store.list_tasks(status=["queued"], limit=10) if t["id"] != later["id"]]
        assert len(pieces) == 3
        for piece in pieces:
            options = json.loads(piece["options"])
            assert options["base"] == "abc123" and options["verify"] == "npm test" and "max_turns" not in options
        last = max(p["id"] for p in pieces)
        assert after.hold(store.get_task(later["id"])) == {"task": last}
        gone = store.get_task(big["id"])
        assert gone["status"] == "dismissed" and "split into" in stops.of(store, gone).reason
    finally:
        store.close()
    assert f"now for #{last}, its last piece" in out


def test_a_worker_never_queues_a_split(monkeypatch, capsys):
    monkeypatch.setenv("LOCKS_WORKER_TASK", "7")
    assert cli.main(["split", "x", "--queue"]) == 2


def test_pieces_whose_folders_collide_go_without_surfaces():
    got = split.by_items("Do these:\n- fix src/a.py\n- fix src/b.py\n- write docs/guide/x.md\n")
    assert [p["surfaces"] for p in got.parts] == [[], [], ["docs/guide/**"]]
