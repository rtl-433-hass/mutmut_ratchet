"""Resolve changed source lines to the mutmut functions that contain them.

Scoping a PR's mutation run to changed *files* still re-mutates every function in
those files. For a large module that is most of the gate's cost: a one-line edit
to a 750-line ``__init__.py`` re-runs all of its mutants. Scoping to the changed
*functions* is what the mature tools in other ecosystems do, and mutmut supports
it -- its mutant names are ``<module>.<mangled function>__mutmut_<n>``, so a
filter pattern can select a single function.

This module is the missing half: given a file's source and the lines a diff
touched, it names the functions to filter to.

**What mutmut actually mutates.** Only module-level functions and the methods of
module-level classes get trampolines (see ``file_mutation.py``: the emission loop
walks ``module.body`` for ``FunctionDef``, and for a ``ClassDef`` walks
``cls.body.body`` one level for its methods -- it never descends further). So:

* ``def foo`` at module level        -> ``x_foo``
* ``def foo`` in ``class C``         -> ``xǁCǁfoo``
* ``def foo`` in a class in a class  -> *no mutants at all*
* ``def foo`` nested inside ``def``  -> part of the enclosing function

A function's span includes its decorators, because mutmut hashes the whole AST
node (``decorator_list`` included) when deciding whether a function changed, so
an edit to a decorator is an edit to the function.

**Escalation is the safe default.** A changed line that belongs to no mutable
function -- a module-level statement, an import, a class body, a nested class's
method -- can affect every function in the file, so :func:`functions_for_lines`
returns ``None`` for "mutate the whole module" rather than silently narrowing to
nothing. Under-scoping here would not fail loudly; it would quietly stop testing
code, which is the one failure mode a mutation gate must not have.
"""

from __future__ import annotations

import ast

__all__ = [
    "MANGLED_CLASS_SEPARATOR",
    "MANGLED_PREFIXES",
    "MUTANT_MARKER",
    "functions_for_lines",
    "mutable_functions",
]

# How mutmut names things. This module owns these facts because it is the one
# that has to reproduce them exactly; everything else imports them from here
# rather than spelling them again.
#
# They are hard-coded rather than imported from mutmut so this module -- and
# `config`, `targets` and `ratchet`, which reach it -- keep working without
# mutmut installed, the property pyproject.toml's dependency comment records.
# (mutmut is imported lazily by `stats`, `timings` and `shards`, which genuinely
# need its module walk and meta files.)

#: ``mutmut.mutation.trampoline_templates.CLASS_NAME_SEPARATOR``.
MANGLED_CLASS_SEPARATOR = "ǁ"
#: Separates a mutant's function from its ordinal: ``x_parse__mutmut_3``.
MUTANT_MARKER = "__mutmut_"
#: What every mangled function name starts with -- ``x_foo`` for a plain
#: function, ``xǁClassǁmethod`` for a method. No module name begins with
#: either, which is what lets a name be told apart from a path segment.
MANGLED_PREFIXES = ("x_", f"x{MANGLED_CLASS_SEPARATOR}")

_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _mangle(name: str, class_name: str | None) -> str:
    """mutmut's mangled key for a function, matching ``mangle_function_name``."""
    if class_name:
        return f"x{MANGLED_CLASS_SEPARATOR}{class_name}{MANGLED_CLASS_SEPARATOR}{name}"
    return f"x_{name}"


def _span(node: ast.AST) -> tuple[int, int]:
    """The 1-based inclusive line range a function occupies, decorators included."""
    assert isinstance(node, _FUNC_NODES)
    first = node.lineno
    for decorator in node.decorator_list:
        first = min(first, decorator.lineno)
    return first, node.end_lineno or node.lineno


def mutable_functions(source: str) -> dict[str, tuple[int, int]] | None:
    """Map every function mutmut would mutate to its ``(first, last)`` line span.

    ``None`` when ``source`` does not parse, which is a different answer from an
    empty mapping: a module of nothing but assignments genuinely has no mutable
    function, while one that will not parse has an *unknown* set. Callers that
    must choose between escalating and contributing nothing need to tell those
    apart, and returning the sentinel here is cheaper than a second parse to ask.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    spans: dict[str, tuple[int, int]] = {}
    for node in tree.body:
        if isinstance(node, _FUNC_NODES):
            spans[_mangle(node.name, None)] = _span(node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, _FUNC_NODES):
                    spans[_mangle(child.name, node.name)] = _span(child)
    return spans


def functions_for_lines(source: str, lines: set[int]) -> set[str] | None:
    """The functions covering ``lines``, or ``None`` to mutate the whole module.

    ``None`` means at least one changed line sits outside every mutable function
    (module-level code, an import, a class body, a nested class's method) and the
    change could therefore affect anything in the file. ``lines`` being empty
    also escalates: a caller that could not determine what changed must not be
    handed an empty filter that mutates nothing.
    """
    if not lines:
        return None

    spans = mutable_functions(source)
    if not spans:
        return None

    covering: set[str] = set()
    for line in lines:
        for name, (first, last) in spans.items():
            if first <= line <= last:
                covering.add(name)
                break
        else:
            # This line belongs to no mutable function.
            return None
    return covering
