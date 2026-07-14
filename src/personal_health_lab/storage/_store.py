from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import plistlib
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import IO, Literal, Self, cast
from uuid import uuid4

import duckdb

from personal_health_lab import DataMode
from personal_health_lab.health_data import (
    CanonicalHealthRecord,
    CanonicalHealthType,
    CanonicalUnit,
    DailyHealthSeries,
    DailyHealthValue,
    LogicalMeasurementId,
    MeasurementVersionId,
)

_METADATA_FILE = "metadata.sqlite3"
_PARQUET_DIRECTORY = "parquet"
_QUERY_FILE = "query.duckdb"
_STORE_SCHEMA_VERSION = 2
_WRITER_LOCK_FILE = ".writer.lock"
_FULL_SNAPSHOT_IMPORT_METHOD = "full-snapshot-import/v1"
_SNAPSHOT_SCHEMA_VERSION = 1
_IDENTITY_RULE_VERSION = "healthkit-natural/v2"
_MAPPING_RULE_VERSION = "healthkit-canonical/v1"
_SNAPSHOT_SCHEMAS = {
    "source_occurrences.parquet": (
        ("occurrence_id", "VARCHAR"),
        ("export_id", "VARCHAR"),
        ("export_ordinal", "BIGINT"),
        ("identity_candidate_id", "VARCHAR"),
        ("measurement_version_id", "VARCHAR"),
        ("mapping_rule_version_id", "VARCHAR"),
        ("occurrence_fingerprint", "VARCHAR"),
    ),
    "measurement_versions.parquet": (
        ("measurement_version_id", "VARCHAR"),
        ("identity_candidate_id", "VARCHAR"),
        ("payload_sha256", "VARCHAR"),
        ("canonical_type", "VARCHAR"),
        ("canonical_unit", "VARCHAR"),
        ("canonical_value", "DOUBLE"),
        ("source_start_utc", "VARCHAR"),
        ("source_end_utc", "VARCHAR"),
        ("source_updated_at_utc", "VARCHAR"),
        ("source_start_offset_minutes", "INTEGER"),
        ("source_end_offset_minutes", "INTEGER"),
        ("source_updated_at_offset_minutes", "INTEGER"),
        ("measurement_local_date", "DATE"),
        ("source_name", "VARCHAR"),
        ("source_version", "VARCHAR"),
        ("device", "VARCHAR"),
        ("original_value", "DOUBLE"),
        ("original_unit", "VARCHAR"),
        ("strong_source_id_hash", "VARCHAR"),
    ),
    "resolved_measurements.parquet": (
        ("logical_measurement_id", "VARCHAR"),
        ("selected_measurement_version_id", "VARCHAR"),
        ("disposition", "VARCHAR"),
        ("effective_value", "DOUBLE"),
        ("canonical_unit", "VARCHAR"),
        ("effective_value_source", "VARCHAR"),
        ("effective_decision_id", "VARCHAR"),
        ("correction_decision_id", "VARCHAR"),
        ("source_deletion_decision_id", "VARCHAR"),
        ("conflict_resolution_decision_id", "VARCHAR"),
    ),
    "open_review_cases.parquet": (
        ("review_case_id", "VARCHAR"),
        ("case_kind", "VARCHAR"),
        ("logical_measurement_id", "VARCHAR"),
        ("measurement_version_id", "VARCHAR"),
        ("rule_version_id", "VARCHAR"),
        ("evidence_fingerprint", "VARCHAR"),
    ),
}
_KIB = 1024
_MIB = 1024 * _KIB
_GIB = 1024 * _MIB
_DIRECTORY_OVERHEAD = 64 * _KIB
_STORE_IDENTITY_DDL = """
CREATE TABLE store_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    mode TEXT NOT NULL CHECK (mode IN ('synthetic', 'real')),
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    store_id TEXT CHECK (
        store_id IS NULL OR (
            length(store_id) = 32 AND store_id NOT GLOB '*[^0-9a-f]*'
        )
    ),
    person_binding TEXT NOT NULL DEFAULT 'unbound'
        CHECK (person_binding IN ('unbound', 'bound', 'pending'))
) STRICT
"""


def _is_lower_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


class StoreError(RuntimeError):
    """A local store cannot be opened safely."""


class StoreConfigurationError(StoreError, ValueError):
    """A local store conflicts with its requested runtime configuration."""


class StoreBusyError(StoreError):
    """Another process owns the store's writer lock."""


class PersonBindingStatus(StrEnum):
    UNBOUND = "unbound"
    BOUND = "bound"
    PENDING = "pending"


class FileVaultStatus(StrEnum):
    PROTECTED = "protected"
    UNPROTECTED = "unprotected"
    TRANSITIONING = "transitioning"
    UNKNOWN = "unknown"


class FileVaultReason(StrEnum):
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    MOUNT_UNRESOLVED = "mount_unresolved"
    DISK_INFO_UNAVAILABLE = "disk_info_unavailable"
    VOLUME_IDENTITY_MISSING = "volume_identity_missing"
    VOLUME_LOCKED = "volume_locked"
    APFS_STATE_UNAVAILABLE = "apfs_state_unavailable"
    FILEVAULT_STATE_UNAVAILABLE = "filevault_state_unavailable"
    FILEVAULT_STATE_UNREPORTED = "filevault_state_unreported"
    ENCRYPTION_STATE_UNCONFIRMED = "encryption_state_unconfirmed"
    PROBE_FAILED = "probe_failed"


@dataclass(frozen=True, slots=True)
class FileVaultCheck:
    status: FileVaultStatus
    target_volume: str
    reason: FileVaultReason | None = None


class CapacityStatus(StrEnum):
    READY = "ready"
    INSUFFICIENT = "insufficient"
    UNKNOWN = "unknown"
    UNWRITABLE = "unwritable"


class CapacityReason(StrEnum):
    ESTIMATE_UNKNOWN = "estimate_unknown"
    SPACE_UNKNOWN = "space_unknown"
    TARGET_READ_ONLY = "target_read_only"
    TARGET_UNWRITABLE = "target_unwritable"


@dataclass(frozen=True, slots=True)
class CapacityCheck:
    status: CapacityStatus
    target_volume: str
    method_id: str
    estimate_bytes: int | None
    safety_margin_bytes: int | None
    minimum_remaining_bytes: int
    available_bytes: int | None
    required_bytes: int | None
    fragment_size: int | None
    reason: CapacityReason | None = None


