"""Apple Health import behind one package-level operation."""

from __future__ import annotations

import errno
import hashlib
import json
import shutil
import stat
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast
from uuid import uuid4
from xml.etree.ElementTree import ParseError, iterparse
from zipfile import BadZipFile, ZipFile, is_zipfile

from personal_health_lab.data_quality import resolve_sources, select_governing_export
from personal_health_lab.health_data import (
    CanonicalHealthRecord,
    CanonicalHealthType,
    CanonicalSleepCategory,
    CanonicalSleepInterval,
    CanonicalUnit,
    CanonicalWorkout,
    HealthProvenance,
    LogicalMeasurementId,
    MeasurementVersionId,
)
from personal_health_lab.recovery import (
    RestoreSourceInspection,
    inspect_restore_sources,
    load_restore_source_packages,
    load_restore_working_copy,
    preflight_restore_source_import,
    restore_source_resolver,
    select_restore_source_versions,
    stage_restore_source_package,
)
from personal_health_lab.storage import (
    CapacityCheck,
    ExportFact,
    ImportId,
    LocalStore,
    OperationId,
    SnapshotId,
    StoreError,
    UnsupportedContentCategory,
    UnsupportedImportContent,
)

_EXPORT_MEMBER = "apple_health_export/export.xml"
_ACTIVE_ENERGY = "HKQuantityTypeIdentifierActiveEnergyBurned"
_APPLE_EXERCISE_TIME = "HKQuantityTypeIdentifierAppleExerciseTime"
_RESTING_HEART_RATE = "HKQuantityTypeIdentifierRestingHeartRate"
_BODY_MASS = "HKQuantityTypeIdentifierBodyMass"
_STEP_COUNT = "HKQuantityTypeIdentifierStepCount"
_WALKING_RUNNING_DISTANCE = "HKQuantityTypeIdentifierDistanceWalkingRunning"
_LOGICAL_IDENTITY_RULE_VERSION = "healthkit-natural/v2"
_PAYLOAD_IDENTITY_RULE_VERSION = "healthkit-payload/v2"
_SYNC_IDENTIFIER = "HKMetadataKeySyncIdentifier"
_MAPPINGS: dict[str, tuple[CanonicalHealthType, CanonicalUnit, dict[str, float]]] = {
    _ACTIVE_ENERGY: (
        CanonicalHealthType.ACTIVE_ENERGY,
        CanonicalUnit.KILOCALORIE,
        {"kcal": 1.0},
    ),
    _APPLE_EXERCISE_TIME: (
        CanonicalHealthType.APPLE_EXERCISE_TIME,
        CanonicalUnit.MINUTE,
        {"min": 1.0, "s": 1 / 60},
    ),
    _RESTING_HEART_RATE: (
        CanonicalHealthType.APPLE_RESTING_HEART_RATE,
        CanonicalUnit.BEATS_PER_MINUTE,
        {"count/min": 1.0},
    ),
    _BODY_MASS: (
        CanonicalHealthType.BODY_MASS,
        CanonicalUnit.KILOGRAM,
        {"kg": 1.0, "g": 0.001, "lb": 0.45359237},
    ),
    _STEP_COUNT: (CanonicalHealthType.STEP_COUNT, CanonicalUnit.COUNT, {"count": 1.0}),
    _WALKING_RUNNING_DISTANCE: (
        CanonicalHealthType.WALKING_RUNNING_DISTANCE,
        CanonicalUnit.KILOMETER,
        {"m": 0.001, "km": 1.0, "mi": 1.609344},
    ),
}
_SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
_SLEEP_VALUES = {
    "HKCategoryValueSleepAnalysisInBed",
    "HKCategoryValueSleepAnalysisAwake",
    "HKCategoryValueSleepAnalysisAsleep",
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepREM",
}
_SLEEP_CATEGORIES = {
    "HKCategoryValueSleepAnalysisInBed": CanonicalSleepCategory.IN_BED,
    "HKCategoryValueSleepAnalysisAwake": CanonicalSleepCategory.AWAKE,
    "HKCategoryValueSleepAnalysisAsleep": CanonicalSleepCategory.ASLEEP_UNSPECIFIED,
    "HKCategoryValueSleepAnalysisAsleepUnspecified": CanonicalSleepCategory.ASLEEP_UNSPECIFIED,
    "HKCategoryValueSleepAnalysisAsleepCore": CanonicalSleepCategory.ASLEEP_CORE,
    "HKCategoryValueSleepAnalysisAsleepDeep": CanonicalSleepCategory.ASLEEP_DEEP,
    "HKCategoryValueSleepAnalysisAsleepREM": CanonicalSleepCategory.ASLEEP_REM,
}
_DIETARY_TYPES = {
    "HKQuantityTypeIdentifierDietaryBiotin": CanonicalHealthType.DIETARY_BIOTIN,
    "HKQuantityTypeIdentifierDietaryCaffeine": CanonicalHealthType.DIETARY_CAFFEINE,
    "HKQuantityTypeIdentifierDietaryCalcium": CanonicalHealthType.DIETARY_CALCIUM,
    "HKQuantityTypeIdentifierDietaryCarbohydrates": CanonicalHealthType.DIETARY_CARBOHYDRATES,
    "HKQuantityTypeIdentifierDietaryChloride": CanonicalHealthType.DIETARY_CHLORIDE,
    "HKQuantityTypeIdentifierDietaryCholesterol": CanonicalHealthType.DIETARY_CHOLESTEROL,
    "HKQuantityTypeIdentifierDietaryChromium": CanonicalHealthType.DIETARY_CHROMIUM,
    "HKQuantityTypeIdentifierDietaryCopper": CanonicalHealthType.DIETARY_COPPER,
    "HKQuantityTypeIdentifierDietaryEnergyConsumed": (CanonicalHealthType.DIETARY_ENERGY_CONSUMED),
    "HKQuantityTypeIdentifierDietaryFatMonounsaturated": (
        CanonicalHealthType.DIETARY_FAT_MONOUNSATURATED
    ),
    "HKQuantityTypeIdentifierDietaryFatPolyunsaturated": (
        CanonicalHealthType.DIETARY_FAT_POLYUNSATURATED
    ),
    "HKQuantityTypeIdentifierDietaryFatSaturated": CanonicalHealthType.DIETARY_FAT_SATURATED,
    "HKQuantityTypeIdentifierDietaryFatTotal": CanonicalHealthType.DIETARY_FAT_TOTAL,
    "HKQuantityTypeIdentifierDietaryFiber": CanonicalHealthType.DIETARY_FIBER,
    "HKQuantityTypeIdentifierDietaryFolate": CanonicalHealthType.DIETARY_FOLATE,
    "HKQuantityTypeIdentifierDietaryIodine": CanonicalHealthType.DIETARY_IODINE,
    "HKQuantityTypeIdentifierDietaryIron": CanonicalHealthType.DIETARY_IRON,
    "HKQuantityTypeIdentifierDietaryMagnesium": CanonicalHealthType.DIETARY_MAGNESIUM,
    "HKQuantityTypeIdentifierDietaryManganese": CanonicalHealthType.DIETARY_MANGANESE,
    "HKQuantityTypeIdentifierDietaryMolybdenum": CanonicalHealthType.DIETARY_MOLYBDENUM,
    "HKQuantityTypeIdentifierDietaryNiacin": CanonicalHealthType.DIETARY_NIACIN,
    "HKQuantityTypeIdentifierDietaryPantothenicAcid": (
        CanonicalHealthType.DIETARY_PANTOTHENIC_ACID
    ),
    "HKQuantityTypeIdentifierDietaryPhosphorus": CanonicalHealthType.DIETARY_PHOSPHORUS,
    "HKQuantityTypeIdentifierDietaryPotassium": CanonicalHealthType.DIETARY_POTASSIUM,
    "HKQuantityTypeIdentifierDietaryProtein": CanonicalHealthType.DIETARY_PROTEIN,
    "HKQuantityTypeIdentifierDietaryRiboflavin": CanonicalHealthType.DIETARY_RIBOFLAVIN,
    "HKQuantityTypeIdentifierDietarySelenium": CanonicalHealthType.DIETARY_SELENIUM,
    "HKQuantityTypeIdentifierDietarySodium": CanonicalHealthType.DIETARY_SODIUM,
    "HKQuantityTypeIdentifierDietarySugar": CanonicalHealthType.DIETARY_SUGAR,
    "HKQuantityTypeIdentifierDietaryThiamin": CanonicalHealthType.DIETARY_THIAMIN,
    "HKQuantityTypeIdentifierDietaryVitaminA": CanonicalHealthType.DIETARY_VITAMIN_A,
    "HKQuantityTypeIdentifierDietaryVitaminB12": CanonicalHealthType.DIETARY_VITAMIN_B12,
    "HKQuantityTypeIdentifierDietaryVitaminB6": CanonicalHealthType.DIETARY_VITAMIN_B6,
    "HKQuantityTypeIdentifierDietaryVitaminC": CanonicalHealthType.DIETARY_VITAMIN_C,
    "HKQuantityTypeIdentifierDietaryVitaminD": CanonicalHealthType.DIETARY_VITAMIN_D,
    "HKQuantityTypeIdentifierDietaryVitaminE": CanonicalHealthType.DIETARY_VITAMIN_E,
    "HKQuantityTypeIdentifierDietaryVitaminK": CanonicalHealthType.DIETARY_VITAMIN_K,
    "HKQuantityTypeIdentifierDietaryWater": CanonicalHealthType.DIETARY_WATER,
    "HKQuantityTypeIdentifierDietaryZinc": CanonicalHealthType.DIETARY_ZINC,
}
_V03_UNITS = {
    "HKQuantityTypeIdentifierBodyMass": frozenset({"kg", "g", "lb"}),
    _APPLE_EXERCISE_TIME: frozenset({"min", "s"}),
    _STEP_COUNT: frozenset({"count"}),
    _WALKING_RUNNING_DISTANCE: frozenset({"m", "km", "mi"}),
    _ACTIVE_ENERGY: frozenset({"kcal"}),
    _RESTING_HEART_RATE: frozenset({"count/min"}),
    **{
        source_type: (
            frozenset({"kcal", "kJ"})
            if source_type.endswith("EnergyConsumed")
            else frozenset({"mL", "L"})
            if source_type.endswith("Water")
            else frozenset({"mcg", "mg", "g"})
        )
        for source_type in _DIETARY_TYPES
    },
}
_MAPPINGS.update(
    {
        source_type: (
            data_type,
            CanonicalUnit.KILOCALORIE,
            {"kcal": 1.0, "kJ": 1 / 4.184},
        )
        if data_type is CanonicalHealthType.DIETARY_ENERGY_CONSUMED
        else (
            data_type,
            CanonicalUnit.MILLILITER,
            {"mL": 1.0, "L": 1000.0},
        )
        if data_type is CanonicalHealthType.DIETARY_WATER
        else (
            data_type,
            CanonicalUnit.GRAM,
            {"mcg": 0.000001, "mg": 0.001, "g": 1.0},
        )
        for source_type, data_type in _DIETARY_TYPES.items()
    }
)


