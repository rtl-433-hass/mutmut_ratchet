"""Mutation-score ratchet: compare a run against a committed per-file baseline.

Compares the current mutmut results (per-file stats from the ``stats``
subcommand) against a committed per-file baseline and enforces that mutation
coverage never meaningfully regresses.

A per-file comparison needs a tolerance band for two reasons:

1. **Run-to-run variance.** mutmut's score is not perfectly reproducible — async
   and time-sensitive paths flip a mutant or two between runs and between
   machines (locally vs CI).
2. **Scoped vs full divergence.** On pull requests CI mutates only the changed
   modules (``mutmut run "<module>.*"``), which is a slight *lower bound* on a
   file's full-suite score: a few mutants are killed only by tests in other files
   that a scoped run doesn't exercise. Observed example: ``number.py`` scores
   27/29 scoped but 29/29 in the full baseline — a 2-mutant (≈7%) gap on a small
   file.

Both effects are measured in **mutants**, not percentage points, so a flat
percentage tolerance is wrong: 2% is ~13 mutants on a 630-mutant coordinator but
0 mutants on a 29-mutant file. The band is therefore
``max(fraction × total, absolute_mutants)`` converted back to score space — an
absolute-mutant cushion that protects small files, plus a fraction that scales for
large ones. A real regression (deleting a test typically kills many more mutants
than the band) still fails; a sub-band drop on a small file passes the PR gate and
is re-measured authoritatively by the nightly full run.

The bar only ratchets **upward** — genuine improvements are captured with
``--update``. Equivalent/unkillable mutants are recorded in the baseline rather
than suppressed: nothing is ignored, the score just cannot fall.

Two modes:

* ``floor`` (the CI gate): fail if any file's score is below its tolerance band.
  Improvements never fail.
* ``strict`` (local check that the committed baseline is still representative):
  fail if any file drifts beyond the band in either direction.

Usage::

    mutmut run
    mutmut-ratchet stats > stats.json
    mutmut-ratchet ratchet --mode floor  --stats stats.json
    mutmut-ratchet ratchet --mode strict --stats stats.json
    mutmut-ratchet ratchet --mode floor  --stats stats.json --update
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import IO, Any

from .config import Config

__all__ = [
    "check_floor",
    "check_strict",
    "run",
    "score_for",
    "scores_from_stats",
    "tolerance_score",
    "write_baseline",
]

FileScore = dict[str, Any]


def score_for(file_stats: dict[str, Any], precision: int) -> tuple[int, int, float]:
    """Return (killed, scoreable_total, score) for one file's mutmut stats."""
    killed = file_stats.get("killed", 0) + file_stats.get("timeout", 0)
    total = file_stats.get("total", 0) - file_stats.get("skipped", 0)
    score = 1.0 if total <= 0 else killed / total
    return killed, total, round(score, precision)


def scores_from_stats(stats: dict[str, Any], precision: int) -> dict[str, FileScore]:
    """Reduce a mutmut stats payload to per-file {killed,total,score}."""
    out: dict[str, FileScore] = {}
    for path, fstats in stats.get("files", {}).items():
        killed, total, score = score_for(fstats, precision)
        out[path] = {"killed": killed, "total": total, "score": score}
    return dict(sorted(out.items()))


def tolerance_score(total: int, fraction: float, mutants: int, precision: int) -> float:
    """Tolerance band, in score space, for a file with ``total`` scoreable mutants."""
    if total <= 0:
        return 0.0
    band_mutants = max(fraction * total, mutants)
    return round(band_mutants / total, precision)


