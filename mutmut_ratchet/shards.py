"""Deterministically partition the package's modules into N balanced shards.

A full-package mutmut run is slow. The matrix workflow splits that work across N
parallel jobs, each mutating a disjoint subset of *modules* (a module is the
smallest unit mutmut can filter to without losing fidelity to a full run).

For the split to be useful the shards must be *balanced* (so the slowest job,
which the gate waits on, is as short as possible) and *deterministic* (so every
matrix job computes the identical assignment from the same inputs, with no
coordination).

**Balance by time, not mutant count.** Per-mutant test time varies widely across
modules (one module can be several times slower per mutant than another), so a
count-balanced split still leaves a slow pole. Each module is therefore weighted
by its measured mutmut run time from the committed timings profile (see the
``timings`` subcommand). A module absent from that profile (a newly added file,
or a stale profile) falls back to ``mutant_count * avg_seconds_per_mutant`` so it
is still placed sensibly. Modules are sorted heaviest first (ties by path), then
each is placed into the currently-lightest bin (ties by lowest index) — the
classic LPT heuristic: within 4/3 of optimal makespan and, with fixed sort/tie-
break keys, fully reproducible (no randomness, no wall-clock).

**Output contract (two lines):**
    line 1: space-separated mutmut filter patterns for the requested shard
    line 2: space-separated source paths for the requested shard
Both lines are empty when the shard received no work.

Line 2 names every file the shard touches, whether it holds all of that file or
only some of its functions, because that is what ``stats --paths`` needs. Which
functions a run actually measured is read back out of the stats payload rather
than carried forward from here.

Run from the repository root, e.g. for an 8-way split, the first shard::

    mutmut-ratchet shards --shard 0 --of 8

``--restrict <path>...`` narrows the output to the intersection of the shard and
the given source paths, without changing the global assignment. This lets a
*scoped* run (a PR mutating only its changed modules) reuse the same shards as a
full run: the partition is still computed over every module (so each module keeps
its stable shard), but only the in-scope modules that fall in this shard are
emitted. The union across all shards of ``shard ∩ restrict`` equals ``restrict``,
so coverage of the scoped set stays complete and disjoint. A shard whose
intersection is empty emits three blank lines (and the caller skips it).
"""

from __future__ import annotations

from collections.abc import Iterable
import json
from pathlib import Path
import sys
from typing import IO

from .config import (
    DEFAULT_FALLBACK_SECONDS_PER_MUTANT,
    Config,
    function_patterns_for,
    module_dotted_for_mutants,
    patterns_for,
)
from .functions import MANGLED_PREFIXES, mutable_functions

__all__ = [
    "Unit",
    "functions_in",
    "load_function_timings",
    "load_counts",
    "load_timings",
    "mutable_modules",
    "partition",
    "partition_units",
    "patterns_for_units",
    "patterns_for",
    "resolve_weights",
    "run",
    "units_for_shard",
    "work_units",
]


def mutable_modules() -> list[str]:
    """Enumerate mutable source modules exactly as mutmut would, as repo paths."""
    from mutmut.__main__ import walk_mutatable_files

    return [str(path) for path in walk_mutatable_files()]


def load_counts(baseline: Path) -> dict[str, int]:
    """Map module path -> baseline mutant ``total`` (for the count-based fallback)."""
    if not baseline.exists():
        return {}
    data = json.loads(baseline.read_text(encoding="utf-8"))
    return {
        path: int(stats.get("total", 0))
        for path, stats in data.get("files", {}).items()
    }


def load_timings(timings: Path) -> dict[str, float]:
    """Map module path -> measured mutmut seconds (the bin-pack weight)."""
    if not timings.exists():
        return {}
    data = json.loads(timings.read_text(encoding="utf-8"))
    return {path: float(secs) for path, secs in data.get("files", {}).items()}


def resolve_weights(
    modules: list[str],
    timings: dict[str, float],
    counts: dict[str, int],
    fallback_seconds_per_mutant: float = DEFAULT_FALLBACK_SECONDS_PER_MUTANT,
) -> dict[str, float]:
    """Per-module bin-pack weight in seconds: measured time, else a count estimate.

    A module with a measured timing uses it directly. A module without one (new
    file, or stale profile) is estimated as ``mutant_count * avg_seconds_per_
    mutant``, where the average is derived from the modules that *do* have both a
    timing and a count, so the estimate is in the same units as the real weights.
    """
    paired = [(timings[m], counts[m]) for m in timings if counts.get(m)]
    total_secs = sum(t for t, _ in paired)
    total_mutants = sum(c for _, c in paired)
    per_mutant = (
        total_secs / total_mutants if total_mutants else fallback_seconds_per_mutant
    )

    def weight(module: str) -> float:
        if module in timings:
            return timings[module]
        # Estimate from mutant count (>=1 so a module is never weightless).
        return max(counts.get(module, 1), 1) * per_mutant

    return {module: weight(module) for module in modules}


