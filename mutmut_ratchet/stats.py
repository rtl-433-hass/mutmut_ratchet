"""Emit per-file mutmut statistics as JSON for the ratchet comparator.

mutmut 3.x's ``export-cicd-stats`` only writes a project-wide summary, so this
helper reads mutmut's per-file ``mutants/*.meta`` data directly (via mutmut's own
API) and emits a ``{"files": {path: {killed, survived, timeout, ...}}}`` payload
in the schema the ratchet expects.

Run from the repository root after ``mutmut run``::

    mutmut-ratchet stats > stats.json

For a scoped (changed-files) run, pass ``--paths`` to restrict the output to the
mutated source files. This is required after a filtered ``mutmut run`` because
mutants outside the filter stay "not checked" (which would otherwise read as 0%)::

    mutmut-ratchet stats --paths my_package/switch.py > stats.json
"""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import sys
from typing import IO

__all__ = ["collect_stats", "run"]

# Map mutmut status strings onto the buckets the ratchet understands. A mutation
# that crashes the interpreter (segfault) or trips an internal pytest error is a
# detection, so it counts as killed; "no tests"/"suspicious"/"not checked" all
# count against the score (they are recorded survivors, never suppressed).
_BUCKET = {
    "killed": "killed",
    "timeout": "timeout",
    "segfault": "killed",
    "survived": "survived",
    "no tests": "no_tests",
    "suspicious": "suspicious",
    "skipped": "skipped",
    "caught by type check": "skipped",
    "not checked": "survived",
    "check was interrupted by user": "survived",
}


def collect_stats(paths: list[str] | None = None) -> dict[str, dict[str, int]]:
    """Per-file mutant tallies, optionally restricted to ``paths``."""
    from mutmut.__main__ import status_by_exit_code, walk_mutatable_files
    from mutmut.mutation.data import SourceFileMutationData

    only = {str(Path(p)) for p in paths} if paths else None

    files: dict[str, dict[str, int]] = {}
    for path in walk_mutatable_files():
        if only is not None and str(path) not in only:
            continue
        meta = Path("mutants") / (str(path) + ".meta")
        if not meta.exists():
            continue
        data = SourceFileMutationData(path=path)
        data.load()
        if not data.exit_code_by_key:
            continue
        counts: dict[str, int] = defaultdict(int)
        for exit_code in data.exit_code_by_key.values():
            status = status_by_exit_code[exit_code]
            counts[_BUCKET.get(status, "suspicious")] += 1
        counts["total"] = sum(v for k, v in counts.items() if k != "total")
        files[str(path)] = {
            "killed": counts.get("killed", 0),
            "survived": counts.get("survived", 0),
            "timeout": counts.get("timeout", 0),
            "suspicious": counts.get("suspicious", 0),
            "skipped": counts.get("skipped", 0),
            "no_tests": counts.get("no_tests", 0),
            "total": counts["total"],
        }
    return dict(sorted(files.items()))


def run(paths: list[str] | None = None, *, stdout: IO[str] | None = None) -> int:
    """Write the stats payload as JSON. Always succeeds (exit code 0)."""
    out = sys.stdout if stdout is None else stdout
    json.dump({"files": collect_stats(paths)}, out, indent=2)
    out.write("\n")
    return 0
