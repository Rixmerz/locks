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


WORD = ("Update the proposal document report.docx:\n- change the title page to the new client\n"
        "- rewrite section 2 as a table\n- fix the numbering of the figures\n- replace the footer text\n"
        "- add a table of contents\n- update the references list in the last section\n"
        "Keep the styles untouched; the changes are small but touch every section of the document.")


def test_items_that_all_change_one_document_are_one_task():
    from locks import route

    items = route.pieces_of(WORD)
    assert len(items) >= 6 and route.one_file(WORD, items)
    assert route.split_first(WORD, "implement") is None
    assert "one file or document" in route.kept_whole(WORD, "implement")
    got = split.by_items(WORD)
    assert not got.parts and "one file or document" in got.problem
    # propose does not ask the model for what is one task
    assert split.propose(WORD, None, ask=lambda *a: (_ for _ in ()).throw(AssertionError("no model"))).problem


def test_items_naming_files_in_different_folders_are_still_pieces():
    from locks import route

    assert not route.one_file(LISTED, route.pieces_of(LISTED))
    two = "Document report.docx and also brief.docx:\n- change report.docx title\n- change brief.docx footer"
    assert not route.one_file(two, route.pieces_of(two))


def test_a_derived_surface_naming_an_absent_path_is_left_out_and_said(tmp_path):
    (tmp_path / "src" / "app").mkdir(parents=True)
    text = "Do these:\n- fix src/app/a.py\n- write docs/guide/x.md\n"
    got = split.by_items(text, where=tmp_path)
    assert got.parts[0]["surfaces"] == ["src/app/**"] and "absent" not in got.parts[0]
    assert got.parts[1]["surfaces"] == [] and got.parts[1]["absent"] == ["docs/guide/**"]
    assert "docs/guide/** left out: not in the repository" in "\n".join(split.lines(got))
    assert split.by_items(text).parts[1]["surfaces"] == ["docs/guide/**"]  # no directory: kept as derived


def test_a_glob_surface_exists_by_its_literal_folder(tmp_path):
    (tmp_path / "src").mkdir()
    assert split._exists(tmp_path, "src/**") and not split._exists(tmp_path, "word/**")
    assert not split._exists(tmp_path, "src/nope/*.py")


def test_pieces_with_the_same_text_are_one_task():
    same = "Do these:\n- fix the totals\n- fix the totals\n"
    got = split.by_items(same)
    assert not got.parts and "same text" in got.problem
    assert split.same_text(["a  b", "a b"]) and not split.same_text(["a", "b"])
    answer = {"contract": "shared", "parts": [{"id": "a", "prompt": "do x", "surfaces": ["a/"]},
                        {"id": "b", "prompt": "do  x", "surfaces": ["b/"]}]}
    assert "one task" in split.by_model("prose", None, ask=lambda *a: (answer, 0.0, "")).problem


def test_cancel_split_cancels_every_piece_still_queued_from_the_source_or_any_piece(git_repo, capsys):
    store = Store.open()
    try:
        src = store.create_task("big", status="dismissed", source="locks", repo="r", cwd=str(git_repo))
        pieces = [store.enqueue(f"piece {k}", repo="r", cwd=str(git_repo))["id"] for k in range(3)]
        for k, n in enumerate(pieces):
            store.add_event(n, "split_from", task=src["id"], part=f"p{k}", group=pieces[0])
        other = store.enqueue("unrelated", repo="r", cwd=str(git_repo))
        store.update_task(pieces[2], status="done")
    finally:
        store.close()
    assert cli.main(["cancel", "--split", str(pieces[1])]) == 0
    out = capsys.readouterr().out
    assert f"cancel requested for #{pieces[0]}, #{pieces[1]}" in out and f"#{pieces[2]} already off" in out
    store = Store.open()
    try:
        assert [store.get_task(n)["status"] for n in pieces[:2]] == ["cancelled", "cancelled"]
        assert store.get_task(pieces[2])["status"] == "done"
        assert store.get_task(other["id"])["status"] == "queued"
        assert split.siblings(store, src["id"]) == pieces
    finally:
        store.close()
    assert cli.main(["cancel", "--split", str(src["id"])]) == 0
    assert "nothing to cancel" in capsys.readouterr().out
    assert cli.main(["cancel", "--split", str(other["id"])]) == 1


def test_dismissing_a_cancelled_or_dismissed_task_succeeds_and_says_so(git_repo, capsys):
    store = Store.open()
    try:
        a = store.enqueue("a", repo="r", cwd=str(git_repo))["id"]
    finally:
        store.close()
    assert cli.main(["cancel", str(a)]) == 0
    capsys.readouterr()
    assert cli.main(["dismiss", str(a)]) == 0
    assert "already off the board (cancelled)" in capsys.readouterr().out
