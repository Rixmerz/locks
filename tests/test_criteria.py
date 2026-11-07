from __future__ import annotations

from locks import criteria


def test_items_the_request_puts_out_of_scope_are_no_criteria():
    text = ("Arregla la parte A:\n- el boton guarda\n- el total redondea\n- no toques el header\n\n"
            "Parte B (no tocar):\n\n- migrar la tabla\n- renombrar el campo\n\nY al final:\n"
            "- un test nuevo\n- el reporte, para despues")
    assert criteria.items(text) == ["el boton guarda", "el total redondea", "no toques el header", "un test nuevo"]
    assert criteria.left_out(text) == ["migrar la tabla", "renombrar el campo", "el reporte, para despues"]
    brief = criteria.brief(text)
    assert "S4: un test nuevo" in brief and "Out of scope, as the request says" in brief
    assert "- migrar la tabla" in brief.split("Out of scope")[1]
    # A constraint before the list is no header: its items stay.
    assert criteria.items("No toques nada fuera de src.\n- a uno\n- b dos") == ["a uno", "b dos"]
    only_out = criteria.brief("Out of scope:\n- x\n- y")
    assert only_out.startswith("Out of scope") and "S1" not in only_out


def test_a_quoted_list_is_context_and_no_criteria():
    own = "Fix the total:\n- rounds once\n- keeps the sign\n\nThe request this fix comes from:\n"
    quoted = "\n".join(f"> {line}" for line in ["- one", "- two", "- three", "1. four", "2) five"])
    assert criteria.items(own + quoted) == ["rounds once", "keeps the sign"]
    assert criteria.ids(own + quoted) == ("S1", "S2")
    assert "five" not in criteria.brief(own + quoted)
    # A text whose only list is quoted lists nothing.
    assert criteria.items(quoted) == [] and criteria.ids("Fix it.\n" + quoted) == ()
    # Indented quotes are quotes too, and a quoted scope line neither opens nor closes the skipping.
    scoped = "Do these:\n- a\n- b\n  > Out of scope:\n- c"
    assert criteria.items(scoped) == ["a", "b", "c"] and criteria.left_out(scoped) == []
    assert criteria.left_out("> Out of scope:\n> - x\n- y\n- z") == []
