"""Tests for the per-file mutmut runtime profile exporter."""

from __future__ import annotations

import io
import json
from pathlib import Path

from conftest import write_meta
from consumers import ConsumerProfile

from mutmut_ratchet.config import Config
from mutmut_ratchet.timings import collect_timings, run


def test_timings_sum_each_files_mutant_durations(
    repo: Path, profile: ConsumerProfile
) -> None:
    a, b = (profile.source(m) for m in profile.modules[1:3])
    write_meta(repo, a, {"m1": 1, "m2": 1}, durations={"m1": 1.5, "m2": 2.25})
    write_meta(repo, b, {"m1": 1}, durations={"m1": 0.0005})
    timings = collect_timings()
    assert timings == {a: 3.75, b: 0.001}  # rounded to 3 decimals
    assert list(timings) == sorted(timings)


def test_files_with_no_recorded_durations_are_omitted(
    repo: Path, profile: ConsumerProfile
) -> None:
    """A module absent from the profile falls back to a count-based estimate in
    the sharder, which is better than pretending it took zero seconds."""
    source = profile.source(profile.modules[1])
    write_meta(repo, source, {"m1": 1}, durations={})
    assert collect_timings() == {}


def test_no_timing_data_at_all_exits_two(repo: Path, config: Config) -> None:
    out, err = io.StringIO(), io.StringIO()
    assert run(config.timings, stdout=out, stderr=err) == 2
    assert "no timing data found in mutants/*.meta" in err.getvalue()
    assert "run a full `mutmut run` first" in err.getvalue()
    assert out.getvalue() == ""
    assert not config.timings.exists()


def test_the_profile_is_written_sorted_with_a_summary_line(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    for i, module in enumerate(profile.modules[1:4], start=1):
        write_meta(
            repo,
            profile.source(module),
            {"m1": 1},
            durations={"m1": float(i * 10)},
        )
    out = io.StringIO()
    # The configured default lives under scripts/, which does not exist yet.
    assert not config.timings.parent.exists()
    assert run(config.timings, stdout=out) == 0
    assert out.getvalue() == (
        f"Wrote {config.timings} (3 files, 60s total mutmut time).\n"
    )
    payload = json.loads(config.timings.read_text(encoding="utf-8"))
    assert list(payload) == ["files"]
    assert list(payload["files"]) == sorted(payload["files"])
    assert sum(payload["files"].values()) == 60.0


def test_the_written_profile_feeds_the_sharder(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    from mutmut_ratchet.shards import load_timings

    source = profile.source(profile.modules[1])
    write_meta(repo, source, {"m1": 1}, durations={"m1": 12.5})
    assert run(config.timings, stdout=io.StringIO()) == 0
    assert load_timings(config.timings) == {source: 12.5}


def test_an_explicit_out_path_is_honoured(
    repo: Path, profile: ConsumerProfile, tmp_path: Path
) -> None:
    write_meta(
        repo,
        profile.source(profile.modules[1]),
        {"m1": 1},
        durations={"m1": 1.0},
    )
    out_path = Path("elsewhere.json")
    assert run(out_path, stdout=io.StringIO()) == 0
    assert (repo / out_path).is_file()
