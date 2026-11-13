"""Mutation testing for locks' own rules: does the suite notice when a rule's code changes?

Coverage says a line ran; it does not say a test would fail if the line were wrong. This
makes one small change at a time to a module (a comparison flipped, `and` for `or`, a
boolean constant negated, an integer constant moved by one), runs the tests named for it,
and lists the changes no test caught: each is a rule the suite runs but does not pin.

    .venv/bin/python scripts/mutate.py src/locks/findings.py tests/test_checking.py [--limit N]

The module is always put back as it was, whatever happens. Slow: one test run per mutant,
so name the tests that cover the module, not the whole suite. stdlib only; no model.
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

FLIP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq,
        ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}


def sites(tree: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    """Every place one mutant can be made, with how it would read, in source order."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in FLIP:
                    yield node, f"compare:{i}"
        elif isinstance(node, ast.BoolOp):
            yield node, "boolop"
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            yield node, "bool"
        elif isinstance(node, ast.Constant) and type(node.value) is int and not isinstance(node.value, bool):
            yield node, "int"


def mutate(node: ast.AST, how: str) -> tuple[str, object]:
    """Apply one mutant in place; returns what to restore it with."""
    if how.startswith("compare:"):
        i = int(how.split(":")[1])
        old = node.ops[i]  # type: ignore[attr-defined]
        node.ops[i] = FLIP[type(old)]()  # type: ignore[attr-defined]
        return "ops", (i, old)
    if how == "boolop":
        old = node.op  # type: ignore[attr-defined]
        node.op = ast.Or() if isinstance(old, ast.And) else ast.And()  # type: ignore[attr-defined]
        return "op", old
    old = node.value  # type: ignore[attr-defined]
    node.value = (not old) if how == "bool" else old + 1  # type: ignore[attr-defined]
    return "value", old


def undo(node: ast.AST, field: str, old: object) -> None:
    if field == "ops":
        i, op = old  # type: ignore[misc]
        node.ops[i] = op  # type: ignore[attr-defined]
    else:
        setattr(node, field, old)


def run(module: Path, tests: list[str], limit: int | None, python: str) -> int:
    base = subprocess.run([python, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *tests],
                          capture_output=True, text=True, timeout=600)
    if base.returncode != 0:
        # A suite that fails unmutated would count every mutant as caught.
        print("the tests fail before any mutant; fix them or name others:\n" + base.stdout[-2000:])
        return 2
    original = module.read_text(encoding="utf-8")
    tree = ast.parse(original)
    found = list(sites(tree))[:limit]
    survived = []
    try:
        for n, (node, how) in enumerate(found, 1):
            field, old = mutate(node, how)
            try:
                module.write_text(ast.unparse(tree) + "\n", encoding="utf-8")
                done = subprocess.run([python, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *tests],
                                      capture_output=True, text=True, timeout=600)
                caught = done.returncode != 0
            except subprocess.TimeoutExpired:
                caught = True  # a mutant that hangs the suite is noticed
            finally:
                undo(node, field, old)
            line = getattr(node, "lineno", "?")
            print(f"[{n}/{len(found)}] {module}:{line} {how}: {'caught' if caught else 'SURVIVED'}", flush=True)
            if not caught:
                survived.append(f"{module}:{line} {how}")
    finally:
        module.write_text(original, encoding="utf-8")
    killed = len(found) - len(survived)
    print(f"\n{killed} of {len(found)} mutants caught ({killed / len(found):.0%})" if found else "no mutable site")
    for s in survived:
        print("  survived:", s)
    return 1 if survived else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("module", type=Path)
    parser.add_argument("tests", nargs="+")
    parser.add_argument("--limit", type=int, default=None, help="the first N sites only")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)
    return run(args.module, args.tests, args.limit, args.python)


if __name__ == "__main__":
    raise SystemExit(main())
