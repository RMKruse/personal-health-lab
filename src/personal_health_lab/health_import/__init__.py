"""Apple Health import behind one package-level operation."""

from __future__ import annotations

import errno
import hashlib
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal
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
from personal_health_lab.storage import (
    ExportFact,
    ImportId,
    LocalStore,
    OperationId,
    SnapshotId,
    StoreError,
)

_EXPORT_MEMBER = "apple_health_export/export.xml"
_ACTIVE_ENERGY = "HKQuantityTypeIdentifierActiveEnergyBurned"
_RESTING_HEART_RATE = "HKQuantityTypeIdentifierRestingHeartRate"
_IDENTITY_RULE_VERSION = "healthkit-natural/v2"
_SYNC_IDENTIFIER = "HKMetadataKeySyncIdentifier"
_MAPPINGS = {
    _ACTIVE_ENERGY: (CanonicalHealthType.ACTIVE_ENERGY, CanonicalUnit.KILOCALORIE, "kcal"),
    _RESTING_HEART_RATE: (
        CanonicalHealthType.APPLE_RESTING_HEART_RATE,
        CanonicalUnit.BEATS_PER_MINUTE,
        "count/min",
    ),
}


class HealthImportError(Exception):
    """The import could not complete because its internal store failed."""


class _RejectedPackage(ValueError):
    """The package boundary is unsafe or unsupported."""


@dataclass(frozen=True, slots=True)
class HealthImportResult:
    operation_id: OperationId
    import_id: ImportId
    status: Literal["committed", "duplicate", "quarantined", "rejected"]
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
class _ParsedExport:
    export_id: str
    export_date: datetime | None
    records: tuple[CanonicalHealthRecord, ...]
    unknown_source_types: tuple[str, ...] = ()


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
            for event, element in iterparse(source, events=("start", "end")):
                if not root_seen:
                    if event != "start" or element.tag != "HealthData":
                        raise ValueError("invalid root")
                    root_seen = True
                if event == "end" and element.tag == "ExportDate":
                    if export_date is not None:
                        raise ValueError("duplicate export date")
                    export_date = _source_datetime(element.attrib["value"])
                    element.clear()
                    continue
                if event != "end" or element.tag != "Record":
                    continue
                mapping = _MAPPINGS.get(element.attrib.get("type", ""))
                if mapping is not None:
                    data_type, canonical_unit, source_unit = mapping
                    if element.attrib.get("unit") != source_unit:
                        raise ValueError("unsupported unit")
                    value = float(element.attrib["value"])
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
                    strong_source_id_hash = None if sync_id is None else _id(source_name, sync_id)
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
                        canonical_unit.value,
                        value,
                        source_start.isoformat(),
                        source_end.isoformat(),
                        source_name,
                        device,
                    )
                    records.append(
                        CanonicalHealthRecord(
                            logical_measurement_id=logical_id,
                            measurement_version_id=MeasurementVersionId(
                                _id(
                                    _IDENTITY_RULE_VERSION,
                                    logical_id,
                                    payload_sha256,
                                )
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
                                original_value=value,
                                original_unit=source_unit,
                                strong_source_id_hash=strong_source_id_hash,
                            ),
                        )
                    )
                elif source_type := element.attrib.get("type"):
                    unknown_source_types.add(source_type)
                element.clear()
    if not records and not unknown_source_types:
        raise ValueError("no supported records")
    return _ParsedExport(
        export_digest.hexdigest(),
        export_date,
        tuple(records),
        tuple(sorted(unknown_source_types)),
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


def import_health_export(
    package_path: Path,
    *,
    store: LocalStore,
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
    "SnapshotId",
    "estimate_health_export",
    "import_health_export",
]
