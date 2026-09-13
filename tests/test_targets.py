"""Tests for the PR mutation-target resolver (ported from both consumers).

The mutation job uses this to decide which source modules a PR should mutate.
The mapping is name-based with an explicit override table; a wrong entry silently
escalates every touching PR to a full run (or, worse, under-scopes and misses a
floor regression), so these tests keep the mapping honest and guard against the
mis-mapping class of bug (e.g. ``test_coordinator`` -> ``coordinator.py``, which
does not exist).

Every behavioural case runs against both real consumer configurations.
"""

from __future__ import annotations

import io
from pathlib import Path

from consumers import PROFILES, PYRTL_433, ConsumerProfile, make_repo
import pytest

from mutmut_ratchet.config import (
    Config,
    load_config,
    module_dotted_for_mutants,
    patterns_for,
)
from mutmut_ratchet.targets import (
    changed_lines_from_diff,
    resolve,
    run,
    source_for_test,
)


def _capture(changed: list[str], config: Config) -> list[str]:
    out = io.StringIO()
    assert run(changed, config, stdout=out) == 0
    return out.getvalue().split("\n")


def test_source_module_change_scopes_to_itself(
    repo: Path, profile: ConsumerProfile, config: Config
) -> None:
    module = profile.source(profile.modules[-1])
    full, sources = resolve([module], config)
    assert full is False
    assert sources == {module}


def test_conforming_test_maps_to_its_module(
    repo: Path, profile: ConsumerProfile, config: Config
) -> None:
    for test_file, module in profile.conforming_tests.items():
        full, sources = resolve([test_file], config)
        assert full is False, test_file
        assert sources == {profile.source(module)}, test_file


def test_full_run_trigger_escalates(
    repo: Path, profile: ConsumerProfile, config: Config
) -> None:
    for trigger in profile.escalate_paths:
        full, sources = resolve([trigger], config)
        assert full is True, trigger
        assert sources == set()


def test_docs_only_change_scopes_with_no_sources(repo: Path, config: Config) -> None:
    full, sources = resolve(["README.md", "docs/index.md", ""], config)
    assert full is False
    assert sources == set()


def test_unmappable_test_escalates(repo: Path, config: Config) -> None:
    """A test whose name maps to no source module must escalate to a full run;
    under-scoping would silently skip a floor check, so escalating is correct."""
    full, sources = resolve(["tests/test_totally_unknown_thing.py"], config)
    assert full is True
    assert sources == set()


def test_broad_tests_still_escalate(
    repo: Path, profile: ConsumerProfile, config: Config
) -> None:
    for test_file in profile.broad_tests:
        full, _ = resolve([test_file], config)
        assert full is True, test_file


def test_explicit_map_entries_scope_to_their_modules(
    repo: Path, profile: ConsumerProfile, config: Config
) -> None:
    for test_file, modules in profile.explicit_test_sources.items():
        full, sources = resolve([test_file], config)
        assert full is False, f"{test_file} should scope, not trigger a full run"
        assert sources == {profile.source(m) for m in modules}


def test_explicit_map_keys_and_targets_all_exist(
    repo: Path, profile: ConsumerProfile, config: Config
) -> None:
    """Every override key is a real test file and every value a real module.

    Prevents the table from rotting into mappings that point at files which no
    longer exist (a renamed test or module would otherwise pass silently).
    """
    for test_file, modules in config.explicit_test_sources.items():
        assert (repo / test_file).is_file(), f"missing test file: {test_file}"
        for module in modules:
            target = repo / config.source(module)
            assert target.is_file(), f"{test_file} maps to missing module: {module}"


def test_no_test_file_silently_escalates(
    repo: Path, profile: ConsumerProfile, config: Config
) -> None:
    """Every ``tests/test_*.py`` resolves, is explicitly mapped, or is declared broad.

    This is the guard for the original bug: a test whose name maps to a
    non-existent module (``test_coordinator`` -> ``coordinator.py``) silently
    escalates every touching PR to a full run.
    """
    offenders = []
    for path in sorted((repo / "tests").glob("test_*.py")):
        rel = f"tests/{path.name}"
        if rel in config.explicit_test_sources or rel in profile.broad_tests:
            continue
        if source_for_test(path.stem, config) is None:
            offenders.append(rel)
    assert not offenders, (
        "these tests escalate to a full mutation run but are neither in "
        f"explicit_test_sources nor declared broad: {offenders}"
    )


