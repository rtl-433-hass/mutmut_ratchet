"""Configuration for the mutation tooling, read from the consumer's pyproject.

Every constant the original per-repo scripts hard-coded (the package path, the
baseline/timings locations, the escalation triggers, the explicit test→source
overrides, the tolerance band) lives here instead, so one shared implementation
can serve any repository.

Settings are read from ``[tool.mutmut_ratchet]`` in the consumer's
``pyproject.toml``; every CLI flag that names a setting overrides it for that
invocation. Only ``package_path`` is required — everything else has the default
the original scripts used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from typing import Any

__all__ = [
    "DEFAULT_BASELINE",
    "DEFAULT_ESCALATE_PATHS",
    "DEFAULT_FALLBACK_SECONDS_PER_MUTANT",
    "DEFAULT_FLOOR",
    "DEFAULT_PRECISION",
    "DEFAULT_TIMINGS",
    "DEFAULT_TOLERANCE_FRACTION",
    "DEFAULT_TOLERANCE_MUTANTS",
    "Config",
    "ConfigError",
    "find_pyproject",
    "load_config",
    "patterns_for",
]

#: Committed per-file mutation-score baseline, relative to the project root.
DEFAULT_BASELINE = "scripts/mutation_baseline.json"
#: Committed per-file mutmut runtime profile, relative to the project root.
DEFAULT_TIMINGS = "scripts/mutation_timings.json"
#: Per-file scores are rounded to this many decimals before comparison.
DEFAULT_PRECISION = 6
# Tolerance band = max(tolerance_fraction * total, tolerance_mutants) mutants.
# - tolerance_mutants (absolute) covers the scoped-vs-full lower-bound gap and
#   run-to-run noise on SMALL files (observed worst case: 2 mutants on a
#   29-mutant module).
# - tolerance_fraction scales the band for LARGE files (e.g. ~13 on a 630-mutant
#   coordinator), absorbing their proportionally larger run-to-run drift.
DEFAULT_TOLERANCE_FRACTION = 0.02
DEFAULT_TOLERANCE_MUTANTS = 3
#: Advisory floor recorded in a freshly written baseline payload.
DEFAULT_FLOOR = 0.70
# Fallback seconds-per-mutant when neither a timing nor any profile exists at all
# (e.g. a fresh checkout with no committed timings). Only used to keep weights
# positive; the relative split is what matters.
DEFAULT_FALLBACK_SECONDS_PER_MUTANT = 1.0
#: Changed paths that escalate a scoped run to a full one, unless overridden.
DEFAULT_ESCALATE_PATHS: tuple[str, ...] = ("pyproject.toml", "tests/conftest.py")


class ConfigError(ValueError):
    """Raised when ``[tool.mutmut_ratchet]`` is missing, malformed, or unknown."""


@dataclass(frozen=True)
class Config:
    """Resolved settings for one invocation of the mutation tooling."""

    #: Repo-relative path of the package under mutation, e.g. ``pyrtl_433``.
    package_path: str
    #: Dotted name mutmut uses for that package's mutants.
    package_dotted: str
    #: Committed per-file baseline scores.
    baseline: Path
    #: Committed per-file mutmut runtime profile.
    timings: Path
    #: Changed paths that force a full run (exact repo-relative matches).
    escalate_paths: frozenset[str]
    #: Test module -> the source modules (relative to ``package_path``) it covers.
    explicit_test_sources: dict[str, list[str]] = field(default_factory=dict)
    tolerance_fraction: float = DEFAULT_TOLERANCE_FRACTION
    tolerance_mutants: int = DEFAULT_TOLERANCE_MUTANTS
    precision: int = DEFAULT_PRECISION
    floor: float = DEFAULT_FLOOR
    fallback_seconds_per_mutant: float = DEFAULT_FALLBACK_SECONDS_PER_MUTANT

    def source(self, module: str) -> str:
        """Repo-relative path of ``module`` inside the mutated package."""
        return f"{self.package_path}/{module}"

    def dotted(self, path: str) -> str:
        """Dotted mutmut module name for a repo-relative source ``path``."""
        stem = path[len(self.package_path) + 1 : -len(".py")]
        return f"{self.package_dotted}.{stem.replace('/', '.')}"


def patterns_for(paths: list[str], config: Config) -> list[str]:
    """Derive the ``mutmut run`` filter patterns that select ``paths``.

    A normal module ``a/b.py`` matches ``<pkg>.a.b.*``.

    A package ``__init__.py`` is special: mutmut strips the ``__init__`` segment
    from its mutant names (``get_mutant_name`` does ``.replace('.__init__.', '.')``),
    so the package-root mutants live directly under the package's dotted name —
    e.g. ``<pkg>.sub.x_lookup__mutmut_1``. The naive ``<pkg>.sub.__init__.*``
    therefore matches *no mutant at all*: every package-root mutant would go
    unrun and score 0. A bare ``<pkg>.sub.*`` would over-match (it also catches
    every submodule), so instead we match only the mutant trampolines, which
    mutmut names ``x_*`` (functions) and ``xǁ*`` (class methods); no module name
    starts with those, so this selects exactly the package-root mutants.
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