def _round_up(value: int, fragment_size: int) -> int:
    return ((value + fragment_size - 1) // fragment_size) * fragment_size


def _utc_offset_minutes(value: datetime) -> int:
    offset = value.utcoffset()
    assert offset is not None
    return int(offset.total_seconds() // 60)


def full_snapshot_import_estimate(
    input_bytes: int,
    active_snapshot_bytes: int,
    fragment_size: int,
    *,
    record_count: int = 0,
    writer_bound: bool = True,
    scratch_bound: bool = True,
) -> int | None:
    """Bound the peak live allocation of ``full-snapshot-import/v1``."""
    if not writer_bound or not scratch_bound:
        return None
    if input_bytes < 0 or active_snapshot_bytes < 0 or record_count < 0 or fragment_size <= 0:
        raise ValueError("Kapazitätseingaben müssen nichtnegativ und Fragmente positiv sein.")
    input_bytes = max(input_bytes, 1)
    scratch = _round_up(4 * input_bytes, fragment_size)
    snapshot = _round_up(2 * input_bytes + 2 * active_snapshot_bytes, fragment_size)
    catalog = _round_up(max((4 * input_bytes + 2) // 3, 512 * record_count), fragment_size)
    journal = _round_up(max((2 * input_bytes + 2) // 3, 256 * record_count), fragment_size)
    manifest = _round_up(max(input_bytes // 12, 64 * _KIB), fragment_size)
    phases = (
        scratch,
        scratch + snapshot + manifest,
        scratch + snapshot + manifest + catalog + journal,
    )
    return _DIRECTORY_OVERHEAD + max(phases)


def _allocation_checkpoint(root: Path, phase: str) -> None:
    """Private test seam for measuring the real writer's live allocation."""


def _publication_fault_point(root: Path, fault_point_id: str) -> None:
    """Private test seam for durable import-publication transitions."""


def probe_capacity(path: Path, estimate_bytes: int | None) -> CapacityCheck:
    method_id = _FULL_SNAPSHOT_IMPORT_METHOD
    minimum_remaining = _GIB
    try:
        stats = os.statvfs(path)
        device = path.stat().st_dev
    except OSError:
        return CapacityCheck(
            CapacityStatus.UNKNOWN,
            "unresolved",
            method_id,
            estimate_bytes,
            None,
            minimum_remaining,
            None,
            None,
            None,
            CapacityReason.SPACE_UNKNOWN,
        )
    target_volume = "volume-" + hashlib.sha256(str(device).encode()).hexdigest()[:12]
    available = stats.f_bavail * stats.f_frsize
    if estimate_bytes is None:
        return CapacityCheck(
            CapacityStatus.UNKNOWN,
            target_volume,
            method_id,
            None,
            None,
            minimum_remaining,
            available,
            None,
            stats.f_frsize,
            CapacityReason.ESTIMATE_UNKNOWN,
        )
    margin = max((estimate_bytes + 3) // 4, 256 * _MIB)
    required = estimate_bytes + margin + minimum_remaining
    read_only = bool(stats.f_flag & getattr(os, "ST_RDONLY", 1))
    if read_only or not os.access(path, os.W_OK):
        return CapacityCheck(
            CapacityStatus.UNWRITABLE,
            target_volume,
            method_id,
            estimate_bytes,
            margin,
            minimum_remaining,
            available,
            required,
            stats.f_frsize,
            (CapacityReason.TARGET_READ_ONLY if read_only else CapacityReason.TARGET_UNWRITABLE),
        )
    return CapacityCheck(
        CapacityStatus.READY if available >= required else CapacityStatus.INSUFFICIENT,
        target_volume,
        method_id,
        estimate_bytes,
        margin,
        minimum_remaining,
        available,
        required,
        stats.f_frsize,
        None,
    )


def probe_filevault(path: Path) -> FileVaultCheck:
    if platform.system() != "Darwin":
        return FileVaultCheck(
            FileVaultStatus.UNKNOWN, "unresolved", FileVaultReason.UNSUPPORTED_PLATFORM
        )
    try:
        mounted = subprocess.run(
            ["/bin/df", "-P", str(path.resolve())],
            capture_output=True,
            check=False,
            timeout=5,
        )
        lines = mounted.stdout.decode("utf-8", errors="strict").splitlines()
        device = lines[-1].split()[0]
        if mounted.returncode != 0 or len(lines) < 2 or not device.startswith("/dev/disk"):
            return FileVaultCheck(
                FileVaultStatus.UNKNOWN, "unresolved", FileVaultReason.MOUNT_UNRESOLVED
            )
        inspected = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", device],
            capture_output=True,
            check=False,
            timeout=5,
        )
        info = plistlib.loads(inspected.stdout)
        if inspected.returncode != 0 or not isinstance(info, dict):
            return FileVaultCheck(
                FileVaultStatus.UNKNOWN,
                "unresolved",
                FileVaultReason.DISK_INFO_UNAVAILABLE,
            )
        target_volume = info.get("VolumeUUID") or info.get("APFSVolumeUUID")
        if not isinstance(target_volume, str) or not target_volume:
            return FileVaultCheck(
                FileVaultStatus.UNKNOWN,
                "unresolved",
                FileVaultReason.VOLUME_IDENTITY_MISSING,
            )
        if info.get("Locked") is True:
            return FileVaultCheck(
                FileVaultStatus.UNKNOWN, target_volume, FileVaultReason.VOLUME_LOCKED
            )
        is_apfs = info.get("FilesystemType") == "apfs" or isinstance(
            info.get("APFSVolumeUUID"), str
        )
        if is_apfs:
            apfs = subprocess.run(
                ["/usr/sbin/diskutil", "apfs", "list", "-plist"],
                capture_output=True,
                check=False,
                timeout=5,
            )
            apfs_info = plistlib.loads(apfs.stdout)
            if apfs.returncode != 0 or not isinstance(apfs_info, dict):
                return FileVaultCheck(
                    FileVaultStatus.UNKNOWN,
                    target_volume,
                    FileVaultReason.APFS_STATE_UNAVAILABLE,
                )
            apfs_volume = _find_apfs_volume(apfs_info, device.removeprefix("/dev/"), target_volume)
            if apfs_volume is None:
                return FileVaultCheck(
                    FileVaultStatus.UNKNOWN,
                    target_volume,
                    FileVaultReason.APFS_STATE_UNAVAILABLE,
                )
            if apfs_volume.get("CryptoMigrationOn") is True:
                return FileVaultCheck(FileVaultStatus.TRANSITIONING, target_volume)
            roles = apfs_volume.get("Roles")
            target_group = info.get("APFSVolumeGroupID")
        else:
            roles = target_group = None
        startup_group: object = None
        if isinstance(roles, list) and {"Data", "System"}.intersection(roles):
            if not isinstance(target_group, str) or not target_group:
                return FileVaultCheck(
                    FileVaultStatus.UNKNOWN,
                    target_volume,
                    FileVaultReason.APFS_STATE_UNAVAILABLE,
                )
            startup = subprocess.run(
                ["/usr/sbin/diskutil", "info", "-plist", "/"],
                capture_output=True,
                check=False,
                timeout=5,
            )
            startup_info = plistlib.loads(startup.stdout)
            if startup.returncode != 0 or not isinstance(startup_info, dict):
                return FileVaultCheck(
                    FileVaultStatus.UNKNOWN,
                    target_volume,
                    FileVaultReason.APFS_STATE_UNAVAILABLE,
                )
            startup_group = startup_info.get("APFSVolumeGroupID")
            if not isinstance(startup_group, str) or not startup_group:
                return FileVaultCheck(
                    FileVaultStatus.UNKNOWN,
                    target_volume,
                    FileVaultReason.APFS_STATE_UNAVAILABLE,
                )
        if isinstance(target_group, str) and target_group and target_group == startup_group:
            active = subprocess.run(
                ["/usr/bin/fdesetup", "isactive"],
                capture_output=True,
                check=False,
                timeout=5,
            )
            if active.returncode == 0 and active.stdout.strip().lower() == b"true":
                return FileVaultCheck(FileVaultStatus.PROTECTED, target_volume)
            if active.returncode == 0 and active.stdout.strip().lower() == b"false":
                return FileVaultCheck(FileVaultStatus.UNPROTECTED, target_volume)
            return FileVaultCheck(
                FileVaultStatus.UNKNOWN,
                target_volume,
                FileVaultReason.FILEVAULT_STATE_UNAVAILABLE,
            )
        filevault = info.get("FileVault")
        if filevault is True and info.get("Encryption") is True:
            return FileVaultCheck(FileVaultStatus.PROTECTED, target_volume)
        if filevault is False:
            return FileVaultCheck(FileVaultStatus.UNPROTECTED, target_volume)
        if filevault is True:
            return FileVaultCheck(
                FileVaultStatus.UNKNOWN,
                target_volume,
                FileVaultReason.ENCRYPTION_STATE_UNCONFIRMED,
            )
        return FileVaultCheck(
            FileVaultStatus.UNKNOWN,
            target_volume,
            FileVaultReason.FILEVAULT_STATE_UNREPORTED,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, IndexError):
        return FileVaultCheck(FileVaultStatus.UNKNOWN, "unresolved", FileVaultReason.PROBE_FAILED)


def _find_apfs_volume(value: object, device: str, volume_uuid: str) -> dict[str, object] | None:
    if isinstance(value, dict):
        matches_volume = (
            value.get("DeviceIdentifier") == device or value.get("APFSVolumeUUID") == volume_uuid
        )
        if matches_volume:
            return value
        return next(
            (
                match
                for child in value.values()
                if (match := _find_apfs_volume(child, device, volume_uuid)) is not None
            ),
            None,
        )
    if isinstance(value, list):
        return next(
            (
                match
                for child in value
                if (match := _find_apfs_volume(child, device, volume_uuid)) is not None
            ),
            None,
        )
    return None


@dataclass(frozen=True, slots=True)
class _OpaqueStoreId:
    _value: str

    def __post_init__(self) -> None:
        if not self._value:
            raise ValueError("ID darf nicht leer sein.")

    def __str__(self) -> str:
        return self._value


@dataclass(frozen=True, slots=True)
class OperationId(_OpaqueStoreId):
    pass


@dataclass(frozen=True, slots=True)
class ImportId(_OpaqueStoreId):
    pass


@dataclass(frozen=True, slots=True)
class SnapshotId(_OpaqueStoreId):
    pass


@dataclass(frozen=True, slots=True)
class StoreId(_OpaqueStoreId):
    def __post_init__(self) -> None:
        if len(self._value) != 32 or any(
            character not in "0123456789abcdef" for character in self._value
        ):
            raise ValueError("Datenspeicher-ID ist ungültig.")


@dataclass(frozen=True, slots=True)
class StoreIdentity:
    store_id: StoreId | None
    mode: DataMode
    person_binding: PersonBindingStatus
    schema_version: str


@dataclass(frozen=True, slots=True)
class AnalysisRunId(_OpaqueStoreId):
    pass


@dataclass(frozen=True, slots=True)
class AnalysisDefinitionId(_OpaqueStoreId):
    pass


@dataclass(frozen=True, slots=True)
class AnalysisResultId(_OpaqueStoreId):
    pass


@dataclass(frozen=True, slots=True)
class AnalysisProvenance:
    analysis_run_id: AnalysisRunId
    result_id: AnalysisResultId | None
    snapshot_id: SnapshotId
    analysis_definition_id: AnalysisDefinitionId
    config_hash: str
    config_schema_version: str
    code_commit: str
    code_dirty: bool
    code_diff_hash: str | None
    environment_lock_hash: str

    @property
    def reuse_key(self) -> str:
        values = {
            "analysis_definition_id": str(self.analysis_definition_id),
            "code_commit": self.code_commit,
            "code_diff_hash": self.code_diff_hash,
            "code_dirty": self.code_dirty,
            "config_hash": self.config_hash,
            "config_schema_version": self.config_schema_version,
            "environment_lock_hash": self.environment_lock_hash,
            "snapshot_id": str(self.snapshot_id),
        }
        return hashlib.sha256(
            json.dumps(values, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()


def _analysis_provenance(row: tuple[object, ...]) -> AnalysisProvenance:
    return AnalysisProvenance(
        analysis_run_id=AnalysisRunId(str(row[0])),
        result_id=None if row[1] is None else AnalysisResultId(str(row[1])),
        snapshot_id=SnapshotId(str(row[2])),
        analysis_definition_id=AnalysisDefinitionId(str(row[3])),
        config_hash=str(row[4]),
        config_schema_version=str(row[5]),
        code_commit=str(row[6]),
        code_dirty=bool(row[7]),
        code_diff_hash=None if row[8] is None else str(row[8]),
        environment_lock_hash=str(row[9]),
    )


class AssociationDirection(StrEnum):
    NEGATIVE = "negative"
    ZERO = "zero"
    POSITIVE = "positive"


@dataclass(frozen=True, slots=True)
class AssociationInterval:
    lower_per_100_kcal: float
    upper_per_100_kcal: float
    lower_per_personal_standard_deviation: float
    upper_per_personal_standard_deviation: float


@dataclass(frozen=True, slots=True)
class AssociationEstimate:
    lag_days: int | None
    direction: AssociationDirection
    estimate_per_100_kcal: float
    estimate_per_personal_standard_deviation: float
    pointwise_interval: AssociationInterval
    simultaneous_band: AssociationInterval | None
    exposure_unit: CanonicalUnit = CanonicalUnit.KILOCALORIE
    outcome_unit: CanonicalUnit = CanonicalUnit.BEATS_PER_MINUTE


@dataclass(frozen=True, slots=True)
class AnalysisMethodology:
    ridge_penalty: float
    minimum_observations: int
    robust_observations: int
    bootstrap_method: Literal["moving_block"]
    block_length_days: int
    resample_count: int
    random_seed: int
    interval_level: float


@dataclass(frozen=True, slots=True)
class AnalysisDiagnostics:
    complete_days: int
    feature_dependency: Literal["acceptable", "high"]
    bootstrap_successes: int
    bootstrap_resamples: int
    model_readiness: Literal["exploratory", "robust"]
    association_guardrail: Literal[
        "simultaneous_band_includes_zero", "simultaneous_band_excludes_zero"
    ]


@dataclass(frozen=True, slots=True)
class RestingHeartRateAnalysisResult:
    snapshot_id: SnapshotId
    analysis_definition_id: AnalysisDefinitionId
    personal_standard_deviation_kcal: float
    lag_associations: tuple[AssociationEstimate, ...]
    cumulative_association: AssociationEstimate
    model_maturity: Literal["exploratory", "robust"]
    diagnostics: AnalysisDiagnostics
    methodology: AnalysisMethodology
    provenance: AnalysisProvenance | None = None


@dataclass(frozen=True, slots=True)
class AnalysisRunRecord:
    provenance: AnalysisProvenance
    model_maturity: Literal["exploratory", "robust"]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublishImportResult:
    status: Literal["committed", "duplicate"]
    snapshot_id: SnapshotId
    record_count: int
    logical_measurement_count: int
    measurement_version_count: int
    source_occurrence_count: int
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OpenDataReviewCase:
    review_case_id: str
    kind: Literal[
        "plausibility",
        "continued_override",
        "suspected_source_deletion",
        "source_conflict",
    ]
    logical_measurement_id: LogicalMeasurementId | None
    measurement_version_id: MeasurementVersionId | None
    rule_version_id: str | None
    evidence_fingerprint: str


@dataclass(frozen=True, slots=True)
class ExportFact:
    export_id: str
    export_date: datetime | None


@dataclass(frozen=True, slots=True)
class SourceOccurrenceFact:
    export_id: str
    export_ordinal: int
    logical_measurement_id: str
    measurement_version_id: str


@dataclass(frozen=True, slots=True)
class MeasurementVersionFact:
    measurement_version_id: str
    logical_measurement_id: str
    canonical_type: str
    canonical_unit: str
    canonical_value: float
    source_start_utc: str
    source_end_utc: str
    source_name: str
    device: str
    strong_source_id_hash: str | None


@dataclass(frozen=True, slots=True)
class ResolvedMeasurement:
    logical_measurement_id: str
    selected_measurement_version_id: str
    disposition: str
    effective_value: float | None
    canonical_unit: str
    effective_value_source: str
    effective_decision_id: str | None
    correction_decision_id: str | None
    source_deletion_decision_id: str | None
    conflict_resolution_decision_id: str | None


@dataclass(frozen=True, slots=True)
class SourceResolution:
    measurements: tuple[ResolvedMeasurement, ...]
    review_cases: tuple[OpenDataReviewCase, ...]


type SourceResolver = Callable[..., SourceResolution]


@dataclass(frozen=True, slots=True)
class ProvenanceCounts:
    import_count: int
    package_count: int
    snapshot_count: int
    logical_measurement_count: int
    measurement_version_count: int
    quarantined_import_count: int


def _ensure_current_tables(metadata: sqlite3.Connection) -> None:
    metadata.executescript(
        """
        CREATE TABLE IF NOT EXISTS imports (
            import_id TEXT PRIMARY KEY CHECK (
                length(import_id) = 32 AND import_id NOT GLOB '*[^0-9a-f]*'
            ),
            operation_id TEXT NOT NULL CHECK (
                length(operation_id) = 32 AND operation_id NOT GLOB '*[^0-9a-f]*'
            ),
            package_hash TEXT NOT NULL CHECK (
                package_hash = '' OR (
                    length(package_hash) = 64 AND package_hash NOT GLOB '*[^0-9a-f]*'
                )
            ),
            status TEXT NOT NULL CHECK (
                status IN ('running', 'committed', 'duplicate', 'rejected', 'quarantined')
            ),
            snapshot_id TEXT NOT NULL CHECK (
                length(snapshot_id) = 32 AND snapshot_id NOT GLOB '*[^0-9a-f]*'
            ),
            package_record_count INTEGER NOT NULL CHECK (package_record_count >= 0),
            record_count INTEGER NOT NULL CHECK (record_count >= 0),
            committed_at TEXT NOT NULL CHECK (
                length(committed_at) >= 20 AND substr(committed_at, 11, 1) = 'T'
            ),
            diagnostics TEXT NOT NULL DEFAULT ''
        ) STRICT;
        CREATE TABLE IF NOT EXISTS write_operations (
            operation_id TEXT PRIMARY KEY CHECK (
                length(operation_id) = 32 AND operation_id NOT GLOB '*[^0-9a-f]*'
            ),
            request_kind TEXT NOT NULL CHECK (request_kind = 'import_health_export'),
            started_at_utc TEXT NOT NULL CHECK (
                length(started_at_utc) >= 20 AND substr(started_at_utc, 11, 1) = 'T'
            ),
            completed_at_utc TEXT NOT NULL CHECK (
                length(completed_at_utc) >= 20 AND substr(completed_at_utc, 11, 1) = 'T'
            ),
            outcome_code TEXT NOT NULL CHECK (outcome_code = 'committed'),
            state_changed INTEGER NOT NULL CHECK (state_changed IN (0, 1))
        ) STRICT;
        CREATE TABLE IF NOT EXISTS dataset_snapshots (
            snapshot_id TEXT PRIMARY KEY CHECK (
                length(snapshot_id) = 32 AND snapshot_id NOT GLOB '*[^0-9a-f]*'
            ),
            snapshot_schema_version INTEGER NOT NULL CHECK (snapshot_schema_version > 0),
            manifest_sha256 TEXT NOT NULL UNIQUE CHECK (
                length(manifest_sha256) = 64
                AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            created_by_operation_id TEXT NOT NULL REFERENCES write_operations(operation_id),
            parent_snapshot_id TEXT REFERENCES dataset_snapshots(snapshot_id),
            created_at_utc TEXT NOT NULL CHECK (
                length(created_at_utc) >= 20 AND substr(created_at_utc, 11, 1) = 'T'
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS snapshot_activations (
            activation_id TEXT PRIMARY KEY CHECK (
                length(activation_id) = 32 AND activation_id NOT GLOB '*[^0-9a-f]*'
            ),
            operation_id TEXT NOT NULL UNIQUE REFERENCES write_operations(operation_id),
            snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(snapshot_id),
            previous_snapshot_id TEXT REFERENCES dataset_snapshots(snapshot_id),
            activation_kind TEXT NOT NULL CHECK (activation_kind = 'import'),
            activated_at_utc TEXT NOT NULL CHECK (
                length(activated_at_utc) >= 20 AND substr(activated_at_utc, 11, 1) = 'T'
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS active_snapshot (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(snapshot_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS import_measurement_versions (
            import_id TEXT NOT NULL REFERENCES imports(import_id),
            measurement_version_id TEXT NOT NULL CHECK (
                length(measurement_version_id) = 64
                AND measurement_version_id NOT GLOB '*[^0-9a-f]*'
            ),
            PRIMARY KEY (import_id, measurement_version_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS exports (
            export_id TEXT PRIMARY KEY CHECK (
                length(export_id) = 64 AND export_id NOT GLOB '*[^0-9a-f]*'
            ),
            package_hash TEXT NOT NULL UNIQUE CHECK (
                length(package_hash) = 64 AND package_hash NOT GLOB '*[^0-9a-f]*'
            ),
            export_date_utc TEXT,
            order_state TEXT NOT NULL CHECK (order_state IN ('ordered', 'unordered')),
            CHECK (
                (order_state = 'ordered' AND export_date_utc IS NOT NULL)
                OR (order_state = 'unordered' AND export_date_utc IS NULL)
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS decision_refs (
            decision_id TEXT PRIMARY KEY CHECK (
                length(decision_id) = 32 AND decision_id NOT GLOB '*[^0-9a-f]*'
            ),
            decision_kind TEXT NOT NULL CHECK (
                decision_kind IN (
                    'correction', 'local_exclusion',
                    'source_deletion', 'conflict_resolution'
                )
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS rule_version_refs (
            rule_version_id TEXT PRIMARY KEY,
            rule_kind TEXT NOT NULL CHECK (
                rule_kind IN ('identity', 'mapping', 'plausibility')
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS audit_events (
            audit_position INTEGER PRIMARY KEY CHECK (audit_position > 0),
            audit_event_id TEXT NOT NULL UNIQUE CHECK (
                length(audit_event_id) = 32 AND audit_event_id NOT GLOB '*[^0-9a-f]*'
            ),
            operation_id TEXT NOT NULL REFERENCES write_operations(operation_id),
            event_kind TEXT NOT NULL CHECK (
                event_kind IN ('import_published', 'metadata_tombstone')
            ),
            occurred_at_utc TEXT NOT NULL CHECK (
                length(occurred_at_utc) >= 20 AND substr(occurred_at_utc, 11, 1) = 'T'
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS import_publications (
            audit_event_id TEXT PRIMARY KEY REFERENCES audit_events(audit_event_id),
            import_id TEXT NOT NULL UNIQUE REFERENCES imports(import_id),
            snapshot_id TEXT NOT NULL UNIQUE REFERENCES dataset_snapshots(snapshot_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS metadata_tombstones (
            tombstone_id TEXT PRIMARY KEY CHECK (
                length(tombstone_id) = 32 AND tombstone_id NOT GLOB '*[^0-9a-f]*'
            ),
            audit_event_id TEXT NOT NULL UNIQUE REFERENCES audit_events(audit_event_id),
            target_audit_event_id TEXT NOT NULL REFERENCES audit_events(audit_event_id),
            tombstone_kind TEXT NOT NULL CHECK (
                tombstone_kind IN ('superseded', 'revoked', 'deactivated', 'deleted')
            ),
            replacement_audit_event_id TEXT REFERENCES audit_events(audit_event_id),
            mandatory_reason TEXT
        ) STRICT;
        CREATE TRIGGER IF NOT EXISTS audit_events_contiguous
        BEFORE INSERT ON audit_events
        WHEN NEW.audit_position != COALESCE((SELECT MAX(audit_position) FROM audit_events), 0) + 1
        BEGIN SELECT RAISE(ABORT, 'audit_position must be contiguous'); END;
        CREATE TRIGGER IF NOT EXISTS audit_events_no_update
        BEFORE UPDATE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
        BEFORE DELETE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS import_publications_no_update
        BEFORE UPDATE ON import_publications
        BEGIN SELECT RAISE(ABORT, 'audit payload is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS import_publications_no_delete
        BEFORE DELETE ON import_publications
        BEGIN SELECT RAISE(ABORT, 'audit payload is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS import_publications_kind
        BEFORE INSERT ON import_publications
        WHEN (SELECT event_kind FROM audit_events WHERE audit_event_id = NEW.audit_event_id)
             != 'import_published'
        BEGIN SELECT RAISE(ABORT, 'wrong audit payload type'); END;
        CREATE TRIGGER IF NOT EXISTS metadata_tombstones_kind
        BEFORE INSERT ON metadata_tombstones
        WHEN (SELECT event_kind FROM audit_events WHERE audit_event_id = NEW.audit_event_id)
             != 'metadata_tombstone'
        BEGIN SELECT RAISE(ABORT, 'wrong audit payload type'); END;
        CREATE TRIGGER IF NOT EXISTS metadata_tombstones_backward
        BEFORE INSERT ON metadata_tombstones
        WHEN ((SELECT audit_position FROM audit_events
               WHERE audit_event_id = NEW.target_audit_event_id)
              >= (SELECT audit_position FROM audit_events
                  WHERE audit_event_id = NEW.audit_event_id))
          OR (NEW.replacement_audit_event_id IS NOT NULL
              AND (SELECT audit_position FROM audit_events
                   WHERE audit_event_id = NEW.replacement_audit_event_id)
                  >= (SELECT audit_position FROM audit_events
                      WHERE audit_event_id = NEW.audit_event_id))
        BEGIN SELECT RAISE(ABORT, 'tombstone target must be earlier'); END;
        CREATE TRIGGER IF NOT EXISTS metadata_tombstones_no_update
        BEFORE UPDATE ON metadata_tombstones
        BEGIN SELECT RAISE(ABORT, 'audit payload is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS metadata_tombstones_no_delete
        BEFORE DELETE ON metadata_tombstones
        BEGIN SELECT RAISE(ABORT, 'audit payload is append-only'); END;
        CREATE TABLE IF NOT EXISTS analysis_receipts (
            operation_id TEXT PRIMARY KEY,
            analysis_run_id TEXT NOT NULL,
            status TEXT NOT NULL,
            result_id TEXT,
            snapshot_id TEXT,
            analysis_definition_id TEXT,
            config_json TEXT NOT NULL,
            config_hash TEXT,
            config_schema_version TEXT,
            code_commit TEXT,
            code_dirty INTEGER,
            code_diff_hash TEXT,
            environment_lock_hash TEXT,
            diagnostics TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    metadata.executemany(
        "INSERT OR IGNORE INTO rule_version_refs VALUES (?, ?)",
        (
            (_IDENTITY_RULE_VERSION, "identity"),
            (_MAPPING_RULE_VERSION, "mapping"),
        ),
    )
    import_columns = {
        str(row[1]) for row in metadata.execute("PRAGMA table_info(imports)").fetchall()
    }
    if "diagnostics" not in import_columns:
        metadata.execute("ALTER TABLE imports ADD COLUMN diagnostics TEXT NOT NULL DEFAULT ''")
    analysis_table = metadata.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'analysis_runs'"
    ).fetchone()
    if analysis_table is None:
        metadata.execute(
            """
            CREATE TABLE analysis_runs (
                analysis_run_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL,
                result_id TEXT NOT NULL UNIQUE,
                snapshot_id TEXT NOT NULL,
                analysis_definition_id TEXT NOT NULL,
                analysis_start_date TEXT,
                analysis_end_date TEXT,
                config_json TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                config_schema_version TEXT NOT NULL,
                code_commit TEXT NOT NULL,
                code_dirty INTEGER NOT NULL,
                code_diff_hash TEXT,
                environment_lock_hash TEXT NOT NULL,
                reuse_key TEXT NOT NULL,
                model_maturity TEXT NOT NULL,
                diagnostics TEXT NOT NULL,
                status TEXT NOT NULL,
                completed_at TEXT NOT NULL
            )
            """
        )
        return
    analysis_columns = {str(row[1]) for row in metadata.execute("PRAGMA table_info(analysis_runs)")}
    migrations = {
        "analysis_start_date": "TEXT",
        "analysis_end_date": "TEXT",
        "config_json": "TEXT NOT NULL DEFAULT '{}'",
        "config_hash": "TEXT NOT NULL DEFAULT ''",
        "config_schema_version": "TEXT NOT NULL DEFAULT '1.0'",
        "code_commit": "TEXT NOT NULL DEFAULT ''",
        "code_dirty": "INTEGER NOT NULL DEFAULT 0",
        "code_diff_hash": "TEXT",
        "environment_lock_hash": "TEXT NOT NULL DEFAULT ''",
        "reuse_key": "TEXT NOT NULL DEFAULT ''",
        "model_maturity": "TEXT",
        "diagnostics": "TEXT NOT NULL DEFAULT '[]'",
    }
    for column, declaration in migrations.items():
        if column not in analysis_columns:
            metadata.execute(f"ALTER TABLE analysis_runs ADD COLUMN {column} {declaration}")


@dataclass(slots=True)
class LocalStore:
    _root: Path
    _mode: DataMode
    _metadata: sqlite3.Connection
    _query: duckdb.DuckDBPyConnection
    _writer_lock: IO[bytes] | None = None
    _scratch_bound: bool = True
    _closed: bool = False

    def preflight_full_snapshot_import(
        self,
        input_bytes: int,
        record_count: int = 0,
        *,
        writer_bound: bool = True,
        scratch_bound: bool = True,
    ) -> CapacityCheck:
        active = self._metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone()
        active_bytes = 0
        if active is not None:
            try:
                for filename in _SNAPSHOT_SCHEMAS:
                    active_path = (
                        self._root / _PARQUET_DIRECTORY / "snapshots" / str(active[0]) / filename
                    )
                    escaped = str(active_path).replace("'", "''")
                    uncompressed = self._query.execute(
                        f"SELECT sum(total_uncompressed_size) FROM parquet_metadata('{escaped}')"
                    ).fetchone()
                    active_bytes += max(
                        active_path.stat().st_blocks * 512,
                        (
                            0
                            if uncompressed is None or uncompressed[0] is None
                            else int(uncompressed[0])
                        ),
                    )
            except (OSError, duckdb.Error):
                return probe_capacity(self._root, None)
        try:
            fragment_size = os.statvfs(self._root).f_frsize
        except OSError:
            return probe_capacity(self._root, None)
        estimate = full_snapshot_import_estimate(
            input_bytes,
            active_bytes,
            fragment_size,
            record_count=record_count,
            writer_bound=writer_bound,
            scratch_bound=scratch_bound and self._scratch_bound,
        )
        return probe_capacity(self._root, estimate)

    def load_identity(self) -> StoreIdentity:
        columns = {
            str(row[1])
            for row in self._metadata.execute("PRAGMA table_info(store_identity)").fetchall()
        }
        store_id = "store_id" if "store_id" in columns else "NULL"
        binding = "person_binding" if "person_binding" in columns else "'unbound'"
        row = self._metadata.execute(
            f"SELECT {store_id}, mode, {binding}, schema_version "
            "FROM store_identity WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise StoreConfigurationError("Datenspeicheridentität fehlt.")
        return StoreIdentity(
            store_id=None if row[0] is None else StoreId(str(row[0])),
            mode=DataMode(str(row[1])),
            person_binding=PersonBindingStatus(str(row[2])),
            schema_version=str(row[3]),
        )

    def initialize_identity(self, *, confirm_existing_person: bool) -> StoreIdentity:
        self._require_writer()
        identity = self.load_identity()
        if identity.store_id is not None:
            return identity
        has_personal_data = bool(
            self._metadata.execute(
                "SELECT 1 FROM imports "
                "WHERE status IN ('committed', 'duplicate', 'quarantined') LIMIT 1"
            ).fetchone()
        )
        if self._mode is DataMode.REAL and has_personal_data and not confirm_existing_person:
            raise StoreConfigurationError(
                "Bestehende reale Daten benötigen die Einpersonenbestätigung."
            )
        binding = (
            PersonBindingStatus.BOUND
            if self._mode is DataMode.REAL and has_personal_data
            else PersonBindingStatus.UNBOUND
        )
        store_id = uuid4().hex
        active = self._metadata.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'active_snapshot'"
        ).fetchone()
        if active is not None:
            snapshot = self._metadata.execute(
                "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
            ).fetchone()
            if snapshot is not None:
                try:
                    candidate = json.loads(
                        (
                            self._root
                            / _PARQUET_DIRECTORY
                            / "snapshots"
                            / str(snapshot[0])
                            / "manifest.json"
                        ).read_bytes()
                    )["store_id"]
                    store_id = str(StoreId(str(candidate)))
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                    pass
        if identity.schema_version in {"1", "1.0"}:
            backup_directory = self._root / "migration-backups"
            backup_directory.mkdir(exist_ok=True)
            backup_path = backup_directory / (
                f"metadata-v1.0-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
            )
            with sqlite3.connect(backup_path) as backup:
                self._metadata.backup(backup)
        with self._metadata:
            _ensure_current_tables(self._metadata)
            self._metadata.execute("ALTER TABLE store_identity RENAME TO legacy_store_identity")
            self._metadata.execute(_STORE_IDENTITY_DDL)
            self._metadata.execute(
                "INSERT INTO store_identity VALUES (1, ?, ?, ?, ?)",
                (self._mode.value, _STORE_SCHEMA_VERSION, store_id, binding.value),
            )
            self._metadata.execute("DROP TABLE legacy_store_identity")
        return self.load_identity()

    @classmethod
    def open(cls, root: Path, mode: DataMode) -> Self:
        try:
            writer_lock = cls._try_writer_lock(root)
        except StoreError:
            if not root.exists():
                raise
            return cls._open(root, mode, initialize=False)
        if writer_lock is None:
            return cls._open(root, mode, initialize=False)
        store: Self | None = None
        try:
            store = cls._open(root, mode, initialize=True)
            store._recover_imports()
            store._validate_store()
            return store
        except Exception:
            if store is not None:
                store.close()
            raise
        finally:
            writer_lock.close()

    @classmethod
    def open_writer(cls, root: Path, mode: DataMode) -> Self:
        writer_lock = cls._try_writer_lock(root)
        if writer_lock is None:
            raise StoreBusyError("Datenspeicher wird bereits beschrieben.")
        store: Self | None = None
        try:
            store = cls._open(root, mode, initialize=True, writer_lock=writer_lock)
            store._recover_imports()
            store._validate_store()
            return store
        except Exception:
            if store is None:
                writer_lock.close()
            else:
                store.close()
            raise

    @classmethod
    def _open(
        cls,
        root: Path,
        mode: DataMode,
        *,
        initialize: bool,
        writer_lock: IO[bytes] | None = None,
    ) -> Self:
        if not isinstance(mode, DataMode):
            raise StoreConfigurationError("Datenmodus muss 'synthetic' oder 'real' sein.")

        metadata: sqlite3.Connection | None = None
        try:
            metadata_path = root / _METADATA_FILE
            metadata = sqlite3.connect(
                metadata_path if initialize else f"{metadata_path.resolve().as_uri()}?mode=ro",
                uri=not initialize,
            )
            metadata.execute("PRAGMA foreign_keys = ON")
            identity_table = metadata.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_identity'"
            ).fetchone()
            existing_identity = (
                None
                if identity_table is None
                else metadata.execute(
                    "SELECT mode, schema_version FROM store_identity WHERE singleton = 1"
                ).fetchone()
            )
            if existing_identity is not None:
                existing_mode, existing_schema = map(str, existing_identity)
                if existing_mode != mode.value or existing_schema not in {
                    "1",
                    "1.0",
                    "1.1",
                    "1.2",
                    str(_STORE_SCHEMA_VERSION),
                }:
                    raise StoreConfigurationError(
                        "Datenspeicher gehört zu einem anderen Modus oder Schema."
                    )
            legacy_identity = existing_identity is not None and str(existing_identity[1]) != str(
                _STORE_SCHEMA_VERSION
            )
            if initialize and not legacy_identity:
                (root / _PARQUET_DIRECTORY).mkdir(exist_ok=True)
                if identity_table is None:
                    metadata.execute(_STORE_IDENTITY_DDL)
                _ensure_current_tables(metadata)
                metadata.commit()
            identity_columns = {
                str(row[1])
                for row in metadata.execute("PRAGMA table_info(store_identity)").fetchall()
            }
            store_id = "store_id" if "store_id" in identity_columns else "NULL"
            binding = "person_binding" if "person_binding" in identity_columns else "'unbound'"
            identity = metadata.execute(
                f"SELECT mode, schema_version, {store_id}, {binding} "
                "FROM store_identity WHERE singleton = 1"
            ).fetchone()
            if identity is None:
                if not initialize:
                    raise StoreConfigurationError("Datenspeicher ist noch nicht initialisiert.")
                metadata.execute(
                    "INSERT INTO store_identity"
                    "(singleton, mode, schema_version, store_id, person_binding) "
                    "VALUES (1, ?, ?, ?, 'unbound')",
                    (mode.value, _STORE_SCHEMA_VERSION, uuid4().hex),
                )
                metadata.commit()
            elif (
                str(identity[0]) != mode.value
                or str(identity[1]) not in {"1", "1.0", "1.1", "1.2", str(_STORE_SCHEMA_VERSION)}
                or (
                    str(identity[1]) == str(_STORE_SCHEMA_VERSION)
                    and (identity[2] is None or not str(identity[2]))
                )
            ):
                raise StoreConfigurationError(
                    "Datenspeicher gehört zu einem anderen Modus oder Schema."
                )
            query_path = root / _QUERY_FILE
            if initialize and not query_path.exists():
                duckdb.connect(str(query_path)).close()
            query = duckdb.connect(str(query_path), config={"access_mode": "READ_ONLY"})
            scratch_bound = True
            if writer_lock is not None:
                temporary = str(root / ".duckdb-temp").replace("'", "''")
                try:
                    query.execute(f"SET temp_directory = '{temporary}'")
                except duckdb.Error:
                    scratch_bound = False
        except StoreError:
            if metadata is not None:
                metadata.close()
            raise
        except (OSError, sqlite3.Error, duckdb.Error) as error:
            if metadata is not None:
                metadata.close()
            raise StoreError("Datenspeicher konnte nicht geöffnet werden.") from error

        return cls(
            _root=root,
            _mode=mode,
            _metadata=metadata,
            _query=query,
            _writer_lock=writer_lock,
            _scratch_bound=scratch_bound,
        )

    @staticmethod
    def _try_writer_lock(root: Path) -> IO[bytes] | None:
        try:
            root.mkdir(parents=True, exist_ok=True)
            writer_lock = (root / _WRITER_LOCK_FILE).open("a+b")
        except OSError as error:
            raise StoreError("Writer-Lock konnte nicht geöffnet werden.") from error
        try:
            fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            writer_lock.close()
            return None
        return writer_lock

    def start_import(
        self,
        *,
        operation_id: OperationId,
        import_id: ImportId,
        snapshot_id: SnapshotId,
    ) -> None:
        self._require_writer()
        staging = self._root / "staging" / str(import_id)
        try:
            staging.mkdir(parents=True)
            self._write_manifest(
                staging,
                operation_id=operation_id,
                import_id=import_id,
                snapshot_id=snapshot_id,
                status="running",
            )
            self._metadata.execute("BEGIN IMMEDIATE")
            self._metadata.execute(
                "INSERT INTO imports VALUES (?, ?, ?, 'running', ?, 0, 0, ?, '')",
                (
                    str(import_id),
                    str(operation_id),
                    "",
                    str(snapshot_id),
                    datetime.now().astimezone().isoformat(),
                ),
            )
        except (OSError, sqlite3.Error) as error:
            self._metadata.rollback()
            shutil.rmtree(staging, ignore_errors=True)
            raise StoreError("Import-Staging konnte nicht angelegt werden.") from error

    def reject_import(self, import_id: ImportId, package_hash: str) -> None:
        self._require_writer()
        self._metadata.execute(
            """
            UPDATE imports
            SET package_hash = ?, status = 'rejected', diagnostics = 'invalid_health_export'
            WHERE import_id = ?
            """,
            (package_hash, str(import_id)),
        )
        self._metadata.commit()
        staging = self._root / "staging" / str(import_id)
        if staging.exists():
            self._remove_tree(staging)

    def mark_import_reading(self, import_id: ImportId) -> None:
        self._require_writer()
        self._metadata.execute(
            "UPDATE imports SET diagnostics = 'reading_package' WHERE import_id = ?",
            (str(import_id),),
        )
        manifest_path = self._root / "staging" / str(import_id) / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "reading_package"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
        except (OSError, TypeError, json.JSONDecodeError) as error:
            raise StoreError("Import-Manifest konnte nicht aktualisiert werden.") from error

    def quarantine_import(
        self, import_id: ImportId, snapshot_id: SnapshotId, diagnostic: str
    ) -> None:
        self._require_writer()
        active = self._metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone()
        paths = [(self._root / "staging" / str(import_id), "staging")]
        if active is None or str(active[0]) != str(snapshot_id):
            paths.append(
                (
                    self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id),
                    "snapshot",
                )
            )
        self._retain_import_quarantine(str(import_id), paths, diagnostic)
        with self._metadata:
            self._metadata.execute(
                "UPDATE imports SET status = 'quarantined', diagnostics = ? "
                "WHERE import_id = ? AND status = 'running'",
                (diagnostic, str(import_id)),
            )
            self._bind_person()

    def publish_import(
        self,
        *,
        operation_id: OperationId,
        import_id: ImportId,
        package_hash: str,
        snapshot_id: SnapshotId,
        export_id: str,
        export_date: datetime | None,
        records: tuple[CanonicalHealthRecord, ...],
        governing_export_id: str,
        resolve_sources: SourceResolver,
    ) -> PublishImportResult:
        self._require_open()
        self._require_writer()
        try:
            return self._publish_import(
                operation_id=operation_id,
                import_id=import_id,
                package_hash=package_hash,
                snapshot_id=snapshot_id,
                export_id=export_id,
                export_date=export_date,
                records=records,
                governing_export_id=governing_export_id,
                resolve_sources=resolve_sources,
            )
        except (OSError, sqlite3.Error, duckdb.Error) as error:
            raise StoreError("Health-Import konnte nicht veröffentlicht werden.") from error

    def _publish_import(
        self,
        *,
        operation_id: OperationId,
        import_id: ImportId,
        package_hash: str,
        snapshot_id: SnapshotId,
        export_id: str,
        export_date: datetime | None,
        records: tuple[CanonicalHealthRecord, ...],
        governing_export_id: str,
        resolve_sources: SourceResolver,
    ) -> PublishImportResult:
        observed_at = datetime.now().astimezone()
        duplicate = self._metadata.execute(
            """
            SELECT active_snapshot.snapshot_id
            FROM active_snapshot
            WHERE singleton = 1 AND (
                EXISTS (
                    SELECT 1 FROM imports
                    WHERE package_hash = ? AND status = 'committed'
                )
                OR EXISTS (SELECT 1 FROM exports WHERE export_id = ?)
            )
            """,
            (package_hash, export_id),
        ).fetchone()
        if duplicate is not None:
            result = self._current_import_result(
                status="duplicate",
                snapshot_id=SnapshotId(str(duplicate[0])),
                record_count=0,
                diagnostics=("identical_package",),
            )
            with self._metadata:
                self._record_import(
                    operation_id=operation_id,
                    import_id=import_id,
                    package_hash=package_hash,
                    snapshot_id=result.snapshot_id,
                    status=result.status,
                    package_record_count=len(records),
                    record_count=0,
                    records=records,
                )
            shutil.rmtree(self._root / "staging" / str(import_id), ignore_errors=True)
            return result

        active = self._metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone()
        staging = self._root / "staging" / str(import_id)
        snapshot = self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id)
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE staged_samples (
                measurement_version_id VARCHAR,
                identity_candidate_id VARCHAR,
                payload_sha256 VARCHAR,
                canonical_type VARCHAR,
                canonical_unit VARCHAR,
                canonical_value DOUBLE,
                source_start_utc VARCHAR,
                source_end_utc VARCHAR,
                source_updated_at_utc VARCHAR,
                source_start_offset_minutes INTEGER,
                source_end_offset_minutes INTEGER,
                source_updated_at_offset_minutes INTEGER,
                measurement_local_date DATE,
                source_name VARCHAR,
                source_version VARCHAR,
                device VARCHAR,
                original_value DOUBLE,
                original_unit VARCHAR,
                strong_source_id_hash VARCHAR
            )
            """
        )
        self._query.executemany(
            """
            INSERT INTO staged_samples
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(record.measurement_version_id),
                    str(record.logical_measurement_id),
                    hashlib.sha256(
                        json.dumps(
                            {
                                "type": record.data_type.value,
                                "unit": record.unit.value,
                                "value": record.value,
                                "start": record.source_start.isoformat(),
                                "end": record.source_end.isoformat(),
                                "source": record.provenance.source_name,
                                "device": record.provenance.device,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                    record.data_type.value,
                    record.unit.value,
                    record.value,
                    record.source_start.astimezone(UTC).isoformat(),
                    record.source_end.astimezone(UTC).isoformat(),
                    record.source_updated_at.astimezone(UTC).isoformat(),
                    _utc_offset_minutes(record.source_start),
                    _utc_offset_minutes(record.source_end),
                    _utc_offset_minutes(record.source_updated_at),
                    record.measurement_local_day,
                    record.provenance.source_name,
                    record.provenance.source_version,
                    record.provenance.device,
                    record.provenance.original_value,
                    record.provenance.original_unit,
                    record.provenance.strong_source_id_hash,
                )
                for record in records
            ],
        )
        if active is None:
            self._query.execute(
                """
                CREATE OR REPLACE TEMP TABLE combined_samples AS
                SELECT *, 1 AS source_priority FROM staged_samples
                QUALIFY row_number() OVER (
                    PARTITION BY measurement_version_id ORDER BY source_priority
                ) = 1
                """
            )
            previous_count = 0
        else:
            current_path = (
                self._root
                / _PARQUET_DIRECTORY
                / "snapshots"
                / str(active[0])
                / "measurement_versions.parquet"
            )
            escaped_current = str(current_path).replace("'", "''")
            previous_row = self._query.execute(
                f"SELECT count(*) FROM read_parquet('{escaped_current}')"
            ).fetchone()
            assert previous_row is not None
            previous_count = int(previous_row[0])
            self._query.execute(
                f"""
                CREATE OR REPLACE TEMP TABLE combined_samples AS
                SELECT * FROM (
                    SELECT *, 0 AS source_priority FROM read_parquet('{escaped_current}')
                    UNION ALL BY NAME
                    SELECT *, 1 AS source_priority FROM staged_samples
                )
                QUALIFY row_number() OVER (
                    PARTITION BY measurement_version_id ORDER BY source_priority
                ) = 1
                """
            )
        count_row = self._query.execute(
            "SELECT count(*), count(DISTINCT identity_candidate_id) FROM combined_samples"
        ).fetchone()
        _allocation_checkpoint(self._root, "combined")
        assert count_row is not None
        version_count, logical_count = map(int, count_row)
        new_record_count = version_count - previous_count

        audit_position = int(
            self._metadata.execute(
                "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
            ).fetchone()[0]
        )
        manifest_sha256 = self._stage_snapshot(
            staging,
            operation_id=operation_id,
            snapshot_id=snapshot_id,
            parent_snapshot_id=None if active is None else SnapshotId(str(active[0])),
            export_id=export_id,
            export_date=export_date,
            governing_export_id=governing_export_id,
            records=records,
            audit_position=audit_position,
            resolve_sources=resolve_sources,
        )
        _allocation_checkpoint(self._root, "staged")
        _publication_fault_point(self._root, "import.before_snapshot_move/v1")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(snapshot)
        _publication_fault_point(self._root, "import.after_snapshot_move/v1")
        completed_at = datetime.now(UTC).isoformat()
        audit_event_id = uuid4().hex
        with self._metadata:
            self._metadata.execute(
                "INSERT INTO write_operations VALUES (?, 'import_health_export', ?, ?, "
                "'committed', 1)",
                (str(operation_id), observed_at.astimezone(UTC).isoformat(), completed_at),
            )
            self._metadata.execute(
                "INSERT INTO exports VALUES (?, ?, ?, ?)",
                (
                    export_id,
                    package_hash,
                    None if export_date is None else export_date.astimezone(UTC).isoformat(),
                    "unordered" if export_date is None else "ordered",
                ),
            )
            self._record_import(
                operation_id=operation_id,
                import_id=import_id,
                package_hash=package_hash,
                snapshot_id=snapshot_id,
                status="committed",
                package_record_count=len(records),
                record_count=new_record_count,
                records=records,
            )
            self._metadata.execute(
                "INSERT INTO dataset_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(snapshot_id),
                    _SNAPSHOT_SCHEMA_VERSION,
                    manifest_sha256,
                    str(operation_id),
                    None if active is None else str(active[0]),
                    completed_at,
                ),
            )
            self._metadata.execute(
                "INSERT INTO snapshot_activations VALUES (?, ?, ?, ?, 'import', ?)",
                (
                    uuid4().hex,
                    str(operation_id),
                    str(snapshot_id),
                    None if active is None else str(active[0]),
                    completed_at,
                ),
            )
            self._metadata.execute(
                """
                INSERT INTO active_snapshot(singleton, snapshot_id) VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET snapshot_id = excluded.snapshot_id
                """,
                (str(snapshot_id),),
            )
            self._metadata.execute(
                "INSERT INTO audit_events VALUES (?, ?, ?, 'import_published', ?)",
                (audit_position, audit_event_id, str(operation_id), completed_at),
            )
            self._metadata.execute(
                "INSERT INTO import_publications VALUES (?, ?, ?)",
                (audit_event_id, str(import_id), str(snapshot_id)),
            )
            self._bind_person()
            _allocation_checkpoint(self._root, "activated")
            _publication_fault_point(self._root, "import.before_sqlite_commit/v1")
        return PublishImportResult(
            status="committed",
            snapshot_id=snapshot_id,
            record_count=new_record_count,
            logical_measurement_count=logical_count,
            measurement_version_count=version_count,
            source_occurrence_count=self._snapshot_occurrence_count(snapshot_id),
        )

    def _stage_snapshot(
        self,
        directory: Path,
        *,
        operation_id: OperationId,
        snapshot_id: SnapshotId,
        parent_snapshot_id: SnapshotId | None,
        export_id: str,
        export_date: datetime | None,
        governing_export_id: str,
        records: tuple[CanonicalHealthRecord, ...],
        audit_position: int,
        resolve_sources: SourceResolver,
    ) -> str:
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE staged_occurrences (
                occurrence_id VARCHAR,
                export_id VARCHAR,
                export_ordinal BIGINT,
                identity_candidate_id VARCHAR,
                measurement_version_id VARCHAR,
                mapping_rule_version_id VARCHAR,
                occurrence_fingerprint VARCHAR
            )
            """
        )
        self._query.executemany(
            "INSERT INTO staged_occurrences VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    hashlib.sha256(f"{export_id}:{ordinal}".encode()).hexdigest(),
                    export_id,
                    ordinal,
                    str(record.logical_measurement_id),
                    str(record.measurement_version_id),
                    _MAPPING_RULE_VERSION,
                    hashlib.sha256(
                        f"{export_id}:{ordinal}:{record.measurement_version_id}".encode()
                    ).hexdigest(),
                )
                for ordinal, record in enumerate(records, start=1)
            ],
        )
        if parent_snapshot_id is None:
            self._query.execute(
                "CREATE OR REPLACE TEMP TABLE source_occurrences AS "
                "SELECT * FROM staged_occurrences"
            )
        else:
            previous = (
                self._root
                / _PARQUET_DIRECTORY
                / "snapshots"
                / str(parent_snapshot_id)
                / "source_occurrences.parquet"
            )
            escaped_previous = str(previous).replace("'", "''")
            self._query.execute(
                f"""
                CREATE OR REPLACE TEMP TABLE source_occurrences AS
                SELECT * FROM (
                    SELECT * FROM read_parquet('{escaped_previous}')
                    UNION ALL BY NAME
                    SELECT * FROM staged_occurrences
                )
                QUALIFY row_number() OVER (
                    PARTITION BY occurrence_id ORDER BY export_id, export_ordinal
                ) = 1
                """
            )
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE measurement_versions AS
            SELECT * EXCLUDE(source_priority) FROM combined_samples
            """
        )
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE export_order (
                export_id VARCHAR,
                export_date_utc VARCHAR
            )
            """
        )
        export_rows = [
            (str(row[0]), None if row[1] is None else str(row[1]))
            for row in self._metadata.execute(
                "SELECT export_id, export_date_utc FROM exports"
            ).fetchall()
        ]
        export_rows.append(
            (
                export_id,
                None if export_date is None else export_date.astimezone(UTC).isoformat(),
            )
        )
        self._query.executemany("INSERT INTO export_order VALUES (?, ?)", export_rows)
        previous_measurements: tuple[ResolvedMeasurement, ...] = ()
        previous_review_cases: tuple[OpenDataReviewCase, ...] = ()
        if parent_snapshot_id is not None:
            previous_directory = (
                self._root / _PARQUET_DIRECTORY / "snapshots" / str(parent_snapshot_id)
            )
            previous_resolved = str(previous_directory / "resolved_measurements.parquet").replace(
                "'", "''"
            )
            previous_reviews = str(previous_directory / "open_review_cases.parquet").replace(
                "'", "''"
            )
            previous_measurements = tuple(
                ResolvedMeasurement(*row)
                for row in self._query.execute(
                    f"SELECT * FROM read_parquet('{previous_resolved}')"
                ).fetchall()
            )
            previous_review_cases = tuple(
                OpenDataReviewCase(
                    review_case_id=str(row[0]),
                    kind=cast(
                        Literal[
                            "plausibility",
                            "continued_override",
                            "suspected_source_deletion",
                            "source_conflict",
                        ],
                        row[1],
                    ),
                    logical_measurement_id=(
                        None if row[2] is None else LogicalMeasurementId(str(row[2]))
                    ),
                    measurement_version_id=(
                        None if row[3] is None else MeasurementVersionId(str(row[3]))
                    ),
                    rule_version_id=None if row[4] is None else str(row[4]),
                    evidence_fingerprint=str(row[5]),
                )
                for row in self._query.execute(
                    f"SELECT * FROM read_parquet('{previous_reviews}')"
                ).fetchall()
            )

        occurrence_facts = tuple(
            SourceOccurrenceFact(str(row[0]), int(row[1]), str(row[2]), str(row[3]))
            for row in self._query.execute(
                "SELECT export_id, export_ordinal, identity_candidate_id, "
                "measurement_version_id FROM source_occurrences"
            ).fetchall()
        )
        version_facts = tuple(
            MeasurementVersionFact(
                measurement_version_id=str(row[0]),
                logical_measurement_id=str(row[1]),
                canonical_type=str(row[2]),
                canonical_unit=str(row[3]),
                canonical_value=float(row[4]),
                source_start_utc=str(row[5]),
                source_end_utc=str(row[6]),
                source_name=str(row[7]),
                device=str(row[8]),
                strong_source_id_hash=None if row[9] is None else str(row[9]),
            )
            for row in self._query.execute(
                "SELECT measurement_version_id, identity_candidate_id, canonical_type, "
                "canonical_unit, canonical_value, source_start_utc, source_end_utc, "
                "source_name, device, strong_source_id_hash FROM measurement_versions"
            ).fetchall()
        )
        export_facts = tuple(
            ExportFact(str(row[0]), None if row[1] is None else datetime.fromisoformat(str(row[1])))
            for row in self._query.execute(
                "SELECT export_id, export_date_utc FROM export_order"
            ).fetchall()
        )
        resolution = resolve_sources(
            occurrences=occurrence_facts,
            versions=version_facts,
            exports=export_facts,
            previous_measurements=previous_measurements,
            previous_review_cases=previous_review_cases,
            governing_export_id=governing_export_id,
        )
        self._query.execute(
            "CREATE OR REPLACE TEMP TABLE resolved_measurements ("
            "logical_measurement_id VARCHAR, selected_measurement_version_id VARCHAR, "
            "disposition VARCHAR, effective_value DOUBLE, canonical_unit VARCHAR, "
            "effective_value_source VARCHAR, effective_decision_id VARCHAR, "
            "correction_decision_id VARCHAR, source_deletion_decision_id VARCHAR, "
            "conflict_resolution_decision_id VARCHAR)"
        )
        self._query.executemany(
            "INSERT INTO resolved_measurements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.logical_measurement_id,
                    item.selected_measurement_version_id,
                    item.disposition,
                    item.effective_value,
                    item.canonical_unit,
                    item.effective_value_source,
                    item.effective_decision_id,
                    item.correction_decision_id,
                    item.source_deletion_decision_id,
                    item.conflict_resolution_decision_id,
                )
                for item in resolution.measurements
            ],
        )
        self._query.execute(
            "CREATE OR REPLACE TEMP TABLE open_review_cases ("
            "review_case_id VARCHAR, case_kind VARCHAR, logical_measurement_id VARCHAR, "
            "measurement_version_id VARCHAR, rule_version_id VARCHAR, "
            "evidence_fingerprint VARCHAR)"
        )
        review_rows = [
            (
                item.review_case_id,
                item.kind,
                None if item.logical_measurement_id is None else str(item.logical_measurement_id),
                None if item.measurement_version_id is None else str(item.measurement_version_id),
                item.rule_version_id,
                item.evidence_fingerprint,
            )
            for item in resolution.review_cases
        ]
        if review_rows:
            self._query.executemany(
                "INSERT INTO open_review_cases VALUES (?, ?, ?, ?, ?, ?)", review_rows
            )

        entries: list[dict[str, int | str]] = []
        for filename in sorted(_SNAPSHOT_SCHEMAS):
            table = filename.removesuffix(".parquet")
            path = directory / filename
            escaped = str(path).replace("'", "''")
            self._query.execute(f"COPY (SELECT * FROM {table}) TO '{escaped}' (FORMAT PARQUET)")
            description = tuple(
                (str(row[0]), str(row[1]))
                for row in self._query.execute(
                    f"DESCRIBE SELECT * FROM read_parquet('{escaped}')"
                ).fetchall()
            )
            if description != _SNAPSHOT_SCHEMAS[filename]:
                raise StoreError("Staging-Snapshot besitzt ein unerwartetes Schema.")
            row = self._query.execute(f"SELECT count(*) FROM read_parquet('{escaped}')").fetchone()
            assert row is not None
            entries.append(
                {
                    "name": filename,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "allocated_bytes": path.stat().st_blocks * 512,
                    "row_count": int(row[0]),
                    "parquet_schema_fingerprint": hashlib.sha256(
                        json.dumps(description, separators=(",", ":")).encode()
                    ).hexdigest(),
                }
            )

        count_row = self._query.execute(
            """
            SELECT
                (SELECT count(DISTINCT export_id) FROM source_occurrences),
                (SELECT count(*) FROM source_occurrences),
                (SELECT count(*) FROM measurement_versions),
                (SELECT count(*) FROM resolved_measurements),
                (SELECT count(*) FROM resolved_measurements
                   WHERE disposition LIKE 'included%'),
                (SELECT count(*) FROM resolved_measurements
                   WHERE disposition LIKE 'excluded%'),
                (SELECT count(*) FROM open_review_cases)
            """
        ).fetchone()
        assert count_row is not None
        (
            export_count,
            occurrence_count,
            version_count,
            logical_count,
            included_count,
            excluded_count,
            open_count,
        ) = map(int, count_row)
        store_row = self._metadata.execute(
            "SELECT store_id FROM store_identity WHERE singleton = 1"
        ).fetchone()
        if store_row is None or store_row[0] is None:
            raise StoreError("Datenspeicheridentität fehlt.")
        manifest = {
            "snapshot_schema_version": _SNAPSHOT_SCHEMA_VERSION,
            "snapshot_id": str(snapshot_id),
            "store_id": str(store_row[0]),
            "created_at_utc": datetime.now(UTC).isoformat(),
            "created_by_operation_id": str(operation_id),
            "parent_snapshot_id": (None if parent_snapshot_id is None else str(parent_snapshot_id)),
            "resolution_basis": {
                "audit_max_position": audit_position,
                "governing_export_id": governing_export_id,
                "identity_rule_version_id": _IDENTITY_RULE_VERSION,
                "mapping_rule_version_id": _MAPPING_RULE_VERSION,
            },
            "files": entries,
            "validation_counts": {
                "exports": export_count,
                "source_occurrences": occurrence_count,
                "measurement_versions": version_count,
                "logical_measurements": logical_count,
                "included": included_count,
                "excluded": excluded_count,
                "open_review_cases": open_count,
            },
        }
        manifest_bytes = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        (directory / "manifest.json").write_bytes(manifest_bytes)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        self._validate_snapshot(directory, str(snapshot_id), manifest_sha256)
        return manifest_sha256

    def _validate_store(self) -> None:
        if self._metadata.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise StoreError("SQLite-Integritätsprüfung fehlgeschlagen.")
        if self._metadata.execute("PRAGMA foreign_key_check").fetchall():
            raise StoreError("SQLite-Fremdschlüsselprüfung fehlgeschlagen.")
        identity_columns = {
            str(row[1])
            for row in self._metadata.execute("PRAGMA table_info(store_identity)").fetchall()
        }
        if "store_id" not in identity_columns:
            return
        identity = self._metadata.execute(
            "SELECT store_id FROM store_identity WHERE singleton = 1"
        ).fetchone()
        if identity is None or identity[0] is None:
            return
        tables = {
            str(row[0])
            for row in self._metadata.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "audit_events" in tables:
            audit = self._metadata.execute(
                """
                SELECT count(*), COALESCE(MIN(audit_position), 1),
                       COALESCE(MAX(audit_position), 0),
                       count(import_publications.audit_event_id)
                       + count(metadata_tombstones.audit_event_id)
                FROM audit_events
                LEFT JOIN import_publications USING (audit_event_id)
                LEFT JOIN metadata_tombstones USING (audit_event_id)
                """
            ).fetchone()
            assert audit is not None
            count, minimum, maximum, payload_count = map(int, audit)
            if minimum != 1 or maximum != count or payload_count != count:
                raise StoreError("Auditfolge ist nicht lückenlos oder vollständig.")
        if "dataset_snapshots" not in tables:
            return
        snapshots = self._metadata.execute(
            """
            SELECT snapshot_id, manifest_sha256, snapshot_schema_version,
                   created_by_operation_id, parent_snapshot_id
            FROM dataset_snapshots
            """
        ).fetchall()
        for snapshot_id, manifest_sha256, schema_version, operation_id, parent_id in snapshots:
            snapshot_id = str(snapshot_id)
            self._validate_snapshot(
                self._root / _PARQUET_DIRECTORY / "snapshots" / snapshot_id,
                snapshot_id,
                str(manifest_sha256),
            )
            manifest = json.loads(
                (
                    self._root / _PARQUET_DIRECTORY / "snapshots" / snapshot_id / "manifest.json"
                ).read_bytes()
            )
            if (
                manifest["snapshot_schema_version"] != schema_version
                or manifest["created_by_operation_id"] != operation_id
                or manifest["parent_snapshot_id"] != parent_id
            ):
                raise StoreError("Snapshot-Katalog und Manifest widersprechen sich.")

    def _validate_snapshot(self, directory: Path, snapshot_id: str, manifest_sha256: str) -> None:
        try:
            manifest_bytes = (directory / "manifest.json").read_bytes()
            manifest = json.loads(manifest_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise StoreError("Snapshot-Manifest ist nicht lesbar.") from error
        store_row = self._metadata.execute(
            "SELECT store_id FROM store_identity WHERE singleton = 1"
        ).fetchone()
        resolution_basis = manifest.get("resolution_basis") if isinstance(manifest, dict) else None
        validation_counts = (
            manifest.get("validation_counts") if isinstance(manifest, dict) else None
        )
        if (
            not isinstance(manifest, dict)
            or store_row is None
            or manifest.get("store_id") != store_row[0]
            or manifest_bytes
            != json.dumps(
                manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode()
            or hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256
            or set(manifest)
            != {
                "snapshot_schema_version",
                "snapshot_id",
                "store_id",
                "created_at_utc",
                "created_by_operation_id",
                "parent_snapshot_id",
                "resolution_basis",
                "files",
                "validation_counts",
            }
            or manifest["snapshot_schema_version"] != _SNAPSHOT_SCHEMA_VERSION
            or manifest["snapshot_id"] != snapshot_id
            or not _is_lower_hex(manifest["snapshot_id"], 32)
            or not _is_lower_hex(manifest["store_id"], 32)
            or not _is_timestamp(manifest["created_at_utc"])
            or not _is_lower_hex(manifest["created_by_operation_id"], 32)
            or (
                manifest["parent_snapshot_id"] is not None
                and not _is_lower_hex(manifest["parent_snapshot_id"], 32)
            )
            or not isinstance(resolution_basis, dict)
            or set(resolution_basis)
            != {
                "audit_max_position",
                "governing_export_id",
                "identity_rule_version_id",
                "mapping_rule_version_id",
            }
            or type(resolution_basis["audit_max_position"]) is not int
            or resolution_basis["audit_max_position"] < 1
            or not _is_lower_hex(resolution_basis["governing_export_id"], 64)
            or not isinstance(resolution_basis["identity_rule_version_id"], str)
            or not resolution_basis["identity_rule_version_id"]
            or resolution_basis["mapping_rule_version_id"] != _MAPPING_RULE_VERSION
            or not isinstance(validation_counts, dict)
            or set(validation_counts)
            != {
                "exports",
                "source_occurrences",
                "measurement_versions",
                "logical_measurements",
                "included",
                "excluded",
                "open_review_cases",
            }
            or any(type(value) is not int or value < 0 for value in validation_counts.values())
        ):
            raise StoreError("Snapshot-Manifest ist nicht kanonisch oder gültig.")
        files = manifest["files"]
        if (
            not isinstance(files, list)
            or any(not isinstance(entry, dict) for entry in files)
            or tuple(entry.get("name") for entry in files) != tuple(sorted(_SNAPSHOT_SCHEMAS))
        ):
            raise StoreError("Snapshot enthält nicht genau vier geschlossene Dateien.")
        for entry in files:
            if not isinstance(entry, dict) or set(entry) != {
                "name",
                "sha256",
                "allocated_bytes",
                "row_count",
                "parquet_schema_fingerprint",
            }:
                raise StoreError("Snapshot-Dateieintrag ist ungültig.")
            filename = str(entry["name"])
            if (
                not _is_lower_hex(entry["sha256"], 64)
                or not _is_lower_hex(entry["parquet_schema_fingerprint"], 64)
                or type(entry["allocated_bytes"]) is not int
                or entry["allocated_bytes"] < 0
                or type(entry["row_count"]) is not int
                or entry["row_count"] < 0
            ):
                raise StoreError("Snapshot-Dateieintrag ist ungültig.")
            path = directory / filename
            try:
                allocated_bytes = path.stat().st_blocks * 512
                sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as error:
                raise StoreError("Snapshot-Datei fehlt.") from error
            escaped = str(path).replace("'", "''")
            try:
                description = tuple(
                    (str(row[0]), str(row[1]))
                    for row in self._query.execute(
                        f"DESCRIBE SELECT * FROM read_parquet('{escaped}')"
                    ).fetchall()
                )
                row = self._query.execute(
                    f"SELECT count(*) FROM read_parquet('{escaped}')"
                ).fetchone()
            except duckdb.Error as error:
                raise StoreError("Snapshot-Parquet ist nicht lesbar.") from error
            schema_fingerprint = hashlib.sha256(
                json.dumps(description, separators=(",", ":")).encode()
            ).hexdigest()
            if (
                description != _SNAPSHOT_SCHEMAS[filename]
                or row is None
                or int(row[0]) != entry["row_count"]
                or allocated_bytes != entry["allocated_bytes"]
                or sha256 != entry["sha256"]
                or schema_fingerprint != entry["parquet_schema_fingerprint"]
            ):
                raise StoreError("Snapshot-Dateivalidierung fehlgeschlagen.")
        if {path.name for path in directory.iterdir()} != {
            "manifest.json",
            *_SNAPSHOT_SCHEMAS,
        }:
            raise StoreError("Snapshot enthält unerlaubte Artefakte.")
        paths = {
            name.removesuffix(".parquet"): str(directory / name).replace("'", "''")
            for name in _SNAPSHOT_SCHEMAS
        }
        snapshot_decisions = {
            (str(row[0]), str(row[1]))
            for row in self._query.execute(
                f"""
                SELECT correction_decision_id, 'correction'
                FROM read_parquet('{paths["resolved_measurements"]}')
                WHERE correction_decision_id IS NOT NULL
                UNION
                SELECT source_deletion_decision_id, 'source_deletion'
                FROM read_parquet('{paths["resolved_measurements"]}')
                WHERE source_deletion_decision_id IS NOT NULL
                UNION
                SELECT conflict_resolution_decision_id, 'conflict_resolution'
                FROM read_parquet('{paths["resolved_measurements"]}')
                WHERE conflict_resolution_decision_id IS NOT NULL
                UNION
                SELECT effective_decision_id, 'local_exclusion'
                FROM read_parquet('{paths["resolved_measurements"]}')
                WHERE disposition = 'excluded_local'
                """
            ).fetchall()
        }
        has_decision_refs = self._metadata.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'decision_refs'"
        ).fetchone()
        cataloged_decisions = (
            set()
            if has_decision_refs is None
            else {
                (str(row[0]), str(row[1]))
                for row in self._metadata.execute(
                    "SELECT decision_id, decision_kind FROM decision_refs"
                ).fetchall()
            }
        )
        snapshot_rules = {
            (str(row[0]), str(row[1]))
            for row in self._query.execute(
                f"""
                SELECT mapping_rule_version_id, 'mapping'
                FROM read_parquet('{paths["source_occurrences"]}')
                UNION
                SELECT rule_version_id, 'plausibility'
                FROM read_parquet('{paths["open_review_cases"]}')
                WHERE rule_version_id IS NOT NULL
                """
            ).fetchall()
        }
        snapshot_rules.add((str(resolution_basis["identity_rule_version_id"]), "identity"))
        has_rule_refs = self._metadata.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rule_version_refs'"
        ).fetchone()
        cataloged_rules = {
            (_IDENTITY_RULE_VERSION, "identity"),
            (_MAPPING_RULE_VERSION, "mapping"),
        }
        if has_rule_refs is not None:
            cataloged_rules.update(
                (str(row[0]), str(row[1]))
                for row in self._metadata.execute(
                    "SELECT rule_version_id, rule_kind FROM rule_version_refs"
                ).fetchall()
            )
        if not snapshot_decisions <= cataloged_decisions or not snapshot_rules <= cataloged_rules:
            raise StoreError("Snapshot-Entscheidungs- oder Regelreferenz ist nicht geschlossen.")
        invalid = self._query.execute(
            f"""
            WITH
            occurrences AS (
                SELECT * FROM read_parquet('{paths["source_occurrences"]}')
            ),
            versions AS (
                SELECT * FROM read_parquet('{paths["measurement_versions"]}')
            ),
            resolved AS (
                SELECT * FROM read_parquet('{paths["resolved_measurements"]}')
            ),
            reviews AS (
                SELECT * FROM read_parquet('{paths["open_review_cases"]}')
            )
            SELECT
                (SELECT count(*) - count(DISTINCT occurrence_id) FROM occurrences)
              + (SELECT count(*) - count(DISTINCT export_id || ':' || export_ordinal)
                   FROM occurrences)
              + (SELECT count(*) - count(DISTINCT measurement_version_id) FROM versions)
              + (SELECT count(*) - count(DISTINCT logical_measurement_id) FROM resolved)
              + (SELECT count(*) - count(DISTINCT review_case_id) FROM reviews)
              + (SELECT count(*) FROM occurrences
                   WHERE occurrence_id IS NULL
                      OR export_id IS NULL
                      OR export_ordinal IS NULL OR export_ordinal < 1
                      OR identity_candidate_id IS NULL
                      OR measurement_version_id IS NULL
                      OR mapping_rule_version_id IS NULL
                      OR occurrence_fingerprint IS NULL
                      OR NOT regexp_full_match(occurrence_id, '[0-9a-f]{{64}}')
                      OR NOT regexp_full_match(export_id, '[0-9a-f]{{64}}')
                      OR NOT regexp_full_match(identity_candidate_id, '[0-9a-f]{{64}}')
                      OR NOT regexp_full_match(measurement_version_id, '[0-9a-f]{{64}}')
                      OR NOT regexp_full_match(occurrence_fingerprint, '[0-9a-f]{{64}}'))
              + (SELECT count(*) FROM occurrences o LEFT JOIN versions v
                   USING (measurement_version_id)
                   WHERE v.measurement_version_id IS NULL
                      OR o.identity_candidate_id != v.identity_candidate_id)
              + (SELECT count(*) FROM resolved r LEFT JOIN versions v
                   ON v.measurement_version_id = r.selected_measurement_version_id
                   WHERE v.measurement_version_id IS NULL
                      OR v.identity_candidate_id != r.logical_measurement_id
                      OR r.canonical_unit != v.canonical_unit
                      OR (r.disposition = 'included_source'
                          AND r.effective_value != v.canonical_value))
              + (SELECT count(*) FROM versions
                   WHERE measurement_version_id IS NULL
                      OR identity_candidate_id IS NULL
                      OR payload_sha256 IS NULL
                      OR NOT regexp_full_match(measurement_version_id, '[0-9a-f]{{64}}')
                      OR NOT regexp_full_match(identity_candidate_id, '[0-9a-f]{{64}}')
                      OR NOT regexp_full_match(payload_sha256, '[0-9a-f]{{64}}')
                      OR canonical_type IS NULL
                      OR canonical_unit IS NULL
                      OR canonical_value IS NULL OR NOT isfinite(canonical_value)
                      OR original_value IS NULL OR NOT isfinite(original_value)
                      OR source_start_utc IS NULL OR source_end_utc IS NULL
                      OR source_updated_at_utc IS NULL
                      OR source_start_utc > source_end_utc
                      OR measurement_local_date IS NULL
                      OR canonical_type NOT IN ('active_energy', 'apple_resting_heart_rate')
                      OR (canonical_type = 'active_energy' AND canonical_unit != 'kcal')
                      OR (canonical_type = 'apple_resting_heart_rate'
                          AND canonical_unit != 'count/min'))
              + (SELECT count(*) FROM resolved
                   WHERE logical_measurement_id IS NULL
                      OR selected_measurement_version_id IS NULL
                      OR canonical_unit IS NULL
                      OR disposition IS NULL
                      OR effective_value_source IS NULL
                      OR NOT regexp_full_match(logical_measurement_id, '[0-9a-f]{{64}}')
                      OR NOT regexp_full_match(
                          selected_measurement_version_id, '[0-9a-f]{{64}}'
                      )
                      OR (effective_decision_id IS NOT NULL AND NOT regexp_full_match(
                          effective_decision_id, '[0-9a-f]{{32}}'
                      ))
                      OR (correction_decision_id IS NOT NULL AND NOT regexp_full_match(
                          correction_decision_id, '[0-9a-f]{{32}}'
                      ))
                      OR (source_deletion_decision_id IS NOT NULL
                          AND NOT regexp_full_match(
                              source_deletion_decision_id, '[0-9a-f]{{32}}'
                          ))
                      OR (conflict_resolution_decision_id IS NOT NULL
                          AND NOT regexp_full_match(
                              conflict_resolution_decision_id, '[0-9a-f]{{32}}'
                          ))
                      OR NOT (
                       (disposition = 'included_source'
                        AND effective_value IS NOT NULL AND isfinite(effective_value)
                        AND effective_value_source = 'source'
                        AND effective_decision_id IS NULL
                        AND correction_decision_id IS NULL
                        AND source_deletion_decision_id IS NULL)
                       OR
                       (disposition = 'included_correction'
                        AND effective_value IS NOT NULL AND isfinite(effective_value)
                        AND effective_value_source = 'correction'
                        AND effective_decision_id IS NOT NULL
                        AND correction_decision_id = effective_decision_id
                        AND source_deletion_decision_id IS NULL)
                       OR
                       (disposition = 'excluded_local'
                        AND effective_value IS NULL
                        AND effective_value_source = 'none'
                        AND effective_decision_id IS NOT NULL
                        AND correction_decision_id IS NULL
                        AND source_deletion_decision_id IS NULL)
                       OR
                       (disposition = 'excluded_source_deletion'
                        AND effective_value IS NULL
                        AND effective_value_source = 'none'
                        AND effective_decision_id IS NOT NULL
                        AND correction_decision_id IS NULL
                        AND source_deletion_decision_id = effective_decision_id)
                   ))
              + (SELECT count(*) FROM reviews
                   WHERE review_case_id IS NULL OR case_kind IS NULL
                      OR evidence_fingerprint IS NULL
                      OR NOT regexp_full_match(review_case_id, '[0-9a-f]{{32}}')
                      OR NOT regexp_full_match(evidence_fingerprint, '[0-9a-f]{{64}}')
                      OR case_kind NOT IN (
                          'plausibility', 'continued_override',
                          'suspected_source_deletion', 'source_conflict'
                      )
                      OR NOT (
                          (case_kind = 'plausibility'
                           AND measurement_version_id IS NOT NULL
                           AND rule_version_id IS NOT NULL)
                          OR
                          (case_kind = 'continued_override'
                           AND logical_measurement_id IS NOT NULL
                           AND measurement_version_id IS NOT NULL
                           AND rule_version_id IS NULL)
                          OR
                          (case_kind IN ('suspected_source_deletion', 'source_conflict')
                           AND logical_measurement_id IS NOT NULL
                           AND measurement_version_id IS NULL
                           AND rule_version_id IS NULL)
                      ))
              + (SELECT count(*) FROM reviews r LEFT JOIN versions v
                   ON v.measurement_version_id = r.measurement_version_id
                   WHERE r.measurement_version_id IS NOT NULL
                     AND v.measurement_version_id IS NULL)
              + (SELECT count(*) FROM reviews r LEFT JOIN resolved m
                   ON m.logical_measurement_id = r.logical_measurement_id
                   WHERE r.logical_measurement_id IS NOT NULL
                     AND m.logical_measurement_id IS NULL)
            """
        ).fetchone()
        if invalid is None or int(invalid[0]) != 0:
            raise StoreError("Snapshot-ID-Schließung oder Payloadvalidierung fehlgeschlagen.")
        counts = self._query.execute(
            f"""
            SELECT
                (SELECT count(DISTINCT export_id)
                   FROM read_parquet('{paths["source_occurrences"]}')),
                (SELECT count(*) FROM read_parquet('{paths["source_occurrences"]}')),
                (SELECT count(*) FROM read_parquet('{paths["measurement_versions"]}')),
                (SELECT count(*) FROM read_parquet('{paths["resolved_measurements"]}')),
                (SELECT count(*) FROM read_parquet('{paths["resolved_measurements"]}')
                   WHERE disposition LIKE 'included%'),
                (SELECT count(*) FROM read_parquet('{paths["resolved_measurements"]}')
                   WHERE disposition LIKE 'excluded%'),
                (SELECT count(*) FROM read_parquet('{paths["open_review_cases"]}'))
            """
        ).fetchone()
        assert counts is not None
        if manifest["validation_counts"] != dict(
            zip(
                (
                    "exports",
                    "source_occurrences",
                    "measurement_versions",
                    "logical_measurements",
                    "included",
                    "excluded",
                    "open_review_cases",
                ),
                map(int, counts),
                strict=True,
            )
        ):
            raise StoreError("Snapshot-Validierungszähler stimmen nicht.")

    def _record_import(
        self,
        *,
        operation_id: OperationId,
        import_id: ImportId,
        package_hash: str,
        snapshot_id: SnapshotId,
        status: Literal["committed", "duplicate"],
        package_record_count: int,
        record_count: int,
        records: tuple[CanonicalHealthRecord, ...],
    ) -> None:
        self._metadata.execute(
            """
            UPDATE imports
            SET operation_id = ?, package_hash = ?, status = ?, snapshot_id = ?,
                package_record_count = ?, record_count = ?, committed_at = ?, diagnostics = ?
            WHERE import_id = ?
            """,
            (
                str(operation_id),
                package_hash,
                status,
                str(snapshot_id),
                package_record_count,
                record_count,
                datetime.now().astimezone().isoformat(),
                "",
                str(import_id),
            ),
        )
        self._metadata.executemany(
            "INSERT OR IGNORE INTO import_measurement_versions VALUES (?, ?)",
            {(str(import_id), str(record.measurement_version_id)) for record in records},
        )

    def _current_import_result(
        self,
        *,
        status: Literal["duplicate"],
        snapshot_id: SnapshotId,
        record_count: int,
        diagnostics: tuple[str, ...],
    ) -> PublishImportResult:
        parquet_path = (
            self._root
            / _PARQUET_DIRECTORY
            / "snapshots"
            / str(snapshot_id)
            / "measurement_versions.parquet"
        )
        escaped_path = str(parquet_path).replace("'", "''")
        count_row = self._query.execute(
            f"""
            SELECT count(*), count(DISTINCT identity_candidate_id)
            FROM read_parquet('{escaped_path}')
            """
        ).fetchone()
        assert count_row is not None
        version_count, logical_count = map(int, count_row)
        return PublishImportResult(
            status=status,
            snapshot_id=snapshot_id,
            record_count=record_count,
            logical_measurement_count=logical_count,
            measurement_version_count=version_count,
            source_occurrence_count=self._snapshot_occurrence_count(snapshot_id),
            diagnostics=diagnostics,
        )

    def _snapshot_occurrence_count(self, snapshot_id: SnapshotId) -> int:
        path = (
            self._root
            / _PARQUET_DIRECTORY
            / "snapshots"
            / str(snapshot_id)
            / "source_occurrences.parquet"
        )
        escaped = str(path).replace("'", "''")
        row = self._query.execute(f"SELECT count(*) FROM read_parquet('{escaped}')").fetchone()
        assert row is not None
        return int(row[0])

    def load_daily_series(
        self, start_date: date | None, end_date: date | None
    ) -> tuple[DailyHealthSeries, ...]:
        self._require_open()
        row = self._metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return ()
        snapshot = self._root / _PARQUET_DIRECTORY / "snapshots" / str(row[0])
        parquet_path = snapshot / "measurement_versions.parquet"
        resolved_path = snapshot / "resolved_measurements.parquet"
        clauses: list[str] = []
        parameters: list[date] = []
        if start_date is not None:
            clauses.append("measurement_local_date >= ?")
            parameters.append(start_date)
        if end_date is not None:
            clauses.append("measurement_local_date <= ?")
            parameters.append(end_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        escaped_path = str(parquet_path).replace("'", "''")
        escaped_resolved = str(resolved_path).replace("'", "''")
        rows = self._query.execute(
            f"""
            WITH effective_versions AS (
                SELECT versions.* EXCLUDE(canonical_value),
                       resolved.effective_value AS canonical_value
                FROM read_parquet('{escaped_path}') AS versions
                JOIN read_parquet('{escaped_resolved}') AS resolved
                  ON resolved.selected_measurement_version_id = versions.measurement_version_id
                WHERE resolved.disposition IN ('included_source', 'included_correction')
            ), projected_samples AS (
                SELECT * FROM effective_versions WHERE canonical_type = 'active_energy'
                UNION ALL
                SELECT * FROM effective_versions
                WHERE canonical_type = 'apple_resting_heart_rate'
                QUALIFY row_number() OVER (
                    PARTITION BY measurement_local_date
                    ORDER BY source_updated_at_utc DESC,
                             source_version DESC, source_start_utc DESC,
                             measurement_version_id DESC
                ) = 1
            )
            SELECT canonical_type, canonical_unit, measurement_local_date,
                   SUM(canonical_value),
                   list(source_start_utc ORDER BY source_start_utc),
                   list(DISTINCT source_name ORDER BY source_name),
                   list(measurement_version_id ORDER BY source_start_utc),
                   list(source_updated_at_utc ORDER BY source_start_utc),
                   list(source_version ORDER BY source_start_utc),
                   list(source_start_offset_minutes ORDER BY source_start_utc)
            FROM projected_samples
            {where}
            GROUP BY canonical_type, canonical_unit, measurement_local_date
            ORDER BY canonical_type, measurement_local_date
            """,
            parameters,
        ).fetchall()
        grouped: dict[tuple[CanonicalHealthType, CanonicalUnit], list[DailyHealthValue]] = {}
        for (
            data_type,
            unit,
            day,
            value,
            source_starts,
            source_names,
            version_ids,
            source_updated_ats,
            source_versions,
            source_start_offsets,
        ) in rows:
            key = (CanonicalHealthType(data_type), CanonicalUnit(unit))
            grouped.setdefault(key, []).append(
                DailyHealthValue(
                    day=day,
                    value=value,
                    source_starts=tuple(
                        datetime.fromisoformat(item).astimezone(timezone(timedelta(minutes=offset)))
                        for item, offset in zip(source_starts, source_start_offsets, strict=True)
                    ),
                    source_names=tuple(source_names),
                    measurement_version_ids=tuple(
                        MeasurementVersionId(item) for item in version_ids
                    ),
                    source_updated_ats=tuple(
                        datetime.fromisoformat(item) for item in source_updated_ats
                    ),
                    source_versions=tuple(source_versions),
                )
            )
        return tuple(
            DailyHealthSeries(data_type=data_type, unit=unit, values=tuple(values))
            for (data_type, unit), values in grouped.items()
        )

    def load_open_data_review_cases(self) -> tuple[OpenDataReviewCase, ...]:
        self._require_open()
        row = self._metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return ()
        path = (
            self._root
            / _PARQUET_DIRECTORY
            / "snapshots"
            / str(row[0])
            / "open_review_cases.parquet"
        )
        escaped = str(path).replace("'", "''")
        rows = self._query.execute(
            f"""
            SELECT review_case_id, case_kind, logical_measurement_id,
                   measurement_version_id, rule_version_id, evidence_fingerprint
            FROM read_parquet('{escaped}')
            ORDER BY review_case_id
            """
        ).fetchall()
        return tuple(
            OpenDataReviewCase(
                review_case_id=str(review_case_id),
                kind=cast(
                    Literal[
                        "plausibility",
                        "continued_override",
                        "suspected_source_deletion",
                        "source_conflict",
                    ],
                    kind,
                ),
                logical_measurement_id=(
                    None
                    if logical_measurement_id is None
                    else LogicalMeasurementId(str(logical_measurement_id))
                ),
                measurement_version_id=(
                    None
                    if measurement_version_id is None
                    else MeasurementVersionId(str(measurement_version_id))
                ),
                rule_version_id=(None if rule_version_id is None else str(rule_version_id)),
                evidence_fingerprint=str(evidence_fingerprint),
            )
            for (
                review_case_id,
                kind,
                logical_measurement_id,
                measurement_version_id,
                rule_version_id,
                evidence_fingerprint,
            ) in rows
        )

    def load_export_facts(self) -> tuple[ExportFact, ...]:
        self._require_open()
        return tuple(
            ExportFact(
                export_id=str(export_id),
                export_date=(
                    None
                    if export_date_utc is None
                    else datetime.fromisoformat(str(export_date_utc))
                ),
            )
            for export_id, export_date_utc in self._metadata.execute(
                "SELECT export_id, export_date_utc FROM exports ORDER BY export_id"
            ).fetchall()
        )

    def load_active_snapshot_id(self) -> SnapshotId | None:
        self._require_open()
        row = self._metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone()
        return None if row is None else SnapshotId(str(row[0]))

    def load_analysis_input(
        self, start_date: date | None, end_date: date | None
    ) -> tuple[SnapshotId | None, tuple[DailyHealthSeries, ...]]:
        self._require_open()
        row = self._metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return None, ()
        return SnapshotId(str(row[0])), self.load_daily_series(start_date, end_date)

    def find_reusable_resting_hr_analysis(
        self, candidate: AnalysisProvenance
    ) -> AnalysisRunRecord | None:
        self._require_open()
        row = self._metadata.execute(
            """
            SELECT analysis_run_id, result_id, snapshot_id, analysis_definition_id,
                   config_hash, config_schema_version, code_commit, code_dirty,
                   code_diff_hash, environment_lock_hash, model_maturity, diagnostics
            FROM analysis_runs
            WHERE status = 'completed'
              AND reuse_key = ?
            ORDER BY completed_at DESC, rowid DESC
            LIMIT 1
            """,
            (candidate.reuse_key,),
        ).fetchone()
        if row is None:
            return None
        (
            analysis_run_id,
            result_id,
            snapshot_id,
            definition_id,
            config_hash,
            config_schema_version,
            code_commit,
            code_dirty,
            code_diff_hash,
            environment_lock_hash,
            model_maturity,
            diagnostics,
        ) = row
        return AnalysisRunRecord(
            provenance=AnalysisProvenance(
                analysis_run_id=AnalysisRunId(str(analysis_run_id)),
                result_id=AnalysisResultId(str(result_id)),
                snapshot_id=SnapshotId(str(snapshot_id)),
                analysis_definition_id=AnalysisDefinitionId(str(definition_id)),
                config_hash=str(config_hash),
                config_schema_version=str(config_schema_version),
                code_commit=str(code_commit),
                code_dirty=bool(code_dirty),
                code_diff_hash=None if code_diff_hash is None else str(code_diff_hash),
                environment_lock_hash=str(environment_lock_hash),
            ),
            model_maturity=cast(Literal["exploratory", "robust"], model_maturity),
            diagnostics=tuple(cast(list[str], json.loads(str(diagnostics)))),
        )

    def persist_analysis_receipt(
        self,
        *,
        operation_id: OperationId,
        analysis_run_id: AnalysisRunId,
        status: Literal["completed", "reused", "insufficient_data", "unstable"],
        provenance: AnalysisProvenance | None,
        config_json: str,
        diagnostics: tuple[str, ...],
    ) -> None:
        self._require_open()
        self._require_writer()
        with self._metadata:
            self._metadata.execute(
                """
                INSERT INTO analysis_receipts(
                    operation_id, analysis_run_id, status, result_id, snapshot_id,
                    analysis_definition_id, config_json, config_hash, config_schema_version,
                    code_commit, code_dirty, code_diff_hash, environment_lock_hash,
                    diagnostics, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(operation_id),
                    str(analysis_run_id),
                    status,
                    (
                        None
                        if provenance is None or provenance.result_id is None
                        else str(provenance.result_id)
                    ),
                    None if provenance is None else str(provenance.snapshot_id),
                    None if provenance is None else str(provenance.analysis_definition_id),
                    config_json,
                    None if provenance is None else provenance.config_hash,
                    None if provenance is None else provenance.config_schema_version,
                    None if provenance is None else provenance.code_commit,
                    None if provenance is None else provenance.code_dirty,
                    None if provenance is None else provenance.code_diff_hash,
                    None if provenance is None else provenance.environment_lock_hash,
                    json.dumps(diagnostics),
                    datetime.now().astimezone().isoformat(),
                ),
            )

    def persist_resting_hr_analysis(
        self,
        *,
        operation_id: OperationId,
        result: RestingHeartRateAnalysisResult,
        start_date: date | None,
        end_date: date | None,
        config_json: str,
        receipt_diagnostics: tuple[str, ...],
    ) -> None:
        self._require_open()
        self._require_writer()
        provenance = result.provenance
        if provenance is None or provenance.result_id is None:
            raise StoreError("Analyseprovenienz fehlt.")
        result_id = provenance.result_id
        staging = self._root / "analysis-staging" / str(provenance.analysis_run_id)
        destination = self._root / _PARQUET_DIRECTORY / "analyses" / str(result_id)
        with self._metadata:
            self._metadata.execute(
                """
                INSERT INTO analysis_runs(
                    analysis_run_id, operation_id, result_id, snapshot_id,
                    analysis_definition_id, analysis_start_date, analysis_end_date,
                    config_json, config_hash, config_schema_version,
                    code_commit, code_dirty, code_diff_hash, environment_lock_hash, reuse_key,
                    model_maturity, diagnostics,
                    status, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    str(provenance.analysis_run_id),
                    str(operation_id),
                    str(result_id),
                    str(result.snapshot_id),
                    str(result.analysis_definition_id),
                    start_date.isoformat() if start_date else None,
                    end_date.isoformat() if end_date else None,
                    config_json,
                    provenance.config_hash,
                    provenance.config_schema_version,
                    provenance.code_commit,
                    provenance.code_dirty,
                    provenance.code_diff_hash,
                    provenance.environment_lock_hash,
                    provenance.reuse_key,
                    result.model_maturity,
                    json.dumps(receipt_diagnostics),
                    datetime.now().astimezone().isoformat(),
                ),
            )
        staging.mkdir(parents=True)
        path = staging / "result.parquet"
        escaped_path = str(path).replace("'", "''")
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE resting_hr_analysis_result (
                lag_days INTEGER,
                direction TEXT NOT NULL,
                estimate_per_100_kcal DOUBLE NOT NULL,
                estimate_per_personal_standard_deviation DOUBLE NOT NULL,
                exposure_unit TEXT NOT NULL,
                outcome_unit TEXT NOT NULL,
                personal_standard_deviation_kcal DOUBLE NOT NULL,
                pointwise_interval TEXT NOT NULL,
                simultaneous_band TEXT,
                model_maturity TEXT NOT NULL,
                diagnostics TEXT NOT NULL,
                methodology TEXT NOT NULL
            )
            """
        )
        estimates = (*result.lag_associations, result.cumulative_association)
        self._query.executemany(
            "INSERT INTO resting_hr_analysis_result VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    estimate.lag_days,
                    estimate.direction.value,
                    estimate.estimate_per_100_kcal,
                    estimate.estimate_per_personal_standard_deviation,
                    estimate.exposure_unit.value,
                    estimate.outcome_unit.value,
                    result.personal_standard_deviation_kcal,
                    json.dumps(
                        [
                            estimate.pointwise_interval.lower_per_100_kcal,
                            estimate.pointwise_interval.upper_per_100_kcal,
                            estimate.pointwise_interval.lower_per_personal_standard_deviation,
                            estimate.pointwise_interval.upper_per_personal_standard_deviation,
                        ]
                    ),
                    (
                        None
                        if estimate.simultaneous_band is None
                        else json.dumps(
                            [
                                estimate.simultaneous_band.lower_per_100_kcal,
                                estimate.simultaneous_band.upper_per_100_kcal,
                                estimate.simultaneous_band.lower_per_personal_standard_deviation,
                                estimate.simultaneous_band.upper_per_personal_standard_deviation,
                            ]
                        )
                    ),
                    result.model_maturity,
                    json.dumps(
                        {
                            "complete_days": result.diagnostics.complete_days,
                            "feature_dependency": result.diagnostics.feature_dependency,
                            "bootstrap_successes": result.diagnostics.bootstrap_successes,
                            "bootstrap_resamples": result.diagnostics.bootstrap_resamples,
                            "model_readiness": result.diagnostics.model_readiness,
                            "association_guardrail": result.diagnostics.association_guardrail,
                        },
                        sort_keys=True,
                    ),
                    json.dumps(
                        {
                            "ridge_penalty": result.methodology.ridge_penalty,
                            "minimum_observations": result.methodology.minimum_observations,
                            "robust_observations": result.methodology.robust_observations,
                            "bootstrap_method": result.methodology.bootstrap_method,
                            "block_length_days": result.methodology.block_length_days,
                            "resample_count": result.methodology.resample_count,
                            "random_seed": result.methodology.random_seed,
                            "interval_level": result.methodology.interval_level,
                        },
                        sort_keys=True,
                    ),
                )
                for estimate in estimates
            ],
        )
        self._query.execute(f"COPY resting_hr_analysis_result TO '{escaped_path}' (FORMAT PARQUET)")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(destination)
        with self._metadata:
            self._metadata.execute(
                "UPDATE analysis_runs SET status = 'completed', completed_at = ? "
                "WHERE analysis_run_id = ?",
                (datetime.now().astimezone().isoformat(), str(provenance.analysis_run_id)),
            )

    def load_latest_resting_hr_analysis(
        self, start_date: date | None, end_date: date | None
    ) -> RestingHeartRateAnalysisResult | None:
        self._require_open()
        row = self._metadata.execute(
            """
            SELECT analysis_runs.analysis_run_id, analysis_runs.result_id,
                   analysis_runs.snapshot_id, analysis_runs.analysis_definition_id,
                   analysis_runs.config_hash, analysis_runs.config_schema_version,
                   analysis_runs.code_commit, analysis_runs.code_dirty,
                   analysis_runs.code_diff_hash, analysis_runs.environment_lock_hash
            FROM analysis_runs, active_snapshot
            WHERE analysis_runs.status = 'completed'
              AND analysis_runs.snapshot_id = active_snapshot.snapshot_id
              AND analysis_runs.analysis_start_date IS ?
              AND analysis_runs.analysis_end_date IS ?
            ORDER BY analysis_runs.completed_at DESC, analysis_runs.rowid DESC
            LIMIT 1
            """,
            (
                start_date.isoformat() if start_date else None,
                end_date.isoformat() if end_date else None,
            ),
        ).fetchone()
        if row is None:
            return None
        result_id = str(row[1])
        snapshot_id = str(row[2])
        definition_id = str(row[3])
        path = self._root / _PARQUET_DIRECTORY / "analyses" / result_id / "result.parquet"
        escaped_path = str(path).replace("'", "''")
        rows = self._query.execute(
            f"""
            SELECT lag_days, direction, estimate_per_100_kcal,
                   estimate_per_personal_standard_deviation, exposure_unit, outcome_unit,
                   personal_standard_deviation_kcal, pointwise_interval, simultaneous_band,
                   model_maturity, diagnostics, methodology
            FROM read_parquet('{escaped_path}')
            ORDER BY lag_days NULLS LAST
            """
        ).fetchall()

        def interval(value: str) -> AssociationInterval:
            values = cast(list[float], json.loads(value))
            return AssociationInterval(
                lower_per_100_kcal=float(values[0]),
                upper_per_100_kcal=float(values[1]),
                lower_per_personal_standard_deviation=float(values[2]),
                upper_per_personal_standard_deviation=float(values[3]),
            )

        estimates = tuple(
            AssociationEstimate(
                lag_days=None if lag_days is None else int(lag_days),
                direction=AssociationDirection(direction),
                estimate_per_100_kcal=float(per_100),
                estimate_per_personal_standard_deviation=float(per_sd),
                exposure_unit=CanonicalUnit(exposure_unit),
                outcome_unit=CanonicalUnit(outcome_unit),
                pointwise_interval=interval(str(pointwise)),
                simultaneous_band=(None if simultaneous is None else interval(str(simultaneous))),
            )
            for (
                lag_days,
                direction,
                per_100,
                per_sd,
                exposure_unit,
                outcome_unit,
                _,
                pointwise,
                simultaneous,
                _,
                _,
                _,
            ) in rows
        )
        assert estimates and estimates[-1].lag_days is None
        diagnostic_values = cast(dict[str, object], json.loads(str(rows[0][10])))
        methodology_values = cast(dict[str, object], json.loads(str(rows[0][11])))
        return RestingHeartRateAnalysisResult(
            snapshot_id=SnapshotId(snapshot_id),
            analysis_definition_id=AnalysisDefinitionId(definition_id),
            personal_standard_deviation_kcal=float(rows[0][6]),
            lag_associations=estimates[:-1],
            cumulative_association=estimates[-1],
            model_maturity=cast(Literal["exploratory", "robust"], rows[0][9]),
            diagnostics=AnalysisDiagnostics(
                complete_days=cast(int, diagnostic_values["complete_days"]),
                feature_dependency=cast(
                    Literal["acceptable", "high"], diagnostic_values["feature_dependency"]
                ),
                bootstrap_successes=cast(int, diagnostic_values["bootstrap_successes"]),
                bootstrap_resamples=cast(int, diagnostic_values["bootstrap_resamples"]),
                model_readiness=cast(
                    Literal["exploratory", "robust"], diagnostic_values["model_readiness"]
                ),
                association_guardrail=cast(
                    Literal["simultaneous_band_includes_zero", "simultaneous_band_excludes_zero"],
                    diagnostic_values["association_guardrail"],
                ),
            ),
            methodology=AnalysisMethodology(
                ridge_penalty=cast(float, methodology_values["ridge_penalty"]),
                minimum_observations=cast(int, methodology_values["minimum_observations"]),
                robust_observations=cast(int, methodology_values["robust_observations"]),
                bootstrap_method=cast(
                    Literal["moving_block"], methodology_values["bootstrap_method"]
                ),
                block_length_days=cast(int, methodology_values["block_length_days"]),
                resample_count=cast(int, methodology_values["resample_count"]),
                random_seed=cast(int, methodology_values["random_seed"]),
                interval_level=cast(float, methodology_values["interval_level"]),
            ),
            provenance=(
                None if not row[4] or not row[6] or not row[9] else _analysis_provenance(row)
            ),
        )

    def load_latest_robust_analysis_provenance(self) -> AnalysisProvenance | None:
        self._require_open()
        row = self._metadata.execute(
            """
            SELECT analysis_run_id, result_id, snapshot_id, analysis_definition_id,
                   config_hash, config_schema_version, code_commit, code_dirty,
                   code_diff_hash, environment_lock_hash
            FROM analysis_runs
            WHERE status = 'completed' AND model_maturity = 'robust'
            ORDER BY completed_at DESC, rowid DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return _analysis_provenance(row)

    def load_provenance_counts(self) -> ProvenanceCounts:
        self._require_open()
        import_count, package_count, snapshot_count, quarantined_count = self._metadata.execute(
            """
            SELECT count(CASE WHEN status IN ('committed', 'duplicate') THEN 1 END),
                   count(DISTINCT CASE
                       WHEN status IN ('committed', 'duplicate') THEN package_hash
                   END),
                   count(DISTINCT CASE WHEN status = 'committed' THEN snapshot_id END),
                   count(CASE WHEN status = 'quarantined' THEN 1 END)
            FROM imports
            """
        ).fetchone()
        active = self._metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone()
        if active is None:
            return ProvenanceCounts(
                import_count, package_count, snapshot_count, 0, 0, quarantined_count
            )
        result = self._current_import_result(
            status="duplicate",
            snapshot_id=SnapshotId(str(active[0])),
            record_count=0,
            diagnostics=(),
        )
        return ProvenanceCounts(
            import_count=import_count,
            package_count=package_count,
            snapshot_count=snapshot_count,
            logical_measurement_count=result.logical_measurement_count,
            measurement_version_count=result.measurement_version_count,
            quarantined_import_count=quarantined_count,
        )

    def close(self) -> None:
        if not self._closed:
            try:
                self._query.close()
                self._metadata.close()
            finally:
                if self._writer_lock is not None:
                    self._writer_lock.close()
                self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Datenspeicher ist geschlossen.")

    def _require_writer(self) -> None:
        if self._writer_lock is None:
            raise RuntimeError("Operation benötigt den exklusiven Writer-Lock.")

    def _bind_person(self) -> None:
        if self._mode is DataMode.REAL:
            self._metadata.execute(
                "UPDATE store_identity SET person_binding = 'bound' WHERE singleton = 1"
            )

    def _recover_imports(self) -> None:
        try:
            self._recover_imports_unchecked()
        except (OSError, sqlite3.Error) as error:
            raise StoreError("Import-Recovery konnte nicht abgeschlossen werden.") from error

    def _recover_imports_unchecked(self) -> None:
        running_analyses = self._metadata.execute(
            "SELECT analysis_run_id, result_id FROM analysis_runs WHERE status = 'running'"
        ).fetchall()
        for analysis_run_id, result_id in running_analyses:
            for path in (
                self._root / "analysis-staging" / str(analysis_run_id),
                self._root / _PARQUET_DIRECTORY / "analyses" / str(result_id),
            ):
                if path.exists():
                    self._remove_tree(path)
        if running_analyses:
            with self._metadata:
                self._metadata.execute(
                    "UPDATE analysis_runs SET status = 'interrupted' WHERE status = 'running'"
                )
        analysis_staging = self._root / "analysis-staging"
        if analysis_staging.exists():
            for path in analysis_staging.iterdir():
                if path.is_dir():
                    self._remove_tree(path)
        analyses = self._root / _PARQUET_DIRECTORY / "analyses"
        if analyses.exists():
            published = {
                str(row[0])
                for row in self._metadata.execute(
                    "SELECT result_id FROM analysis_runs WHERE status = 'completed'"
                )
            }
            for path in analyses.iterdir():
                if path.is_dir() and path.name not in published:
                    self._remove_tree(path)

        active = self._metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone()
        active_snapshot = None if active is None else str(active[0])
        import_columns = {
            str(row[1]) for row in self._metadata.execute("PRAGMA table_info(imports)").fetchall()
        }
        diagnostics_column = "diagnostics" if "diagnostics" in import_columns else "''"
        running = self._metadata.execute(
            f"SELECT import_id, snapshot_id, {diagnostics_column} "
            "FROM imports WHERE status = 'running'"
        ).fetchall()
        bind_recovered_person = False
        for import_id, snapshot_id, diagnostics in running:
            bind_recovered_person = bind_recovered_person or diagnostics == "reading_package"
            staging = self._root / "staging" / str(import_id)
            paths = [(staging, "staging")]
            if str(snapshot_id) != active_snapshot:
                paths.append(
                    (
                        self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id),
                        "snapshot",
                    )
                )
            self._retain_import_quarantine(str(import_id), paths, "interrupted_before_publish")
        if running:
            with self._metadata:
                if "diagnostics" in import_columns:
                    self._metadata.execute(
                        """
                        UPDATE imports
                        SET status = 'quarantined', diagnostics = 'interrupted_before_publish'
                        WHERE status = 'running'
                        """
                    )
                else:
                    self._metadata.execute(
                        "UPDATE imports SET status = 'quarantined' WHERE status = 'running'"
                    )
                if bind_recovered_person:
                    self._bind_person()
        staging_root = self._root / "staging"
        if staging_root.exists():
            known_imports = {
                str(row[0]) for row in self._metadata.execute("SELECT import_id FROM imports")
            }
            for path in staging_root.iterdir():
                if path.is_dir():
                    if path.name not in known_imports:
                        self._quarantine_orphaned_import_artifact(path, "staging")
                        continue
                    self._remove_tree(path)

        snapshots = self._root / _PARQUET_DIRECTORY / "snapshots"
        if snapshots.exists():
            known_snapshots = {
                str(row[0])
                for row in self._metadata.execute(
                    "SELECT snapshot_id FROM imports WHERE status IN ('committed', 'duplicate')"
                )
            }
            for path in snapshots.iterdir():
                if path.is_dir() and path.name not in known_snapshots:
                    self._quarantine_orphaned_import_artifact(path, "snapshot")
        self._recover_import_quarantine()

    def _quarantine_orphaned_import_artifact(
        self, path: Path, label: Literal["staging", "snapshot"]
    ) -> None:
        recovered_id = hashlib.sha256(f"recovered:{path.name}".encode()).hexdigest()[:32]
        import_id = (
            path.name
            if len(path.name) == 32 and not set(path.name) - set("0123456789abcdef")
            else recovered_id
        )
        operation_id = snapshot_id = recovered_id
        bind_person = label == "snapshot"
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            if isinstance(manifest, dict):
                import_id = str(manifest.get("import_id", import_id))
                operation_id = str(
                    manifest.get(
                        "operation_id",
                        manifest.get("created_by_operation_id", recovered_id),
                    )
                )
                snapshot_id = str(manifest.get("snapshot_id", recovered_id))
                bind_person = manifest.get("status") in {"reading_package", "published"}
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        with self._metadata:
            self._metadata.execute(
                "INSERT INTO imports VALUES (?, ?, '', 'running', ?, 0, 0, ?, ?)",
                (
                    import_id,
                    operation_id,
                    snapshot_id,
                    datetime.now().astimezone().isoformat(),
                    f"orphaned_{label}_detected",
                ),
            )
        self._retain_import_quarantine(import_id, [(path, label)], f"orphaned_{label}")
        with self._metadata:
            self._metadata.execute(
                "UPDATE imports SET status = 'quarantined', diagnostics = ? WHERE import_id = ?",
                (f"orphaned_{label}", import_id),
            )
            if bind_person:
                self._bind_person()

    def _recover_import_quarantine(self) -> None:
        quarantine_root = self._root / "quarantine" / "imports"
        if not quarantine_root.exists():
            return
        for path in quarantine_root.iterdir():
            if not path.is_dir():
                continue
            recovered_id = hashlib.sha256(f"recovered:{path.name}".encode()).hexdigest()[:32]
            operation_id = snapshot_id = recovered_id
            personal_artifacts = (path / "snapshot").exists()
            for label in ("staging", "snapshot"):
                try:
                    manifest = json.loads(
                        (path / label / "manifest.json").read_text(encoding="utf-8")
                    )
                    if isinstance(manifest, dict):
                        operation_id = str(
                            manifest.get(
                                "operation_id",
                                manifest.get("created_by_operation_id", operation_id),
                            )
                        )
                        snapshot_id = str(manifest.get("snapshot_id", snapshot_id))
                        personal_artifacts = personal_artifacts or manifest.get("status") in {
                            "reading_package",
                            "published",
                        }
                        break
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
            diagnostic = "orphaned_quarantine"
            try:
                report = json.loads((path / "diagnostic.json").read_text(encoding="utf-8"))
                if isinstance(report, dict) and isinstance(report.get("diagnostic"), str):
                    diagnostic = report["diagnostic"]
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
            with self._metadata:
                self._metadata.execute(
                    """
                    INSERT INTO imports VALUES (?, ?, '', 'quarantined', ?, 0, 0, ?, ?)
                    ON CONFLICT(import_id) DO UPDATE SET
                        status = 'quarantined', diagnostics = excluded.diagnostics
                    """,
                    (
                        path.name,
                        operation_id,
                        snapshot_id,
                        datetime.now().astimezone().isoformat(),
                        diagnostic,
                    ),
                )
                if personal_artifacts:
                    self._bind_person()

    def _retain_import_quarantine(
        self,
        import_id: str,
        paths: list[tuple[Path, str]],
        diagnostic: str,
    ) -> None:
        quarantine = self._root / "quarantine" / "imports" / import_id
        try:
            quarantine.mkdir(parents=True, exist_ok=True)
            for source, label in paths:
                if source.exists():
                    os.replace(source, quarantine / label)
            (quarantine / "diagnostic.json").write_text(
                json.dumps({"diagnostic": diagnostic}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as error:
            raise StoreError("Importquarantäne konnte nicht gesichert werden.") from error

    @staticmethod
    def _remove_tree(path: Path) -> None:
        try:
            shutil.rmtree(path)
        except OSError as error:
            raise StoreError("Recovery konnte Zwischenzustand nicht bereinigen.") from error

    @staticmethod
    def _write_manifest(
        directory: Path,
        *,
        operation_id: OperationId,
        import_id: ImportId,
        snapshot_id: SnapshotId,
        status: Literal["running", "published"],
        record_count: int = 0,
    ) -> None:
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "operation_id": str(operation_id),
                    "import_id": str(import_id),
                    "snapshot_id": str(snapshot_id),
                    "status": status,
                    "record_count": record_count,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
