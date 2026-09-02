"""Shared mutation-testing CI tooling: sharding, scoping, stats, and a ratchet.

The five pieces work together to make a slow full-package ``mutmut run`` viable
as a blocking CI gate:

``targets``
    Turn a PR's changed files into the set of source modules worth mutating (or
    escalate to a full run when a change could move results package-wide).
``shards``
    Split the package deterministically into N time-balanced shards, so the
    matrix jobs finish together.
``stats``
    Reduce mutmut's on-disk per-file meta into a comparable JSON payload.
``ratchet``
    Compare that payload against a committed per-file baseline with a
    mutant-denominated tolerance band, and fail on a real regression.
``timings``
    Record the measured per-file mutmut runtime the sharder balances on.

All configuration lives in the consumer's ``pyproject.toml`` under
``[tool.mutmut_ratchet]``; see :mod:`mutmut_ratchet.config`.
"""

from __future__ import annotations

from .config import Config, ConfigError, load_config

__all__ = ["Config", "ConfigError", "__version__", "load_config"]

__version__ = "0.1.0"
