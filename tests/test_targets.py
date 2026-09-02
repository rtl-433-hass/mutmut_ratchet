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

from mutmut_ratchet.config import Config, load_config, patterns_for
from mutmut_ratchet.targets import resolve, run, source_for_test


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


def test_full_run_output_is_all_plus_two_blank_lines(
    repo: Path, config: Config
) -> None:
    assert _capture(["pyproject.toml"], config)[:3] == ["all", "", ""]


def test_nothing_in_scope_emits_scoped_with_blank_lines(
    repo: Path, config: Config
) -> None:
    assert _capture(["README.md"], config)[:3] == ["scoped", "", ""]


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
