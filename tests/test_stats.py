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

from mutmut_ratchet.stats import collect_stats, run

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
    assert list(payload) == ["files"]
    assert list(payload["files"]) == sorted(payload["files"])
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
