"""Application configuration.

All tunable behaviour is defined here and can be overridden with environment
variables (see ``.env.example``).  The module deliberately contains no database or
data-frame logic so that it can be imported cheaply by every component.

Units used throughout the project:

* active power: kW
* irradiance: W/m²
* temperature: °C
* timestamps: stored and processed in UTC
* energy: kWh
* irradiation: kWh/m²
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Canonical measurement channels.  A CSV row contains one value per channel.
METRIC_COLUMNS: tuple[str, ...] = (
    "active_power_kw",
    "irradiance_w_m2",
    "module_temperature_c",
    "ambient_temperature_c",
)

METRIC_UNITS: Mapping[str, str] = {
    "active_power_kw": "kW",
    "irradiance_w_m2": "W/m2",
    "module_temperature_c": "degC",
    "ambient_temperature_c": "degC",
}

METRIC_LABELS: Mapping[str, str] = {
    "active_power_kw": "Active power",
    "irradiance_w_m2": "Irradiance",
    "module_temperature_c": "Module temperature",
    "ambient_temperature_c": "Ambient temperature",
}

REQUIRED_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "installation_id",
    "sensor_id",
    *METRIC_COLUMNS,
)

VALID_MISSING_STRATEGIES = ("none", "interpolate", "ffill", "drop")
VALID_DUPLICATE_POLICIES = ("first", "last")

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg2://renewable:renewable@localhost:5432/renewable_energy"
)


class ConfigurationError(ValueError):
    """Raised when configuration values are invalid."""


@dataclass(frozen=True)
class ValidationConfig:
    """Physical plausibility thresholds.

    These are *physical* limits, not statistical outlier rules.  A value inside a
    plausible range is never invalidated merely because it is unusual.
    """

    power_min_kw: float = -0.1
    power_max_kw: float = 500.0
    power_capacity_suspicious_factor: float = 1.2
    power_capacity_invalid_factor: float = 3.0
    irradiance_min_w_m2: float = 0.0
    irradiance_max_w_m2: float = 1500.0
    irradiance_suspicious_max_w_m2: float = 1300.0
    module_temp_invalid_min_c: float = -40.0
    module_temp_invalid_max_c: float = 120.0
    module_temp_suspicious_min_c: float = -20.0
    module_temp_suspicious_max_c: float = 90.0
    ambient_temp_invalid_min_c: float = -50.0
    ambient_temp_invalid_max_c: float = 60.0
    ambient_temp_suspicious_min_c: float = -30.0
    ambient_temp_suspicious_max_c: float = 50.0

    def __post_init__(self) -> None:
        if self.power_min_kw >= self.power_max_kw:
            raise ConfigurationError("POWER_MIN_KW must be smaller than POWER_MAX_KW")
        if self.irradiance_min_w_m2 >= self.irradiance_max_w_m2:
            raise ConfigurationError(
                "IRRADIANCE_MIN_W_M2 must be smaller than IRRADIANCE_MAX_W_M2"
            )
        if not (
            self.irradiance_min_w_m2
            <= self.irradiance_suspicious_max_w_m2
            <= self.irradiance_max_w_m2
        ):
            raise ConfigurationError(
                "IRRADIANCE_SUSPICIOUS_MAX_W_M2 must lie inside the valid range"
            )
        for name in ("module_temp", "ambient_temp"):
            invalid_min = getattr(self, f"{name}_invalid_min_c")
            invalid_max = getattr(self, f"{name}_invalid_max_c")
            suspicious_min = getattr(self, f"{name}_suspicious_min_c")
            suspicious_max = getattr(self, f"{name}_suspicious_max_c")
            if not (invalid_min <= suspicious_min <= suspicious_max <= invalid_max):
                raise ConfigurationError(
                    f"{name} suspicious range must lie inside the invalid range"
                )
        if self.power_capacity_suspicious_factor >= self.power_capacity_invalid_factor:
            raise ConfigurationError(
                "power_capacity_suspicious_factor must be smaller than "
                "power_capacity_invalid_factor"
            )


@dataclass(frozen=True)
class CleaningConfig:
    """Cleaning and imputation settings."""

    expected_interval_minutes: int = 15
    # Empty string disables resampling; data then keeps its observed timestamps.
    resample_frequency: str | None = "15min"
    missing_strategy: str = "interpolate"
    interpolation_max_gap_minutes: int = 30
    duplicate_keep: str = "first"

    def __post_init__(self) -> None:
        if self.expected_interval_minutes <= 0:
            raise ConfigurationError("EXPECTED_INTERVAL_MINUTES must be positive")
        if self.missing_strategy not in VALID_MISSING_STRATEGIES:
            raise ConfigurationError(
                f"MISSING_STRATEGY must be one of {VALID_MISSING_STRATEGIES}"
            )
        if self.duplicate_keep not in VALID_DUPLICATE_POLICIES:
            raise ConfigurationError(
                f"DUPLICATE_KEEP must be one of {VALID_DUPLICATE_POLICIES}"
            )
        if self.interpolation_max_gap_minutes < 0:
            raise ConfigurationError("INTERPOLATION_MAX_GAP_MINUTES must be >= 0")

    @property
    def max_interpolation_slots(self) -> int:
        """Maximum number of consecutive missing samples that may be filled."""
        return self.interpolation_max_gap_minutes // self.expected_interval_minutes


@dataclass(frozen=True)
class AnalyticsConfig:
    """Analytics requirements, especially for Performance Ratio."""

    pr_min_irradiance_w_m2: float = 20.0
    pr_min_coverage: float = 0.8
    min_months_for_trend: int = 3

    def __post_init__(self) -> None:
        if not 0 <= self.pr_min_coverage <= 1:
            raise ConfigurationError("PR_MIN_COVERAGE must be between 0 and 1")
        if self.pr_min_irradiance_w_m2 < 0:
            raise ConfigurationError("PR_MIN_IRRADIANCE_W_M2 must be >= 0")
        if self.min_months_for_trend < 2:
            raise ConfigurationError("min_months_for_trend must be at least 2")


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    input_dir: Path = PROJECT_ROOT / "data" / "raw"
    output_dir: Path = PROJECT_ROOT / "output"
    synthetic_dir: Path = PROJECT_ROOT / "data" / "synthetic"
    installations_file: Path = PROJECT_ROOT / "config" / "installations.csv"
    database_url: str = DEFAULT_DATABASE_URL
    input_timezone: str = "Europe/Berlin"
    data_provenance: str = (
        "SYNTHETIC EXAMPLE DATA - generated for demonstration, not real measurements"
    )
    plot_dpi: int = 120
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    cleaning: CleaningConfig = field(default_factory=CleaningConfig)
    analytics: AnalyticsConfig = field(default_factory=AnalyticsConfig)

    @property
    def plots_dir(self) -> Path:
        return self.output_dir / "plots"

    @property
    def reports_dir(self) -> Path:
        return self.output_dir / "reports"

    @property
    def csv_dir(self) -> Path:
        return self.output_dir / "csv"

    def ensure_directories(self) -> None:
        for path in (
            self.input_dir,
            self.output_dir,
            self.plots_dir,
            self.reports_dir,
            self.csv_dir,
            self.synthetic_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class InstallationMeta:
    """Optional installation master data supplied by the research group."""

    installation_id: str
    name: str | None = None
    location: str | None = None
    capacity_kw: float | None = None


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value is not None else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc


def _resolve_path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from ``.env`` and environment variables.

    Real environment variables take precedence over values in ``.env``.
    """

    root = PROJECT_ROOT
    env_path = env_file if env_file is not None else root / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    data_provenance = _env(
        "DATA_PROVENANCE",
        "SYNTHETIC EXAMPLE DATA - generated for demonstration, not real measurements",
    )
    resample = _env("RESAMPLE_FREQUENCY", "15min")
    settings = Settings(
        project_root=root,
        input_dir=_resolve_path(_env("INPUT_DIR", "data/raw"), root),
        output_dir=_resolve_path(_env("OUTPUT_DIR", "output"), root),
        synthetic_dir=_resolve_path("data/synthetic", root),
        installations_file=_resolve_path(
            _env("INSTALLATIONS_FILE", "config/installations.csv"), root
        ),
        database_url=_env("DATABASE_URL", DEFAULT_DATABASE_URL),
        input_timezone=_env("INPUT_TIMEZONE", "Europe/Berlin"),
        data_provenance=data_provenance,
        plot_dpi=_env_int("PLOT_DPI", 120),
        validation=ValidationConfig(
            power_min_kw=_env_float("POWER_MIN_KW", -0.1),
            power_max_kw=_env_float("POWER_MAX_KW", 500.0),
            irradiance_min_w_m2=_env_float("IRRADIANCE_MIN_W_M2", 0.0),
            irradiance_max_w_m2=_env_float("IRRADIANCE_MAX_W_M2", 1500.0),
            irradiance_suspicious_max_w_m2=_env_float(
                "IRRADIANCE_SUSPICIOUS_MAX_W_M2", 1300.0
            ),
            module_temp_invalid_min_c=_env_float("MODULE_TEMP_INVALID_MIN_C", -40.0),
            module_temp_invalid_max_c=_env_float("MODULE_TEMP_INVALID_MAX_C", 120.0),
            module_temp_suspicious_min_c=_env_float(
                "MODULE_TEMP_SUSPICIOUS_MIN_C", -20.0
            ),
            module_temp_suspicious_max_c=_env_float(
                "MODULE_TEMP_SUSPICIOUS_MAX_C", 90.0
            ),
            ambient_temp_invalid_min_c=_env_float(
                "AMBIENT_TEMP_INVALID_MIN_C", -50.0
            ),
            ambient_temp_invalid_max_c=_env_float("AMBIENT_TEMP_INVALID_MAX_C", 60.0),
            ambient_temp_suspicious_min_c=_env_float(
                "AMBIENT_TEMP_SUSPICIOUS_MIN_C", -30.0
            ),
            ambient_temp_suspicious_max_c=_env_float(
                "AMBIENT_TEMP_SUSPICIOUS_MAX_C", 50.0
            ),
        ),
        cleaning=CleaningConfig(
            expected_interval_minutes=_env_int("EXPECTED_INTERVAL_MINUTES", 15),
            resample_frequency=resample or None,
            missing_strategy=_env("MISSING_STRATEGY", "interpolate").lower(),
            interpolation_max_gap_minutes=_env_int(
                "INTERPOLATION_MAX_GAP_MINUTES", 30
            ),
            duplicate_keep=_env("DUPLICATE_KEEP", "first").lower(),
        ),
        analytics=AnalyticsConfig(
            pr_min_irradiance_w_m2=_env_float("PR_MIN_IRRADIANCE_W_M2", 20.0),
            pr_min_coverage=_env_float("PR_MIN_COVERAGE", 0.8),
        ),
    )
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(settings.input_timezone)
    except Exception as exc:  # noqa: BLE001 - reported as configuration error
        raise ConfigurationError(
            f"Unknown INPUT_TIMEZONE {settings.input_timezone!r}: {exc}"
        ) from exc
    return settings


