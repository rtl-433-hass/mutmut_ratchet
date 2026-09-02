"""Tests for the mutation-score ratchet, including its tolerance-band maths.

The band is the load-bearing part: too narrow and every PR fails spuriously on
run-to-run mutmut noise, too wide and a real regression slips through. The
worked examples the original docstring quotes are pinned here as regression
tests, so the band cannot drift silently during refactors.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from mutmut_ratchet.config import Config, load_config
from mutmut_ratchet.ratchet import (
    check_floor,
    check_strict,
    run,
    score_for,
    scores_from_stats,
    tolerance_score,
    write_baseline,
)

PRECISION = 6


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut_ratchet]\npackage_path = "pkg"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    return load_config(tmp_path / "pyproject.toml")


def write_stats(path: Path, files: dict[str, dict[str, int]]) -> Path:
    path.write_text(json.dumps({"files": files}), encoding="utf-8")
    return path


def write_scores(config: Config, files: dict[str, dict[str, float]]) -> None:
    config.baseline.parent.mkdir(parents=True, exist_ok=True)
    config.baseline.write_text(
        json.dumps(
            {
                "floor": 0.7,
                "tolerance_fraction": 0.02,
                "tolerance_mutants": 3,
                "files": files,
            }
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# score reduction
# --------------------------------------------------------------------------


def test_timeouts_count_as_kills_and_skips_leave_the_denominator() -> None:
    killed, total, score = score_for(
        {"killed": 8, "timeout": 2, "skipped": 5, "total": 20}, PRECISION
    )
    assert (killed, total) == (10, 15)
    assert score == round(10 / 15, PRECISION)


def test_a_file_with_no_scoreable_mutants_scores_one() -> None:
    """All-skipped (or empty) files must not read as 0% and fail the floor."""
    assert score_for({"total": 4, "skipped": 4}, PRECISION) == (0, 0, 1.0)
    assert score_for({}, PRECISION) == (0, 0, 1.0)


def test_scores_from_stats_is_sorted_and_reduced() -> None:
    out = scores_from_stats(
        {
            "files": {
                "b.py": {"killed": 1, "total": 2},
                "a.py": {"killed": 2, "total": 2},
            }
        },
        PRECISION,
    )
    assert list(out) == ["a.py", "b.py"]
    assert out["a.py"] == {"killed": 2, "total": 2, "score": 1.0}
    assert out["b.py"] == {"killed": 1, "total": 2, "score": 0.5}


# --------------------------------------------------------------------------
# the tolerance band
# --------------------------------------------------------------------------


def test_band_is_the_absolute_mutant_cushion_on_small_files() -> None:
    """The docstring's ``number.py`` case: 29 scoreable mutants.

    2% of 29 is 0.58 mutants — less than one — so a flat percentage band would
    be a zero-tolerance gate on this file. ``max(0.02*29, 3) = 3`` mutants keeps
    a real cushion.
    """
    assert tolerance_score(29, 0.02, 3, PRECISION) == round(3 / 29, PRECISION)


def test_band_scales_with_the_fraction_on_large_files() -> None:
    """A 630-mutant module: 2% is 12.6 mutants, well past the 3-mutant floor."""
    assert tolerance_score(630, 0.02, 3, PRECISION) == round(12.6 / 630, PRECISION)
    assert tolerance_score(630, 0.02, 3, PRECISION) == 0.02


def test_band_is_zero_for_an_unscoreable_file() -> None:
    assert tolerance_score(0, 0.02, 3, PRECISION) == 0.0
    assert tolerance_score(-1, 0.02, 3, PRECISION) == 0.0


def test_scoped_27_of_29_passes_against_a_full_29_of_29_baseline() -> None:
    """The exact divergence the band exists for.

    A scoped PR run mutates only the changed module, so a couple of mutants that
    only tests in *other* files kill go unkilled: ``number.py`` scores 27/29
    scoped where the full baseline recorded 29/29. That 2-mutant (~6.9%) gap must
    not fail the PR gate.
    """
    current = {"number.py": {"killed": 27, "total": 29, "score": round(27 / 29, 6)}}
    baseline = {"files": {"number.py": {"killed": 29, "total": 29, "score": 1.0}}}
    assert check_floor(current, baseline, 0.02, 3, PRECISION) == []
    # One more lost mutant (26/29) is 3 below the baseline — still inside the
    # 3-mutant band, by exactly zero margin.
    current["number.py"] = {"killed": 26, "total": 29, "score": round(26 / 29, 6)}
    assert check_floor(current, baseline, 0.02, 3, PRECISION) == []
    # Four lost mutants is a real regression and must fail.
    current["number.py"] = {"killed": 25, "total": 29, "score": round(25 / 29, 6)}
    failures = check_floor(current, baseline, 0.02, 3, PRECISION)
    assert len(failures) == 1
    assert "REGRESSION number.py" in failures[0]
    assert "killed 25/29" in failures[0]


def test_a_large_file_gets_a_proportionally_larger_band() -> None:
    """~13 mutants of slack on a 630-mutant module, not 3."""
    baseline = {"files": {"base.py": {"killed": 630, "total": 630, "score": 1.0}}}
    ok = {"base.py": {"killed": 618, "total": 630, "score": round(618 / 630, 6)}}
    assert check_floor(ok, baseline, 0.02, 3, PRECISION) == []
    bad = {"base.py": {"killed": 617, "total": 630, "score": round(617 / 630, 6)}}
    assert len(check_floor(bad, baseline, 0.02, 3, PRECISION)) == 1


# --------------------------------------------------------------------------
# floor and strict modes
# --------------------------------------------------------------------------


def test_floor_reports_but_never_fails_on_a_new_file() -> None:
    out = io.StringIO()
    current = {"new.py": {"killed": 1, "total": 4, "score": 0.25}}
    assert check_floor(current, {"files": {}}, 0.02, 3, PRECISION, stdout=out) == []
    assert out.getvalue() == "  + new file (not yet in baseline): new.py score=0.250\n"


def test_floor_never_fails_on_an_improvement() -> None:
    baseline = {"files": {"a.py": {"killed": 5, "total": 10, "score": 0.5}}}
    current = {"a.py": {"killed": 10, "total": 10, "score": 1.0}}
    assert check_floor(current, baseline, 0.02, 3, PRECISION) == []


def test_strict_flags_drift_in_both_directions_and_set_mismatches() -> None:
    baseline = {
        "files": {
            "gone.py": {"killed": 1, "total": 1, "score": 1.0},
            "down.py": {"killed": 100, "total": 100, "score": 1.0},
            "up.py": {"killed": 50, "total": 100, "score": 0.5},
        }
    }
    current = {
        "down.py": {"killed": 80, "total": 100, "score": 0.8},
        "up.py": {"killed": 90, "total": 100, "score": 0.9},
        "new.py": {"killed": 1, "total": 2, "score": 0.5},
    }
    failures = check_strict(current, baseline, 0.02, 3, PRECISION)
    joined = "\n".join(failures)
    assert "MISSING in current results (in baseline): gone.py" in joined
    assert "UNRECORDED file (not in baseline): new.py score=0.500" in joined
    assert "DRIFT down.py: regressed 1.000 -> 0.800" in joined
    assert "DRIFT up.py: improved 0.500 -> 0.900" in joined
    assert len(failures) == 4


def test_strict_passes_inside_the_band() -> None:
    baseline = {"files": {"a.py": {"killed": 100, "total": 100, "score": 1.0}}}
    current = {"a.py": {"killed": 99, "total": 100, "score": 0.99}}
    assert check_strict(current, baseline, 0.02, 3, PRECISION) == []


# --------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------


def test_missing_stats_file_exits_two(project: Config) -> None:
    err = io.StringIO()
    stats = Path("absent.json")
    assert run(project, "floor", stats, stderr=err) == 2
    assert err.getvalue() == f"ERROR: stats file not found: {stats}\n"


def test_missing_baseline_exits_two_unless_updating(
    project: Config, tmp_path: Path
) -> None:
    stats = write_stats(tmp_path / "s.json", {"a.py": {"killed": 4, "total": 4}})
    err, out = io.StringIO(), io.StringIO()
    assert run(project, "floor", stats, stdout=out, stderr=err) == 2
    assert "ERROR: baseline not found" in err.getvalue()
    assert "run with --update to create it" in err.getvalue()

    out = io.StringIO()
    assert run(project, "floor", stats, update=True, stdout=out) == 0
    assert out.getvalue() == (f"Created baseline {project.baseline} with 1 files.\n")
    written = json.loads(project.baseline.read_text(encoding="utf-8"))
    assert written == {
        "files": {"a.py": {"killed": 4, "score": 1.0, "total": 4}},
        "floor": 0.7,
        "tolerance_fraction": 0.02,
        "tolerance_mutants": 3,
    }


def test_floor_pass_prints_the_summary_line(project: Config, tmp_path: Path) -> None:
    write_scores(project, {"a.py": {"killed": 9, "total": 10, "score": 0.9}})
    stats = write_stats(tmp_path / "s.json", {"a.py": {"killed": 9, "total": 10}})
    out = io.StringIO()
    assert run(project, "floor", stats, stdout=out) == 0
    assert out.getvalue() == (
        "Mutation score: 9/10 = 90.0% across 1 files "
        "(mode=floor, band=max(0.02xN, 3).\n"
        "OK: no mutation-score regression beyond tolerance.\n"
    )


def test_an_empty_stats_payload_reports_one_hundred_percent(
    project: Config, tmp_path: Path
) -> None:
    write_scores(project, {})
    stats = write_stats(tmp_path / "s.json", {})
    out = io.StringIO()
    assert run(project, "floor", stats, stdout=out) == 0
    assert "Mutation score: 0/0 = 100.0% across 0 files" in out.getvalue()


def test_a_regression_exits_one_and_lists_the_failures(
    project: Config, tmp_path: Path
) -> None:
    write_scores(project, {"a.py": {"killed": 100, "total": 100, "score": 1.0}})
    stats = write_stats(tmp_path / "s.json", {"a.py": {"killed": 50, "total": 100}})
    out = io.StringIO()
    assert run(project, "floor", stats, stdout=out) == 1
    assert "1 ratchet failure(s):" in out.getvalue()
    assert "REGRESSION a.py: 0.500 < baseline 1.000 - band 0.030" in out.getvalue()


def test_update_refuses_while_a_regression_stands(
    project: Config, tmp_path: Path
) -> None:
    write_scores(project, {"a.py": {"killed": 100, "total": 100, "score": 1.0}})
    stats = write_stats(tmp_path / "s.json", {"a.py": {"killed": 50, "total": 100}})
    out = io.StringIO()
    assert run(project, "floor", stats, update=True, stdout=out) == 1
    assert "Refusing to update baseline while regressions exist." in out.getvalue()
    # The committed baseline is untouched.
    assert json.loads(project.baseline.read_text())["files"]["a.py"]["score"] == 1.0


def test_update_ratchets_upward_only(project: Config, tmp_path: Path) -> None:
    """An improvement is recorded; a within-band dip must not lower the bar, and
    a file only in the baseline is preserved."""
    write_scores(
        project,
        {
            "up.py": {"killed": 50, "total": 100, "score": 0.5},
            "dip.py": {"killed": 100, "total": 100, "score": 1.0},
            "absent.py": {"killed": 3, "total": 3, "score": 1.0},
        },
    )
    stats = write_stats(
        tmp_path / "s.json",
        {
            "up.py": {"killed": 90, "total": 100},
            "dip.py": {"killed": 99, "total": 100},
        },
    )
    out = io.StringIO()
    assert run(project, "floor", stats, update=True, stdout=out) == 0
    assert f"Baseline updated: {project.baseline}" in out.getvalue()
    files = json.loads(project.baseline.read_text())["files"]
    assert files["up.py"]["score"] == 0.9  # improvement captured
    assert files["dip.py"]["score"] == 1.0  # within-band dip never lowers the bar
    assert files["absent.py"]["score"] == 1.0  # untouched file preserved
    assert list(files) == sorted(files)


def test_strict_mode_exits_one_on_drift(project: Config, tmp_path: Path) -> None:
    write_scores(project, {"a.py": {"killed": 50, "total": 100, "score": 0.5}})
    stats = write_stats(tmp_path / "s.json", {"a.py": {"killed": 100, "total": 100}})
    out = io.StringIO()
    assert run(project, "strict", stats, stdout=out) == 1
    assert "DRIFT a.py: improved 0.500 -> 1.000" in out.getvalue()
    assert "mode=strict" in out.getvalue()


def test_the_baselines_own_band_settings_win_over_the_defaults(
    project: Config, tmp_path: Path
) -> None:
    """A committed baseline carries the band it was written with, so refreshing
    the tolerance is a baseline edit rather than a workflow edit."""
    project.baseline.parent.mkdir(parents=True, exist_ok=True)
    project.baseline.write_text(
        json.dumps(
            {
                "tolerance_fraction": 0.5,
                "tolerance_mutants": 60,
                "files": {"a.py": {"killed": 100, "total": 100, "score": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    stats = write_stats(tmp_path / "s.json", {"a.py": {"killed": 50, "total": 100}})
    out = io.StringIO()
    assert run(project, "floor", stats, stdout=out) == 0
    assert "band=max(0.50xN, 60)" in out.getvalue()
    # An explicit CLI band overrides even the baseline's.
    out = io.StringIO()
    assert (
        run(
            project,
            "floor",
            stats,
            tolerance_fraction=0.0,
            tolerance_mutants=1,
            stdout=out,
        )
        == 1
    )
    assert "band=max(0.00xN, 1)" in out.getvalue()


def test_write_baseline_creates_missing_directories(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "ci" / "base.json"
    write_baseline(
        target, {"a.py": {"killed": 1, "total": 1, "score": 1.0}}, 0.02, 3, 0.7
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["floor"] == 0.7
    assert target.read_text(encoding="utf-8").endswith("\n")