def test_a_non_test_file_in_tests_never_resolves(repo: Path, config: Config) -> None:
    assert source_for_test("helpers", config) is None
    assert source_for_test("conftest", config) is None


def test_scoped_output_is_three_lines_of_patterns_and_paths(
    repo: Path, profile: ConsumerProfile, config: Config
) -> None:
    changed = [profile.source("__init__.py"), profile.source(profile.modules[-1])]
    mode, patterns, paths = _capture(changed, config)[:3]
    assert mode == "scoped"
    assert paths.split() == sorted(changed)
    # Patterns come from the same derivation the sharder uses, so a package
    # ``__init__`` in scope is matched by its trampoline patterns.
    assert patterns.split() == patterns_for(sorted(changed), config)
    assert f"{config.package_dotted}.__init__.*" not in patterns.split()


def test_full_run_output_is_all_plus_three_blank_lines(
    repo: Path, config: Config
) -> None:
    assert _capture(["pyproject.toml"], config)[:4] == ["all", "", "", ""]


def test_nothing_in_scope_emits_scoped_with_blank_lines(
    repo: Path, config: Config
) -> None:
    assert _capture(["README.md"], config)[:4] == ["scoped", "", "", ""]


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
def test_nested_module_resolution_prefers_the_deepest_split(
    tmp_path: Path, profile: ConsumerProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``a_b_c`` tries ``a/b/c.py`` first, then ``a_b/c.py``, then ``a_b_c.py``.

    The order matters: a flat ``mapping_loader.py`` must not shadow a real
    ``mapping/_loader.py`` and vice versa.
    """
    make_repo(tmp_path, profile)
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path / "pyproject.toml")
    pkg = tmp_path / cfg.package_path
    (pkg / "a" / "b").mkdir(parents=True)
    (pkg / "a" / "b" / "c.py").write_text("", encoding="utf-8")
    assert source_for_test("test_a_b_c", cfg) == cfg.source("a/b/c.py")

    (pkg / "a_b").mkdir()
    (pkg / "a_b" / "d.py").write_text("", encoding="utf-8")
    assert source_for_test("test_a_b_d", cfg) == cfg.source("a_b/d.py")

    (pkg / "e_f_g.py").write_text("", encoding="utf-8")
    assert source_for_test("test_e_f_g", cfg) == cfg.source("e_f_g.py")


def test_test_mut_prefix_is_stripped_before_test(repo: Path, config: Config) -> None:
    """``test_mut_x`` must resolve to ``x.py``, not ``mut/x.py``."""
    module = config.package_path
    (Path(module) / "widget.py").write_text("", encoding="utf-8")
    assert source_for_test("test_mut_widget", config) == config.source("widget.py")


@pytest.mark.parametrize(
    "module, expected",
    [
        ("normalizer.py", ["{pkg}.normalizer.*"]),
        ("library/_loader.py", ["{pkg}.library._loader.*"]),
        # A package ``__init__.py`` *is* the package as far as mutmut mutant
        # names go, so it must never produce a ``....__init__.*`` pattern: that
        # matches no mutant, and the module would silently run zero of them.
        ("library/__init__.py", ["{pkg}.library.x_*", "{pkg}.library.x\u01c1*"]),
        ("__init__.py", ["{pkg}.x_*", "{pkg}.x\u01c1*"]),
    ],
)
def test_scoped_patterns_never_name_an_init_module(
    tmp_path: Path, module: str, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``pyrtl_433/library/__init__.py`` matched zero mutants.

    mutmut strips the ``__init__`` segment from mutant names, so a subpackage
    root's mutants are named ``<pkg>.library.x_lookup__mutmut_1``. Scoping a PR
    to that file with a ``<pkg>.library.__init__.*`` filter ran nothing while
    still reporting success, so the per-file floor was silently unenforced.
    """
    make_repo(tmp_path, PYRTL_433)
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path / "pyproject.toml")
    pkg = cfg.package_dotted
    changed = cfg.source(module)
    assert _capture([changed], cfg)[:3] == [
        "scoped",
        " ".join(e.format(pkg=pkg) for e in expected),
        changed,
    ]


# --- function-level narrowing ------------------------------------------------


def _capture_narrowed(
    changed: list[str], config: Config, changed_lines: dict[str, set[int]] | None
) -> list[str]:
    out = io.StringIO()
    assert run(changed, config, changed_lines=changed_lines, stdout=out) == 0
    return out.getvalue().split("\n")