class HealthImportError(Exception):
    """The import could not complete because its internal store failed."""


class _RejectedPackage(ValueError):
    """The package boundary is unsafe or unsupported."""


@dataclass(frozen=True, slots=True)
class HealthImportResult:
    operation_id: OperationId
    import_id: ImportId
    status: Literal["committed", "duplicate", "quarantined", "rejected", "restore_pending"]
    package_hash: str
    snapshot_id: SnapshotId | None
    record_count: int
    package_record_count: int = 0
    logical_measurement_count: int = 0
    measurement_version_count: int = 0
    source_occurrence_count: int = 0
    anomaly_count: int = 0
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HealthExportEstimate:
    input_bytes: int
    record_count: int


@dataclass(frozen=True, slots=True)
class RestoreHealthExportInspection:
    estimate: HealthExportEstimate
    sources: RestoreSourceInspection
    capacity: CapacityCheck


@dataclass(frozen=True, slots=True)
class _ParsedExport:
    export_id: str
    export_date: datetime | None
    records: tuple[CanonicalHealthRecord, ...]
    sleep_intervals: tuple[CanonicalSleepInterval, ...] = ()
    workouts: tuple[CanonicalWorkout, ...] = ()
    unknown_source_types: tuple[str, ...] = ()
    unsupported_content: tuple[UnsupportedImportContent, ...] = ()