def partition(n: int, modules: list[str], weights: dict[str, float]) -> list[list[str]]:
    """Partition modules into ``n`` balanced bins via deterministic LPT greedy.

    Returns a list of ``n`` lists of source paths (each inner list sorted). The
    partition is a pure function of (modules, weights, n): every module appears
    in exactly one bin and the union equals the full module set.
    """

    def weight(path: str) -> float:
        return weights.get(path, 0.0)

    # Heaviest first; stable tie-break by path so the order is reproducible.
    order = sorted(modules, key=lambda p: (-weight(p), p))
    bins: list[list[str]] = [[] for _ in range(n)]
    loads = [0.0] * n
    for path in order:
        # Place in the currently-lightest bin; lowest index wins on a tie.
        j = min(range(n), key=lambda k: (loads[k], k))
        bins[j].append(path)
        loads[j] += weight(path)
    return [sorted(b) for b in bins]


def shard_for(
    config: Config,
    shard: int,
    of: int,
    *,
    restrict: list[str] | None = None,
    modules: list[str] | None = None,
) -> list[str]:
    """Source paths assigned to ``shard`` of ``of``, optionally intersected."""
    all_modules = mutable_modules() if modules is None else modules
    weights = resolve_weights(
        all_modules,
        load_timings(config.timings),
        load_counts(config.baseline),
        config.fallback_seconds_per_mutant,
    )
    paths = partition(of, all_modules, weights)[shard]
    if restrict is not None:
        wanted = {str(Path(p)) for p in restrict}
        paths = [p for p in paths if p in wanted]
    return paths


def functions_in(path: str, functions: Iterable[str], config: Config) -> list[str]:
    """The *mangled* names among ``functions`` that belong to ``path``.

    ``functions`` are fully qualified; the module prefix is stripped so the
    result can go straight to :func:`function_patterns_for`, which is the one
    place that knows how a mutant pattern is spelled.

    A name belongs to a module when it is that module's dotted prefix followed by
    a mangled function name. Testing for the mangled prefix on what remains is
    what separates a function from a submodule: ``pkg.a.x_f`` is a function of
    ``pkg/a.py``, while ``pkg.a.b.x_f`` is a function of ``pkg/a/b.py`` and must
    not be claimed by ``pkg/a.py``.
    """
    prefix = f"{module_dotted_for_mutants(path, config)}."
    found = [
        name[len(prefix) :]
        for name in functions
        if name.startswith(prefix) and name[len(prefix) :].startswith(MANGLED_PREFIXES)
    ]
    return sorted(found)


#: One unit of shardable work: a function, or a whole module when no per-function
#: weight is available for it. ``(path, mangled name | None)``.
Unit = tuple[str, str | None]


def load_function_timings(timings: Path) -> dict[str, dict[str, float]]:
    """Map module path -> {mangled function: measured seconds}, when profiled."""
    if not timings.exists():
        return {}
    data = json.loads(timings.read_text(encoding="utf-8"))
    return {
        path: {name: float(secs) for name, secs in functions.items()}
        for path, functions in data.get("functions", {}).items()
    }


def work_units(
    modules: list[str],
    module_weights: dict[str, float],
    function_timings: dict[str, dict[str, float]],
    config: Config,
) -> dict[Unit, float]:
    """Weighted units to bin-pack: per function where measured, else per module.

    A module is only as divisible as the profile is detailed. With per-function
    seconds it contributes one unit per function, so a single oversized module no
    longer sets the makespan on its own; without them it contributes one unit and
    the split is exactly what it always was.

    The function list comes from the *source*, not from the profile, and that
    matters: a function added since the profile was written has no recorded
    weight, and taking the profile's word for the file's contents would leave it
    in no bin at all -- mutated by nobody, reported by nobody. Instead the
    unprofiled functions share whatever the module's measured total does not
    already account for, which is both a sane estimate and, more importantly,
    a bin.
    """
    units: dict[Unit, float] = {}
    for path in modules:
        whole = module_weights.get(path, 0.0)
        profiled = function_timings.get(path)
        if not profiled:
            units[(path, None)] = whole
            continue
        try:
            source = Path(path).read_text(encoding="utf-8")
        except OSError:
            units[(path, None)] = whole
            continue
        names = mutable_functions(source)
        if not names:
            # Unreadable, unparseable, or genuinely function-free: keep it whole
            # rather than emit a filter that covers only part of it.
            units[(path, None)] = whole
            continue
        # The profile keys functions the way mutmut names mutants -- fully
        # qualified -- while a unit holds the bare mangled name that
        # `function_patterns_for` wants. Bridge the two here rather than let a
        # silent miss weight every function at zero.
        prefix = f"{module_dotted_for_mutants(path, config)}."
        by_bare = {
            name[len(prefix) :]: secs
            for name, secs in profiled.items()
            if name.startswith(prefix)
        }
        unprofiled = [name for name in names if name not in by_bare]
        spare = max(whole - sum(by_bare.values()), 0.0)
        each = spare / len(unprofiled) if unprofiled else 0.0
        for name in names:
            units[(path, name)] = by_bare.get(name, each)
    return units


