"""Which way a request is done: the session itself, one worker, or parts side by side."""
from __future__ import annotations

import json
from pathlib import Path

from locks import hooks, route
from locks.store import Store

SMALL = "Fix the typo in the README heading of the install section"
MEDIUM = ("Add a --json flag to the export command in src/cli/export.py that prints the rows as JSON, "
          "with a test in tests/test_export.py")
BIG = ("Build the billing module: a schema migration for invoices, the payment webhook with its signature check, "
       "the admin screens to list and refund them, and tests for each, in src/billing/models.py, "
       "src/billing/webhook.py, src/admin/invoices.tsx and src/billing/refund.py")
FOOTPRINT = (2_300_000, 26)


def test_a_short_turn_reading_and_the_conversation_stay_with_the_session():
    assert route.choose("ok go on").mode == route.MAIN
    read = route.choose("Explain how the dispatcher decides which queued task starts next in this repository")
    assert read.mode == route.MAIN and "reading" in read.reasons[0]
    said = route.choose("Apply this to the export command as we discussed above, with its test",
                        context_tokens=600_000, footprint=FOOTPRINT)
    assert said.mode == route.MAIN and "this conversation" in said.reasons[0] and said.advice() == ""


def test_large_or_spread_work_runs_in_parts():
    found = route.choose(BIG, context_tokens=50_000, footprint=FOOTPRINT)
    assert found.mode == route.PARALLEL and "locks workflow run feature" in found.advice()
    pieces = ("Do these:\n- update src/api/users.py for the new field\n- update web/views/users.tsx to show it\n"
              "- update docs/guide/users.md to describe it")
    assert route.choose(pieces).mode == route.PARALLEL
    assert "3 listed pieces in separate folders" in route.choose(pieces).reasons


def test_the_cheaper_reading_decides_once_it_is_measured():
    # A session with a large context re-reads all of it each turn: a worker reads less.
    heavy = route.choose(MEDIUM, context_tokens=600_000, footprint=FOOTPRINT)
    assert heavy.mode == route.WORKER and "about 3.0M tokens (5 turns over this session's 600k context)" \
        in heavy.reasons[0] and "a worker about 442k at its measured 88k a turn" in heavy.reasons[0]
    assert "Queue it" in heavy.advice() and "do it yourself instead" in heavy.advice()
    light = route.choose(MEDIUM, context_tokens=40_000, footprint=FOOTPRINT)
    assert light.mode == route.MAIN and "200k" in light.reasons[0] and light.advice() == ""
    assert route.choose(SMALL, context_tokens=600_000, footprint=FOOTPRINT).mode == route.WORKER


def test_without_a_measure_size_and_a_full_context_decide():
    assert route.choose(SMALL).mode == route.MAIN
    listed = ("Implement the export:\n- S1 json\n- S2 csv\n- S3 a test in tests/test_export.py\n"
              "- S4 docs in docs/export.md")
    assert route.choose(listed).mode == route.WORKER and "3 points" in route.choose(listed).reasons[0]
    full = route.choose(SMALL, context_share=0.7)
    assert full.mode == route.WORKER and "70% full" in full.reasons[0]
    assert route.choose(MEDIUM).data()["kind"] == "implement" and route._tokens(999) == "1k"
    assert route.choose("Why does the export drop the last row when the file is empty?").mode == route.MAIN
    assert route.choose("Fix the export so this repository's tests pass again", context_tokens=600_000,
                        footprint=FOOTPRINT).mode == route.WORKER  # "this repository" is no pointer back


def test_a_worker_s_footprint_is_measured_from_its_attempts(store: Store):
    assert store.worker_footprint("r") is None
    for i in range(6):
        task = store.create_task(f"t{i}", status="done", source="locks", repo="r")
        store.add_event(task["id"], "attempt_finished", input_tokens=1_000_000 * (i + 1), turns=10 + i)
    assert store.worker_footprint("r") == (4_000_000, 13)
    assert store.worker_footprint("elsewhere") == (4_000_000, 13)  # anywhere, when the repository has too few
    assert store.worker_footprint(None) == (4_000_000, 13)


