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

Output (stdout), four lines:
    line 1: ``all`` for a full run, or ``scoped``
    line 2: space-separated mutmut filter patterns (empty when nothing in scope)
    line 3: space-separated source paths (empty when nothing in scope)
    line 4: space-separated fully-qualified functions the run will mutate, for
            ``stats --functions`` (empty means "do not filter the per-function
            block")

Line 2 is derived by :func:`mutmut_ratchet.config.patterns_for`, which the
sharder shares, so a package ``__init__.py`` in scope gets the trampoline
patterns that actually match its mutants rather than a ``.__init__.*`` filter
that matches none of them.

A ``scoped`` mode with empty lines 2/3 means "no source in scope" — the caller
should pass (e.g. a docs-only PR).
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
from typing import IO

from .config import (
    Config,
    function_patterns_for,
    module_dotted_for_mutants,
    patterns_for,
)
from .functions import functions_for_lines

__all__ = [
    "changed_lines_from_diff",
    "git_changed_lines",
    "narrow_to_functions",
    "resolve",
    "run",
    "source_for_test",
]


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


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines_from_diff(diff: str) -> dict[str, set[int]]:
    """Map file path -> the new-file line numbers a unified diff touches.

    Expects ``git diff --unified=0`` output. Only the *new* side is read: that is
    where the code now lives, and it is what a line-to-function lookup against the
    working tree can resolve.

    A pure deletion (``+c,0``) has no new lines at all. The removed code still
    came from somewhere, so the two positions it was cut from between are recorded
    instead -- a deletion inside a function is a change to that function, and a
    deletion at module level escalates exactly as any other module-level change
    does.
    """
    lines: dict[str, set[int]] = {}
    path: str | None = None
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
            continue
        if path is None:
            continue
        match = _HUNK.match(raw)
        if not match:
            continue
        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))
        if count == 0:
            # Deletion: nothing new here, so note where it was cut out. The
            # removed code sat *between* new lines ``start`` and ``start + 1``,
            # so both are recorded. Taking only the line above would attribute a
            # function's lost first line -- or its deleted decorator -- to
            # whatever ends immediately above it, and the function that actually
            # changed would never be re-mutated.
            lines.setdefault(path, set()).update({max(start, 1), start + 1})
        else:
            lines.setdefault(path, set()).update(range(start, start + count))
    return lines


def git_changed_lines(
    base: str, paths: list[str] | None = None
) -> dict[str, set[int]] | None:
    """Changed lines per file between the merge base of ``base`` and ``HEAD``.

    ``paths`` restricts the diff to those files -- every file the caller will
    actually look up. Without it a PR touching a large lockfile or generated
    assets renders all of that as patch text only for it to be discarded.

    Three-dot on purpose: that is a pull request's own diff, so changes that
    landed on ``base`` after the branch forked are not attributed to it.

    The line numbers are resolved against the *working tree*, so an uncommitted
    edit shifts them out from under the lookup; run this on a clean tree (which
    is what CI has) or pass no ``--base`` at all.

    Returns ``None`` when git cannot answer, which callers must treat as "scope
    to whole modules" rather than "nothing changed".
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--unified=0", f"{base}...HEAD", "--", *(paths or [])],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return changed_lines_from_diff(result.stdout)


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


def narrow_to_functions(
    changed: list[str],
    sources: set[str],
    changed_lines: dict[str, set[int]] | None,
) -> dict[str, set[str] | None]:
    """Map each in-scope source to the functions to mutate, ``None`` for all of it.

    Only a source the diff touched *directly* can be narrowed. A source pulled in
    because one of its tests changed must stay whole-module: a weakened test can
    let a mutant survive anywhere in the module it exercises, not just in the
    functions the test file happens to name.
    """
    directly_changed = {raw.strip() for raw in changed}
    narrowed: dict[str, set[str] | None] = {}
    for path in sorted(sources):
        if changed_lines is None or path not in directly_changed:
            narrowed[path] = None
            continue
        try:
            source = Path(path).read_text(encoding="utf-8")
        except OSError:
            narrowed[path] = None
            continue
        narrowed[path] = functions_for_lines(source, changed_lines.get(path, set()))
    return narrowed


def run(
    changed: list[str],
    config: Config,
    *,
    base: str | None = None,
    changed_lines: dict[str, set[int]] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    """Emit the four-line target contract. Always succeeds (exit code 0).

    Line 4 names the functions the scope was narrowed *to*, for
    ``shards --restrict-functions``. A module that stays whole contributes
    nothing to it -- the shard falls back to that module's own pattern when no
    name mentions it -- so this never enumerates a file's every function, and a
    file it cannot read simply stays whole rather than blanking the line.

    ``base`` is a git ref to diff against. The diff is taken only once the scope
    is known to be narrowable, and only over the sources in it, so an escalated
    or docs-only PR never pays for it. ``changed_lines`` supplies the same
    information directly, for a caller that already has it.
    """
    stream = sys.stdout if stdout is None else stdout
    full, sources = resolve(changed, config)
    if full:
        print("all", file=stream)
        print("", file=stream)
        print("", file=stream)
        print("", file=stream)
        return 0

    paths = sorted(sources)
    if changed_lines is None and base is not None and paths:
        changed_lines = git_changed_lines(base, paths)
    narrowed = narrow_to_functions(changed, sources, changed_lines)

    patterns: list[str] = []
    functions: list[str] = []
    for path, selected in narrowed.items():
        if selected is None:
            patterns.extend(patterns_for([path], config))
            continue
        patterns.extend(function_patterns_for(path, selected, config))
        dotted = module_dotted_for_mutants(path, config)
        functions.extend(f"{dotted}.{name}" for name in sorted(selected))

    print("scoped", file=stream)
    print(" ".join(patterns), file=stream)
    print(" ".join(paths), file=stream)
    print(" ".join(functions), file=stream)
    return 0
