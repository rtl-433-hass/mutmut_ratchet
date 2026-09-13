"""Tests for the mutation sharder (ported from both consumers).

The matrix splits a full mutmut run across N parallel jobs, each mutating a
disjoint subset of modules. The coverage-critical property is that the shards
form a *complete, non-overlapping* partition of every mutable module: a module
dropped from the union (or duplicated across shards) would silently escape the
per-file mutation floor, so these tests guard that invariant — plus determinism,
the path<->pattern derivation, and that ``--restrict`` (used to fan a scoped PR
run across the same shards) still covers exactly the in-scope set.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from consumers import ConsumerProfile
import pytest

from mutmut_ratchet.config import Config, module_dotted_for_mutants
from mutmut_ratchet.shards import (
    functions_in,
    load_counts,
    load_function_timings,
    load_timings,
    mutable_modules,
    partition,
    patterns_for,
    patterns_for_units,
    resolve_weights,
    run,
    shard_for,
    units_for_shard,
    work_units,
)


def _even_weights(modules: list[str]) -> dict[str, float]:
    return dict.fromkeys(modules, 1.0)


def test_mutable_modules_matches_the_packages_sources(
    repo: Path, profile: ConsumerProfile
) -> None:
    """The sharder enumerates modules exactly as mutmut would, so a shard set
    covers precisely what a full ``mutmut run`` would mutate."""
    assert sorted(mutable_modules()) == sorted(
        profile.source(m) for m in profile.modules
    )


def test_partition_is_complete_and_disjoint(
    repo: Path, profile: ConsumerProfile
) -> None:
    modules = mutable_modules()
    shards = partition(8, modules, _even_weights(modules))
    all_paths = [p for shard in shards for p in shard]
    assert len(all_paths) == len(set(all_paths)), "a path appears in >1 shard"
    assert set(all_paths) == set(modules)
    assert len(shards) == 8


def test_every_baseline_file_lands_in_exactly_one_shard(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    baseline = {"files": {profile.source(m): {"total": 10} for m in profile.modules}}
    config.baseline.parent.mkdir(parents=True, exist_ok=True)
    config.baseline.write_text(json.dumps(baseline), encoding="utf-8")
    shards = [shard_for(config, i, 8) for i in range(8)]
    for path in baseline["files"]:
        hits = sum(path in shard for shard in shards)
        assert hits == 1, f"{path} is in {hits} shards, expected 1"


def test_partition_is_deterministic(repo: Path, config: Config) -> None:
    assert [shard_for(config, i, 5) for i in range(5)] == [
        shard_for(config, i, 5) for i in range(5)
    ]


def test_pattern_round_trip(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    for module in profile.modules:
        if module.endswith("__init__.py"):
            continue
        path = profile.source(module)
        dotted = f"{profile.package_dotted}.{module[: -len('.py')].replace('/', '.')}"
        assert patterns_for([path], config) == [f"{dotted}.*"]


def test_package_init_patterns_target_root_mutants(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """``__init__.py`` mutants live under the package name with no module segment.

    mutmut strips the ``__init__`` segment from mutant names, so the naive
    ``<pkg>.__init__.*`` filter matches *nothing* (every package-root mutant goes
    unrun and scores 0). They must be matched via the ``x_``/``xǁ`` trampoline
    prefixes instead, which no submodule name shares.
    """
    root = profile.package_dotted
    assert patterns_for([profile.source("__init__.py")], config) == [
        f"{root}.x_*",
        f"{root}.xǁ*",
    ]
    if "coordinator/__init__.py" in profile.modules:
        # A nested package __init__ keeps its package path but still drops __init__.
        assert patterns_for([profile.source("coordinator/__init__.py")], config) == [
            f"{root}.coordinator.x_*",
            f"{root}.coordinator.xǁ*",
        ]


def test_resolve_weights_prefers_timing_then_count_fallback() -> None:
    """A module's weight is its measured time; an untimed module is estimated as
    ``count * avg_seconds_per_mutant`` derived from the timed modules — so the
    bin-packer balances by *time*, not raw mutant count (the source of the slow
    pole when one module is far slower per mutant than another)."""
    modules = ["a.py", "b.py", "c.py"]
    # a: 100s/100 mutants and b: 300s/100 mutants -> avg 2 s/mutant. c has no
    # timing, 50 mutants -> estimated 100s. (Count alone would tie a and c.)
    timings = {"a.py": 100.0, "b.py": 300.0}
    counts = {"a.py": 100, "b.py": 100, "c.py": 50}
    weights = resolve_weights(modules, timings, counts)
    assert weights["a.py"] == 100.0  # measured time used directly
    assert weights["b.py"] == 300.0
    assert weights["c.py"] == 100.0  # 50 mutants * (400s / 200 mutants) = 100s
    # b is the heaviest by time even though a and b tie on mutant count.
    assert max(weights, key=lambda k: weights[k]) == "b.py"


def test_resolve_weights_falls_back_when_no_profile_exists_at_all() -> None:
    """A fresh checkout with neither timings nor counts still gets positive,
    count-proportional weights, so the split is sane rather than degenerate."""
    weights = resolve_weights(["a.py", "b.py"], {}, {"a.py": 7})
    assert weights == {"a.py": 7.0, "b.py": 1.0}
    weights = resolve_weights(["a.py"], {}, {}, 4.0)
    assert weights == {"a.py": 4.0}


def test_lpt_places_the_heaviest_module_first_and_ties_break_by_path() -> None:
    """LPT with fixed sort/tie-break keys: heaviest first (ties by path), into the
    lightest bin (ties by lowest index). Reproducible across every matrix job."""
    modules = ["big.py", "mid.py", "a.py", "b.py"]
    weights = {"big.py": 10.0, "mid.py": 6.0, "a.py": 2.0, "b.py": 2.0}
    # big(10) opens bin 0; mid+a+b (6+2+2) then fill bin 1 to exactly 10.
    assert partition(2, modules, weights) == [["big.py"], ["a.py", "b.py", "mid.py"]]
    # An unweighted module contributes 0.0 rather than raising.
    assert partition(1, ["z.py"], {}) == [["z.py"]]


def test_load_counts_and_timings_tolerate_a_missing_profile(tmp_path: Path) -> None:
    assert load_counts(tmp_path / "nope.json") == {}
    assert load_timings(tmp_path / "nope.json") == {}
    (tmp_path / "b.json").write_text(
        json.dumps({"files": {"a.py": {"total": 4}}}), encoding="utf-8"
    )
    (tmp_path / "t.json").write_text(
        json.dumps({"files": {"a.py": 1.5}}), encoding="utf-8"
    )
    assert load_counts(tmp_path / "b.json") == {"a.py": 4}
    assert load_timings(tmp_path / "t.json") == {"a.py": 1.5}


def _shard_paths(config: Config, *argv: str) -> list[str]:
    """Run the shards command for one shard and return its source paths (line 2)."""
    out = io.StringIO()
    shard = int(argv[argv.index("--shard") + 1])
    of = int(argv[argv.index("--of") + 1])
    restrict = (
        list(argv[argv.index("--restrict") + 1 :]) if "--restrict" in argv else None
    )
    assert run(config, shard, of, restrict=restrict, stdout=out) == 0
    lines = out.getvalue().splitlines()
    return lines[1].split() if len(lines) > 1 and lines[1] else []


def test_restrict_covers_exactly_the_in_scope_set(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """Union of (shard ∩ restrict) over all shards == restrict, and is disjoint.

    This is what lets a scoped PR run fan its changed modules across the same
    4 shards without dropping or double-counting any in-scope module.
    """
    restrict = [profile.source(m) for m in profile.modules[:3]]
    collected: list[str] = []
    for shard in range(4):
        collected += _shard_paths(
            config, "--shard", str(shard), "--of", "4", "--restrict", *restrict
        )
    assert sorted(collected) == sorted(restrict)
    assert len(collected) == len(set(collected)), "an in-scope path appears in >1 shard"


def test_restrict_with_nothing_in_scope_is_empty(repo: Path, config: Config) -> None:
    """An empty --restrict (docs-only PR) yields no work in any shard."""
    for shard in range(4):
        assert (
            _shard_paths(config, "--shard", str(shard), "--of", "4", "--restrict") == []
        )


def test_shard_output_is_two_lines(repo: Path, config: Config) -> None:
    out = io.StringIO()
    assert run(config, 0, 1, stdout=out) == 0
    patterns, paths = out.getvalue().splitlines()
    assert len(patterns.split()) >= len(paths.split())
    assert sorted(paths.split()) == sorted(mutable_modules())


def test_an_empty_shard_emits_two_blank_lines(repo: Path, config: Config) -> None:
    out = io.StringIO()
    # More shards than modules guarantees at least one empty bin.
    assert run(config, 199, 200, stdout=out) == 0
    assert out.getvalue() == "\n\n"


@pytest.mark.parametrize(
    "shard, of, message",
    [
        (0, 0, "--of must be >= 1, got 0"),
        (0, -1, "--of must be >= 1, got -1"),
        (4, 4, "--shard must satisfy 0 <= shard < 4, got 4"),
        (-1, 4, "--shard must satisfy 0 <= shard < 4, got -1"),
    ],
)
def test_invalid_bounds_exit_two(
    repo: Path, config: Config, shard: int, of: int, message: str
) -> None:
    out, err = io.StringIO(), io.StringIO()
    assert run(config, shard, of, stdout=out, stderr=err) == 2
    assert err.getvalue() == f"ERROR: {message}\n"
    assert out.getvalue() == ""


def test_timings_drive_the_split_not_mutant_counts(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """One module with an enormous measured time must land alone against all the
    rest — the whole point of balancing by time rather than count."""
    heavy = profile.source(profile.modules[0])
    config.timings.parent.mkdir(parents=True, exist_ok=True)
    config.timings.write_text(
        json.dumps({"files": {heavy: 10_000.0}}), encoding="utf-8"
    )
    shards = [shard_for(config, i, 2) for i in range(2)]
    assert [heavy] in shards


# --- narrowing a shard to functions ------------------------------------------


def test_functions_in_claims_only_its_own_modules(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """``pkg.a.x_f`` belongs to ``pkg/a.py``; ``pkg.a.b.x_f`` belongs to
    ``pkg/a/b.py`` and must not be claimed by ``pkg/a.py``."""
    pkg = config.package_dotted
    flat = profile.source(profile.modules[1])
    names = [
        f"{pkg}.{profile.modules[1][: -len('.py')]}.x_mine",
        f"{pkg}.{profile.modules[1][: -len('.py')]}.xǁCǁmethod",
        f"{pkg}.somewhere.else_.x_theirs",
    ]
    found = functions_in(flat, names, config)
    assert found == sorted(["x_mine", "xǁCǁmethod"]), (
        "the module prefix is stripped, so the result feeds function_patterns_for"
    )


def test_functions_in_rejects_a_submodule_that_shares_the_prefix(
    repo: Path, config: Config
) -> None:
    pkg = config.package_dotted
    # `pkg.a.b.x_f` shares the `pkg.a.` prefix but `b.x_f` is not a mangled name.
    assert functions_in(config.source("a.py"), [f"{pkg}.a.b.x_f"], config) == []
    assert functions_in(config.source("a.py"), [f"{pkg}.a.x_f"], config) == ["x_f"]


def test_restrict_functions_narrows_the_emitted_patterns(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    module = profile.modules[1]
    source = profile.source(module)
    wanted = f"{config.package_dotted}.{module[: -len('.py')]}.x_chosen"

    out = io.StringIO()
    assert (
        run(config, 0, 1, restrict=[source], restrict_functions=[wanted], stdout=out)
        == 0
    )
    patterns, paths = out.getvalue().splitlines()
    assert patterns == f"{wanted}__mutmut_*"
    assert paths == source


def test_a_module_no_function_names_keeps_its_whole_module_pattern(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """A partially narrowed scope must not silently drop the rest of the shard."""
    a, b = (profile.source(m) for m in profile.modules[1:3])
    wanted = f"{config.package_dotted}.{profile.modules[1][: -len('.py')]}.x_chosen"

    out = io.StringIO()
    assert (
        run(config, 0, 1, restrict=[a, b], restrict_functions=[wanted], stdout=out) == 0
    )
    patterns, paths = out.getvalue().splitlines()
    assert f"{wanted}__mutmut_*" in patterns.split()
    assert patterns_for([b], config)[0] in patterns.split(), (
        "the unnamed module keeps its whole-module pattern"
    )
    assert sorted(paths.split()) == sorted([a, b])


def test_no_restrict_functions_leaves_the_old_behaviour(
    repo: Path, config: Config
) -> None:
    plain, narrowed = io.StringIO(), io.StringIO()
    assert run(config, 0, 1, stdout=plain) == 0
    assert run(config, 0, 1, restrict_functions=[], stdout=narrowed) == 0
    assert plain.getvalue() == narrowed.getvalue()


# --- sharding by function ----------------------------------------------------


def _profile(config: Config, files: dict, functions: dict | None = None) -> None:
    payload: dict = {"files": files}
    if functions is not None:
        payload["functions"] = functions
    config.timings.parent.mkdir(parents=True, exist_ok=True)
    config.timings.write_text(json.dumps(payload), encoding="utf-8")


def test_without_function_timings_the_split_is_exactly_what_it_was(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """A consumer whose profile predates per-function seconds must be unaffected."""
    modules = [profile.source(m) for m in profile.modules]
    _profile(config, dict.fromkeys(modules, 10.0))
    for shard in range(3):
        by_unit = units_for_shard(config, shard, 3)
        assert all(name is None for _, name in by_unit), "one unit per module"
        assert sorted({p for p, _ in by_unit}) == shard_for(config, shard, 3)


def test_function_timings_split_one_module_across_bins(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """The whole point: a module heavy enough to set the makespan is divisible."""
    source = profile.source(profile.modules[1])
    Path(source).write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n", encoding="utf-8"
    )
    dotted = module_dotted_for_mutants(source, config)
    _profile(
        config,
        {source: 100.0},
        {source: {f"{dotted}.x_alpha": 50.0, f"{dotted}.x_beta": 50.0}},
    )
    modules = [source]
    left = units_for_shard(config, 0, 2, modules=modules)
    right = units_for_shard(config, 1, 2, modules=modules)
    assert {u[1] for u in left} == {"x_alpha"}
    assert {u[1] for u in right} == {"x_beta"}
    # Each bin still names the file, because `stats --paths` needs it.
    assert [p for p, _ in left] == [source]
    assert patterns_for_units(left, config) == [f"{dotted}.x_alpha__mutmut_*"]


def test_a_function_the_profile_has_not_seen_still_gets_a_bin(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """The trap: taking the profile's word for a file's contents would leave a
    newly added function in no bin at all -- mutated by nobody, reported by
    nobody. The function list comes from the source for exactly this reason."""
    source = profile.source(profile.modules[1])
    Path(source).write_text(
        "def old():\n    return 1\n\n\ndef brand_new():\n    return 2\n",
        encoding="utf-8",
    )
    dotted = module_dotted_for_mutants(source, config)
    _profile(config, {source: 100.0}, {source: {f"{dotted}.x_old": 60.0}})

    placed = {
        u[1]
        for shard in range(3)
        for u in units_for_shard(config, shard, 3, modules=[source])
    }
    assert placed == {"x_old", "x_brand_new"}


def test_every_unit_lands_in_exactly_one_bin(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """Disjoint and complete, the property the whole matrix rests on."""
    modules = [profile.source(m) for m in profile.modules]
    _profile(
        config,
        dict.fromkeys(modules, 30.0),
        {
            modules[1]: {
                f"{module_dotted_for_mutants(modules[1], config)}.x_a": 10.0,
                f"{module_dotted_for_mutants(modules[1], config)}.x_b": 20.0,
            }
        },
    )
    seen: list = []
    for shard in range(4):
        seen += units_for_shard(config, shard, 4, modules=modules)
    assert len(seen) == len(set(seen)), "a unit appears in two bins"
    every = set(
        work_units(
            modules,
            dict.fromkeys(modules, 30.0),
            load_function_timings(config.timings),
            config,
        )
    )
    assert set(seen) == every


def test_restrict_functions_narrows_a_whole_module_unit(
    repo: Path, config: Config, profile: ConsumerProfile
) -> None:
    """Filtering it out instead would run nothing at all for that module, which
    is the one outcome a scoping bug must never produce."""
    source = profile.source(profile.modules[1])
    Path(source).write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n", encoding="utf-8"
    )
    _profile(config, {source: 100.0})  # no functions block -> one whole unit
    dotted = module_dotted_for_mutants(source, config)

    found = []
    for shard in range(2):
        found += units_for_shard(
            config,
            shard,
            2,
            restrict=[source],
            restrict_functions=[f"{dotted}.x_alpha"],
            modules=[source],
        )
    assert found, "the module must still run something"
    assert {u[1] for u in found} == {"x_alpha"}
