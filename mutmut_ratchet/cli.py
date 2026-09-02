"""``mutmut-ratchet`` console entry point.

One command with five subcommands, each a drop-in replacement for the
copy-pasted ``scripts/mutation_*.py`` helpers these tools were extracted from::

    mutmut-ratchet targets <changed paths...>
    mutmut-ratchet shards --shard 0 --of 6 [--restrict PATH...]
    mutmut-ratchet stats [--paths PATH...]
    mutmut-ratchet ratchet --mode floor --stats stats.json [--update]
    mutmut-ratchet timings [--out PATH]

stdout and exit codes match the originals exactly, so migrating a workflow is a
mechanical rewrite of the command line.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import IO, Any

from . import (
    ratchet as ratchet_mod,
    shards as shards_mod,
    stats as stats_mod,
    targets as targets_mod,
    timings as timings_mod,
)
from .config import (
    DEFAULT_TOLERANCE_FRACTION,
    DEFAULT_TOLERANCE_MUTANTS,
    Config,
    ConfigError,
    load_config,
)

__all__ = ["build_parser", "main"]

# CLI flag -> config key. Every one of these overrides the pyproject value for
# this invocation; an unset flag (None) leaves the configured value alone.
_OVERRIDE_KEYS = ("package_path", "package_dotted", "baseline", "timings")


def _add_common(parser: argparse.ArgumentParser) -> None:
    """Flags every subcommand accepts: where config comes from, and its overrides."""
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PYPROJECT",
        help="pyproject.toml holding [tool.mutmut_ratchet] "
        "(default: the nearest one at or above the cwd)",
    )
    parser.add_argument(
        "--package-path",
        default=None,
        help="override the configured package path (e.g. src/my_package)",
    )
    parser.add_argument(
        "--package-dotted",
        default=None,
        help="override the configured dotted package name",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mutmut-ratchet",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ratchet = sub.add_parser(
        "ratchet",
        help="enforce the per-file mutation-score floor against the baseline",
        description=ratchet_mod.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_ratchet)
    p_ratchet.add_argument("--mode", choices=("floor", "strict"), required=True)
    p_ratchet.add_argument(
        "--stats",
        type=Path,
        required=True,
        help="per-file stats JSON from `mutmut-ratchet stats`",
    )
    p_ratchet.add_argument("--baseline", type=Path, default=None)
    p_ratchet.add_argument(
        "--tolerance-fraction",
        type=float,
        default=None,
        help=f"fractional band (default: baseline's value or {DEFAULT_TOLERANCE_FRACTION})",
    )
    p_ratchet.add_argument(
        "--tolerance-mutants",
        type=int,
        default=None,
        help=f"absolute-mutant band (default: baseline's value or {DEFAULT_TOLERANCE_MUTANTS})",
    )
    p_ratchet.add_argument(
        "--update",
        action="store_true",
        help="write current scores back to the baseline (ratchets upward)",
    )

    p_shards = sub.add_parser(
        "shards",
        help="emit one shard's mutmut patterns and source paths",
        description=shards_mod.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_shards)
    p_shards.add_argument(
        "--shard", type=int, required=True, help="shard index (0-based)"
    )
    p_shards.add_argument(
        "--of", type=int, required=True, help="total number of shards"
    )
    p_shards.add_argument("--baseline", type=Path, default=None)
    p_shards.add_argument("--timings", type=Path, default=None)
    p_shards.add_argument(
        "--restrict",
        nargs="*",
        default=None,
        metavar="PATH",
        help="emit only the intersection of this shard with these source paths "
        "(for a scoped run); the global assignment is unchanged",
    )

    p_stats = sub.add_parser(
        "stats",
        help="emit per-file mutmut statistics as JSON",
        description=stats_mod.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_stats)
    p_stats.add_argument(
        "--paths",
        nargs="*",
        default=None,
        help="restrict output to these source paths (for scoped runs)",
    )

    p_targets = sub.add_parser(
        "targets",
        help="map a PR's changed files to mutation targets",
        description=targets_mod.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_targets)
    p_targets.add_argument(
        "changed",
        nargs="*",
        metavar="PATH",
        help="changed paths (default: whitespace-separated paths on stdin)",
    )

    p_timings = sub.add_parser(
        "timings",
        help="write the committed per-file mutmut runtime profile",
        description=timings_mod.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_timings)
    p_timings.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output path (default: the configured timings file)",
    )

    return parser


def _config_for(args: argparse.Namespace) -> Config:
    overrides: dict[str, Any] = {}
    for key in _OVERRIDE_KEYS:
        value = getattr(args, key, None)
        if value is None:
            continue
        # A path given on the command line is relative to the cwd, not to the
        # pyproject.toml's directory (which is where configured paths anchor).
        overrides[key] = (
            str(Path(value).resolve()) if isinstance(value, Path) else str(value)
        )
    return load_config(args.config, overrides=overrides)


def _dispatch(
    args: argparse.Namespace,
    config: Config,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    if args.command == "ratchet":
        return ratchet_mod.run(
            config,
            args.mode,
            args.stats,
            update=args.update,
            tolerance_fraction=args.tolerance_fraction,
            tolerance_mutants=args.tolerance_mutants,
            stdout=stdout,
            stderr=stderr,
        )
    if args.command == "shards":
        return shards_mod.run(
            config,
            args.shard,
            args.of,
            restrict=args.restrict,
            stdout=stdout,
            stderr=stderr,
        )
    if args.command == "stats":
        return stats_mod.run(args.paths, stdout=stdout)
    if args.command == "targets":
        changed = args.changed or sys.stdin.read().split()
        return targets_mod.run(changed, config, stdout=stdout)
    # args.command == "timings" — argparse rejects anything else.
    out = args.out if args.out is not None else config.timings
    return timings_mod.run(out, stdout=stdout, stderr=stderr)


def main(
    argv: list[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = _config_for(args)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=err)
        return 2
    return _dispatch(args, config, out, err)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
