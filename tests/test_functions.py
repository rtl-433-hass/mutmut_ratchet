"""Tests for resolving changed lines to the functions mutmut would mutate.

The mangled names are cross-checked against mutmut's own ``mangle_function_name``
rather than hard-coded twice, so a rename in mutmut surfaces here instead of
silently producing filter patterns that match no mutant.
"""

from __future__ import annotations

import textwrap

import pytest

from mutmut_ratchet.functions import functions_for_lines, mutable_functions


def src(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def test_mangling_matches_mutmut() -> None:
    """Our names must be byte-identical to mutmut's or the filters match nothing."""
    from mutmut.mutation.trampoline_templates import mangle_function_name

    spans = mutable_functions(
        src(
            """
            def top():
                pass

            class C:
                def method(self):
                    pass
            """
        )
    )
    assert set(spans) == {
        mangle_function_name(name="top", class_name=None),
        mangle_function_name(name="method", class_name="C"),
    }


def test_module_level_functions_and_methods_are_mutable() -> None:
    spans = mutable_functions(
        src(
            """
            def alpha():
                pass

            async def beta():
                pass

            class C:
                def gamma(self):
                    pass

                async def delta(self):
                    pass
            """
        )
    )
    assert set(spans) == {"x_alpha", "x_beta", "xǁCǁgamma", "xǁCǁdelta"}


def test_nested_class_methods_are_not_mutable() -> None:
    """mutmut's trampoline loop descends exactly one class level, so a method of
    a nested class gets no mutants; claiming it exists would filter to nothing."""
    spans = mutable_functions(
        src(
            """
            class Outer:
                class Inner:
                    def buried(self):
                        pass

                def surfaced(self):
                    pass
            """
        )
    )
    assert set(spans) == {"xǁOuterǁsurfaced"}


def test_nested_functions_belong_to_their_enclosing_function() -> None:
    """mutmut does not recurse into a function body, so an inner def is part of
    the outer function's mutants rather than a target of its own."""
    spans = mutable_functions(
        src(
            """
            def outer():
                def inner():
                    pass
                return inner
            """
        )
    )
    assert set(spans) == {"x_outer"}
    first, last = spans["x_outer"]
    assert first == 1 and last == 4, "the span must cover the nested def too"


def test_span_includes_decorators() -> None:
    """mutmut hashes the whole AST node, decorator_list included, so an edit to a
    decorator is an edit to the function."""
    spans = mutable_functions(
        src(
            """
            import functools

            @functools.cache
            @functools.wraps(print)
            def decorated():
                pass
            """
        )
    )
    first, last = spans["x_decorated"]
    assert first == 3, "the span starts at the first decorator, not at `def`"
    assert last == 6


def test_lines_inside_one_function_resolve_to_it() -> None:
    source = src(
        """
        def alpha():
            return 1

        def beta():
            return 2
        """
    )
    assert functions_for_lines(source, {2}) == {"x_alpha"}
    assert functions_for_lines(source, {5}) == {"x_beta"}
    assert functions_for_lines(source, {2, 5}) == {"x_alpha", "x_beta"}


@pytest.mark.parametrize(
    ("description", "line"),
    [
        ("an import", 1),
        ("a module-level statement", 3),
        ("a class body statement", 6),
    ],
)
def test_a_line_outside_every_function_escalates(description: str, line: int) -> None:
    """Module-level code can affect anything in the file, so the honest answer is
    'mutate the whole module', never 'mutate nothing'."""
    source = src(
        """
        import os

        CONSTANT = 1

        class C:
            attribute = 2

            def method(self):
                pass
        """
    )
    assert functions_for_lines(source, {line}) is None, description


def test_one_unmappable_line_escalates_the_whole_file() -> None:
    """Even when every other changed line maps cleanly."""
    source = src(
        """
        CONSTANT = 1

        def alpha():
            return CONSTANT
        """
    )
    assert functions_for_lines(source, {4}) == {"x_alpha"}
    assert functions_for_lines(source, {1, 4}) is None


def test_no_changed_lines_escalates() -> None:
    """A caller that could not work out what changed must not get an empty filter
    that silently mutates nothing."""
    assert functions_for_lines("def alpha():\n    pass\n", set()) is None


def test_unparseable_source_is_unknown_not_empty() -> None:
    """``None`` and ``{}`` are different answers: a module of pure assignments
    genuinely has no mutable function, while one that will not parse has an
    unknown set, and only the second must escalate."""
    assert mutable_functions("def broken(:\n") is None
    assert mutable_functions("CONSTANT = 1\n") == {}
    assert functions_for_lines("def broken(:\n", {1}) is None


def test_a_file_with_no_functions_escalates() -> None:
    assert functions_for_lines("CONSTANT = 1\n", {1}) is None