def load_installation_metadata(path: Path) -> dict[str, InstallationMeta]:
    """Load optional installation master data from CSV.

    Required column: ``installation_id``.  Optional columns: ``name``,
    ``location``, ``capacity_kw``.  Capacity is required before a Performance
    Ratio can be calculated.
    """

    if not path.exists():
        return {}
    metadata: dict[str, InstallationMeta] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return {}
        normalized = [name.strip().lower() for name in reader.fieldnames]
        if "installation_id" not in normalized:
            raise ConfigurationError(
                f"{path} must contain an installation_id column"
            )
        for line_number, row in enumerate(reader, start=2):
            clean = {
                (key or "").strip().lower(): (value or "").strip()
                for key, value in row.items()
            }
            installation_id = clean.get("installation_id", "")
            if not installation_id:
                raise ConfigurationError(
                    f"{path}:{line_number}: empty installation_id"
                )
            if installation_id in metadata:
                raise ConfigurationError(
                    f"{path}:{line_number}: duplicate installation_id "
                    f"{installation_id!r}"
                )
            capacity_raw = clean.get("capacity_kw", "")
            capacity: float | None = None
            if capacity_raw:
                try:
                    capacity = float(capacity_raw)
                except ValueError as exc:
                    raise ConfigurationError(
                        f"{path}:{line_number}: capacity_kw must be numeric"
                    ) from exc
                if capacity <= 0:
                    raise ConfigurationError(
                        f"{path}:{line_number}: capacity_kw must be positive"
                    )
            metadata[installation_id] = InstallationMeta(
                installation_id=installation_id,
                name=clean.get("name") or None,
                location=clean.get("location") or None,
                capacity_kw=capacity,
            )
    return metadata


def describe_database_url(url: str) -> str:
    """Return a safe display form of a database URL (password removed)."""

    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    credentials, host = rest.rsplit("@", 1)
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"
