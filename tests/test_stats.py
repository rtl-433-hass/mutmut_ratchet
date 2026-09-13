"""Tests for the per-file mutmut stats exporter.

These run against a real ``mutmut`` module walk over a synthetic package plus
hand-written ``mutants/*.meta`` files in mutmut's own on-disk format, so the
status→bucket mapping is exercised end to end rather than through a stub.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from conftest import write_meta
from consumers import ConsumerProfile
import pytest

from mutmut_ratchet.stats import collect_function_stats, collect_stats, function_of, run

# mutmut's exit-code table, by the status each code maps to.
KILLED, SURVIVED, NO_TESTS = 1, 0, 5
INTERRUPTED, TIMEOUT, TYPE_CHECKED = 2, 36, 37
SKIPPED, SUSPICIOUS, SEGFAULT = 34, 35, -11


def test_every_mutmut_status_lands_in_the_right_bucket(
    repo: Path, profile: ConsumerProfile
) -> None:
    """Detections (kill, timeout, segfault, pytest-internal-error) count as
    killed; everything unproven counts against the score and is never suppressed.
    """
    source = profile.source(profile.modules[1])
    write_meta(
        repo,
        source,
        {
            "k1": KILLED,
            "k2": 3,  # internal pytest error is a detection
            "k3": SEGFAULT,
            "t1": TIMEOUT,
            "s1": SURVIVED,
            "s2": None,  # "not checked" is a recorded survivor, not a free pass
            "s3": INTERRUPTED,
            "n1": NO_TESTS,
            "x1": SKIPPED,
            "x2": TYPE_CHECKED,
            "q1": SUSPICIOUS,
            "q2": 9999,  # unknown code -> suspicious, never silently dropped
        },
    )
    stats = collect_stats()
    assert stats == {
        source: {
            "killed": 3,
            "survived": 3,
            "timeout": 1,
            "suspicious": 2,
            "skipped": 2,
            "no_tests": 1,
            "total": 12,
            # s2 alone: recorded but never run, a subset of the 3 survivors.
            "not_checked": 1,
        }
    }


def test_paths_restricts_the_output_to_the_scoped_modules(
    repo: Path, profile: ConsumerProfile
) -> None:
    """Required after a filtered ``mutmut run``: modules outside the filter stay
    "not checked", which would otherwise read as 0% and fail the floor."""
    a, b = (profile.source(m) for m in profile.modules[1:3])
    write_meta(repo, a, {"m1": 1})
    write_meta(repo, b, {"m1": 0})
    assert set(collect_stats()) == {a, b}
    assert set(collect_stats([a])) == {a}
    # A path is normalised before comparison, so "./a.py" matches "a.py".
    assert set(collect_stats([f"./{a}"])) == {a}
    assert collect_stats([]) == collect_stats(None)


def test_modules_without_meta_or_results_are_omitted(
    repo: Path, profile: ConsumerProfile
) -> None:
    """A module mutmut never ran has no score to compare, so it must not appear
    (appearing with 0 mutants would read as a 100% score it has not earned)."""
    empty = profile.source(profile.modules[1])
    write_meta(repo, empty, {})
    assert collect_stats() == {}


def test_output_is_sorted_json_on_stdout(repo: Path, profile: ConsumerProfile) -> None:
    for module in profile.modules[1:4]:
        write_meta(repo, profile.source(module), {"m1": 1})
    out = io.StringIO()
    assert run(stdout=out) == 0
    text = out.getvalue()
    assert text.endswith("\n")
    payload = json.loads(text)
    assert list(payload) == ["files", "functions"]
    assert list(payload["files"]) == sorted(payload["files"])
    assert list(payload["functions"]) == sorted(payload["functions"])
    assert all(
        set(f)
        == {
            "killed",
            "survived",
            "timeout",
            "suspicious",
            "skipped",
            "no_tests",
            "total",
            "not_checked",
        }
        for f in payload["files"].values()
    )


def test_stats_are_ratchet_ready(repo: Path, profile: ConsumerProfile) -> None:
    """The payload feeds straight into the ratchet's score reduction."""
    from mutmut_ratchet.ratchet import scores_from_stats

    source = profile.source(profile.modules[1])
    write_meta(repo, source, {"a": 1, "b": 36, "c": 0, "d": 34})
    scores = scores_from_stats({"files": collect_stats()}, 6)
    # 1 killed + 1 timeout out of (4 total - 1 skipped) = 2/3.
    assert scores[source] == {"killed": 2, "total": 3, "score": round(2 / 3, 6)}


