"""Map a PR's changed files to the mutmut targets the CI mutation job should run.

A full-package mutmut run is slow. On pull requests we only need to re-check the
modules a PR could have affected, so this helper turns ``git diff --name-only``
output into:

* the source modules to mutate (so the per-file floor is enforced on touched code), and
* the matching ``mutmut run`` filter patterns.

Mapping rules (given changed paths on argv):

* A changed ``<package_path>/<mod>.py`` maps to itself.
* A changed test file maps to the source module it exercises, when that can be
  resolved unambiguously: ``tests/test[_mut]_<name>.py`` →
  ``<package_path>/<name>.py`` (also trying ``<a>/<b>.py`` for a ``<a>_<b>``
  name, e.g. ``coordinator_base`` → ``coordinator/base.py``). This closes the "a
  test was weakened but its source is unchanged" blind spot for the common case.
  A test that can't be resolved to one module escalates to a full run.
* Tests that don't follow that 1:1 convention — compound tests exercising several
  modules, package roots, and any ``__init__`` root (no ``init.py`` exists for the
  resolver to find) — are listed in the ``explicit_test_sources`` config table
  with the exact modules they cover, so touching them scopes rather than
  escalates. Genuinely broad integration tests are deliberately left out, so they
  still escalate — a full run is correct when they change.
* Any change to a path in the ``escalate_paths`` config list (mutation
  infrastructure, shared test scaffolding, the workflow itself) escalates to a
  full run, because it can change results package-wide.

Output (stdout), three lines:
    line 1: ``all`` for a full run, or ``scoped``
    line 2: space-separated mutmut filter patterns (empty when nothing in scope)
    line 3: space-separated source paths (empty when nothing in scope)

Line 2 is derived by :func:`mutmut_ratchet.config.patterns_for`, which the
sharder shares, so a package ``__init__.py`` in scope gets the trampoline
patterns that actually match its mutants rather than a ``.__init__.*`` filter
that matches none of them.

A ``scoped`` mode with empty lines 2/3 means "no source in scope" — the caller
should pass (e.g. a docs-only PR).
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import IO

from .config import Config, patterns_for

__all__ = ["resolve", "run", "source_for_test"]


def source_for_test(stem: str, config: Config) -> str | None:
    """Resolve a test-file stem to its source module path, or None if ambiguous."""
    for prefix in ("test_mut_", "test_"):
        if stem.startswith(prefix):
            name = stem[len(prefix) :]
            break
    else:
        return None
    # Try a flat module, then progressively turn underscores into a sub-path
    # (coordinator_base -> coordinator/base) so package submodules resolve.
    candidates = [name.replace("_", "/")]
    parts = name.split("_")
    for i in range(len(parts) - 1, 0, -1):
        candidates.append("/".join(["_".join(parts[:i]), *parts[i:]]))
    candidates.append(name)
    for cand in dict.fromkeys(candidates):
        path = f"{config.package_path}/{cand}.py"
        if Path(path).is_file():
            return path
    return None


def resolve(changed: list[str], config: Config) -> tuple[bool, set[str]]:
    """Return (full_run, source_paths) for the changed files."""
    sources: set[str] = set()
    pkg_prefix = f"{config.package_path}/"
    for raw in changed:
        f = raw.strip()
        if not f:
            continue
        if f in config.escalate_paths:
            return True, set()
        if f.startswith(pkg_prefix) and f.endswith(".py"):
            sources.add(f)
        elif f.startswith("tests/") and f.endswith(".py"):
            explicit = config.explicit_test_sources.get(f)
            if explicit is not None:
                sources.update(config.source(module) for module in explicit)
                continue
            src = source_for_test(Path(f).stem, config)
            if src is None:
                # A broad/unmappable test changed — be safe and run everything.
                return True, set()
            sources.add(src)
        # Any other path (docs, brands, etc.) is irrelevant to mutation.
    return False, sources


def run(changed: list[str], config: Config, *, stdout: IO[str] | None = None) -> int:
    """Emit the three-line target contract. Always succeeds (exit code 0)."""
    stream = sys.stdout if stdout is None else stdout
    full, sources = resolve(changed, config)
    if full:
        print("all", file=stream)
        print("", file=stream)
        print("", file=stream)
        return 0
    paths = sorted(sources)
    patterns = patterns_for(paths, config)
    print("scoped", file=stream)
    print(" ".join(patterns), file=stream)
    print(" ".join(paths), file=stream)
    return 0