def test_hunk_headers_become_new_file_line_numbers() -> None:
    diff = (
        "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        "--- a/pkg/mod.py\n"
        "+++ b/pkg/mod.py\n"
        "@@ -10,0 +11,3 @@\n"
        "+one\n+two\n+three\n"
        "@@ -40 +43 @@\n"
        "-old\n+new\n"
    )
    assert changed_lines_from_diff(diff) == {"pkg/mod.py": {11, 12, 13, 43}}


def test_a_pure_deletion_records_both_sides_of_the_cut() -> None:
    """``+c,0`` adds no lines, but the removal is still a change to whatever
    contained it -- and the removed code sat *between* two surviving lines, so
    both are recorded. Taking only the line above would blame the previous
    function for a decorator deleted off the next one."""
    diff = "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -20,5 +19,0 @@\n-gone\n"
    assert changed_lines_from_diff(diff) == {"pkg/mod.py": {19, 20}}


def test_a_deleted_file_contributes_no_lines() -> None:
    diff = "--- a/pkg/mod.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-a\n-b\n-c\n"
    assert changed_lines_from_diff(diff) == {}


def test_a_directly_changed_source_narrows_to_its_functions(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """The whole point: a one-line edit mutates one function, not the module."""
    source = profile.source(profile.modules[1])
    Path(source).write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
        encoding="utf-8",
    )
    mode, patterns, paths, functions = _capture_narrowed(
        [source], config, {source: {2}}
    )[:4]
    assert mode == "scoped"
    assert paths == source
    dotted = module_dotted_for_mutants(source, config)
    assert patterns == f"{dotted}.x_alpha__mutmut_*"
    assert functions == f"{dotted}.x_alpha"


def test_a_module_level_change_still_mutates_the_whole_module(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """An import or constant can affect every function, so narrowing would be
    unsound -- the patterns fall back to the whole-module form."""
    source = profile.source(profile.modules[1])
    Path(source).write_text(
        "import os\n\n\ndef alpha():\n    return os\n", encoding="utf-8"
    )
    mode, patterns, _, functions = _capture_narrowed([source], config, {source: {1}})[
        :4
    ]
    assert mode == "scoped"
    assert patterns.split() == patterns_for([source], config)
    # Line 4 names what the scope was narrowed *to*. This module was not, so it
    # contributes nothing -- the shard falls back to its whole-module pattern
    # when no name mentions it, and the gate reads what actually ran back out of
    # the stats payload rather than being told in advance.
    assert functions == ""


def test_a_source_reached_through_a_changed_test_is_never_narrowed(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """A weakened test can free a mutant anywhere in the module it exercises,
    not only in functions the test file happens to name."""
    test_file, module = next(iter(profile.conforming_tests.items()))
    source = profile.source(module)
    Path(source).write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
        encoding="utf-8",
    )
    # Line info exists, but it is for the *test* file, not the source.
    mode, patterns, paths, _ = _capture_narrowed([test_file], config, {test_file: {3}})[
        :4
    ]
    assert mode == "scoped"
    assert paths == source
    assert patterns.split() == patterns_for([source], config)


def test_no_line_information_falls_back_to_whole_modules(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """``git_changed_lines`` returning None must not read as 'nothing changed'."""
    source = profile.source(profile.modules[1])
    Path(source).write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _, patterns, _, _ = _capture_narrowed([source], config, None)[:4]
    assert patterns.split() == patterns_for([source], config)


def test_a_function_less_module_does_not_blank_the_function_list(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """A ``const.py`` of nothing but assignments has no mutable function -- which
    is an answer, not an unknown. Reading it as "cannot determine" would blank
    line 4 for the whole run, and an unfiltered per-function block then reports
    every never-executed mutant of the *narrowed* files as a fresh survivor."""
    narrowed = profile.source(profile.modules[1])
    Path(narrowed).write_text("def alpha():\n    return 1\n", encoding="utf-8")
    constants = profile.source(profile.modules[2])
    Path(constants).write_text("VALUE = 1\n", encoding="utf-8")

    functions = _capture_narrowed(
        [narrowed, constants], config, {narrowed: {1}, constants: {1}}
    )[3]
    assert functions.split() == [
        f"{module_dotted_for_mutants(narrowed, config)}.x_alpha"
    ]
