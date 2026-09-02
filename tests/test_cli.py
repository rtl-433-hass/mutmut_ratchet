"""CLI dispatch, flag surface, override precedence, and exit codes.

The consumers' workflows are a mechanical rewrite of
``python scripts/mutation_<x>.py <flags>`` into ``mutmut-ratchet <x> <flags>``,
so the flag names, the stdout, and the exit codes are the contract these tests
pin down.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from conftest import write_meta
from consumers import ConsumerProfile
import pytest

from mutmut_ratchet.cli import build_parser, main


def call(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------
# parser surface
# --------------------------------------------------------------------------


def test_a_subcommand_is_required() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["ratchet", "--stats", "s.json"],  # missing --mode
        ["ratchet", "--mode", "floor"],  # missing --stats
        ["ratchet", "--mode", "nope", "--stats", "s.json"],  # bad choice
        ["shards", "--shard", "0"],  # missing --of
        ["shards", "--of", "4"],  # missing --shard
    ],
)
def test_missing_or_invalid_required_flags_exit_two(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "command, flags",
    [
        (
            "ratchet",
            {
                "mode",
                "stats",
                "baseline",
                "tolerance_fraction",
                "tolerance_mutants",
                "update",
            },
        ),
        ("shards", {"shard", "of", "baseline", "timings", "restrict"}),
        ("stats", {"paths"}),
        ("targets", {"changed"}),
        ("timings", {"out"}),
    ],
)
def test_each_subcommand_keeps_the_original_scripts_flags(
    command: str, flags: set[str]
) -> None:
    """Every flag the copy-pasted scripts accepted still exists, plus the three
    common config flags, so the workflow rewrite stays mechanical."""
    minimal = {
        "ratchet": ["--mode", "floor", "--stats", "s.json"],
        "shards": ["--shard", "0", "--of", "1"],
        "stats": [],
        "targets": [],
        "timings": [],
    }[command]
    args = build_parser().parse_args([command, *minimal])
    assert flags | {"config", "package_path", "package_dotted", "command"} == set(
        vars(args)
    )


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------


def test_targets_dispatch(repo: Path, profile: ConsumerProfile) -> None:
    module = profile.source(profile.modules[-1])
    code, out, _ = call("targets", module)
    assert code == 0
    assert out.splitlines() == [
        "scoped",
        f"{profile.package_dotted}.{profile.modules[-1][: -len('.py')]}.*",
        module,
    ]
    assert call("targets", "pyproject.toml")[1].splitlines() == ["all", "", ""]


def test_targets_reads_stdin_when_no_paths_are_given(
    repo: Path, profile: ConsumerProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``uv run mutmut-ratchet targets $changed`` with an empty ``$changed``
    falls back to stdin, exactly as the original script did."""
    module = profile.source(profile.modules[-1])
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{module}\nREADME.md\n"))
    code, out, _ = call("targets")
    assert code == 0
    assert out.splitlines()[0] == "scoped"
    assert out.splitlines()[2] == module


def test_shards_dispatch(repo: Path, profile: ConsumerProfile) -> None:
    code, out, _ = call("shards", "--shard", "0", "--of", "1")
    assert code == 0
    patterns, paths = out.splitlines()
    assert sorted(paths.split()) == sorted(profile.source(m) for m in profile.modules)
    assert patterns

    code, _, err = call("shards", "--shard", "9", "--of", "2")
    assert code == 2
    assert "--shard must satisfy" in err


def test_shards_restrict_dispatch(repo: Path, profile: ConsumerProfile) -> None:
    wanted = [profile.source(m) for m in profile.modules[:2]]
    collected: list[str] = []
    for shard in range(3):
        _, out, _ = call(
            "shards", "--shard", str(shard), "--of", "3", "--restrict", *wanted
        )
        collected += out.splitlines()[1].split()
    assert sorted(collected) == sorted(wanted)


def test_stats_dispatch(repo: Path, profile: ConsumerProfile) -> None:
    source = profile.source(profile.modules[1])
    write_meta(repo, source, {"m1": 1, "m2": 0})
    code, out, _ = call("stats")
    assert code == 0
    assert json.loads(out)["files"][source]["killed"] == 1
    code, out, _ = call("stats", "--paths", "nothing.py")
    assert code == 0
    assert json.loads(out) == {"files": {}}


def test_timings_dispatch(repo: Path, profile: ConsumerProfile) -> None:
    write_meta(
        repo,
        profile.source(profile.modules[1]),
        {"m1": 1},
        durations={"m1": 3.0},
    )
    code, out, _ = call("timings", "--out", "t.json")
    assert code == 0
    assert out.startswith("Wrote t.json (1 files, 3s total mutmut time).")
    assert json.loads((repo / "t.json").read_text())["files"]

    # And with no --out it writes the configured default.
    code, out, _ = call("timings")
    assert code == 0
    assert (repo / "scripts" / "mutation_timings.json").is_file()


def test_timings_with_no_data_exits_two(repo: Path) -> None:
    code, _, err = call("timings", "--out", "t.json")
    assert code == 2
    assert "no timing data found" in err


