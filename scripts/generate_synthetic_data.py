"""Generate deterministic, clearly synthetic photovoltaic example data.

The generator models a simple clear-sky / cloud-cover pattern and a temperature
derating factor.  It is a demonstration fixture, **not** a validated PV simulation
model, and the output must never be presented as real measurement data.

Usage:
    python scripts/generate_synthetic_data.py
    python scripts/generate_synthetic_data.py --no-copy-to-raw
"""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LATITUDE_DEG = 52.5  # Berlin, used only for plausible synthetic solar geometry
LONGITUDE_DEG = 13.4
LOCAL_TZ = "Europe/Berlin"

COLUMNS = [
    "timestamp",
    "installation_id",
    "sensor_id",
    "active_power_kw",
    "irradiance_w_m2",
    "module_temperature_c",
    "ambient_temperature_c",
]


@dataclass(frozen=True)
class InstallationSpec:
    installation_id: str
    sensor_id: str
    capacity_kw: float
    tilt_deg: float
    azimuth_deg: float  # 0 = south, negative = east, positive = west
    performance_factor: float
    start: str
    end: str


INSTALLATIONS = (
    InstallationSpec("INST_A", "A-INV1", 10.0, 30.0, 0.0, 0.82, "2023-01-01", "2024-01-01"),
    InstallationSpec("INST_B", "B-INV1", 5.5, 15.0, -55.0, 0.78, "2023-01-01", "2024-01-01"),
    InstallationSpec("INST_C", "C-INV1", 3.2, 90.0, 90.0, 0.70, "2024-01-01", "2024-07-01"),
)


