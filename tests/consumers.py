"""The two real consumer configurations these tools were extracted from.

The originals were copy-pasted per repository, with only the package-path
constants, the escalation triggers, and the explicit test→source overrides
differing. Both are transcribed here verbatim so every behavioural test runs
against both shapes: a nested, package-heavy Home Assistant integration
(``custom_components/rtl_433``) and a flat single-package library
(``pyrtl_433``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib

__all__ = ["PROFILES", "PYRTL_433", "RTL_433", "ConsumerProfile", "make_repo"]


@dataclass(frozen=True)
class ConsumerProfile:
    """One consumer repository's mutation-tooling configuration and layout."""

    name: str
    package_path: str
    #: Source modules, relative to ``package_path``.
    modules: tuple[str, ...]
    escalate_paths: tuple[str, ...]
    explicit_test_sources: dict[str, list[str]]
    #: ``tests/<file>`` -> the module the naming convention must resolve it to.
    conforming_tests: dict[str, str] = field(default_factory=dict)
    #: Tests with no 1:1 module by design; changing one must escalate.
    broad_tests: tuple[str, ...] = ()

    @property
    def package_dotted(self) -> str:
        return self.package_path.replace("/", ".")

    def source(self, module: str) -> str:
        return f"{self.package_path}/{module}"

    @property
    def test_files(self) -> tuple[str, ...]:
        return (
            *sorted(self.explicit_test_sources),
            *sorted(self.conforming_tests),
            *sorted(self.broad_tests),
        )


# Transcribed from rtl-433-hass/rtl_433 pyproject.toml [tool.mutmut_ratchet]
# (post-migration: the mapping/ modules now live in pyrtl_433.library).
RTL_433 = ConsumerProfile(
    name="rtl_433",
    package_path="custom_components/rtl_433",
    modules=(
        "__init__.py",
        "binary_sensor.py",
        "calibration.py",
        "config_flow.py",
        "const.py",
        "coordinator/__init__.py",
        "coordinator/_watchdog.py",
        "coordinator/base.py",
        "diagnostics.py",
        "entity.py",
        "event.py",
        "hub_settings.py",
        "library.py",
        "migration.py",
        "number.py",
        "options_flow.py",
        "repairs.py",
        "sdr_settings.py",
        "select.py",
        "sensor.py",
        "switch.py",
    ),
    escalate_paths=(
        "pyproject.toml",
        "requirements_test.txt",
        "tests/conftest.py",
        ".github/workflows/mutation.yml",
    ),
    explicit_test_sources={
        "tests/test_coordinator.py": ["coordinator/base.py"],
        "tests/test_mut_init.py": ["__init__.py", "migration.py", "hub_settings.py"],
        "tests/test_config_flow.py": ["config_flow.py", "options_flow.py"],
        "tests/test_mut_config_flow.py": ["config_flow.py", "options_flow.py"],
        "tests/test_binary_sensor_motion.py": ["binary_sensor.py", "event.py"],
        "tests/test_event_trace.py": ["event.py"],
        "tests/test_diagnostics_repairs.py": ["diagnostics.py", "repairs.py"],
        "tests/test_sdr_controls.py": [
            "number.py",
            "select.py",
            "switch.py",
            "sdr_settings.py",
        ],
        "tests/test_sdr_settings_adapter.py": ["sdr_settings.py"],
        # Data-driven sweep over pyrtl_433.library (not mutated here): scopes to
        # no source module rather than escalating.
        "tests/test_fixture_coverage.py": [],
        "tests/test_mut_calibration_floor.py": ["calibration.py"],
        "tests/test_mut_library_floor.py": ["library.py"],
        "tests/test_mut_migration_floor.py": ["migration.py"],
        "tests/test_migration_roundtrip.py": ["migration.py"],
        "tests/test_mut_repairs_floor.py": ["repairs.py"],
        "tests/test_hub_availability.py": [
            "coordinator/_watchdog.py",
            "coordinator/base.py",
            "entity.py",
            "sensor.py",
            "event.py",
            "diagnostics.py",
        ],
    },
    conforming_tests={
        # The nested-package case: coordinator_base -> coordinator/base.py.
        "tests/test_mut_coordinator_base.py": "coordinator/base.py",
        "tests/test_sensor.py": "sensor.py",
        "tests/test_mut_entity.py": "entity.py",
    },
    broad_tests=(
        "tests/test_lifecycle.py",
        "tests/test_availability_class_defaults.py",
    ),
)

