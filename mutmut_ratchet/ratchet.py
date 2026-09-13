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

Three modes:

* ``floor`` (the CI gate): fail if any file's score is below its tolerance band.
  Improvements never fail.
* ``strict`` (local check that the committed baseline is still representative):
  fail if any file drifts beyond the band in either direction.
* ``functions`` (the gate for a *function*-scoped PR run): fail if a changed
  function gained surviving mutants, or if a function new to the baseline does
  not reach ``floor``. A per-file score is a partial measurement when only some
  of a file's functions were mutated, so it cannot be compared against a
  whole-file baseline; each mutated function's score can.

Usage::

    mutmut run
    mutmut-ratchet stats > stats.json
    mutmut-ratchet ratchet --mode floor     --stats stats.json
    mutmut-ratchet ratchet --mode strict    --stats stats.json
    mutmut-ratchet ratchet --mode functions --stats stats.json
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
    "check_functions",
    "check_strict",
    "run",
    "score_for",
    "scores_from_function_stats",
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


def _measured(stats: dict[str, Any]) -> bool:
    """Whether a *function's* tally is of mutants the run actually executed.

    Everything mutmut generated is in its meta file whether the run's filter
    selected it or not; what it skipped is recorded "not checked". A tally that
    is *entirely* not-checked is therefore not a bad score, it is no score --
    and reading it as one is how a scoped run would fail on functions it never
    touched.

    Wholly, because mutmut's filter selects whole functions: a function is
    either in the run or out of it, never half. Files are not like that, which
    is what :func:`_fully_measured` is for.
    """
    return int(stats.get("not_checked", 0)) < int(stats.get("total", 0))


def _fully_measured(stats: dict[str, Any]) -> bool:
    """Whether a *file's* tally is complete enough to record as a baseline.

    Unlike a function, a file can be measured in part -- a scoped run mutates
    some of its functions and skips the rest -- and the resulting score is lower
    than the file deserves by exactly the mutants that never ran. Any
    "not checked" at all therefore disqualifies it from becoming the bar.
    """
    return not int(stats.get("not_checked", 0))


def scores_from_stats(stats: dict[str, Any], precision: int) -> dict[str, FileScore]:
    """Reduce a mutmut stats payload to per-file {killed,total,score}."""
    out: dict[str, FileScore] = {}
    for path, fstats in stats.get("files", {}).items():
        killed, total, score = score_for(fstats, precision)
        out[path] = {"killed": killed, "total": total, "score": score}
    return dict(sorted(out.items()))


def scores_from_function_stats(
    stats: dict[str, Any], precision: int
) -> dict[str, dict[str, FileScore]]:
    """Reduce a stats payload's per-function block to {path: {function: score}}."""
    out: dict[str, dict[str, FileScore]] = {}
    for path, functions in stats.get("functions", {}).items():
        per_function: dict[str, FileScore] = {}
        for name, fstats in functions.items():
            # A function the run's filter skipped has no score to compare; see
            # :func:`_measured`. This is what makes a scoped run gateable
            # without an out-of-band list of what it covered.
            if not _measured(fstats):
                continue
            killed, total, score = score_for(fstats, precision)
            per_function[name] = {
                "killed": killed,
                "total": total,
                "survived": total - killed,
                "score": score,
            }
        if per_function:
            out[path] = dict(sorted(per_function.items()))
    return dict(sorted(out.items()))