def test_the_prompt_hook_advises_and_keeps_each_decision(store: Store, git_repo, tmp_path, monkeypatch):
    from locks import project

    project.enroll(git_repo)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "timestamp": "2026-10-20T10:00:00Z", "message": {
        "model": "claude-opus-5-5", "usage": {"input_tokens": 10, "cache_read_input_tokens": 599_990,
                                              "cache_creation_input_tokens": 0}}}) + "\n")
    monkeypatch.setattr(store, "worker_footprint", lambda key: FOOTPRINT)
    event = {"session_id": "s", "prompt": MEDIUM, "prompt_id": "p", "cwd": str(git_repo),
             "transcript_path": str(transcript)}
    monkeypatch.setenv("LOCKS_ROUTE", "off")
    assert hooks.user_prompt_submit(event, store) is None  # with the setting off, nothing
    monkeypatch.setenv("LOCKS_ROUTE", "on")
    out = hooks.user_prompt_submit({**event, "prompt_id": "p2"}, store)
    said = out["hookSpecificOutput"]["additionalContext"]
    assert said.startswith("locks route: a worker (done here it reads about")
    routed = store.session_events("s", ("routed",))[-1]["data"]
    assert routed["mode"] == "worker" and routed["context_tokens"] == 600_000 and routed["worker_tokens"] == 2_300_000
    # The session does it: kept, and nothing is said.
    assert hooks.user_prompt_submit({**event, "prompt": SMALL, "prompt_id": "p3", "transcript_path": None},
                                    store) is None
    assert store.session_events("s", ("routed",))[-1]["data"]["mode"] == "main"
    # Outside a project, nothing; a failure costs the turn nothing.
    assert hooks._route(store, 1, MEDIUM, str(tmp_path), None) is None

    def broken(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(route, "choose", broken)
    assert hooks._route(store, 1, MEDIUM, str(git_repo), None) is None


def test_a_question_about_the_work_stays_with_the_session_and_an_order_with_one_does_not():
    asked = ("También se podría reutilizar siempre la misma sesión por cada rol para tener nociones generales "
             "quizás? Al menos una sesión por rol por proyecto")
    found = route.choose(asked, context_tokens=600_000, footprint=FOOTPRINT)
    assert found.mode == route.MAIN and "a question about the work" in found.reasons[0]
    assert route.asks("Should we cache the parsed config between runs, or is it cheap enough?")
    assert route.asks("Se ve bien. ¿Vale la pena limitar los turnos por intento en cada tipo de tarea?")
    order = "Fix the export so the tests pass again. Could it also log the row count while it is at it?"
    assert not route.asks(order)
    assert route.choose(order, context_tokens=600_000, footprint=FOOTPRINT).mode == route.WORKER
    assert not route.asks(MEDIUM)


def test_a_request_that_talks_to_the_session_stays_with_it():
    said = "Habla en español y dime si falta mergear algo o testear la nueva versión de locks en los repositorios"
    found = route.choose(said, context_tokens=600_000, footprint=FOOTPRINT)
    assert found.mode == route.MAIN and found.reasons == ("it talks to this session, which answers it",)
    assert route.talks("Tell me why the dispatcher waited on the second task before starting it")
    assert not route.talks("Realiza todas las pruebas sobre MECHA, testing de seguridad, inventa algo")
    assert not route.talks("Fix the summary line the report prints when a task stops for review")


def test_informing_imperatives_questions_about_state_and_reports_back_stay_with_the_session():
    for said in ("indícame cuál es el problema de la exportación de reportes en el módulo de facturas",
                 "Explícame por qué falló el workflow de ayer en el paso de integración",
                 "Muéstrame los pendientes que quedaron abiertos en el repositorio de la API",
                 "resume lo que hicimos hoy en el repositorio de la plataforma",
                 "Enumera los errores que encontraste en la última corrida del workflow",
                 "Qué opinas de cambiar el dispatcher para que use colas por repositorio",
                 "Tienes pendientes de la tarea de ayer en el repositorio de la API",
                 "Qué falta para terminar la migración del dispatcher a las colas",
                 "En qué quedó la tarea del gráfico de capacidad que mandamos ayer"):
        assert route.talks(said), said
        assert route.choose(said, context_tokens=600_000, footprint=FOOTPRINT).mode == route.MAIN, said
    pasted = ("- el botón de guardar no responde\n- el gráfico se ve cortado en el teléfono\n- el aviso no llega\n"
              "Necesito que me des todos los errores que veas, con su causa y dónde están en el código.")
    assert route.reports(pasted)
    assert route.choose(pasted, context_tokens=600_000, footprint=FOOTPRINT).mode == route.MAIN
    # a report that comes with a change, or after an order, is how the change is told: work
    assert not route.reports("- el botón no responde\nNecesito que lo arregles y me digas qué cambiaste.")
    assert not route.talks("Arregla el eje del gráfico de carga y dime qué cambiaste en el componente")
    assert not route.talks("Lista de cambios: arregla el eje, agrega la leyenda y corrige el color")
    assert not route.talks("Muestra el total bajo la tabla de tickets del tablero")
    assert not route.talks("Dimensiones del gráfico: ajusta el alto a 320 en el componente de carga")
    assert not route.talks("Resume the queued task with more turns in the same worktree")


def test_a_merge_with_review_words_is_work_not_reading():
    merge = ("Integra la rama locks/task-1394 en locks/task-1400 con cherry-pick y resuelve conflictos; son los "
             "arreglos de la auditoría de seguridad")
    found = route.choose(merge, context_tokens=600_000, footprint=FOOTPRINT)
    assert found.kind == "implement" and found.mode != route.MAIN
    assert route.choose("Audita el módulo de pagos y arregla lo que encuentres en el cálculo de cuotas",
                        context_tokens=600_000, footprint=FOOTPRINT).kind == "implement"


def test_the_corpus_of_real_requests_goes_where_each_belongs():
    """Requests people typed: one the router once sent the wrong way is kept here, so it never
    goes that way again. 'session' stays with the conversation; 'work' is not kept there for
    being a question or a word to the session (its size and the context still decide)."""
    corpus = json.loads((Path(__file__).parent / "corpus" / "requests.json").read_text())["requests"]
    wrong = []
    for item in corpus:
        found = route.choose(item["text"], context_tokens=600_000, footprint=FOOTPRINT)
        talked = route.talks(item["text"]) or route.asks(item["text"])
        if item["expect"] == "session" and found.mode != route.MAIN:
            wrong.append(f"to the session, went {found.mode}: {item['text'][:80]}")
        if item["expect"] == "work" and (talked or (found.mode == route.MAIN and len(item["text"].split()) >= 8)):
            wrong.append(f"is work, kept here ({found.reasons[0]}): {item['text'][:80]}")
    assert wrong == []


def test_a_reading_kind_a_session_gave_an_order_in_the_corpus_is_widened():
    from locks.orchestrate import Options, _caller_kind

    corpus = json.loads((Path(__file__).parent / "corpus" / "requests.json").read_text())["requests"]
    given = [item for item in corpus if "given_kind" in item]
    assert given
    assert [item["text"] for item in given
            if _caller_kind(Options(kind=item["given_kind"]), item["text"]).kind != "implement"] == []


def test_a_build_that_lists_many_items_is_asked_split_before_a_worker_starts():
    big = ("Implement the student side of the platform:\n- the opinion form and grade before each quiz opens\n"
           "- a quiz per module and a final exam module\n- submodules the server locks, by 403 or a locked state\n"
           "- the start screen with the calendar the API returns\n- the questions and answers forum\n"
           "- the library of resources\n- module covers read from coverPath")
    assert route.split_first(big) == "7 listed items"
    # a list of one-word items is one small task
    assert route.split_first("Implement the calculator:\n- mul\n- div\n- pow\n- mod\n- sqrt\n- abs") is None
    # a reading request, a small change and a short list are one task
    assert route.split_first("Explain why the forum is slow:\n- a\n- b\n- c\n- d\n- e\n- f") is None
    assert route.split_first("fix the typo in README") is None
    assert route.split_first("Add the export:\n- CSV\n- JSON\n- a test") is None
    assert route.split_first(big, "review-routine") is None
    pieces = ("Add the screens:\n- api/users.py\n- web/users.tsx\n- docs/users.md")
    assert route.split_first(pieces, "feature") == "3 pieces in folders of their own"


def test_a_heavy_read_is_said_at_the_next_prompt_with_what_an_explore_worker_costs_here(store: Store, git_repo,
                                                                                         monkeypatch):
    from locks import project, scribe
    from locks.store import home

    assert route.read_advice(route.READ_HEAVY_CHARS - 1, 9) is None
    said = route.read_advice(80_000, 12, {"passes": 4, "p50_s": 31.0, "cost_per_pass": 0.06})
    assert said.startswith("locks route: last turn this session read about 20k tokens of tool output into its own "
                           "context over 12 call(s)")
    assert "An explore worker here passed 4 task(s) at 31s and $0.06 a pass (observed)" in said
    assert "reads in its own context" in route.read_advice(80_000, 1, None)
    # The hook reads the scribe's count of the last turn, keeps it on the routed event, and says it.
    project.enroll(git_repo)
    monkeypatch.setenv("LOCKS_ROUTE", "on")
    state = scribe.fresh("s", git_repo / "t.jsonl")
    state["read"] = {"chars": 90_000, "calls": 7, "last_chars": 0, "last_calls": 0}
    scribe.save(home(), state)
    event = {"session_id": "s", "prompt": SMALL, "prompt_id": "p", "cwd": str(git_repo), "transcript_path": None}
    out = hooks.user_prompt_submit(event, store)
    said = out["hookSpecificOutput"]["additionalContext"]
    assert said.startswith("locks route: last turn this session read about 22k tokens")
    routed = store.session_events("s", ("routed",))[-1]["data"]
    assert routed["mode"] == "main" and routed["read_chars"] == 90_000 and routed["read_said"] is True
    assert store.kind_record("explore", "r") == {"passes": 0, "p50_s": None, "cost_per_pass": None}


def test_listed_rules_are_no_pieces_and_one_module_is_one_task():
    rules = ("Update the router docs:\n1. add a paragraph about split rules to docs/router.md\n"
             "2. add a bullet to docs/guide.md\n3. add src/locks/route.py a helper for rules\n"
             "4. Tests: cover each item in tests/test_route.py\n5. Standard library only. Do not edit CHANGELOG.md")
    assert route.pieces_of(rules) == [
        item for item in route.criteria_mod.items(rules) if not item.startswith(("Tests", "Standard"))]
    assert route.split_first(rules, "implement") is None
    # an item that opens with a verb of change is a piece whatever rule words follow it
    assert not route.is_rule("Add the export endpoint, only for admins, without a page size")
    assert route.is_rule("Do not edit CHANGELOG.md") and route.is_rule("Standard library only")
    one = ("Change the router:\n- add src/locks/route.py helper\n- add src/locks/route.py test hook\n"
           "- fix src/locks/route.py docs\n- update src/locks/route.py names\n- split src/locks/route.py\n"
           "- tidy src/locks/route.py imports " + "word " * 40)
    assert route.split_first(one, "implement") is None
    five = ("Build the platform:\n- add api/users.py endpoint\n- add web/users.tsx screen\n- add docs/users.md page\n"
            "- add db/users.sql table\n- add jobs/users.py worker")
    assert route.split_first(five, "feature") == "5 pieces in folders of their own"


def test_a_bug_sent_to_a_worker_is_told_of_the_bugfix_workflow():
    bug = ("Arregla el error: al aplicar dos cupones el total queda negativo en src/pricing/engine.py y el pedido "
           "se guarda igual en la base de datos sin validar nada")
    found = route.choose(bug, context_share=0.9)
    assert found.mode == route.WORKER and found.workflow == "bugfix" and found.data()["workflow"] == "bugfix"
    assert "locks workflow run bugfix" in found.advice()
    build = route.choose(MEDIUM, context_share=0.9)
    assert build.workflow == "" and "workflow" not in build.data() and "workflow run" not in build.advice()
