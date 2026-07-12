"""Apple Health import behind one package-level operation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4
from xml.etree.ElementTree import ParseError, iterparse
from zipfile import BadZipFile, ZipFile, is_zipfile

from personal_health_lab.health_data import (
    CanonicalHealthRecord,
    CanonicalHealthType,
    CanonicalUnit,
    HealthProvenance,
)
from personal_health_lab.storage import (
    DataMode,
    ImportId,
    LocalStore,
    OperationId,
    SnapshotId,
    StoreError,
)

_EXPORT_MEMBER = "apple_health_export/export.xml"
_ACTIVE_ENERGY = "HKQuantityTypeIdentifierActiveEnergyBurned"
_RESTING_HEART_RATE = "HKQuantityTypeIdentifierRestingHeartRate"
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


@dataclass(frozen=True, slots=True)
class HealthImportResult:
    operation_id: OperationId
    import_id: ImportId
    status: Literal["committed", "rejected"]
    package_hash: str
    snapshot_id: SnapshotId | None
    record_count: int
    diagnostics: tuple[str, ...] = ()


def _source_datetime(value: str) -> datetime:
    timestamp = datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
    if timestamp.tzinfo is None:
        raise ValueError("missing timezone")
    return timestamp


def _records(package_path: Path) -> tuple[CanonicalHealthRecord, ...]:
    if not package_path.is_file() or not is_zipfile(package_path):
        raise ValueError("invalid zip")
    records: list[CanonicalHealthRecord] = []
    with ZipFile(package_path) as archive:
        if _EXPORT_MEMBER not in archive.namelist():
            raise ValueError("missing export.xml")
        with archive.open(_EXPORT_MEMBER) as source:
            root_seen = False
            for event, element in iterparse(source, events=("start", "end")):
                if not root_seen:
                    if event != "start" or element.tag != "HealthData":
                        raise ValueError("invalid root")
                    root_seen = True
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
                    records.append(
                        CanonicalHealthRecord(
                            data_type=data_type,
                            unit=canonical_unit,
                            value=value,
                            source_start=source_start,
                            source_end=source_end,
                            measurement_local_day=source_start.date(),
                            provenance=HealthProvenance(
                                source_name=element.attrib["sourceName"],
                                source_version=element.attrib.get("sourceVersion", ""),
                                device=element.attrib.get("device", ""),
                                original_value=value,
                                original_unit=source_unit,
                            ),
                        )
                    )
                element.clear()
    if not records:
        raise ValueError("no supported records")
    return tuple(records)


def import_health_export(
    package_path: Path, *, root: Path, mode: DataMode
) -> HealthImportResult:
    operation_id = OperationId(uuid4().hex)
    import_id = ImportId(uuid4().hex)
    package_hash = ""
    try:
        with package_path.open("rb") as package:
            package_hash = hashlib.file_digest(package, "sha256").hexdigest()
        records = _records(package_path)
    except (OSError, BadZipFile, KeyError, ParseError, ValueError):
        return HealthImportResult(
            operation_id=operation_id,
            import_id=import_id,
            status="rejected",
            package_hash=package_hash,
            snapshot_id=None,
            record_count=0,
            diagnostics=("invalid_health_export",),
        )

    try:
        snapshot_id = SnapshotId(uuid4().hex)
        store = LocalStore.open(root=root, mode=mode)
        try:
            store.publish_import(
                operation_id=operation_id,
                import_id=import_id,
                package_hash=package_hash,
                snapshot_id=snapshot_id,
                records=records,
            )
        finally:
            store.close()
    except StoreError as error:
        raise HealthImportError("Health-Importspeicher ist nicht verfügbar.") from error
    return HealthImportResult(
        operation_id=operation_id,
        import_id=import_id,
        status="committed",
        package_hash=package_hash,
        snapshot_id=snapshot_id,
        record_count=len(records),
    )


__all__ = [
    "HealthImportError",
    "HealthImportResult",
    "ImportId",
    "OperationId",
    "SnapshotId",
    "import_health_export",
]