def check_functions(
    current: dict[str, dict[str, FileScore]],
    baseline: dict[str, Any],
    floor: float,
    tolerance_survivors: int,
    *,
    stdout: IO[str] | None = None,
) -> list[str]:
    """Fail when a changed function gains survivors, or a new one misses the floor.

    This is the gate for a *function*-scoped run, where a per-file score is a
    partial measurement and cannot be compared against a whole-file baseline.

    Two rules, because a function in the baseline and a function that has never
    been measured are different questions:

    * **Known function**: fail when it has more surviving mutants than the
      baseline recorded, beyond ``tolerance_survivors``. Counting survivors
      rather than comparing scores is what makes this "no *new* survivors": a
      function that grows by ten well-tested lines keeps its survivor count and
      passes, where a score comparison could drift either way on the changed
      denominator. It also means a function that already had survivors does not
      block the PR that touches it -- only *additional* ones do.
    * **New function**: nothing to compare against, so it must reach ``floor``.
      Passing it silently would leave the one case a mutation gate exists for --
      new code with no tests -- completely unguarded.
    """
    stream = sys.stdout if stdout is None else stdout
    failures: list[str] = []
    base_functions = baseline.get("functions", {})
    for path, functions in current.items():
        known = base_functions.get(path, {})
        for name, cur in functions.items():
            base = known.get(name)
            if base is None:
                if cur["score"] < floor:
                    failures.append(
                        f"  UNDER FLOOR {path}::{name}: {cur['score']:.3f} < "
                        f"floor {floor:.3f} (killed {cur['killed']}/{cur['total']}) "
                        "-- a new function needs tests"
                    )
                else:
                    print(
                        f"  + new function (not yet in baseline): {path}::{name} "
                        f"score={cur['score']:.3f}",
                        file=stream,
                    )
                continue
            allowed = base.get("survived", 0) + tolerance_survivors
            if cur["survived"] > allowed:
                failures.append(
                    f"  NEW SURVIVORS {path}::{name}: {cur['survived']} survived, "
                    f"baseline {base.get('survived', 0)} (+{tolerance_survivors} "
                    f"allowed) (killed {cur['killed']}/{cur['total']})"
                )
    return failures


def tolerance_score(total: int, fraction: float, mutants: int, precision: int) -> float:
    """Tolerance band, in score space, for a file with ``total`` scoreable mutants."""
    if total <= 0:
        return 0.0
    band_mutants = max(fraction * total, mutants)
    return round(band_mutants / total, precision)


def _ratchet_up(
    base: dict[str, FileScore], current: dict[str, FileScore]
) -> dict[str, FileScore]:
    """Keep the higher-scoring of baseline/current for each key, sorted.

    The bar only ever goes up; ``base`` is never mutated.
    """
    merged = dict(base)
    for key, score in current.items():
        previous = merged.get(key)
        if previous is None or score["score"] >= previous["score"]:
            merged[key] = score
    return dict(sorted(merged.items()))