# Every module of pyrtl_433's ``library`` subpackage, shared by its three tests.
_PYRTL_433_LIBRARY = [
    "library/__init__.py",
    "library/_loader.py",
    "library/_model.py",
    "library/_overrides.py",
    "library/_transform.py",
]

# Transcribed from rtl-433-hass/pyrtl_433's [tool.mutmut_ratchet] config.
PYRTL_433 = ConsumerProfile(
    name="pyrtl_433",
    package_path="pyrtl_433",
    modules=(
        "__init__.py",
        "_urls.py",
        "autolevel.py",
        "availability.py",
        "client.py",
        "library/__init__.py",
        "library/_loader.py",
        "library/_model.py",
        "library/_overrides.py",
        "library/_transform.py",
        "naming.py",
        "normalizer.py",
        "replay.py",
        "sdr.py",
    ),
    escalate_paths=(
        "pyproject.toml",
        "uv.lock",
        "tests/conftest.py",
    ),
    explicit_test_sources={
        "tests/test_urls.py": ["_urls.py"],
        "tests/test_mut_client_floor.py": ["client.py"],
        "tests/test_library.py": _PYRTL_433_LIBRARY,
        "tests/test_mut_library.py": _PYRTL_433_LIBRARY,
        "tests/test_mut_library_floor.py": _PYRTL_433_LIBRARY,
    },
    conforming_tests={
        "tests/test_normalizer.py": "normalizer.py",
        "tests/test_mut_client.py": "client.py",
        "tests/test_replay.py": "replay.py",
        "tests/test_naming.py": "naming.py",
        "tests/test_availability.py": "availability.py",
    },
    broad_tests=(
        # Sweeps every JSON fixture through the normalizer and the whole device
        # library, so a change to it is correctly a full run.
        "tests/test_fixture_coverage.py",
    ),
)

PROFILES = (RTL_433, PYRTL_433)


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_list(values: object) -> str:
    assert isinstance(values, (list, tuple))
    return "[" + ", ".join(_toml_str(str(v)) for v in values) + "]"


def make_repo(root: Path, profile: ConsumerProfile, *, extra: str = "") -> Path:
    """Materialise a synthetic consumer repository under ``root``.

    Writes the package's source modules, its test files, and a ``pyproject.toml``
    carrying both ``[tool.mutmut]`` (so mutmut's own module walk finds the
    package) and ``[tool.mutmut_ratchet]``. Returns ``root``.
    """
    for module in profile.modules:
        path = root / profile.source(module)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"VALUE = {len(module)}\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    for test_file in profile.test_files:
        (root / test_file).write_text("", encoding="utf-8")

    explicit = "\n".join(
        f"{_toml_str(key)} = {_toml_list(value)}"
        for key, value in sorted(profile.explicit_test_sources.items())
    )
    (root / "pyproject.toml").write_text(
        f"""[tool.mutmut]
source_paths = [{_toml_str(profile.package_path)}]

[tool.mutmut_ratchet]
package_path = {_toml_str(profile.package_path)}
escalate_paths = {_toml_list(profile.escalate_paths)}
{extra}
[tool.mutmut_ratchet.explicit_test_sources]
{explicit}
""",
        encoding="utf-8",
    )
    # Fail loudly here rather than in a downstream assertion if the generated
    # TOML is malformed.
    tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return root