def partition_units(n: int, units: dict[Unit, float]) -> list[list[Unit]]:
    """Bin-pack units the same deterministic LPT way :func:`partition` packs modules."""
    order = sorted(units, key=lambda u: (-units[u], u[0], u[1] or ""))
    bins: list[list[Unit]] = [[] for _ in range(n)]
    loads = [0.0] * n
    for unit in order:
        j = min(range(n), key=lambda k: (loads[k], k))
        bins[j].append(unit)
        loads[j] += units[unit]
    return [sorted(b, key=lambda u: (u[0], u[1] or "")) for b in bins]


def units_for_shard(
    config: Config,
    shard: int,
    of: int,
    *,
    restrict: list[str] | None = None,
    restrict_functions: list[str] | None = None,
    modules: list[str] | None = None,
) -> list[Unit]:
    """The units assigned to ``shard``, after any scoping.

    The partition is computed over every unit first, so a unit keeps its shard
    whatever the scope -- the property that lets a scoped run reuse the same
    matrix as a full one.
    """
    all_modules = mutable_modules() if modules is None else modules
    module_weights = resolve_weights(
        all_modules,
        load_timings(config.timings),
        load_counts(config.baseline),
        config.fallback_seconds_per_mutant,
    )
    units = work_units(
        all_modules, module_weights, load_function_timings(config.timings), config
    )
    mine = partition_units(of, units)[shard]

    if restrict is not None:
        wanted = {str(Path(p)) for p in restrict}
        mine = [u for u in mine if u[0] in wanted]

    if restrict_functions:
        named: dict[str, set[str]] = {}
        for path in {u[0] for u in mine}:
            found = set(functions_in(path, restrict_functions, config))
            if found:
                named[path] = found
        narrowed: list[Unit] = []
        for path, name in mine:
            if path not in named:
                # No name mentions this module, so it stays exactly as it was --
                # which is what keeps a partly narrowed scope from dropping the
                # rest of the shard.
                narrowed.append((path, name))
            elif name is None:
                # A whole-module unit this shard owns, narrowed by the caller.
                # Filtering it out instead would run *nothing* for the module,
                # which is the one outcome a scoping bug must never produce.
                narrowed.extend((path, n) for n in sorted(named[path]))
            elif name in named[path]:
                # Per-function units: the ones this shard does not own belong to
                # another shard, so dropping them here is correct.
                narrowed.append((path, name))
        mine = narrowed
    return mine


def patterns_for_units(units: list[Unit], config: Config) -> list[str]:
    """The mutmut filter patterns selecting exactly ``units``."""
    patterns: list[str] = []
    by_path: dict[str, list[str]] = {}
    for path, name in units:
        if name is None:
            patterns.extend(patterns_for([path], config))
        else:
            by_path.setdefault(path, []).append(name)
    for path, names in sorted(by_path.items()):
        patterns.extend(function_patterns_for(path, names, config))
    return patterns


def run(
    config: Config,
    shard: int,
    of: int,
    *,
    restrict: list[str] | None = None,
    restrict_functions: list[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Emit this shard's patterns and paths.

    Returns 2 on invalid shard bounds. ``restrict_functions`` narrows the emitted
    patterns from whole modules to those functions, for a function-scoped run;
    a module in this shard that none of them name keeps its whole-module pattern,
    so a partially narrowed scope stays correct rather than silently dropping it.
    """
    stream = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr

    if of < 1:
        print(f"ERROR: --of must be >= 1, got {of}", file=errors)
        return 2
    if not (0 <= shard < of):
        print(
            f"ERROR: --shard must satisfy 0 <= shard < {of}, got {shard}",
            file=errors,
        )
        return 2

    units = units_for_shard(
        config,
        shard,
        of,
        restrict=restrict,
        restrict_functions=restrict_functions,
    )
    paths = sorted({path for path, _ in units})
    print(" ".join(patterns_for_units(units, config)), file=stream)
    print(" ".join(paths), file=stream)
    return 0