def _solar_geometry(index: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """Approximate solar elevation and azimuth (radians) for synthetic data."""

    day_of_year = index.dayofyear.to_numpy(dtype=float)
    solar_hour = (
        index.hour.to_numpy(dtype=float)
        + index.minute.to_numpy(dtype=float) / 60.0
        + LONGITUDE_DEG / 15.0
    )
    declination = np.radians(23.45) * np.sin(
        2.0 * np.pi * (284.0 + day_of_year) / 365.0
    )
    hour_angle = np.radians(15.0 * (solar_hour - 12.0))
    latitude = np.radians(LATITUDE_DEG)
    sin_elevation = (
        np.sin(latitude) * np.sin(declination)
        + np.cos(latitude) * np.cos(declination) * np.cos(hour_angle)
    )
    elevation = np.arcsin(np.clip(sin_elevation, -1.0, 1.0))
    cos_elevation = np.maximum(np.cos(elevation), 1e-6)
    cos_azimuth = (
        np.sin(declination) - np.sin(elevation) * np.sin(latitude)
    ) / (cos_elevation * np.cos(latitude))
    sin_azimuth = (
        -np.cos(declination) * np.sin(hour_angle) / cos_elevation
    )
    # Azimuth is returned relative to south (negative = east, positive = west).
    azimuth = np.arctan2(sin_azimuth, cos_azimuth) - np.pi
    azimuth = (azimuth + np.pi) % (2.0 * np.pi) - np.pi
    return elevation, azimuth


def _base_frame(
    spec: InstallationSpec, start: pd.Timestamp, end: pd.Timestamp, seed: int
) -> pd.DataFrame:
    index = pd.date_range(start, end, freq="15min", inclusive="left", tz="UTC")
    rng = np.random.default_rng(seed)
    n = len(index)
    elevation, solar_azimuth = _solar_geometry(index)
    day_of_year = index.dayofyear.to_numpy(dtype=float)
    hour = index.hour.to_numpy(dtype=float) + index.minute.to_numpy(dtype=float) / 60.0

    # Smooth, deterministic cloud variability (not a physical cloud model).
    cloud = (
        0.66
        + 0.16 * np.sin(2.0 * np.pi * day_of_year / 11.0 + seed % 17)
        + 0.10 * np.sin(2.0 * np.pi * hour / 6.0 + seed % 13)
        + rng.normal(0.0, 0.035, size=n)
    )
    cloud = np.clip(cloud, 0.12, 1.0)

    tilt = np.radians(spec.tilt_deg)
    panel_azimuth = np.radians(spec.azimuth_deg)
    cos_incidence = (
        np.sin(elevation) * np.cos(tilt)
        + np.cos(elevation) * np.sin(tilt) * np.cos(solar_azimuth - panel_azimuth)
    )
    irradiance = 1000.0 * np.clip(cos_incidence, 0.0, None) ** 1.05 * cloud
    irradiance = np.where(elevation > 0, irradiance, 0.0)
    irradiance = np.clip(irradiance + rng.normal(0.0, 3.0, size=n), 0.0, None)

    seasonal = 10.0 + 9.0 * np.sin(
        2.0 * np.pi * (day_of_year - 105.0) / 365.0
    )
    diurnal = 4.5 * np.sin(2.0 * np.pi * (hour - 9.0) / 24.0)
    ambient = seasonal + diurnal + rng.normal(0.0, 1.2, size=n)
    module = ambient + irradiance / 1000.0 * 28.0 + rng.normal(0.0, 0.8, size=n)

    temperature_factor = 1.0 - 0.0035 * (module - 25.0)
    power = (
        spec.capacity_kw
        * irradiance
        / 1000.0
        * spec.performance_factor
        * np.clip(temperature_factor, 0.75, 1.1)
    )
    power = np.clip(
        power * (1.0 + rng.normal(0.0, 0.012, size=n)), 0.0, spec.capacity_kw * 1.05
    )
    power = np.where(irradiance > 0, power, 0.0)

    return pd.DataFrame(
        {
            "timestamp": index,
            "installation_id": spec.installation_id,
            "sensor_id": spec.sensor_id,
            "active_power_kw": power,
            "irradiance_w_m2": irradiance,
            "module_temperature_c": module,
            "ambient_temperature_c": ambient,
        }
    )


def _pick(rng: np.random.Generator, mask: np.ndarray, count: int = 1) -> np.ndarray:
    indices = np.flatnonzero(mask)
    if len(indices) == 0:
        return np.array([], dtype=int)
    return rng.choice(indices, size=min(count, len(indices)), replace=False)


def _inject_quality_problems(
    frame: pd.DataFrame,
    spec: InstallationSpec,
    rng: np.random.Generator,
    drop_block_rows: int,
    add_cross_file_duplicate: bool,
) -> tuple[pd.DataFrame, list[tuple[int, str, str]]]:
    """Inject documented, deliberate defects. Returns frame and malformed overrides."""

    frame = frame.copy().reset_index(drop=True)
    n = len(frame)
    malformed: list[tuple[int, str, str]] = []
    if n < 200:
        return frame, malformed

    # Remove a contiguous block to create a missing interval, plus isolated rows.
    if drop_block_rows:
        start = int(n * 0.55)
        frame = frame.drop(index=range(start, start + drop_block_rows)).reset_index(
            drop=True
        )
        n = len(frame)

    # Missing values (blank fields) in roughly 0.2 % of each channel.
    for column in COLUMNS[3:]:
        missing_idx = rng.choice(n, size=max(1, int(n * 0.002)), replace=False)
        frame.loc[missing_idx, column] = np.nan

    daylight = (frame["irradiance_w_m2"].fillna(0) > 250).to_numpy()
    night = (frame["irradiance_w_m2"].fillna(0) <= 0).to_numpy()
    midday = (frame["irradiance_w_m2"].fillna(0) > 500).to_numpy()

    # Invalid physical values.
    for idx in _pick(rng, night, 2):
        frame.loc[idx, "irradiance_w_m2"] = -5.0
    for idx in _pick(rng, night, 1):
        frame.loc[idx, "active_power_kw"] = -0.8
    for idx in _pick(rng, night, 1):
        frame.loc[idx, "active_power_kw"] = -0.05  # suspicious, not invalid
    for idx in _pick(rng, midday, 1):
        frame.loc[idx, "module_temperature_c"] = 150.0
    for idx in _pick(rng, midday, 1):
        frame.loc[idx, "module_temperature_c"] = 97.0
    for idx in _pick(rng, daylight, 1):
        frame.loc[idx, "ambient_temperature_c"] = -70.0
    for idx in _pick(rng, daylight, 1):
        frame.loc[idx, "ambient_temperature_c"] = 55.0
    for idx in _pick(rng, midday, 1):
        frame.loc[idx, "irradiance_w_m2"] = 1400.0
    for idx in _pick(rng, midday, 1):
        frame.loc[idx, "irradiance_w_m2"] = 1800.0
    for idx in _pick(rng, midday, 1):
        frame.loc[idx, "active_power_kw"] = spec.capacity_kw * 1.5
    for idx in _pick(rng, midday, 1):
        frame.loc[idx, "active_power_kw"] = spec.capacity_kw * 4.0

    # Unparseable numeric values.
    for column, text in (
        ("active_power_kw", "n/a"),
        ("module_temperature_c", "sensor-error"),
    ):
        idx = int(_pick(rng, daylight, 1)[0]) if daylight.any() else n // 2
        frame.loc[idx, column] = np.nan
        malformed.append((idx, column, text))

    # Irregular sampling: shift a short block by seven minutes.
    block_start = int(n * 0.35)
    for offset in range(4):
        idx = block_start + offset
        if idx < n and pd.notna(frame.loc[idx, "timestamp"]):
            frame.loc[idx, "timestamp"] = frame.loc[idx, "timestamp"] + pd.Timedelta(
                minutes=7
            )

    # Duplicate rows inside the file (raw duplicates are preserved).
    duplicate_idx = rng.choice(n, size=3, replace=False)
    duplicates = frame.loc[duplicate_idx].copy()

    if add_cross_file_duplicate:
        overlap = frame.iloc[[0]].copy()
        overlap.loc[:, "timestamp"] = pd.Timestamp("2023-03-31T23:45:00Z")
        frame = pd.concat([overlap, frame], ignore_index=True)

    frame = pd.concat([frame, duplicates], ignore_index=True)

    # A row with an unparseable timestamp is kept by the ingestion layer as a rejected
    # record instead of being silently discarded.
    bad = frame.iloc[[10]].copy()
    bad.loc[:, "timestamp"] = pd.NaT
    bad.loc[:, "active_power_kw"] = 1.0
    frame = pd.concat([frame, bad], ignore_index=True)

    return frame.reset_index(drop=True), malformed


def _format_timestamp(value: pd.Timestamp, row: int) -> str:
    if pd.isna(value):
        return "not-a-timestamp"
    if row % 997 == 0 and value.month not in (3, 10):
        # German-style local time without an offset. Interpreted as Europe/Berlin.
        return value.tz_convert(LOCAL_TZ).strftime("%d.%m.%Y %H:%M:%S")
    if row % 1499 == 0:
        return value.tz_convert(LOCAL_TZ).isoformat()
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_csv(
    frame: pd.DataFrame, path: Path, malformed: list[tuple[int, str, str]]
) -> None:
    output = pd.DataFrame(
        {
            "installation_id": frame["installation_id"].astype(str),
            "sensor_id": frame["sensor_id"].astype(str),
        }
    )
    output.insert(
        0,
        "timestamp",
        [_format_timestamp(value, i) for i, value in enumerate(frame["timestamp"])],
    )
    formats = {
        "active_power_kw": "{:.3f}",
        "irradiance_w_m2": "{:.1f}",
        "module_temperature_c": "{:.2f}",
        "ambient_temperature_c": "{:.2f}",
    }
    for column, fmt in formats.items():
        output[column] = [
            "" if pd.isna(value) else fmt.format(value)
            for value in frame[column]
        ]
    for index, column, text in malformed:
        output.loc[index, column] = text
    output = output[COLUMNS]
    output.to_csv(path, index=False, encoding="utf-8", lineterminator="\n")


def generate(output_dir: Path, copy_to_raw: bool) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = PROJECT_ROOT / "data" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for spec_index, spec in enumerate(INSTALLATIONS):
        start = pd.Timestamp(spec.start, tz="UTC")
        end = pd.Timestamp(spec.end, tz="UTC")
        # Split the long series into quarterly files to exercise multi-file ingestion.
        # The half-year installation stays in one file.
        if spec.installation_id == "INST_C":
            boundaries = pd.DatetimeIndex([start, end])
        else:
            boundaries = pd.date_range(start, end, freq="QS", tz="UTC")
            if boundaries.empty or boundaries[0] != start:
                boundaries = boundaries.insert(0, start)
            boundaries = boundaries.append(pd.DatetimeIndex([end]))
        for segment_index, (segment_start, segment_end) in enumerate(
            zip(boundaries[:-1], boundaries[1:])
        ):
            if segment_start >= segment_end:
                continue
            seed = 1000 * (spec_index + 1) + segment_index + 1
            frame = _base_frame(spec, segment_start, segment_end, seed)
            rng = np.random.default_rng(seed + 99)
            frame, malformed = _inject_quality_problems(
                frame,
                spec,
                rng,
                drop_block_rows=12 if segment_index == 1 else 0,
                add_cross_file_duplicate=(
                    spec.installation_id == "INST_A" and segment_index == 1
                ),
            )
            if spec.installation_id == "INST_C":
                suffix = "2024_h1"
            else:
                suffix = f"{segment_start.year}_q{segment_index + 1}"
            filename = f"synthetic_{spec.installation_id.lower()}_{suffix}.csv"
            path = output_dir / filename
            _write_csv(frame, path, malformed)
            written.append(path)
            if copy_to_raw:
                shutil.copyfile(path, raw_dir / filename)

    readme = output_dir / "README.md"
    readme.write_text(
        "# Synthetic example data\n\n"
        "All CSV files here are **synthetic**. They were generated by\n"
        "`scripts/generate_synthetic_data.py` with fixed seeds and contain deliberate\n"
        "quality defects. They are not real measurements and must not be presented as\n"
        "research results.\n",
        encoding="utf-8",
    )
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data" / "synthetic",
        help="Directory for generated CSV files",
    )
    parser.add_argument(
        "--no-copy-to-raw",
        action="store_true",
        help="Do not copy the generated files into data/raw",
    )
    args = parser.parse_args()
    files = generate(args.output, copy_to_raw=not args.no_copy_to_raw)
    for path in files:
        print(f"wrote {path}")
    print(
        "\nAll generated files are SYNTHETIC example data, not real measurements."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
