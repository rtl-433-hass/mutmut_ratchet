"""Config loading and CLI-override precedence.

Every constant the per-repo scripts used to hard-code now arrives from
``[tool.mutmut_ratchet]``. A silently mis-read setting would weaken the gate
without failing anything, so loading is strict: unknown keys and wrong types are
errors, and ``package_path`` is mandatory.
"""

from __future__ import annotations

from pathlib import Path

from consumers import PROFILES, ConsumerProfile, make_repo
import pytest

from mutmut_ratchet.config import (
    DEFAULT_ESCALATE_PATHS,
    DEFAULT_FALLBACK_SECONDS_PER_MUTANT,
    DEFAULT_FLOOR,
    DEFAULT_PRECISION,
    DEFAULT_TOLERANCE_FRACTION,
    DEFAULT_TOLERANCE_MUTANTS,
    ConfigError,
    find_pyproject,
    load_config,
)


def write_pyproject(root: Path, body: str) -> Path:
    path = root / "pyproject.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_defaults_match_the_original_script_constants(tmp_path: Path) -> None:
    path = write_pyproject(tmp_path, '[tool.mutmut_ratchet]\npackage_path = "my_pkg"\n')
    cfg = load_config(path)
    assert cfg.package_path == "my_pkg"
    assert cfg.package_dotted == "my_pkg"
    assert cfg.baseline == tmp_path / "scripts" / "mutation_baseline.json"
    assert cfg.timings == tmp_path / "scripts" / "mutation_timings.json"
    assert cfg.escalate_paths == frozenset(DEFAULT_ESCALATE_PATHS)
    assert cfg.explicit_test_sources == {}
    assert cfg.tolerance_fraction == DEFAULT_TOLERANCE_FRACTION == 0.02
    assert cfg.tolerance_mutants == DEFAULT_TOLERANCE_MUTANTS == 3
    assert cfg.precision == DEFAULT_PRECISION == 6
    assert cfg.floor == DEFAULT_FLOOR == 0.70
    assert cfg.fallback_seconds_per_mutant == DEFAULT_FALLBACK_SECONDS_PER_MUTANT


def test_nested_package_path_derives_the_dotted_name(tmp_path: Path) -> None:
    path = write_pyproject(
        tmp_path,
        '[tool.mutmut_ratchet]\npackage_path = "custom_components/rtl_433"\n',
    )
    cfg = load_config(path)
    assert cfg.package_dotted == "custom_components.rtl_433"
    assert cfg.source("coordinator/base.py") == (
        "custom_components/rtl_433/coordinator/base.py"
    )
    assert cfg.dotted("custom_components/rtl_433/coordinator/base.py") == (
        "custom_components.rtl_433.coordinator.base"
    )


def test_explicit_package_dotted_wins_over_the_derived_one(tmp_path: Path) -> None:
    path = write_pyproject(
        tmp_path,
        "[tool.mutmut_ratchet]\n"
        'package_path = "src/my_pkg"\n'
        'package_dotted = "my_pkg"\n',
    )
    assert load_config(path).package_dotted == "my_pkg"


def test_trailing_slashes_are_stripped_from_the_package_path(tmp_path: Path) -> None:
    path = write_pyproject(
        tmp_path, '[tool.mutmut_ratchet]\npackage_path = "my_pkg/"\n'
    )
    cfg = load_config(path)
    assert cfg.package_path == "my_pkg"
    assert cfg.package_dotted == "my_pkg"


def test_relative_data_paths_anchor_to_the_pyproject_directory(tmp_path: Path) -> None:
    """Anchoring to the project root (not the cwd) keeps the tool usable from a
    subdirectory, which the original ``Path(__file__).with_name`` default did too."""
    path = write_pyproject(
        tmp_path,
        "[tool.mutmut_ratchet]\n"
        'package_path = "my_pkg"\n'
        'baseline = "ci/base.json"\n'
        'timings = "ci/times.json"\n',
    )
    cfg = load_config(path)
    assert cfg.baseline == tmp_path / "ci" / "base.json"
    assert cfg.timings == tmp_path / "ci" / "times.json"


def test_absolute_data_paths_are_kept_as_given(tmp_path: Path) -> None:
    absolute = tmp_path / "elsewhere" / "base.json"
    path = write_pyproject(
        tmp_path,
        f'[tool.mutmut_ratchet]\npackage_path = "my_pkg"\nbaseline = "{absolute}"\n',
    )
    assert load_config(path).baseline == absolute


def test_overrides_beat_the_file_and_none_is_ignored(tmp_path: Path) -> None:
    path = write_pyproject(
        tmp_path,
        '[tool.mutmut_ratchet]\npackage_path = "my_pkg"\npackage_dotted = "my_pkg"\n',
    )
    cfg = load_config(
        path,
        overrides={
            "package_path": "other_pkg",
            "package_dotted": None,  # an unset CLI flag must not clobber the file
        },
    )
    assert cfg.package_path == "other_pkg"
    assert cfg.package_dotted == "my_pkg"