def load_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def write_baseline(
    path: Path,
    scores: dict[str, FileScore],
    fraction: float,
    mutants: int,
    floor: float,
) -> None:
    payload = {
        "floor": floor,
        "tolerance_fraction": fraction,
        "tolerance_mutants": mutants,
        "files": scores,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def check_floor(
    current: dict[str, FileScore],
    baseline: dict[str, Any],
    fraction: float,
    mutants: int,
    precision: int,
    *,
    stdout: IO[str] | None = None,
) -> list[str]:
    """Fail when a known file's score drops below baseline beyond its tolerance band."""
    stream = sys.stdout if stdout is None else stdout
    failures: list[str] = []
    base_files = baseline.get("files", {})
    for path, cur in current.items():
        base = base_files.get(path)
        if base is None:
            print(
                f"  + new file (not yet in baseline): {path} score={cur['score']:.3f}",
                file=stream,
            )
            continue
        band = tolerance_score(cur["total"], fraction, mutants, precision)
        if cur["score"] < base["score"] - band:
            failures.append(
                f"  REGRESSION {path}: {cur['score']:.3f} < baseline {base['score']:.3f} "
                f"- band {band:.3f} (killed {cur['killed']}/{cur['total']})"
            )
    return failures


def check_strict(
    current: dict[str, FileScore],
    baseline: dict[str, Any],
    fraction: float,
    mutants: int,
    precision: int,
) -> list[str]:
    """Fail when current results drift from baseline beyond the tolerance band."""
    failures: list[str] = []
    base_files = baseline.get("files", {})
    cur_paths, base_paths = set(current), set(base_files)
    for path in sorted(base_paths - cur_paths):
        failures.append(f"  MISSING in current results (in baseline): {path}")
    for path in sorted(cur_paths - base_paths):
        failures.append(
            f"  UNRECORDED file (not in baseline): {path} score={current[path]['score']:.3f}"
        )
    for path in sorted(cur_paths & base_paths):
        cur, base = current[path], base_files[path]
        band = tolerance_score(cur["total"], fraction, mutants, precision)
        if abs(cur["score"] - base["score"]) > band:
            direction = "improved" if cur["score"] > base["score"] else "regressed"
            failures.append(
                f"  DRIFT {path}: {direction} {base['score']:.3f} -> {cur['score']:.3f} "
                f"(> band {band:.3f}; refresh the committed baseline)"
            )
    return failures


def run(
    config: Config,
    mode: str,
    stats_path: Path,
    *,
    update: bool = False,
    tolerance_fraction: float | None = None,
    tolerance_mutants: int | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run the ratchet. 0 = pass, 1 = regression/drift, 2 = missing input."""
    stream = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr

    if not stats_path.exists():
        print(f"ERROR: stats file not found: {stats_path}", file=errors)
        return 2

    current = scores_from_stats(load_json(stats_path), config.precision)

    baseline = load_json(config.baseline) if config.baseline.exists() else None

    def setting(flag: Any, key: str, default: Any) -> Any:
        if flag is not None:
            return flag
        return (baseline or {}).get(key, default)

    fraction = setting(
        tolerance_fraction, "tolerance_fraction", config.tolerance_fraction
    )
    mutants = setting(tolerance_mutants, "tolerance_mutants", config.tolerance_mutants)

    if baseline is None:
        if update:
            write_baseline(config.baseline, current, fraction, mutants, config.floor)
            print(
                f"Created baseline {config.baseline} with {len(current)} files.",
                file=stream,
            )
            return 0
        print(
            f"ERROR: baseline not found: {config.baseline} "
            "(run with --update to create it)",
            file=errors,
        )
        return 2

    failures = (
        check_floor(
            current, baseline, fraction, mutants, config.precision, stdout=stream
        )
        if mode == "floor"
        else check_strict(current, baseline, fraction, mutants, config.precision)
    )

    overall = sum(c["killed"] for c in current.values())
    overall_total = sum(c["total"] for c in current.values())
    pct = (overall / overall_total * 100) if overall_total else 100.0
    print(
        f"Mutation score: {overall}/{overall_total} = {pct:.1f}% across "
        f"{len(current)} files (mode={mode}, band=max({fraction:.2f}xN, {mutants}).",
        file=stream,
    )

    if failures:
        print(f"\n{len(failures)} ratchet failure(s):", file=stream)
        print("\n".join(failures), file=stream)
        if update and mode == "floor":
            print("\nRefusing to update baseline while regressions exist.", file=stream)
        return 1

    if update:
        # Ratchet upward: keep the higher of baseline/current per file.
        merged: dict[str, FileScore] = {**baseline.get("files", {})}
        for path, cur in current.items():
            base = merged.get(path)
            merged[path] = (
                cur if base is None or cur["score"] >= base["score"] else base
            )
        write_baseline(
            config.baseline,
            dict(sorted(merged.items())),
            fraction,
            mutants,
            config.floor,
        )
        print(f"Baseline updated: {config.baseline}", file=stream)

    print("OK: no mutation-score regression beyond tolerance.", file=stream)
    return 0
