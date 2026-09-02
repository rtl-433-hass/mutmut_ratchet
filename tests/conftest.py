"""Shared fixtures: synthetic consumer repositories and a fake mutmut run."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from consumers import PROFILES, ConsumerProfile, make_repo  # noqa: E402

from mutmut_ratchet.config import Config, load_config  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_mutmut_config() -> Iterator[None]:
    """mutmut caches its config globally; drop it around every test."""
    from mutmut.configuration import Config as MutmutConfig

    MutmutConfig.reset()
    yield
    MutmutConfig.reset()


@pytest.fixture(params=PROFILES, ids=lambda p: p.name)
def profile(request: pytest.FixtureRequest) -> ConsumerProfile:
    """Each test using this fixture runs once per consumer configuration."""
    assert isinstance(request.param, ConsumerProfile)
    return request.param


@pytest.fixture
def repo(
    tmp_path: Path, profile: ConsumerProfile, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A synthetic consumer repo for ``profile``, with the cwd moved into it."""
    make_repo(tmp_path, profile)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def config(repo: Path) -> Config:
    """The resolved config for the synthetic repo (read from its pyproject)."""
    return load_config(repo / "pyproject.toml")


def write_meta(
    repo: Path,
    source: str,
    exit_codes: dict[str, int | None],
    durations: dict[str, float] | None = None,
) -> Path:
    """Write a mutmut per-file ``.meta`` file, as a real ``mutmut run`` would."""
    meta = repo / "mutants" / (source + ".meta")
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        json.dumps(
            {
                "exit_code_by_key": exit_codes,
                "type_check_error_by_key": {},
                "durations_by_key": (
                    {k: 0.5 for k in exit_codes} if durations is None else durations
                ),
                "estimated_durations_by_key": {},
            }
        ),
        encoding="utf-8",
    )
    return meta