@pytest.mark.parametrize("bad", ["not-a-module.py"])
def test_paths_naming_an_unmutated_module_yields_nothing(repo: Path, bad: str) -> None:
    assert collect_stats([bad]) == {}


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("pkg.mod.x_parse__mutmut_1", "pkg.mod.x_parse"),
        ("pkg.mod.x_parse__mutmut_137", "pkg.mod.x_parse"),
        ("pkg.mod.xǁCoordinatorǁrefresh__mutmut_2", "pkg.mod.xǁCoordinatorǁrefresh"),
        # A key mutmut itself would assert on degrades to its own function rather
        # than raising in the middle of a CI gate.
        ("hand_written_key", "hand_written_key"),
    ],
)
def test_function_of_splits_on_the_mutant_marker(key: str, expected: str) -> None:
    assert function_of(key) == expected


def test_function_stats_group_mutants_by_their_function(
    repo: Path, profile: ConsumerProfile
) -> None:
    """Each function's tally is complete on its own, which is what lets a
    function-scoped run be gated when the enclosing file's score cannot be."""
    source = profile.source(profile.modules[1])
    mod = source[: -len(".py")].replace("/", ".")
    write_meta(
        repo,
        source,
        {
            f"{mod}.x_parse__mutmut_1": KILLED,
            f"{mod}.x_parse__mutmut_2": KILLED,
            f"{mod}.x_parse__mutmut_3": SURVIVED,
            f"{mod}.xǁCǁrun__mutmut_1": SURVIVED,
        },
    )
    stats = collect_function_stats()
    assert set(stats[source]) == {f"{mod}.x_parse", f"{mod}.xǁCǁrun"}
    assert stats[source][f"{mod}.x_parse"]["killed"] == 2
    assert stats[source][f"{mod}.x_parse"]["survived"] == 1
    assert stats[source][f"{mod}.x_parse"]["total"] == 3
    assert stats[source][f"{mod}.xǁCǁrun"]["killed"] == 0
    assert stats[source][f"{mod}.xǁCǁrun"]["total"] == 1


def test_a_function_the_run_skipped_is_reported_but_flagged(
    repo: Path, profile: ConsumerProfile
) -> None:
    """A function-scoped run leaves the rest of the file "not checked". Those
    are reported faithfully -- this exporter never hides a mutant -- but the
    flag is what lets the gate tell them from real survivors, with no
    out-of-band list of what the run covered."""
    source = profile.source(profile.modules[1])
    mod = source[: -len(".py")].replace("/", ".")
    write_meta(
        repo,
        source,
        {
            f"{mod}.x_touched__mutmut_1": KILLED,
            f"{mod}.x_untouched__mutmut_1": None,
            f"{mod}.x_untouched__mutmut_2": None,
        },
    )
    every = collect_function_stats()
    assert every[source][f"{mod}.x_touched"]["killed"] == 1
    assert every[source][f"{mod}.x_touched"]["not_checked"] == 0

    untouched = every[source][f"{mod}.x_untouched"]
    assert untouched["survived"] == 2, "an unrun mutant still counts against us"
    assert untouched["not_checked"] == untouched["total"], (
        "...and being wholly unrun is what marks it as no measurement at all"
    )


def test_not_checked_is_a_subset_of_survived(
    repo: Path, profile: ConsumerProfile
) -> None:
    """``not_checked`` never double-counts: it narrows ``survived``, so a caller
    that ignores it still sees the conservative total."""
    source = profile.source(profile.modules[1])
    mod = source[: -len(".py")].replace("/", ".")
    write_meta(
        repo,
        source,
        {
            f"{mod}.x_f__mutmut_1": SURVIVED,  # genuinely survived
            f"{mod}.x_f__mutmut_2": None,  # never run
        },
    )
    tally = collect_function_stats()[source][f"{mod}.x_f"]
    assert tally["survived"] == 2
    assert tally["not_checked"] == 1
    assert tally["total"] == 2


def test_file_and_function_tallies_agree(repo: Path, profile: ConsumerProfile) -> None:
    """The two views are reductions of the same data, so they must add up."""
    source = profile.source(profile.modules[1])
    mod = source[: -len(".py")].replace("/", ".")
    write_meta(
        repo,
        source,
        {
            f"{mod}.x_a__mutmut_1": KILLED,
            f"{mod}.x_a__mutmut_2": SURVIVED,
            f"{mod}.x_b__mutmut_1": TIMEOUT,
            f"{mod}.x_b__mutmut_2": NO_TESTS,
            f"{mod}.x_b__mutmut_3": SKIPPED,
        },
    )
    whole = collect_stats()[source]
    per_function = collect_function_stats()[source]
    for bucket in ("killed", "survived", "timeout", "skipped", "no_tests", "total"):
        assert whole[bucket] == sum(f[bucket] for f in per_function.values()), bucket
