"""Apple Health import behind one package-level operation."""

from __future__ import annotations

import errno
import hashlib
import json
import shutil
import stat
from collections import Counter
from dataclasses import dataclass
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
    CanonicalUnit,
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
_RESTING_HEART_RATE = "HKQuantityTypeIdentifierRestingHeartRate"
_BODY_MASS = "HKQuantityTypeIdentifierBodyMass"
_IDENTITY_RULE_VERSION = "healthkit-natural/v2"
_SYNC_IDENTIFIER = "HKMetadataKeySyncIdentifier"
_MAPPINGS = {
    _ACTIVE_ENERGY: (
        CanonicalHealthType.ACTIVE_ENERGY,
        CanonicalUnit.KILOCALORIE,
        {"kcal": 1.0},
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
_DIETARY_TYPES = {
    f"HKQuantityTypeIdentifierDietary{name}"
    for name in (
        "Biotin",
        "Caffeine",
        "Calcium",
        "Carbohydrates",
        "Chloride",
        "Cholesterol",
        "Chromium",
        "Copper",
        "EnergyConsumed",
        "FatMonounsaturated",
        "FatPolyunsaturated",
        "FatSaturated",
        "FatTotal",
        "Fiber",
        "Folate",
        "Iodine",
        "Iron",
        "Magnesium",
        "Manganese",
        "Molybdenum",
        "Niacin",
        "PantothenicAcid",
        "Phosphorus",
        "Potassium",
        "Protein",
        "Riboflavin",
        "Selenium",
        "Sodium",
        "Sugar",
        "Thiamin",
        "VitaminA",
        "VitaminB12",
        "VitaminB6",
        "VitaminC",
        "VitaminD",
        "VitaminE",
        "VitaminK",
        "Water",
        "Zinc",
    )
}
_V03_UNITS = {
    "HKQuantityTypeIdentifierBodyMass": frozenset({"kg", "g", "lb"}),
    "HKQuantityTypeIdentifierAppleExerciseTime": frozenset({"min", "s"}),
    "HKQuantityTypeIdentifierStepCount": frozenset({"count"}),
    "HKQuantityTypeIdentifierDistanceWalkingRunning": frozenset({"m", "km", "mi"}),
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
    unknown_source_types: tuple[str, ...] = ()
    unsupported_content: tuple[UnsupportedImportContent, ...] = ()


def _source_datetime(value: str) -> datetime:
    timestamp = datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
    if timestamp.tzinfo is None:
        raise ValueError("missing timezone")
    return timestamp


def _id(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()


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
                    unsupported["workout_activity_type", element.attrib["workoutActivityType"]] += 1
                    element.clear()
                elif parent == "HealthData" and element.tag == "Record":
                    source_type = element.attrib.get("type", "")
                    mapping = _MAPPINGS.get(source_type)
                    source_unit = element.attrib.get("unit", "")
                    if mapping is not None and source_unit in mapping[2]:
                        data_type, canonical_unit, conversions = mapping
                        original_value = float(element.attrib["value"])
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
                            _id(_IDENTITY_RULE_VERSION, "strong", strong_source_id_hash)
                            if strong_source_id_hash is not None
                            else _id(
                                _IDENTITY_RULE_VERSION,
                                "natural",
                                data_type.value,
                                source_start.isoformat(),
                                source_end.isoformat(),
                                source_name,
                                device,
                            )
                        )
                        payload_sha256 = _id(
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
                        records.append(
                            CanonicalHealthRecord(
                                logical_measurement_id=logical_id,
                                measurement_version_id=MeasurementVersionId(
                                    _id(_IDENTITY_RULE_VERSION, logical_id, payload_sha256)
                                ),
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
                            )
                        )
                    elif source_type:
                        unknown_source_types.add(source_type)
                        if source_type == _SLEEP_TYPE:
                            value = element.attrib.get("value", "")
                            if value not in _SLEEP_VALUES:
                                unsupported["sleep_value", value] += 1
                        elif source_type in _V03_UNITS:
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
    if not records and not unknown_source_types and not unsupported:
        raise ValueError("no supported records")
    return _ParsedExport(
        export_digest.hexdigest(),
        export_date,
        tuple(records),
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
        return HealthExportEstimate(max(input_bytes, package_size), len(parsed.records))
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
    return (
        exports,
        tuple(records.values()),
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
    _, records, input_bytes, _ = _restore_exports(
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
    estimate = HealthExportEstimate(max(input_bytes, 1), len(records))
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
        exports, records, _, restore_exports = _restore_exports(
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
            package_record_count=len(current.records),
            logical_measurement_count=len(
                {str(record.logical_measurement_id) for record in records}
            ),
            measurement_version_count=len(records),
            diagnostics=("restore_sources_pending",),
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
        package_record_count=len(current.records),
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
                package_record_count=len(parsed.records),
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
        package_record_count=len(parsed.records),
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