def load_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def write_baseline(
    path: Path,
    scores: dict[str, FileScore],
    fraction: float,
    mutants: int,
    floor: float,
    functions: dict[str, dict[str, FileScore]] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "floor": floor,
        "tolerance_fraction": fraction,
        "tolerance_mutants": mutants,
        "files": scores,
    }
    # Only written when a run produced per-function data, so a consumer that
    # never uses the function gate keeps a baseline of exactly the old shape.
    if functions:
        payload["functions"] = functions
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
    tolerance_survivors: int | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run the ratchet. 0 = pass, 1 = regression/drift, 2 = missing input."""
    stream = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr

    if not stats_path.exists():
        print(f"ERROR: stats file not found: {stats_path}", file=errors)
        return 2

    payload = load_json(stats_path)
    current = scores_from_stats(payload, config.precision)
    current_functions = scores_from_function_stats(payload, config.precision)

    if mode == "functions" and not current_functions:
        print(
            "ERROR: --mode functions needs the per-function block from "
            f"`mutmut-ratchet stats`, which {stats_path} does not have",
            file=errors,
        )
        return 2

    # A file whose mutants were only partly executed is a *partial* measurement:
    # everything the filter skipped is in its tally, recorded "not checked",
    # which reads as survived. It must never reach the baseline -- a file not yet
    # recorded would take that artificially low score as the bar every later
    # floor run is held to. Keyed off the data rather than the mode, so a file
    # that ran whole in a scoped shard still records.
    recordable_files: dict[str, FileScore] = {
        path: score
        for path, score in current.items()
        if _fully_measured(payload.get("files", {}).get(path, {}))
    }

    baseline = load_json(config.baseline) if config.baseline.exists() else None

    def setting(flag: Any, key: str, default: Any) -> Any:
        if flag is not None:
            return flag
        return (baseline or {}).get(key, default)

    fraction = setting(
        tolerance_fraction, "tolerance_fraction", config.tolerance_fraction
    )
    mutants = setting(tolerance_mutants, "tolerance_mutants", config.tolerance_mutants)
    survivors = setting(
        tolerance_survivors, "tolerance_survivors", config.tolerance_survivors
    )

    if baseline is None:
        if update:
            write_baseline(
                config.baseline,
                recordable_files,
                fraction,
                mutants,
                config.floor,
                current_functions,
            )
            print(
                f"Created baseline {config.baseline} with "
                f"{len(recordable_files)} files.",
                file=stream,
            )
            return 0
        print(
            f"ERROR: baseline not found: {config.baseline} "
            "(run with --update to create it)",
            file=errors,
        )
        return 2

    # A baseline predating the function gate has no per-function block, which
    # would make every function "new" and gate the whole package against the
    # floor at once. That is a bootstrap, not a regression: record the block on
    # this run and gate against it from the next one. Only with --update, so a
    # plain gate run never silently passes on a missing baseline.
    bootstrapping = mode == "functions" and "functions" not in baseline

    if mode == "floor":
        failures = check_floor(
            current, baseline, fraction, mutants, config.precision, stdout=stream
        )
    elif mode == "functions":
        if bootstrapping and update:
            print(
                f"Baseline {config.baseline} has no per-function block yet; "
                f"recording {sum(len(f) for f in current_functions.values())} "
                "function(s) without gating.",
                file=stream,
            )
            failures = []
        else:
            failures = check_functions(
                current_functions, baseline, config.floor, survivors, stdout=stream
            )
    else:
        failures = check_strict(current, baseline, fraction, mutants, config.precision)

    # Tally whatever this mode actually gated on. In functions mode that is the
    # functions: the file block still carries every mutant the filter skipped, so
    # a per-file total would report a near-zero score for a run that passed.
    if mode == "functions":
        tallied = [s for fns in current_functions.values() for s in fns.values()]
        scope = (
            f"{len(tallied)} functions in {len(current_functions)} files "
            f"(mode={mode}, +{survivors} survivor(s) allowed, "
            f"floor={config.floor:.2f})"
        )
    else:
        tallied = list(current.values())
        scope = (
            f"{len(current)} files (mode={mode}, band=max({fraction:.2f}xN, {mutants})"
        )
    overall = sum(s["killed"] for s in tallied)
    overall_total = sum(s["total"] for s in tallied)
    pct = (overall / overall_total * 100) if overall_total else 100.0
    print(
        f"Mutation score: {overall}/{overall_total} = {pct:.1f}% across {scope}.",
        file=stream,
    )

    if failures:
        print(f"\n{len(failures)} ratchet failure(s):", file=stream)
        print("\n".join(failures), file=stream)
        if update and mode in ("floor", "functions"):
            print("\nRefusing to update baseline while regressions exist.", file=stream)
        return 1

    if update:
        merged = _ratchet_up(baseline.get("files", {}), recordable_files)
        # The same rule per function, keyed within each file so a scoped run only
        # ever replaces the functions it actually measured.
        merged_functions = {
            path: dict(fns) for path, fns in baseline.get("functions", {}).items()
        }
        for path, functions in current_functions.items():
            merged_functions[path] = _ratchet_up(
                merged_functions.get(path, {}), functions
            )
        write_baseline(
            config.baseline,
            merged,
            fraction,
            mutants,
            config.floor,
            dict(sorted(merged_functions.items())),
        )
        print(f"Baseline updated: {config.baseline}", file=stream)

    print("OK: no mutation-score regression beyond tolerance.", file=stream)
    return 0