def test_ratchet_dispatch_round_trip(repo: Path, profile: ConsumerProfile) -> None:
    """The full workflow shape: run -> stats -> create baseline -> gate."""
    source = profile.source(profile.modules[1])
    # 100 mutants, so the 3-mutant tolerance band cannot swamp the signal.
    write_meta(repo, source, {f"m{i}": (1 if i < 90 else 0) for i in range(100)})
    _, stats_json, _ = call("stats", "--paths", source)
    (repo / "stats.json").write_text(stats_json, encoding="utf-8")

    code, out, err = call("ratchet", "--mode", "floor", "--stats", "stats.json")
    assert code == 2
    assert "baseline not found" in err

    code, out, _ = call(
        "ratchet", "--mode", "floor", "--stats", "stats.json", "--update"
    )
    assert code == 0
    assert out.startswith("Created baseline ")

    code, out, _ = call("ratchet", "--mode", "floor", "--stats", "stats.json")
    assert code == 0
    assert "OK: no mutation-score regression beyond tolerance." in out

    # A wholesale collapse fails the gate.
    write_meta(repo, source, dict.fromkeys((f"m{i}" for i in range(100)), 0))
    _, stats_json, _ = call("stats", "--paths", source)
    (repo / "stats.json").write_text(stats_json, encoding="utf-8")
    code, out, _ = call("ratchet", "--mode", "floor", "--stats", "stats.json")
    assert code == 1
    assert "REGRESSION" in out


def test_ratchet_strict_mode_and_band_flags(repo: Path) -> None:
    (repo / "stats.json").write_text(
        json.dumps({"files": {"a.py": {"killed": 5, "total": 10}}}), encoding="utf-8"
    )
    baseline = repo / "base.json"
    baseline.write_text(
        json.dumps({"files": {"a.py": {"killed": 10, "total": 10, "score": 1.0}}}),
        encoding="utf-8",
    )
    code, out, _ = call(
        "ratchet",
        "--mode",
        "strict",
        "--stats",
        "stats.json",
        "--baseline",
        "base.json",
        "--tolerance-fraction",
        "0.9",
        "--tolerance-mutants",
        "0",
    )
    assert code == 0
    assert "band=max(0.90xN, 0)" in out


# --------------------------------------------------------------------------
# configuration plumbing
# --------------------------------------------------------------------------


def test_package_path_flag_overrides_the_pyproject_value(
    repo: Path, profile: ConsumerProfile
) -> None:
    (repo / "other_pkg").mkdir()
    (repo / "other_pkg" / "thing.py").write_text("", encoding="utf-8")
    code, out, _ = call("targets", "--package-path", "other_pkg", "other_pkg/thing.py")
    assert code == 0
    assert out.splitlines() == ["scoped", "other_pkg.thing.*", "other_pkg/thing.py"]


def test_package_dotted_flag_overrides_the_derived_name(repo: Path) -> None:
    (repo / "src").mkdir()
    (repo / "src" / "lib").mkdir()
    (repo / "src" / "lib" / "core.py").write_text("", encoding="utf-8")
    code, out, _ = call(
        "targets",
        "--package-path",
        "src/lib",
        "--package-dotted",
        "lib",
        "src/lib/core.py",
    )
    assert code == 0
    assert out.splitlines()[1] == "lib.core.*"


def test_config_flag_points_at_another_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "pyproject.toml").write_text(
        '[tool.mutmut_ratchet]\npackage_path = "widget"\n', encoding="utf-8"
    )
    work = tmp_path / "work"
    (work / "widget").mkdir(parents=True)
    (work / "widget" / "core.py").write_text("", encoding="utf-8")
    (work / "pyproject.toml").write_text(
        '[tool.mutmut_ratchet]\npackage_path = "not_this_one"\n', encoding="utf-8"
    )
    monkeypatch.chdir(work)
    code, out, _ = call(
        "targets", "--config", str(elsewhere / "pyproject.toml"), "widget/core.py"
    )
    assert code == 0
    assert out.splitlines()[1] == "widget.core.*"


def test_a_relative_baseline_flag_is_relative_to_the_cwd(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured paths anchor to the project root, but a path typed on the
    command line must mean what the shell means by it."""
    sub = repo / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    (sub / "stats.json").write_text(
        json.dumps({"files": {"a.py": {"killed": 1, "total": 1}}}), encoding="utf-8"
    )
    code, out, _ = call(
        "ratchet",
        "--mode",
        "floor",
        "--stats",
        "stats.json",
        "--baseline",
        "here.json",
        "--update",
    )
    assert code == 0
    assert (sub / "here.json").is_file()
    assert not (repo / "here.json").exists()


def test_a_bad_config_exits_two_with_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.mutmut_ratchet]\nbogus = 1\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    code, out, err = call("targets", "README.md")
    assert code == 2
    assert err.startswith("ERROR: ")
    assert "unknown setting 'bogus'" in err
    assert out == ""


def test_main_defaults_to_sys_argv(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["mutmut-ratchet", "targets", "README.md"])
    assert main() == 0
    assert capsys.readouterr().out == "scoped\n\n\n"
