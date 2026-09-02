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
Both lines are empty when the shard received no modules.

Run from the repository root, e.g. for an 8-way split, the first shard::

    mutmut-ratchet shards --shard 0 --of 8

``--restrict <path>...`` narrows the output to the intersection of the shard and
the given source paths, without changing the global assignment. This lets a
*scoped* run (a PR mutating only its changed modules) reuse the same shards as a
full run: the partition is still computed over every module (so each module keeps
its stable shard), but only the in-scope modules that fall in this shard are
emitted. The union across all shards of ``shard ∩ restrict`` equals ``restrict``,
so coverage of the scoped set stays complete and disjoint. A shard whose
intersection is empty emits two blank lines (and the caller skips it).
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import IO

from .config import DEFAULT_FALLBACK_SECONDS_PER_MUTANT, Config

__all__ = [
    "load_counts",
    "load_timings",
    "mutable_modules",
    "partition",
    "patterns_for",
    "resolve_weights",
    "run",
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


def patterns_for(paths: list[str], config: Config) -> list[str]:
    """Derive mutmut dotted filter patterns from source paths.

    A normal module ``a/b.py`` matches ``<pkg>.a.b.*``.

    A package ``__init__.py`` is special: mutmut strips the ``__init__`` segment
    from its mutant names (``get_mutant_name`` does ``.replace('.__init__.', '.')``),
    so the package-root mutants live directly under the package's dotted name —
    e.g. ``<pkg>.x_async_setup_entry__mutmut_1``. A bare ``<pkg>.*`` would also
    match every *submodule*, so instead we match only the mutant trampolines,
    which mutmut names ``x_*`` (functions) and ``xǁ*`` (class methods); no module
    name starts with those, so this matches exactly the package-root mutants and
    nothing else.
    """
    patterns: list[str] = []
    for p in paths:
        dotted = config.dotted(p)
        if dotted.endswith(".__init__"):
            base = dotted[: -len(".__init__")]
            patterns += [f"{base}.x_*", f"{base}.xǁ*"]
        else:
            patterns.append(f"{dotted}.*")
    return patterns


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


def run(
    config: Config,
    shard: int,
    of: int,
    *,
    restrict: list[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Emit this shard's patterns and paths. Returns 2 on invalid shard bounds."""
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

    paths = shard_for(config, shard, of, restrict=restrict)
    print(" ".join(patterns_for(paths, config)), file=stream)
    print(" ".join(paths), file=stream)
    return 0