# Keys accepted in ``[tool.mutmut_ratchet]``; anything else is a typo, and a
# silently ignored typo here would quietly weaken the gate.
_KEYS: dict[str, type | tuple[type, ...]] = {
    "package_path": str,
    "package_dotted": str,
    "baseline": str,
    "timings": str,
    "escalate_paths": list,
    "explicit_test_sources": dict,
    "tolerance_fraction": (int, float),
    "tolerance_mutants": int,
    "precision": int,
    "floor": (int, float),
    "fallback_seconds_per_mutant": (int, float),
}


def find_pyproject(start: Path | None = None) -> Path | None:
    """Nearest ``pyproject.toml`` at or above ``start`` (default: the cwd)."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        path = candidate / "pyproject.toml"
        if path.is_file():
            return path
    return None


def _read_table(pyproject: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - message passthrough
        raise ConfigError(f"{pyproject}: invalid TOML: {exc}") from exc
    tool = data.get("tool")
    table = tool.get("mutmut_ratchet") if isinstance(tool, dict) else None
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ConfigError(f"{pyproject}: [tool.mutmut_ratchet] must be a table")
    return table


def _validate(table: dict[str, Any], origin: str) -> None:
    for key, value in table.items():
        expected = _KEYS.get(key)
        if expected is None:
            known = ", ".join(sorted(_KEYS))
            raise ConfigError(f"{origin}: unknown setting {key!r} (known: {known})")
        # bool is a subclass of int, but never a valid value for a numeric key.
        if isinstance(value, bool) or not isinstance(value, expected):
            names = expected if isinstance(expected, tuple) else (expected,)
            wanted = " or ".join(t.__name__ for t in names)
            raise ConfigError(f"{origin}: {key!r} must be {wanted}")


def _escalate_paths(raw: list[Any], origin: str) -> frozenset[str]:
    """``_validate`` has already proven this is a list; check the elements."""
    if not all(isinstance(p, str) for p in raw):
        raise ConfigError(f"{origin}: 'escalate_paths' must be a list of strings")
    return frozenset(str(p) for p in raw)


def _explicit_test_sources(raw: dict[str, Any], origin: str) -> dict[str, list[str]]:
    """``_validate`` has already proven this is a table; check the values."""
    out: dict[str, list[str]] = {}
    for test_file, modules in raw.items():
        if not isinstance(modules, list) or not all(
            isinstance(m, str) for m in modules
        ):
            raise ConfigError(
                f"{origin}: explicit_test_sources[{test_file!r}] must be a list of "
                "module paths"
            )
        out[str(test_file)] = [str(m) for m in modules]
    return out


def load_config(
    pyproject: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    required: bool = True,
) -> Config:
    """Resolve settings from ``pyproject.toml``, then apply CLI ``overrides``.

    ``pyproject`` defaults to the nearest one at or above the cwd. Relative
    ``baseline``/``timings`` paths resolve against that file's directory, so the
    tooling works from a subdirectory as well as from the repository root.

    Overrides whose value is ``None`` (an unset CLI flag) are ignored, so flag
    precedence is simply "flag beats file beats built-in default".
    """
    path = pyproject if pyproject is not None else find_pyproject()
    if path is None:
        if required:
            raise ConfigError(
                "no pyproject.toml found at or above the current directory; "
                "pass --config to point at one"
            )
        table: dict[str, Any] = {}
        root = Path.cwd()
        origin = "<no pyproject.toml>"
    else:
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        table = _read_table(path)
        root = path.parent
        origin = str(path)

    # Overrides are validated alongside the file's own settings, so a bad
    # programmatic override fails the same way a bad pyproject entry does.
    merged: dict[str, Any] = dict(table)
    merged.update({k: v for k, v in (overrides or {}).items() if v is not None})
    _validate(merged, origin)

    package_path = merged.get("package_path")
    if not isinstance(package_path, str) or not package_path:
        raise ConfigError(
            f"{origin}: [tool.mutmut_ratchet] requires 'package_path' "
            '(e.g. package_path = "my_package"); pass --package-path to override'
        )
    package_path = package_path.strip("/")

    def _path(key: str, default: str) -> Path:
        raw = merged.get(key, default)
        candidate = Path(str(raw))
        return candidate if candidate.is_absolute() else root / candidate

    return Config(
        package_path=package_path,
        package_dotted=str(merged.get("package_dotted") or package_path).replace(
            "/", "."
        ),
        baseline=_path("baseline", DEFAULT_BASELINE),
        timings=_path("timings", DEFAULT_TIMINGS),
        escalate_paths=_escalate_paths(
            merged.get("escalate_paths", list(DEFAULT_ESCALATE_PATHS)), origin
        ),
        explicit_test_sources=_explicit_test_sources(
            merged.get("explicit_test_sources", {}), origin
        ),
        tolerance_fraction=float(
            merged.get("tolerance_fraction", DEFAULT_TOLERANCE_FRACTION)
        ),
        tolerance_mutants=int(
            merged.get("tolerance_mutants", DEFAULT_TOLERANCE_MUTANTS)
        ),
        precision=int(merged.get("precision", DEFAULT_PRECISION)),
        floor=float(merged.get("floor", DEFAULT_FLOOR)),
        fallback_seconds_per_mutant=float(
            merged.get(
                "fallback_seconds_per_mutant", DEFAULT_FALLBACK_SECONDS_PER_MUTANT
            )
        ),
    )
