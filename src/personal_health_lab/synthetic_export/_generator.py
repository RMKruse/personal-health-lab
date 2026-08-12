from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from html import escape
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
from zoneinfo import ZoneInfo

_EXPORT_NAME = "apple-health-export.zip"
_METADATA_NAME = "scenario-metadata.json"
_CHECKSUMS_NAME = "checksums.sha256"
_EXPORT_MEMBER = "apple_health_export/export.xml"
_STORE_MARKER = "metadata.sqlite3"
_START_DATE = date(2024, 1, 1)
_DAY_COUNT = 365
_SOURCE_NAME = "HealthLab Synthetic Apple Watch"
_SOURCE_VERSION = "1.0"
_ACTIVE_ENERGY_TYPE = "HKQuantityTypeIdentifierActiveEnergyBurned"
_RESTING_HEART_RATE_TYPE = "HKQuantityTypeIdentifierRestingHeartRate"
_DEVICE = (
    "<<HKDevice: synthetic>, name:HealthLab Synthetic Apple Watch, "
    "manufacturer:HealthLab, model:Synthetic Watch, hardware:1, software:1.0>"
)


class ScenarioId(StrEnum):
    """Stable identifiers for built-in synthetic scenario definitions."""

    LAG_SIGNAL_V1 = "lag-signal-v1"
    NULL_V1 = "null-v1"


@dataclass(frozen=True, slots=True)
class GeneratedFixture:
    """Paths and identity of one generated synthetic Health export fixture."""

    scenario_id: ScenarioId
    seed: int
    export_path: Path
    metadata_path: Path
    checksums_path: Path


@dataclass(frozen=True, slots=True)
class GenerationOptions:
    """Versioned stochastic settings for a synthetic scenario instance."""

    active_energy_noise_standard_deviation: float = 55.0
    resting_heart_rate_noise_standard_deviation: float = 0.7
    missing_active_energy_probability: float = 0.0
    missing_resting_heart_rate_probability: float = 0.0

    def __post_init__(self) -> None:
        noise_values = (
            self.active_energy_noise_standard_deviation,
            self.resting_heart_rate_noise_standard_deviation,
        )
        if any(not math.isfinite(value) or value < 0 for value in noise_values):
            raise ValueError("Rausch-Standardabweichungen müssen endlich und nicht negativ sein")
        probabilities = (
            self.missing_active_energy_probability,
            self.missing_resting_heart_rate_probability,
        )
        if any(not math.isfinite(value) or not 0.0 <= value < 1.0 for value in probabilities):
            raise ValueError(
                "Missingness-Wahrscheinlichkeiten müssen zwischen 0 und kleiner als 1 liegen"
            )


@dataclass(frozen=True, slots=True)
class _ScenarioDefinition:
    signal_description: str
    expected_lag_days: int | None
    association_bpm_per_kcal: float


