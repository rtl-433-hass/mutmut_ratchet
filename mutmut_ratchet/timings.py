"""Export per-file mutmut runtime so the sharder can balance by *time*, not count.

The mutation matrix shards the package across parallel jobs. Balancing by mutant
*count* is wrong because per-mutant test time varies widely (one module can be
several times slower per mutant than another), so a count-balanced shard set
still has a slow pole. This command records each file's measured mutmut run time
— the sum of its mutants' actual test durations (``durations_by_key`` in mutmut's
per-file meta) — which the sharder then uses as the bin-pack weight.

Like the baseline, the resulting timings JSON is a committed profile refreshed
periodically: run a FULL ``mutmut run`` (so every file's mutants execute and get
timed), then::

    mutmut run
    mutmut-ratchet timings          # writes scripts/mutation_timings.json

Timing drifts run-to-run and machine-to-machine, but the sharder only needs the
*relative* ordering to be roughly right, so a stale profile degrades gracefully to
a slightly suboptimal (never incorrect) split. A file absent from the profile
falls back to a count-based estimate in the sharder.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import IO

__all__ = ["collect_timings", "run"]


def collect_timings() -> dict[str, float]:
    """Sum each mutable file's actual per-mutant test durations, in seconds."""
    from mutmut.__main__ import walk_mutatable_files
    from mutmut.mutation.data import SourceFileMutationData

    timings: dict[str, float] = {}
    for path in walk_mutatable_files():
        meta = Path("mutants") / (str(path) + ".meta")
        if not meta.exists():
            continue
        data = SourceFileMutationData(path=path)
        data.load()
        durations = getattr(data, "durations_by_key", {}) or {}
        if not durations:
            continue
        timings[str(path)] = round(sum(durations.values()), 3)
    return dict(sorted(timings.items()))


def run(
    out: Path,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Write the timings profile to ``out``. Returns 2 when no timings exist."""
    stream = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr

    timings = collect_timings()
    if not timings:
        print(
            "ERROR: no timing data found in mutants/*.meta — run a full `mutmut "
            "run` first so every file's mutants execute and get timed.",
            file=errors,
        )
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"files": timings}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    total = sum(timings.values())
    print(
        f"Wrote {out} ({len(timings)} files, {total:.0f}s total mutmut time).",
        file=stream,
    )
    return 0