def test_override_can_supply_a_missing_required_setting(tmp_path: Path) -> None:
    path = write_pyproject(tmp_path, "[project]\nname = 'x'\n")
    assert load_config(path, overrides={"package_path": "my_pkg"}).package_path == (
        "my_pkg"
    )


def test_package_path_is_required(tmp_path: Path) -> None:
    path = write_pyproject(tmp_path, "[tool.mutmut_ratchet]\nfloor = 0.5\n")
    with pytest.raises(ConfigError, match="requires 'package_path'"):
        load_config(path)


@pytest.mark.parametrize(
    "body, message",
    [
        ('[tool.mutmut_ratchet]\npackage_path = "p"\nnope = 1\n', "unknown setting"),
        ("[tool.mutmut_ratchet]\npackage_path = 1\n", "must be str"),
        (
            '[tool.mutmut_ratchet]\npackage_path = "p"\ntolerance_mutants = true\n',
            "must be int",
        ),
        (
            '[tool.mutmut_ratchet]\npackage_path = "p"\ntolerance_fraction = "x"\n',
            "must be int or float",
        ),
        (
            '[tool.mutmut_ratchet]\npackage_path = "p"\nescalate_paths = [1]\n',
            "must be a list of strings",
        ),
        (
            '[tool.mutmut_ratchet]\npackage_path = "p"\n'
            "[tool.mutmut_ratchet.explicit_test_sources]\n"
            '"tests/a.py" = "b.py"\n',
            "must be a list of module paths",
        ),
        ('tool = { mutmut_ratchet = "nope" }\n', "must be a table"),
    ],
)
def test_malformed_config_is_rejected(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_pyproject(tmp_path, body))


def test_escalate_paths_must_be_a_list(tmp_path: Path) -> None:
    path = write_pyproject(
        tmp_path,
        '[tool.mutmut_ratchet]\npackage_path = "p"\nescalate_paths = 3\n',
    )
    with pytest.raises(ConfigError, match="must be list"):
        load_config(path)


def test_explicit_test_sources_must_be_a_table(tmp_path: Path) -> None:
    path = write_pyproject(
        tmp_path,
        '[tool.mutmut_ratchet]\npackage_path = "p"\nexplicit_test_sources = 3\n',
    )
    with pytest.raises(ConfigError, match="must be dict"):
        load_config(path)


def test_missing_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "absent.toml")


def test_no_pyproject_anywhere_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "deep" / "dir"
    root.mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.setattr("mutmut_ratchet.config.find_pyproject", lambda: None)
    with pytest.raises(ConfigError, match="no pyproject.toml found"):
        load_config()
    cfg = load_config(overrides={"package_path": "p"}, required=False)
    assert cfg.package_path == "p"
    assert cfg.baseline == Path.cwd() / "scripts" / "mutation_baseline.json"


def test_find_pyproject_walks_up_from_a_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_pyproject(tmp_path, '[tool.mutmut_ratchet]\npackage_path = "my_pkg"\n')
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert find_pyproject() == path
    # And the default (no explicit path) finds the same file.
    assert load_config().baseline == tmp_path / "scripts" / "mutation_baseline.json"


def test_find_pyproject_returns_none_when_there_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    monkeypatch.chdir(tmp_path)
    assert find_pyproject() is None


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
def test_real_consumer_blocks_round_trip(
    tmp_path: Path, profile: ConsumerProfile
) -> None:
    """The exact ``[tool.mutmut_ratchet]`` block each consumer will adopt loads
    back into precisely the constants its script hard-coded."""
    make_repo(tmp_path, profile)
    cfg = load_config(tmp_path / "pyproject.toml")
    assert cfg.package_path == profile.package_path
    assert cfg.package_dotted == profile.package_dotted
    assert cfg.escalate_paths == frozenset(profile.escalate_paths)
    assert cfg.explicit_test_sources == profile.explicit_test_sources


def test_overrides_are_validated_like_file_settings(tmp_path: Path) -> None:
    """A programmatic override gets the same strictness as a pyproject entry."""
    path = write_pyproject(tmp_path, '[tool.mutmut_ratchet]\npackage_path = "my_pkg"\n')
    with pytest.raises(ConfigError, match="unknown setting 'nope'"):
        load_config(path, overrides={"nope": "x"})
    with pytest.raises(ConfigError, match="'tolerance_mutants' must be int"):
        load_config(path, overrides={"tolerance_mutants": "three"})
    with pytest.raises(ConfigError, match="'explicit_test_sources' must be dict"):
        load_config(path, overrides={"explicit_test_sources": "nope"})