_SCENARIO_DEFINITIONS = {
    ScenarioId.LAG_SIGNAL_V1: _ScenarioDefinition(
        signal_description=(
            "Negative Assoziation der aktiven Energie mit dem Apple-Ruhepuls "
            "des folgenden messlokalen Tages."
        ),
        expected_lag_days=1,
        association_bpm_per_kcal=-0.009,
    ),
    ScenarioId.NULL_V1: _ScenarioDefinition(
        signal_description="Kein eingebautes Verzögerungssignal.",
        expected_lag_days=None,
        association_bpm_per_kcal=0.0,
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_inside_healthlab_store(destination: Path) -> bool:
    resolved = destination.expanduser().resolve(strict=False)
    return any((candidate / _STORE_MARKER).is_file() for candidate in (resolved, *resolved.parents))


def _write_reproducible_zip(path: Path, export_xml: bytes) -> None:
    member = ZipInfo(_EXPORT_MEMBER, date_time=(1980, 1, 1, 0, 0, 0))
    member.compress_type = ZIP_DEFLATED
    member.create_system = 3
    member.external_attr = 0o100644 << 16
    with ZipFile(path, "w") as archive:
        archive.writestr(member, export_xml, compresslevel=9)


def _stable_rng(seed: int, stream: str) -> random.Random:
    material = f"healthlab-synthetic-v1:{seed}:{stream}".encode()
    return random.Random(int.from_bytes(hashlib.sha256(material).digest()))


def _timezone_for(day: date) -> ZoneInfo:
    if date(2024, 5, 6) <= day <= date(2024, 5, 12):
        return ZoneInfo("America/New_York")
    if date(2024, 9, 16) <= day <= date(2024, 9, 22):
        return ZoneInfo("Asia/Tokyo")
    return ZoneInfo("Europe/Berlin")


def _health_date(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S %z")


def _xml_tag(name: str, attributes: tuple[tuple[str, str], ...], indent: int = 2) -> str:
    serialized = " ".join(f'{key}="{escape(value, quote=True)}"' for key, value in attributes)
    return f"{' ' * indent}<{name} {serialized}/>\n"


def _record(
    data_type: str,
    unit: str,
    value: float,
    start: datetime,
    end: datetime,
) -> str:
    return _xml_tag(
        "Record",
        (
            ("type", data_type),
            ("sourceName", _SOURCE_NAME),
            ("sourceVersion", _SOURCE_VERSION),
            ("device", _DEVICE),
            ("unit", unit),
            ("creationDate", _health_date(end)),
            ("startDate", _health_date(start)),
            ("endDate", _health_date(end)),
            ("value", f"{value:.2f}"),
        ),
    )


def _daily_active_energy(seed: int, noise_standard_deviation: float) -> list[float]:
    rng = _stable_rng(seed, "active-energy")
    totals: list[float] = []
    for day_index in range(_DAY_COUNT):
        weekly_pattern = 155.0 * math.sin(2.0 * math.pi * day_index / 7.0)
        seasonal_pattern = 45.0 * math.sin(2.0 * math.pi * day_index / 365.0)
        totals.append(
            max(
                80.0,
                520.0 + weekly_pattern + seasonal_pattern + rng.gauss(0, noise_standard_deviation),
            )
        )
    return totals


def _resting_heart_rates(
    definition: _ScenarioDefinition,
    seed: int,
    active_energy: list[float],
    noise_standard_deviation: float,
) -> list[float]:
    rng = _stable_rng(seed, "resting-heart-rate")
    values: list[float] = []
    for day_index in range(_DAY_COUNT):
        lag_association = (
            definition.association_bpm_per_kcal * (active_energy[day_index - 1] - 520.0)
            if day_index > 0
            else 0.0
        )
        values.append(62.0 + lag_association + rng.gauss(0, noise_standard_deviation))
    return values


def _missing_days(seed: int, stream: str, probability: float) -> set[int]:
    missing_count = int(probability * _DAY_COUNT)
    return set(_stable_rng(seed, stream).sample(range(_DAY_COUNT), missing_count))


def _scenario_metadata(
    definition: _ScenarioDefinition,
    options: GenerationOptions,
    active_sample_count: int,
    resting_sample_count: int,
) -> dict[str, object]:
    end_date = _START_DATE + timedelta(days=_DAY_COUNT - 1)
    return {
        "date_range": {
            "measurement_local_start": _START_DATE.isoformat(),
            "measurement_local_end": end_date.isoformat(),
            "measurement_local_days": _DAY_COUNT,
        },
        "healthkit_data_types": {
            _ACTIVE_ENERGY_TYPE: {"samples": active_sample_count, "unit": "kcal"},
            _RESTING_HEART_RATE_TYPE: {"samples": resting_sample_count, "unit": "count/min"},
        },
        "generation_options": {
            "active_energy_noise_standard_deviation": (
                options.active_energy_noise_standard_deviation
            ),
            "missing_active_energy_probability": options.missing_active_energy_probability,
            "missing_resting_heart_rate_probability": (
                options.missing_resting_heart_rate_probability
            ),
            "resting_heart_rate_noise_standard_deviation": (
                options.resting_heart_rate_noise_standard_deviation
            ),
        },
        "realized_missingness": {
            "active_energy_missing_days": _DAY_COUNT - active_sample_count // 4,
            "resting_heart_rate_missing_days": _DAY_COUNT - resting_sample_count,
        },
        "signal": {
            "description": definition.signal_description,
            "expected_lag_days": definition.expected_lag_days,
            "expected_association_bpm_per_100_kcal": (definition.association_bpm_per_kcal * 100.0),
        },
        "time_fixtures": {
            "base_timezone": "Europe/Berlin",
            "dst_dates": ["2024-03-31", "2024-10-27"],
            "travel_periods": [
                {"start": "2024-05-06", "end": "2024-05-12", "timezone": "America/New_York"},
                {"start": "2024-09-16", "end": "2024-09-22", "timezone": "Asia/Tokyo"},
            ],
        },
    }


def _scenario_export(
    definition: _ScenarioDefinition,
    seed: int,
    options: GenerationOptions,
) -> tuple[bytes, dict[str, object]]:
    active_energy = _daily_active_energy(
        seed,
        options.active_energy_noise_standard_deviation,
    )
    resting_heart_rates = _resting_heart_rates(
        definition,
        seed,
        active_energy,
        options.resting_heart_rate_noise_standard_deviation,
    )
    missing_active_days = _missing_days(
        seed,
        "missing-active-energy",
        options.missing_active_energy_probability,
    )
    missing_resting_days = _missing_days(
        seed,
        "missing-resting-heart-rate",
        options.missing_resting_heart_rate_probability,
    )
    records: list[str] = []
    active_sample_count = 0
    resting_sample_count = 0
    sample_hours = (8, 12, 17, 20)
    shares = (0.15, 0.25, 0.40, 0.20)

    for day_index, daily_total in enumerate(active_energy):
        local_day = _START_DATE + timedelta(days=day_index)
        timezone = _timezone_for(local_day)
        active_present = day_index not in missing_active_days
        resting_present = day_index not in missing_resting_days

        if active_present:
            for hour, share in zip(sample_hours, shares, strict=True):
                start = datetime.combine(local_day, time(hour=hour), timezone)
                records.append(
                    _record(
                        _ACTIVE_ENERGY_TYPE,
                        "kcal",
                        daily_total * share,
                        start,
                        start + timedelta(minutes=30),
                    )
                )
                active_sample_count += 1
        if resting_present:
            resting_start = datetime.combine(local_day, time(hour=7), timezone)
            records.append(
                _record(
                    _RESTING_HEART_RATE_TYPE,
                    "count/min",
                    resting_heart_rates[day_index],
                    resting_start,
                    resting_start + timedelta(minutes=1),
                )
            )
            resting_sample_count += 1

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<HealthData locale="de_DE">\n'
        '  <ExportDate value="2024-12-30 23:59:59 +0100"/>\n'
        '  <Me HKCharacteristicTypeIdentifierDateOfBirth="1990-01-01" '
        'HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexNotSet"/>\n'
        + "".join(records)
        + "</HealthData>\n"
    ).encode()
    return xml, _scenario_metadata(
        definition,
        options,
        active_sample_count,
        resting_sample_count,
    )


def generate_export(
    scenario_id: ScenarioId | str,
    seed: int,
    destination: Path,
    *,
    options: GenerationOptions | None = None,
) -> GeneratedFixture:
    """Generate a deterministic, repository-safe synthetic Health export fixture."""

    try:
        normalized_scenario_id = ScenarioId(scenario_id)
    except ValueError as error:
        raise ValueError(f"Unbekanntes synthetisches Szenario: {scenario_id}") from error
    if not isinstance(seed, int):
        raise TypeError("seed muss eine Ganzzahl sein")
    if _is_inside_healthlab_store(destination):
        raise ValueError("Synthetische Exporte dürfen nicht in einen HealthLab-Datenspeicher.")

    destination.mkdir(parents=True, exist_ok=True)
    export_path = destination / _EXPORT_NAME
    metadata_path = destination / _METADATA_NAME
    checksums_path = destination / _CHECKSUMS_NAME

    effective_options = options or GenerationOptions()
    definition = _SCENARIO_DEFINITIONS[normalized_scenario_id]
    export_xml, scenario_metadata = _scenario_export(definition, seed, effective_options)
    _write_reproducible_zip(export_path, export_xml)
    metadata: dict[str, object] = {
        "generator": "personal-health-lab",
        "scenario_id": normalized_scenario_id,
        "schema_version": "1.0",
        "seed": seed,
        "synthetic": True,
        **scenario_metadata,
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksums_path.write_text(
        f"{_sha256(export_path)}  {_EXPORT_NAME}\n{_sha256(metadata_path)}  {_METADATA_NAME}\n",
        encoding="ascii",
    )
    return GeneratedFixture(
        scenario_id=normalized_scenario_id,
        seed=seed,
        export_path=export_path,
        metadata_path=metadata_path,
        checksums_path=checksums_path,
    )