def _source_datetime(value: str) -> datetime:
    timestamp = datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
    if timestamp.tzinfo is None:
        raise ValueError("missing timezone")
    return timestamp


def _id(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()


def _measurement_version_ids(
    logical_id: LogicalMeasurementId,
    *,
    data_type: CanonicalHealthType,
    source_unit: str,
    original_value: float,
    source_start: datetime,
    source_end: datetime,
    source_updated_at: datetime,
    source_name: str,
    source_version: str,
    device: str,
) -> tuple[MeasurementVersionId, MeasurementVersionId]:
    legacy_payload = _id(
        data_type.value,
        source_unit,
        original_value,
        source_start.isoformat(),
        source_end.isoformat(),
        source_name,
        device,
    )
    current_payload = _id(
        _PAYLOAD_IDENTITY_RULE_VERSION,
        data_type.value,
        source_unit,
        original_value,
        source_start.isoformat(),
        source_end.isoformat(),
        source_updated_at.isoformat(),
        source_name,
        source_version,
        device,
    )
    return (
        MeasurementVersionId(_id(_PAYLOAD_IDENTITY_RULE_VERSION, logical_id, current_payload)),
        MeasurementVersionId(_id(_LOGICAL_IDENTITY_RULE_VERSION, logical_id, legacy_payload)),
    )


def _records(
    package_path: Path,
    *,
    max_package_bytes: int,
    max_entries: int,
    max_entry_bytes: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: float,
) -> _ParsedExport:
    if not package_path.is_file() or not is_zipfile(package_path):
        raise _RejectedPackage("invalid zip")
    if package_path.stat().st_size > max_package_bytes:
        raise _RejectedPackage("package too large")
    records: list[CanonicalHealthRecord] = []
    sleep_intervals: list[CanonicalSleepInterval] = []
    workouts: list[CanonicalWorkout] = []
    unknown_source_types: set[str] = set()
    unsupported: Counter[tuple[str, str]] = Counter()
    with ZipFile(package_path) as archive:
        entries = archive.infolist()
        if len(entries) > max_entries:
            raise _RejectedPackage("too many entries")
        total_size = 0
        export_count = 0
        for entry in entries:
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            total_size += entry.file_size
            if (
                not entry.filename
                or "\\" in entry.filename
                or path.is_absolute()
                or ".." in path.parts
                or entry.flag_bits & 1
                or stat.S_ISLNK(mode)
                or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                or entry.file_size > max_entry_bytes
                or total_size > max_uncompressed_bytes
                or entry.file_size / max(entry.compress_size, 1) > max_compression_ratio
            ):
                raise _RejectedPackage("unsafe archive entry")
            if entry.is_dir():
                continue
            if entry.filename != _EXPORT_MEMBER:
                raise _RejectedPackage("unsupported archive entry")
            export_count += 1
        if export_count != 1:
            raise _RejectedPackage("missing or duplicate export.xml")
        export_digest = hashlib.sha256()
        with archive.open(_EXPORT_MEMBER) as source:
            tail = b""
            while chunk := source.read(64 * 1024):
                export_digest.update(chunk)
                probe = (tail + chunk).upper()
                if b"\0" in probe or b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
                    raise _RejectedPackage("unsafe xml declaration")
                tail = probe[-8:]
        with archive.open(_EXPORT_MEMBER) as source:
            root_seen = False
            export_date = None
            tags: list[str] = []
            for event, element in iterparse(source, events=("start", "end")):
                if event == "start":
                    if not root_seen and element.tag != "HealthData":
                        raise ValueError("invalid root")
                    root_seen = True
                    tags.append(element.tag)
                    continue
                parent = tags[-2] if len(tags) > 1 else None
                if parent == "HealthData" and element.tag == "ExportDate":
                    if export_date is not None:
                        raise ValueError("duplicate export date")
                    export_date = _source_datetime(element.attrib["value"])
                    element.clear()
                elif parent == "Workout" and element.tag != "MetadataEntry":
                    unsupported["workout_child", element.tag] += 1
                elif parent == "HealthData" and element.tag == "Workout":
                    attrs = element.attrib
                    if (
                        not {
                            "workoutActivityType",
                            "startDate",
                            "endDate",
                            "creationDate",
                            "sourceName",
                        }
                        <= attrs.keys()
                    ):
                        unsupported[
                            "workout_activity_type", attrs.get("workoutActivityType", "")
                        ] += 1
                        element.clear()
                        tags.pop()
                        continue
                    source_start = _source_datetime(element.attrib["startDate"])
                    source_end = _source_datetime(element.attrib["endDate"])
                    source_updated_at = _source_datetime(element.attrib["creationDate"])
                    source_name = element.attrib["sourceName"]
                    source_version = element.attrib.get("sourceVersion", "")
                    device = element.attrib.get("device", "")
                    activity_type = element.attrib.get("workoutActivityType", "")
                    if not activity_type:
                        unsupported["workout_activity_type", ""] += 1
                        element.clear()
                        tags.pop()
                        continue
                    sync_id = next(
                        (
                            child.attrib.get("value")
                            for child in element
                            if child.tag == "MetadataEntry"
                            and child.attrib.get("key") == _SYNC_IDENTIFIER
                            and child.attrib.get("value")
                        ),
                        None,
                    )
                    strong_source_id_hash = None if sync_id is None else _id(source_name, sync_id)
                    logical_id = LogicalMeasurementId(
                        _id(_LOGICAL_IDENTITY_RULE_VERSION, "strong", strong_source_id_hash)
                        if strong_source_id_hash is not None
                        else _id(
                            _LOGICAL_IDENTITY_RULE_VERSION,
                            "natural",
                            "Workout",
                            source_start.isoformat(),
                            source_end.isoformat(),
                            source_name,
                            device,
                        )
                    )

                    def optional_value(
                        attributes: dict[str, str],
                        name: str,
                        unit_name: str,
                        units: dict[str, float],
                    ) -> float | None:
                        raw = attributes.get(name)
                        if raw is None:
                            return None
                        unit = attributes.get(unit_name, "")
                        if unit not in units:
                            unsupported[
                                "unit", json.dumps(["Workout", unit], separators=(",", ":"))
                            ] += 1
                            return None
                        return float(raw) * units[unit]

                    duration = optional_value(
                        attrs, "duration", "durationUnit", {"min": 1.0, "s": 1 / 60}
                    )
                    distance = optional_value(
                        attrs,
                        "totalDistance",
                        "totalDistanceUnit",
                        {"m": 0.001, "km": 1.0, "mi": 1.609344},
                    )
                    energy = optional_value(
                        attrs, "totalEnergyBurned", "totalEnergyBurnedUnit", {"kcal": 1.0}
                    )
                    version_id = MeasurementVersionId(
                        _id(
                            _PAYLOAD_IDENTITY_RULE_VERSION,
                            logical_id,
                            activity_type,
                            duration,
                            distance,
                            energy,
                            source_start.isoformat(),
                            source_end.isoformat(),
                            source_updated_at.isoformat(),
                            source_name,
                            source_version,
                            device,
                        )
                    )
                    workouts.append(
                        CanonicalWorkout(
                            logical_workout_id=logical_id,
                            workout_version_id=version_id,
                            original_activity_type=activity_type,
                            source_start=source_start,
                            source_end=source_end,
                            source_updated_at=source_updated_at,
                            measurement_local_day=source_start.date(),
                            provenance=HealthProvenance(
                                source_name, source_version, device, 0.0, "", strong_source_id_hash
                            ),
                            reported_duration_minutes=duration,
                            distance_kilometers=distance,
                            active_energy_kilocalories=energy,
                        )
                    )
                    element.clear()
                elif parent == "HealthData" and element.tag == "Record":
                    source_type = element.attrib.get("type", "")
                    mapping = _MAPPINGS.get(source_type)
                    source_unit = element.attrib.get("unit", "")
                    if source_type == _SLEEP_TYPE:
                        original_category = element.attrib.get("value", "")
                        category = _SLEEP_CATEGORIES.get(original_category)
                        if category is None:
                            unsupported["sleep_value", original_category] += 1
                        elif source_unit:
                            unsupported[
                                "unit",
                                json.dumps([source_type, source_unit], separators=(",", ":")),
                            ] += 1
                        else:
                            source_start = _source_datetime(element.attrib["startDate"])
                            source_end = _source_datetime(element.attrib["endDate"])
                            source_updated_at = _source_datetime(element.attrib["creationDate"])
                            source_name = element.attrib["sourceName"]
                            source_version = element.attrib.get("sourceVersion", "")
                            device = element.attrib.get("device", "")
                            sync_id = next(
                                (
                                    child.attrib.get("value")
                                    for child in element
                                    if child.tag == "MetadataEntry"
                                    and child.attrib.get("key") == _SYNC_IDENTIFIER
                                    and child.attrib.get("value")
                                ),
                                None,
                            )
                            strong_source_id_hash = (
                                None if sync_id is None else _id(source_name, sync_id)
                            )
                            logical_id = LogicalMeasurementId(
                                _id(_LOGICAL_IDENTITY_RULE_VERSION, "strong", strong_source_id_hash)
                                if strong_source_id_hash is not None
                                else _id(
                                    _LOGICAL_IDENTITY_RULE_VERSION,
                                    "natural",
                                    _SLEEP_TYPE,
                                    source_start.isoformat(),
                                    source_end.isoformat(),
                                    source_name,
                                    device,
                                )
                            )
                            measurement_version_id = MeasurementVersionId(
                                _id(
                                    _PAYLOAD_IDENTITY_RULE_VERSION,
                                    logical_id,
                                    original_category,
                                    source_start.isoformat(),
                                    source_end.isoformat(),
                                    source_updated_at.isoformat(),
                                    source_name,
                                    source_version,
                                    device,
                                )
                            )
                            sleep_intervals.append(
                                CanonicalSleepInterval(
                                    logical_measurement_id=logical_id,
                                    measurement_version_id=measurement_version_id,
                                    original_category=original_category,
                                    canonical_category=category,
                                    source_start=source_start,
                                    source_end=source_end,
                                    source_updated_at=source_updated_at,
                                    source_name=source_name,
                                    source_version=source_version,
                                    device=device,
                                    strong_source_id_hash=strong_source_id_hash,
                                )
                            )
                    elif mapping is not None and source_unit in mapping[2]:
                        data_type, canonical_unit, conversions = mapping
                        original_value = (
                            1.0 if source_type == _SLEEP_TYPE else float(element.attrib["value"])
                        )
                        value = original_value * conversions[source_unit]
                        source_start = _source_datetime(element.attrib["startDate"])
                        source_end = _source_datetime(element.attrib["endDate"])
                        source_updated_at = _source_datetime(element.attrib["creationDate"])
                        source_name = element.attrib["sourceName"]
                        source_version = element.attrib.get("sourceVersion", "")
                        device = element.attrib.get("device", "")
                        sync_id = next(
                            (
                                child.attrib.get("value")
                                for child in element
                                if child.tag == "MetadataEntry"
                                and child.attrib.get("key") == _SYNC_IDENTIFIER
                                and child.attrib.get("value")
                            ),
                            None,
                        )
                        strong_source_id_hash = (
                            None if sync_id is None else _id(source_name, sync_id)
                        )
                        logical_id = LogicalMeasurementId(
                            _id(
                                _LOGICAL_IDENTITY_RULE_VERSION,
                                "strong",
                                strong_source_id_hash,
                            )
                            if strong_source_id_hash is not None
                            else _id(
                                _LOGICAL_IDENTITY_RULE_VERSION,
                                "natural",
                                source_type,
                                source_start.isoformat(),
                                source_end.isoformat(),
                                source_name,
                                device,
                            )
                        )
                        measurement_version_id, legacy_measurement_version_id = (
                            _measurement_version_ids(
                                logical_id,
                                data_type=data_type,
                                source_unit=source_unit,
                                original_value=original_value,
                                source_start=source_start,
                                source_end=source_end,
                                source_updated_at=source_updated_at,
                                source_name=source_name,
                                source_version=source_version,
                                device=device,
                            )
                        )
                        records.append(
                            CanonicalHealthRecord(
                                logical_measurement_id=logical_id,
                                measurement_version_id=measurement_version_id,
                                data_type=data_type,
                                unit=canonical_unit,
                                value=value,
                                source_start=source_start,
                                source_end=source_end,
                                source_updated_at=source_updated_at,
                                measurement_local_day=source_start.date(),
                                provenance=HealthProvenance(
                                    source_name=source_name,
                                    source_version=source_version,
                                    device=device,
                                    original_value=original_value,
                                    original_unit=source_unit,
                                    strong_source_id_hash=strong_source_id_hash,
                                ),
                                legacy_measurement_version_id=legacy_measurement_version_id,
                            )
                        )
                    elif source_type:
                        unknown_source_types.add(source_type)
                        if source_type in _V03_UNITS:
                            unit = element.attrib.get("unit", "")
                            if unit not in _V03_UNITS[source_type]:
                                unsupported[
                                    "unit",
                                    json.dumps([source_type, unit], separators=(",", ":")),
                                ] += 1
                        else:
                            unsupported["record_type", source_type] += 1
                    element.clear()
                elif parent == "HealthData":
                    unsupported["top_level_element", element.tag] += 1
                    element.clear()
                tags.pop()
    if (
        not records
        and not sleep_intervals
        and not workouts
        and not unknown_source_types
        and not unsupported
    ):
        raise ValueError("no supported records")
    return _ParsedExport(
        export_digest.hexdigest(),
        export_date,
        tuple(records),
        tuple(sleep_intervals),
        tuple(workouts),
        tuple(sorted(unknown_source_types)),
        tuple(
            UnsupportedImportContent(cast(UnsupportedContentCategory, category), identifier, count)
            for (category, identifier), count in sorted(unsupported.items())
            if identifier
        ),
    )


def estimate_health_export(
    package_path: Path,
    *,
    max_package_bytes: int,
    max_entries: int,
    max_entry_bytes: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: float,
) -> HealthExportEstimate:
    package_size = package_path.stat().st_size
    try:
        with ZipFile(package_path) as archive:
            input_bytes = archive.getinfo(_EXPORT_MEMBER).file_size
        parsed = _records(
            package_path,
            max_package_bytes=max_package_bytes,
            max_entries=max_entries,
            max_entry_bytes=max_entry_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
            max_compression_ratio=max_compression_ratio,
        )
        return HealthExportEstimate(
            max(input_bytes, package_size),
            len(parsed.records) + len(parsed.sleep_intervals) + len(parsed.workouts),
        )
    except (
        BadZipFile,
        KeyError,
        NotImplementedError,
        ParseError,
        RuntimeError,
        ValueError,
    ):
        return HealthExportEstimate(max(package_size, 1), 0)


def _restore_exports(
    package_path: Path,
    *,
    store: LocalStore,
    target_root: Path,
    max_package_bytes: int,
    max_entries: int,
    max_entry_bytes: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: float,
) -> tuple[
    tuple[_ParsedExport, ...],
    tuple[CanonicalHealthRecord, ...],
    tuple[CanonicalSleepInterval, ...],
    tuple[CanonicalWorkout, ...],
    int,
    tuple[tuple[str, datetime | None, str, tuple[CanonicalHealthRecord, ...]], ...],
]:
    paths = (*load_restore_source_packages(store, target_root), package_path)
    packages: dict[str, Path] = {}
    for path in paths:
        with path.open("rb") as package:
            packages[hashlib.file_digest(package, "sha256").hexdigest()] = path
    parsed_packages = tuple(
        (
            _records(
                path,
                max_package_bytes=max_package_bytes,
                max_entries=max_entries,
                max_entry_bytes=max_entry_bytes,
                max_uncompressed_bytes=max_uncompressed_bytes,
                max_compression_ratio=max_compression_ratio,
            ),
            package_hash,
        )
        for package_hash, path in packages.items()
    )
    packages_by_export: dict[str, tuple[_ParsedExport, str]] = {}
    for export, package_hash in parsed_packages:
        current = packages_by_export.get(export.export_id)
        if current is None or package_hash < current[1]:
            packages_by_export[export.export_id] = (export, package_hash)
    ordered_packages = tuple(
        sorted(
            packages_by_export.values(),
            key=lambda item: (
                item[0].export_date is not None,
                datetime.min.replace(tzinfo=UTC)
                if item[0].export_date is None
                else item[0].export_date,
                item[0].export_id,
            ),
            reverse=True,
        )
    )
    exports = tuple(item[0] for item in ordered_packages)
    records = {
        str(record.measurement_version_id): record
        for export in reversed(exports)
        for record in export.records
    }
    sleep_intervals = {
        str(interval.measurement_version_id): interval
        for export in reversed(exports)
        for interval in export.sleep_intervals
    }
    workouts = {
        str(workout.workout_version_id): workout
        for export in reversed(exports)
        for workout in export.workouts
    }
    return (
        exports,
        tuple(records.values()),
        tuple(sleep_intervals.values()),
        tuple(workouts.values()),
        sum(path.stat().st_size for path in packages.values()),
        tuple(
            (export.export_id, export.export_date, package_hash, export.records)
            for export, package_hash in ordered_packages
        ),
    )


def inspect_restore_health_export(
    package_path: Path,
    *,
    store: LocalStore,
    target_root: Path,
    max_package_bytes: int,
    max_entries: int,
    max_entry_bytes: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: float,
) -> RestoreHealthExportInspection:
    _, records, sleep_intervals, workouts, input_bytes, _ = _restore_exports(
        package_path,
        store=store,
        target_root=target_root,
        max_package_bytes=max_package_bytes,
        max_entries=max_entries,
        max_entry_bytes=max_entry_bytes,
        max_uncompressed_bytes=max_uncompressed_bytes,
        max_compression_ratio=max_compression_ratio,
    )
    sources = inspect_restore_sources(store, target_root, records)
    estimate = HealthExportEstimate(
        max(input_bytes, 1), len(records) + len(sleep_intervals) + len(workouts)
    )
    return RestoreHealthExportInspection(
        estimate,
        sources,
        preflight_restore_source_import(
            store,
            target_root,
            input_bytes=estimate.input_bytes,
            record_count=estimate.record_count,
            complete=sources.complete,
        ),
    )


def _import_restore_health_export(
    package_path: Path,
    *,
    store: LocalStore,
    target_root: Path,
    max_package_bytes: int,
    max_entries: int,
    max_entry_bytes: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: float,
) -> HealthImportResult:
    operation_id = OperationId(uuid4().hex)
    import_id = ImportId(uuid4().hex)
    snapshot_id = SnapshotId(uuid4().hex)
    package_hash = ""
    try:
        with package_path.open("rb") as package:
            package_hash = hashlib.file_digest(package, "sha256").hexdigest()
        current = _records(
            package_path,
            max_package_bytes=max_package_bytes,
            max_entries=max_entries,
            max_entry_bytes=max_entry_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
            max_compression_ratio=max_compression_ratio,
        )
        exports, records, sleep_intervals, workouts, _, restore_exports = _restore_exports(
            package_path,
            store=store,
            target_root=target_root,
            max_package_bytes=max_package_bytes,
            max_entries=max_entries,
            max_entry_bytes=max_entry_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
            max_compression_ratio=max_compression_ratio,
        )
        inspection = inspect_restore_sources(store, target_root, records)
    except (_RejectedPackage, OSError, BadZipFile, NotImplementedError):
        return HealthImportResult(
            operation_id,
            import_id,
            "rejected",
            package_hash,
            None,
            0,
            diagnostics=("invalid_health_export",),
        )
    except (KeyError, ParseError, RuntimeError, ValueError):
        return HealthImportResult(
            operation_id,
            import_id,
            "quarantined",
            package_hash,
            None,
            0,
            diagnostics=("invalid_health_data",),
        )

    stage_restore_source_package(store, target_root, package_path, package_hash)
    if not inspection.complete:
        return HealthImportResult(
            operation_id,
            import_id,
            "restore_pending",
            package_hash,
            None,
            0,
            package_record_count=len(current.records)
            + len(current.sleep_intervals)
            + len(current.workouts),
            logical_measurement_count=len(
                {str(record.logical_measurement_id) for record in records}
            ),
            measurement_version_count=len(records),
            diagnostics=("restore_sources_pending",),
        )

    selected_records = select_restore_source_versions(store, target_root, records)
    selected_version_ids = {
        str(original.measurement_version_id): selected.measurement_version_id
        for original, selected in zip(records, selected_records, strict=True)
    }
    records = selected_records
    restore_exports = tuple(
        (
            item_export_id,
            item_export_date,
            item_package_hash,
            tuple(
                replace(
                    record,
                    measurement_version_id=selected_version_ids.get(
                        str(record.measurement_version_id), record.measurement_version_id
                    ),
                )
                for record in item_records
            ),
        )
        for item_export_id, item_export_date, item_package_hash, item_records in restore_exports
    )

    session = store.load_restore_session()
    if session is None:
        raise HealthImportError("Wiederherstellungssitzung fehlt.")
    working = load_restore_working_copy(store, target_root)
    governing_export = exports[0]
    export_id = governing_export.export_id
    export_date = governing_export.export_date
    store.start_import(
        operation_id=operation_id,
        import_id=import_id,
        snapshot_id=snapshot_id,
    )
    try:
        published = store.publish_import(
            operation_id=operation_id,
            import_id=import_id,
            package_hash=package_hash,
            snapshot_id=snapshot_id,
            export_id=export_id,
            export_date=export_date,
            records=records,
            sleep_intervals=sleep_intervals,
            workouts=workouts,
            unknown_source_types=tuple(
                sorted(
                    {
                        source_type
                        for export in exports
                        for source_type in export.unknown_source_types
                    }
                )
            ),
            unsupported_content=current.unsupported_content,
            governing_export_id=export_id,
            resolve_sources=restore_source_resolver(working),
            restore_overlay=working,
            restore_overlay_sha256=session.working_copy_sha256,
            restore_exports=restore_exports,
        )
    except (OSError, StoreError):
        store.reject_import(import_id, package_hash)
        raise
    shutil.rmtree(working.parent / "sources", ignore_errors=True)
    return HealthImportResult(
        operation_id,
        import_id,
        published.status,
        package_hash,
        published.snapshot_id,
        published.record_count,
        package_record_count=len(current.records)
        + len(current.sleep_intervals)
        + len(current.workouts),
        logical_measurement_count=published.logical_measurement_count,
        measurement_version_count=published.measurement_version_count,
        source_occurrence_count=published.source_occurrence_count,
        anomaly_count=published.anomaly_count,
        diagnostics=published.diagnostics,
    )


def import_health_export(
    package_path: Path,
    *,
    store: LocalStore,
    max_package_bytes: int,
    max_entries: int,
    max_entry_bytes: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: float,
    target_root: Path | None = None,
) -> HealthImportResult:
    if store.load_restore_session() is not None:
        if target_root is None:
            raise HealthImportError("Wiederherstellungsziel fehlt.")
        return _import_restore_health_export(
            package_path,
            store=store,
            target_root=target_root,
            max_package_bytes=max_package_bytes,
            max_entries=max_entries,
            max_entry_bytes=max_entry_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
            max_compression_ratio=max_compression_ratio,
        )
    operation_id = OperationId(uuid4().hex)
    import_id = ImportId(uuid4().hex)
    snapshot_id = SnapshotId(uuid4().hex)
    package_hash = ""
    try:
        store.start_import(
            operation_id=operation_id,
            import_id=import_id,
            snapshot_id=snapshot_id,
        )
        try:
            if package_path.stat().st_size > max_package_bytes:
                raise _RejectedPackage("package too large")
            with package_path.open("rb") as package:
                package_hash = hashlib.file_digest(package, "sha256").hexdigest()
            store.mark_import_reading(import_id)
            parsed = _records(
                package_path,
                max_package_bytes=max_package_bytes,
                max_entries=max_entries,
                max_entry_bytes=max_entry_bytes,
                max_uncompressed_bytes=max_uncompressed_bytes,
                max_compression_ratio=max_compression_ratio,
            )
        except (
            _RejectedPackage,
            OSError,
            BadZipFile,
            NotImplementedError,
        ):
            store.reject_import(import_id, package_hash)
            return HealthImportResult(
                operation_id=operation_id,
                import_id=import_id,
                status="rejected",
                package_hash=package_hash,
                snapshot_id=None,
                record_count=0,
                diagnostics=("invalid_health_export",),
            )
        except (KeyError, ParseError, RuntimeError, ValueError):
            store.quarantine_import(import_id, snapshot_id, "invalid_health_data")
            return HealthImportResult(
                operation_id=operation_id,
                import_id=import_id,
                status="quarantined",
                package_hash=package_hash,
                snapshot_id=None,
                record_count=0,
                diagnostics=("invalid_health_data",),
            )
        try:
            governing_export_id = select_governing_export(
                (
                    *store.load_export_facts(),
                    ExportFact(parsed.export_id, parsed.export_date),
                )
            )
            published = store.publish_import(
                operation_id=operation_id,
                import_id=import_id,
                package_hash=package_hash,
                snapshot_id=snapshot_id,
                export_id=parsed.export_id,
                export_date=parsed.export_date,
                records=parsed.records,
                sleep_intervals=parsed.sleep_intervals,
                workouts=parsed.workouts,
                unknown_source_types=parsed.unknown_source_types,
                unsupported_content=parsed.unsupported_content,
                governing_export_id=governing_export_id,
                resolve_sources=resolve_sources,
            )
        except (OSError, StoreError) as error:
            cause = error.__cause__ if isinstance(error, StoreError) else error
            diagnostics = {
                errno.ENOSPC: "capacity_exhausted",
                errno.EROFS: "target_read_only",
                errno.EACCES: "target_unwritable",
            }
            if not isinstance(cause, OSError) or cause.errno not in diagnostics:
                raise
            diagnostic = diagnostics[cause.errno]
            store.quarantine_import(import_id, snapshot_id, diagnostic)
            return HealthImportResult(
                operation_id=operation_id,
                import_id=import_id,
                status="quarantined",
                package_hash=package_hash,
                snapshot_id=None,
                record_count=0,
                package_record_count=len(parsed.records)
                + len(parsed.sleep_intervals)
                + len(parsed.workouts),
                diagnostics=(diagnostic,),
            )
    except StoreError as error:
        raise HealthImportError("Health-Importspeicher ist nicht verfügbar.") from error
    return HealthImportResult(
        operation_id=operation_id,
        import_id=import_id,
        status=published.status,
        package_hash=package_hash,
        snapshot_id=published.snapshot_id,
        record_count=published.record_count,
        package_record_count=len(parsed.records)
        + len(parsed.sleep_intervals)
        + len(parsed.workouts),
        logical_measurement_count=published.logical_measurement_count,
        measurement_version_count=published.measurement_version_count,
        source_occurrence_count=published.source_occurrence_count,
        anomaly_count=published.anomaly_count,
        diagnostics=published.diagnostics,
    )


__all__ = [
    "CanonicalHealthType",
    "CanonicalUnit",
    "HealthExportEstimate",
    "HealthImportError",
    "HealthImportResult",
    "ImportId",
    "LogicalMeasurementId",
    "MeasurementVersionId",
    "OperationId",
    "RestoreHealthExportInspection",
    "SnapshotId",
    "estimate_health_export",
    "import_health_export",
    "inspect_restore_health_export",
]
