"""Emit per-file and per-function mutmut statistics as JSON for the ratchet.

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

**Per-function tallies.** The payload also carries a ``"functions"`` block,
``{path: {mangled_function_name: {...}}}``, derived by grouping each file's
mutants on the function they belong to. mutmut names a mutant
``<module>.<mangled function>__mutmut_<n>`` (``x_name`` for a plain function,
``xǁClassǁmethod`` for a method), so the function is simply the key up to the
``__mutmut_`` marker. This is what lets a *function*-scoped run be gated: with
only some of a file's functions mutated, the file's own score is a partial
measurement and cannot be compared against a whole-file baseline, but each
mutated function's score is complete and can.

``--functions`` restricts that block the way ``--paths`` restricts ``files``.
Pass the functions the run actually filtered to; without it, every function in
the file appears, including ones whose mutants were never executed (they would
read as 0% for the same reason a whole unfiltered file does). The block also
reports ``not_checked`` — the subset of ``survived`` that mutmut never ran — so a
consumer that cannot pass an explicit list can still tell "no tests killed these"
apart from "these were never attempted".
"""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import sys
from typing import IO

from .functions import MUTANT_MARKER

__all__ = ["collect_function_stats", "collect_stats", "function_of", "run"]

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


def function_of(mutant_key: str) -> str:
    """The function a mutant belongs to, from its mutmut key.

    ``pkg.mod.x_parse__mutmut_3`` -> ``pkg.mod.x_parse``. Mirrors mutmut's own
    ``mangled_name_from_mutant_name`` but tolerates a key without the marker
    (which mutmut itself asserts on) by treating it as its own function, so a
    hand-written or future-format meta file degrades into one-function-per-mutant
    rather than raising in the middle of a CI gate.
    """
    return (
        mutant_key.partition(MUTANT_MARKER)[0]
        if MUTANT_MARKER in mutant_key
        else mutant_key
    )


def _tally(statuses: list[str]) -> dict[str, int]:
    """Bucket a list of mutmut status strings into the ratchet's schema."""
    counts: dict[str, int] = defaultdict(int)
    for status in statuses:
        counts[_BUCKET.get(status, "suspicious")] += 1
    return {
        "killed": counts.get("killed", 0),
        "survived": counts.get("survived", 0),
        "timeout": counts.get("timeout", 0),
        "suspicious": counts.get("suspicious", 0),
        "skipped": counts.get("skipped", 0),
        "no_tests": counts.get("no_tests", 0),
        "total": sum(counts.values()),
        # A subset of "survived": mutmut recorded the mutant but never ran it,
        # which is exactly what a mutant outside the run's filter looks like.
        # This is the field that tells a measured tally from an unmeasured one,
        # so a scoped run needs no out-of-band list of what it covered.
        "not_checked": sum(1 for s in statuses if s == "not checked"),
    }


def _load_statuses(paths: list[str] | None) -> dict[str, dict[str, str]]:
    """Map source path -> {mutant key: mutmut status}, optionally restricted."""
    from mutmut.__main__ import status_by_exit_code, walk_mutatable_files
    from mutmut.mutation.data import SourceFileMutationData

    only = {str(Path(p)) for p in paths} if paths else None

    by_path: dict[str, dict[str, str]] = {}
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
        by_path[str(path)] = {
            key: status_by_exit_code[exit_code]
            for key, exit_code in data.exit_code_by_key.items()
        }
    return by_path


def _files_from(by_path: dict[str, dict[str, str]]) -> dict[str, dict[str, int]]:
    """Per-file tallies from already-loaded statuses."""
    return {
        path: _tally(list(statuses.values()))
        for path, statuses in sorted(by_path.items())
    }


def collect_stats(paths: list[str] | None = None) -> dict[str, dict[str, int]]:
    """Per-file mutant tallies, optionally restricted to ``paths``."""
    return _files_from(_load_statuses(paths))


def collect_function_stats(
    paths: list[str] | None = None,
) -> dict[str, dict[str, dict[str, int]]]:
    """Per-function mutant tallies, keyed ``{path: {function: tally}}``."""
    return _functions_from(_load_statuses(paths))


def _functions_from(
    by_path: dict[str, dict[str, str]],
) -> dict[str, dict[str, dict[str, int]]]:
    """Per-function tallies from already-loaded statuses."""
    out: dict[str, dict[str, dict[str, int]]] = {}
    for path, statuses in sorted(by_path.items()):
        grouped: dict[str, list[str]] = defaultdict(list)
        for key, status in statuses.items():
            grouped[function_of(key)].append(status)
        out[path] = {func: _tally(found) for func, found in sorted(grouped.items())}
    return out


def run(
    paths: list[str] | None = None,
    *,
    stdout: IO[str] | None = None,
) -> int:
    """Write the stats payload as JSON. Always succeeds (exit code 0)."""
    out = sys.stdout if stdout is None else stdout
    # One pass over the meta files; the two blocks are reductions of the same
    # data, and loading it twice doubles the I/O for every mutant in the package.
    by_path = _load_statuses(paths)
    payload = {
        "files": _files_from(by_path),
        "functions": _functions_from(by_path),
    }
    json.dump(payload, out, indent=2)
    out.write("\n")
    return 0
