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
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import IO, Literal, Self, cast
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb

from personal_health_lab import DataMode
from personal_health_lab.health_data import (
    AnalysisDataStatusReason,
    AnalysisFreshness,
    CanonicalHealthRecord,
    CanonicalHealthType,
    CanonicalSleepCategory,
    CanonicalSleepInterval,
    CanonicalUnit,
    CanonicalWorkout,
    DailyHealthSeries,
    DailyHealthValue,
    DataQualityStatus,
    DataStatusReasonCode,
    LogicalMeasurementId,
    MeasurementVersionId,
    ModelMaturityCriterion,
    ModelMaturityCriterionCode,
    ModelMaturityStatus,
    ReproducibilityStatus,
)

_METADATA_FILE = "metadata.sqlite3"
_PARQUET_DIRECTORY = "parquet"
_QUERY_FILE = "query.duckdb"
_STORE_SCHEMA_VERSION = 11
_WRITER_LOCK_FILE = ".writer.lock"
_FULL_SNAPSHOT_IMPORT_METHOD = "full-snapshot-import/v1"
_STORE_MIGRATION_METHOD = "cow-migration/v1"
_SNAPSHOT_SCHEMA_VERSION = 7
_RESTORABLE_MANUAL_METADATA_TABLES = (
    "activity_derivation_versions",
    "manual_context_revisions",
    "context_coverage_start_values",
    "illness_category_values",
    "daily_stress_values",
    "custom_context_label_values",
    "custom_context_period_values",
    "illness_period_values",
    "manual_context_publications",
    "medication_regime_revisions",
    "medication_regime_values",
    "medication_scheduled_doses",
    "medication_as_needed_entries",
    "medication_publications",
    "medication_deviation_revisions",
    "medication_deviation_values",
    "medication_deviation_intakes",
    "medication_deviation_publications",
    "intake_reason_category_revisions",
    "intake_reason_category_values",
    "intake_reason_category_publications",
    "as_needed_intake_revisions",
    "as_needed_intake_values",
    "as_needed_intake_publications",
    "manual_revision_intents",
)
_CANONICAL_HEALTH_TYPES_SQL = ", ".join(f"'{item.value}'" for item in CanonicalHealthType)
_CANONICAL_UNITS_SQL = ", ".join(f"'{item.value}'" for item in CanonicalUnit)
_PRE_ACTIVITY_HEALTH_TYPES_SQL = ", ".join(
    f"'{item.value}'"
    for item in CanonicalHealthType
    if item
    not in {
        CanonicalHealthType.APPLE_EXERCISE_TIME,
        CanonicalHealthType.STEP_COUNT,
        CanonicalHealthType.WALKING_RUNNING_DISTANCE,
    }
)
_PRE_ACTIVITY_UNITS_SQL = ", ".join(
    f"'{item.value}'"
    for item in CanonicalUnit
    if item not in {CanonicalUnit.COUNT, CanonicalUnit.KILOMETER, CanonicalUnit.MINUTE}
)
_IDENTITY_RULE_VERSION = "healthkit-identity/v3"
_SUPPORTED_IDENTITY_RULE_VERSIONS = {"healthkit-natural/v2", _IDENTITY_RULE_VERSION}
_MAPPING_RULE_VERSION = "healthkit-canonical/v3"
_SUPPORTED_MAPPING_RULE_VERSIONS = {
    "healthkit-canonical/v1",
    "healthkit-canonical/v2",
    _MAPPING_RULE_VERSION,
}
_FIXED_PLAUSIBILITY_RULE_VERSION = "fixed-plausibility/v1"
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
    "sleep_intervals.parquet": (
        ("measurement_version_id", "VARCHAR"),
        ("identity_candidate_id", "VARCHAR"),
        ("original_category", "VARCHAR"),
        ("canonical_category", "VARCHAR"),
        ("source_start_utc", "VARCHAR"),
        ("source_end_utc", "VARCHAR"),
        ("source_updated_at_utc", "VARCHAR"),
        ("source_start_offset_minutes", "INTEGER"),
        ("source_end_offset_minutes", "INTEGER"),
        ("source_updated_at_offset_minutes", "INTEGER"),
        ("source_name", "VARCHAR"),
        ("source_version", "VARCHAR"),
        ("device", "VARCHAR"),
        ("strong_source_id_hash", "VARCHAR"),
        ("is_selected", "BOOLEAN"),
    ),
    "workouts.parquet": (
        ("workout_version_id", "VARCHAR"),
        ("logical_workout_id", "VARCHAR"),
        ("original_activity_type", "VARCHAR"),
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
        ("strong_source_id_hash", "VARCHAR"),
        ("reported_duration_minutes", "DOUBLE"),
        ("distance_kilometers", "DOUBLE"),
        ("active_energy_kilocalories", "DOUBLE"),
        ("is_selected", "BOOLEAN"),
    ),
    "resolved_workouts.parquet": (
        ("logical_workout_id", "VARCHAR"),
        ("selected_workout_version_id", "VARCHAR"),
        ("disposition", "VARCHAR"),
        ("effective_duration_minutes", "DOUBLE"),
        ("distance_kilometers", "DOUBLE"),
        ("active_energy_kilocalories", "DOUBLE"),
        ("effective_decision_id", "VARCHAR"),
    ),
    "workout_review_links.parquet": (
        ("review_case_id", "VARCHAR"),
        ("workout_version_id", "VARCHAR"),
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
    "weight_nutrition_days.parquet": (
        ("derived_record_id", "VARCHAR"),
        ("day", "DATE"),
        ("feature_kind", "VARCHAR"),
        ("canonical_unit", "VARCHAR"),
        ("effective_value", "DOUBLE"),
        ("observation_status", "VARCHAR"),
        ("quality_status", "VARCHAR"),
        ("derivation_contract_id", "VARCHAR"),
        ("snapshot_id", "VARCHAR"),
        ("derived_at_utc", "VARCHAR"),
    ),
    "sleep_episodes.parquet": (
        ("derived_record_id", "VARCHAR"),
        ("episode_start_utc", "VARCHAR"),
        ("episode_end_utc", "VARCHAR"),
        ("observed_sleep_minutes", "DOUBLE"),
        ("quality_status", "VARCHAR"),
        ("derivation_contract_id", "VARCHAR"),
        ("snapshot_id", "VARCHAR"),
        ("derived_at_utc", "VARCHAR"),
    ),
    "sleep_nights.parquet": (
        ("derived_record_id", "VARCHAR"),
        ("day", "DATE"),
        ("observation_status", "VARCHAR"),
        ("primary_episode_id", "VARCHAR"),
        ("quality_status", "VARCHAR"),
        ("derivation_contract_id", "VARCHAR"),
        ("snapshot_id", "VARCHAR"),
        ("derived_at_utc", "VARCHAR"),
    ),
    "activity_days.parquet": (
        ("derived_record_id", "VARCHAR"),
        ("day", "DATE"),
        ("metric", "VARCHAR"),
        ("canonical_unit", "VARCHAR"),
        ("effective_value", "DOUBLE"),
        ("watch_value", "DOUBLE"),
        ("iphone_value", "DOUBLE"),
        ("quality_status", "VARCHAR"),
        ("derivation_contract_id", "VARCHAR"),
        ("snapshot_id", "VARCHAR"),
        ("derived_at_utc", "VARCHAR"),
    ),
    "activity_coverage_segments.parquet": (
        ("derived_record_id", "VARCHAR"),
        ("start_utc", "VARCHAR"),
        ("end_utc", "VARCHAR"),
        ("coverage_kind", "VARCHAR"),
        ("derivation_contract_id", "VARCHAR"),
        ("snapshot_id", "VARCHAR"),
        ("derived_at_utc", "VARCHAR"),
    ),
    "workout_features.parquet": (
        ("derived_record_id", "VARCHAR"),
        ("logical_workout_id", "VARCHAR"),
        ("day", "DATE"),
        ("activity_type", "VARCHAR"),
        ("duration_minutes", "DOUBLE"),
        ("distance_kilometers", "DOUBLE"),
        ("active_energy_kilocalories", "DOUBLE"),
        ("quality_status", "VARCHAR"),
        ("derivation_contract_id", "VARCHAR"),
        ("snapshot_id", "VARCHAR"),
        ("derived_at_utc", "VARCHAR"),
    ),
    "daily_context.parquet": (
        ("derived_record_id", "VARCHAR"),
        ("day", "DATE"),
        ("illness_severity", "VARCHAR"),
        ("stress_level", "VARCHAR"),
        ("quality_status", "VARCHAR"),
        ("derivation_contract_id", "VARCHAR"),
        ("snapshot_id", "VARCHAR"),
        ("derived_at_utc", "VARCHAR"),
    ),
    "medication_context.parquet": (
        ("derived_record_id", "VARCHAR"),
        ("day", "DATE"),
        ("scheduled_dose_count", "BIGINT"),
        ("deviation_count", "BIGINT"),
        ("as_needed_intake_count", "BIGINT"),
        ("quality_status", "VARCHAR"),
        ("derivation_contract_id", "VARCHAR"),
        ("snapshot_id", "VARCHAR"),
        ("derived_at_utc", "VARCHAR"),
    ),
    "derivation_lineage.parquet": (
        ("derived_record_id", "VARCHAR"),
        ("derived_family", "VARCHAR"),
        ("derivation_contract_id", "VARCHAR"),
        ("source_logical_id", "VARCHAR"),
        ("source_version_id", "VARCHAR"),
        ("contribution_role", "VARCHAR"),
        ("snapshot_id", "VARCHAR"),
        ("derived_at_utc", "VARCHAR"),
    ),
}
_SNAPSHOT_FILE_CONTRACTS = {
    "source_occurrences.parquet": (_IDENTITY_RULE_VERSION, _MAPPING_RULE_VERSION),
    "measurement_versions.parquet": (_MAPPING_RULE_VERSION,),
    "sleep_intervals.parquet": (_MAPPING_RULE_VERSION,),
    "workouts.parquet": (_MAPPING_RULE_VERSION,),
    "resolved_measurements.parquet": ("resolved-measurement/v1",),
    "resolved_workouts.parquet": ("resolved-workout/v1",),
    "workout_review_links.parquet": ("resolved-workout/v1",),
    "open_review_cases.parquet": (_FIXED_PLAUSIBILITY_RULE_VERSION,),
    "weight_nutrition_days.parquet": ("weight-nutrition-day/v1",),
    "sleep_episodes.parquet": ("sleep-episode/v1",),
    "sleep_nights.parquet": ("sleep-night/v1",),
    "activity_days.parquet": ("activity-day/v1",),
    "activity_coverage_segments.parquet": ("activity-coverage/v1",),
    "workout_features.parquet": ("workout-feature/v1",),
    "daily_context.parquet": ("daily-context/v1",),
    "medication_context.parquet": ("medication-context/v1",),
    "derivation_lineage.parquet": (
        "resolved-measurement/v1",
        "resolved-workout/v1",
        "weight-nutrition-day/v1",
        "sleep-episode/v1",
        "sleep-night/v1",
        "activity-day/v1",
        "activity-coverage/v1",
        "workout-feature/v1",
        "daily-context/v1",
        "medication-context/v1",
    ),
}
_V6_DERIVATION_FILES = {
    "weight_nutrition_days.parquet",
    "sleep_episodes.parquet",
    "sleep_nights.parquet",
    "activity_days.parquet",
    "activity_coverage_segments.parquet",
    "workout_features.parquet",
    "daily_context.parquet",
    "medication_context.parquet",
}
_DERIVATION_CONTRACT_IDS = tuple(
    sorted(
        {
            contract_id
            for filename in _V6_DERIVATION_FILES | {"derivation_lineage.parquet"}
            for contract_id in _SNAPSHOT_FILE_CONTRACTS[filename]
        }
    )
)
_MAPPING_CONTRACT_FILES = {
    "measurement_versions.parquet",
    "sleep_intervals.parquet",
    "workouts.parquet",
}
_LEGACY_SNAPSHOT_SCHEMAS = {
    name: schema
    for name, schema in _SNAPSHOT_SCHEMAS.items()
    if name
    in {
        "source_occurrences.parquet",
        "measurement_versions.parquet",
        "resolved_measurements.parquet",
        "open_review_cases.parquet",
    }
}
_V2_SNAPSHOT_SCHEMAS = {
    name: (
        tuple(field for field in schema if field[0] != "strong_source_id_hash")
        if name == "workouts.parquet"
        else schema
    )
    for name, schema in _SNAPSHOT_SCHEMAS.items()
    if name
    not in {
        "resolved_workouts.parquet",
        "workout_review_links.parquet",
        "derivation_lineage.parquet",
    }
    | _V6_DERIVATION_FILES
}
_V3_SNAPSHOT_SCHEMAS = {
    name: schema
    for name, schema in _SNAPSHOT_SCHEMAS.items()
    if name
    not in {
        "resolved_workouts.parquet",
        "workout_review_links.parquet",
        "derivation_lineage.parquet",
    }
    | _V6_DERIVATION_FILES
}
_V4_SNAPSHOT_SCHEMAS = {
    name: schema
    for name, schema in _SNAPSHOT_SCHEMAS.items()
    if name not in _V6_DERIVATION_FILES | {"derivation_lineage.parquet"}
}
_V5_SNAPSHOT_SCHEMAS = {
    name: (
        tuple(field for field in schema if field[0] not in {"snapshot_id", "derived_at_utc"})
        if name == "derivation_lineage.parquet"
        else schema
    )
    for name, schema in _SNAPSHOT_SCHEMAS.items()
    if name not in _V6_DERIVATION_FILES
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


def _snapshot_file_contract_ids(
    filename: str, identity_rule_version_id: str, mapping_rule_version_id: str
) -> tuple[str, ...]:
    if filename == "source_occurrences.parquet":
        return identity_rule_version_id, mapping_rule_version_id
    if filename in _MAPPING_CONTRACT_FILES:
        return (mapping_rule_version_id,)
    return _SNAPSHOT_FILE_CONTRACTS[filename]


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


def cow_migration_estimate(
    metadata_bytes: int,
    snapshot_bytes: int,
    fragment_size: int,
    *,
    writer_bound: bool = True,
    scratch_bound: bool = True,
) -> int | None:
    """Bound peak live allocation for ``cow-migration/v1``."""
    if not writer_bound or not scratch_bound:
        return None
    if metadata_bytes < 0 or snapshot_bytes < 0 or fragment_size <= 0:
        raise ValueError("Kapazitätseingaben müssen nichtnegativ und Fragmente positiv sein.")
    backup_catalog_and_journal = _round_up(16 * metadata_bytes, fragment_size)
    snapshot_and_scratch = _round_up(8 * snapshot_bytes, fragment_size)
    return _DIRECTORY_OVERHEAD + backup_catalog_and_journal + snapshot_and_scratch


def _allocation_checkpoint(root: Path, phase: str) -> None:
    """Private test seam for measuring the real writer's live allocation."""


def _publication_fault_point(root: Path, fault_point_id: str) -> None:
    """Private test seam for durable import-publication transitions."""


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_snapshot(directory: Path) -> None:
    for path in sorted(directory.iterdir()):
        if path.is_file():
            with path.open("rb") as artifact:
                os.fsync(artifact.fileno())
    _fsync_directory(directory)


def _migration_backup_fault_point(root: Path) -> None:
    """Private test seam immediately before the migration backup starts."""


def _migration_fault_point(root: Path, fault_point_id: str) -> None:
    """Private test seam for durable copy-on-write migration transitions."""


def probe_capacity(
    path: Path,
    estimate_bytes: int | None,
    *,
    method_id: str = _FULL_SNAPSHOT_IMPORT_METHOD,
) -> CapacityCheck:
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

    @property
    def is_current(self) -> bool:
        return self.schema_version == str(_STORE_SCHEMA_VERSION)


@dataclass(frozen=True, slots=True)
class RestoreSessionFacts:
    restore_id: str
    operation_id: str
    backup_id: str
    source_store_id: str
    original_backup_sha256: str
    canonical_content_sha256: str
    working_copy_sha256: str
    audit_max_position: int
    source_schema_version: int
    target_schema_version: int
    completion_status: str | None


@dataclass(frozen=True, slots=True)
class MigrationRollbackFacts:
    migration_operation_id: OperationId
    latest_state_change_operation_id: OperationId | None
    backup_file: str
    backup_sha256: str | None
    backup_exists: bool
    backup_matches_migration: bool
    pre_migration_version: int
    post_migration_version: int
    migrated_snapshot_id: SnapshotId | None
    previous_snapshot_id: SnapshotId | None
    active_snapshot_id: SnapshotId | None


def current_store_schema_version() -> int:
    return _STORE_SCHEMA_VERSION


def current_snapshot_schema_version() -> int:
    return _SNAPSHOT_SCHEMA_VERSION


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
    minimum_input_completeness: float | None
    max_feature_dependency: float | None
    minimum_bootstrap_success_rate: float | None
    minimum_outcome_standard_deviation: float | None
    maximum_time_series_gap_days: int | None


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
    input_completeness: float | None
    outcome_standard_deviation: float | None
    maximum_time_series_gap_days: int | None


@dataclass(frozen=True, slots=True)
class RestingHeartRateAnalysisResult:
    snapshot_id: SnapshotId
    analysis_definition_id: AnalysisDefinitionId
    personal_standard_deviation_kcal: float
    lag_associations: tuple[AssociationEstimate, ...]
    cumulative_association: AssociationEstimate
    model_maturity: ModelMaturityStatus
    diagnostics: AnalysisDiagnostics
    methodology: AnalysisMethodology
    provenance: AnalysisProvenance | None = None
    data_status: DataQualityStatus = DataQualityStatus.REVIEWED
    data_status_reasons: tuple[AnalysisDataStatusReason, ...] = ()
    maturity_criteria: tuple[ModelMaturityCriterion, ...] = ()
    freshness: AnalysisFreshness = AnalysisFreshness.CURRENT
    completed_at: datetime | None = None
    reproducibility: ReproducibilityStatus = ReproducibilityStatus.REPRODUCIBLE

    @property
    def status_facts_recorded(self) -> bool:
        return bool(self.maturity_criteria)


@dataclass(frozen=True, slots=True)
class AnalysisRunRecord:
    provenance: AnalysisProvenance
    model_maturity: ModelMaturityStatus
    diagnostics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublishImportResult:
    status: Literal["committed", "duplicate"]
    snapshot_id: SnapshotId
    record_count: int
    logical_measurement_count: int
    measurement_version_count: int
    source_occurrence_count: int
    anomaly_count: int = 0
    diagnostics: tuple[str, ...] = ()


UnsupportedContentCategory = Literal[
    "record_type",
    "sleep_value",
    "top_level_element",
    "unit",
    "workout_activity_type",
    "workout_child",
]


@dataclass(frozen=True, slots=True)
class UnsupportedImportContent:
    category: UnsupportedContentCategory
    external_identifier: str
    count: int


@dataclass(frozen=True, slots=True)
class StoredImportDetails:
    package_record_count: int
    record_count: int
    logical_measurement_count: int
    measurement_version_count: int
    source_occurrence_count: int
    anomaly_count: int
    unsupported_content: tuple[UnsupportedImportContent, ...]


@dataclass(frozen=True, slots=True)
class StoredMeasurement:
    logical_measurement_id: LogicalMeasurementId
    measurement_version_id: MeasurementVersionId
    data_type: CanonicalHealthType
    unit: CanonicalUnit
    value: float
    effective_value: float | None
    disposition: (
        Literal[
            "included_source",
            "included_correction",
            "excluded_local",
            "excluded_source_deletion",
        ]
        | None
    )
    is_selected: bool
    source_start: datetime
    source_end: datetime
    source_updated_at: datetime
    measurement_local_day: date
    source_name: str
    source_version: str
    device: str
    original_value: float
    original_unit: str
    review_case_ids: tuple[ReviewCaseId, ...]


@dataclass(frozen=True, slots=True)
class StoredWorkout:
    logical_workout_id: LogicalMeasurementId
    workout_version_id: MeasurementVersionId
    original_activity_type: str
    source_start: datetime
    source_end: datetime
    source_updated_at: datetime
    measurement_local_day: date
    source_name: str
    source_version: str
    device: str
    strong_source_id_hash: str | None
    reported_duration_minutes: float | None
    distance_kilometers: float | None
    active_energy_kilocalories: float | None
    is_selected: bool
    effective_duration_minutes: float | None
    disposition: str | None
    review_case_ids: tuple[ReviewCaseId, ...]


@dataclass(frozen=True, slots=True)
class ActivityDerivationRecord:
    version_id: str
    coverage_gap_minutes: int
    source_classifier_version: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PublishDecisionResult:
    operation_id: OperationId
    decision_id: str
    snapshot_id: SnapshotId


@dataclass(frozen=True, slots=True)
class PublishBatchDecisionResult:
    operation_id: OperationId
    batch_action_id: str
    decision_ids: tuple[str, ...]
    snapshot_id: SnapshotId


@dataclass(frozen=True, slots=True)
class OpenDataReviewCase:
    review_case_id: str
    kind: Literal[
        "plausibility",
        "workout_plausibility",
        "workout_overlap",
        "continued_override",
        "suspected_source_deletion",
        "source_conflict",
        "rule_definition",
        "preferred_daily_weight_conflict",
    ]
    logical_measurement_id: LogicalMeasurementId | None
    measurement_version_id: MeasurementVersionId | None
    rule_version_id: str | None
    evidence_fingerprint: str


@dataclass(frozen=True, slots=True)
class ExportFact:
    export_id: str
    export_date: datetime | None
    covered_types: tuple[str, ...] = ()


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
    source_updated_at_utc: str
    source_version: str
    source_name: str
    device: str
    strong_source_id_hash: str | None
    measurement_local_date: date


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
class WorkoutVersionFact:
    workout_version_id: str
    logical_workout_id: str
    source_start_utc: str
    source_end_utc: str
    source_updated_at_utc: str
    source_version: str
    reported_duration_minutes: float | None
    distance_kilometers: float | None
    active_energy_kilocalories: float | None


@dataclass(frozen=True, slots=True)
class ResolvedWorkout:
    logical_workout_id: str
    selected_workout_version_id: str
    disposition: str
    effective_duration_minutes: float | None
    distance_kilometers: float | None
    active_energy_kilocalories: float | None
    effective_decision_id: str | None


@dataclass(frozen=True, slots=True)
class WorkoutResolution:
    workouts: tuple[ResolvedWorkout, ...]
    review_cases: tuple[OpenDataReviewCase, ...]
    review_links: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ReviewCaseId:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SourceTypeRuleRequest:
    source_type: str
    review_case_id: ReviewCaseId


@dataclass(frozen=True, slots=True)
class PlausibilityRuleRecord:
    version_id: str
    data_type: str
    unit: str
    fixed_lower_bound: float | None
    fixed_upper_bound: float | None
    personal_range_enabled: bool
    effective_from: datetime | None
    created_at: datetime
    recommendation_id: str | None = None
    effective_timezone: str | None = None
    effective_offset_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class StoredReviewCycleId:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SourceResolution:
    measurements: tuple[ResolvedMeasurement, ...]
    review_cases: tuple[OpenDataReviewCase, ...]
    new_review_case_ids: tuple[ReviewCaseId, ...] = ()
    source_type_requests: tuple[SourceTypeRuleRequest, ...] = ()
    cycle_status: Literal["open", "closed"] = "closed"
    cycle_open_case_count: int = 0
    anomaly_count: int = 0


@dataclass(frozen=True, slots=True)
class ReviewCycleRecord:
    cycle_id: StoredReviewCycleId
    snapshot_id: SnapshotId
    status: Literal["open", "closed"]
    open_case_count: int
    kind: Literal["import", "rule_version", "historical"] = "import"
    base_snapshot_id: SnapshotId | None = None
    start_date: date | None = None
    end_date: date | None = None
    rule_version_id: str | None = None


@dataclass(frozen=True, slots=True)
class HistoricalReviewResult:
    operation_id: OperationId
    cycle_id: StoredReviewCycleId
    snapshot_id: SnapshotId
    open_case_count: int


@dataclass(frozen=True, slots=True)
class ReviewCycleUpdate:
    cycle_id: StoredReviewCycleId
    open_case_count: int
    status: Literal["open", "closed"]


@dataclass(frozen=True, slots=True)
class HistoricalReviewPublication:
    operation_id: OperationId
    base_snapshot_id: SnapshotId
    start_date: date
    end_date: date
    rule_version_id: str
    reproduced_cases: tuple[OpenDataReviewCase, ...]
    open_cases: tuple[OpenDataReviewCase, ...]
    cycle_status: Literal["open", "closed"]


type SourceResolver = Callable[..., SourceResolution]
type WorkoutResolver = Callable[..., WorkoutResolution]


@dataclass(frozen=True, slots=True)
class ProvenanceCounts:
    import_count: int
    package_count: int
    snapshot_count: int
    logical_measurement_count: int
    measurement_version_count: int
    quarantined_import_count: int


@dataclass(frozen=True, slots=True)
class BackupImportFact:
    import_id: ImportId
    operation_id: OperationId
    status: str
    snapshot_id: SnapshotId
    record_count: int
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class BackupSnapshotFact:
    snapshot_id: SnapshotId
    schema_version: int
    created_by_operation_id: OperationId
    parent_snapshot_id: SnapshotId | None
    created_at_utc: datetime


@dataclass(frozen=True, slots=True)
class BackupSnapshotOrigin:
    snapshot_id: SnapshotId
    schema_version: int
    manifest_sha256: str
    snapshot_as_of: datetime
    context_timezone: str
    context_as_of_date: date
    medication_as_of: datetime
    activity_derivation_version_id: str
    manual_revision_bindings: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ReviewSnapshotFacts:
    snapshot_id: SnapshotId
    cases: tuple[OpenDataReviewCase, ...]
    versions: tuple[MeasurementVersionFact, ...]
    measurements: tuple[ResolvedMeasurement, ...]


@dataclass(frozen=True, slots=True)
class StoredContextCoverageStart:
    logical_id: str
    revision_id: str
    previous_revision_id: str | None
    state: Literal["active", "withdrawn"]
    start_date: date | None
    snapshot_id: SnapshotId | None


@dataclass(frozen=True, slots=True)
class ContextLogicalId:
    value: str

    def __post_init__(self) -> None:
        if len(self.value) != 32 or not set(self.value) <= set("0123456789abcdef"):
            raise ValueError("Kontext-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ContextRevisionId:
    value: str

    def __post_init__(self) -> None:
        if len(self.value) != 32 or not set(self.value) <= set("0123456789abcdef"):
            raise ValueError("Kontextrevisions-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ContextCoverageStartPublication:
    operation_id: OperationId
    intent: Literal["create", "revise", "withdraw", "restore"]
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId | None
    start_date: date | None
    withdrawal_reason: str | None
    expected_snapshot_id: SnapshotId
    snapshot_as_of: datetime
    context_as_of_date: date
    context_timezone: str


@dataclass(frozen=True, slots=True)
class ContextCoverageStartPublicationResult:
    logical_id: ContextLogicalId
    revision_id: ContextRevisionId
    snapshot_id: SnapshotId


@dataclass(frozen=True, slots=True)
class MedicationLogicalId:
    value: str

    def __post_init__(self) -> None:
        if len(self.value) != 32 or not set(self.value) <= set("0123456789abcdef"):
            raise ValueError("Medikamenten-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class MedicationRevisionId:
    value: str

    def __post_init__(self) -> None:
        if len(self.value) != 32 or not set(self.value) <= set("0123456789abcdef"):
            raise ValueError("Medikamentenrevisions-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class MedicationRegimePublication:
    operation_id: OperationId
    intent: Literal["create", "revise"]
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId | None
    starts_at: datetime
    timezone: str
    scheduled_doses: tuple[tuple[str, str, str, time, tuple[str, ...]], ...]
    as_needed_medications: tuple[tuple[str, str, str, tuple[str, ...], str], ...]
    expected_snapshot_id: SnapshotId
    medication_as_of: datetime


@dataclass(frozen=True, slots=True)
class MedicationRegimePublicationResult:
    logical_id: MedicationLogicalId
    revision_id: MedicationRevisionId
    snapshot_id: SnapshotId


@dataclass(frozen=True, slots=True)
class MedicationDeviationPublication:
    operation_id: OperationId
    intent: Literal["create", "revise", "withdraw", "restore"]
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId | None
    regime_logical_id: MedicationLogicalId
    scheduled_at: datetime
    actual_intakes: tuple[tuple[datetime, str], ...]
    withdrawal_reason: str | None
    expected_snapshot_id: SnapshotId
    medication_as_of: datetime


@dataclass(frozen=True, slots=True)
class MedicationDeviationPublicationResult:
    logical_id: MedicationLogicalId
    revision_id: MedicationRevisionId
    snapshot_id: SnapshotId


@dataclass(frozen=True, slots=True)
class AsNeededIntakePublication:
    operation_id: OperationId
    intent: Literal["create", "revise", "withdraw", "restore"]
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId | None
    regime_logical_id: MedicationLogicalId
    entry_id: str
    taken_at: datetime
    amount: str
    reason_category_logical_id: MedicationLogicalId | None
    withdrawal_reason: str | None
    expected_snapshot_id: SnapshotId
    medication_as_of: datetime


@dataclass(frozen=True, slots=True)
class IntakeReasonCategoryPublication:
    operation_id: OperationId
    intent: Literal["create", "revise", "withdraw", "restore"]
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId | None
    name: str | None
    withdrawal_reason: str | None
    expected_snapshot_id: SnapshotId
    medication_as_of: datetime


@dataclass(frozen=True, slots=True)
class MedicationRootPublicationResult:
    logical_id: MedicationLogicalId
    revision_id: MedicationRevisionId
    snapshot_id: SnapshotId


@dataclass(frozen=True, slots=True)
class StoredMedicationDeviation:
    logical_id: str
    revision_id: str
    previous_revision_id: str | None
    state: Literal["active", "withdrawn"]
    regime_logical_id: str
    scheduled_at: datetime
    actual_intakes: tuple[tuple[datetime, str], ...]
    withdrawal_reason: str | None


@dataclass(frozen=True, slots=True)
class StoredMedicationRegime:
    logical_id: str
    revision_id: str
    previous_revision_id: str | None
    starts_at: datetime
    timezone: str
    scheduled_doses: tuple[tuple[str, str, str, time, tuple[str, ...]], ...]
    as_needed_medications: tuple[tuple[str, str, str, tuple[str, ...], str], ...]


@dataclass(frozen=True, slots=True)
class StoredAsNeededIntake:
    logical_id: str
    revision_id: str
    previous_revision_id: str | None
    state: Literal["active", "withdrawn"]
    regime_logical_id: str
    entry_id: str
    taken_at: datetime
    amount: str
    reason_category_logical_id: str | None
    withdrawal_reason: str | None


@dataclass(frozen=True, slots=True)
class StoredIntakeReasonCategory:
    logical_id: str
    revision_id: str
    previous_revision_id: str | None
    state: Literal["active", "withdrawn"]
    name: str | None
    withdrawal_reason: str | None


@dataclass(frozen=True, slots=True)
class StoredIllnessRevision:
    logical_id: str
    revision_id: str
    previous_revision_id: str | None
    state: Literal["active", "withdrawn"]
    object_kind: Literal[
        "illness_category",
        "illness_period",
        "daily_stress",
        "custom_context_label",
        "custom_context_period",
    ]
    name: str | None
    category_logical_id: str | None
    start_date: date | None
    end_date: date | None
    severity: str | None
    stress_level: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class IllnessPublication:
    operation_id: OperationId
    intent: Literal["create", "revise", "withdraw", "restore"]
    object_kind: Literal[
        "illness_category",
        "illness_period",
        "daily_stress",
        "custom_context_label",
        "custom_context_period",
    ]
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId | None
    name: str | None
    category_logical_id: ContextLogicalId | None
    start_date: date | None
    end_date: date | None
    severity: str | None
    stress_level: str | None
    note: str | None
    withdrawal_reason: str | None
    expected_snapshot_id: SnapshotId
    snapshot_as_of: datetime
    context_as_of_date: date
    context_timezone: str


@dataclass(frozen=True, slots=True)
class IllnessPublicationResult:
    logical_id: ContextLogicalId
    revision_id: ContextRevisionId
    snapshot_id: SnapshotId


def _execute_script(metadata: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            metadata.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.DatabaseError("incomplete schema statement")


def _normalized_context_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def _manual_payload_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _ensure_current_tables(metadata: sqlite3.Connection) -> None:
    _execute_script(
        metadata,
        f"""
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
            request_kind TEXT NOT NULL CHECK (
                request_kind IN ('import_health_export', 'resolve_data_review_case',
                                 'revoke_data_review_decision',
                                 'create_plausibility_rule_version',
                                 'run_historical_review', 'migrate_store',
                                 'rollback_migration', 'revise_context_coverage_start',
                                 'revise_medication_regime', 'revise_medication_deviation',
                                 'revise_as_needed_intake', 'revise_intake_reason_category')
            ),
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
            activation_kind TEXT NOT NULL CHECK (
                activation_kind IN (
                    'import', 'data_review_decision', 'rule_version', 'historical',
                    'migration', 'manual_context_revision'
                )
            ),
            activated_at_utc TEXT NOT NULL CHECK (
                length(activated_at_utc) >= 20 AND substr(activated_at_utc, 11, 1) = 'T'
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS snapshot_contract_bindings (
            snapshot_id TEXT PRIMARY KEY REFERENCES dataset_snapshots(snapshot_id),
            snapshot_as_of TEXT NOT NULL,
            context_timezone TEXT NOT NULL CHECK (context_timezone != ''),
            context_as_of_date TEXT NOT NULL CHECK (length(context_as_of_date) = 10),
            medication_as_of TEXT NOT NULL,
            CHECK (snapshot_as_of = medication_as_of)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS active_snapshot (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(snapshot_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS activity_derivation_versions (
            version_id TEXT PRIMARY KEY,
            coverage_gap_minutes INTEGER NOT NULL CHECK (
                coverage_gap_minutes BETWEEN 1 AND 1440
            ),
            source_classifier_version TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS activity_derivation_active (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            version_id TEXT NOT NULL REFERENCES activity_derivation_versions(version_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS activity_derivation_snapshot_bindings (
            snapshot_id TEXT PRIMARY KEY REFERENCES dataset_snapshots(snapshot_id),
            version_id TEXT NOT NULL REFERENCES activity_derivation_versions(version_id)
        ) STRICT;
        CREATE TRIGGER IF NOT EXISTS activity_derivation_versions_no_update
        BEFORE UPDATE ON activity_derivation_versions
        BEGIN SELECT RAISE(ABORT, 'activity derivation versions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS activity_derivation_versions_no_delete
        BEFORE DELETE ON activity_derivation_versions
        BEGIN SELECT RAISE(ABORT, 'activity derivation versions are append-only'); END;
        CREATE TABLE IF NOT EXISTS import_measurement_versions (
            import_id TEXT NOT NULL REFERENCES imports(import_id),
            measurement_version_id TEXT NOT NULL CHECK (
                length(measurement_version_id) = 64
                AND measurement_version_id NOT GLOB '*[^0-9a-f]*'
            ),
            PRIMARY KEY (import_id, measurement_version_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS import_canonical_counts (
            import_id TEXT PRIMARY KEY REFERENCES imports(import_id),
            logical_measurement_count INTEGER NOT NULL CHECK (logical_measurement_count >= 0),
            measurement_version_count INTEGER NOT NULL CHECK (measurement_version_count >= 0),
            source_occurrence_count INTEGER NOT NULL CHECK (source_occurrence_count >= 0),
            anomaly_count INTEGER NOT NULL CHECK (anomaly_count >= 0)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS unsupported_import_content (
            import_id TEXT NOT NULL REFERENCES imports(import_id),
            category TEXT NOT NULL CHECK (
                category IN (
                    'record_type', 'sleep_value', 'top_level_element', 'unit',
                    'workout_activity_type', 'workout_child'
                )
            ),
            external_identifier TEXT NOT NULL CHECK (length(external_identifier) > 0),
            count INTEGER NOT NULL CHECK (count > 0),
            PRIMARY KEY (import_id, category, external_identifier)
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
                    'source_deletion', 'conflict_resolution', 'confirmation',
                    'source_value_acceptance'
                )
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS rule_version_refs (
            rule_version_id TEXT PRIMARY KEY,
            rule_kind TEXT NOT NULL CHECK (
                rule_kind IN ('identity', 'mapping', 'plausibility')
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS plausibility_rule_versions (
            rule_version_id TEXT NOT NULL REFERENCES rule_version_refs(rule_version_id),
            data_type TEXT NOT NULL CHECK (
                data_type IN ({_CANONICAL_HEALTH_TYPES_SQL})
            ),
            canonical_unit TEXT NOT NULL CHECK (canonical_unit IN ({_CANONICAL_UNITS_SQL})),
            fixed_lower_bound REAL,
            fixed_upper_bound REAL,
            personal_range_enabled INTEGER NOT NULL CHECK (personal_range_enabled IN (0, 1)),
            effective_from TEXT,
            effective_timezone TEXT,
            effective_offset_minutes INTEGER,
            created_at TEXT NOT NULL,
            recommendation_id TEXT,
            PRIMARY KEY (rule_version_id, data_type),
            CHECK (fixed_lower_bound IS NULL OR fixed_lower_bound = fixed_lower_bound),
            CHECK (fixed_upper_bound IS NULL OR fixed_upper_bound = fixed_upper_bound),
            CHECK (fixed_lower_bound IS NULL OR fixed_upper_bound IS NULL
                   OR fixed_lower_bound < fixed_upper_bound)
        ) STRICT;
        CREATE UNIQUE INDEX IF NOT EXISTS one_initial_plausibility_rule
        ON plausibility_rule_versions(data_type) WHERE effective_from IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS one_plausibility_rule_per_boundary
        ON plausibility_rule_versions(data_type, effective_from)
        WHERE effective_from IS NOT NULL;
        CREATE TRIGGER IF NOT EXISTS plausibility_rule_versions_no_update
        BEFORE UPDATE ON plausibility_rule_versions
        BEGIN SELECT RAISE(ABORT, 'plausibility rules are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS plausibility_rule_versions_no_delete
        BEFORE DELETE ON plausibility_rule_versions
        BEGIN SELECT RAISE(ABORT, 'plausibility rules are append-only'); END;
        CREATE TABLE IF NOT EXISTS source_type_catalog (
            source_type TEXT PRIMARY KEY,
            review_case_id TEXT NOT NULL UNIQUE CHECK (
                length(review_case_id) = 32
                AND review_case_id NOT GLOB '*[^0-9a-f]*'
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS review_cycles (
            cycle_id TEXT PRIMARY KEY CHECK (
                length(cycle_id) = 32 AND cycle_id NOT GLOB '*[^0-9a-f]*'
            ),
            snapshot_id TEXT NOT NULL UNIQUE REFERENCES dataset_snapshots(snapshot_id),
            cycle_kind TEXT NOT NULL CHECK (
                cycle_kind IN ('import', 'rule_version', 'historical')
            ),
            status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
            open_case_count INTEGER NOT NULL CHECK (open_case_count >= 0),
            CHECK (
                (status = 'open' AND open_case_count > 0)
                OR (status = 'closed' AND open_case_count = 0)
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS review_cycle_cases (
            cycle_id TEXT NOT NULL REFERENCES review_cycles(cycle_id),
            review_case_id TEXT NOT NULL,
            PRIMARY KEY (cycle_id, review_case_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS historical_review_cycles (
            cycle_id TEXT PRIMARY KEY REFERENCES review_cycles(cycle_id),
            base_snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(snapshot_id),
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            rule_version_id TEXT NOT NULL REFERENCES rule_version_refs(rule_version_id),
            CHECK (start_date <= end_date)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS audit_events (
            audit_position INTEGER PRIMARY KEY CHECK (audit_position > 0),
            audit_event_id TEXT NOT NULL UNIQUE CHECK (
                length(audit_event_id) = 32 AND audit_event_id NOT GLOB '*[^0-9a-f]*'
            ),
            operation_id TEXT NOT NULL REFERENCES write_operations(operation_id),
            event_kind TEXT NOT NULL CHECK (
                event_kind IN (
                    'import_published', 'data_review_decision', 'metadata_tombstone',
                    'store_migrated', 'manual_context_revision', 'manual_medication_revision'
                )
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
        CREATE TABLE IF NOT EXISTS migration_publications (
            audit_event_id TEXT PRIMARY KEY REFERENCES audit_events(audit_event_id),
            snapshot_id TEXT REFERENCES dataset_snapshots(snapshot_id),
            source_schema_version INTEGER NOT NULL CHECK (source_schema_version > 0),
            target_schema_version INTEGER NOT NULL CHECK (
                target_schema_version >= source_schema_version
            ),
            snapshot_source_schema_version INTEGER CHECK (
                snapshot_source_schema_version IS NULL
                OR snapshot_source_schema_version > 0
            ),
            snapshot_target_schema_version INTEGER CHECK (
                snapshot_target_schema_version IS NULL
                OR snapshot_target_schema_version > snapshot_source_schema_version
            ),
            backup_file TEXT NOT NULL,
            CHECK (
                (snapshot_source_schema_version IS NULL)
                    = (snapshot_target_schema_version IS NULL)
            ),
            CHECK (
                target_schema_version > source_schema_version
                OR snapshot_target_schema_version > snapshot_source_schema_version
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS restored_publications (
            audit_event_id TEXT PRIMARY KEY REFERENCES audit_events(audit_event_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS snapshot_restore_origins (
            snapshot_id TEXT PRIMARY KEY REFERENCES dataset_snapshots(snapshot_id),
            backup_id TEXT NOT NULL CHECK (
                length(backup_id) = 32 AND backup_id NOT GLOB '*[^0-9a-f]*'
            ),
            source_snapshot_id TEXT NOT NULL CHECK (
                length(source_snapshot_id) = 32
                AND source_snapshot_id NOT GLOB '*[^0-9a-f]*'
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS manual_revision_intents (
            revision_id TEXT PRIMARY KEY CHECK (
                length(revision_id) = 32 AND revision_id NOT GLOB '*[^0-9a-f]*'
            ),
            intent TEXT NOT NULL CHECK (intent IN ('create', 'revise', 'withdraw', 'restore')),
            withdrawal_reason TEXT
        ) STRICT;
        CREATE TABLE IF NOT EXISTS manual_context_revisions (
            revision_id TEXT PRIMARY KEY CHECK (
                length(revision_id) = 32 AND revision_id NOT GLOB '*[^0-9a-f]*'
            ),
            logical_id TEXT NOT NULL CHECK (
                length(logical_id) = 32 AND logical_id NOT GLOB '*[^0-9a-f]*'
            ),
            object_kind TEXT NOT NULL CHECK (object_kind IN (
                'context_coverage_start', 'illness_category', 'illness_period',
                'daily_stress', 'custom_context_label', 'custom_context_period'
            )),
            previous_revision_id TEXT UNIQUE REFERENCES manual_context_revisions(revision_id),
            state TEXT NOT NULL CHECK (state IN ('active', 'withdrawn')),
            operation_id TEXT NOT NULL UNIQUE REFERENCES write_operations(operation_id),
            created_at_utc TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL CHECK (
                length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS context_coverage_start_values (
            revision_id TEXT PRIMARY KEY REFERENCES manual_context_revisions(revision_id),
            start_date TEXT NOT NULL CHECK (length(start_date) = 10)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS manual_context_snapshot_bindings (
            snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(snapshot_id),
            revision_id TEXT NOT NULL REFERENCES manual_context_revisions(revision_id)
            , PRIMARY KEY (snapshot_id, revision_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS manual_context_publications (
            audit_event_id TEXT PRIMARY KEY REFERENCES audit_events(audit_event_id),
            revision_id TEXT NOT NULL UNIQUE REFERENCES manual_context_revisions(revision_id),
            snapshot_id TEXT NOT NULL UNIQUE REFERENCES dataset_snapshots(snapshot_id),
            context_as_of_date TEXT NOT NULL CHECK (length(context_as_of_date) = 10),
            context_timezone TEXT NOT NULL
        ) STRICT;
        CREATE TRIGGER IF NOT EXISTS manual_context_revisions_no_update
        BEFORE UPDATE ON manual_context_revisions
        BEGIN SELECT RAISE(ABORT, 'manual context revisions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS manual_context_revisions_no_delete
        BEFORE DELETE ON manual_context_revisions
        BEGIN SELECT RAISE(ABORT, 'manual context revisions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS manual_context_revisions_same_chain
        BEFORE INSERT ON manual_context_revisions
        WHEN NEW.previous_revision_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM manual_context_revisions previous
            WHERE previous.revision_id = NEW.previous_revision_id
              AND (previous.logical_id != NEW.logical_id OR previous.object_kind != NEW.object_kind)
        )
        BEGIN SELECT RAISE(ABORT, 'manual context revision chain changed object'); END;
        CREATE TRIGGER IF NOT EXISTS context_coverage_start_values_no_update
        BEFORE UPDATE ON context_coverage_start_values
        BEGIN SELECT RAISE(ABORT, 'manual context values are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS context_coverage_start_values_no_delete
        BEFORE DELETE ON context_coverage_start_values
        BEGIN SELECT RAISE(ABORT, 'manual context values are immutable'); END;
        CREATE TABLE IF NOT EXISTS illness_category_values (
            revision_id TEXT PRIMARY KEY REFERENCES manual_context_revisions(revision_id),
            name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
            name_key TEXT NOT NULL CHECK (length(name_key) BETWEEN 1 AND 80)
        ) STRICT;
        CREATE UNIQUE INDEX IF NOT EXISTS illness_category_name_reserved
        ON illness_category_values(name_key);
        CREATE TABLE IF NOT EXISTS daily_stress_values (
            revision_id TEXT PRIMARY KEY REFERENCES manual_context_revisions(revision_id),
            day TEXT NOT NULL CHECK (length(day) = 10),
            level TEXT NOT NULL CHECK (level IN ('very_low', 'low', 'average', 'high', 'very_high'))
        ) STRICT;
        CREATE TABLE IF NOT EXISTS custom_context_label_values (
            revision_id TEXT PRIMARY KEY REFERENCES manual_context_revisions(revision_id),
            name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
            name_key TEXT NOT NULL CHECK (length(name_key) BETWEEN 1 AND 80)
        ) STRICT;
        CREATE UNIQUE INDEX IF NOT EXISTS custom_context_label_name_reserved
        ON custom_context_label_values(name_key);
        CREATE TABLE IF NOT EXISTS custom_context_period_values (
            revision_id TEXT PRIMARY KEY REFERENCES manual_context_revisions(revision_id),
            label_logical_id TEXT NOT NULL,
            start_date TEXT NOT NULL CHECK (length(start_date) = 10),
            end_date TEXT CHECK (end_date IS NULL OR length(end_date) = 10),
            note TEXT CHECK (note IS NULL OR length(note) <= 1000)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS illness_period_values (
            revision_id TEXT PRIMARY KEY REFERENCES manual_context_revisions(revision_id),
            category_logical_id TEXT NOT NULL CHECK (
                length(category_logical_id) = 32 AND category_logical_id NOT GLOB '*[^0-9a-f]*'
            ),
            start_date TEXT NOT NULL CHECK (length(start_date) = 10),
            end_date TEXT CHECK (end_date IS NULL OR length(end_date) = 10),
            severity TEXT NOT NULL CHECK (severity IN ('mild', 'moderate', 'severe')),
            CHECK (end_date IS NULL OR start_date <= end_date)
        ) STRICT;
        CREATE TRIGGER IF NOT EXISTS illness_category_values_no_update
        BEFORE UPDATE ON illness_category_values
        BEGIN SELECT RAISE(ABORT, 'illness category values are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS illness_category_values_no_delete
        BEFORE DELETE ON illness_category_values
        BEGIN SELECT RAISE(ABORT, 'illness category values are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS illness_period_values_no_update
        BEFORE UPDATE ON illness_period_values
        BEGIN SELECT RAISE(ABORT, 'illness period values are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS illness_period_values_no_delete
        BEFORE DELETE ON illness_period_values
        BEGIN SELECT RAISE(ABORT, 'illness period values are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS manual_context_snapshot_bindings_no_update
        BEFORE UPDATE ON manual_context_snapshot_bindings
        BEGIN SELECT RAISE(ABORT, 'manual context bindings are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS manual_context_snapshot_bindings_no_delete
        BEFORE DELETE ON manual_context_snapshot_bindings
        BEGIN SELECT RAISE(ABORT, 'manual context bindings are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS manual_context_publications_no_update
        BEFORE UPDATE ON manual_context_publications
        BEGIN SELECT RAISE(ABORT, 'manual context publications are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS manual_context_publications_no_delete
        BEFORE DELETE ON manual_context_publications
        BEGIN SELECT RAISE(ABORT, 'manual context publications are immutable'); END;
        CREATE TABLE IF NOT EXISTS medication_regime_revisions (
            revision_id TEXT PRIMARY KEY CHECK (
                length(revision_id) = 32 AND revision_id NOT GLOB '*[^0-9a-f]*'
            ),
            logical_id TEXT NOT NULL CHECK (
                length(logical_id) = 32 AND logical_id NOT GLOB '*[^0-9a-f]*'
            ),
            previous_revision_id TEXT UNIQUE REFERENCES medication_regime_revisions(revision_id),
            operation_id TEXT NOT NULL UNIQUE REFERENCES write_operations(operation_id),
            created_at_utc TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL CHECK (
                length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS medication_regime_values (
            revision_id TEXT PRIMARY KEY REFERENCES medication_regime_revisions(revision_id),
            starts_at TEXT NOT NULL,
            timezone TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS medication_scheduled_doses (
            dose_id TEXT PRIMARY KEY CHECK (
                length(dose_id) = 32 AND dose_id NOT GLOB '*[^0-9a-f]*'
            ),
            revision_id TEXT NOT NULL REFERENCES medication_regime_revisions(revision_id),
            medication_name TEXT NOT NULL CHECK (length(medication_name) BETWEEN 1 AND 120),
            amount TEXT NOT NULL,
            unit TEXT NOT NULL CHECK (length(unit) BETWEEN 1 AND 32),
            local_time TEXT NOT NULL CHECK (length(local_time) = 8),
            weekdays TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS medication_as_needed_entries (
            entry_id TEXT NOT NULL CHECK (
                length(entry_id) = 32 AND entry_id NOT GLOB '*[^0-9a-f]*'
            ),
            revision_id TEXT NOT NULL REFERENCES medication_regime_revisions(revision_id),
            medication_name TEXT NOT NULL CHECK (length(medication_name) BETWEEN 1 AND 120),
            amount TEXT NOT NULL,
            unit TEXT NOT NULL CHECK (length(unit) BETWEEN 1 AND 32),
            preferred_reason_category_ids TEXT NOT NULL,
            PRIMARY KEY (revision_id, entry_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS medication_snapshot_bindings (
            snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(snapshot_id),
            revision_id TEXT NOT NULL REFERENCES medication_regime_revisions(revision_id),
            PRIMARY KEY (snapshot_id, revision_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS medication_publications (
            audit_event_id TEXT PRIMARY KEY REFERENCES audit_events(audit_event_id),
            revision_id TEXT NOT NULL UNIQUE REFERENCES medication_regime_revisions(revision_id),
            snapshot_id TEXT NOT NULL UNIQUE REFERENCES dataset_snapshots(snapshot_id),
            medication_as_of TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS medication_deviation_revisions (
            revision_id TEXT PRIMARY KEY CHECK (
                length(revision_id) = 32 AND revision_id NOT GLOB '*[^0-9a-f]*'
            ),
            logical_id TEXT NOT NULL CHECK (
                length(logical_id) = 32 AND logical_id NOT GLOB '*[^0-9a-f]*'
            ),
            previous_revision_id TEXT UNIQUE REFERENCES medication_deviation_revisions(revision_id),
            operation_id TEXT NOT NULL UNIQUE REFERENCES write_operations(operation_id),
            state TEXT NOT NULL CHECK (state IN ('active', 'withdrawn')),
            created_at_utc TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL CHECK (
                length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS medication_deviation_values (
            revision_id TEXT PRIMARY KEY REFERENCES medication_deviation_revisions(revision_id),
            regime_logical_id TEXT NOT NULL CHECK (
                length(regime_logical_id) = 32 AND regime_logical_id NOT GLOB '*[^0-9a-f]*'
            ),
            scheduled_at TEXT NOT NULL
            , withdrawal_reason TEXT
        ) STRICT;
        CREATE TABLE IF NOT EXISTS medication_deviation_intakes (
            revision_id TEXT NOT NULL REFERENCES medication_deviation_revisions(revision_id),
            taken_at TEXT NOT NULL,
            amount TEXT NOT NULL,
            PRIMARY KEY (revision_id, taken_at, amount)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS medication_deviation_snapshot_bindings (
            snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(snapshot_id),
            revision_id TEXT NOT NULL REFERENCES medication_deviation_revisions(revision_id),
            PRIMARY KEY (snapshot_id, revision_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS medication_deviation_publications (
            audit_event_id TEXT PRIMARY KEY REFERENCES audit_events(audit_event_id),
            revision_id TEXT NOT NULL UNIQUE REFERENCES medication_deviation_revisions(revision_id),
            snapshot_id TEXT NOT NULL UNIQUE REFERENCES dataset_snapshots(snapshot_id),
            medication_as_of TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS intake_reason_category_revisions (
            revision_id TEXT PRIMARY KEY CHECK (
                length(revision_id) = 32 AND revision_id NOT GLOB '*[^0-9a-f]*'
            ),
            logical_id TEXT NOT NULL CHECK (
                length(logical_id) = 32 AND logical_id NOT GLOB '*[^0-9a-f]*'
            ),
            previous_revision_id TEXT UNIQUE
                REFERENCES intake_reason_category_revisions(revision_id),
            operation_id TEXT NOT NULL UNIQUE REFERENCES write_operations(operation_id),
            state TEXT NOT NULL CHECK (state IN ('active', 'withdrawn')),
            created_at_utc TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL CHECK (
                length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS intake_reason_category_values (
            revision_id TEXT PRIMARY KEY REFERENCES intake_reason_category_revisions(revision_id),
            name TEXT,
            name_key TEXT
        ) STRICT;
        CREATE TABLE IF NOT EXISTS intake_reason_category_snapshot_bindings (
            snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(snapshot_id),
            revision_id TEXT NOT NULL REFERENCES intake_reason_category_revisions(revision_id),
            PRIMARY KEY (snapshot_id, revision_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS intake_reason_category_publications (
            audit_event_id TEXT PRIMARY KEY REFERENCES audit_events(audit_event_id),
            revision_id TEXT NOT NULL UNIQUE
                REFERENCES intake_reason_category_revisions(revision_id),
            snapshot_id TEXT NOT NULL UNIQUE REFERENCES dataset_snapshots(snapshot_id),
            medication_as_of TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS as_needed_intake_revisions (
            revision_id TEXT PRIMARY KEY CHECK (
                length(revision_id) = 32 AND revision_id NOT GLOB '*[^0-9a-f]*'
            ),
            logical_id TEXT NOT NULL CHECK (
                length(logical_id) = 32 AND logical_id NOT GLOB '*[^0-9a-f]*'
            ),
            previous_revision_id TEXT UNIQUE REFERENCES as_needed_intake_revisions(revision_id),
            operation_id TEXT NOT NULL UNIQUE REFERENCES write_operations(operation_id),
            state TEXT NOT NULL CHECK (state IN ('active', 'withdrawn')),
            created_at_utc TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL CHECK (
                length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS as_needed_intake_values (
            revision_id TEXT PRIMARY KEY REFERENCES as_needed_intake_revisions(revision_id),
            regime_logical_id TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            taken_at TEXT NOT NULL,
            amount TEXT NOT NULL,
            reason_category_logical_id TEXT,
            withdrawal_reason TEXT
        ) STRICT;
        CREATE TABLE IF NOT EXISTS as_needed_intake_snapshot_bindings (
            snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(snapshot_id),
            revision_id TEXT NOT NULL REFERENCES as_needed_intake_revisions(revision_id),
            PRIMARY KEY (snapshot_id, revision_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS as_needed_intake_publications (
            audit_event_id TEXT PRIMARY KEY REFERENCES audit_events(audit_event_id),
            revision_id TEXT NOT NULL UNIQUE REFERENCES as_needed_intake_revisions(revision_id),
            snapshot_id TEXT NOT NULL UNIQUE REFERENCES dataset_snapshots(snapshot_id),
            medication_as_of TEXT NOT NULL
        ) STRICT;
        CREATE TRIGGER IF NOT EXISTS manual_revision_intents_owner
        BEFORE INSERT ON manual_revision_intents
        WHEN (
            SELECT count(*) FROM (
                SELECT revision_id FROM manual_context_revisions
                UNION ALL SELECT revision_id FROM medication_regime_revisions
                UNION ALL SELECT revision_id FROM medication_deviation_revisions
                UNION ALL SELECT revision_id FROM intake_reason_category_revisions
                UNION ALL SELECT revision_id FROM as_needed_intake_revisions
            ) WHERE revision_id = NEW.revision_id
        ) != 1
        BEGIN SELECT RAISE(ABORT, 'manual revision intent owner invalid'); END;
        CREATE TRIGGER IF NOT EXISTS manual_revision_intents_no_update
        BEFORE UPDATE ON manual_revision_intents
        BEGIN SELECT RAISE(ABORT, 'manual revision intents are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS manual_revision_intents_no_delete
        BEFORE DELETE ON manual_revision_intents
        BEGIN SELECT RAISE(ABORT, 'manual revision intents are append-only'); END;
        CREATE TABLE IF NOT EXISTS metadata_restores (
            restore_id TEXT PRIMARY KEY CHECK (
                length(restore_id) = 32 AND restore_id NOT GLOB '*[^0-9a-f]*'
            ),
            operation_id TEXT NOT NULL UNIQUE CHECK (
                length(operation_id) = 32 AND operation_id NOT GLOB '*[^0-9a-f]*'
            ),
            backup_id TEXT NOT NULL CHECK (
                length(backup_id) = 32 AND backup_id NOT GLOB '*[^0-9a-f]*'
            ),
            source_store_id TEXT NOT NULL CHECK (
                length(source_store_id) = 32
                AND source_store_id NOT GLOB '*[^0-9a-f]*'
            ),
            original_backup_sha256 TEXT NOT NULL CHECK (
                length(original_backup_sha256) = 64
                AND original_backup_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            canonical_content_sha256 TEXT NOT NULL CHECK (
                length(canonical_content_sha256) = 64
                AND canonical_content_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            working_copy_sha256 TEXT NOT NULL CHECK (
                length(working_copy_sha256) = 64
                AND working_copy_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            audit_max_position INTEGER NOT NULL CHECK (audit_max_position >= 0),
            source_schema_version INTEGER NOT NULL CHECK (source_schema_version > 0),
            target_schema_version INTEGER NOT NULL CHECK (
                target_schema_version >= source_schema_version
            ),
            started_at_utc TEXT NOT NULL CHECK (
                length(started_at_utc) >= 20 AND substr(started_at_utc, 11, 1) = 'T'
            ),
            activated_at_utc TEXT CHECK (
                activated_at_utc IS NULL OR (
                    length(activated_at_utc) >= 20
                    AND substr(activated_at_utc, 11, 1) = 'T'
                )
            )
        ) STRICT;
        CREATE TABLE IF NOT EXISTS data_review_decisions (
            audit_event_id TEXT PRIMARY KEY REFERENCES audit_events(audit_event_id),
            decision_id TEXT NOT NULL UNIQUE REFERENCES decision_refs(decision_id),
            review_case_id TEXT,
            case_kind TEXT NOT NULL CHECK (
                case_kind IN (
                    'plausibility', 'continued_override', 'suspected_source_deletion',
                    'source_conflict', 'preferred_daily_weight_conflict', 'direct_correction'
                )
            ),
            logical_measurement_id TEXT NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            action TEXT NOT NULL CHECK (
                action IN (
                    'confirm', 'reject', 'prefer', 'split', 'correct',
                    'exclude_local', 'accept_source'
                )
            ),
            selected_measurement_version_id TEXT,
            previous_measurement_version_id TEXT NOT NULL,
            candidate_version_ids TEXT NOT NULL,
            note TEXT,
            mandatory_reason TEXT
        ) STRICT;
        CREATE TABLE IF NOT EXISTS data_review_batch_actions (
            batch_action_id TEXT PRIMARY KEY CHECK (
                length(batch_action_id) = 32 AND batch_action_id NOT GLOB '*[^0-9a-f]*'
            ),
            operation_id TEXT NOT NULL UNIQUE REFERENCES write_operations(operation_id),
            selection_kind TEXT,
            materialized_matches TEXT NOT NULL,
            match_count INTEGER NOT NULL CHECK (match_count >= 0),
            note TEXT
        ) STRICT;
        CREATE TABLE IF NOT EXISTS data_review_batch_members (
            batch_action_id TEXT NOT NULL REFERENCES data_review_batch_actions(batch_action_id),
            decision_id TEXT NOT NULL UNIQUE REFERENCES decision_refs(decision_id),
            PRIMARY KEY (batch_action_id, decision_id)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS source_absence_suppressions (
            logical_measurement_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL REFERENCES decision_refs(decision_id),
            active INTEGER NOT NULL CHECK (active IN (0, 1))
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
        CREATE TRIGGER IF NOT EXISTS migration_publications_no_update
        BEFORE UPDATE ON migration_publications
        BEGIN SELECT RAISE(ABORT, 'audit payload is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS migration_publications_no_delete
        BEFORE DELETE ON migration_publications
        BEGIN SELECT RAISE(ABORT, 'audit payload is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS migration_publications_kind
        BEFORE INSERT ON migration_publications
        WHEN (SELECT event_kind FROM audit_events WHERE audit_event_id = NEW.audit_event_id)
             != 'store_migrated'
        BEGIN SELECT RAISE(ABORT, 'wrong audit payload type'); END;
        CREATE TRIGGER IF NOT EXISTS data_review_decisions_kind
        BEFORE INSERT ON data_review_decisions
        WHEN (SELECT event_kind FROM audit_events WHERE audit_event_id = NEW.audit_event_id)
             != 'data_review_decision'
        BEGIN SELECT RAISE(ABORT, 'wrong audit payload type'); END;
        CREATE TRIGGER IF NOT EXISTS data_review_batch_actions_no_update
        BEFORE UPDATE ON data_review_batch_actions
        BEGIN SELECT RAISE(ABORT, 'batch audit is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS data_review_batch_actions_no_delete
        BEFORE DELETE ON data_review_batch_actions
        BEGIN SELECT RAISE(ABORT, 'batch audit is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS data_review_batch_members_no_update
        BEFORE UPDATE ON data_review_batch_members
        BEGIN SELECT RAISE(ABORT, 'batch audit is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS data_review_batch_members_no_delete
        BEFORE DELETE ON data_review_batch_members
        BEGIN SELECT RAISE(ABORT, 'batch audit is append-only'); END;
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
        """,
    )
    metadata.executemany(
        "INSERT OR IGNORE INTO rule_version_refs VALUES (?, ?)",
        (
            (_IDENTITY_RULE_VERSION, "identity"),
            (_MAPPING_RULE_VERSION, "mapping"),
            (_FIXED_PLAUSIBILITY_RULE_VERSION, "plausibility"),
        ),
    )
    metadata.executemany(
        "INSERT OR IGNORE INTO plausibility_rule_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                _FIXED_PLAUSIBILITY_RULE_VERSION,
                "apple_resting_heart_rate",
                "count/min",
                20.0,
                250.0,
                1,
                None,
                None,
                None,
                "1970-01-01T00:00:00+00:00",
                None,
            ),
            (
                _FIXED_PLAUSIBILITY_RULE_VERSION,
                "active_energy",
                "kcal",
                0.0,
                None,
                0,
                None,
                None,
                None,
                "1970-01-01T00:00:00+00:00",
                None,
            ),
            *(
                (
                    _FIXED_PLAUSIBILITY_RULE_VERSION,
                    data_type,
                    unit,
                    0.0,
                    None,
                    0,
                    None,
                    None,
                    None,
                    "1970-01-01T00:00:00+00:00",
                    None,
                )
                for data_type, unit in (
                    ("apple_exercise_time", "min"),
                    ("step_count", "count"),
                    ("walking_running_distance", "km"),
                )
            ),
        ),
    )
    metadata.execute(
        "INSERT OR IGNORE INTO activity_derivation_versions VALUES (?, ?, ?, ?)",
        (
            "activity-derivation/v1",
            240,
            "activity-source-classification/v1",
            "1970-01-01T00:00:00+00:00",
        ),
    )
    metadata.execute(
        "INSERT OR IGNORE INTO activity_derivation_active VALUES (1, 'activity-derivation/v1')"
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
                data_status TEXT NOT NULL,
                data_status_reasons TEXT NOT NULL,
                maturity_criteria TEXT NOT NULL,
                reproducibility TEXT NOT NULL,
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
        "data_status": "TEXT NOT NULL DEFAULT 'reviewed'",
        "data_status_reasons": "TEXT NOT NULL DEFAULT '[]'",
        "maturity_criteria": "TEXT NOT NULL DEFAULT '[]'",
        "reproducibility": "TEXT NOT NULL DEFAULT 'not_recorded'",
        "diagnostics": "TEXT NOT NULL DEFAULT '[]'",
    }
    for column, declaration in migrations.items():
        if column not in analysis_columns:
            metadata.execute(f"ALTER TABLE analysis_runs ADD COLUMN {column} {declaration}")
    migration_columns = {
        str(row[1]) for row in metadata.execute("PRAGMA table_info(migration_publications)")
    }
    if migration_columns and "backup_file" not in migration_columns:
        metadata.execute("ALTER TABLE migration_publications ADD COLUMN backup_file TEXT")


def _upgrade_migration_event_constraints(metadata: sqlite3.Connection) -> None:
    upgrades = (
        (
            "write_operations",
            "'rollback_migration'",
            ("'run_historical_review', 'migrate_store'", "'run_historical_review'"),
            "'run_historical_review', 'migrate_store', 'rollback_migration'",
        ),
        (
            "snapshot_activations",
            "'migration'",
            ("'historical'",),
            "'historical', 'migration'",
        ),
        (
            "audit_events",
            "'store_migrated'",
            ("'metadata_tombstone'",),
            "'metadata_tombstone', 'store_migrated'",
        ),
    )
    rebuilt: list[tuple[str, str, str]] = []
    for table, new_value, old_constraints, new_constraint in upgrades:
        row = metadata.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            continue
        definition = row[0]
        if new_value in definition:
            continue
        old_constraint = next(
            (candidate for candidate in old_constraints if candidate in definition), None
        )
        if old_constraint is None:
            raise sqlite3.DatabaseError(f"unsupported {table} constraint")
        temporary = f"{table}__migration_upgrade"
        upgraded = definition.replace(old_constraint, new_constraint, 1).replace(
            f"CREATE TABLE {table}", f"CREATE TABLE {temporary}", 1
        )
        rebuilt.append((table, temporary, upgraded))
    if not rebuilt:
        return

    if any(table == "audit_events" for table, _, _ in rebuilt):
        for trigger in (
            "import_publications_kind",
            "migration_publications_kind",
            "data_review_decisions_kind",
            "metadata_tombstones_kind",
            "metadata_tombstones_backward",
        ):
            metadata.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table, temporary, definition in rebuilt:
        metadata.execute(f"DROP TABLE IF EXISTS {temporary}")
        metadata.execute(definition)
        metadata.execute(f"INSERT INTO {temporary} SELECT * FROM {table}")
        metadata.execute(f"DROP TABLE {table}")
        metadata.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
    violations = metadata.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError("foreign key violation after constraint upgrade")


def _upgrade_migration_publication_schema(metadata: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in metadata.execute("PRAGMA table_info(migration_publications)")}
    if not columns or "snapshot_source_schema_version" in columns:
        return
    for trigger in (
        "migration_publications_no_update",
        "migration_publications_no_delete",
        "migration_publications_kind",
    ):
        metadata.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    metadata.execute("ALTER TABLE migration_publications RENAME TO legacy_migration_publications")
    metadata.execute(
        """
        CREATE TABLE migration_publications (
            audit_event_id TEXT PRIMARY KEY REFERENCES audit_events(audit_event_id),
            snapshot_id TEXT REFERENCES dataset_snapshots(snapshot_id),
            source_schema_version INTEGER NOT NULL CHECK (source_schema_version > 0),
            target_schema_version INTEGER NOT NULL CHECK (
                target_schema_version >= source_schema_version
            ),
            snapshot_source_schema_version INTEGER CHECK (
                snapshot_source_schema_version IS NULL
                OR snapshot_source_schema_version > 0
            ),
            snapshot_target_schema_version INTEGER CHECK (
                snapshot_target_schema_version IS NULL
                OR snapshot_target_schema_version > snapshot_source_schema_version
            ),
            backup_file TEXT NOT NULL,
            CHECK (
                (snapshot_source_schema_version IS NULL)
                    = (snapshot_target_schema_version IS NULL)
            ),
            CHECK (
                target_schema_version > source_schema_version
                OR snapshot_target_schema_version > snapshot_source_schema_version
            )
        ) STRICT
        """
    )
    metadata.execute(
        "INSERT INTO migration_publications "
        "(audit_event_id, snapshot_id, source_schema_version, target_schema_version, "
        "backup_file) SELECT audit_event_id, snapshot_id, source_schema_version, "
        "target_schema_version, backup_file FROM legacy_migration_publications"
    )
    metadata.execute("DROP TABLE legacy_migration_publications")


def _upgrade_v03_constraints(metadata: sqlite3.Connection) -> None:
    metadata.execute("DROP TRIGGER IF EXISTS manual_revision_intents_owner")
    upgrades = (
        (
            "plausibility_rule_versions",
            (
                (
                    "'active_energy', 'apple_resting_heart_rate')",
                    "'active_energy', 'apple_resting_heart_rate', 'body_mass')",
                ),
                (
                    "'active_energy', 'apple_resting_heart_rate', 'body_mass')",
                    f"{_CANONICAL_HEALTH_TYPES_SQL})",
                ),
                (
                    "canonical_unit IN ('kcal', 'count/min'))",
                    "canonical_unit IN ('kcal', 'count/min', 'kg'))",
                ),
                (
                    "canonical_unit IN ('kcal', 'count/min', 'kg'))",
                    f"canonical_unit IN ({_CANONICAL_UNITS_SQL}))",
                ),
                (
                    f"data_type IN ({_PRE_ACTIVITY_HEALTH_TYPES_SQL})",
                    f"data_type IN ({_CANONICAL_HEALTH_TYPES_SQL})",
                ),
                (
                    f"canonical_unit IN ({_PRE_ACTIVITY_UNITS_SQL})",
                    f"canonical_unit IN ({_CANONICAL_UNITS_SQL})",
                ),
            ),
        ),
        (
            "data_review_decisions",
            (
                (
                    "'source_conflict', 'direct_correction'",
                    "'source_conflict', 'preferred_daily_weight_conflict', 'direct_correction'",
                ),
            ),
        ),
        (
            "manual_context_revisions",
            (
                (
                    "'context_coverage_start', 'illness_category', 'illness_period'",
                    "'context_coverage_start', 'illness_category', 'illness_period', "
                    "'daily_stress', 'custom_context_label', 'custom_context_period'",
                ),
            ),
        ),
    )
    for table, replacements in upgrades:
        row = metadata.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if row is None:
            continue
        if not isinstance(row[0], str):
            raise sqlite3.DatabaseError(f"invalid {table} definition")
        definition = row[0]
        if table == "plausibility_rule_versions" and all(
            value in definition
            for value in (
                f"{_CANONICAL_HEALTH_TYPES_SQL})",
                f"canonical_unit IN ({_CANONICAL_UNITS_SQL}))",
            )
        ):
            continue
        for old, new in replacements:
            if new in definition:
                continue
            if old not in definition:
                raise sqlite3.DatabaseError(f"unsupported {table} constraint")
            definition = definition.replace(old, new, 1)
        temporary = f"{table}__v03_constraint_upgrade"
        upgraded = definition.replace(f"CREATE TABLE {table}", f"CREATE TABLE {temporary}", 1)
        metadata.execute(f"DROP TABLE IF EXISTS {temporary}")
        metadata.execute(upgraded)
        metadata.execute(f"INSERT INTO {temporary} SELECT * FROM {table}")
        metadata.execute(f"DROP TABLE {table}")
        metadata.execute(f"ALTER TABLE {temporary} RENAME TO {table}")


@dataclass(slots=True)
class LocalStore:
    _root: Path
    _mode: DataMode
    _metadata: sqlite3.Connection
    _query: duckdb.DuckDBPyConnection
    _writer_lock: IO[bytes] | None = None
    _scratch_bound: bool = True
    _closed: bool = False

    def copy_metadata_tables(
        self, destination: sqlite3.Connection, table_names: tuple[str, ...]
    ) -> None:
        """Copy an explicit recovery-owned positive list into a portable database."""
        self._require_open()
        for table in table_names:
            if not table.isidentifier():
                raise ValueError("Metadatentabellenname ist ungültig.")
            columns = tuple(self._metadata.execute(f'PRAGMA table_info("{table}")'))
            declaration = ", ".join(
                f'"{column[1]!s}" {str(column[2]) or "BLOB"}' for column in columns
            )
            destination.execute(f'CREATE TABLE "{table}" ({declaration}) STRICT')
            rows = self._metadata.execute(f'SELECT * FROM "{table}"').fetchall()
            if rows:
                placeholders = ", ".join("?" for _ in columns)
                destination.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', rows)

    def validate_recovery_writer(self) -> StoreIdentity:
        self._require_open()
        self._require_writer()
        self._validate_store()
        return self.load_identity()

    def _restore_tables_exist(self) -> bool:
        return bool(
            self._metadata.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
                "AND name = 'metadata_restores'"
            ).fetchone()[0]
            == 1
        )

    @staticmethod
    def _restore_session_facts(row: tuple[object, ...]) -> RestoreSessionFacts:
        return RestoreSessionFacts(
            restore_id=str(row[0]),
            operation_id=str(row[1]),
            backup_id=str(row[2]),
            source_store_id=str(row[3]),
            original_backup_sha256=str(row[4]),
            canonical_content_sha256=str(row[5]),
            working_copy_sha256=str(row[6]),
            audit_max_position=int(str(row[7])),
            source_schema_version=int(str(row[8])),
            target_schema_version=int(str(row[9])),
            completion_status=None if row[10] is None else "activated",
        )

    def load_restore_session(self) -> RestoreSessionFacts | None:
        self._require_open()
        if not self._restore_tables_exist():
            return None
        row = self._metadata.execute(
            "SELECT restore_id, operation_id, backup_id, source_store_id, "
            "original_backup_sha256, canonical_content_sha256, working_copy_sha256, "
            "audit_max_position, source_schema_version, target_schema_version, "
            "activated_at_utc FROM metadata_restores WHERE activated_at_utc IS NULL "
            "ORDER BY started_at_utc DESC LIMIT 1"
        ).fetchone()
        return None if row is None else self._restore_session_facts(row)

    def load_completed_restore(
        self, backup_id: str, canonical_content_sha256: str
    ) -> RestoreSessionFacts | None:
        self._require_open()
        if not self._restore_tables_exist():
            return None
        row = self._metadata.execute(
            "SELECT restore_id, operation_id, backup_id, source_store_id, "
            "original_backup_sha256, canonical_content_sha256, working_copy_sha256, "
            "audit_max_position, source_schema_version, target_schema_version, "
            "activated_at_utc FROM metadata_restores WHERE backup_id = ? "
            "AND canonical_content_sha256 = ? AND activated_at_utc IS NOT NULL LIMIT 1",
            (backup_id, canonical_content_sha256),
        ).fetchone()
        return None if row is None else self._restore_session_facts(row)

    def is_empty_for_restore(self) -> bool:
        self._require_open()
        identity = self.load_identity()
        if identity.person_binding is not PersonBindingStatus.UNBOUND:
            return False
        return all(
            int(self._metadata.execute(f"SELECT count(*) FROM {table}").fetchone()[0]) == 0
            for table in ("write_operations", "imports", "audit_events", "active_snapshot")
        )

    def start_restore_session(self, facts: RestoreSessionFacts) -> None:
        self._require_open()
        self._require_writer()
        if self.load_restore_session() is not None or not self.is_empty_for_restore():
            raise StoreError("restore_store_not_empty")
        now = datetime.now(UTC).isoformat()
        with self._metadata:
            self._metadata.execute(
                "INSERT INTO metadata_restores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    facts.restore_id,
                    facts.operation_id,
                    facts.backup_id,
                    facts.source_store_id,
                    facts.original_backup_sha256,
                    facts.canonical_content_sha256,
                    facts.working_copy_sha256,
                    facts.audit_max_position,
                    facts.source_schema_version,
                    facts.target_schema_version,
                    now,
                ),
            )
            self._metadata.execute(
                "UPDATE store_identity SET store_id = ?, person_binding = 'pending' "
                "WHERE singleton = 1",
                (facts.source_store_id,),
            )

    def load_backup_audit_position(self) -> int:
        self._require_open()
        return int(
            self._metadata.execute(
                "SELECT COALESCE(MAX(audit_position), 0) FROM audit_events"
            ).fetchone()[0]
        )

    def load_backup_import_facts(self) -> tuple[BackupImportFact, ...]:
        self._require_open()
        return tuple(
            BackupImportFact(
                ImportId(str(row[0])),
                OperationId(str(row[1])),
                str(row[2]),
                SnapshotId(str(row[3])),
                int(row[4]),
                datetime.fromisoformat(str(row[5])),
            )
            for row in self._metadata.execute(
                "SELECT import_id, operation_id, status, snapshot_id, record_count, "
                "committed_at FROM imports "
                "WHERE status IN ('committed', 'duplicate', 'quarantined') "
                "ORDER BY import_id"
            )
        )

    def load_backup_snapshot_facts(self) -> tuple[BackupSnapshotFact, ...]:
        self._require_open()
        return tuple(
            BackupSnapshotFact(
                SnapshotId(str(row[0])),
                int(row[1]),
                OperationId(str(row[2])),
                None if row[3] is None else SnapshotId(str(row[3])),
                datetime.fromisoformat(str(row[4])),
            )
            for row in self._metadata.execute(
                "SELECT snapshot_id, snapshot_schema_version, created_by_operation_id, "
                "parent_snapshot_id, created_at_utc FROM dataset_snapshots "
                "ORDER BY snapshot_id"
            )
        )

    def load_backup_snapshot_origin(self) -> BackupSnapshotOrigin | None:
        self._require_open()
        row = self._metadata.execute(
            "SELECT snapshot.snapshot_id, snapshot.snapshot_schema_version, "
            "snapshot.manifest_sha256, binding.snapshot_as_of, binding.context_timezone, "
            "binding.context_as_of_date, binding.medication_as_of, derivation.version_id "
            "FROM active_snapshot active "
            "JOIN dataset_snapshots snapshot USING (snapshot_id) "
            "JOIN snapshot_contract_bindings binding USING (snapshot_id) "
            "JOIN activity_derivation_snapshot_bindings derivation USING (snapshot_id)"
        ).fetchone()
        if row is None:
            return None
        snapshot_id = str(row[0])
        bindings = tuple(
            (str(item[0]), str(item[1]))
            for item in self._metadata.execute(
                "SELECT 'context', revision_id FROM manual_context_snapshot_bindings "
                "WHERE snapshot_id = ? UNION ALL "
                "SELECT 'medication_regime', revision_id FROM medication_snapshot_bindings "
                "WHERE snapshot_id = ? UNION ALL "
                "SELECT 'medication_deviation', revision_id "
                "FROM medication_deviation_snapshot_bindings WHERE snapshot_id = ? UNION ALL "
                "SELECT 'intake_reason_category', revision_id "
                "FROM intake_reason_category_snapshot_bindings WHERE snapshot_id = ? UNION ALL "
                "SELECT 'as_needed_intake', revision_id "
                "FROM as_needed_intake_snapshot_bindings WHERE snapshot_id = ? "
                "ORDER BY 1, 2",
                (snapshot_id,) * 5,
            )
        )
        return BackupSnapshotOrigin(
            SnapshotId(snapshot_id),
            int(row[1]),
            str(row[2]),
            datetime.fromisoformat(str(row[3])),
            str(row[4]),
            date.fromisoformat(str(row[5])),
            datetime.fromisoformat(str(row[6])),
            str(row[7]),
            bindings,
        )

    def load_backup_interval_source_refs(self) -> tuple[tuple[str, str, str], ...]:
        self._require_open()
        snapshot_id = self.load_active_snapshot_id()
        if snapshot_id is None:
            return ()
        directory = self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id)
        refs: list[tuple[str, str, str]] = []
        for family, filename, logical_column, version_column in (
            (
                "sleep",
                "sleep_intervals.parquet",
                "identity_candidate_id",
                "measurement_version_id",
            ),
            ("workout", "workouts.parquet", "logical_workout_id", "workout_version_id"),
        ):
            path = directory / filename
            if not path.exists():
                continue
            escaped = str(path).replace("'", "''")
            refs.extend(
                (family, str(row[0]), str(row[1]))
                for row in self._query.execute(
                    f"SELECT {logical_column}, {version_column} "
                    f"FROM read_parquet('{escaped}') ORDER BY {version_column}"
                ).fetchall()
            )
        return tuple(refs)

    def load_migration_rollback_facts(self) -> MigrationRollbackFacts | None:
        self._require_open()
        columns = {
            str(row[1])
            for row in self._metadata.execute("PRAGMA table_info(migration_publications)")
        }
        if "backup_file" not in columns:
            return None
        row = self._metadata.execute(
            "SELECT operation.operation_id, publication.backup_file, "
            "publication.source_schema_version, publication.target_schema_version, "
            "publication.snapshot_id, activation.previous_snapshot_id, "
            "(SELECT operation_id FROM ("
            "SELECT operation_id, julianday(completed_at_utc) AS completed_at, "
            "0 AS analysis_change, rowid AS local_sequence "
            "FROM write_operations WHERE state_changed = 1 "
            "UNION ALL "
            "SELECT operation_id, julianday(created_at) AS completed_at, "
            "1 AS analysis_change, rowid AS local_sequence "
            "FROM analysis_receipts"
            ") ORDER BY completed_at DESC, analysis_change DESC, "
            "local_sequence DESC LIMIT 1) "
            "FROM migration_publications publication "
            "JOIN audit_events event USING (audit_event_id) "
            "JOIN write_operations operation USING (operation_id) "
            "LEFT JOIN snapshot_activations activation USING (operation_id) "
            "ORDER BY operation.rowid DESC LIMIT 1"
        ).fetchone()
        if row is None or row[1] is None:
            return None
        backup_file = str(row[1])
        current_snapshot = None if row[4] is None else SnapshotId(str(row[4]))
        restored_snapshot = None if row[5] is None else SnapshotId(str(row[5]))
        backup_path = self._root / "migration-backups" / backup_file
        backup_sha256: str | None = None
        backup_exists = Path(backup_file).name == backup_file and backup_path.is_file()
        backup_matches_migration = False
        if backup_exists:
            try:
                with backup_path.open("rb") as source:
                    backup_sha256 = hashlib.file_digest(source, "sha256").hexdigest()
                with sqlite3.connect(
                    f"{backup_path.resolve().as_uri()}?mode=ro", uri=True
                ) as backup:
                    backup_active = backup.execute(
                        "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
                    ).fetchone()
                    backup_identity = backup.execute(
                        "SELECT mode, schema_version FROM store_identity WHERE singleton = 1"
                    ).fetchone()
                backup_matches_migration = backup_identity == (
                    self._mode.value,
                    row[2],
                ) and (None if backup_active is None else str(backup_active[0])) == (
                    None if restored_snapshot is None else str(restored_snapshot)
                )
            except (OSError, sqlite3.Error):
                pass
        return MigrationRollbackFacts(
            migration_operation_id=OperationId(str(row[0])),
            latest_state_change_operation_id=(None if row[6] is None else OperationId(str(row[6]))),
            backup_file=backup_file,
            backup_sha256=backup_sha256,
            backup_exists=backup_exists,
            backup_matches_migration=backup_matches_migration,
            pre_migration_version=int(row[2]),
            post_migration_version=int(row[3]),
            migrated_snapshot_id=current_snapshot,
            previous_snapshot_id=restored_snapshot,
            active_snapshot_id=self.load_active_snapshot_id(),
        )

    def rollback_store_migration(
        self, facts: MigrationRollbackFacts, operation_id: OperationId
    ) -> StoreIdentity:
        self._require_writer()
        if facts != self.load_migration_rollback_facts():
            raise StoreError("migration_rollback_changed")
        backup_path = self._root / "migration-backups" / facts.backup_file
        restored = sqlite3.connect(":memory:")
        try:
            with sqlite3.connect(f"{backup_path.resolve().as_uri()}?mode=ro", uri=True) as backup:
                backup.backup(restored)
            restored.execute("PRAGMA foreign_keys = OFF")
            _upgrade_migration_event_constraints(restored)
            created_at = datetime.now(UTC).isoformat()
            restored.execute(
                "INSERT INTO write_operations VALUES (?, 'rollback_migration', ?, ?, "
                "'committed', 1)",
                (str(operation_id), created_at, created_at),
            )
            restored.commit()
            restored.execute("PRAGMA foreign_keys = ON")
            if restored.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise sqlite3.IntegrityError("rollback integrity check failed")
            if restored.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError("rollback foreign key check failed")
            restored.backup(self._metadata)
        except (OSError, sqlite3.Error) as error:
            raise StoreError("migration_rollback_failed") from error
        finally:
            restored.close()
        identity = self.load_identity()
        if (
            identity.schema_version != str(facts.pre_migration_version)
            or self.load_active_snapshot_id() != facts.previous_snapshot_id
        ):
            raise StoreError("migration_rollback_failed")
        return identity

    def load_review_snapshot_facts(self) -> tuple[ReviewSnapshotFacts, ...]:
        self._require_open()
        snapshots = []
        for snapshot in self.load_backup_snapshot_facts():
            directory = self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot.snapshot_id)
            paths = {
                name: str(directory / name).replace("'", "''")
                for name in (
                    "open_review_cases.parquet",
                    "measurement_versions.parquet",
                    "resolved_measurements.parquet",
                )
            }
            cases = tuple(
                OpenDataReviewCase(
                    str(row[0]),
                    cast(
                        Literal[
                            "plausibility",
                            "workout_plausibility",
                            "workout_overlap",
                            "continued_override",
                            "suspected_source_deletion",
                            "source_conflict",
                            "rule_definition",
                            "preferred_daily_weight_conflict",
                        ],
                        str(row[1]),
                    ),
                    None if row[2] is None else LogicalMeasurementId(str(row[2])),
                    None if row[3] is None else MeasurementVersionId(str(row[3])),
                    None if row[4] is None else str(row[4]),
                    str(row[5]),
                )
                for row in self._query.execute(
                    f"SELECT * FROM read_parquet('{paths['open_review_cases.parquet']}')"
                ).fetchall()
            )
            versions = tuple(
                MeasurementVersionFact(
                    measurement_version_id=str(row[0]),
                    logical_measurement_id=str(row[1]),
                    canonical_type=str(row[2]),
                    canonical_unit=str(row[3]),
                    canonical_value=float(row[4]),
                    source_start_utc=str(row[5]),
                    source_end_utc=str(row[6]),
                    source_updated_at_utc=str(row[7]),
                    source_version=str(row[8]),
                    source_name=str(row[9]),
                    device=str(row[10]),
                    strong_source_id_hash=None if row[11] is None else str(row[11]),
                    measurement_local_date=row[12],
                )
                for row in self._query.execute(
                    "SELECT measurement_version_id, identity_candidate_id, canonical_type, "
                    "canonical_unit, canonical_value, source_start_utc, source_end_utc, "
                    "source_updated_at_utc, source_version, source_name, device, "
                    "strong_source_id_hash, measurement_local_date FROM read_parquet"
                    f"('{paths['measurement_versions.parquet']}')"
                ).fetchall()
            )
            measurements = tuple(
                ResolvedMeasurement(*row)
                for row in self._query.execute(
                    f"SELECT * FROM read_parquet('{paths['resolved_measurements.parquet']}')"
                ).fetchall()
            )
            snapshots.append(
                ReviewSnapshotFacts(snapshot.snapshot_id, cases, versions, measurements)
            )
        return tuple(snapshots)

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

    def contains_health_data(self) -> bool:
        imports_table = self._metadata.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'imports'"
        ).fetchone()
        if (
            imports_table is not None
            and self._metadata.execute(
                "SELECT 1 FROM imports "
                "WHERE status IN ('committed', 'duplicate', 'quarantined') LIMIT 1"
            ).fetchone()
            is not None
        ):
            return True
        snapshots_table = self._metadata.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'snapshots'"
        ).fetchone()
        return (
            snapshots_table is not None
            and self._metadata.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone() is not None
        )

    def preflight_store_migration(self) -> CapacityCheck:
        try:
            metadata_bytes = (self._root / _METADATA_FILE).stat().st_blocks * 512
            fragment_size = os.statvfs(self._root).f_frsize
            active = self.load_active_snapshot_id()
            snapshot_bytes = (
                0
                if active is None
                else sum(
                    path.stat().st_blocks * 512
                    for path in (
                        self._root / _PARQUET_DIRECTORY / "snapshots" / str(active)
                    ).iterdir()
                    if path.is_file()
                )
            )
        except OSError:
            return probe_capacity(self._root, None, method_id=_STORE_MIGRATION_METHOD)
        estimate = cow_migration_estimate(
            metadata_bytes,
            snapshot_bytes,
            fragment_size,
            scratch_bound=self._scratch_bound,
        )
        return probe_capacity(self._root, estimate, method_id=_STORE_MIGRATION_METHOD)

    def migrate_store_schema(
        self,
        steps: tuple[tuple[int, int], ...],
        snapshot_steps: tuple[tuple[int, int], ...],
        snapshot_as_of: datetime | None,
        backup_file: str,
        operation_id: OperationId,
    ) -> StoreIdentity:
        self._require_writer()
        identity = self.load_identity()
        try:
            source_version = int(identity.schema_version)
        except ValueError as error:
            raise StoreConfigurationError(
                "Datenspeicherschema ist keine positive Ganzzahl."
            ) from error
        if steps and (
            steps[0][0] != source_version
            or steps[-1][1] != _STORE_SCHEMA_VERSION
            or any(target != source + 1 for source, target in steps)
            or any(left[1] != right[0] for left, right in pairwise(steps))
        ):
            raise StoreConfigurationError("Migrationskette ist nicht lückenlos registriert.")
        if not steps and source_version != _STORE_SCHEMA_VERSION:
            raise StoreConfigurationError("Migrationskette ist nicht lückenlos registriert.")

        active = self.load_active_snapshot_id()
        snapshot_source_version = (
            None if active is None else self.load_active_snapshot_schema_version()
        )
        if snapshot_steps and (
            snapshot_source_version is None
            or snapshot_steps[0][0] != snapshot_source_version
            or snapshot_steps[-1][1] != _SNAPSHOT_SCHEMA_VERSION
            or any(target != source + 1 for source, target in snapshot_steps)
            or any(left[1] != right[0] for left, right in pairwise(snapshot_steps))
        ):
            raise StoreConfigurationError(
                "Snapshot-Migrationskette ist nicht lückenlos registriert."
            )
        if (
            active is not None
            and not snapshot_steps
            and (snapshot_source_version != _SNAPSHOT_SCHEMA_VERSION)
        ):
            raise StoreConfigurationError(
                "Snapshot-Migrationskette ist nicht lückenlos registriert."
            )
        if active is None and snapshot_steps:
            raise StoreConfigurationError(
                "Snapshot-Migrationskette ist nicht lückenlos registriert."
            )
        if (active is None) != (snapshot_as_of is None) or (
            snapshot_as_of is not None and snapshot_as_of.tzinfo is None
        ):
            raise StoreConfigurationError("Snapshot-Stichtag stimmt nicht mit dem Plan überein.")

        backup_directory = self._root / "migration-backups"
        backup_directory.mkdir(exist_ok=True)
        backup_path = backup_directory / backup_file
        temporary = backup_path.with_suffix(backup_path.suffix + ".tmp")
        if backup_path.exists() or temporary.exists():
            raise StoreError("migration_backup_failed")
        try:
            _migration_backup_fault_point(self._root)
            with sqlite3.connect(temporary) as backup:
                self._metadata.backup(backup)
                backup_identity = backup.execute(
                    "SELECT schema_version FROM store_identity WHERE singleton = 1"
                ).fetchone()
                backup_active = backup.execute(
                    "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
                ).fetchone()
                if (
                    backup.execute("PRAGMA integrity_check").fetchone() != ("ok",)
                    or backup.execute("PRAGMA foreign_key_check").fetchall()
                    or backup_identity != (source_version,)
                    or (active is None and backup_active not in {None, (None,)})
                    or (active is not None and backup_active != (str(active),))
                ):
                    raise sqlite3.IntegrityError("migration backup validation failed")
            os.replace(temporary, backup_path)
            _allocation_checkpoint(self._root, "migration_backup")
            _migration_fault_point(self._root, "migration.after_backup/v1")
        except (OSError, sqlite3.Error) as error:
            temporary.unlink(missing_ok=True)
            raise StoreError("migration_backup_failed") from error

        existing_store_id = identity.store_id
        store_id = str(existing_store_id) if existing_store_id is not None else None
        if store_id is None and active is not None:
            try:
                existing_manifest = json.loads(
                    (
                        self._root
                        / _PARQUET_DIRECTORY
                        / "snapshots"
                        / str(active)
                        / "manifest.json"
                    ).read_bytes()
                )
                manifest_store_id = existing_manifest.get("store_id")
                if _is_lower_hex(manifest_store_id, 32):
                    store_id = str(manifest_store_id)
            except (OSError, AttributeError, json.JSONDecodeError):
                pass
        if store_id is None:
            store_id = uuid4().hex
        person_binding = identity.person_binding
        new_snapshot: SnapshotId | None = None
        manifest_sha256: str | None = None
        created_at = datetime.now(UTC).isoformat()
        marker = self._root / "migration-staging" / f"{operation_id}.json"
        staging: Path | None = None
        snapshot: Path | None = None
        if active is not None:
            assert snapshot_as_of is not None
            new_snapshot = SnapshotId(uuid4().hex)
            staging_root = self._root / "migration-staging"
            staging_root.mkdir(exist_ok=True)
            staging = staging_root / str(operation_id)
            snapshot = self._root / _PARQUET_DIRECTORY / "snapshots" / str(new_snapshot)
            marker.write_text(
                json.dumps(
                    {"operation_id": str(operation_id), "snapshot_id": str(new_snapshot)},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            try:
                manifest_sha256, manifest = self._stage_migration_snapshot(
                    source_snapshot_id=active,
                    snapshot_id=new_snapshot,
                    operation_id=operation_id,
                    staging=staging,
                    created_at=created_at,
                    store_id=store_id,
                    snapshot_as_of=snapshot_as_of,
                )
                _fsync_snapshot(staging)
                _allocation_checkpoint(self._root, "migration_staged")
                _migration_fault_point(self._root, "migration.before_snapshot_move/v1")
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                staging.replace(snapshot)
                _fsync_directory(snapshot.parent)
                _allocation_checkpoint(self._root, "migration_moved")
                _migration_fault_point(self._root, "migration.after_snapshot_move/v1")
            except (OSError, sqlite3.Error, duckdb.Error, StoreError) as error:
                self._quarantine_migration_artifacts(operation_id, staging, snapshot)
                raise StoreError("migration_validation_failed") from error

        self._metadata.commit()
        self._metadata.execute("PRAGMA foreign_keys = OFF")
        try:
            with self._metadata:
                _upgrade_migration_event_constraints(self._metadata)
                _upgrade_v03_constraints(self._metadata)
                _upgrade_migration_publication_schema(self._metadata)
                _ensure_current_tables(self._metadata)
                self._metadata.execute("ALTER TABLE store_identity RENAME TO legacy_store_identity")
                self._metadata.execute(_STORE_IDENTITY_DDL)
                self._metadata.execute(
                    "INSERT INTO store_identity VALUES (1, ?, ?, ?, ?)",
                    (self._mode.value, _STORE_SCHEMA_VERSION, store_id, person_binding.value),
                )
                self._metadata.execute("DROP TABLE legacy_store_identity")
                self._metadata.execute(
                    "INSERT INTO write_operations VALUES (?, 'migrate_store', ?, ?, "
                    "'committed', 1)",
                    (str(operation_id), created_at, created_at),
                )
                if new_snapshot is not None:
                    assert active is not None
                    assert manifest_sha256 is not None
                    self._metadata.execute(
                        "INSERT INTO dataset_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            str(new_snapshot),
                            _SNAPSHOT_SCHEMA_VERSION,
                            manifest_sha256,
                            str(operation_id),
                            str(active),
                            created_at,
                        ),
                    )
                    snapshot_binding = manifest["snapshot_binding"]
                    assert isinstance(snapshot_binding, dict)
                    self._metadata.execute(
                        "INSERT INTO snapshot_contract_bindings VALUES (?, ?, ?, ?, ?)",
                        (
                            str(new_snapshot),
                            snapshot_binding["snapshot_as_of"],
                            snapshot_binding["context_timezone"],
                            snapshot_binding["context_as_of_date"],
                            snapshot_binding["medication_as_of"],
                        ),
                    )
                    self._metadata.execute(
                        "INSERT INTO activity_derivation_snapshot_bindings "
                        "SELECT ?, version_id FROM activity_derivation_active WHERE singleton = 1",
                        (str(new_snapshot),),
                    )
                    for table in (
                        "manual_context_snapshot_bindings",
                        "medication_snapshot_bindings",
                        "medication_deviation_snapshot_bindings",
                        "intake_reason_category_snapshot_bindings",
                        "as_needed_intake_snapshot_bindings",
                    ):
                        columns = (
                            "version_id"
                            if table == "activity_derivation_snapshot_bindings"
                            else "revision_id"
                        )
                        self._metadata.execute(
                            f"INSERT INTO {table} (snapshot_id, {columns}) "
                            f"SELECT ?, {columns} FROM {table} WHERE snapshot_id = ?",
                            (str(new_snapshot), str(active)),
                        )
                    self._metadata.execute(
                        "INSERT INTO snapshot_activations VALUES (?, ?, ?, ?, 'migration', ?)",
                        (
                            uuid4().hex,
                            str(operation_id),
                            str(new_snapshot),
                            str(active),
                            created_at,
                        ),
                    )
                    self._metadata.execute(
                        "UPDATE active_snapshot SET snapshot_id = ? WHERE singleton = 1",
                        (str(new_snapshot),),
                    )
                audit_position = int(
                    self._metadata.execute(
                        "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
                    ).fetchone()[0]
                )
                audit_event_id = uuid4().hex
                self._metadata.execute(
                    "INSERT INTO audit_events VALUES (?, ?, ?, 'store_migrated', ?)",
                    (audit_position, audit_event_id, str(operation_id), created_at),
                )
                self._metadata.execute(
                    "INSERT INTO migration_publications "
                    "(audit_event_id, snapshot_id, source_schema_version, "
                    "target_schema_version, snapshot_source_schema_version, "
                    "snapshot_target_schema_version, backup_file) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        audit_event_id,
                        None if new_snapshot is None else str(new_snapshot),
                        source_version,
                        _STORE_SCHEMA_VERSION,
                        snapshot_source_version if snapshot_steps else None,
                        _SNAPSHOT_SCHEMA_VERSION if snapshot_steps else None,
                        backup_file,
                    ),
                )
                self._validate_store()
                _allocation_checkpoint(self._root, "migration_activated")
                _migration_fault_point(self._root, "migration.before_sqlite_commit/v1")
        except (OSError, sqlite3.Error, duckdb.Error, StoreError) as error:
            self._quarantine_migration_artifacts(operation_id, staging, snapshot)
            raise StoreError("migration_validation_failed") from error
        finally:
            self._metadata.execute("PRAGMA foreign_keys = ON")
        with suppress(OSError):
            marker.unlink(missing_ok=True)
        return self.load_identity()

    def _stage_migration_snapshot(
        self,
        *,
        source_snapshot_id: SnapshotId,
        snapshot_id: SnapshotId,
        operation_id: OperationId,
        staging: Path,
        created_at: str,
        store_id: str,
        snapshot_as_of: datetime,
    ) -> tuple[str, dict[str, object]]:
        source = self._root / _PARQUET_DIRECTORY / "snapshots" / str(source_snapshot_id)

        def link_parquet(source_name: str, target_name: str) -> str:
            if Path(source_name).suffix == ".parquet":
                os.link(source_name, target_name)
                return target_name
            return shutil.copy2(source_name, target_name)

        shutil.copytree(source, staging, copy_function=link_parquet)
        manifest = json.loads((staging / "manifest.json").read_bytes())
        if not isinstance(manifest, dict):
            raise StoreError("Snapshot-Manifest ist ungültig.")
        resolution_basis = manifest.get("resolution_basis")
        if not isinstance(resolution_basis, dict):
            raise StoreError("Snapshot-Auflösungsbasis fehlt.")
        identity_rule_version_id = str(resolution_basis.get("identity_rule_version_id"))
        mapping_rule_version_id = str(resolution_basis.get("mapping_rule_version_id"))

        reused_files: set[str] = set()
        for filename, schema in _SNAPSHOT_SCHEMAS.items():
            table = filename.removesuffix(".parquet")
            path = staging / filename
            if path.exists():
                escaped = str(path).replace("'", "''")
                source_description = tuple(
                    (str(row[0]), str(row[1]))
                    for row in self._query.execute(
                        f"DESCRIBE SELECT * FROM read_parquet('{escaped}')"
                    ).fetchall()
                )
                columns = {name for name, _ in source_description}
                if source_description == schema and filename not in _V6_DERIVATION_FILES | {
                    "derivation_lineage.parquet"
                }:
                    reused_files.add(filename)
                projection = ", ".join(
                    f'CAST("{name}" AS {kind}) AS "{name}"'
                    if name in columns
                    else f'NULL::{kind} AS "{name}"'
                    for name, kind in schema
                )
                self._query.execute(
                    f"CREATE OR REPLACE TEMP TABLE {table} AS "
                    f"SELECT {projection} FROM read_parquet('{escaped}')"
                )
            else:
                definitions = ", ".join(f'"{name}" {kind}' for name, kind in schema)
                self._query.execute(f"CREATE OR REPLACE TEMP TABLE {table} ({definitions})")

        raw_binding = manifest.get("snapshot_binding")
        source_binding = raw_binding if isinstance(raw_binding, dict) else {}
        raw_revision_ids = source_binding.get("manual_revision_ids", [])
        manual_revision_ids = (
            tuple(str(value) for value in raw_revision_ids)
            if isinstance(raw_revision_ids, list)
            else ()
        )
        context_timezone = str(source_binding.get("context_timezone", "Europe/Berlin"))
        self._refresh_v03_derivations(snapshot_id, snapshot_as_of, manual_revision_ids)
        self._refresh_derivation_lineage(snapshot_id, snapshot_as_of, manual_revision_ids)

        entries: list[dict[str, int | str | list[str]]] = []
        for filename in sorted(_SNAPSHOT_SCHEMAS):
            table = filename.removesuffix(".parquet")
            path = staging / filename
            escaped = str(path).replace("'", "''")
            if filename not in reused_files:
                path.unlink(missing_ok=True)
                self._query.execute(f"COPY (SELECT * FROM {table}) TO '{escaped}' (FORMAT PARQUET)")
            description = tuple(
                (str(row[0]), str(row[1]))
                for row in self._query.execute(
                    f"DESCRIBE SELECT * FROM read_parquet('{escaped}')"
                ).fetchall()
            )
            if description != _SNAPSHOT_SCHEMAS[filename]:
                raise StoreError(
                    f"Staging-Snapshot besitzt für {filename} ein unerwartetes Schema."
                )
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
                    "contract_ids": list(
                        _snapshot_file_contract_ids(
                            filename,
                            identity_rule_version_id,
                            mapping_rule_version_id,
                        )
                    ),
                }
            )

        counts = self._query.execute(
            "SELECT (SELECT count(DISTINCT export_id) FROM source_occurrences), "
            "(SELECT count(*) FROM source_occurrences), "
            "(SELECT count(*) FROM measurement_versions), "
            "(SELECT count(*) FROM resolved_measurements), "
            "(SELECT count(*) FROM resolved_measurements WHERE disposition LIKE 'included%'), "
            "(SELECT count(*) FROM resolved_measurements WHERE disposition LIKE 'excluded%'), "
            "(SELECT count(*) FROM open_review_cases)"
        ).fetchone()
        assert counts is not None
        audit_position = int(
            self._metadata.execute(
                "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
            ).fetchone()[0]
        )
        resolution_basis["audit_max_position"] = audit_position
        manifest.update(
            snapshot_schema_version=_SNAPSHOT_SCHEMA_VERSION,
            snapshot_id=str(snapshot_id),
            store_id=store_id,
            created_at_utc=created_at,
            created_by_operation_id=str(operation_id),
            parent_snapshot_id=str(source_snapshot_id),
            resolution_basis=resolution_basis,
            derivation_contract_ids=list(_DERIVATION_CONTRACT_IDS),
            snapshot_binding=self._snapshot_binding(
                snapshot_as_of=snapshot_as_of,
                context_timezone=context_timezone,
                manual_revision_ids=manual_revision_ids,
            ),
            files=entries,
            validation_counts=dict(
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
            ),
        )
        manifest_bytes = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        (staging / "manifest.json").write_bytes(manifest_bytes)
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        self._validate_snapshot(staging, str(snapshot_id), digest, expected_store_id=store_id)
        return digest, manifest

    def _quarantine_migration_artifacts(
        self,
        operation_id: OperationId,
        staging: Path | None,
        snapshot: Path | None,
    ) -> None:
        quarantine = self._root / "quarantine" / "migrations" / str(operation_id)
        quarantine.mkdir(parents=True, exist_ok=True)
        for source, label in ((staging, "staging"), (snapshot, "snapshot")):
            if source is not None and source.exists():
                os.replace(source, quarantine / label)
        (quarantine / "diagnostic.json").write_text(
            json.dumps({"diagnostic": "migration_not_activated"}), encoding="utf-8"
        )
        (self._root / "migration-staging" / f"{operation_id}.json").unlink(missing_ok=True)

    def _recover_migrations(self) -> None:
        backup_root = self._root / "migration-backups"
        if backup_root.exists():
            for temporary in backup_root.glob("*.tmp"):
                quarantine = self._root / "quarantine" / "migrations" / uuid4().hex
                quarantine.mkdir(parents=True)
                os.replace(temporary, quarantine / "backup")
                (quarantine / "diagnostic.json").write_text(
                    json.dumps({"diagnostic": "interrupted_migration_backup"}),
                    encoding="utf-8",
                )
        marker_root = self._root / "migration-staging"
        for marker in () if not marker_root.exists() else marker_root.glob("*.json"):
            try:
                values = json.loads(marker.read_bytes())
                raw_operation_id = values["operation_id"]
                raw_snapshot_id = values["snapshot_id"]
                if not _is_lower_hex(raw_operation_id, 32) or not _is_lower_hex(
                    raw_snapshot_id, 32
                ):
                    raise ValueError
                operation_id = OperationId(str(raw_operation_id))
                snapshot_id = SnapshotId(str(raw_snapshot_id))
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                quarantine = self._root / "quarantine" / "migrations" / uuid4().hex
                quarantine.mkdir(parents=True)
                os.replace(marker, quarantine / "marker.json")
                (quarantine / "diagnostic.json").write_text(
                    json.dumps({"diagnostic": "invalid_migration_marker"}), encoding="utf-8"
                )
                continue
            cataloged = self._metadata.execute(
                "SELECT 1 FROM dataset_snapshots WHERE snapshot_id = ?", (str(snapshot_id),)
            ).fetchone()
            if cataloged is not None:
                marker.unlink(missing_ok=True)
                continue
            self._quarantine_migration_artifacts(
                operation_id,
                marker_root / str(operation_id),
                self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id),
            )
        if marker_root.exists():
            for staging in tuple(path for path in marker_root.iterdir() if path.is_dir()):
                self._quarantine_migration_artifacts(OperationId(uuid4().hex), staging, None)
        has_snapshot_catalog = self._metadata.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dataset_snapshots'"
        ).fetchone()
        if has_snapshot_catalog is None:
            return
        cataloged = {
            str(row[0])
            for row in self._metadata.execute("SELECT snapshot_id FROM dataset_snapshots")
        }
        snapshot_root = self._root / _PARQUET_DIRECTORY / "snapshots"
        if snapshot_root.exists():
            for snapshot in tuple(path for path in snapshot_root.iterdir() if path.is_dir()):
                if snapshot.name not in cataloged:
                    self._quarantine_migration_artifacts(OperationId(uuid4().hex), None, snapshot)

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
            if store.load_identity().is_current:
                store._recover_imports()
            store._recover_migrations()
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
            if store.load_identity().is_current:
                store._recover_imports()
            store._recover_migrations()
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
                supported_legacy = existing_schema in {"1.0", "1.1", "1.2"}
                integer_schema = existing_schema.isdecimal() and int(existing_schema) > 0
                if existing_mode != mode.value or not (supported_legacy or integer_schema):
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
                or not (
                    str(identity[1]) in {"1.0", "1.1", "1.2"}
                    or (str(identity[1]).isdecimal() and int(str(identity[1])) > 0)
                )
                or (
                    str(identity[1]) == str(_STORE_SCHEMA_VERSION)
                    and (identity[2] is None or not str(identity[2]))
                )
            ):
                raise StoreConfigurationError(
                    "Datenspeicher gehört zu einem anderen Modus oder Schema."
                )
            query_path = root / _QUERY_FILE
            if initialize and not legacy_identity and not query_path.exists():
                duckdb.connect(str(query_path)).close()
            query = (
                duckdb.connect(":memory:")
                if legacy_identity and not query_path.exists()
                else duckdb.connect(str(query_path), config={"access_mode": "READ_ONLY"})
            )
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
        sleep_intervals: tuple[CanonicalSleepInterval, ...],
        workouts: tuple[CanonicalWorkout, ...],
        unknown_source_types: tuple[str, ...],
        unsupported_content: tuple[UnsupportedImportContent, ...],
        governing_export_id: str,
        resolve_sources: SourceResolver,
        resolve_workouts: WorkoutResolver,
        restore_overlay: Path | None = None,
        restore_overlay_sha256: str | None = None,
        restore_exports: tuple[
            tuple[str, datetime | None, str, tuple[CanonicalHealthRecord, ...]], ...
        ] = (),
    ) -> PublishImportResult:
        self._require_open()
        self._require_writer()
        verified_overlay: Path | None = None
        try:
            if restore_overlay is not None:
                if restore_overlay_sha256 is None:
                    raise StoreError("restore_working_copy_changed")
                verified_overlay = self._root / "staging" / f".{operation_id}.restore.sqlite3"
                verified_overlay.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(restore_overlay, verified_overlay)
                verified_overlay.chmod(0o400)
                with verified_overlay.open("rb") as working:
                    if (
                        hashlib.file_digest(working, "sha256").hexdigest()
                        != restore_overlay_sha256
                    ):
                        raise StoreError("restore_working_copy_changed")
            return self._publish_import(
                operation_id=operation_id,
                import_id=import_id,
                package_hash=package_hash,
                snapshot_id=snapshot_id,
                export_id=export_id,
                export_date=export_date,
                records=records,
                sleep_intervals=sleep_intervals,
                workouts=workouts,
                unknown_source_types=unknown_source_types,
                unsupported_content=unsupported_content,
                governing_export_id=governing_export_id,
                resolve_sources=resolve_sources,
                resolve_workouts=resolve_workouts,
                restore_overlay=verified_overlay,
                restore_overlay_sha256=restore_overlay_sha256,
                restore_exports=restore_exports,
            )
        except (OSError, sqlite3.Error, duckdb.Error) as error:
            raise StoreError("Health-Import konnte nicht veröffentlicht werden.") from error
        finally:
            if verified_overlay is not None and verified_overlay.exists():
                verified_overlay.chmod(0o600)
                verified_overlay.unlink(missing_ok=True)

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
        sleep_intervals: tuple[CanonicalSleepInterval, ...],
        workouts: tuple[CanonicalWorkout, ...],
        unknown_source_types: tuple[str, ...],
        unsupported_content: tuple[UnsupportedImportContent, ...],
        governing_export_id: str,
        resolve_sources: SourceResolver,
        resolve_workouts: WorkoutResolver,
        restore_overlay: Path | None,
        restore_overlay_sha256: str | None,
        restore_exports: tuple[
            tuple[str, datetime | None, str, tuple[CanonicalHealthRecord, ...]], ...
        ],
    ) -> PublishImportResult:
        observed_at = datetime.now().astimezone()
        active = self._metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone()
        if active is not None:
            previous_versions = (
                self._root
                / _PARQUET_DIRECTORY
                / "snapshots"
                / str(active[0])
                / "measurement_versions.parquet"
            )
            escaped_previous_versions = str(previous_versions).replace("'", "''")
            legacy_versions = {
                str(row[0]): (str(row[1]), str(row[2]))
                for row in self._query.execute(
                    "SELECT measurement_version_id, source_updated_at_utc, source_version "
                    f"FROM read_parquet('{escaped_previous_versions}')"
                ).fetchall()
            }
            records = tuple(
                replace(record, measurement_version_id=legacy_id)
                if (
                    (legacy_id := record.legacy_measurement_version_id) is not None
                    and legacy_versions.get(str(legacy_id))
                    == (
                        record.source_updated_at.astimezone(UTC).isoformat(),
                        record.provenance.source_version,
                    )
                )
                else record
                for record in records
            )
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
                    package_record_count=len(records) + len(sleep_intervals) + len(workouts),
                    record_count=0,
                    records=(*records, *sleep_intervals, *workouts),
                    logical_measurement_count=result.logical_measurement_count,
                    measurement_version_count=result.measurement_version_count,
                    source_occurrence_count=result.source_occurrence_count,
                    anomaly_count=result.anomaly_count,
                    unsupported_content=unsupported_content,
                )
            shutil.rmtree(self._root / "staging" / str(import_id), ignore_errors=True)
            return result

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
        staged_rows = [
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
                            "updated": record.source_updated_at.isoformat(),
                            "source": record.provenance.source_name,
                            "source_version": record.provenance.source_version,
                            "device": record.provenance.device,
                            "original_value": record.provenance.original_value,
                            "original_unit": record.provenance.original_unit,
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
        ]
        if staged_rows:
            self._query.executemany(
                """
            INSERT INTO staged_samples
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                staged_rows,
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
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE staged_sleep_intervals (
                measurement_version_id VARCHAR,
                identity_candidate_id VARCHAR,
                original_category VARCHAR,
                canonical_category VARCHAR,
                source_start_utc VARCHAR,
                source_end_utc VARCHAR,
                source_updated_at_utc VARCHAR,
                source_start_offset_minutes INTEGER,
                source_end_offset_minutes INTEGER,
                source_updated_at_offset_minutes INTEGER,
                source_name VARCHAR,
                source_version VARCHAR,
                device VARCHAR,
                strong_source_id_hash VARCHAR,
                is_selected BOOLEAN
            )
            """
        )
        if sleep_intervals:
            self._query.executemany(
                "INSERT INTO staged_sleep_intervals "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(interval.measurement_version_id),
                        str(interval.logical_measurement_id),
                        interval.original_category,
                        interval.canonical_category.value,
                        interval.source_start.astimezone(UTC).isoformat(),
                        interval.source_end.astimezone(UTC).isoformat(),
                        interval.source_updated_at.astimezone(UTC).isoformat(),
                        _utc_offset_minutes(interval.source_start),
                        _utc_offset_minutes(interval.source_end),
                        _utc_offset_minutes(interval.source_updated_at),
                        interval.source_name,
                        interval.source_version,
                        interval.device,
                        interval.strong_source_id_hash,
                        False,
                    )
                    for interval in sleep_intervals
                ],
            )
        if active is None:
            combined_sleep = "SELECT *, 1 AS source_priority FROM staged_sleep_intervals"
            previous_sleep_count = 0
        else:
            previous_sleep = (
                self._root
                / _PARQUET_DIRECTORY
                / "snapshots"
                / str(active[0])
                / "sleep_intervals.parquet"
            )
            escaped_previous_sleep = str(previous_sleep).replace("'", "''")
            combined_sleep = (
                f"SELECT * FROM read_parquet('{escaped_previous_sleep}') "
                "UNION ALL BY NAME SELECT *, 1 AS source_priority FROM staged_sleep_intervals"
            )
            previous_sleep_row = self._query.execute(
                f"SELECT count(*) FROM read_parquet('{escaped_previous_sleep}')"
            ).fetchone()
            assert previous_sleep_row is not None
            previous_sleep_count = int(previous_sleep_row[0])
        self._query.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE sleep_intervals AS
            SELECT * EXCLUDE(source_priority, is_selected), row_number() OVER (
                PARTITION BY identity_candidate_id
                ORDER BY source_updated_at_utc DESC, source_version DESC,
                         measurement_version_id DESC
            ) = 1 AS is_selected
            FROM ({combined_sleep})
            QUALIFY row_number() OVER (
                PARTITION BY measurement_version_id ORDER BY source_priority
            ) = 1
            """
        )
        sleep_count_row = self._query.execute(
            "SELECT count(*), count(DISTINCT identity_candidate_id) FROM sleep_intervals"
        ).fetchone()
        assert sleep_count_row is not None
        sleep_version_count, sleep_logical_count = map(int, sleep_count_row)
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE staged_workouts (
                workout_version_id VARCHAR,
                logical_workout_id VARCHAR,
                original_activity_type VARCHAR,
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
                strong_source_id_hash VARCHAR,
                reported_duration_minutes DOUBLE,
                distance_kilometers DOUBLE,
                active_energy_kilocalories DOUBLE
            )
            """
        )
        if workouts:
            self._query.executemany(
                "INSERT INTO staged_workouts VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(item.workout_version_id),
                        str(item.logical_workout_id),
                        item.original_activity_type,
                        item.source_start.astimezone(UTC).isoformat(),
                        item.source_end.astimezone(UTC).isoformat(),
                        item.source_updated_at.astimezone(UTC).isoformat(),
                        _utc_offset_minutes(item.source_start),
                        _utc_offset_minutes(item.source_end),
                        _utc_offset_minutes(item.source_updated_at),
                        item.measurement_local_day,
                        item.provenance.source_name,
                        item.provenance.source_version,
                        item.provenance.device,
                        item.provenance.strong_source_id_hash,
                        item.reported_duration_minutes,
                        item.distance_kilometers,
                        item.active_energy_kilocalories,
                    )
                    for item in workouts
                ],
            )
        previous_workouts = (
            None
            if active is None
            else (
                self._root / _PARQUET_DIRECTORY / "snapshots" / str(active[0]) / "workouts.parquet"
            )
        )
        if previous_workouts is None or not previous_workouts.exists():
            self._query.execute(
                "CREATE OR REPLACE TEMP TABLE workouts AS SELECT * FROM staged_workouts"
            )
            previous_workout_count = 0
        else:
            escaped_previous_workouts = str(previous_workouts).replace("'", "''")
            self._query.execute(
                f"""
                CREATE OR REPLACE TEMP TABLE workouts AS
                SELECT * EXCLUDE(source_priority, is_selected), false AS is_selected
                FROM (
                    SELECT *, 0 AS source_priority FROM read_parquet('{escaped_previous_workouts}')
                    UNION ALL BY NAME SELECT *, 1 AS source_priority FROM staged_workouts
                )
                QUALIFY row_number() OVER (
                    PARTITION BY workout_version_id ORDER BY source_priority
                ) = 1
                """
            )
            count_row = self._query.execute(
                f"SELECT count(*) FROM read_parquet('{escaped_previous_workouts}')"
            ).fetchone()
            assert count_row is not None
            previous_workout_count = int(count_row[0])
        if previous_workouts is None or not previous_workouts.exists():
            self._query.execute("ALTER TABLE workouts ADD COLUMN is_selected BOOLEAN DEFAULT false")
        workout_count_row = self._query.execute(
            "SELECT count(*), count(DISTINCT logical_workout_id) FROM workouts"
        ).fetchone()
        assert workout_count_row is not None
        workout_version_count, workout_logical_count = map(int, workout_count_row)
        count_row = self._query.execute(
            "SELECT count(*), count(DISTINCT identity_candidate_id) FROM combined_samples"
        ).fetchone()
        _allocation_checkpoint(self._root, "combined")
        assert count_row is not None
        version_count, logical_count = map(int, count_row)
        new_record_count = version_count - previous_count

        restored_decision_refs: tuple[tuple[str, str], ...] = ()
        restored_rule_refs: tuple[tuple[str, str], ...] = ()
        bound_snapshot_as_of = observed_at
        bound_context_timezone = "Europe/Berlin"
        if restore_overlay is None:
            audit_position = int(
                self._metadata.execute(
                    "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
                ).fetchone()[0]
            )
        else:
            with sqlite3.connect(
                f"{restore_overlay.resolve().as_uri()}?mode=ro", uri=True
            ) as backup:
                audit_position = int(
                    backup.execute(
                        "SELECT audit_max_position + 1 FROM backup_manifest WHERE singleton = 1"
                    ).fetchone()[0]
                )
                restored_decision_refs = tuple(
                    (str(row[0]), str(row[1]))
                    for row in backup.execute(
                        "SELECT decision_id, decision_kind FROM decision_refs"
                    ).fetchall()
                )
                restored_rule_refs = tuple(
                    (str(row[0]), str(row[1]))
                    for row in backup.execute(
                        "SELECT rule_version_id, rule_kind FROM rule_version_refs"
                    ).fetchall()
                )
                if "snapshot_origin" in {
                    str(row[0])
                    for row in backup.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }:
                    origin = backup.execute(
                        "SELECT snapshot_as_of, context_timezone FROM snapshot_origin"
                    ).fetchone()
                    if origin is None:
                        raise StoreError("backup_integrity_conflict")
                    bound_snapshot_as_of = datetime.fromisoformat(str(origin[0]))
                    bound_context_timezone = str(origin[1])
        manifest_sha256, resolution = self._stage_snapshot(
            staging,
            operation_id=operation_id,
            snapshot_id=snapshot_id,
            parent_snapshot_id=None if active is None else SnapshotId(str(active[0])),
            export_id=export_id,
            export_date=export_date,
            governing_export_id=governing_export_id,
            records=records,
            unknown_source_types=unknown_source_types,
            audit_position=audit_position,
            resolve_sources=resolve_sources,
            resolve_workouts=resolve_workouts,
            restore_exports=restore_exports,
            restored_decision_refs=restored_decision_refs,
            restored_rule_refs=restored_rule_refs,
            snapshot_as_of=bound_snapshot_as_of,
            context_timezone=bound_context_timezone,
            restore_overlay=restore_overlay,
        )
        _allocation_checkpoint(self._root, "staged")
        _fsync_snapshot(staging)
        _publication_fault_point(self._root, "import.before_snapshot_move/v1")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(snapshot)
        _fsync_directory(snapshot.parent)
        _publication_fault_point(self._root, "import.after_snapshot_move/v1")
        completed_at = datetime.now(UTC).isoformat()
        audit_event_id = uuid4().hex
        with self._metadata:
            if restore_overlay is not None:
                self._activate_restore_overlay(
                    restore_overlay, expected_sha256=restore_overlay_sha256
                )
            self._metadata.execute(
                "INSERT INTO write_operations VALUES (?, 'import_health_export', ?, ?, "
                "'committed', 1)",
                (str(operation_id), observed_at.astimezone(UTC).isoformat(), completed_at),
            )
            exports_to_record = restore_exports or (
                (export_id, export_date, package_hash, records),
            )
            self._metadata.executemany(
                "INSERT INTO exports VALUES (?, ?, ?, ?)",
                (
                    (
                        item_export_id,
                        item_package_hash,
                        (
                            None
                            if item_export_date is None
                            else item_export_date.astimezone(UTC).isoformat()
                        ),
                        "unordered" if item_export_date is None else "ordered",
                    )
                    for item_export_id, item_export_date, item_package_hash, _ in exports_to_record
                ),
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
            self._insert_snapshot_contract_binding(snapshot_id)
            self._metadata.execute(
                "INSERT INTO activity_derivation_snapshot_bindings VALUES (?, "
                "(SELECT version_id FROM activity_derivation_active WHERE singleton = 1))",
                (str(snapshot_id),),
            )
            if active is not None:
                self._metadata.execute(
                    "INSERT INTO manual_context_snapshot_bindings "
                    "SELECT ?, revision_id FROM manual_context_snapshot_bindings "
                    "WHERE snapshot_id = ?",
                    (str(snapshot_id), str(active[0])),
                )
                self._metadata.execute(
                    "INSERT INTO medication_snapshot_bindings "
                    "SELECT ?, revision_id FROM medication_snapshot_bindings WHERE snapshot_id = ?",
                    (str(snapshot_id), str(active[0])),
                )
                self._metadata.execute(
                    "INSERT INTO medication_deviation_snapshot_bindings "
                    "SELECT ?, revision_id FROM medication_deviation_snapshot_bindings "
                    "WHERE snapshot_id = ?",
                    (str(snapshot_id), str(active[0])),
                )
                self._metadata.execute(
                    "INSERT INTO intake_reason_category_snapshot_bindings "
                    "SELECT ?, revision_id FROM intake_reason_category_snapshot_bindings "
                    "WHERE snapshot_id = ?",
                    (str(snapshot_id), str(active[0])),
                )
                self._metadata.execute(
                    "INSERT INTO as_needed_intake_snapshot_bindings "
                    "SELECT ?, revision_id FROM as_needed_intake_snapshot_bindings "
                    "WHERE snapshot_id = ?",
                    (str(snapshot_id), str(active[0])),
                )
            elif restore_overlay is not None:
                self._bind_restored_snapshot(snapshot_id, restore_overlay)
            all_intervals: tuple[
                CanonicalHealthRecord | CanonicalSleepInterval | CanonicalWorkout, ...
            ] = (
                *records,
                *sleep_intervals,
                *workouts,
            )
            self._record_import(
                operation_id=operation_id,
                import_id=import_id,
                package_hash=package_hash,
                snapshot_id=snapshot_id,
                status="committed",
                package_record_count=len(all_intervals),
                record_count=(
                    new_record_count
                    + sleep_version_count
                    - previous_sleep_count
                    + workout_version_count
                    - previous_workout_count
                ),
                records=all_intervals,
                logical_measurement_count=logical_count
                + sleep_logical_count
                + workout_logical_count,
                measurement_version_count=version_count
                + sleep_version_count
                + workout_version_count,
                source_occurrence_count=self._snapshot_occurrence_count(snapshot_id),
                anomaly_count=resolution.anomaly_count,
                unsupported_content=unsupported_content,
            )
            self._metadata.executemany(
                "INSERT OR IGNORE INTO source_type_catalog VALUES (?, ?)",
                (
                    (request.source_type, str(request.review_case_id))
                    for request in resolution.source_type_requests
                ),
            )
            self._metadata.execute(
                "INSERT INTO review_cycles VALUES (?, ?, 'import', ?, ?)",
                (
                    str(snapshot_id),
                    str(snapshot_id),
                    resolution.cycle_status,
                    resolution.cycle_open_case_count,
                ),
            )
            self._metadata.executemany(
                "INSERT INTO review_cycle_cases VALUES (?, ?)",
                ((str(snapshot_id), str(case_id)) for case_id in resolution.new_review_case_ids),
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
            if governing_export_id == export_id:
                self._metadata.executemany(
                    "UPDATE source_absence_suppressions SET active = 0 "
                    "WHERE logical_measurement_id = ?",
                    {(str(record.logical_measurement_id),) for record in records},
                )
            self._bind_person()
            if restore_overlay is not None:
                self._metadata.execute(
                    "UPDATE metadata_restores SET activated_at_utc = ? "
                    "WHERE activated_at_utc IS NULL",
                    (completed_at,),
                )
            _allocation_checkpoint(self._root, "activated")
            _publication_fault_point(self._root, "import.before_sqlite_commit/v1")
        return PublishImportResult(
            status="committed",
            snapshot_id=snapshot_id,
            record_count=new_record_count + sleep_version_count - previous_sleep_count,
            logical_measurement_count=logical_count + sleep_logical_count,
            measurement_version_count=version_count + sleep_version_count,
            source_occurrence_count=self._snapshot_occurrence_count(snapshot_id),
            anomaly_count=resolution.anomaly_count,
        )

    def _activate_restore_overlay(self, path: Path, *, expected_sha256: str | None) -> None:
        if expected_sha256 is None:
            raise StoreError("restore_working_copy_changed")
        with path.open("rb") as working:
            if hashlib.file_digest(working, "sha256").hexdigest() != expected_sha256:
                raise StoreError("restore_working_copy_changed")
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as backup:
            backup_tables = {
                str(row[0])
                for row in backup.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }

            def copy(
                table: str, *, ignore_existing: bool = False, order_by: str | None = None
            ) -> None:
                columns = len(backup.execute(f'PRAGMA table_info("{table}")').fetchall())
                ordering = "" if order_by is None else f' ORDER BY "{order_by}"'
                rows = backup.execute(f'SELECT * FROM "{table}"{ordering}').fetchall()
                if rows:
                    self._metadata.executemany(
                        f'INSERT {"OR IGNORE " if ignore_existing else ""}INTO "{table}" '
                        f"VALUES ({', '.join('?' for _ in range(columns))})",
                        rows,
                    )

            for table in (
                "write_operations",
                "decision_refs",
                "source_type_catalog",
                "data_review_batch_actions",
            ):
                copy(table)
            copy("rule_version_refs", ignore_existing=True)
            copy("plausibility_rule_versions", ignore_existing=True)
            if "activity_derivation_versions" in backup_tables:
                copy("activity_derivation_versions", ignore_existing=True)
            copy("audit_events", order_by="audit_position")
            for table in (
                "data_review_decisions",
                "data_review_batch_members",
                "metadata_tombstones",
                "source_absence_suppressions",
            ):
                copy(table)
            for table in _RESTORABLE_MANUAL_METADATA_TABLES:
                if (
                    table in backup_tables
                    and table != "activity_derivation_versions"
                    and not table.endswith("_publications")
                ):
                    copy(table)
            if "snapshot_origin" in backup_tables:
                version = backup.execute(
                    "SELECT activity_derivation_version_id FROM snapshot_origin"
                ).fetchone()
                if version is None:
                    raise StoreError("backup_integrity_conflict")
                self._metadata.execute(
                    "UPDATE activity_derivation_active SET version_id = ? WHERE singleton = 1",
                    (str(version[0]),),
                )
            payload_ids = {
                str(row[0])
                for table in ("data_review_decisions", "metadata_tombstones")
                for row in backup.execute(f"SELECT audit_event_id FROM {table}").fetchall()
            }
            self._metadata.executemany(
                "INSERT INTO restored_publications VALUES (?)",
                (
                    (str(row[0]),)
                    for row in backup.execute(
                        "SELECT audit_event_id FROM audit_events ORDER BY audit_position"
                    ).fetchall()
                    if str(row[0]) not in payload_ids
                ),
            )
        self._metadata.execute(
            "UPDATE store_identity SET person_binding = 'bound' WHERE singleton = 1"
        )

    def _bind_restored_snapshot(self, snapshot_id: SnapshotId, path: Path) -> None:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as backup:
            tables = {
                str(row[0])
                for row in backup.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if "snapshot_origin" not in tables:
                return
            origin = backup.execute("SELECT source_snapshot_id FROM snapshot_origin").fetchone()
            manifest = backup.execute(
                "SELECT backup_id FROM backup_manifest WHERE singleton = 1"
            ).fetchone()
            if origin is None or manifest is None:
                raise StoreError("backup_integrity_conflict")
            binding_tables = {
                "context": "manual_context_snapshot_bindings",
                "medication_regime": "medication_snapshot_bindings",
                "medication_deviation": "medication_deviation_snapshot_bindings",
                "intake_reason_category": "intake_reason_category_snapshot_bindings",
                "as_needed_intake": "as_needed_intake_snapshot_bindings",
            }
            for kind, revision_id in backup.execute(
                "SELECT revision_kind, revision_id FROM manual_revision_bindings"
            ):
                table = binding_tables.get(str(kind))
                if table is None:
                    raise StoreError("backup_integrity_conflict")
                self._metadata.execute(
                    f'INSERT INTO "{table}" VALUES (?, ?)',
                    (str(snapshot_id), str(revision_id)),
                )
            self._metadata.execute(
                "INSERT INTO snapshot_restore_origins VALUES (?, ?, ?)",
                (str(snapshot_id), str(manifest[0]), str(origin[0])),
            )

    def _effective_manual_revision_ids(self) -> tuple[str, ...]:
        rows = self._metadata.execute(
            """
            SELECT revision_id FROM manual_context_revisions current
            WHERE state = 'active' AND NOT EXISTS (
                SELECT 1 FROM manual_context_revisions next
                WHERE next.previous_revision_id = current.revision_id
            )
            UNION ALL
            SELECT revision_id FROM medication_regime_revisions current
            WHERE NOT EXISTS (
                SELECT 1 FROM medication_regime_revisions next
                WHERE next.previous_revision_id = current.revision_id
            )
            UNION ALL
            SELECT revision_id FROM medication_deviation_revisions current
            WHERE state = 'active' AND NOT EXISTS (
                SELECT 1 FROM medication_deviation_revisions next
                WHERE next.previous_revision_id = current.revision_id
            )
            UNION ALL
            SELECT revision_id FROM intake_reason_category_revisions current
            WHERE state = 'active' AND NOT EXISTS (
                SELECT 1 FROM intake_reason_category_revisions next
                WHERE next.previous_revision_id = current.revision_id
            )
            UNION ALL
            SELECT revision_id FROM as_needed_intake_revisions current
            WHERE state = 'active' AND NOT EXISTS (
                SELECT 1 FROM as_needed_intake_revisions next
                WHERE next.previous_revision_id = current.revision_id
            )
            """
        ).fetchall()
        return tuple(sorted(str(row[0]) for row in rows))

    def _bound_manual_revision_ids(self, snapshot_id: SnapshotId) -> tuple[str, ...]:
        rows = self._metadata.execute(
            """
            SELECT revision_id FROM manual_context_snapshot_bindings WHERE snapshot_id = ?
            UNION ALL
            SELECT revision_id FROM medication_snapshot_bindings WHERE snapshot_id = ?
            UNION ALL
            SELECT revision_id FROM medication_deviation_snapshot_bindings WHERE snapshot_id = ?
            UNION ALL
            SELECT revision_id FROM intake_reason_category_snapshot_bindings WHERE snapshot_id = ?
            UNION ALL
            SELECT revision_id FROM as_needed_intake_snapshot_bindings WHERE snapshot_id = ?
            """,
            (str(snapshot_id),) * 5,
        ).fetchall()
        return tuple(sorted(str(row[0]) for row in rows))

    def _snapshot_binding(
        self,
        *,
        snapshot_as_of: datetime,
        context_timezone: str,
        manual_revision_ids: tuple[str, ...],
    ) -> dict[str, str | list[str]]:
        source_version_ids = tuple(
            sorted(
                str(row[0])
                for row in self._query.execute(
                    """
                    SELECT selected_measurement_version_id FROM resolved_measurements
                    UNION
                    SELECT measurement_version_id FROM sleep_intervals WHERE is_selected
                    UNION
                    SELECT selected_workout_version_id FROM resolved_workouts
                    """
                ).fetchall()
            )
        )
        return {
            "snapshot_as_of": snapshot_as_of.isoformat(),
            "context_timezone": context_timezone,
            "context_as_of_date": snapshot_as_of.astimezone(ZoneInfo(context_timezone))
            .date()
            .isoformat(),
            "medication_as_of": snapshot_as_of.isoformat(),
            "source_version_ids": list(source_version_ids),
            "manual_revision_ids": list(manual_revision_ids),
        }

    def _insert_snapshot_contract_binding(self, snapshot_id: SnapshotId) -> None:
        binding = self._load_snapshot_binding(snapshot_id)
        if binding is None:
            raise StoreError("Snapshot-Vertragsbindung fehlt.")
        self._metadata.execute(
            "INSERT INTO snapshot_contract_bindings VALUES (?, ?, ?, ?, ?)",
            (
                str(snapshot_id),
                str(binding["snapshot_as_of"]),
                str(binding["context_timezone"]),
                str(binding["context_as_of_date"]),
                str(binding["medication_as_of"]),
            ),
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
        unknown_source_types: tuple[str, ...],
        audit_position: int,
        resolve_sources: SourceResolver,
        resolve_workouts: WorkoutResolver,
        restore_exports: tuple[
            tuple[str, datetime | None, str, tuple[CanonicalHealthRecord, ...]], ...
        ],
        restored_decision_refs: tuple[tuple[str, str], ...],
        restored_rule_refs: tuple[tuple[str, str], ...],
        snapshot_as_of: datetime,
        context_timezone: str,
        restore_overlay: Path | None,
    ) -> tuple[str, SourceResolution]:
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
        export_records = (
            tuple((item[0], item[3]) for item in restore_exports)
            if restore_exports
            else ((export_id, records),)
        )
        occurrence_rows = [
            (
                hashlib.sha256(f"{item_export_id}:{ordinal}".encode()).hexdigest(),
                item_export_id,
                ordinal,
                str(record.logical_measurement_id),
                str(record.measurement_version_id),
                _MAPPING_RULE_VERSION,
                hashlib.sha256(
                    f"{item_export_id}:{ordinal}:{record.measurement_version_id}".encode()
                ).hexdigest(),
            )
            for item_export_id, item_records in export_records
            for ordinal, record in enumerate(item_records, start=1)
        ]
        if occurrence_rows:
            self._query.executemany(
                "INSERT INTO staged_occurrences VALUES (?, ?, ?, ?, ?, ?, ?)",
                occurrence_rows,
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
        export_rows.extend(
            (
                item_export_id,
                (
                    None
                    if item_export_date is None
                    else item_export_date.astimezone(UTC).isoformat()
                ),
            )
            for item_export_id, item_export_date, _, _ in (
                restore_exports or ((export_id, export_date, "", records),)
            )
        )
        self._query.executemany("INSERT INTO export_order VALUES (?, ?)", export_rows)
        previous_measurements: tuple[ResolvedMeasurement, ...] = ()
        previous_review_cases: tuple[OpenDataReviewCase, ...] = ()
        previous_workouts: tuple[ResolvedWorkout, ...] = ()
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
                            "rule_definition",
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
            previous_workout_path = previous_directory / "resolved_workouts.parquet"
            if previous_workout_path.exists():
                escaped_previous_workouts = str(previous_workout_path).replace("'", "''")
                previous_workouts = tuple(
                    ResolvedWorkout(*row)
                    for row in self._query.execute(
                        f"SELECT * FROM read_parquet('{escaped_previous_workouts}')"
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
                source_updated_at_utc=str(row[7]),
                source_version=str(row[8]),
                source_name=str(row[9]),
                device=str(row[10]),
                strong_source_id_hash=None if row[11] is None else str(row[11]),
                measurement_local_date=row[12],
            )
            for row in self._query.execute(
                "SELECT measurement_version_id, identity_candidate_id, canonical_type, "
                "canonical_unit, canonical_value, source_start_utc, source_end_utc, "
                "source_updated_at_utc, source_version, source_name, device, "
                "strong_source_id_hash, measurement_local_date "
                "FROM measurement_versions"
            ).fetchall()
        )
        export_facts = tuple(
            ExportFact(
                str(row[0]),
                None if row[1] is None else datetime.fromisoformat(str(row[1])),
                tuple(str(item) for item in row[2]),
            )
            for row in self._query.execute(
                """
                SELECT export_order.export_id, export_date_utc,
                       list(DISTINCT canonical_type ORDER BY canonical_type)
                FROM export_order
                LEFT JOIN source_occurrences USING (export_id)
                LEFT JOIN measurement_versions USING (measurement_version_id)
                GROUP BY export_order.export_id, export_date_utc
                """
            ).fetchall()
        )
        resolution = resolve_sources(
            occurrences=occurrence_facts,
            versions=version_facts,
            exports=export_facts,
            previous_measurements=previous_measurements,
            previous_review_cases=previous_review_cases,
            governing_export_id=governing_export_id,
            imported_measurement_version_ids=tuple(
                str(record.measurement_version_id) for record in records
            ),
            unknown_source_types=unknown_source_types,
            suppressed_deletion_ids=frozenset(
                str(row[0])
                for row in self._metadata.execute(
                    "SELECT logical_measurement_id FROM source_absence_suppressions "
                    "WHERE active = 1"
                ).fetchall()
            ),
            plausibility_rules=self.load_plausibility_rule_versions(),
        )
        workout_resolution = resolve_workouts(
            versions=tuple(
                WorkoutVersionFact(
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    str(row[4]),
                    str(row[5]),
                    None if row[6] is None else float(row[6]),
                    None if row[7] is None else float(row[7]),
                    None if row[8] is None else float(row[8]),
                )
                for row in self._query.execute(
                    "SELECT workout_version_id, logical_workout_id, source_start_utc, "
                    "source_end_utc, source_updated_at_utc, source_version, "
                    "reported_duration_minutes, distance_kilometers, "
                    "active_energy_kilocalories FROM workouts"
                ).fetchall()
            ),
            previous_workouts=previous_workouts,
        )
        prior_case_ids = {item.review_case_id for item in previous_review_cases}
        new_workout_case_ids = tuple(
            ReviewCaseId(item.review_case_id)
            for item in workout_resolution.review_cases
            if item.review_case_id not in prior_case_ids
        )
        all_cases = {item.review_case_id: item for item in resolution.review_cases}
        all_cases.update((item.review_case_id, item) for item in workout_resolution.review_cases)
        resolution = replace(
            resolution,
            review_cases=tuple(all_cases[key] for key in sorted(all_cases)),
            new_review_case_ids=(*resolution.new_review_case_ids, *new_workout_case_ids),
            cycle_status="open"
            if resolution.new_review_case_ids or new_workout_case_ids
            else "closed",
            cycle_open_case_count=(len(resolution.new_review_case_ids) + len(new_workout_case_ids)),
            anomaly_count=resolution.anomaly_count
            + sum(item.kind == "workout_plausibility" for item in workout_resolution.review_cases),
        )
        self._query.execute(
            "CREATE OR REPLACE TEMP TABLE resolved_measurements ("
            "logical_measurement_id VARCHAR, selected_measurement_version_id VARCHAR, "
            "disposition VARCHAR, effective_value DOUBLE, canonical_unit VARCHAR, "
            "effective_value_source VARCHAR, effective_decision_id VARCHAR, "
            "correction_decision_id VARCHAR, source_deletion_decision_id VARCHAR, "
            "conflict_resolution_decision_id VARCHAR)"
        )
        resolved_rows = [
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
        ]
        if resolved_rows:
            self._query.executemany(
                "INSERT INTO resolved_measurements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                resolved_rows,
            )
        self._query.execute(
            "CREATE OR REPLACE TEMP TABLE resolved_workouts ("
            "logical_workout_id VARCHAR, selected_workout_version_id VARCHAR, disposition VARCHAR, "
            "effective_duration_minutes DOUBLE, distance_kilometers DOUBLE, "
            "active_energy_kilocalories DOUBLE, effective_decision_id VARCHAR)"
        )
        if workout_resolution.workouts:
            self._query.executemany(
                "INSERT INTO resolved_workouts VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item.logical_workout_id,
                        item.selected_workout_version_id,
                        item.disposition,
                        item.effective_duration_minutes,
                        item.distance_kilometers,
                        item.active_energy_kilocalories,
                        item.effective_decision_id,
                    )
                    for item in workout_resolution.workouts
                ],
            )
        self._query.execute(
            "CREATE OR REPLACE TEMP TABLE workout_review_links "
            "(review_case_id VARCHAR, workout_version_id VARCHAR)"
        )
        if workout_resolution.review_links:
            self._query.executemany(
                "INSERT INTO workout_review_links VALUES (?, ?)", workout_resolution.review_links
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
        restored_metadata: sqlite3.Connection | None = None
        if restore_overlay is not None:
            restored_metadata = sqlite3.connect(
                f"{restore_overlay.resolve().as_uri()}?mode=ro", uri=True
            )
            restored_tables = {
                str(row[0])
                for row in restored_metadata.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            bound_manual_revision_ids = (
                tuple(
                    str(row[0])
                    for row in restored_metadata.execute(
                        "SELECT revision_id FROM manual_revision_bindings ORDER BY revision_id"
                    )
                )
                if "manual_revision_bindings" in restored_tables
                else ()
            )
        else:
            bound_manual_revision_ids = (
                ()
                if parent_snapshot_id is None
                else self._bound_manual_revision_ids(parent_snapshot_id)
            )
        try:
            self._refresh_v03_derivations(
                snapshot_id,
                snapshot_as_of,
                bound_manual_revision_ids,
                restored_metadata,
            )
        finally:
            if restored_metadata is not None:
                restored_metadata.close()
        self._refresh_derivation_lineage(snapshot_id, snapshot_as_of, bound_manual_revision_ids)

        entries: list[dict[str, int | str | list[str]]] = []
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
                raise StoreError(
                    f"Staging-Snapshot besitzt für {filename} ein unerwartetes Schema."
                )
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
                    "contract_ids": list(_SNAPSHOT_FILE_CONTRACTS[filename]),
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
            "derivation_contract_ids": list(_DERIVATION_CONTRACT_IDS),
            "snapshot_binding": self._snapshot_binding(
                snapshot_as_of=snapshot_as_of,
                context_timezone=context_timezone,
                manual_revision_ids=bound_manual_revision_ids,
            ),
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
        self._validate_snapshot(
            directory,
            str(snapshot_id),
            manifest_sha256,
            additional_decision_refs=restored_decision_refs,
            additional_rule_refs=restored_rule_refs,
            additional_manual_revision_ids=bound_manual_revision_ids,
        )
        return manifest_sha256, resolution

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
            migration_count = (
                "+ count(migration_publications.audit_event_id)"
                if "migration_publications" in tables
                else ""
            )
            migration_join = (
                "LEFT JOIN migration_publications USING (audit_event_id)"
                if "migration_publications" in tables
                else ""
            )
            restored_count = (
                "+ count(restored_publications.audit_event_id)"
                if "restored_publications" in tables
                else ""
            )
            restored_join = (
                "LEFT JOIN restored_publications USING (audit_event_id)"
                if "restored_publications" in tables
                else ""
            )
            manual_count = (
                "+ count(manual_context_publications.audit_event_id)"
                if "manual_context_publications" in tables
                else ""
            )
            manual_join = (
                "LEFT JOIN manual_context_publications USING (audit_event_id)"
                if "manual_context_publications" in tables
                else ""
            )
            medication_count = (
                "+ count(medication_publications.audit_event_id)"
                if "medication_publications" in tables
                else ""
            )
            medication_join = (
                "LEFT JOIN medication_publications USING (audit_event_id)"
                if "medication_publications" in tables
                else ""
            )
            deviation_count = (
                "+ count(medication_deviation_publications.audit_event_id)"
                if "medication_deviation_publications" in tables
                else ""
            )
            deviation_join = (
                "LEFT JOIN medication_deviation_publications USING (audit_event_id)"
                if "medication_deviation_publications" in tables
                else ""
            )
            as_needed_count = (
                "+ count(as_needed_intake_publications.audit_event_id)"
                if "as_needed_intake_publications" in tables
                else ""
            )
            as_needed_join = (
                "LEFT JOIN as_needed_intake_publications USING (audit_event_id)"
                if "as_needed_intake_publications" in tables
                else ""
            )
            reason_category_count = (
                "+ count(intake_reason_category_publications.audit_event_id)"
                if "intake_reason_category_publications" in tables
                else ""
            )
            reason_category_join = (
                "LEFT JOIN intake_reason_category_publications USING (audit_event_id)"
                if "intake_reason_category_publications" in tables
                else ""
            )
            audit = self._metadata.execute(
                f"""
                SELECT count(*), COALESCE(MIN(audit_position), 1),
                       COALESCE(MAX(audit_position), 0),
                       count(import_publications.audit_event_id)
                       + count(data_review_decisions.audit_event_id)
                       + count(metadata_tombstones.audit_event_id)
                       {migration_count}
                       {restored_count}
                       {manual_count}
                       {medication_count}
                       {deviation_count}
                       {as_needed_count}
                       {reason_category_count}
                FROM audit_events
                LEFT JOIN import_publications USING (audit_event_id)
                LEFT JOIN data_review_decisions USING (audit_event_id)
                LEFT JOIN metadata_tombstones USING (audit_event_id)
                {migration_join}
                {restored_join}
                {manual_join}
                {medication_join}
                {deviation_join}
                {as_needed_join}
                {reason_category_join}
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
            if schema_version >= 6 and manifest["snapshot_binding"]["manual_revision_ids"] != list(
                self._bound_manual_revision_ids(SnapshotId(snapshot_id))
            ):
                raise StoreError("Snapshot-Revisionsbindung ist nicht geschlossen.")
            if schema_version >= 6:
                binding = manifest["snapshot_binding"]
                catalog_binding = self._metadata.execute(
                    "SELECT snapshot_as_of, context_timezone, context_as_of_date, "
                    "medication_as_of FROM snapshot_contract_bindings WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
                if catalog_binding != (
                    binding["snapshot_as_of"],
                    binding["context_timezone"],
                    binding["context_as_of_date"],
                    binding["medication_as_of"],
                ):
                    raise StoreError("Snapshot-Katalogbindung ist nicht geschlossen.")

    def _validate_snapshot(
        self,
        directory: Path,
        snapshot_id: str,
        manifest_sha256: str,
        *,
        expected_store_id: str | None = None,
        additional_decision_refs: tuple[tuple[str, str], ...] = (),
        additional_rule_refs: tuple[tuple[str, str], ...] = (),
        additional_manual_revision_ids: tuple[str, ...] = (),
    ) -> None:
        try:
            manifest_bytes = (directory / "manifest.json").read_bytes()
            manifest = json.loads(manifest_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise StoreError("Snapshot-Manifest ist nicht lesbar.") from error
        if (
            not isinstance(manifest, dict)
            or type(manifest.get("snapshot_schema_version")) is not int
        ):
            raise StoreError("Snapshot-Manifest ist nicht kanonisch oder gültig.")
        snapshot_schema_version = int(manifest["snapshot_schema_version"])
        store_row = self._metadata.execute(
            "SELECT store_id FROM store_identity WHERE singleton = 1"
        ).fetchone()
        resolution_basis = manifest.get("resolution_basis") if isinstance(manifest, dict) else None
        validation_counts = (
            manifest.get("validation_counts") if isinstance(manifest, dict) else None
        )
        snapshot_binding = manifest.get("snapshot_binding") if isinstance(manifest, dict) else None
        binding_valid = False
        if isinstance(snapshot_binding, dict):
            try:
                snapshot_as_of = datetime.fromisoformat(str(snapshot_binding["snapshot_as_of"]))
                medication_as_of = datetime.fromisoformat(str(snapshot_binding["medication_as_of"]))
                context_timezone = str(snapshot_binding["context_timezone"])
                context_as_of_date = date.fromisoformat(str(snapshot_binding["context_as_of_date"]))
                source_version_ids = snapshot_binding["source_version_ids"]
                manual_revision_ids = snapshot_binding["manual_revision_ids"]
                binding_valid = (
                    set(snapshot_binding)
                    == {
                        "snapshot_as_of",
                        "context_timezone",
                        "context_as_of_date",
                        "medication_as_of",
                        "source_version_ids",
                        "manual_revision_ids",
                    }
                    and snapshot_as_of.tzinfo is not None
                    and medication_as_of == snapshot_as_of
                    and context_as_of_date
                    == snapshot_as_of.astimezone(ZoneInfo(context_timezone)).date()
                    and isinstance(source_version_ids, list)
                    and source_version_ids == sorted(set(source_version_ids))
                    and all(_is_lower_hex(value, 64) for value in source_version_ids)
                    and isinstance(manual_revision_ids, list)
                    and manual_revision_ids == sorted(set(manual_revision_ids))
                    and all(_is_lower_hex(value, 32) for value in manual_revision_ids)
                )
            except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
                binding_valid = False
        if (
            not isinstance(manifest, dict)
            or store_row is None
            or manifest.get("store_id")
            != (store_row[0] if expected_store_id is None else expected_store_id)
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
                *(("derivation_contract_ids",) if snapshot_schema_version >= 5 else ()),
                *(("snapshot_binding",) if snapshot_schema_version >= 6 else ()),
            }
            or snapshot_schema_version not in range(1, _SNAPSHOT_SCHEMA_VERSION + 1)
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
            or resolution_basis["identity_rule_version_id"] not in _SUPPORTED_IDENTITY_RULE_VERSIONS
            or resolution_basis["mapping_rule_version_id"] not in _SUPPORTED_MAPPING_RULE_VERSIONS
            or (
                manifest["snapshot_schema_version"] == 5
                and manifest.get("derivation_contract_ids")
                != ["resolved-measurement/v1", "resolved-workout/v1"]
            )
            or (
                manifest["snapshot_schema_version"] >= 6
                and manifest.get("derivation_contract_ids") != list(_DERIVATION_CONTRACT_IDS)
            )
            or (manifest["snapshot_schema_version"] >= 6 and not binding_valid)
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
        schemas = {
            1: _LEGACY_SNAPSHOT_SCHEMAS,
            2: _V2_SNAPSHOT_SCHEMAS,
            3: _V3_SNAPSHOT_SCHEMAS,
            4: _V4_SNAPSHOT_SCHEMAS,
            5: _V5_SNAPSHOT_SCHEMAS,
            6: _SNAPSHOT_SCHEMAS,
            _SNAPSHOT_SCHEMA_VERSION: _SNAPSHOT_SCHEMAS,
        }[manifest["snapshot_schema_version"]]
        files = manifest["files"]
        if (
            not isinstance(files, list)
            or any(not isinstance(entry, dict) for entry in files)
            or tuple(entry.get("name") for entry in files) != tuple(sorted(schemas))
        ):
            raise StoreError("Snapshot enthält nicht genau vier geschlossene Dateien.")
        for entry in files:
            expected_entry_fields = {
                "name",
                "sha256",
                "allocated_bytes",
                "row_count",
                "parquet_schema_fingerprint",
            }
            if manifest["snapshot_schema_version"] >= 6:
                expected_entry_fields.add("contract_ids")
            if not isinstance(entry, dict) or set(entry) != expected_entry_fields:
                raise StoreError("Snapshot-Dateieintrag ist ungültig.")
            filename = str(entry["name"])
            if (
                not _is_lower_hex(entry["sha256"], 64)
                or not _is_lower_hex(entry["parquet_schema_fingerprint"], 64)
                or type(entry["allocated_bytes"]) is not int
                or entry["allocated_bytes"] < 0
                or type(entry["row_count"]) is not int
                or entry["row_count"] < 0
                or (
                    manifest["snapshot_schema_version"] >= 6
                    and entry["contract_ids"]
                    != list(
                        _snapshot_file_contract_ids(
                            filename,
                            str(resolution_basis["identity_rule_version_id"]),
                            str(resolution_basis["mapping_rule_version_id"]),
                        )
                    )
                )
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
                description != schemas[filename]
                or row is None
                or int(row[0]) != entry["row_count"]
                or allocated_bytes != entry["allocated_bytes"]
                or sha256 != entry["sha256"]
                or schema_fingerprint != entry["parquet_schema_fingerprint"]
            ):
                raise StoreError("Snapshot-Dateivalidierung fehlgeschlagen.")
        if {path.name for path in directory.iterdir()} != {
            "manifest.json",
            *schemas,
        }:
            raise StoreError("Snapshot enthält unerlaubte Artefakte.")
        paths = {
            name.removesuffix(".parquet"): str(directory / name).replace("'", "''")
            for name in _SNAPSHOT_SCHEMAS
        }
        if manifest["snapshot_schema_version"] >= 6:
            for filename in _V6_DERIVATION_FILES:
                contract = _SNAPSHOT_FILE_CONTRACTS[filename][0]
                table = paths[filename.removesuffix(".parquet")]
                invalid_derivation = self._query.execute(
                    f"SELECT count(*) FROM read_parquet('{table}') "
                    "WHERE derivation_contract_id != ? OR snapshot_id != ? "
                    "OR try_cast(derived_at_utc AS TIMESTAMPTZ) IS NULL",
                    (contract, snapshot_id),
                ).fetchone()
                if invalid_derivation is None or int(invalid_derivation[0]) != 0:
                    raise StoreError("Snapshot-Ableitungsbindung ist ungültig.")
            expected_source_version_ids = sorted(
                str(row[0])
                for row in self._query.execute(
                    f"""
                    SELECT selected_measurement_version_id
                    FROM read_parquet('{paths["resolved_measurements"]}')
                    UNION
                    SELECT measurement_version_id
                    FROM read_parquet('{paths["sleep_intervals"]}') WHERE is_selected
                    UNION
                    SELECT selected_workout_version_id
                    FROM read_parquet('{paths["resolved_workouts"]}')
                    """
                ).fetchall()
            )
            assert isinstance(snapshot_binding, dict)
            if snapshot_binding["source_version_ids"] != expected_source_version_ids:
                raise StoreError("Snapshot-Quellversionsbindung ist nicht geschlossen.")
            manual_revision_ids = cast(list[str], snapshot_binding["manual_revision_ids"])
            if manual_revision_ids:
                placeholders = ",".join("?" for _ in manual_revision_ids)
                known_manual_revision_ids = {
                    str(row[0])
                    for row in self._metadata.execute(
                        "SELECT revision_id FROM ("
                        "SELECT revision_id FROM manual_context_revisions UNION ALL "
                        "SELECT revision_id FROM medication_regime_revisions UNION ALL "
                        "SELECT revision_id FROM medication_deviation_revisions UNION ALL "
                        "SELECT revision_id FROM intake_reason_category_revisions UNION ALL "
                        "SELECT revision_id FROM as_needed_intake_revisions"
                        f") WHERE revision_id IN ({placeholders})",
                        tuple(manual_revision_ids),
                    ).fetchall()
                }
                known_manual_revision_ids.update(additional_manual_revision_ids)
                if known_manual_revision_ids != set(manual_revision_ids):
                    raise StoreError("Snapshot-Revisionsreferenz ist nicht geschlossen.")
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
        cataloged_decisions.update(additional_decision_refs)
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
            *((version, "identity") for version in _SUPPORTED_IDENTITY_RULE_VERSIONS),
            *((version, "mapping") for version in _SUPPORTED_MAPPING_RULE_VERSIONS),
            (_FIXED_PLAUSIBILITY_RULE_VERSION, "plausibility"),
        }
        if has_rule_refs is not None:
            cataloged_rules.update(
                (str(row[0]), str(row[1]))
                for row in self._metadata.execute(
                    "SELECT rule_version_id, rule_kind FROM rule_version_refs"
                ).fetchall()
            )
        cataloged_rules.update(additional_rule_refs)
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
                      OR (v.identity_candidate_id != r.logical_measurement_id
                          AND r.conflict_resolution_decision_id IS NULL)
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
                      OR canonical_type NOT IN ({_CANONICAL_HEALTH_TYPES_SQL})
                      OR canonical_unit != CASE
                          WHEN canonical_type IN ('active_energy', 'dietary_energy_consumed')
                              THEN 'kcal'
                          WHEN canonical_type = 'apple_resting_heart_rate' THEN 'count/min'
                          WHEN canonical_type = 'apple_exercise_time' THEN 'min'
                          WHEN canonical_type = 'step_count' THEN 'count'
                          WHEN canonical_type = 'walking_running_distance' THEN 'km'
                          WHEN canonical_type = 'body_mass' THEN 'kg'
                          WHEN canonical_type LIKE 'sleep_%' THEN 'count'
                          WHEN canonical_type = 'dietary_water' THEN 'mL'
                          ELSE 'g'
                      END)
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
                          'suspected_source_deletion', 'source_conflict', 'rule_definition',
                          'preferred_daily_weight_conflict', 'workout_plausibility',
                          'workout_overlap'
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
                          OR
                          (case_kind = 'preferred_daily_weight_conflict'
                           AND logical_measurement_id IS NOT NULL
                           AND measurement_version_id IS NOT NULL
                           AND rule_version_id IS NULL)
                          OR
                          (case_kind IN ('workout_plausibility', 'workout_overlap')
                           AND logical_measurement_id IS NOT NULL
                           AND measurement_version_id IS NOT NULL
                           AND rule_version_id IS NULL)
                          OR
                          (case_kind = 'rule_definition'
                           AND logical_measurement_id IS NULL
                           AND measurement_version_id IS NULL
                           AND rule_version_id IS NULL)
                      ))
              + (SELECT count(*) FROM reviews r LEFT JOIN versions v
                   ON v.measurement_version_id = r.measurement_version_id
                   WHERE r.measurement_version_id IS NOT NULL
                     AND r.case_kind NOT IN ('workout_plausibility', 'workout_overlap')
                     AND v.measurement_version_id IS NULL)
              + (SELECT count(*) FROM reviews r LEFT JOIN resolved m
                   ON m.logical_measurement_id = r.logical_measurement_id
                   WHERE r.logical_measurement_id IS NOT NULL
                     AND r.case_kind NOT IN (
                         'rule_definition', 'workout_plausibility', 'workout_overlap'
                     )
                     AND m.logical_measurement_id IS NULL)
            """
        ).fetchone()
        if invalid is None or int(invalid[0]) != 0:
            raise StoreError("Snapshot-ID-Schließung oder Payloadvalidierung fehlgeschlagen.")
        if manifest["snapshot_schema_version"] >= 6:
            lineage = paths["derivation_lineage"]
            invalid_lineage = self._query.execute(
                f"""
                WITH lineage AS (SELECT * FROM read_parquet('{lineage}')),
                sources AS (
                    SELECT identity_candidate_id AS source_logical_id,
                           measurement_version_id AS source_version_id
                    FROM read_parquet('{paths["measurement_versions"]}')
                    UNION ALL
                    SELECT identity_candidate_id, measurement_version_id
                    FROM read_parquet('{paths["sleep_intervals"]}')
                    UNION ALL
                    SELECT logical_workout_id, workout_version_id
                    FROM read_parquet('{paths["workouts"]}')
                ),
                expected AS (
                    SELECT
                        sha256('resolved-measurement/v1:' || r.logical_measurement_id),
                        'resolved_measurement',
                        'resolved-measurement/v1',
                        v.identity_candidate_id,
                        r.selected_measurement_version_id,
                        'selected_source_version'
                    FROM read_parquet('{paths["resolved_measurements"]}') r
                    JOIN read_parquet('{paths["measurement_versions"]}') v
                      ON v.measurement_version_id = r.selected_measurement_version_id
                    UNION ALL
                    SELECT
                        sha256('resolved-workout/v1:' || r.logical_workout_id),
                        'resolved_workout',
                        'resolved-workout/v1',
                        r.logical_workout_id,
                        r.selected_workout_version_id,
                        'selected_source_version'
                    FROM read_parquet('{paths["resolved_workouts"]}') r
                    JOIN read_parquet('{paths["workouts"]}') w
                      ON w.workout_version_id = r.selected_workout_version_id
                    UNION ALL
                    SELECT d.derived_record_id, 'weight_nutrition_day',
                           'weight-nutrition-day/v1', v.identity_candidate_id,
                           v.measurement_version_id, 'daily_feature_contributor'
                    FROM read_parquet('{paths["weight_nutrition_days"]}') d
                    JOIN read_parquet('{paths["measurement_versions"]}') v
                      ON v.measurement_local_date = d.day AND v.canonical_type = d.feature_kind
                    JOIN read_parquet('{paths["resolved_measurements"]}') r
                      ON r.selected_measurement_version_id = v.measurement_version_id
                    WHERE r.disposition IN ('included_source', 'included_correction')
                    UNION ALL
                    SELECT d.derived_record_id, 'activity_day', 'activity-day/v1',
                           v.identity_candidate_id, v.measurement_version_id,
                           'daily_metric_contributor'
                    FROM read_parquet('{paths["activity_days"]}') d
                    JOIN read_parquet('{paths["measurement_versions"]}') v
                      ON v.measurement_local_date = d.day AND v.canonical_type = d.metric
                    JOIN read_parquet('{paths["resolved_measurements"]}') r
                      ON r.selected_measurement_version_id = v.measurement_version_id
                    WHERE r.disposition IN ('included_source', 'included_correction')
                      AND ((v.source_name = 'Apple Watch' AND v.device = 'Apple Watch')
                           OR ((v.source_name = 'iPhone' AND v.device = 'iPhone')
                               AND EXISTS (
                                   SELECT 1
                                   FROM read_parquet('{paths["activity_coverage_segments"]}') c
                                   WHERE c.coverage_kind = 'iphone_fallback'
                                     AND c.start_utc::TIMESTAMPTZ
                                         <= v.source_start_utc::TIMESTAMPTZ
                                     AND v.source_end_utc::TIMESTAMPTZ
                                         <= c.end_utc::TIMESTAMPTZ)))
                    UNION ALL
                    SELECT c.derived_record_id, 'activity_coverage', 'activity-coverage/v1',
                           v.identity_candidate_id, v.measurement_version_id, 'coverage_interval'
                    FROM read_parquet('{paths["activity_coverage_segments"]}') c
                    JOIN read_parquet('{paths["measurement_versions"]}') v
                      ON (c.coverage_kind = 'unobserved'
                          OR (v.source_start_utc::TIMESTAMPTZ < c.end_utc::TIMESTAMPTZ
                              AND c.start_utc::TIMESTAMPTZ
                                  < v.source_end_utc::TIMESTAMPTZ))
                    JOIN read_parquet('{paths["resolved_measurements"]}') r
                      ON r.selected_measurement_version_id = v.measurement_version_id
                    WHERE r.disposition IN ('included_source', 'included_correction')
                      AND v.canonical_type IN ('apple_exercise_time', 'step_count',
                                               'walking_running_distance', 'active_energy')
                      AND ((v.source_name = 'Apple Watch' AND v.device = 'Apple Watch')
                           OR (v.source_name = 'iPhone' AND v.device = 'iPhone'))
                    UNION ALL
                    SELECT f.derived_record_id, 'workout_feature', 'workout-feature/v1',
                           w.logical_workout_id, w.workout_version_id,
                           'workout_source_version'
                    FROM read_parquet('{paths["workout_features"]}') f
                    JOIN read_parquet('{paths["workouts"]}') w USING (logical_workout_id)
                    JOIN read_parquet('{paths["resolved_workouts"]}') r
                      ON r.selected_workout_version_id = w.workout_version_id
                ),
                derived_ids AS (
                    SELECT sha256('resolved-measurement/v1:' || logical_measurement_id)
                               AS derived_record_id,
                           'resolved_measurement' AS derived_family,
                           'resolved-measurement/v1' AS derivation_contract_id
                    FROM read_parquet('{paths["resolved_measurements"]}')
                    UNION ALL SELECT sha256('resolved-workout/v1:' || logical_workout_id),
                           'resolved_workout', 'resolved-workout/v1'
                    FROM read_parquet('{paths["resolved_workouts"]}')
                    UNION ALL SELECT derived_record_id, 'weight_nutrition_day',
                           'weight-nutrition-day/v1'
                    FROM read_parquet('{paths["weight_nutrition_days"]}')
                    UNION ALL SELECT derived_record_id, 'sleep_episode', 'sleep-episode/v1'
                    FROM read_parquet('{paths["sleep_episodes"]}')
                    UNION ALL SELECT derived_record_id, 'sleep_night', 'sleep-night/v1'
                    FROM read_parquet('{paths["sleep_nights"]}')
                    UNION ALL SELECT derived_record_id, 'activity_day', 'activity-day/v1'
                    FROM read_parquet('{paths["activity_days"]}')
                    UNION ALL SELECT derived_record_id, 'activity_coverage',
                           'activity-coverage/v1'
                    FROM read_parquet('{paths["activity_coverage_segments"]}')
                    UNION ALL SELECT derived_record_id, 'workout_feature', 'workout-feature/v1'
                    FROM read_parquet('{paths["workout_features"]}')
                    UNION ALL SELECT derived_record_id, 'daily_context', 'daily-context/v1'
                    FROM read_parquet('{paths["daily_context"]}')
                    UNION ALL SELECT derived_record_id, 'medication_context',
                           'medication-context/v1'
                    FROM read_parquet('{paths["medication_context"]}')
                )
                SELECT
                    (SELECT count(*) - count(DISTINCT
                        derived_record_id || ':' || source_version_id || ':' || contribution_role
                    ) FROM lineage)
                  + (SELECT count(*) FROM lineage
                     WHERE NOT regexp_full_match(derived_record_id, '[0-9a-f]{{64}}')
                        OR NOT regexp_full_match(source_logical_id, '[0-9a-f]{{32}}|[0-9a-f]{{64}}')
                        OR NOT regexp_full_match(source_version_id, '[0-9a-f]{{32}}|[0-9a-f]{{64}}')
                        OR NOT regexp_full_match(snapshot_id, '[0-9a-f]{{32}}')
                        OR try_cast(derived_at_utc AS TIMESTAMPTZ) IS NULL
                        OR contribution_role NOT IN (
                            'selected_source_version', 'daily_feature_contributor',
                            'episode_interval', 'night_interval', 'daily_metric_contributor',
                            'coverage_interval', 'workout_source_version', 'manual_revision'
                        )
                        OR (derived_family, derivation_contract_id) NOT IN (
                            ('resolved_measurement', 'resolved-measurement/v1'),
                            ('resolved_workout', 'resolved-workout/v1'),
                            ('weight_nutrition_day', 'weight-nutrition-day/v1'),
                            ('sleep_episode', 'sleep-episode/v1'),
                            ('sleep_night', 'sleep-night/v1'),
                            ('activity_day', 'activity-day/v1'),
                            ('activity_coverage', 'activity-coverage/v1'),
                            ('workout_feature', 'workout-feature/v1'),
                            ('daily_context', 'daily-context/v1'),
                            ('medication_context', 'medication-context/v1')
                        ))
                  + (SELECT count(*) FROM lineage l LEFT JOIN sources s
                     USING (source_logical_id, source_version_id)
                     WHERE length(l.source_version_id) = 64 AND s.source_version_id IS NULL)
                  + (SELECT count(*) FROM (
                        SELECT * FROM expected EXCEPT
                        SELECT derived_record_id, derived_family, derivation_contract_id,
                               source_logical_id, source_version_id, contribution_role
                        FROM lineage
                    ))
                  + (SELECT count(*) FROM (
                        SELECT derived_record_id, derived_family, derivation_contract_id,
                               source_logical_id, source_version_id, contribution_role
                        FROM lineage
                        WHERE length(source_version_id) = 64
                          AND derived_family NOT IN ('sleep_episode', 'sleep_night')
                        EXCEPT SELECT * FROM expected
                    ))
                  + (SELECT count(*) FROM derived_ids d LEFT JOIN lineage l
                     USING (derived_record_id, derived_family, derivation_contract_id)
                     WHERE l.derived_record_id IS NULL)
                  + (SELECT count(*) FROM lineage l LEFT JOIN derived_ids d
                     USING (derived_record_id, derived_family, derivation_contract_id)
                     WHERE d.derived_record_id IS NULL)
                """
            ).fetchone()
            if invalid_lineage is None or int(invalid_lineage[0]) != 0:
                raise StoreError("Snapshot-Lineage ist nicht vollständig oder geschlossen.")
            manual_lineage_revision_ids = {
                str(row[0])
                for row in self._query.execute(
                    f"SELECT DISTINCT source_version_id FROM read_parquet('{lineage}') "
                    "WHERE length(source_version_id) = 32"
                ).fetchall()
            }
            assert isinstance(snapshot_binding, dict)
            if not manual_lineage_revision_ids <= set(snapshot_binding["manual_revision_ids"]):
                raise StoreError("Snapshot-Lineage verweist auf ungebundene Revisionen.")
            lineage_snapshot_ids = {
                str(row[0])
                for row in self._query.execute(
                    f"SELECT DISTINCT snapshot_id FROM read_parquet('{lineage}')"
                ).fetchall()
            }
            unknown_lineage_snapshots = lineage_snapshot_ids - {snapshot_id}
            if unknown_lineage_snapshots:
                known = {
                    str(row[0])
                    for row in self._metadata.execute(
                        "SELECT snapshot_id FROM dataset_snapshots WHERE snapshot_id IN ("
                        + ",".join("?" for _ in unknown_lineage_snapshots)
                        + ")",
                        tuple(sorted(unknown_lineage_snapshots)),
                    ).fetchall()
                }
                if known != unknown_lineage_snapshots:
                    raise StoreError("Snapshot-Lineage verweist auf unbekannte Snapshots.")
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
        records: tuple[CanonicalHealthRecord | CanonicalSleepInterval | CanonicalWorkout, ...],
        logical_measurement_count: int,
        measurement_version_count: int,
        source_occurrence_count: int,
        anomaly_count: int,
        unsupported_content: tuple[UnsupportedImportContent, ...],
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
            {
                (
                    str(import_id),
                    str(
                        record.workout_version_id
                        if isinstance(record, CanonicalWorkout)
                        else record.measurement_version_id
                    ),
                )
                for record in records
            },
        )
        self._metadata.execute(
            "INSERT INTO import_canonical_counts VALUES (?, ?, ?, ?, ?)",
            (
                str(import_id),
                logical_measurement_count,
                measurement_version_count,
                source_occurrence_count,
                anomaly_count,
            ),
        )
        self._metadata.executemany(
            "INSERT INTO unsupported_import_content VALUES (?, ?, ?, ?)",
            (
                (str(import_id), item.category, item.external_identifier, item.count)
                for item in unsupported_content
            ),
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
        sleep_path = (
            self._root
            / _PARQUET_DIRECTORY
            / "snapshots"
            / str(snapshot_id)
            / "sleep_intervals.parquet"
        )
        escaped_sleep_path = str(sleep_path).replace("'", "''")
        sleep_count_row = self._query.execute(
            f"SELECT count(*), count(DISTINCT identity_candidate_id) "
            f"FROM read_parquet('{escaped_sleep_path}')"
        ).fetchone()
        assert sleep_count_row is not None
        sleep_version_count, sleep_logical_count = map(int, sleep_count_row)
        return PublishImportResult(
            status=status,
            snapshot_id=snapshot_id,
            record_count=record_count,
            logical_measurement_count=logical_count + sleep_logical_count,
            measurement_version_count=version_count + sleep_version_count,
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
        sleep_path = path.with_name("sleep_intervals.parquet")
        escaped_sleep_path = str(sleep_path).replace("'", "''")
        sleep_row = self._query.execute(
            f"SELECT count(*) FROM read_parquet('{escaped_sleep_path}')"
        ).fetchone()
        assert sleep_row is not None
        workouts_path = path.with_name("workouts.parquet")
        if not workouts_path.exists():
            return int(row[0]) + int(sleep_row[0])
        escaped_workouts_path = str(workouts_path).replace("'", "''")
        workout_row = self._query.execute(
            f"SELECT count(*) FROM read_parquet('{escaped_workouts_path}')"
        ).fetchone()
        assert workout_row is not None
        return int(row[0]) + int(sleep_row[0]) + int(workout_row[0])

    def publish_data_review_resolution(
        self,
        *,
        operation_id: OperationId,
        snapshot_id: SnapshotId,
        review_case_id: str | None,
        logical_measurement_id: str | None,
        action: Literal[
            "confirm",
            "reject",
            "prefer",
            "split",
            "correct",
            "exclude_local",
            "accept_source",
            "correct_workout",
            "exclude_workout_local",
        ],
        selected_measurement_version_id: str | None,
        candidate_version_ids: tuple[str, ...],
        note: str | None,
        reason: str | None,
        corrected_value: float | None,
        canonical_unit: str | None,
        cycle_updates: tuple[ReviewCycleUpdate, ...],
        corrected_distance_kilometers: float | None = None,
        corrected_active_energy_kilocalories: float | None = None,
    ) -> PublishDecisionResult:
        self._require_open()
        self._require_writer()
        active = self.load_active_snapshot_id()
        if active is None:
            raise StoreError("Aktiver Snapshot fehlt.")
        case = next(
            (
                item
                for item in self.load_open_data_review_cases()
                if review_case_id is not None and item.review_case_id == review_case_id
            ),
            None,
        )
        if review_case_id is not None and (case is None or case.logical_measurement_id is None):
            raise StoreError("Datenprüffall ist nicht mehr offen.")
        logical_id = (
            logical_measurement_id
            if case is None
            or (case is not None and case.kind in {"workout_plausibility", "workout_overlap"})
            else str(case.logical_measurement_id)
        )
        if logical_id is None:
            raise StoreError("Logische Quellmessung fehlt.")
        case_logical_id = None if case is None else case.logical_measurement_id
        if case is not None:
            assert case_logical_id is not None
        candidates = (
            candidate_version_ids
            if candidate_version_ids
            else (
                ()
                if case_logical_id is None
                else self.load_source_conflict_candidates(case_logical_id)
            )
        )
        if action == "prefer" and selected_measurement_version_id not in {
            str(item) for item in candidates
        }:
            raise StoreError("Bevorzugte Quellversion gehört nicht zum Prüffall.")
        decision_id = uuid4().hex
        audit_event_id = uuid4().hex
        audit_position = int(
            self._metadata.execute(
                "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
            ).fetchone()[0]
        )
        case_kind = "direct_correction" if case is None else case.kind
        is_workout_case = case_kind in {"workout_plausibility", "workout_overlap"}
        recorded_case_kind = "plausibility" if is_workout_case else case_kind
        recorded_action = {
            "correct_workout": "correct",
            "exclude_workout_local": "exclude_local",
        }.get(action, action)
        previous_version = (
            self._resolved_workout_version(active, logical_id)
            if is_workout_case
            else self._resolved_version(active, logical_id)
        )
        resolved = None if is_workout_case else self.load_resolved_measurement(logical_id)
        superseded_decision_id = (
            None
            if resolved is None
            or (
                action
                not in {
                    "correct",
                    "exclude_local",
                    "accept_source",
                    "correct_workout",
                    "exclude_workout_local",
                }
                and not (action == "confirm" and case_kind == "continued_override")
            )
            else resolved.effective_decision_id
        )
        superseded_audit = (
            None
            if superseded_decision_id is None
            else self._metadata.execute(
                "SELECT d.audit_event_id FROM data_review_decisions d "
                "WHERE d.decision_id = ? AND NOT EXISTS ("
                "SELECT 1 FROM metadata_tombstones t "
                "WHERE t.target_audit_event_id = d.audit_event_id)",
                (superseded_decision_id,),
            ).fetchone()
        )
        decision_kind = {
            "correct": "correction",
            "exclude_local": "local_exclusion",
            "accept_source": "source_value_acceptance",
            "prefer": "conflict_resolution",
            "split": "conflict_resolution",
            "reject": "source_deletion",
            "correct_workout": "correction",
            "exclude_workout_local": "local_exclusion",
        }.get(
            action,
            "source_deletion" if case_kind == "suspected_source_deletion" else "confirmation",
        )
        completed_at = datetime.now(UTC).isoformat()
        with self._metadata:
            self._metadata.execute(
                "INSERT INTO write_operations VALUES (?, 'resolve_data_review_case', ?, ?, "
                "'committed', 1)",
                (str(operation_id), completed_at, completed_at),
            )
            self._metadata.execute(
                "INSERT INTO decision_refs VALUES (?, ?)",
                (decision_id, decision_kind),
            )
            self._metadata.execute(
                "INSERT INTO audit_events VALUES (?, ?, ?, 'data_review_decision', ?)",
                (audit_position, audit_event_id, str(operation_id), completed_at),
            )
            self._metadata.execute(
                "INSERT INTO data_review_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    audit_event_id,
                    decision_id,
                    review_case_id,
                    recorded_case_kind,
                    logical_id,
                    (
                        hashlib.sha256(
                            f"direct_correction:{selected_measurement_version_id}".encode()
                        ).hexdigest()
                        if case is None
                        else case.evidence_fingerprint
                    ),
                    recorded_action,
                    selected_measurement_version_id,
                    previous_version,
                    json.dumps([str(item) for item in candidates], separators=(",", ":")),
                    note,
                    reason,
                ),
            )
            snapshot_audit_position = audit_position
            if superseded_audit is not None:
                tombstone_audit_id = uuid4().hex
                snapshot_audit_position += 1
                self._metadata.execute(
                    "INSERT INTO audit_events VALUES (?, ?, ?, 'metadata_tombstone', ?)",
                    (
                        snapshot_audit_position,
                        tombstone_audit_id,
                        str(operation_id),
                        completed_at,
                    ),
                )
                self._metadata.execute(
                    "INSERT INTO metadata_tombstones VALUES (?, ?, ?, 'superseded', ?, NULL)",
                    (uuid4().hex, tombstone_audit_id, str(superseded_audit[0]), audit_event_id),
                )
            if case_kind == "suspected_source_deletion" and action in {"confirm", "reject"}:
                self._metadata.execute(
                    "INSERT INTO source_absence_suppressions VALUES (?, ?, 1) "
                    "ON CONFLICT(logical_measurement_id) DO UPDATE SET "
                    "decision_id = excluded.decision_id, active = 1",
                    (logical_id, decision_id),
                )
            self._metadata.executemany(
                "UPDATE review_cycles SET open_case_count = ?, status = ? WHERE cycle_id = ?",
                (
                    (update.open_case_count, update.status, str(update.cycle_id))
                    for update in cycle_updates
                ),
            )
            manifest_sha256 = self._stage_review_snapshot(
                parent_snapshot_id=active,
                snapshot_id=snapshot_id,
                operation_id=operation_id,
                audit_position=snapshot_audit_position,
                review_case_id=review_case_id,
                decision_id=decision_id,
                action=action,
                selected_measurement_version_id=selected_measurement_version_id,
                candidate_version_ids=tuple(str(item) for item in candidates),
                corrected_value=corrected_value,
                canonical_unit=canonical_unit,
                corrected_distance_kilometers=corrected_distance_kilometers,
                corrected_active_energy_kilocalories=corrected_active_energy_kilocalories,
            )
            self._activate_review_snapshot(
                operation_id, snapshot_id, active, manifest_sha256, completed_at
            )
        return PublishDecisionResult(operation_id, decision_id, snapshot_id)

    def publish_data_review_batch(
        self,
        *,
        operation_id: OperationId,
        snapshot_id: SnapshotId,
        selection_kind: str | None,
        materialized_matches: str,
        case_ids: tuple[str, ...],
        note: str | None,
        cycle_updates: tuple[ReviewCycleUpdate, ...],
    ) -> PublishBatchDecisionResult:
        self._require_open()
        self._require_writer()
        active = self.load_active_snapshot_id()
        if active is None:
            raise StoreError("Aktiver Snapshot fehlt.")
        selected_cases = {
            case.review_case_id: case
            for case in self.load_open_data_review_cases()
            if (selection_kind is None or case.kind == selection_kind)
        }
        if not set(case_ids) <= selected_cases.keys():
            raise StoreError("Sammelmenge hat sich geändert.")
        open_cases = {case_id: selected_cases[case_id] for case_id in case_ids}
        batch_action_id = uuid4().hex
        decisions = tuple((open_cases[case_id], uuid4().hex) for case_id in case_ids)
        audit_position = int(
            self._metadata.execute(
                "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
            ).fetchone()[0]
        )
        completed_at = datetime.now(UTC).isoformat()
        with self._metadata:
            self._metadata.execute(
                "INSERT INTO write_operations VALUES (?, 'resolve_data_review_case', ?, ?, "
                "'committed', 1)",
                (str(operation_id), completed_at, completed_at),
            )
            self._metadata.execute(
                "INSERT INTO data_review_batch_actions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    batch_action_id,
                    str(operation_id),
                    selection_kind,
                    materialized_matches,
                    len(decisions),
                    note,
                ),
            )
            for offset, (case, decision_id) in enumerate(decisions):
                audit_event_id = uuid4().hex
                self._metadata.execute(
                    "INSERT INTO decision_refs VALUES (?, 'confirmation')", (decision_id,)
                )
                self._metadata.execute(
                    "INSERT INTO audit_events VALUES (?, ?, ?, 'data_review_decision', ?)",
                    (audit_position + offset, audit_event_id, str(operation_id), completed_at),
                )
                assert case.logical_measurement_id is not None
                logical_id = str(case.logical_measurement_id)
                selected = (
                    None
                    if case.measurement_version_id is None
                    else str(case.measurement_version_id)
                )
                self._metadata.execute(
                    "INSERT INTO data_review_decisions VALUES "
                    "(?, ?, ?, ?, ?, ?, 'confirm', ?, ?, '[]', ?, NULL)",
                    (
                        audit_event_id,
                        decision_id,
                        case.review_case_id,
                        case.kind,
                        logical_id,
                        case.evidence_fingerprint,
                        selected,
                        self._resolved_version(active, logical_id),
                        note,
                    ),
                )
                self._metadata.execute(
                    "INSERT INTO data_review_batch_members VALUES (?, ?)",
                    (batch_action_id, decision_id),
                )
            self._metadata.executemany(
                "UPDATE review_cycles SET open_case_count = ?, status = ? WHERE cycle_id = ?",
                (
                    (update.open_case_count, update.status, str(update.cycle_id))
                    for update in cycle_updates
                ),
            )
            manifest_sha256 = self._stage_review_snapshot(
                parent_snapshot_id=active,
                snapshot_id=snapshot_id,
                operation_id=operation_id,
                audit_position=audit_position + len(decisions) - 1,
                review_case_id=None,
                decision_id=batch_action_id,
                action="confirm_batch",
                selected_measurement_version_id=None,
                candidate_version_ids=(),
                batch_confirmations=decisions,
            )
            self._activate_review_snapshot(
                operation_id, snapshot_id, active, manifest_sha256, completed_at
            )
        return PublishBatchDecisionResult(
            operation_id,
            batch_action_id,
            tuple(decision_id for _, decision_id in decisions),
            snapshot_id,
        )

    def revoke_data_review_decision(
        self,
        *,
        operation_id: OperationId,
        snapshot_id: SnapshotId,
        decision_id: str,
        reason: str,
        cycle_updates: tuple[ReviewCycleUpdate, ...],
    ) -> PublishDecisionResult:
        self._require_open()
        self._require_writer()
        active = self.load_active_snapshot_id()
        if active is None:
            raise StoreError("Aktiver Snapshot fehlt.")
        row = self._metadata.execute(
            """
            SELECT d.audit_event_id, d.review_case_id, d.case_kind,
                   d.logical_measurement_id, d.evidence_fingerprint, d.action,
                   d.selected_measurement_version_id,
                   d.previous_measurement_version_id, d.candidate_version_ids
            FROM data_review_decisions d
            WHERE d.decision_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM metadata_tombstones t
                  WHERE t.target_audit_event_id = d.audit_event_id
              )
            """,
            (decision_id,),
        ).fetchone()
        if row is None:
            raise StoreError("Entscheidung ist nicht wirksam.")
        target_audit_id = str(row[0])
        review_case_id = None if row[1] is None else str(row[1])
        _case_kind = str(row[2])
        _logical_id = str(row[3])
        _evidence = str(row[4])
        _action = str(row[5])
        selected_version = None if row[6] is None else str(row[6])
        previous_version = str(row[7])
        candidates_json = str(row[8])
        case_audit_id = target_audit_id
        if review_case_id is None:
            prior_case = self._metadata.execute(
                "SELECT d.review_case_id, d.audit_event_id "
                "FROM data_review_decisions d JOIN audit_events e USING (audit_event_id) "
                "WHERE d.logical_measurement_id = ? AND d.review_case_id IS NOT NULL "
                "ORDER BY e.audit_position DESC LIMIT 1",
                (_logical_id,),
            ).fetchone()
            if prior_case is not None:
                review_case_id, case_audit_id = map(str, prior_case)
        basis = self._metadata.execute(
            "SELECT a.previous_snapshot_id FROM snapshot_activations a "
            "JOIN audit_events e ON e.operation_id = a.operation_id "
            "WHERE e.audit_event_id = ?",
            (case_audit_id,),
        ).fetchone()
        if basis is None or basis[0] is None:
            raise StoreError("Entscheidungsbasis fehlt.")
        case_path = (
            self._root
            / _PARQUET_DIRECTORY
            / "snapshots"
            / str(basis[0])
            / "open_review_cases.parquet"
        )
        escaped_case_path = str(case_path).replace("'", "''")
        case_row = self._query.execute(
            "SELECT review_case_id, case_kind, logical_measurement_id, "
            "measurement_version_id, rule_version_id, evidence_fingerprint "
            f"FROM read_parquet('{escaped_case_path}') WHERE review_case_id = ?",
            (review_case_id,),
        ).fetchone()
        if case_row is None and review_case_id is not None:
            raise StoreError("Ursprünglicher Datenprüffall fehlt.")
        reopened_case = (
            None
            if case_row is None
            else OpenDataReviewCase(
                review_case_id=str(case_row[0]),
                kind=cast(
                    Literal[
                        "plausibility",
                        "continued_override",
                        "suspected_source_deletion",
                        "source_conflict",
                        "rule_definition",
                    ],
                    str(case_row[1]),
                ),
                logical_measurement_id=LogicalMeasurementId(str(case_row[2])),
                measurement_version_id=(
                    None if case_row[3] is None else MeasurementVersionId(str(case_row[3]))
                ),
                rule_version_id=None if case_row[4] is None else str(case_row[4]),
                evidence_fingerprint=str(case_row[5]),
            )
        )
        audit_event_id = uuid4().hex
        audit_position = int(
            self._metadata.execute(
                "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
            ).fetchone()[0]
        )
        completed_at = datetime.now(UTC).isoformat()
        with self._metadata:
            self._metadata.execute(
                "INSERT INTO write_operations VALUES (?, 'revoke_data_review_decision', ?, ?, "
                "'committed', 1)",
                (str(operation_id), completed_at, completed_at),
            )
            self._metadata.execute(
                "INSERT INTO audit_events VALUES (?, ?, ?, 'metadata_tombstone', ?)",
                (audit_position, audit_event_id, str(operation_id), completed_at),
            )
            self._metadata.execute(
                "INSERT INTO metadata_tombstones VALUES (?, ?, ?, 'revoked', NULL, ?)",
                (uuid4().hex, audit_event_id, target_audit_id, reason),
            )
            self._metadata.execute(
                "UPDATE source_absence_suppressions SET active = 0 WHERE decision_id = ?",
                (decision_id,),
            )
            self._metadata.executemany(
                "UPDATE review_cycles SET open_case_count = ?, status = ? WHERE cycle_id = ?",
                (
                    (update.open_case_count, update.status, str(update.cycle_id))
                    for update in cycle_updates
                ),
            )
            manifest_sha256 = self._stage_review_snapshot(
                parent_snapshot_id=active,
                snapshot_id=snapshot_id,
                operation_id=operation_id,
                audit_position=audit_position,
                review_case_id=review_case_id,
                decision_id=decision_id,
                action="revoke",
                selected_measurement_version_id=(
                    selected_version
                    if _action in {"correct", "exclude_local", "accept_source"}
                    else previous_version
                ),
                candidate_version_ids=tuple(json.loads(candidates_json)),
                reopened_case=reopened_case,
            )
            self._activate_review_snapshot(
                operation_id, snapshot_id, active, manifest_sha256, completed_at
            )
        return PublishDecisionResult(operation_id, decision_id, snapshot_id)

    def load_effective_batch_decision_ids(self, batch_action_id: str) -> tuple[str, ...]:
        self._require_open()
        rows = self._metadata.execute(
            "SELECT m.decision_id, d.logical_measurement_id, d.case_kind, "
            "d.selected_measurement_version_id, d.previous_measurement_version_id "
            "FROM data_review_batch_members m "
            "JOIN data_review_decisions d USING (decision_id) "
            "JOIN audit_events original_event USING (audit_event_id) "
            "WHERE m.batch_action_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM metadata_tombstones t "
            "WHERE t.target_audit_event_id = d.audit_event_id) AND NOT EXISTS ("
            "SELECT 1 FROM data_review_decisions later "
            "JOIN audit_events later_event USING (audit_event_id) "
            "WHERE later.logical_measurement_id = d.logical_measurement_id "
            "AND later_event.audit_position > original_event.audit_position "
            "AND later_event.operation_id != original_event.operation_id) "
            "ORDER BY m.decision_id",
            (batch_action_id,),
        ).fetchall()
        effective: list[str] = []
        for row in rows:
            resolved = self.load_resolved_measurement(str(row[1]))
            expected_version = str(row[3] if str(row[2]) == "continued_override" else row[4])
            if (
                resolved is not None
                and resolved.selected_measurement_version_id == expected_version
                and resolved.effective_decision_id in {None, str(row[0])}
            ):
                effective.append(str(row[0]))
        return tuple(effective)

    def revoke_data_review_batch(
        self,
        *,
        operation_id: OperationId,
        snapshot_id: SnapshotId,
        batch_action_id: str,
        expected_decision_ids: tuple[str, ...],
        reason: str,
        cycle_updates: tuple[ReviewCycleUpdate, ...],
    ) -> PublishBatchDecisionResult:
        self._require_open()
        self._require_writer()
        active = self.load_active_snapshot_id()
        if active is None:
            raise StoreError("Aktiver Snapshot fehlt.")
        if self.load_effective_batch_decision_ids(batch_action_id) != expected_decision_ids:
            raise StoreError("Wirksame Sammelentscheidungen haben sich geändert.")
        placeholders = ", ".join("?" for _ in expected_decision_ids)
        rows = self._metadata.execute(
            "SELECT d.audit_event_id, d.decision_id, d.review_case_id, "
            "d.selected_measurement_version_id, d.previous_measurement_version_id, "
            "d.candidate_version_ids FROM data_review_decisions d "
            "WHERE d.decision_id IN (SELECT decision_id FROM data_review_batch_members "
            "WHERE batch_action_id = ?) "
            f"AND d.decision_id IN ({placeholders}) AND NOT EXISTS ("
            "SELECT 1 FROM metadata_tombstones t "
            "WHERE t.target_audit_event_id = d.audit_event_id) ORDER BY d.decision_id",
            (batch_action_id, *expected_decision_ids),
        ).fetchall()
        if not rows:
            raise StoreError("Sammelaktion hat keine wirksame Entscheidung.")
        basis = self._metadata.execute(
            "SELECT a.previous_snapshot_id FROM snapshot_activations a "
            "JOIN audit_events e ON e.operation_id = a.operation_id "
            "WHERE e.audit_event_id = ?",
            (str(rows[0][0]),),
        ).fetchone()
        if basis is None or basis[0] is None:
            raise StoreError("Entscheidungsbasis fehlt.")
        case_path = (
            self._root
            / _PARQUET_DIRECTORY
            / "snapshots"
            / str(basis[0])
            / "open_review_cases.parquet"
        )
        escaped_case_path = str(case_path).replace("'", "''")
        revocations: list[tuple[OpenDataReviewCase, str, str, tuple[str, ...]]] = []
        for row in rows:
            case_row = self._query.execute(
                "SELECT review_case_id, case_kind, logical_measurement_id, "
                "measurement_version_id, rule_version_id, evidence_fingerprint "
                f"FROM read_parquet('{escaped_case_path}') WHERE review_case_id = ?",
                (str(row[2]),),
            ).fetchone()
            if case_row is None:
                raise StoreError("Ursprünglicher Datenprüffall fehlt.")
            revocations.append(
                (
                    OpenDataReviewCase(
                        review_case_id=str(case_row[0]),
                        kind=cast(
                            Literal[
                                "plausibility",
                                "continued_override",
                                "suspected_source_deletion",
                                "source_conflict",
                                "rule_definition",
                            ],
                            str(case_row[1]),
                        ),
                        logical_measurement_id=LogicalMeasurementId(str(case_row[2])),
                        measurement_version_id=(
                            None if case_row[3] is None else MeasurementVersionId(str(case_row[3]))
                        ),
                        rule_version_id=None if case_row[4] is None else str(case_row[4]),
                        evidence_fingerprint=str(case_row[5]),
                    ),
                    str(row[1]),
                    str(row[4]),
                    tuple(json.loads(str(row[5]))),
                )
            )
        completed_at = datetime.now(UTC).isoformat()
        audit_position = int(
            self._metadata.execute(
                "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
            ).fetchone()[0]
        )
        with self._metadata:
            self._metadata.execute(
                "INSERT INTO write_operations VALUES (?, 'revoke_data_review_decision', ?, ?, "
                "'committed', 1)",
                (str(operation_id), completed_at, completed_at),
            )
            for offset, row in enumerate(rows):
                audit_event_id = uuid4().hex
                self._metadata.execute(
                    "INSERT INTO audit_events VALUES (?, ?, ?, 'metadata_tombstone', ?)",
                    (audit_position + offset, audit_event_id, str(operation_id), completed_at),
                )
                self._metadata.execute(
                    "INSERT INTO metadata_tombstones VALUES (?, ?, ?, 'revoked', NULL, ?)",
                    (uuid4().hex, audit_event_id, str(row[0]), reason),
                )
            self._metadata.executemany(
                "UPDATE review_cycles SET open_case_count = ?, status = ? WHERE cycle_id = ?",
                (
                    (update.open_case_count, update.status, str(update.cycle_id))
                    for update in cycle_updates
                ),
            )
            manifest_sha256 = self._stage_review_snapshot(
                parent_snapshot_id=active,
                snapshot_id=snapshot_id,
                operation_id=operation_id,
                audit_position=audit_position + len(rows) - 1,
                review_case_id=None,
                decision_id=batch_action_id,
                action="revoke_batch",
                selected_measurement_version_id=None,
                candidate_version_ids=(),
                batch_revocations=tuple(revocations),
            )
            self._activate_review_snapshot(
                operation_id, snapshot_id, active, manifest_sha256, completed_at
            )
        return PublishBatchDecisionResult(
            operation_id,
            batch_action_id,
            tuple(str(row[1]) for row in rows),
            snapshot_id,
        )

    def _resolved_version(self, snapshot_id: SnapshotId, logical_id: str) -> str:
        path = self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id)
        escaped = str(path / "resolved_measurements.parquet").replace("'", "''")
        row = self._query.execute(
            f"SELECT selected_measurement_version_id FROM read_parquet('{escaped}') "
            "WHERE logical_measurement_id = ?",
            (logical_id,),
        ).fetchone()
        if row is None:
            raise StoreError("Aufgelöste Quellmessung fehlt.")
        return str(row[0])

    def _resolved_workout_version(self, snapshot_id: SnapshotId, logical_id: str) -> str:
        path = self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id)
        resolved = path / "resolved_workouts.parquet"
        if not resolved.exists():
            raise StoreError("Aufgelöstes Training fehlt.")
        escaped = str(resolved).replace("'", "''")
        row = self._query.execute(
            f"SELECT selected_workout_version_id FROM read_parquet('{escaped}') "
            "WHERE logical_workout_id = ?",
            (logical_id,),
        ).fetchone()
        if row is None:
            raise StoreError("Aufgelöstes Training fehlt.")
        return str(row[0])

    def _include_source(self, logical_id: str, version_id: str) -> None:
        version = self._query.execute(
            "SELECT canonical_value, canonical_unit FROM measurement_versions "
            "WHERE measurement_version_id = ?",
            (version_id,),
        ).fetchone()
        if version is None:
            raise StoreError("Quellmessungsversion fehlt.")
        self._query.execute(
            "UPDATE resolved_measurements SET selected_measurement_version_id = ?, "
            "disposition = 'included_source', effective_value = ?, canonical_unit = ?, "
            "effective_value_source = 'source', effective_decision_id = NULL, "
            "correction_decision_id = NULL, source_deletion_decision_id = NULL, "
            "conflict_resolution_decision_id = NULL WHERE logical_measurement_id = ?",
            (version_id, float(version[0]), str(version[1]), logical_id),
        )

    def _reopen_review_case(
        self,
        case: OpenDataReviewCase,
        selected_version_id: str | None,
        candidate_version_ids: tuple[str, ...],
        review_case_id: str | None,
    ) -> None:
        assert selected_version_id is not None
        logical_id = str(case.logical_measurement_id)
        if case.kind in {"plausibility", "continued_override"}:
            self._query.execute(
                "DELETE FROM resolved_measurements WHERE logical_measurement_id = ?",
                (logical_id,),
            )
            version = self._query.execute(
                "SELECT canonical_value, canonical_unit FROM measurement_versions "
                "WHERE measurement_version_id = ?",
                (selected_version_id,),
            ).fetchone()
            assert version is not None
            self._query.execute(
                "INSERT INTO resolved_measurements VALUES (?, ?, 'included_source', ?, ?, "
                "'source', NULL, NULL, NULL, NULL)",
                (logical_id, selected_version_id, float(version[0]), str(version[1])),
            )
            self._query.execute(
                "INSERT INTO open_review_cases VALUES (?, ?, ?, ?, ?, ?)",
                (
                    case.review_case_id,
                    case.kind,
                    logical_id,
                    str(case.measurement_version_id),
                    case.rule_version_id,
                    case.evidence_fingerprint,
                ),
            )
            return
        self._query.execute(
            "DELETE FROM resolved_measurements WHERE logical_measurement_id = ?",
            (logical_id,),
        )
        restore_ids = (
            (selected_version_id,)
            if case.kind == "suspected_source_deletion"
            else candidate_version_ids
        )
        restored_identities: set[str] = set()
        previous_identity = self._query.execute(
            "SELECT identity_candidate_id FROM measurement_versions "
            "WHERE measurement_version_id = ?",
            (selected_version_id,),
        ).fetchone()
        assert previous_identity is not None
        for candidate_id in (selected_version_id, *restore_ids):
            version = self._query.execute(
                "SELECT identity_candidate_id, canonical_value, canonical_unit "
                "FROM measurement_versions WHERE measurement_version_id = ?",
                (candidate_id,),
            ).fetchone()
            assert version is not None
            identity_id = str(version[0])
            if identity_id in restored_identities:
                continue
            restored_identities.add(identity_id)
            version_id = (
                selected_version_id if identity_id == str(previous_identity[0]) else candidate_id
            )
            chosen = self._query.execute(
                "SELECT canonical_value, canonical_unit FROM measurement_versions "
                "WHERE measurement_version_id = ?",
                (version_id,),
            ).fetchone()
            assert chosen is not None
            self._query.execute(
                "INSERT INTO resolved_measurements VALUES (?, ?, 'included_source', ?, ?, "
                "'source', NULL, NULL, NULL, NULL)",
                (identity_id, version_id, float(chosen[0]), str(chosen[1])),
            )
        self._query.execute(
            "INSERT INTO open_review_cases VALUES (?, ?, ?, NULL, NULL, ?)",
            (review_case_id, case.kind, logical_id, case.evidence_fingerprint),
        )

    def _stage_review_snapshot(
        self,
        *,
        parent_snapshot_id: SnapshotId,
        snapshot_id: SnapshotId,
        operation_id: OperationId,
        audit_position: int,
        review_case_id: str | None,
        decision_id: str,
        action: str,
        selected_measurement_version_id: str | None,
        candidate_version_ids: tuple[str, ...],
        corrected_value: float | None = None,
        canonical_unit: str | None = None,
        corrected_distance_kilometers: float | None = None,
        corrected_active_energy_kilocalories: float | None = None,
        reopened_case: OpenDataReviewCase | None = None,
        replacement_plausibility_cases: tuple[OpenDataReviewCase, ...] | None = None,
        replaced_measurement_version_ids: tuple[str, ...] = (),
        batch_confirmations: tuple[tuple[OpenDataReviewCase, str], ...] = (),
        batch_revocations: tuple[tuple[OpenDataReviewCase, str, str, tuple[str, ...]], ...] = (),
        snapshot_as_of: datetime | None = None,
        context_timezone: str = "Europe/Berlin",
    ) -> str:
        bound_as_of = (
            datetime.now(ZoneInfo(context_timezone)) if snapshot_as_of is None else snapshot_as_of
        )
        parent = self._root / _PARQUET_DIRECTORY / "snapshots" / str(parent_snapshot_id)
        staging = self._root / "staging" / str(operation_id)
        shutil.copytree(parent, staging, copy_function=os.link)
        for filename in _V4_SNAPSHOT_SCHEMAS:
            table = filename.removesuffix(".parquet")
            escaped = str(staging / filename).replace("'", "''")
            self._query.execute(
                f"CREATE OR REPLACE TEMP TABLE {table} AS SELECT * FROM read_parquet('{escaped}')"
            )
        if batch_revocations:
            for batch_case, decision, selected_version, candidates in batch_revocations:
                self._query.execute(
                    "DELETE FROM resolved_measurements WHERE effective_decision_id = ? "
                    "OR conflict_resolution_decision_id = ?",
                    (decision, decision),
                )
                self._reopen_review_case(
                    batch_case,
                    selected_version,
                    candidates,
                    batch_case.review_case_id,
                )
        elif batch_confirmations:
            self._query.executemany(
                "DELETE FROM open_review_cases WHERE review_case_id = ?",
                ((batch_case.review_case_id,) for batch_case, _ in batch_confirmations),
            )
            for batch_case, _decision_id in batch_confirmations:
                if batch_case.kind == "continued_override":
                    assert batch_case.logical_measurement_id is not None
                    assert batch_case.measurement_version_id is not None
                    self._include_source(
                        str(batch_case.logical_measurement_id),
                        str(batch_case.measurement_version_id),
                    )
        elif replacement_plausibility_cases is not None:
            if replaced_measurement_version_ids:
                placeholders = ",".join("?" for _ in replaced_measurement_version_ids)
                self._query.execute(
                    f"DELETE FROM open_review_cases WHERE case_kind = 'plausibility' "
                    f"AND measurement_version_id IN ({placeholders})",
                    replaced_measurement_version_ids,
                )
            if replacement_plausibility_cases:
                case_ids = tuple(case.review_case_id for case in replacement_plausibility_cases)
                placeholders = ",".join("?" for _ in case_ids)
                self._query.execute(
                    f"DELETE FROM open_review_cases WHERE review_case_id IN ({placeholders})",
                    case_ids,
                )
                self._query.executemany(
                    "INSERT INTO open_review_cases VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        (
                            case.review_case_id,
                            case.kind,
                            None
                            if case.logical_measurement_id is None
                            else str(case.logical_measurement_id),
                            None
                            if case.measurement_version_id is None
                            else str(case.measurement_version_id),
                            case.rule_version_id,
                            case.evidence_fingerprint,
                        )
                        for case in replacement_plausibility_cases
                    ),
                )
        elif action == "revoke":
            self._query.execute(
                "DELETE FROM resolved_measurements WHERE effective_decision_id = ? "
                "OR conflict_resolution_decision_id = ?",
                (decision_id, decision_id),
            )
            if reopened_case is None:
                assert selected_measurement_version_id is not None
                version = self._query.execute(
                    "SELECT identity_candidate_id, canonical_value, canonical_unit "
                    "FROM measurement_versions WHERE measurement_version_id = ?",
                    (selected_measurement_version_id,),
                ).fetchone()
                assert version is not None
                self._query.execute(
                    "DELETE FROM resolved_measurements WHERE logical_measurement_id = ?",
                    (str(version[0]),),
                )
                self._query.execute(
                    "INSERT INTO resolved_measurements VALUES (?, ?, 'included_source', ?, ?, "
                    "'source', NULL, NULL, NULL, NULL)",
                    (
                        str(version[0]),
                        selected_measurement_version_id,
                        float(version[1]),
                        str(version[2]),
                    ),
                )
            else:
                self._reopen_review_case(
                    reopened_case,
                    selected_measurement_version_id,
                    candidate_version_ids,
                    review_case_id,
                )
        else:
            case = (
                None
                if review_case_id is None
                else self._query.execute(
                    "SELECT case_kind, logical_measurement_id FROM open_review_cases "
                    "WHERE review_case_id = ?",
                    (review_case_id,),
                ).fetchone()
            )
            if case is None and review_case_id is not None:
                raise StoreError("Datenprüffall ist nicht mehr offen.")
            if case is None:
                version_row = self._query.execute(
                    "SELECT identity_candidate_id FROM measurement_versions "
                    "WHERE measurement_version_id = ?",
                    (selected_measurement_version_id,),
                ).fetchone()
                if version_row is None:
                    raise StoreError("Quellmessungsversion fehlt.")
                case_kind, logical_id = "direct_correction", str(version_row[0])
            else:
                case_kind, logical_id = map(str, case)
                self._query.execute(
                    "DELETE FROM open_review_cases WHERE review_case_id = ?",
                    (review_case_id,),
                )
            if case_kind in {"workout_plausibility", "workout_overlap"}:
                if action == "correct_workout":
                    assert selected_measurement_version_id is not None
                    assert corrected_value is not None
                    self._query.execute(
                        "UPDATE resolved_workouts SET disposition = 'included_correction', "
                        "effective_duration_minutes = ?, distance_kilometers = "
                        "COALESCE(?, distance_kilometers), active_energy_kilocalories = "
                        "COALESCE(?, active_energy_kilocalories), effective_decision_id = ? "
                        "WHERE logical_workout_id = ? AND selected_workout_version_id = ?",
                        (
                            corrected_value,
                            corrected_distance_kilometers,
                            corrected_active_energy_kilocalories,
                            decision_id,
                            logical_id,
                            selected_measurement_version_id,
                        ),
                    )
                elif action == "exclude_workout_local":
                    assert selected_measurement_version_id is not None
                    self._query.execute(
                        "UPDATE resolved_workouts SET disposition = 'excluded_local', "
                        "effective_duration_minutes = NULL, distance_kilometers = NULL, "
                        "active_energy_kilocalories = NULL, effective_decision_id = ? "
                        "WHERE logical_workout_id = ? AND selected_workout_version_id = ?",
                        (decision_id, logical_id, selected_measurement_version_id),
                    )
            elif action == "confirm":
                if case_kind == "suspected_source_deletion":
                    self._query.execute(
                        """
                        UPDATE resolved_measurements
                        SET disposition = 'excluded_source_deletion', effective_value = NULL,
                            effective_value_source = 'none', effective_decision_id = ?,
                            source_deletion_decision_id = ?
                        WHERE logical_measurement_id = ?
                        """,
                        (decision_id, decision_id, logical_id),
                    )
                elif case_kind == "continued_override":
                    assert selected_measurement_version_id is not None
                    self._include_source(logical_id, selected_measurement_version_id)
            elif action == "correct":
                assert selected_measurement_version_id is not None
                assert corrected_value is not None
                assert canonical_unit is not None
                self._query.execute(
                    "UPDATE resolved_measurements SET selected_measurement_version_id = ?, "
                    "disposition = 'included_correction', effective_value = ?, "
                    "canonical_unit = ?, effective_value_source = 'correction', "
                    "effective_decision_id = ?, correction_decision_id = ?, "
                    "source_deletion_decision_id = NULL, "
                    "conflict_resolution_decision_id = NULL "
                    "WHERE logical_measurement_id = ?",
                    (
                        selected_measurement_version_id,
                        corrected_value,
                        canonical_unit,
                        decision_id,
                        decision_id,
                        logical_id,
                    ),
                )
            elif action == "exclude_local":
                assert selected_measurement_version_id is not None
                self._query.execute(
                    "UPDATE resolved_measurements SET selected_measurement_version_id = ?, "
                    "disposition = 'excluded_local', effective_value = NULL, "
                    "effective_value_source = 'none', effective_decision_id = ?, "
                    "correction_decision_id = NULL, source_deletion_decision_id = NULL, "
                    "conflict_resolution_decision_id = NULL "
                    "WHERE logical_measurement_id = ?",
                    (selected_measurement_version_id, decision_id, logical_id),
                )
            elif action == "accept_source":
                assert selected_measurement_version_id is not None
                self._include_source(logical_id, selected_measurement_version_id)
            elif action == "prefer":
                assert selected_measurement_version_id is not None
                version = self._query.execute(
                    "SELECT canonical_value, canonical_unit FROM measurement_versions "
                    "WHERE measurement_version_id = ?",
                    (selected_measurement_version_id,),
                ).fetchone()
                if version is None:
                    raise StoreError("Bevorzugte Quellversion fehlt.")
                if candidate_version_ids:
                    placeholders = ",".join("?" for _ in candidate_version_ids)
                    self._query.execute(
                        f"DELETE FROM resolved_measurements "
                        f"WHERE logical_measurement_id != ? "
                        f"AND selected_measurement_version_id IN ({placeholders})",
                        (logical_id, *candidate_version_ids),
                    )
                self._query.execute(
                    """
                    UPDATE resolved_measurements
                    SET selected_measurement_version_id = ?, disposition = 'included_source',
                        effective_value = ?, canonical_unit = ?, effective_value_source = 'source',
                        effective_decision_id = NULL, conflict_resolution_decision_id = ?
                    WHERE logical_measurement_id = ?
                    """,
                    (
                        selected_measurement_version_id,
                        float(version[0]),
                        str(version[1]),
                        decision_id,
                        logical_id,
                    ),
                )
            elif action == "split":
                placeholders = ",".join("?" for _ in candidate_version_ids)
                self._query.execute(
                    f"DELETE FROM resolved_measurements WHERE logical_measurement_id = ? "
                    f"OR selected_measurement_version_id IN ({placeholders})",
                    (logical_id, *candidate_version_ids),
                )
                for version_id in candidate_version_ids:
                    version = self._query.execute(
                        "SELECT canonical_value, canonical_unit FROM measurement_versions "
                        "WHERE measurement_version_id = ?",
                        (version_id,),
                    ).fetchone()
                    assert version is not None
                    split_id = hashlib.sha256(f"{decision_id}:{version_id}".encode()).hexdigest()
                    self._query.execute(
                        "INSERT INTO resolved_measurements VALUES (?, ?, 'included_source', ?, ?, "
                        "'source', NULL, NULL, NULL, ?)",
                        (split_id, version_id, float(version[0]), str(version[1]), decision_id),
                    )
        manual_revision_ids = (
            self._effective_manual_revision_ids()
            if action.startswith("manual_")
            else self._bound_manual_revision_ids(parent_snapshot_id)
        )
        self._refresh_v03_derivations(snapshot_id, bound_as_of, manual_revision_ids)
        self._refresh_derivation_lineage(snapshot_id, bound_as_of, manual_revision_ids)
        manifest = json.loads((staging / "manifest.json").read_bytes())
        rewritten_files = (
            *(
                (
                    "resolved_measurements.parquet",
                    "resolved_workouts.parquet",
                    "workout_review_links.parquet",
                    "open_review_cases.parquet",
                )
                if not action.startswith("manual_")
                else ()
            ),
            "derivation_lineage.parquet",
            *sorted(_V6_DERIVATION_FILES),
        )
        for filename in rewritten_files:
            table = filename.removesuffix(".parquet")
            path = staging / filename
            path.unlink(missing_ok=True)
            escaped = str(path).replace("'", "''")
            self._query.execute(f"COPY (SELECT * FROM {table}) TO '{escaped}' (FORMAT PARQUET)")
        manifest.update(
            snapshot_schema_version=_SNAPSHOT_SCHEMA_VERSION,
            snapshot_id=str(snapshot_id),
            created_at_utc=datetime.now(UTC).isoformat(),
            created_by_operation_id=str(operation_id),
            parent_snapshot_id=str(parent_snapshot_id),
            derivation_contract_ids=list(_DERIVATION_CONTRACT_IDS),
            snapshot_binding=self._snapshot_binding(
                snapshot_as_of=bound_as_of,
                context_timezone=context_timezone,
                manual_revision_ids=manual_revision_ids,
            ),
        )
        manifest["resolution_basis"]["audit_max_position"] = audit_position
        entries = []
        for filename in sorted(_SNAPSHOT_SCHEMAS):
            path = staging / filename
            escaped = str(path).replace("'", "''")
            description = tuple(
                (str(row[0]), str(row[1]))
                for row in self._query.execute(
                    f"DESCRIBE SELECT * FROM read_parquet('{escaped}')"
                ).fetchall()
            )
            row_count = self._query.execute(
                f"SELECT count(*) FROM read_parquet('{escaped}')"
            ).fetchone()
            assert row_count is not None
            entries.append(
                {
                    "name": filename,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "allocated_bytes": path.stat().st_blocks * 512,
                    "row_count": int(row_count[0]),
                    "parquet_schema_fingerprint": hashlib.sha256(
                        json.dumps(description, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "contract_ids": list(_SNAPSHOT_FILE_CONTRACTS[filename]),
                }
            )
        manifest["files"] = entries
        counts = self._query.execute(
            """
            SELECT (SELECT count(DISTINCT export_id) FROM source_occurrences),
                   (SELECT count(*) FROM source_occurrences),
                   (SELECT count(*) FROM measurement_versions),
                   (SELECT count(*) FROM resolved_measurements),
                   (SELECT count(*) FROM resolved_measurements WHERE disposition LIKE 'included%'),
                   (SELECT count(*) FROM resolved_measurements WHERE disposition LIKE 'excluded%'),
                   (SELECT count(*) FROM open_review_cases)
            """
        ).fetchone()
        assert counts is not None
        manifest["validation_counts"] = dict(
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
        )
        manifest_bytes = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        manifest_path = staging / "manifest.json"
        manifest_path.unlink()
        manifest_path.write_bytes(manifest_bytes)
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        self._validate_snapshot(staging, str(snapshot_id), digest)
        return digest

    def _activate_review_snapshot(
        self,
        operation_id: OperationId,
        snapshot_id: SnapshotId,
        previous_snapshot_id: SnapshotId,
        manifest_sha256: str,
        completed_at: str,
        *,
        activation_kind: Literal[
            "data_review_decision", "rule_version", "historical", "manual_context_revision"
        ] = ("data_review_decision"),
        replaced_context_logical_id: str | None = None,
    ) -> None:
        staging = self._root / "staging" / str(operation_id)
        snapshot = self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id)
        _fsync_snapshot(staging)
        _publication_fault_point(self._root, "snapshot.before_move/v1")
        staging.replace(snapshot)
        _fsync_directory(snapshot.parent)
        _publication_fault_point(self._root, "snapshot.after_move/v1")
        self._metadata.execute(
            "INSERT INTO dataset_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(snapshot_id),
                _SNAPSHOT_SCHEMA_VERSION,
                manifest_sha256,
                str(operation_id),
                str(previous_snapshot_id),
                completed_at,
            ),
        )
        self._insert_snapshot_contract_binding(snapshot_id)
        self._metadata.execute(
            "INSERT INTO snapshot_activations VALUES (?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                str(operation_id),
                str(snapshot_id),
                str(previous_snapshot_id),
                activation_kind,
                completed_at,
            ),
        )
        self._metadata.execute(
            "UPDATE active_snapshot SET snapshot_id = ? WHERE singleton = 1", (str(snapshot_id),)
        )
        self._metadata.execute(
            "INSERT INTO activity_derivation_snapshot_bindings "
            "SELECT ?, version_id FROM activity_derivation_snapshot_bindings WHERE snapshot_id = ?",
            (str(snapshot_id), str(previous_snapshot_id)),
        )
        if replaced_context_logical_id is None:
            self._metadata.execute(
                "INSERT INTO manual_context_snapshot_bindings "
                "SELECT ?, revision_id FROM manual_context_snapshot_bindings WHERE snapshot_id = ?",
                (str(snapshot_id), str(previous_snapshot_id)),
            )
        else:
            self._metadata.execute(
                "INSERT INTO manual_context_snapshot_bindings "
                "SELECT ?, revision_id FROM manual_context_snapshot_bindings "
                "WHERE snapshot_id = ? AND revision_id NOT IN "
                "(SELECT revision_id FROM manual_context_revisions WHERE logical_id = ?)",
                (str(snapshot_id), str(previous_snapshot_id), replaced_context_logical_id),
            )
        self._metadata.execute(
            "INSERT INTO medication_snapshot_bindings "
            "SELECT ?, revision_id FROM medication_snapshot_bindings WHERE snapshot_id = ?",
            (str(snapshot_id), str(previous_snapshot_id)),
        )
        self._metadata.execute(
            "INSERT INTO medication_deviation_snapshot_bindings "
            "SELECT ?, revision_id FROM medication_deviation_snapshot_bindings "
            "WHERE snapshot_id = ?",
            (str(snapshot_id), str(previous_snapshot_id)),
        )
        self._metadata.execute(
            "INSERT INTO intake_reason_category_snapshot_bindings "
            "SELECT ?, revision_id FROM intake_reason_category_snapshot_bindings "
            "WHERE snapshot_id = ?",
            (str(snapshot_id), str(previous_snapshot_id)),
        )
        self._metadata.execute(
            "INSERT INTO as_needed_intake_snapshot_bindings "
            "SELECT ?, revision_id FROM as_needed_intake_snapshot_bindings "
            "WHERE snapshot_id = ?",
            (str(snapshot_id), str(previous_snapshot_id)),
        )
        _publication_fault_point(self._root, "snapshot.before_sqlite_commit/v1")

    def _refresh_v03_derivations(
        self,
        snapshot_id: SnapshotId,
        snapshot_as_of: datetime,
        manual_revision_ids: tuple[str, ...],
        metadata: sqlite3.Connection | None = None,
    ) -> None:
        source = self._metadata if metadata is None else metadata
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE weight_nutrition_days AS
            WITH contributors AS (
                SELECT v.measurement_local_date AS day, v.canonical_type AS feature_kind,
                       v.canonical_unit, r.effective_value, v.source_start_utc
                FROM resolved_measurements r JOIN measurement_versions v
                  ON v.measurement_version_id = r.selected_measurement_version_id
                WHERE r.disposition IN ('included_source', 'included_correction')
                  AND (v.canonical_type = 'body_mass' OR v.canonical_type LIKE 'dietary_%')
            )
            SELECT sha256('weight-nutrition-day/v1:' || day || ':' || feature_kind)
                       AS derived_record_id,
                   day, feature_kind, canonical_unit,
                   CASE WHEN feature_kind = 'body_mass'
                        THEN arg_max(effective_value, source_start_utc)
                        ELSE sum(effective_value) END AS effective_value,
                   'observed' AS observation_status, 'reviewed' AS quality_status
            FROM contributors GROUP BY day, feature_kind, canonical_unit
            """
        )
        context_revision_ids = tuple(
            revision_id
            for revision_id in manual_revision_ids
            if source.execute(
                "SELECT 1 FROM manual_context_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
        )
        medication_revision_ids = tuple(
            revision_id
            for revision_id in manual_revision_ids
            if source.execute(
                "SELECT 1 FROM medication_regime_revisions WHERE revision_id = ? UNION ALL "
                "SELECT 1 FROM medication_deviation_revisions WHERE revision_id = ? UNION ALL "
                "SELECT 1 FROM intake_reason_category_revisions WHERE revision_id = ? UNION ALL "
                "SELECT 1 FROM as_needed_intake_revisions WHERE revision_id = ?",
                (revision_id,) * 4,
            ).fetchone()
        )
        self._refresh_context_derivations(snapshot_as_of, context_revision_ids, source)
        self._refresh_medication_derivations(snapshot_as_of, medication_revision_ids, source)
        self._refresh_sleep_derivations()
        self._refresh_activity_derivations(source)
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE workout_features AS
            SELECT sha256('workout-feature/v1:' || r.logical_workout_id) AS derived_record_id,
                   r.logical_workout_id, w.measurement_local_date AS day,
                   w.original_activity_type AS activity_type,
                   r.effective_duration_minutes AS duration_minutes,
                   r.distance_kilometers, r.active_energy_kilocalories,
                   'reviewed' AS quality_status
            FROM resolved_workouts r JOIN workouts w
              ON w.workout_version_id = r.selected_workout_version_id
            WHERE r.disposition LIKE 'included%'
            """
        )
        derived_at = snapshot_as_of.astimezone(UTC).isoformat()
        for filename in _V6_DERIVATION_FILES:
            table = filename.removesuffix(".parquet")
            contract = _SNAPSHOT_FILE_CONTRACTS[filename][0]
            self._query.execute(
                f"ALTER TABLE {table} ADD COLUMN derivation_contract_id VARCHAR; "
                f"ALTER TABLE {table} ADD COLUMN snapshot_id VARCHAR; "
                f"ALTER TABLE {table} ADD COLUMN derived_at_utc VARCHAR; "
                f"UPDATE {table} SET derivation_contract_id = ?, snapshot_id = ?, "
                "derived_at_utc = ?",
                (contract, str(snapshot_id), derived_at),
            )

    def _refresh_context_derivations(
        self,
        snapshot_as_of: datetime,
        revision_ids: tuple[str, ...],
        metadata: sqlite3.Connection,
    ) -> None:
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE daily_context (
                derived_record_id VARCHAR, day DATE, illness_severity VARCHAR,
                stress_level VARCHAR, quality_status VARCHAR
            );
            CREATE OR REPLACE TEMP TABLE daily_context_contributors (
                derived_record_id VARCHAR, source_logical_id VARCHAR, source_version_id VARCHAR
            );
            """
        )
        if not revision_ids:
            return
        placeholders = ",".join("?" for _ in revision_ids)
        rows = metadata.execute(
            "SELECT revision.logical_id, revision.revision_id, revision.object_kind, "
            "revision.state, COALESCE(period.start_date, stress.day, custom.start_date), "
            "COALESCE(period.end_date, custom.end_date), period.severity, stress.level, "
            "COALESCE(period.category_logical_id, custom.label_logical_id) "
            "FROM manual_context_revisions revision "
            "LEFT JOIN illness_period_values period USING (revision_id) "
            "LEFT JOIN daily_stress_values stress USING (revision_id) "
            "LEFT JOIN custom_context_period_values custom USING (revision_id) "
            f"WHERE revision.revision_id IN ({placeholders}) ORDER BY revision.rowid",
            revision_ids,
        ).fetchall()
        coverage = metadata.execute(
            "SELECT revision.logical_id, revision.revision_id, value.start_date "
            "FROM manual_context_revisions revision "
            "JOIN context_coverage_start_values value USING (revision_id) "
            f"WHERE revision.revision_id IN ({placeholders}) AND revision.state = 'active'",
            revision_ids,
        ).fetchone()
        if coverage is None and not any(
            row[2] in {"illness_period", "daily_stress", "custom_context_period"}
            and row[3] == "active"
            for row in rows
        ):
            return
        as_of = snapshot_as_of.astimezone(ZoneInfo("Europe/Berlin")).date()
        severity_order = {"mild": 0, "moderate": 1, "severe": 2}
        derived_rows = []
        lineage_rows = []
        if coverage is not None:
            start = date.fromisoformat(str(coverage[2]))
            days = tuple(
                start + timedelta(days=offset) for offset in range((as_of - start).days + 1)
            )
        else:
            days = tuple(
                sorted(
                    {
                        current
                        for row in rows
                        if row[3] == "active" and row[4] is not None
                        for start in (date.fromisoformat(str(row[4])),)
                        for end in (
                            min(
                                as_of,
                                as_of if row[5] is None else date.fromisoformat(str(row[5])),
                            ),
                        )
                        for current in (
                            start + timedelta(days=offset)
                            for offset in range((end - start).days + 1)
                        )
                    }
                )
            )
        for current in days:
            active_periods = tuple(
                row
                for row in rows
                if row[2] == "illness_period"
                and row[3] == "active"
                and row[4] is not None
                and date.fromisoformat(str(row[4])) <= current
                and (row[5] is None or current <= date.fromisoformat(str(row[5])))
            )
            stress = next(
                (
                    row
                    for row in rows
                    if row[2] == "daily_stress"
                    and row[3] == "active"
                    and row[4] is not None
                    and date.fromisoformat(str(row[4])) == current
                ),
                None,
            )
            severity = (
                max((str(row[6]) for row in active_periods), key=severity_order.__getitem__)
                if active_periods
                else None
            )
            stress_level = str(stress[7]) if stress is not None else "average"
            derived_id = hashlib.sha256(f"daily-context/v1:{current}".encode()).hexdigest()
            derived_rows.append((derived_id, current, severity, stress_level, "reviewed"))
            contributors = list(active_periods)
            if stress is not None:
                contributors.append(stress)
            contributors.extend(
                row
                for row in rows
                if row[2] == "custom_context_period"
                and row[3] == "active"
                and row[4] is not None
                and date.fromisoformat(str(row[4])) <= current
                and (row[5] is None or current <= date.fromisoformat(str(row[5])))
            )
            if coverage is not None:
                lineage_rows.append((derived_id, str(coverage[0]), str(coverage[1])))
            lineage_rows.extend((derived_id, str(row[0]), str(row[1])) for row in contributors)
        self._query.executemany("INSERT INTO daily_context VALUES (?, ?, ?, ?, ?)", derived_rows)
        if lineage_rows:
            self._query.executemany(
                "INSERT INTO daily_context_contributors VALUES (?, ?, ?)", lineage_rows
            )

    def _refresh_medication_derivations(
        self,
        snapshot_as_of: datetime,
        revision_ids: tuple[str, ...],
        metadata: sqlite3.Connection,
    ) -> None:
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE medication_context (
                derived_record_id VARCHAR, day DATE, scheduled_dose_count BIGINT,
                deviation_count BIGINT, as_needed_intake_count BIGINT,
                quality_status VARCHAR
            );
            CREATE OR REPLACE TEMP TABLE medication_context_contributors (
                derived_record_id VARCHAR, source_logical_id VARCHAR, source_version_id VARCHAR
            );
            """
        )
        if not revision_ids:
            return
        placeholders = ",".join("?" for _ in revision_ids)
        regimes = metadata.execute(
            "SELECT revision.logical_id, revision.revision_id, value.starts_at, value.timezone "
            "FROM medication_regime_revisions revision "
            "JOIN medication_regime_values value USING (revision_id) "
            f"WHERE revision.revision_id IN ({placeholders}) "
            "ORDER BY value.starts_at, revision.rowid",
            revision_ids,
        ).fetchall()
        deviations = metadata.execute(
            "SELECT revision.logical_id, revision.revision_id, value.regime_logical_id, "
            "value.scheduled_at FROM medication_deviation_revisions revision "
            "JOIN medication_deviation_values value USING (revision_id) "
            f"WHERE revision.revision_id IN ({placeholders}) AND revision.state = 'active'",
            revision_ids,
        ).fetchall()
        as_needed = metadata.execute(
            "SELECT revision.logical_id, revision.revision_id, value.taken_at "
            "FROM as_needed_intake_revisions revision "
            "JOIN as_needed_intake_values value USING (revision_id) "
            f"WHERE revision.revision_id IN ({placeholders}) AND revision.state = 'active'",
            revision_ids,
        ).fetchall()
        categories = metadata.execute(
            "SELECT logical_id, revision_id FROM intake_reason_category_revisions "
            f"WHERE revision_id IN ({placeholders}) AND state = 'active'",
            revision_ids,
        ).fetchall()
        if not regimes and not deviations and not as_needed and not categories:
            return
        timezone_name = str(regimes[0][3]) if regimes else "Europe/Berlin"
        as_of_day = snapshot_as_of.astimezone(ZoneInfo(timezone_name)).date()
        start = (
            min(datetime.fromisoformat(str(row[2])).date() for row in regimes)
            if regimes
            else as_of_day
        )

        def scheduled_at(day: date, local_time: time, zone_name: str) -> datetime:
            zone = ZoneInfo(zone_name)
            local = datetime.combine(day, local_time)
            for minute in range(181):
                shifted = local + timedelta(minutes=minute)
                candidate = shifted.replace(tzinfo=zone, fold=0)
                if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == shifted:
                    return candidate
            raise StoreError("Lokale Dosiszeit konnte nicht aufgelöst werden.")

        derived_rows = []
        lineage_rows = []
        parsed_regimes = tuple(
            (str(row[0]), str(row[1]), datetime.fromisoformat(str(row[2])), str(row[3]))
            for row in regimes
        )
        for offset in range((as_of_day - start).days + 1):
            current = start + timedelta(days=offset)
            regime = next(
                (row for row in reversed(parsed_regimes) if row[2].date() <= current), None
            )
            next_regime = (
                None
                if regime is None
                else next((row for row in parsed_regimes if row[2] > regime[2]), None)
            )
            scheduled_count = 0
            if regime is not None:
                for local_time, weekdays in metadata.execute(
                    "SELECT local_time, weekdays FROM medication_scheduled_doses "
                    "WHERE revision_id = ?",
                    (regime[1],),
                ).fetchall():
                    if current.strftime("%A").lower() not in json.loads(str(weekdays)):
                        continue
                    occurrence = scheduled_at(
                        current, time.fromisoformat(str(local_time)), regime[3]
                    )
                    if (
                        occurrence >= regime[2]
                        and occurrence <= snapshot_as_of
                        and (next_regime is None or occurrence < next_regime[2])
                    ):
                        scheduled_count += 1
            day_deviations = tuple(
                row for row in deviations if datetime.fromisoformat(str(row[3])).date() == current
            )
            day_as_needed = tuple(
                row for row in as_needed if datetime.fromisoformat(str(row[2])).date() == current
            )
            derived_id = hashlib.sha256(f"medication-context/v1:{current}".encode()).hexdigest()
            derived_rows.append(
                (
                    derived_id,
                    current,
                    scheduled_count,
                    len(day_deviations),
                    len(day_as_needed),
                    "reviewed",
                )
            )
            if regime is not None:
                lineage_rows.append((derived_id, regime[0], regime[1]))
            lineage_rows.extend((derived_id, str(row[0]), str(row[1])) for row in day_deviations)
            lineage_rows.extend((derived_id, str(row[0]), str(row[1])) for row in day_as_needed)
            if regime is None and not day_deviations and not day_as_needed:
                lineage_rows.extend((derived_id, str(row[0]), str(row[1])) for row in categories)
        self._query.executemany(
            "INSERT INTO medication_context VALUES (?, ?, ?, ?, ?, ?)", derived_rows
        )
        if lineage_rows:
            self._query.executemany(
                "INSERT INTO medication_context_contributors VALUES (?, ?, ?)", lineage_rows
            )

    def _refresh_sleep_derivations(self) -> None:
        rows = self._query.execute(
            """
            SELECT measurement_version_id, identity_candidate_id, canonical_category,
                   source_start_utc, source_end_utc, source_start_offset_minutes,
                   source_end_offset_minutes
            FROM sleep_intervals
            WHERE is_selected AND source_end_utc::TIMESTAMPTZ > source_start_utc::TIMESTAMPTZ
              AND source_name = 'Apple Watch' AND device = 'Apple Watch'
            ORDER BY source_start_utc, source_end_utc, measurement_version_id
            """
        ).fetchall()

        def value_datetime(value: object, offset: object) -> datetime:
            return datetime.fromisoformat(str(value)).astimezone(
                timezone(timedelta(minutes=int(str(offset))))
            )

        intervals = tuple(
            (
                str(row[0]),
                str(row[1]),
                str(row[2]),
                value_datetime(row[3], row[5]),
                value_datetime(row[4], row[6]),
            )
            for row in rows
        )
        groups: list[list[tuple[str, str, str, datetime, datetime]]] = []
        for interval in intervals:
            if interval[2] == "in_bed":
                continue
            if not groups or interval[3] - max(item[4] for item in groups[-1]) > timedelta(
                minutes=90
            ):
                groups.append([interval])
            else:
                groups[-1].append(interval)

        asleep_categories = {"asleep_unspecified", "asleep_core", "asleep_deep", "asleep_rem"}
        episode_rows: list[tuple[str, str, str, float, str]] = []
        contributor_rows: list[tuple[str, str, str]] = []
        episodes_by_day: dict[date, list[tuple[str, float]]] = {}
        for group in groups:
            start = min(item[3] for item in group)
            end = max(item[4] for item in group)
            boundaries = sorted({value for item in group for value in (item[3], item[4])})
            observed = timedelta()
            for left, right in pairwise(boundaries):
                active = {item[2] for item in group if item[3] <= left and item[4] >= right}
                asleep = active & asleep_categories
                if asleep and "awake" not in active:
                    observed += right - left
            version_ids = tuple(sorted(item[0] for item in group))
            derived_id = hashlib.sha256(
                f"sleep-episode/v1:{':'.join(version_ids)}".encode()
            ).hexdigest()
            observed_minutes = observed.total_seconds() / 60
            episode_rows.append(
                (
                    derived_id,
                    start.astimezone(UTC).isoformat(),
                    end.astimezone(UTC).isoformat(),
                    observed_minutes,
                    "reviewed",
                )
            )
            contributors = tuple(group) + tuple(
                item
                for item in intervals
                if item[2] == "in_bed" and item[3] < end and item[4] > start
            )
            contributor_rows.extend((derived_id, item[1], item[0]) for item in contributors)
            episodes_by_day.setdefault(end.date(), []).append((derived_id, observed_minutes))
        night_rows: list[tuple[str, date, str, str | None, str]] = []
        night_contributors: list[tuple[str, str, str]] = []
        for day, episodes in sorted(episodes_by_day.items()):
            largest = max(value for _, value in episodes)
            primary = tuple(derived_id for derived_id, value in episodes if value == largest)
            night_id = hashlib.sha256(f"sleep-night/v1:{day}".encode()).hexdigest()
            night_rows.append(
                (
                    night_id,
                    day,
                    "observed" if len(primary) == 1 else "partial",
                    primary[0] if len(primary) == 1 else None,
                    "reviewed" if len(primary) == 1 else "provisional",
                )
            )
            episode_ids = {derived_id for derived_id, _ in episodes}
            night_contributors.extend(
                (night_id, logical_id, version_id)
                for episode_id, logical_id, version_id in contributor_rows
                if episode_id in episode_ids
            )
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE sleep_episodes (
                derived_record_id VARCHAR, episode_start_utc VARCHAR, episode_end_utc VARCHAR,
                observed_sleep_minutes DOUBLE, quality_status VARCHAR
            );
            CREATE OR REPLACE TEMP TABLE sleep_nights (
                derived_record_id VARCHAR, day DATE, observation_status VARCHAR,
                primary_episode_id VARCHAR, quality_status VARCHAR
            );
            CREATE OR REPLACE TEMP TABLE sleep_episode_contributors (
                derived_record_id VARCHAR, source_logical_id VARCHAR, source_version_id VARCHAR
            );
            CREATE OR REPLACE TEMP TABLE sleep_night_contributors (
                derived_record_id VARCHAR, source_logical_id VARCHAR, source_version_id VARCHAR
            );
            """
        )
        if episode_rows:
            self._query.executemany(
                "INSERT INTO sleep_episodes VALUES (?, ?, ?, ?, ?)", episode_rows
            )
            self._query.executemany(
                "INSERT INTO sleep_episode_contributors VALUES (?, ?, ?)", contributor_rows
            )
        if night_rows:
            self._query.executemany("INSERT INTO sleep_nights VALUES (?, ?, ?, ?, ?)", night_rows)
            self._query.executemany(
                "INSERT INTO sleep_night_contributors VALUES (?, ?, ?)", night_contributors
            )

    def _refresh_activity_derivations(self, metadata: sqlite3.Connection) -> None:
        rows = self._query.execute(
            """
            SELECT v.measurement_version_id, v.measurement_local_date, v.canonical_type,
                   v.canonical_unit, r.effective_value, v.source_start_utc, v.source_end_utc,
                   v.source_start_offset_minutes, v.source_end_offset_minutes,
                   v.source_name, v.device,
                   EXISTS (SELECT 1 FROM open_review_cases c
                           WHERE c.logical_measurement_id = r.logical_measurement_id
                              OR c.measurement_version_id = v.measurement_version_id)
            FROM resolved_measurements r JOIN measurement_versions v
              ON v.measurement_version_id = r.selected_measurement_version_id
            WHERE r.disposition IN ('included_source', 'included_correction')
              AND v.canonical_type IN ('apple_exercise_time', 'step_count',
                                       'walking_running_distance', 'active_energy')
            ORDER BY v.source_start_utc, v.measurement_version_id
            """
        ).fetchall()

        def local_datetime(value: object, offset: object) -> datetime:
            return datetime.fromisoformat(str(value)).astimezone(
                timezone(timedelta(minutes=int(str(offset))))
            )

        measurements = tuple(
            (
                str(row[0]),
                cast(date, row[1]),
                str(row[2]),
                str(row[3]),
                float(row[4]),
                local_datetime(row[5], row[7]),
                local_datetime(row[6], row[8]),
                "watch"
                if (str(row[9]), str(row[10])) == ("Apple Watch", "Apple Watch")
                else "iphone"
                if (str(row[9]), str(row[10])) == ("iPhone", "iPhone")
                else "other",
                bool(row[11]),
            )
            for row in rows
        )

        def union(
            intervals: tuple[tuple[datetime, datetime], ...],
        ) -> tuple[tuple[datetime, datetime], ...]:
            merged: list[tuple[datetime, datetime]] = []
            for start, end in sorted(intervals):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            return tuple(merged)

        metadata_tables = {
            str(row[0])
            for row in metadata.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        version = (
            metadata.execute(
                "SELECT coverage_gap_minutes FROM activity_derivation_active "
                "JOIN activity_derivation_versions USING (version_id) WHERE singleton = 1"
            ).fetchone()
            if "activity_derivation_active" in metadata_tables
            else metadata.execute(
                "SELECT coverage_gap_minutes FROM snapshot_origin origin "
                "JOIN activity_derivation_versions version "
                "ON version.version_id = origin.activity_derivation_version_id"
            ).fetchone()
            if "snapshot_origin" in metadata_tables
            else None
        )
        gap_minutes = 240 if version is None else int(version[0])

        def bridge(
            intervals: tuple[tuple[datetime, datetime], ...],
        ) -> tuple[tuple[datetime, datetime], ...]:
            merged: list[tuple[datetime, datetime]] = []
            for start, end in intervals:
                if (
                    merged
                    and start - merged[-1][1] < timedelta(minutes=gap_minutes)
                    and merged[-1][1].utcoffset() == start.utcoffset()
                ):
                    merged[-1] = (merged[-1][0], end)
                else:
                    merged.append((start, end))
            return tuple(merged)

        interval_measurements = tuple(item for item in measurements if item[6] > item[5])
        watch_intervals = [
            (item[5], item[6]) for item in interval_measurements if item[7] == "watch"
        ]
        for table in ("workouts", "sleep_intervals"):
            watch_intervals.extend(
                (
                    local_datetime(row[0], row[2]),
                    local_datetime(row[1], row[3]),
                )
                for row in self._query.execute(
                    f"SELECT source_start_utc, source_end_utc, source_start_offset_minutes, "
                    f"source_end_offset_minutes FROM {table} WHERE is_selected "
                    "AND source_end_utc::TIMESTAMPTZ > source_start_utc::TIMESTAMPTZ "
                    "AND source_name = 'Apple Watch' AND device = 'Apple Watch'"
                ).fetchall()
            )
        watch_coverage = bridge(union(tuple(watch_intervals)))
        watch_gaps = tuple(
            (left[1], right[0]) for left, right in pairwise(watch_coverage) if left[1] < right[0]
        )
        iphone_ids = {
            item[0]
            for item in interval_measurements
            if item[7] == "iphone"
            and any(start <= item[5] and item[6] <= end for start, end in watch_gaps)
        }
        iphone_coverage = tuple(
            interval
            for gap_start, gap_end in watch_gaps
            for interval in bridge(
                union(
                    tuple(
                        (item[5], item[6])
                        for item in interval_measurements
                        if item[0] in iphone_ids and gap_start <= item[5] and item[6] <= gap_end
                    )
                )
            )
        )
        eligible = tuple(
            item for item in measurements if item[7] == "watch" or item[0] in iphone_ids
        )
        day_rows = []
        for day, metric, unit in sorted({(item[1], item[2], item[3]) for item in eligible}):
            contributors = tuple(
                item for item in eligible if (item[1], item[2], item[3]) == (day, metric, unit)
            )
            watch = tuple(item[4] for item in contributors if item[7] == "watch")
            iphone = tuple(item[4] for item in contributors if item[7] == "iphone")
            day_rows.append(
                (
                    hashlib.sha256(f"activity-day/v1:{day}:{metric}".encode()).hexdigest(),
                    day,
                    metric,
                    unit,
                    sum(item[4] for item in contributors),
                    sum(watch) if watch else None,
                    sum(iphone) if iphone else None,
                    "provisional" if any(item[8] for item in contributors) else "reviewed",
                )
            )
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE activity_days (
                derived_record_id VARCHAR, day DATE, metric VARCHAR, canonical_unit VARCHAR,
                effective_value DOUBLE, watch_value DOUBLE, iphone_value DOUBLE,
                quality_status VARCHAR
            )
            """
        )
        if day_rows:
            self._query.executemany(
                "INSERT INTO activity_days VALUES (?, ?, ?, ?, ?, ?, ?, ?)", day_rows
            )

        segments: list[tuple[str, str, str, str]] = []
        coverage_candidates = tuple(
            item for item in interval_measurements if item[7] in {"watch", "iphone"}
        )
        if coverage_candidates:
            first = min(coverage_candidates, key=lambda item: item[1])
            last = max(coverage_candidates, key=lambda item: item[1])
            range_start = datetime.combine(first[1], time.min, first[5].tzinfo)
            range_end = datetime.combine(last[1] + timedelta(days=1), time.min, last[5].tzinfo)
            covered = tuple((start, end, "watch") for start, end in watch_coverage) + tuple(
                (start, end, "iphone_fallback") for start, end in iphone_coverage
            )
            boundaries = sorted(
                {
                    range_start,
                    range_end,
                    *(
                        value
                        for start, end, _ in covered
                        for value in (max(start, range_start), min(end, range_end))
                    ),
                }
            )
            for start, end in pairwise(boundaries):
                if start == end:
                    continue
                kind = next(
                    (kind for left, right, kind in covered if left <= start and end <= right),
                    "unobserved",
                )
                current = start
                while current < end:
                    midnight = datetime.combine(
                        current.date() + timedelta(days=1), time.min, current.tzinfo
                    )
                    current_end = min(end, midnight)
                    key = (
                        f"activity-coverage/v1:{current.isoformat()}:"
                        f"{current_end.isoformat()}:{kind}"
                    )
                    segments.append(
                        (
                            hashlib.sha256(key.encode()).hexdigest(),
                            current.astimezone(UTC).isoformat(),
                            current_end.astimezone(UTC).isoformat(),
                            kind,
                        )
                    )
                    current = current_end
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE activity_coverage_segments (
                derived_record_id VARCHAR, start_utc VARCHAR, end_utc VARCHAR,
                coverage_kind VARCHAR
            )
            """
        )
        if segments:
            self._query.executemany(
                "INSERT INTO activity_coverage_segments VALUES (?, ?, ?, ?)", segments
            )

    def _refresh_derivation_lineage(
        self,
        snapshot_id: SnapshotId,
        derived_at: datetime,
        manual_revision_ids: tuple[str, ...],
    ) -> None:
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE derivation_lineage AS
            SELECT
                sha256('resolved-measurement/v1:' || r.logical_measurement_id) AS derived_record_id,
                'resolved_measurement' AS derived_family,
                'resolved-measurement/v1' AS derivation_contract_id,
                v.identity_candidate_id AS source_logical_id,
                r.selected_measurement_version_id AS source_version_id,
                'selected_source_version' AS contribution_role,
                ? AS snapshot_id,
                ? AS derived_at_utc
            FROM resolved_measurements r
            JOIN measurement_versions v
              ON v.measurement_version_id = r.selected_measurement_version_id
            UNION ALL
            SELECT
                sha256('resolved-workout/v1:' || r.logical_workout_id) AS derived_record_id,
                'resolved_workout' AS derived_family,
                'resolved-workout/v1' AS derivation_contract_id,
                r.logical_workout_id AS source_logical_id,
                r.selected_workout_version_id AS source_version_id,
                'selected_source_version' AS contribution_role,
                ? AS snapshot_id,
                ? AS derived_at_utc
            FROM resolved_workouts r
            JOIN workouts w ON w.workout_version_id = r.selected_workout_version_id
            """,
            (
                str(snapshot_id),
                derived_at.astimezone(UTC).isoformat(),
                str(snapshot_id),
                derived_at.astimezone(UTC).isoformat(),
            ),
        )

        snapshot_ref = str(snapshot_id)
        derived_ref = derived_at.astimezone(UTC).isoformat()
        self._query.execute(
            f"""
            INSERT INTO derivation_lineage
            SELECT d.derived_record_id, 'weight_nutrition_day', 'weight-nutrition-day/v1',
                   v.identity_candidate_id, v.measurement_version_id,
                   'daily_feature_contributor', '{snapshot_ref}', '{derived_ref}'
            FROM weight_nutrition_days d JOIN measurement_versions v
              ON v.measurement_local_date = d.day AND v.canonical_type = d.feature_kind
            JOIN resolved_measurements r
              ON r.selected_measurement_version_id = v.measurement_version_id
            WHERE r.disposition IN ('included_source', 'included_correction');

            INSERT INTO derivation_lineage
            SELECT e.derived_record_id, 'sleep_episode', 'sleep-episode/v1',
                   s.source_logical_id, s.source_version_id,
                   'episode_interval', '{snapshot_ref}', '{derived_ref}'
            FROM sleep_episodes e JOIN sleep_episode_contributors s
              USING (derived_record_id)
            UNION ALL
            SELECT n.derived_record_id, 'sleep_night', 'sleep-night/v1',
                   s.source_logical_id, s.source_version_id,
                   'night_interval', '{snapshot_ref}', '{derived_ref}'
            FROM sleep_nights n JOIN sleep_night_contributors s
              USING (derived_record_id);

            INSERT INTO derivation_lineage
            SELECT d.derived_record_id, 'activity_day', 'activity-day/v1',
                   v.identity_candidate_id, v.measurement_version_id,
                   'daily_metric_contributor', '{snapshot_ref}', '{derived_ref}'
            FROM activity_days d JOIN measurement_versions v
              ON v.measurement_local_date = d.day AND v.canonical_type = d.metric
            JOIN resolved_measurements r
              ON r.selected_measurement_version_id = v.measurement_version_id
            WHERE r.disposition IN ('included_source', 'included_correction')
              AND ((v.source_name = 'Apple Watch' AND v.device = 'Apple Watch')
                   OR ((v.source_name = 'iPhone' AND v.device = 'iPhone')
                       AND EXISTS (
                           SELECT 1 FROM activity_coverage_segments c
                           WHERE c.coverage_kind = 'iphone_fallback'
                             AND c.start_utc::TIMESTAMPTZ <= v.source_start_utc::TIMESTAMPTZ
                             AND v.source_end_utc::TIMESTAMPTZ <= c.end_utc::TIMESTAMPTZ
                       )))
            UNION ALL
            SELECT c.derived_record_id, 'activity_coverage', 'activity-coverage/v1',
                   v.identity_candidate_id, v.measurement_version_id,
                   'coverage_interval', '{snapshot_ref}', '{derived_ref}'
            FROM activity_coverage_segments c JOIN measurement_versions v
              ON (c.coverage_kind = 'unobserved'
                  OR (v.source_start_utc::TIMESTAMPTZ < c.end_utc::TIMESTAMPTZ
                      AND c.start_utc::TIMESTAMPTZ < v.source_end_utc::TIMESTAMPTZ))
            JOIN resolved_measurements r
              ON r.selected_measurement_version_id = v.measurement_version_id
            WHERE r.disposition IN ('included_source', 'included_correction')
              AND v.canonical_type IN ('apple_exercise_time', 'step_count',
                                       'walking_running_distance', 'active_energy')
              AND ((v.source_name = 'Apple Watch' AND v.device = 'Apple Watch')
                   OR (v.source_name = 'iPhone' AND v.device = 'iPhone'));

            INSERT INTO derivation_lineage
            SELECT f.derived_record_id, 'workout_feature', 'workout-feature/v1',
                   w.logical_workout_id, w.workout_version_id,
                   'workout_source_version', '{snapshot_ref}', '{derived_ref}'
            FROM workout_features f JOIN workouts w USING (logical_workout_id)
            JOIN resolved_workouts r
              ON r.selected_workout_version_id = w.workout_version_id;
            """
        )

        self._query.execute(
            f"""
            INSERT INTO derivation_lineage
            SELECT derived_record_id, 'daily_context', 'daily-context/v1',
                   source_logical_id, source_version_id, 'manual_revision',
                   '{snapshot_ref}', '{derived_ref}'
            FROM daily_context_contributors
            UNION ALL
            SELECT derived_record_id, 'medication_context', 'medication-context/v1',
                   source_logical_id, source_version_id, 'manual_revision',
                   '{snapshot_ref}', '{derived_ref}'
            FROM medication_context_contributors
            """
        )

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

    def load_weight_nutrition_measurements(
        self,
        snapshot_id: SnapshotId | None,
        start_date: date | None,
        end_date: date | None,
    ) -> tuple[SnapshotId | None, tuple[StoredMeasurement, ...]]:
        return self._load_measurements(
            snapshot_id,
            start_date,
            end_date,
            "(versions.canonical_type = 'body_mass' OR versions.canonical_type LIKE 'dietary_%')",
        )

    def load_activity_measurements(
        self,
        snapshot_id: SnapshotId | None,
        start_date: date | None,
        end_date: date | None,
    ) -> tuple[SnapshotId | None, tuple[StoredMeasurement, ...]]:
        return self._load_measurements(
            snapshot_id,
            start_date,
            end_date,
            "versions.canonical_type IN ('apple_exercise_time', 'step_count', "
            "'walking_running_distance', 'active_energy')",
        )

    def _load_measurements(
        self,
        snapshot_id: SnapshotId | None,
        start_date: date | None,
        end_date: date | None,
        type_clause: str,
    ) -> tuple[SnapshotId | None, tuple[StoredMeasurement, ...]]:
        self._require_open()
        selected_snapshot = snapshot_id or self.load_active_snapshot_id()
        if selected_snapshot is None:
            return None, ()
        if (
            self._metadata.execute(
                "SELECT 1 FROM dataset_snapshots WHERE snapshot_id = ?", (str(selected_snapshot),)
            ).fetchone()
            is None
        ):
            raise StoreError("Datensatz-Snapshot ist unbekannt.")
        directory = self._root / _PARQUET_DIRECTORY / "snapshots" / str(selected_snapshot)
        versions = str(directory / "measurement_versions.parquet").replace("'", "''")
        resolved = str(directory / "resolved_measurements.parquet").replace("'", "''")
        reviews = str(directory / "open_review_cases.parquet").replace("'", "''")
        clauses = [type_clause]
        parameters: list[date] = []
        if start_date is not None:
            clauses.append("versions.measurement_local_date >= ?")
            parameters.append(start_date)
        if end_date is not None:
            clauses.append("versions.measurement_local_date <= ?")
            parameters.append(end_date)
        rows = self._query.execute(
            f"""
            SELECT versions.identity_candidate_id, versions.measurement_version_id,
                   versions.canonical_type, versions.canonical_unit,
                   versions.canonical_value, resolved.effective_value, resolved.disposition,
                   resolved.selected_measurement_version_id IS NOT NULL,
                   versions.source_start_utc, versions.source_end_utc,
                   versions.source_updated_at_utc, versions.source_start_offset_minutes,
                   versions.source_end_offset_minutes, versions.source_updated_at_offset_minutes,
                   versions.measurement_local_date, versions.source_name,
                   versions.source_version, versions.device, versions.original_value,
                   versions.original_unit,
                   coalesce(list(reviews.review_case_id ORDER BY reviews.review_case_id)
                            FILTER (WHERE reviews.review_case_id IS NOT NULL), [])
            FROM read_parquet('{versions}') AS versions
            LEFT JOIN read_parquet('{resolved}') AS resolved
              ON resolved.selected_measurement_version_id = versions.measurement_version_id
            LEFT JOIN read_parquet('{reviews}') AS reviews
              ON reviews.logical_measurement_id = versions.identity_candidate_id
            WHERE {" AND ".join(clauses)}
            GROUP BY ALL
            ORDER BY versions.measurement_local_date, versions.source_start_utc,
                     versions.measurement_version_id
            """,
            parameters,
        ).fetchall()

        def local_time(value: str, offset: int) -> datetime:
            return datetime.fromisoformat(value).astimezone(timezone(timedelta(minutes=offset)))

        return selected_snapshot, tuple(
            StoredMeasurement(
                logical_measurement_id=LogicalMeasurementId(str(row[0])),
                measurement_version_id=MeasurementVersionId(str(row[1])),
                data_type=CanonicalHealthType(str(row[2])),
                unit=CanonicalUnit(str(row[3])),
                value=float(row[4]),
                effective_value=None if row[5] is None else float(row[5]),
                disposition=(
                    None
                    if row[6] is None
                    else cast(
                        Literal[
                            "included_source",
                            "included_correction",
                            "excluded_local",
                            "excluded_source_deletion",
                        ],
                        row[6],
                    )
                ),
                is_selected=bool(row[7]),
                source_start=local_time(str(row[8]), int(row[11])),
                source_end=local_time(str(row[9]), int(row[12])),
                source_updated_at=local_time(str(row[10]), int(row[13])),
                measurement_local_day=row[14],
                source_name=str(row[15]),
                source_version=str(row[16]),
                device=str(row[17]),
                original_value=float(row[18]),
                original_unit=str(row[19]),
                review_case_ids=tuple(ReviewCaseId(str(item)) for item in row[20]),
            )
            for row in rows
        )

    def load_sleep_measurements(
        self, snapshot_id: SnapshotId | None
    ) -> tuple[SnapshotId | None, tuple[CanonicalSleepInterval, ...]]:
        self._require_open()
        selected_snapshot = snapshot_id or self.load_active_snapshot_id()
        if selected_snapshot is None:
            return None, ()
        if (
            self._metadata.execute(
                "SELECT 1 FROM dataset_snapshots WHERE snapshot_id = ?", (str(selected_snapshot),)
            ).fetchone()
            is None
        ):
            raise StoreError("Datensatz-Snapshot ist unbekannt.")
        path = self._root / _PARQUET_DIRECTORY / "snapshots" / str(selected_snapshot)
        sleep_path = path / "sleep_intervals.parquet"
        if not sleep_path.exists():
            return selected_snapshot, ()
        sleep = str(sleep_path).replace("'", "''")
        rows = self._query.execute(
            f"""
            SELECT identity_candidate_id, measurement_version_id, original_category,
                   canonical_category, source_start_utc, source_end_utc,
                   source_updated_at_utc, source_start_offset_minutes,
                   source_end_offset_minutes, source_updated_at_offset_minutes,
                   source_name, source_version, device, strong_source_id_hash
                   , is_selected
            FROM read_parquet('{sleep}')
            ORDER BY source_start_utc, measurement_version_id
            """
        ).fetchall()

        def local_time(value: str, offset: int) -> datetime:
            return datetime.fromisoformat(value).astimezone(timezone(timedelta(minutes=offset)))

        return selected_snapshot, tuple(
            CanonicalSleepInterval(
                logical_measurement_id=LogicalMeasurementId(str(row[0])),
                measurement_version_id=MeasurementVersionId(str(row[1])),
                original_category=str(row[2]),
                canonical_category=CanonicalSleepCategory(str(row[3])),
                source_start=local_time(str(row[4]), int(row[7])),
                source_end=local_time(str(row[5]), int(row[8])),
                source_updated_at=local_time(str(row[6]), int(row[9])),
                source_name=str(row[10]),
                source_version=str(row[11]),
                device=str(row[12]),
                strong_source_id_hash=None if row[13] is None else str(row[13]),
                is_selected=bool(row[14]),
            )
            for row in rows
        )

    def load_workouts(
        self, snapshot_id: SnapshotId | None, start_date: date | None, end_date: date | None
    ) -> tuple[SnapshotId | None, tuple[StoredWorkout, ...]]:
        self._require_open()
        selected_snapshot = snapshot_id or self.load_active_snapshot_id()
        if selected_snapshot is None:
            return None, ()
        path = (
            self._root
            / _PARQUET_DIRECTORY
            / "snapshots"
            / str(selected_snapshot)
            / "workouts.parquet"
        )
        if not path.exists():
            return selected_snapshot, ()
        clauses, parameters = [], []
        if start_date is not None:
            clauses.append("measurement_local_date >= ?")
            parameters.append(start_date)
        if end_date is not None:
            clauses.append("measurement_local_date <= ?")
            parameters.append(end_date)
        escaped = str(path).replace("'", "''")
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        rows = self._query.execute(
            f"SELECT * FROM read_parquet('{escaped}'){where} "
            "ORDER BY measurement_local_date, source_start_utc, workout_version_id",
            parameters,
        ).fetchall()
        resolved_path = path.parent / "resolved_workouts.parquet"
        resolved: dict[str, ResolvedWorkout] = {}
        if resolved_path.exists():
            escaped_resolved = str(resolved_path).replace("'", "''")
            resolved = {
                str(row[0]): ResolvedWorkout(*row)
                for row in self._query.execute(
                    f"SELECT * FROM read_parquet('{escaped_resolved}')"
                ).fetchall()
            }
        review_cases: dict[str, tuple[ReviewCaseId, ...]] = {}
        links_path = path.parent / "workout_review_links.parquet"
        if links_path.exists():
            escaped_links = str(links_path).replace("'", "''")
            review_path = path.parent / "open_review_cases.parquet"
            escaped_reviews = str(review_path).replace("'", "''")
            grouped: dict[str, list[ReviewCaseId]] = {}
            for case_id, version_id in self._query.execute(
                f"SELECT links.review_case_id, links.workout_version_id "
                f"FROM read_parquet('{escaped_links}') AS links JOIN "
                f"read_parquet('{escaped_reviews}') AS reviews USING (review_case_id)"
            ).fetchall():
                grouped.setdefault(str(version_id), []).append(ReviewCaseId(str(case_id)))
            review_cases = {
                version_id: tuple(sorted(case_ids, key=str))
                for version_id, case_ids in grouped.items()
            }

        def local_time(value: str, offset: int) -> datetime:
            return datetime.fromisoformat(value).astimezone(timezone(timedelta(minutes=offset)))

        return selected_snapshot, tuple(
            StoredWorkout(
                LogicalMeasurementId(str(row[1])),
                MeasurementVersionId(str(row[0])),
                str(row[2]),
                local_time(str(row[3]), int(row[6])),
                local_time(str(row[4]), int(row[7])),
                local_time(str(row[5]), int(row[8])),
                row[9],
                str(row[10]),
                str(row[11]),
                str(row[12]),
                None if row[13] is None else str(row[13]),
                None if row[14] is None else float(row[14]),
                None if row[15] is None else float(row[15]),
                None if row[16] is None else float(row[16]),
                (
                    resolved[str(row[1])].selected_workout_version_id == str(row[0])
                    and resolved[str(row[1])].disposition.startswith("included")
                    if str(row[1]) in resolved
                    else bool(row[17])
                ),
                (
                    resolved[str(row[1])].effective_duration_minutes
                    if str(row[1]) in resolved
                    and resolved[str(row[1])].selected_workout_version_id == str(row[0])
                    else None
                ),
                (
                    resolved[str(row[1])].disposition
                    if str(row[1]) in resolved
                    and resolved[str(row[1])].selected_workout_version_id == str(row[0])
                    else None
                ),
                review_cases.get(str(row[0]), ()),
            )
            for row in rows
        )

    def load_workout_review_case_versions(
        self, review_case_id: str
    ) -> tuple[MeasurementVersionId, ...]:
        self._require_open()
        snapshot = self.load_active_snapshot_id()
        if snapshot is None:
            return ()
        path = (
            self._root
            / _PARQUET_DIRECTORY
            / "snapshots"
            / str(snapshot)
            / "workout_review_links.parquet"
        )
        if not path.exists():
            return ()
        escaped = str(path).replace("'", "''")
        return tuple(
            MeasurementVersionId(str(row[0]))
            for row in self._query.execute(
                f"SELECT workout_version_id FROM read_parquet('{escaped}') "
                "WHERE review_case_id = ? ORDER BY workout_version_id",
                (review_case_id,),
            ).fetchall()
        )

    def load_workout_logical_id(self, workout_version_id: MeasurementVersionId) -> str | None:
        self._require_open()
        snapshot = self.load_active_snapshot_id()
        if snapshot is None:
            return None
        path = self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot) / "workouts.parquet"
        escaped = str(path).replace("'", "''")
        row = self._query.execute(
            f"SELECT logical_workout_id FROM read_parquet('{escaped}') "
            "WHERE workout_version_id = ?",
            (str(workout_version_id),),
        ).fetchone()
        return None if row is None else str(row[0])

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
                        "rule_definition",
                        "preferred_daily_weight_conflict",
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

    def load_review_cycles(self) -> tuple[ReviewCycleRecord, ...]:
        self._require_open()
        return tuple(
            ReviewCycleRecord(
                cycle_id=StoredReviewCycleId(str(cycle_id)),
                snapshot_id=SnapshotId(str(snapshot_id)),
                status=cast(Literal["open", "closed"], status),
                open_case_count=int(open_case_count),
                kind=cast(Literal["import", "rule_version", "historical"], kind),
                base_snapshot_id=(
                    None if base_snapshot_id is None else SnapshotId(str(base_snapshot_id))
                ),
                start_date=None if start_date is None else date.fromisoformat(str(start_date)),
                end_date=None if end_date is None else date.fromisoformat(str(end_date)),
                rule_version_id=None if rule_version_id is None else str(rule_version_id),
            )
            for (
                cycle_id,
                snapshot_id,
                kind,
                status,
                open_case_count,
                base_snapshot_id,
                start_date,
                end_date,
                rule_version_id,
            ) in self._metadata.execute(
                "SELECT c.cycle_id, c.snapshot_id, c.cycle_kind, c.status, "
                "c.open_case_count, h.base_snapshot_id, h.start_date, h.end_date, "
                "h.rule_version_id FROM review_cycles c LEFT JOIN historical_review_cycles h "
                "USING (cycle_id) ORDER BY c.rowid"
            ).fetchall()
        )

    def load_review_cycles_for_case(self, review_case_id: str) -> tuple[ReviewCycleRecord, ...]:
        cycle_ids = frozenset(
            str(row[0])
            for row in self._metadata.execute(
                "SELECT cycle_id FROM review_cycle_cases WHERE review_case_id = ?",
                (review_case_id,),
            ).fetchall()
        )
        return tuple(
            cycle for cycle in self.load_review_cycles() if str(cycle.cycle_id) in cycle_ids
        )

    def load_plausibility_rule_versions(self) -> tuple[PlausibilityRuleRecord, ...]:
        self._require_open()
        return tuple(
            PlausibilityRuleRecord(
                version_id=str(row[0]),
                data_type=str(row[1]),
                unit=str(row[2]),
                fixed_lower_bound=None if row[3] is None else float(row[3]),
                fixed_upper_bound=None if row[4] is None else float(row[4]),
                personal_range_enabled=bool(row[5]),
                effective_from=None if row[6] is None else datetime.fromisoformat(str(row[6])),
                created_at=datetime.fromisoformat(str(row[9])),
                recommendation_id=None if row[10] is None else str(row[10]),
                effective_timezone=None if row[7] is None else str(row[7]),
                effective_offset_minutes=None if row[8] is None else int(row[8]),
            )
            for row in self._metadata.execute(
                "SELECT rule_version_id, data_type, canonical_unit, fixed_lower_bound, "
                "fixed_upper_bound, personal_range_enabled, effective_from, "
                "effective_timezone, effective_offset_minutes, created_at, recommendation_id "
                "FROM plausibility_rule_versions "
                "ORDER BY data_type, effective_from IS NOT NULL, effective_from, created_at"
            ).fetchall()
        )

    def load_activity_derivation_version(
        self, snapshot_id: SnapshotId | None
    ) -> ActivityDerivationRecord:
        self._require_open()
        selected_snapshot = snapshot_id or self.load_active_snapshot_id()
        row = None
        if selected_snapshot is not None:
            row = self._metadata.execute(
                "SELECT version_id, coverage_gap_minutes, source_classifier_version, "
                "created_at_utc "
                "FROM activity_derivation_snapshot_bindings JOIN activity_derivation_versions "
                "USING (version_id) WHERE snapshot_id = ?",
                (str(selected_snapshot),),
            ).fetchone()
        if row is None:
            row = self._metadata.execute(
                "SELECT version_id, coverage_gap_minutes, source_classifier_version, "
                "created_at_utc "
                "FROM activity_derivation_versions WHERE version_id = 'activity-derivation/v1'"
            ).fetchone()
        assert row is not None
        return ActivityDerivationRecord(
            str(row[0]), int(row[1]), str(row[2]), datetime.fromisoformat(str(row[3]))
        )

    def load_context_coverage_start(
        self, snapshot_id: SnapshotId | None
    ) -> StoredContextCoverageStart | None:
        self._require_open()
        selected = self.load_active_snapshot_id() if snapshot_id is None else snapshot_id
        if selected is None:
            return None
        row = self._metadata.execute(
            """
            SELECT revision.logical_id, revision.revision_id, revision.previous_revision_id,
                   revision.state, value.start_date
            FROM manual_context_snapshot_bindings binding
            JOIN manual_context_revisions revision USING (revision_id)
            LEFT JOIN context_coverage_start_values value USING (revision_id)
            WHERE binding.snapshot_id = ? AND revision.object_kind = 'context_coverage_start'
            """,
            (str(selected),),
        ).fetchone()
        if row is None:
            return None
        return StoredContextCoverageStart(
            str(row[0]),
            str(row[1]),
            None if row[2] is None else str(row[2]),
            cast(Literal["active", "withdrawn"], str(row[3])),
            None if row[4] is None else date.fromisoformat(str(row[4])),
            selected,
        )

    def _load_snapshot_binding(self, snapshot_id: SnapshotId) -> dict[str, object] | None:
        try:
            manifest = json.loads(
                (
                    self._root
                    / _PARQUET_DIRECTORY
                    / "snapshots"
                    / str(snapshot_id)
                    / "manifest.json"
                ).read_bytes()
            )
        except (OSError, json.JSONDecodeError) as error:
            raise StoreError("Snapshot-Manifest ist nicht lesbar.") from error
        binding = manifest.get("snapshot_binding") if isinstance(manifest, dict) else None
        return binding if isinstance(binding, dict) else None

    def load_medication_as_of(self, snapshot_id: SnapshotId | None) -> datetime:
        self._require_open()
        selected = self.load_active_snapshot_id() if snapshot_id is None else snapshot_id
        if selected is None:
            return datetime.now(UTC)
        binding = self._load_snapshot_binding(selected)
        if binding is not None:
            return datetime.fromisoformat(str(binding["medication_as_of"]))
        row = self._metadata.execute(
            "SELECT medication_as_of FROM medication_publications WHERE snapshot_id = ?",
            (str(selected),),
        ).fetchone()
        if row is None:
            row = self._metadata.execute(
                "SELECT medication_as_of FROM medication_deviation_publications "
                "WHERE snapshot_id = ?",
                (str(selected),),
            ).fetchone()
        if row is None:
            row = self._metadata.execute(
                "SELECT medication_as_of FROM intake_reason_category_publications "
                "WHERE snapshot_id = ?",
                (str(selected),),
            ).fetchone()
        if row is None:
            row = self._metadata.execute(
                "SELECT medication_as_of FROM as_needed_intake_publications WHERE snapshot_id = ?",
                (str(selected),),
            ).fetchone()
        if row is not None:
            return datetime.fromisoformat(str(row[0]))
        row = self._metadata.execute(
            "SELECT created_at_utc FROM dataset_snapshots WHERE snapshot_id = ?", (str(selected),)
        ).fetchone()
        if row is None:
            raise StoreError("Snapshot fehlt.")
        return datetime.fromisoformat(str(row[0]))

    def _stored_medication_regimes(
        self, where: str, args: tuple[object, ...]
    ) -> tuple[StoredMedicationRegime, ...]:
        rows = self._metadata.execute(
            "SELECT revision.logical_id, revision.revision_id, revision.previous_revision_id, "
            "value.starts_at, value.timezone FROM medication_regime_revisions revision "
            "JOIN medication_regime_values value USING (revision_id) "
            + where
            + " ORDER BY value.starts_at, revision.rowid",
            args,
        ).fetchall()
        values: list[StoredMedicationRegime] = []
        for row in rows:
            doses = tuple(
                (
                    str(dose[0]),
                    str(dose[1]),
                    str(dose[2]),
                    time.fromisoformat(str(dose[3])),
                    tuple(json.loads(str(dose[4]))),
                )
                for dose in self._metadata.execute(
                    "SELECT medication_name, amount, unit, local_time, weekdays "
                    "FROM medication_scheduled_doses WHERE revision_id = ? ORDER BY rowid",
                    (str(row[1]),),
                ).fetchall()
            )
            as_needed = tuple(
                (
                    str(entry[0]),
                    str(entry[1]),
                    str(entry[2]),
                    tuple(json.loads(str(entry[3]))),
                    str(entry[4]),
                )
                for entry in self._metadata.execute(
                    "SELECT medication_name, amount, unit, preferred_reason_category_ids, entry_id "
                    "FROM medication_as_needed_entries WHERE revision_id = ? ORDER BY rowid",
                    (str(row[1]),),
                ).fetchall()
            )
            values.append(
                StoredMedicationRegime(
                    str(row[0]),
                    str(row[1]),
                    None if row[2] is None else str(row[2]),
                    datetime.fromisoformat(str(row[3])),
                    str(row[4]),
                    doses,
                    as_needed,
                )
            )
        return tuple(values)

    def load_active_medication_regimes(
        self, snapshot_id: SnapshotId | None
    ) -> tuple[StoredMedicationRegime, ...]:
        self._require_open()
        selected = self.load_active_snapshot_id() if snapshot_id is None else snapshot_id
        if selected is None:
            return ()
        return self._stored_medication_regimes(
            "JOIN medication_snapshot_bindings binding USING (revision_id) "
            "WHERE binding.snapshot_id = ?",
            (str(selected),),
        )

    def load_medication_regime(
        self, snapshot_id: SnapshotId | None, logical_id: str
    ) -> StoredMedicationRegime | None:
        return next(
            (
                item
                for item in self.load_active_medication_regimes(snapshot_id)
                if item.logical_id == logical_id
            ),
            None,
        )

    def load_medication_regime_audit(self, logical_id: str) -> tuple[StoredMedicationRegime, ...]:
        self._require_open()
        return self._stored_medication_regimes("WHERE revision.logical_id = ?", (logical_id,))

    def _stored_medication_deviations(
        self, where: str, args: tuple[object, ...]
    ) -> tuple[StoredMedicationDeviation, ...]:
        rows = self._metadata.execute(
            "SELECT revision.logical_id, revision.revision_id, revision.previous_revision_id, "
            "revision.state, value.regime_logical_id, value.scheduled_at, value.withdrawal_reason "
            "FROM medication_deviation_revisions revision "
            "JOIN medication_deviation_values value USING (revision_id) "
            + where
            + " ORDER BY revision.rowid",
            args,
        ).fetchall()
        return tuple(
            StoredMedicationDeviation(
                str(row[0]),
                str(row[1]),
                None if row[2] is None else str(row[2]),
                cast(Literal["active", "withdrawn"], str(row[3])),
                str(row[4]),
                datetime.fromisoformat(str(row[5])),
                tuple(
                    (datetime.fromisoformat(str(item[0])), str(item[1]))
                    for item in self._metadata.execute(
                        "SELECT taken_at, amount FROM medication_deviation_intakes "
                        "WHERE revision_id = ? ORDER BY rowid",
                        (str(row[1]),),
                    ).fetchall()
                ),
                None if row[6] is None else str(row[6]),
            )
            for row in rows
        )

    def load_active_medication_deviations(
        self, snapshot_id: SnapshotId | None
    ) -> tuple[StoredMedicationDeviation, ...]:
        self._require_open()
        selected = self.load_active_snapshot_id() if snapshot_id is None else snapshot_id
        if selected is None:
            return ()
        return self._stored_medication_deviations(
            "JOIN medication_deviation_snapshot_bindings binding USING (revision_id) "
            "WHERE binding.snapshot_id = ?",
            (str(selected),),
        )

    def load_medication_deviation(
        self, snapshot_id: SnapshotId | None, logical_id: str
    ) -> StoredMedicationDeviation | None:
        return next(
            (
                item
                for item in self.load_active_medication_deviations(snapshot_id)
                if item.logical_id == logical_id
            ),
            None,
        )

    def load_medication_deviation_audit(
        self, logical_id: str
    ) -> tuple[StoredMedicationDeviation, ...]:
        self._require_open()
        return self._stored_medication_deviations("WHERE revision.logical_id = ?", (logical_id,))

    def _stored_intake_reason_categories(
        self, where: str, args: tuple[object, ...]
    ) -> tuple[StoredIntakeReasonCategory, ...]:
        rows = self._metadata.execute(
            "SELECT revision.logical_id, revision.revision_id, revision.previous_revision_id, "
            "revision.state, value.name, value.name_key, value.rowid "
            "FROM intake_reason_category_revisions revision "
            "JOIN intake_reason_category_values value USING (revision_id) "
            + where
            + " ORDER BY value.rowid",
            args,
        ).fetchall()
        return tuple(
            StoredIntakeReasonCategory(
                str(row[0]),
                str(row[1]),
                None if row[2] is None else str(row[2]),
                cast(Literal["active", "withdrawn"], str(row[3])),
                None if row[4] is None else str(row[4]),
                None,
            )
            for row in rows
        )

    def load_active_intake_reason_categories(
        self, snapshot_id: SnapshotId | None
    ) -> tuple[StoredIntakeReasonCategory, ...]:
        self._require_open()
        selected = self.load_active_snapshot_id() if snapshot_id is None else snapshot_id
        if selected is None:
            return ()
        return self._stored_intake_reason_categories(
            "JOIN intake_reason_category_snapshot_bindings binding USING (revision_id) "
            "WHERE binding.snapshot_id = ? AND revision.state = 'active'",
            (str(selected),),
        )

    def load_intake_reason_category(
        self, snapshot_id: SnapshotId | None, logical_id: str
    ) -> StoredIntakeReasonCategory | None:
        return next(
            (
                item
                for item in self.load_active_intake_reason_categories(snapshot_id)
                if item.logical_id == logical_id
            ),
            None,
        )

    def load_intake_reason_category_audit(
        self, logical_id: str
    ) -> tuple[StoredIntakeReasonCategory, ...]:
        self._require_open()
        return self._stored_intake_reason_categories("WHERE revision.logical_id = ?", (logical_id,))

    def is_intake_reason_category_name_reserved(
        self, name: str, *, excluding_logical_id: str | None = None
    ) -> bool:
        self._require_open()
        name_key = " ".join(name.split()).casefold()
        return (
            self._metadata.execute(
                "SELECT 1 FROM intake_reason_category_values value "
                "JOIN intake_reason_category_revisions revision USING (revision_id) "
                "WHERE value.name_key = ? AND (? IS NULL OR revision.logical_id != ?)",
                (name_key, excluding_logical_id, excluding_logical_id),
            ).fetchone()
            is not None
        )

    def _stored_as_needed_intakes(
        self, where: str, args: tuple[object, ...]
    ) -> tuple[StoredAsNeededIntake, ...]:
        rows = self._metadata.execute(
            "SELECT revision.logical_id, revision.revision_id, revision.previous_revision_id, "
            "revision.state, value.regime_logical_id, value.entry_id, value.taken_at, "
            "value.amount, "
            "value.reason_category_logical_id, value.withdrawal_reason "
            "FROM as_needed_intake_revisions revision "
            "JOIN as_needed_intake_values value USING (revision_id) "
            + where
            + " ORDER BY revision.rowid",
            args,
        ).fetchall()
        return tuple(
            StoredAsNeededIntake(
                str(row[0]),
                str(row[1]),
                None if row[2] is None else str(row[2]),
                cast(Literal["active", "withdrawn"], str(row[3])),
                str(row[4]),
                str(row[5]),
                datetime.fromisoformat(str(row[6])),
                str(row[7]),
                None if row[8] is None else str(row[8]),
                None if row[9] is None else str(row[9]),
            )
            for row in rows
        )

    def load_active_as_needed_intakes(
        self, snapshot_id: SnapshotId | None
    ) -> tuple[StoredAsNeededIntake, ...]:
        self._require_open()
        selected = self.load_active_snapshot_id() if snapshot_id is None else snapshot_id
        if selected is None:
            return ()
        return self._stored_as_needed_intakes(
            "JOIN as_needed_intake_snapshot_bindings binding USING (revision_id) "
            "WHERE binding.snapshot_id = ? AND revision.state = 'active'",
            (str(selected),),
        )

    def load_as_needed_intake(
        self, snapshot_id: SnapshotId | None, logical_id: str
    ) -> StoredAsNeededIntake | None:
        return next(
            (
                item
                for item in self.load_active_as_needed_intakes(snapshot_id)
                if item.logical_id == logical_id
            ),
            None,
        )

    def load_as_needed_intake_audit(self, logical_id: str) -> tuple[StoredAsNeededIntake, ...]:
        self._require_open()
        return self._stored_as_needed_intakes("WHERE revision.logical_id = ?", (logical_id,))

    def publish_intake_reason_category(
        self, publication: IntakeReasonCategoryPublication
    ) -> MedicationRootPublicationResult:
        self._require_open()
        self._require_writer()
        active = self.load_active_snapshot_id()
        if active != publication.expected_snapshot_id:
            raise StoreError("Aktiver Snapshot hat sich geändert.")
        audit = self.load_intake_reason_category_audit(str(publication.logical_id))
        current = self.load_intake_reason_category(active, str(publication.logical_id))
        if publication.intent == "create":
            if audit:
                raise StoreError("Einnahmegrund existiert bereits.")
            previous, state = None, "active"
        elif publication.intent == "restore":
            if (
                not audit
                or audit[-1].revision_id != str(publication.expected_revision_id)
                or audit[-1].state != "withdrawn"
            ):
                raise StoreError("Einnahmegrundrevision hat sich geändert.")
            previous, state = audit[-1].revision_id, "active"
        else:
            if current is None or current.revision_id != str(publication.expected_revision_id):
                raise StoreError("Einnahmegrundrevision hat sich geändert.")
            previous, state = (
                current.revision_id,
                "withdrawn" if publication.intent == "withdraw" else "active",
            )
        revision_id, snapshot_id = MedicationRevisionId(uuid4().hex), SnapshotId(uuid4().hex)
        created_at = datetime.now(UTC).isoformat()
        payload = {
            "intent": publication.intent,
            "name": publication.name,
            "withdrawal_reason": publication.withdrawal_reason,
        }
        payload_sha256 = _manual_payload_sha256(payload)
        try:
            with self._metadata:
                self._metadata.execute(
                    "INSERT INTO write_operations VALUES "
                    "(?, 'revise_intake_reason_category', ?, ?, 'committed', 1)",
                    (str(publication.operation_id), created_at, created_at),
                )
                self._metadata.execute(
                    "INSERT INTO intake_reason_category_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(revision_id),
                        str(publication.logical_id),
                        previous,
                        str(publication.operation_id),
                        state,
                        created_at,
                        payload_sha256,
                    ),
                )
                self._metadata.execute(
                    "INSERT INTO manual_revision_intents VALUES (?, ?, ?)",
                    (str(revision_id), publication.intent, publication.withdrawal_reason),
                )
                self._metadata.execute(
                    "INSERT INTO intake_reason_category_values VALUES (?, ?, ?)",
                    (
                        str(revision_id),
                        publication.name,
                        None
                        if publication.name is None
                        else " ".join(publication.name.split()).casefold(),
                    ),
                )
                position = int(
                    self._metadata.execute(
                        "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
                    ).fetchone()[0]
                )
                manifest = self._stage_review_snapshot(
                    parent_snapshot_id=active,
                    snapshot_id=snapshot_id,
                    operation_id=publication.operation_id,
                    audit_position=position,
                    review_case_id=None,
                    decision_id="",
                    action="manual_medication_revision",
                    selected_measurement_version_id=None,
                    candidate_version_ids=(),
                    replacement_plausibility_cases=(),
                    snapshot_as_of=publication.medication_as_of,
                )
                self._activate_review_snapshot(
                    publication.operation_id,
                    snapshot_id,
                    active,
                    manifest,
                    created_at,
                    activation_kind="manual_context_revision",
                )
                self._metadata.execute(
                    "DELETE FROM intake_reason_category_snapshot_bindings WHERE snapshot_id = ? "
                    "AND revision_id IN (SELECT revision_id FROM intake_reason_category_revisions "
                    "WHERE logical_id = ?)",
                    (str(snapshot_id), str(publication.logical_id)),
                )
                if state == "active":
                    self._metadata.execute(
                        "INSERT INTO intake_reason_category_snapshot_bindings VALUES (?, ?)",
                        (str(snapshot_id), str(revision_id)),
                    )
                audit_event_id = uuid4().hex
                self._metadata.execute(
                    "INSERT INTO audit_events VALUES (?, ?, ?, 'manual_medication_revision', ?)",
                    (position, audit_event_id, str(publication.operation_id), created_at),
                )
                self._metadata.execute(
                    "INSERT INTO intake_reason_category_publications VALUES (?, ?, ?, ?)",
                    (
                        audit_event_id,
                        str(revision_id),
                        str(snapshot_id),
                        publication.medication_as_of.isoformat(),
                    ),
                )
        except Exception:
            shutil.rmtree(
                self._root / "staging" / str(publication.operation_id), ignore_errors=True
            )
            shutil.rmtree(
                self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id), ignore_errors=True
            )
            raise
        return MedicationRootPublicationResult(publication.logical_id, revision_id, snapshot_id)

    def publish_as_needed_intake(
        self, publication: AsNeededIntakePublication
    ) -> MedicationRootPublicationResult:
        self._require_open()
        self._require_writer()
        active = self.load_active_snapshot_id()
        if active != publication.expected_snapshot_id:
            raise StoreError("Aktiver Snapshot hat sich geändert.")
        audit = self.load_as_needed_intake_audit(str(publication.logical_id))
        current = self.load_as_needed_intake(active, str(publication.logical_id))
        if publication.intent == "create":
            if audit:
                raise StoreError("Bedarfseinnahme existiert bereits.")
            previous, state = None, "active"
        elif publication.intent == "restore":
            if (
                not audit
                or audit[-1].revision_id != str(publication.expected_revision_id)
                or audit[-1].state != "withdrawn"
            ):
                raise StoreError("Bedarfseinnahmerevision hat sich geändert.")
            previous, state = audit[-1].revision_id, "active"
        else:
            if current is None or current.revision_id != str(publication.expected_revision_id):
                raise StoreError("Bedarfseinnahmerevision hat sich geändert.")
            previous, state = (
                current.revision_id,
                "withdrawn" if publication.intent == "withdraw" else "active",
            )
        revision_id, snapshot_id = MedicationRevisionId(uuid4().hex), SnapshotId(uuid4().hex)
        created_at = datetime.now(UTC).isoformat()
        payload = {
            "intent": publication.intent,
            "regime_logical_id": str(publication.regime_logical_id),
            "entry_id": publication.entry_id,
            "taken_at": publication.taken_at.isoformat(),
            "amount": publication.amount,
            "reason_category_logical_id": None
            if publication.reason_category_logical_id is None
            else str(publication.reason_category_logical_id),
            "withdrawal_reason": publication.withdrawal_reason,
        }
        payload_sha256 = _manual_payload_sha256(payload)
        try:
            with self._metadata:
                self._metadata.execute(
                    "INSERT INTO write_operations VALUES "
                    "(?, 'revise_as_needed_intake', ?, ?, 'committed', 1)",
                    (str(publication.operation_id), created_at, created_at),
                )
                self._metadata.execute(
                    "INSERT INTO as_needed_intake_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(revision_id),
                        str(publication.logical_id),
                        previous,
                        str(publication.operation_id),
                        state,
                        created_at,
                        payload_sha256,
                    ),
                )
                self._metadata.execute(
                    "INSERT INTO manual_revision_intents VALUES (?, ?, ?)",
                    (str(revision_id), publication.intent, publication.withdrawal_reason),
                )
                self._metadata.execute(
                    "INSERT INTO as_needed_intake_values VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(revision_id),
                        str(publication.regime_logical_id),
                        publication.entry_id,
                        publication.taken_at.isoformat(),
                        publication.amount,
                        None
                        if publication.reason_category_logical_id is None
                        else str(publication.reason_category_logical_id),
                        publication.withdrawal_reason,
                    ),
                )
                position = int(
                    self._metadata.execute(
                        "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
                    ).fetchone()[0]
                )
                manifest = self._stage_review_snapshot(
                    parent_snapshot_id=active,
                    snapshot_id=snapshot_id,
                    operation_id=publication.operation_id,
                    audit_position=position,
                    review_case_id=None,
                    decision_id="",
                    action="manual_medication_revision",
                    selected_measurement_version_id=None,
                    candidate_version_ids=(),
                    replacement_plausibility_cases=(),
                    snapshot_as_of=publication.medication_as_of,
                )
                self._activate_review_snapshot(
                    publication.operation_id,
                    snapshot_id,
                    active,
                    manifest,
                    created_at,
                    activation_kind="manual_context_revision",
                )
                self._metadata.execute(
                    "DELETE FROM as_needed_intake_snapshot_bindings WHERE snapshot_id = ? "
                    "AND revision_id IN (SELECT revision_id FROM as_needed_intake_revisions "
                    "WHERE logical_id = ?)",
                    (str(snapshot_id), str(publication.logical_id)),
                )
                if state == "active":
                    self._metadata.execute(
                        "INSERT INTO as_needed_intake_snapshot_bindings VALUES (?, ?)",
                        (str(snapshot_id), str(revision_id)),
                    )
                audit_event_id = uuid4().hex
                self._metadata.execute(
                    "INSERT INTO audit_events VALUES (?, ?, ?, 'manual_medication_revision', ?)",
                    (position, audit_event_id, str(publication.operation_id), created_at),
                )
                self._metadata.execute(
                    "INSERT INTO as_needed_intake_publications VALUES (?, ?, ?, ?)",
                    (
                        audit_event_id,
                        str(revision_id),
                        str(snapshot_id),
                        publication.medication_as_of.isoformat(),
                    ),
                )
        except Exception:
            shutil.rmtree(
                self._root / "staging" / str(publication.operation_id), ignore_errors=True
            )
            shutil.rmtree(
                self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id), ignore_errors=True
            )
            raise
        return MedicationRootPublicationResult(publication.logical_id, revision_id, snapshot_id)

    def publish_medication_regime(
        self, publication: MedicationRegimePublication
    ) -> MedicationRegimePublicationResult:
        self._require_open()
        self._require_writer()
        active = self.load_active_snapshot_id()
        if active != publication.expected_snapshot_id:
            raise StoreError("Aktiver Snapshot hat sich geändert.")
        audit = self.load_medication_regime_audit(str(publication.logical_id))
        current = self.load_medication_regime(active, str(publication.logical_id))
        if publication.intent == "create":
            if audit or any(
                regime.starts_at == publication.starts_at
                for regime in self.load_active_medication_regimes(active)
            ):
                raise StoreError("Medikamentenregime existiert bereits.")
            previous = None
        else:
            if (
                not audit
                or current is None
                or current.revision_id != str(publication.expected_revision_id)
            ):
                raise StoreError("Medikamentenrevision hat sich geändert.")
            previous = current.revision_id
        revision_id, snapshot_id = MedicationRevisionId(uuid4().hex), SnapshotId(uuid4().hex)
        created_at = datetime.now(UTC).isoformat()
        payload = {
            "intent": publication.intent,
            "starts_at": publication.starts_at.isoformat(),
            "timezone": publication.timezone,
            "doses": [
                (name, amount, unit, local_time.isoformat(), days)
                for name, amount, unit, local_time, days in publication.scheduled_doses
            ],
            "as_needed": publication.as_needed_medications,
        }
        payload_sha256 = _manual_payload_sha256(payload)
        try:
            with self._metadata:
                self._metadata.execute(
                    "INSERT INTO write_operations VALUES "
                    "(?, 'revise_medication_regime', ?, ?, 'committed', 1)",
                    (str(publication.operation_id), created_at, created_at),
                )
                self._metadata.execute(
                    "INSERT INTO medication_regime_revisions VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(revision_id),
                        str(publication.logical_id),
                        previous,
                        str(publication.operation_id),
                        created_at,
                        payload_sha256,
                    ),
                )
                self._metadata.execute(
                    "INSERT INTO manual_revision_intents VALUES (?, ?, NULL)",
                    (str(revision_id), publication.intent),
                )
                self._metadata.execute(
                    "INSERT INTO medication_regime_values VALUES (?, ?, ?)",
                    (str(revision_id), publication.starts_at.isoformat(), publication.timezone),
                )
                self._metadata.executemany(
                    "INSERT INTO medication_scheduled_doses VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            uuid4().hex,
                            str(revision_id),
                            name,
                            amount,
                            unit,
                            local_time.isoformat(),
                            json.dumps(days),
                        )
                        for name, amount, unit, local_time, days in publication.scheduled_doses
                    ],
                )
                self._metadata.executemany(
                    "INSERT INTO medication_as_needed_entries VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            entry_id,
                            str(revision_id),
                            name,
                            amount,
                            unit,
                            json.dumps(reason_ids),
                        )
                        for name, amount, unit, reason_ids, entry_id in (
                            publication.as_needed_medications
                        )
                    ],
                )
                position = int(
                    self._metadata.execute(
                        "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
                    ).fetchone()[0]
                )
                manifest = self._stage_review_snapshot(
                    parent_snapshot_id=active,
                    snapshot_id=snapshot_id,
                    operation_id=publication.operation_id,
                    audit_position=position,
                    review_case_id=None,
                    decision_id="",
                    action="manual_medication_revision",
                    selected_measurement_version_id=None,
                    candidate_version_ids=(),
                    replacement_plausibility_cases=(),
                    snapshot_as_of=publication.medication_as_of,
                )
                self._activate_review_snapshot(
                    publication.operation_id,
                    snapshot_id,
                    active,
                    manifest,
                    created_at,
                    activation_kind="manual_context_revision",
                )
                self._metadata.execute(
                    "DELETE FROM medication_snapshot_bindings WHERE snapshot_id = ? "
                    "AND revision_id IN (SELECT revision_id FROM medication_regime_revisions "
                    "WHERE logical_id = ?)",
                    (str(snapshot_id), str(publication.logical_id)),
                )
                self._metadata.execute(
                    "INSERT INTO medication_snapshot_bindings VALUES (?, ?)",
                    (str(snapshot_id), str(revision_id)),
                )
                audit_event_id = uuid4().hex
                self._metadata.execute(
                    "INSERT INTO audit_events VALUES (?, ?, ?, 'manual_medication_revision', ?)",
                    (position, audit_event_id, str(publication.operation_id), created_at),
                )
                self._metadata.execute(
                    "INSERT INTO medication_publications VALUES (?, ?, ?, ?)",
                    (
                        audit_event_id,
                        str(revision_id),
                        str(snapshot_id),
                        publication.medication_as_of.isoformat(),
                    ),
                )
        except Exception:
            shutil.rmtree(
                self._root / "staging" / str(publication.operation_id), ignore_errors=True
            )
            shutil.rmtree(
                self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id), ignore_errors=True
            )
            raise
        return MedicationRegimePublicationResult(publication.logical_id, revision_id, snapshot_id)

    def publish_medication_deviation(
        self, publication: MedicationDeviationPublication
    ) -> MedicationDeviationPublicationResult:
        self._require_open()
        self._require_writer()
        active = self.load_active_snapshot_id()
        if active != publication.expected_snapshot_id:
            raise StoreError("Aktiver Snapshot hat sich geändert.")
        audit = self.load_medication_deviation_audit(str(publication.logical_id))
        current = self.load_medication_deviation(active, str(publication.logical_id))
        if publication.intent == "create":
            if audit or any(
                item.regime_logical_id == str(publication.regime_logical_id)
                and item.scheduled_at == publication.scheduled_at
                for item in self.load_active_medication_deviations(active)
            ):
                raise StoreError("Einnahmeabweichung existiert bereits.")
            previous, state = None, "active"
        elif publication.intent == "restore":
            if (
                not audit
                or audit[-1].revision_id != str(publication.expected_revision_id)
                or audit[-1].state != "withdrawn"
            ):
                raise StoreError("Einnahmeabweichungsrevision hat sich geändert.")
            previous, state = audit[-1].revision_id, "active"
        else:
            if current is None or current.revision_id != str(publication.expected_revision_id):
                raise StoreError("Einnahmeabweichungsrevision hat sich geändert.")
            previous, state = (
                current.revision_id,
                "withdrawn" if publication.intent == "withdraw" else "active",
            )
        revision_id, snapshot_id = MedicationRevisionId(uuid4().hex), SnapshotId(uuid4().hex)
        created_at = datetime.now(UTC).isoformat()
        payload = {
            "intent": publication.intent,
            "regime_logical_id": str(publication.regime_logical_id),
            "scheduled_at": publication.scheduled_at.isoformat(),
            "withdrawal_reason": publication.withdrawal_reason,
            "actual_intakes": [
                (item.isoformat(), amount) for item, amount in publication.actual_intakes
            ],
        }
        payload_sha256 = _manual_payload_sha256(payload)
        try:
            with self._metadata:
                self._metadata.execute(
                    "INSERT INTO write_operations VALUES "
                    "(?, 'revise_medication_deviation', ?, ?, 'committed', 1)",
                    (str(publication.operation_id), created_at, created_at),
                )
                self._metadata.execute(
                    "INSERT INTO medication_deviation_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(revision_id),
                        str(publication.logical_id),
                        previous,
                        str(publication.operation_id),
                        state,
                        created_at,
                        payload_sha256,
                    ),
                )
                self._metadata.execute(
                    "INSERT INTO manual_revision_intents VALUES (?, ?, ?)",
                    (str(revision_id), publication.intent, publication.withdrawal_reason),
                )
                self._metadata.execute(
                    "INSERT INTO medication_deviation_values VALUES (?, ?, ?, ?)",
                    (
                        str(revision_id),
                        str(publication.regime_logical_id),
                        publication.scheduled_at.isoformat(),
                        publication.withdrawal_reason,
                    ),
                )
                self._metadata.executemany(
                    "INSERT INTO medication_deviation_intakes VALUES (?, ?, ?)",
                    [
                        (str(revision_id), taken_at.isoformat(), amount)
                        for taken_at, amount in publication.actual_intakes
                    ],
                )
                position = int(
                    self._metadata.execute(
                        "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
                    ).fetchone()[0]
                )
                manifest = self._stage_review_snapshot(
                    parent_snapshot_id=active,
                    snapshot_id=snapshot_id,
                    operation_id=publication.operation_id,
                    audit_position=position,
                    review_case_id=None,
                    decision_id="",
                    action="manual_medication_revision",
                    selected_measurement_version_id=None,
                    candidate_version_ids=(),
                    replacement_plausibility_cases=(),
                    snapshot_as_of=publication.medication_as_of,
                )
                self._activate_review_snapshot(
                    publication.operation_id,
                    snapshot_id,
                    active,
                    manifest,
                    created_at,
                    activation_kind="manual_context_revision",
                )
                self._metadata.execute(
                    "DELETE FROM medication_deviation_snapshot_bindings "
                    "WHERE snapshot_id = ? AND revision_id IN "
                    "(SELECT revision_id FROM medication_deviation_revisions WHERE logical_id = ?)",
                    (str(snapshot_id), str(publication.logical_id)),
                )
                if state == "active":
                    self._metadata.execute(
                        "INSERT INTO medication_deviation_snapshot_bindings VALUES (?, ?)",
                        (str(snapshot_id), str(revision_id)),
                    )
                audit_event_id = uuid4().hex
                self._metadata.execute(
                    "INSERT INTO audit_events VALUES (?, ?, ?, 'manual_medication_revision', ?)",
                    (position, audit_event_id, str(publication.operation_id), created_at),
                )
                self._metadata.execute(
                    "INSERT INTO medication_deviation_publications VALUES (?, ?, ?, ?)",
                    (
                        audit_event_id,
                        str(revision_id),
                        str(snapshot_id),
                        publication.medication_as_of.isoformat(),
                    ),
                )
        except Exception:
            shutil.rmtree(
                self._root / "staging" / str(publication.operation_id), ignore_errors=True
            )
            shutil.rmtree(
                self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id), ignore_errors=True
            )
            raise
        return MedicationDeviationPublicationResult(
            publication.logical_id, revision_id, snapshot_id
        )

    def load_context_coverage_audit(
        self, logical_id: str
    ) -> tuple[StoredContextCoverageStart, ...]:
        self._require_open()
        rows = self._metadata.execute(
            """
            SELECT revision.logical_id, revision.revision_id, revision.previous_revision_id,
                   revision.state, value.start_date
            FROM manual_context_revisions revision
            LEFT JOIN context_coverage_start_values value USING (revision_id)
            WHERE revision.logical_id = ? AND revision.object_kind = 'context_coverage_start'
            ORDER BY revision.rowid
            """,
            (logical_id,),
        ).fetchall()
        return tuple(
            StoredContextCoverageStart(
                str(row[0]),
                str(row[1]),
                None if row[2] is None else str(row[2]),
                cast(Literal["active", "withdrawn"], str(row[3])),
                None if row[4] is None else date.fromisoformat(str(row[4])),
                None,
            )
            for row in rows
        )

    def load_illness_revisions(self, logical_id: str) -> tuple[StoredIllnessRevision, ...]:
        self._require_open()
        rows = self._metadata.execute(
            """
            SELECT revision.logical_id, revision.revision_id, revision.previous_revision_id,
                   revision.state, revision.object_kind, COALESCE(category.name, label.name),
                   COALESCE(period.category_logical_id, custom_period.label_logical_id),
                   COALESCE(period.start_date, custom_period.start_date, stress.day),
                   COALESCE(period.end_date, custom_period.end_date), period.severity,
                   stress.level, custom_period.note
            FROM manual_context_revisions revision
            LEFT JOIN illness_category_values category USING (revision_id)
            LEFT JOIN illness_period_values period USING (revision_id)
            LEFT JOIN daily_stress_values stress USING (revision_id)
            LEFT JOIN custom_context_label_values label USING (revision_id)
            LEFT JOIN custom_context_period_values custom_period USING (revision_id)
            WHERE revision.logical_id = ?
              AND revision.object_kind IN (
                  'illness_category', 'illness_period', 'daily_stress',
                  'custom_context_label', 'custom_context_period'
              )
            ORDER BY revision.rowid
            """,
            (logical_id,),
        ).fetchall()
        return tuple(
            StoredIllnessRevision(
                str(row[0]),
                str(row[1]),
                None if row[2] is None else str(row[2]),
                cast(Literal["active", "withdrawn"], str(row[3])),
                cast(
                    Literal[
                        "illness_category",
                        "illness_period",
                        "daily_stress",
                        "custom_context_label",
                        "custom_context_period",
                    ],
                    str(row[4]),
                ),
                None if row[5] is None else str(row[5]),
                None if row[6] is None else str(row[6]),
                None if row[7] is None else date.fromisoformat(str(row[7])),
                None if row[8] is None else date.fromisoformat(str(row[8])),
                None if row[9] is None else str(row[9]),
                None if row[10] is None else str(row[10]),
                None if row[11] is None else str(row[11]),
            )
            for row in rows
        )

    def is_custom_context_label_name_reserved(
        self, name: str, *, excluding_logical_id: str | None = None
    ) -> bool:
        self._require_open()
        return (
            self._metadata.execute(
                """
                SELECT 1
                FROM custom_context_label_values value
                JOIN manual_context_revisions revision USING (revision_id)
                WHERE value.name_key = ?
                  AND (? IS NULL OR revision.logical_id != ?)
                """,
                (_normalized_context_name(name), excluding_logical_id, excluding_logical_id),
            ).fetchone()
            is not None
        )

    def load_active_illness(
        self, snapshot_id: SnapshotId | None
    ) -> tuple[StoredIllnessRevision, ...]:
        self._require_open()
        selected = self.load_active_snapshot_id() if snapshot_id is None else snapshot_id
        if selected is None:
            return ()
        rows = self._metadata.execute(
            """
            SELECT revision.logical_id, revision.revision_id, revision.previous_revision_id,
                   revision.state, revision.object_kind, COALESCE(category.name, label.name),
                   COALESCE(period.category_logical_id, custom_period.label_logical_id),
                   COALESCE(period.start_date, custom_period.start_date, stress.day),
                   COALESCE(period.end_date, custom_period.end_date), period.severity,
                   stress.level, custom_period.note
            FROM manual_context_snapshot_bindings binding
            JOIN manual_context_revisions revision USING (revision_id)
            LEFT JOIN illness_category_values category USING (revision_id)
            LEFT JOIN illness_period_values period USING (revision_id)
            LEFT JOIN daily_stress_values stress USING (revision_id)
            LEFT JOIN custom_context_label_values label USING (revision_id)
            LEFT JOIN custom_context_period_values custom_period USING (revision_id)
            WHERE binding.snapshot_id = ?
              AND revision.object_kind IN (
                  'illness_category', 'illness_period', 'daily_stress',
                  'custom_context_label', 'custom_context_period'
              )
            ORDER BY revision.rowid
            """,
            (str(selected),),
        ).fetchall()
        return tuple(
            StoredIllnessRevision(
                str(row[0]),
                str(row[1]),
                None if row[2] is None else str(row[2]),
                cast(Literal["active", "withdrawn"], str(row[3])),
                cast(
                    Literal[
                        "illness_category",
                        "illness_period",
                        "daily_stress",
                        "custom_context_label",
                        "custom_context_period",
                    ],
                    str(row[4]),
                ),
                None if row[5] is None else str(row[5]),
                None if row[6] is None else str(row[6]),
                None if row[7] is None else date.fromisoformat(str(row[7])),
                None if row[8] is None else date.fromisoformat(str(row[8])),
                None if row[9] is None else str(row[9]),
                None if row[10] is None else str(row[10]),
                None if row[11] is None else str(row[11]),
            )
            for row in rows
        )

    def load_context_as_of_date(self, snapshot_id: SnapshotId) -> tuple[date, str]:
        self._require_open()
        binding = self._load_snapshot_binding(snapshot_id)
        if binding is not None:
            return (
                date.fromisoformat(str(binding["context_as_of_date"])),
                str(binding["context_timezone"]),
            )
        row = self._metadata.execute(
            "SELECT context_as_of_date, context_timezone FROM manual_context_publications "
            "WHERE snapshot_id = ?",
            (str(snapshot_id),),
        ).fetchone()
        if row is not None:
            return date.fromisoformat(str(row[0])), str(row[1])
        snapshot = self._metadata.execute(
            "SELECT created_at_utc FROM dataset_snapshots WHERE snapshot_id = ?",
            (str(snapshot_id),),
        ).fetchone()
        if snapshot is None:
            raise StoreError("Snapshot fehlt.")
        return datetime.fromisoformat(str(snapshot[0])).date(), "Europe/Berlin"

    def publish_context_coverage_start(
        self,
        *,
        publication: ContextCoverageStartPublication,
    ) -> ContextCoverageStartPublicationResult:
        operation_id = publication.operation_id
        intent = publication.intent
        logical_id = publication.logical_id
        expected_revision_id = publication.expected_revision_id
        start_date = publication.start_date
        withdrawal_reason = publication.withdrawal_reason
        expected_snapshot_id = publication.expected_snapshot_id
        context_as_of_date = publication.context_as_of_date
        context_timezone = publication.context_timezone
        self._require_open()
        self._require_writer()
        active = self.load_active_snapshot_id()
        if active != expected_snapshot_id:
            raise StoreError("Aktiver Snapshot hat sich geändert.")
        current = self.load_context_coverage_start(active)
        audit = self.load_context_coverage_audit(str(logical_id))
        latest = audit[-1] if audit else None
        if intent == "create":
            if current is not None or latest is not None or start_date is None:
                raise StoreError("Kontextabdeckungsbeginn kann nicht erstellt werden.")
            previous = None
            state = "active"
        elif latest is None or latest.revision_id != str(expected_revision_id):
            raise StoreError("Kontextrevision hat sich geändert.")
        elif intent == "withdraw":
            if current is None or current.revision_id != str(expected_revision_id):
                raise StoreError("Kontextabdeckungsbeginn ist nicht aktiv.")
            previous, state, start_date = latest.revision_id, "withdrawn", None
        else:
            if start_date is None:
                raise StoreError("Kontextabdeckungsbeginn fehlt.")
            if intent == "revise" and (
                current is None or current.revision_id != str(expected_revision_id)
            ):
                raise StoreError("Kontextabdeckungsbeginn ist nicht aktiv.")
            if intent == "restore" and current is not None:
                raise StoreError("Kontextabdeckungsbeginn ist bereits aktiv.")
            previous, state = latest.revision_id, "active"
        revision_id = ContextRevisionId(uuid4().hex)
        snapshot_id = SnapshotId(uuid4().hex)
        created_at = datetime.now(UTC).isoformat()
        payload_sha256 = _manual_payload_sha256(
            {
                "intent": intent,
                "logical_id": str(logical_id),
                "start_date": None if start_date is None else start_date.isoformat(),
                "withdrawal_reason": withdrawal_reason,
            }
        )
        try:
            with self._metadata:
                self._metadata.execute(
                    "INSERT INTO write_operations VALUES "
                    "(?, 'revise_context_coverage_start', ?, ?, 'committed', 1)",
                    (str(operation_id), created_at, created_at),
                )
                self._metadata.execute(
                    "INSERT INTO manual_context_revisions VALUES "
                    "(?, ?, 'context_coverage_start', ?, ?, ?, ?, ?)",
                    (
                        str(revision_id),
                        str(logical_id),
                        None if previous is None else str(previous),
                        state,
                        str(operation_id),
                        created_at,
                        payload_sha256,
                    ),
                )
                self._metadata.execute(
                    "INSERT INTO manual_revision_intents VALUES (?, ?, ?)",
                    (str(revision_id), intent, withdrawal_reason),
                )
                if start_date is not None:
                    self._metadata.execute(
                        "INSERT INTO context_coverage_start_values VALUES (?, ?)",
                        (str(revision_id), start_date.isoformat()),
                    )
                audit_position = int(
                    self._metadata.execute(
                        "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
                    ).fetchone()[0]
                )
                manifest = self._stage_review_snapshot(
                    parent_snapshot_id=active,
                    snapshot_id=snapshot_id,
                    operation_id=operation_id,
                    audit_position=audit_position,
                    review_case_id=None,
                    decision_id="",
                    action="manual_context_revision",
                    selected_measurement_version_id=None,
                    candidate_version_ids=(),
                    replacement_plausibility_cases=(),
                    snapshot_as_of=publication.snapshot_as_of,
                    context_timezone=publication.context_timezone,
                )
                self._activate_review_snapshot(
                    operation_id,
                    snapshot_id,
                    active,
                    manifest,
                    created_at,
                    activation_kind="manual_context_revision",
                    replaced_context_logical_id=str(logical_id),
                )
                if state == "active":
                    self._metadata.execute(
                        "INSERT INTO manual_context_snapshot_bindings VALUES (?, ?)",
                        (str(snapshot_id), str(revision_id)),
                    )
                audit_event_id = uuid4().hex
                self._metadata.execute(
                    "INSERT INTO audit_events VALUES (?, ?, ?, 'manual_context_revision', ?)",
                    (audit_position, audit_event_id, str(operation_id), created_at),
                )
                self._metadata.execute(
                    "INSERT INTO manual_context_publications VALUES (?, ?, ?, ?, ?)",
                    (
                        audit_event_id,
                        str(revision_id),
                        str(snapshot_id),
                        context_as_of_date.isoformat(),
                        context_timezone,
                    ),
                )
        except Exception:
            shutil.rmtree(self._root / "staging" / str(operation_id), ignore_errors=True)
            shutil.rmtree(
                self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id), ignore_errors=True
            )
            raise
        return ContextCoverageStartPublicationResult(logical_id, revision_id, snapshot_id)

    def publish_illness(self, *, publication: IllnessPublication) -> IllnessPublicationResult:
        self._require_open()
        self._require_writer()
        active = self.load_active_snapshot_id()
        if active != publication.expected_snapshot_id:
            raise StoreError("Aktiver Snapshot hat sich geändert.")
        audit = self.load_illness_revisions(str(publication.logical_id))
        latest = audit[-1] if audit else None
        current = next(
            (
                item
                for item in self.load_active_illness(active)
                if item.logical_id == str(publication.logical_id)
            ),
            None,
        )
        if publication.intent == "create":
            if latest is not None:
                raise StoreError("Krankheitsobjekt existiert bereits.")
            previous, state = None, "active"
        elif latest is None or latest.revision_id != str(publication.expected_revision_id):
            raise StoreError("Krankheitsrevision hat sich geändert.")
        elif publication.intent == "withdraw":
            if current is None:
                raise StoreError("Krankheitsobjekt ist nicht aktiv.")
            previous, state = latest.revision_id, "withdrawn"
        else:
            if publication.intent == "revise" and current is None:
                raise StoreError("Krankheitsobjekt ist nicht aktiv.")
            if publication.intent == "restore" and current is not None:
                raise StoreError("Krankheitsobjekt ist bereits aktiv.")
            previous, state = latest.revision_id, "active"
        revision_id = ContextRevisionId(uuid4().hex)
        snapshot_id = SnapshotId(uuid4().hex)
        created_at = datetime.now(UTC).isoformat()
        payload_sha256 = _manual_payload_sha256(
            {
                "intent": publication.intent,
                "kind": publication.object_kind,
                "name": publication.name,
                "category": None
                if publication.category_logical_id is None
                else str(publication.category_logical_id),
                "start": None
                if publication.start_date is None
                else publication.start_date.isoformat(),
                "end": None
                if publication.end_date is None
                else publication.end_date.isoformat(),
                "severity": publication.severity,
                "stress": publication.stress_level,
                "note": publication.note,
                "reason": publication.withdrawal_reason,
            }
        )
        try:
            with self._metadata:
                self._metadata.execute(
                    "INSERT INTO write_operations VALUES (?, ?, ?, ?, 'committed', 1)",
                    (
                        str(publication.operation_id),
                        "revise_context_coverage_start",
                        created_at,
                        created_at,
                    ),
                )
                self._metadata.execute(
                    "INSERT INTO manual_context_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(revision_id),
                        str(publication.logical_id),
                        publication.object_kind,
                        previous,
                        state,
                        str(publication.operation_id),
                        created_at,
                        payload_sha256,
                    ),
                )
                self._metadata.execute(
                    "INSERT INTO manual_revision_intents VALUES (?, ?, ?)",
                    (
                        str(revision_id),
                        publication.intent,
                        publication.withdrawal_reason,
                    ),
                )
                if state == "active" and publication.object_kind == "illness_category":
                    assert publication.name is not None
                    self._metadata.execute(
                        "INSERT INTO illness_category_values VALUES (?, ?, ?)",
                        (
                            str(revision_id),
                            publication.name,
                            _normalized_context_name(publication.name),
                        ),
                    )
                if state == "active" and publication.object_kind == "illness_period":
                    assert (
                        publication.category_logical_id is not None
                        and publication.start_date is not None
                        and publication.severity is not None
                    )
                    self._metadata.execute(
                        "INSERT INTO illness_period_values VALUES (?, ?, ?, ?, ?)",
                        (
                            str(revision_id),
                            str(publication.category_logical_id),
                            publication.start_date.isoformat(),
                            None
                            if publication.end_date is None
                            else publication.end_date.isoformat(),
                            publication.severity,
                        ),
                    )
                if state == "active" and publication.object_kind == "daily_stress":
                    assert (
                        publication.start_date is not None and publication.stress_level is not None
                    )
                    self._metadata.execute(
                        "INSERT INTO daily_stress_values VALUES (?, ?, ?)",
                        (
                            str(revision_id),
                            publication.start_date.isoformat(),
                            publication.stress_level,
                        ),
                    )
                if state == "active" and publication.object_kind == "custom_context_label":
                    assert publication.name is not None
                    self._metadata.execute(
                        "INSERT INTO custom_context_label_values VALUES (?, ?, ?)",
                        (
                            str(revision_id),
                            publication.name,
                            _normalized_context_name(publication.name),
                        ),
                    )
                if state == "active" and publication.object_kind == "custom_context_period":
                    assert (
                        publication.category_logical_id is not None
                        and publication.start_date is not None
                    )
                    self._metadata.execute(
                        "INSERT INTO custom_context_period_values VALUES (?, ?, ?, ?, ?)",
                        (
                            str(revision_id),
                            str(publication.category_logical_id),
                            publication.start_date.isoformat(),
                            None
                            if publication.end_date is None
                            else publication.end_date.isoformat(),
                            publication.note,
                        ),
                    )
                audit_position = int(
                    self._metadata.execute(
                        "SELECT COALESCE(MAX(audit_position), 0) + 1 FROM audit_events"
                    ).fetchone()[0]
                )
                manifest = self._stage_review_snapshot(
                    parent_snapshot_id=active,
                    snapshot_id=snapshot_id,
                    operation_id=publication.operation_id,
                    audit_position=audit_position,
                    review_case_id=None,
                    decision_id="",
                    action="manual_context_revision",
                    selected_measurement_version_id=None,
                    candidate_version_ids=(),
                    replacement_plausibility_cases=(),
                    snapshot_as_of=publication.snapshot_as_of,
                    context_timezone=publication.context_timezone,
                )
                self._activate_review_snapshot(
                    publication.operation_id,
                    snapshot_id,
                    active,
                    manifest,
                    created_at,
                    activation_kind="manual_context_revision",
                    replaced_context_logical_id=str(publication.logical_id),
                )
                if state == "active":
                    self._metadata.execute(
                        "INSERT INTO manual_context_snapshot_bindings VALUES (?, ?)",
                        (str(snapshot_id), str(revision_id)),
                    )
                audit_event_id = uuid4().hex
                self._metadata.execute(
                    "INSERT INTO audit_events VALUES (?, ?, ?, 'manual_context_revision', ?)",
                    (audit_position, audit_event_id, str(publication.operation_id), created_at),
                )
                self._metadata.execute(
                    "INSERT INTO manual_context_publications VALUES (?, ?, ?, ?, ?)",
                    (
                        audit_event_id,
                        str(revision_id),
                        str(snapshot_id),
                        publication.context_as_of_date.isoformat(),
                        publication.context_timezone,
                    ),
                )
        except Exception:
            shutil.rmtree(
                self._root / "staging" / str(publication.operation_id), ignore_errors=True
            )
            shutil.rmtree(
                self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id), ignore_errors=True
            )
            raise
        return IllnessPublicationResult(publication.logical_id, revision_id, snapshot_id)

    def create_activity_derivation_version(
        self,
        *,
        operation_id: OperationId,
        version_id: str,
        coverage_gap_minutes: int,
        source_classifier_version: str,
        expected_snapshot_id: SnapshotId | None,
    ) -> SnapshotId | None:
        self._require_open()
        self._require_writer()
        active = self.load_active_snapshot_id()
        if active != expected_snapshot_id:
            raise StoreError("Aktiver Snapshot hat sich geändert.")
        created_at = datetime.now(UTC).isoformat()
        snapshot_id = None if active is None else SnapshotId(uuid4().hex)
        try:
            with self._metadata:
                # Existing stores constrain this ledger to the established rule-version kind.
                self._metadata.execute(
                    "INSERT INTO write_operations VALUES (?, "
                    "'create_plausibility_rule_version', ?, ?, "
                    "'committed', 1)",
                    (str(operation_id), created_at, created_at),
                )
                self._metadata.execute(
                    "INSERT INTO activity_derivation_versions VALUES (?, ?, ?, ?)",
                    (version_id, coverage_gap_minutes, source_classifier_version, created_at),
                )
                self._metadata.execute(
                    "UPDATE activity_derivation_active SET version_id = ? WHERE singleton = 1",
                    (version_id,),
                )
                if active is not None and snapshot_id is not None:
                    audit_position = int(
                        self._metadata.execute(
                            "SELECT COALESCE(MAX(audit_position), 0) FROM audit_events"
                        ).fetchone()[0]
                    )
                    manifest = self._stage_review_snapshot(
                        parent_snapshot_id=active,
                        snapshot_id=snapshot_id,
                        operation_id=operation_id,
                        audit_position=audit_position,
                        review_case_id=None,
                        decision_id="",
                        action="activity_derivation_version",
                        selected_measurement_version_id=None,
                        candidate_version_ids=(),
                        replacement_plausibility_cases=(),
                    )
                    self._activate_review_snapshot(
                        operation_id,
                        snapshot_id,
                        active,
                        manifest,
                        created_at,
                        activation_kind="rule_version",
                    )
                    self._metadata.execute(
                        "UPDATE activity_derivation_snapshot_bindings SET version_id = ? "
                        "WHERE snapshot_id = ?",
                        (version_id, str(snapshot_id)),
                    )
        except Exception:
            shutil.rmtree(self._root / "staging" / str(operation_id), ignore_errors=True)
            if snapshot_id is not None:
                shutil.rmtree(
                    self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id),
                    ignore_errors=True,
                )
            raise
        return snapshot_id

    def load_effective_confirmation_case_ids(self) -> frozenset[str]:
        self._require_open()
        return frozenset(
            str(row[0])
            for row in self._metadata.execute(
                "SELECT d.review_case_id FROM data_review_decisions d "
                "WHERE d.case_kind = 'plausibility' AND d.action = 'confirm' "
                "AND NOT EXISTS (SELECT 1 FROM metadata_tombstones t "
                "WHERE t.target_audit_event_id = d.audit_event_id)"
            ).fetchall()
        )

    def load_effective_decision_case_id(self, decision_id: str) -> str | None:
        self._require_open()
        row = self._metadata.execute(
            "SELECT d.review_case_id, d.logical_measurement_id "
            "FROM data_review_decisions d "
            "WHERE d.decision_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM metadata_tombstones t "
            "WHERE t.target_audit_event_id = d.audit_event_id) LIMIT 1",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        if row[0] is not None:
            return str(row[0])
        prior = self._metadata.execute(
            "SELECT d.review_case_id FROM data_review_decisions d "
            "JOIN audit_events e USING (audit_event_id) "
            "WHERE d.logical_measurement_id = ? AND d.review_case_id IS NOT NULL "
            "ORDER BY e.audit_position DESC LIMIT 1",
            (str(row[1]),),
        ).fetchone()
        return None if prior is None else str(prior[0])

    def publish_historical_review(
        self, publication: HistoricalReviewPublication
    ) -> HistoricalReviewResult:
        self._require_open()
        self._require_writer()
        operation_id = publication.operation_id
        base_snapshot_id = publication.base_snapshot_id
        if self.load_active_snapshot_id() != base_snapshot_id:
            raise StoreError("Historische Reproduktionsbasis ist nicht mehr aktiv.")
        if not any(
            rule.version_id == publication.rule_version_id
            for rule in self.load_plausibility_rule_versions()
        ):
            raise StoreError("Historische Regelversion fehlt.")
        snapshot_id = SnapshotId(uuid4().hex)
        cycle_id = StoredReviewCycleId(uuid4().hex)
        created_at = datetime.now(UTC).isoformat()
        audit_position = int(
            self._metadata.execute(
                "SELECT COALESCE(MAX(audit_position), 0) FROM audit_events"
            ).fetchone()[0]
        )
        try:
            with self._metadata:
                self._metadata.execute(
                    "INSERT INTO write_operations VALUES "
                    "(?, 'run_historical_review', ?, ?, 'committed', 1)",
                    (str(operation_id), created_at, created_at),
                )
                manifest = self._stage_review_snapshot(
                    parent_snapshot_id=base_snapshot_id,
                    snapshot_id=snapshot_id,
                    operation_id=operation_id,
                    audit_position=audit_position,
                    review_case_id="",
                    decision_id="",
                    action="historical",
                    selected_measurement_version_id=None,
                    candidate_version_ids=(),
                    replacement_plausibility_cases=publication.open_cases,
                )
                self._activate_review_snapshot(
                    operation_id,
                    snapshot_id,
                    base_snapshot_id,
                    manifest,
                    created_at,
                    activation_kind="historical",
                )
                self._metadata.execute(
                    "INSERT INTO review_cycles VALUES (?, ?, 'historical', ?, ?)",
                    (
                        str(cycle_id),
                        str(snapshot_id),
                        publication.cycle_status,
                        len(publication.open_cases),
                    ),
                )
                self._metadata.execute(
                    "INSERT INTO historical_review_cycles VALUES (?, ?, ?, ?, ?)",
                    (
                        str(cycle_id),
                        str(base_snapshot_id),
                        publication.start_date.isoformat(),
                        publication.end_date.isoformat(),
                        publication.rule_version_id,
                    ),
                )
                self._metadata.executemany(
                    "INSERT INTO review_cycle_cases VALUES (?, ?)",
                    ((str(cycle_id), case.review_case_id) for case in publication.reproduced_cases),
                )
        except Exception:
            shutil.rmtree(self._root / "staging" / str(operation_id), ignore_errors=True)
            shutil.rmtree(
                self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id),
                ignore_errors=True,
            )
            raise
        return HistoricalReviewResult(
            operation_id, cycle_id, snapshot_id, len(publication.open_cases)
        )

    def create_plausibility_rule_version(
        self,
        *,
        operation_id: OperationId,
        version_id: str,
        data_type: str,
        unit: str,
        fixed_lower_bound: float | None,
        fixed_upper_bound: float | None,
        personal_range_enabled: bool,
        effective_from: datetime | None,
        effective_timezone: str | None,
        effective_offset_minutes: int | None,
        recommendation_id: str | None,
        replaced_measurement_version_ids: tuple[str, ...],
        cases: tuple[OpenDataReviewCase, ...],
        cycle_status: Literal["open", "closed"],
    ) -> tuple[PlausibilityRuleRecord, SnapshotId | None]:
        self._require_open()
        self._require_writer()
        existing = tuple(
            rule for rule in self.load_plausibility_rule_versions() if rule.data_type == data_type
        )
        if (not existing) != (effective_from is None):
            raise StoreError("Nur die erste Regelversion darf ohne Gültigkeitsbeginn sein.")
        if existing and effective_from is not None:
            latest = existing[-1].effective_from
            if latest is not None and effective_from <= latest:
                raise StoreError("Regelgültigkeitsgrenzen müssen streng steigen.")
        created_at = datetime.now(UTC)
        active = self.load_active_snapshot_id()
        snapshot_id = None if active is None else SnapshotId(uuid4().hex)
        manifest_sha256 = None
        audit_position = int(
            self._metadata.execute(
                "SELECT COALESCE(MAX(audit_position), 0) FROM audit_events"
            ).fetchone()[0]
        )
        try:
            with self._metadata:
                self._metadata.execute(
                    "INSERT INTO write_operations VALUES "
                    "(?, 'create_plausibility_rule_version', ?, ?, 'committed', 1)",
                    (str(operation_id), created_at.isoformat(), created_at.isoformat()),
                )
                self._metadata.execute(
                    "INSERT INTO rule_version_refs VALUES (?, 'plausibility')", (version_id,)
                )
                self._metadata.execute(
                    "INSERT INTO plausibility_rule_versions VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        version_id,
                        data_type,
                        unit,
                        fixed_lower_bound,
                        fixed_upper_bound,
                        int(personal_range_enabled),
                        None if effective_from is None else effective_from.isoformat(),
                        effective_timezone,
                        effective_offset_minutes,
                        created_at.isoformat(),
                        recommendation_id,
                    ),
                )
                if active is not None and snapshot_id is not None:
                    manifest_sha256 = self._stage_review_snapshot(
                        parent_snapshot_id=active,
                        snapshot_id=snapshot_id,
                        operation_id=operation_id,
                        audit_position=audit_position,
                        review_case_id="",
                        decision_id="",
                        action="rule_version",
                        selected_measurement_version_id=None,
                        candidate_version_ids=(),
                        replacement_plausibility_cases=cases,
                        replaced_measurement_version_ids=replaced_measurement_version_ids,
                    )
                    assert manifest_sha256 is not None
                    self._activate_review_snapshot(
                        operation_id,
                        snapshot_id,
                        active,
                        manifest_sha256,
                        created_at.isoformat(),
                        activation_kind="rule_version",
                    )
                    cycle_id = uuid4().hex
                    self._metadata.execute(
                        "INSERT INTO review_cycles VALUES (?, ?, 'rule_version', ?, ?)",
                        (cycle_id, str(snapshot_id), cycle_status, len(cases)),
                    )
                    self._metadata.executemany(
                        "INSERT INTO review_cycle_cases VALUES (?, ?)",
                        ((cycle_id, case.review_case_id) for case in cases),
                    )
        except Exception:
            shutil.rmtree(self._root / "staging" / str(operation_id), ignore_errors=True)
            if snapshot_id is not None:
                shutil.rmtree(
                    self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id),
                    ignore_errors=True,
                )
            raise
        record = PlausibilityRuleRecord(
            version_id,
            data_type,
            unit,
            fixed_lower_bound,
            fixed_upper_bound,
            personal_range_enabled,
            effective_from,
            created_at,
            recommendation_id,
            effective_timezone,
            effective_offset_minutes,
        )
        return record, snapshot_id

    def load_active_plausibility_facts(
        self,
    ) -> tuple[
        SnapshotId | None,
        tuple[MeasurementVersionFact, ...],
        tuple[ResolvedMeasurement, ...],
    ]:
        self._require_open()
        active = self.load_active_snapshot_id()
        if active is None:
            return None, (), ()
        directory = self._root / _PARQUET_DIRECTORY / "snapshots" / str(active)
        versions_path = str(directory / "measurement_versions.parquet").replace("'", "''")
        resolved_path = str(directory / "resolved_measurements.parquet").replace("'", "''")
        versions = tuple(
            MeasurementVersionFact(
                measurement_version_id=str(row[0]),
                logical_measurement_id=str(row[1]),
                canonical_type=str(row[2]),
                canonical_unit=str(row[3]),
                canonical_value=float(row[4]),
                source_start_utc=str(row[5]),
                source_end_utc=str(row[6]),
                source_updated_at_utc=str(row[7]),
                source_version=str(row[8]),
                source_name=str(row[9]),
                device=str(row[10]),
                strong_source_id_hash=None if row[11] is None else str(row[11]),
                measurement_local_date=row[12],
            )
            for row in self._query.execute(
                "SELECT measurement_version_id, identity_candidate_id, canonical_type, "
                "canonical_unit, canonical_value, source_start_utc, source_end_utc, "
                "source_updated_at_utc, source_version, source_name, device, "
                "strong_source_id_hash, measurement_local_date "
                f"FROM read_parquet('{versions_path}')"
            ).fetchall()
        )
        measurements = tuple(
            ResolvedMeasurement(*row)
            for row in self._query.execute(
                f"SELECT * FROM read_parquet('{resolved_path}')"
            ).fetchall()
        )
        return active, versions, measurements

    def load_measurement_version_fact(
        self, measurement_version_id: MeasurementVersionId
    ) -> MeasurementVersionFact | None:
        self._require_open()
        active = self.load_active_snapshot_id()
        if active is None:
            return None
        path = (
            self._root
            / _PARQUET_DIRECTORY
            / "snapshots"
            / str(active)
            / "measurement_versions.parquet"
        )
        escaped = str(path).replace("'", "''")
        row = self._query.execute(
            f"SELECT measurement_version_id, identity_candidate_id, canonical_type, "
            f"canonical_unit, canonical_value, source_start_utc, source_end_utc, "
            f"source_updated_at_utc, source_version, source_name, device, "
            f"strong_source_id_hash, measurement_local_date "
            f"FROM read_parquet('{escaped}') "
            "WHERE measurement_version_id = ?",
            (str(measurement_version_id),),
        ).fetchone()
        if row is None:
            return None
        return MeasurementVersionFact(
            measurement_version_id=str(row[0]),
            logical_measurement_id=str(row[1]),
            canonical_type=str(row[2]),
            canonical_unit=str(row[3]),
            canonical_value=float(row[4]),
            source_start_utc=str(row[5]),
            source_end_utc=str(row[6]),
            source_updated_at_utc=str(row[7]),
            source_version=str(row[8]),
            source_name=str(row[9]),
            device=str(row[10]),
            strong_source_id_hash=None if row[11] is None else str(row[11]),
            measurement_local_date=row[12],
        )

    def load_resolved_measurement(self, logical_measurement_id: str) -> ResolvedMeasurement | None:
        self._require_open()
        active = self.load_active_snapshot_id()
        if active is None:
            return None
        path = (
            self._root
            / _PARQUET_DIRECTORY
            / "snapshots"
            / str(active)
            / "resolved_measurements.parquet"
        )
        escaped = str(path).replace("'", "''")
        row = self._query.execute(
            f"SELECT * FROM read_parquet('{escaped}') WHERE logical_measurement_id = ?",
            (logical_measurement_id,),
        ).fetchone()
        return None if row is None else ResolvedMeasurement(*row)

    def load_source_type_for_review_case(self, review_case_id: str) -> str | None:
        self._require_open()
        row = self._metadata.execute(
            "SELECT source_type FROM source_type_catalog WHERE review_case_id = ?",
            (review_case_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    def load_source_conflict_candidates(
        self, logical_measurement_id: LogicalMeasurementId
    ) -> tuple[MeasurementVersionId, ...]:
        self._require_open()
        snapshot_id = self.load_active_snapshot_id()
        if snapshot_id is None:
            return ()
        path = (
            self._root
            / _PARQUET_DIRECTORY
            / "snapshots"
            / str(snapshot_id)
            / "measurement_versions.parquet"
        )
        escaped = str(path).replace("'", "''")
        rows = self._query.execute(
            f"""
            WITH seed AS (
                SELECT canonical_type, source_start_utc, source_end_utc, source_name, device
                FROM read_parquet('{escaped}')
                WHERE identity_candidate_id = ?
            )
            SELECT DISTINCT candidates.measurement_version_id
            FROM read_parquet('{escaped}') AS candidates
            WHERE candidates.identity_candidate_id = ?
               OR (candidates.canonical_type, candidates.source_start_utc,
                   candidates.source_end_utc, candidates.source_name, candidates.device)
                  IN (SELECT * FROM seed)
               OR EXISTS (
                    SELECT 1 FROM seed
                    WHERE candidates.canonical_type IN (
                        'apple_exercise_time', 'step_count',
                        'walking_running_distance', 'active_energy'
                    )
                      AND candidates.canonical_type = seed.canonical_type
                      AND candidates.source_start_utc < candidates.source_end_utc
                      AND seed.source_start_utc < seed.source_end_utc
                      AND candidates.source_start_utc < seed.source_end_utc
                      AND candidates.source_end_utc > seed.source_start_utc
                      AND CASE
                            WHEN candidates.source_name = 'Apple Watch'
                             AND candidates.device = 'Apple Watch'
                                THEN 'watch'
                            WHEN candidates.source_name = 'iPhone' AND candidates.device = 'iPhone'
                                THEN 'iphone'
                            WHEN candidates.source_name != ''
                              OR candidates.device != '' THEN 'other'
                            ELSE 'unknown'
                          END = CASE
                            WHEN seed.source_name = 'Apple Watch' AND seed.device = 'Apple Watch'
                                THEN 'watch'
                            WHEN seed.source_name = 'iPhone' AND seed.device = 'iPhone'
                                THEN 'iphone'
                            WHEN seed.source_name != '' OR seed.device != '' THEN 'other'
                            ELSE 'unknown'
                          END
               )
            ORDER BY measurement_version_id
            """,
            (str(logical_measurement_id), str(logical_measurement_id)),
        ).fetchall()
        return tuple(MeasurementVersionId(str(row[0])) for row in rows)

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

    def load_import_details(self, import_id: ImportId) -> StoredImportDetails | None:
        self._require_open()
        try:
            row = self._metadata.execute(
                """
                SELECT imports.package_record_count, imports.record_count,
                       counts.logical_measurement_count, counts.measurement_version_count,
                       counts.source_occurrence_count, counts.anomaly_count
                FROM imports
                JOIN import_canonical_counts AS counts USING (import_id)
                WHERE import_id = ?
                """,
                (str(import_id),),
            ).fetchone()
            if row is None:
                return None
            return StoredImportDetails(
                int(row[0]),
                int(row[1]),
                int(row[2]),
                int(row[3]),
                int(row[4]),
                int(row[5]),
                tuple(
                    UnsupportedImportContent(category, str(identifier), int(count))
                    for category, identifier, count in self._metadata.execute(
                        """
                        SELECT category, external_identifier, count
                        FROM unsupported_import_content
                        WHERE import_id = ?
                        ORDER BY category, external_identifier
                        """,
                        (str(import_id),),
                    ).fetchall()
                ),
            )
        except sqlite3.Error as error:
            raise StoreError("Importdetails sind nicht verfügbar.") from error

    def load_active_snapshot_id(self) -> SnapshotId | None:
        self._require_open()
        row = self._metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone()
        return None if row is None else SnapshotId(str(row[0]))

    def load_active_snapshot_schema_version(self) -> int | None:
        row = self._metadata.execute(
            "SELECT snapshot_schema_version FROM dataset_snapshots "
            "JOIN active_snapshot USING (snapshot_id) WHERE singleton = 1"
        ).fetchone()
        return None if row is None else int(row[0])

    def load_active_snapshot_as_of(self) -> datetime | None:
        active = self.load_active_snapshot_id()
        if active is None:
            return None
        try:
            manifest = json.loads(
                (
                    self._root / _PARQUET_DIRECTORY / "snapshots" / str(active) / "manifest.json"
                ).read_bytes()
            )
            binding = manifest.get("snapshot_binding")
            raw_value = (
                binding.get("snapshot_as_of")
                if isinstance(binding, dict)
                else manifest.get("created_at_utc")
            )
            value = datetime.fromisoformat(str(raw_value))
        except (OSError, AttributeError, ValueError, json.JSONDecodeError) as error:
            raise StoreError("Snapshot-Stichtag ist ungültig.") from error
        if value.tzinfo is None:
            raise StoreError("Snapshot-Stichtag ist ungültig.")
        return value

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
            model_maturity=ModelMaturityStatus(str(model_maturity)),
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
                    model_maturity, data_status, data_status_reasons, maturity_criteria,
                    reproducibility, diagnostics,
                    status, completed_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'running', ?
                )
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
                    result.model_maturity.value,
                    result.data_status.value,
                    json.dumps(
                        [
                            {
                                "code": reason.code.value,
                                "evidence_ids": reason.evidence_ids,
                            }
                            for reason in result.data_status_reasons
                        ],
                        sort_keys=True,
                    ),
                    json.dumps(
                        [
                            {
                                "code": criterion.code.value,
                                "passed": criterion.passed,
                                "observed_value": criterion.observed_value,
                                "threshold": criterion.threshold,
                            }
                            for criterion in result.maturity_criteria
                        ],
                        sort_keys=True,
                    ),
                    result.reproducibility.value,
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
                            "input_completeness": result.diagnostics.input_completeness,
                            "outcome_standard_deviation": (
                                result.diagnostics.outcome_standard_deviation
                            ),
                            "maximum_time_series_gap_days": (
                                result.diagnostics.maximum_time_series_gap_days
                            ),
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
                            "minimum_input_completeness": (
                                result.methodology.minimum_input_completeness
                            ),
                            "max_feature_dependency": (result.methodology.max_feature_dependency),
                            "minimum_bootstrap_success_rate": (
                                result.methodology.minimum_bootstrap_success_rate
                            ),
                            "minimum_outcome_standard_deviation": (
                                result.methodology.minimum_outcome_standard_deviation
                            ),
                            "maximum_time_series_gap_days": (
                                result.methodology.maximum_time_series_gap_days
                            ),
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
                   analysis_runs.code_diff_hash, analysis_runs.environment_lock_hash,
                   analysis_runs.model_maturity, analysis_runs.data_status,
                   analysis_runs.data_status_reasons, analysis_runs.maturity_criteria,
                   analysis_runs.reproducibility, analysis_runs.completed_at
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
        return self._load_resting_hr_analysis_row(row, AnalysisFreshness.CURRENT)

    def load_resting_hr_analysis_history(
        self, start_date: date | None, end_date: date | None
    ) -> tuple[RestingHeartRateAnalysisResult, ...]:
        self._require_open()
        rows = self._metadata.execute(
            """
            SELECT analysis_runs.analysis_run_id, analysis_runs.result_id,
                   analysis_runs.snapshot_id, analysis_runs.analysis_definition_id,
                   analysis_runs.config_hash, analysis_runs.config_schema_version,
                   analysis_runs.code_commit, analysis_runs.code_dirty,
                   analysis_runs.code_diff_hash, analysis_runs.environment_lock_hash,
                   analysis_runs.model_maturity, analysis_runs.data_status,
                   analysis_runs.data_status_reasons, analysis_runs.maturity_criteria,
                   analysis_runs.reproducibility, analysis_runs.completed_at
            FROM analysis_runs, active_snapshot
            WHERE analysis_runs.status = 'completed'
              AND analysis_runs.snapshot_id != active_snapshot.snapshot_id
              AND analysis_runs.analysis_start_date IS ?
              AND analysis_runs.analysis_end_date IS ?
            ORDER BY analysis_runs.completed_at DESC, analysis_runs.rowid DESC
            """,
            (
                start_date.isoformat() if start_date else None,
                end_date.isoformat() if end_date else None,
            ),
        ).fetchall()
        return tuple(
            self._load_resting_hr_analysis_row(row, AnalysisFreshness.STALE) for row in rows
        )

    def _load_resting_hr_analysis_row(
        self, row: tuple[object, ...], freshness: AnalysisFreshness
    ) -> RestingHeartRateAnalysisResult:
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
            model_maturity=ModelMaturityStatus(str(row[10])),
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
                input_completeness=cast(float | None, diagnostic_values.get("input_completeness")),
                outcome_standard_deviation=cast(
                    float | None, diagnostic_values.get("outcome_standard_deviation")
                ),
                maximum_time_series_gap_days=cast(
                    int | None, diagnostic_values.get("maximum_time_series_gap_days")
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
                minimum_input_completeness=cast(
                    float | None, methodology_values.get("minimum_input_completeness")
                ),
                max_feature_dependency=cast(
                    float | None, methodology_values.get("max_feature_dependency")
                ),
                minimum_bootstrap_success_rate=cast(
                    float | None,
                    methodology_values.get("minimum_bootstrap_success_rate"),
                ),
                minimum_outcome_standard_deviation=cast(
                    float | None,
                    methodology_values.get("minimum_outcome_standard_deviation"),
                ),
                maximum_time_series_gap_days=cast(
                    int | None, methodology_values.get("maximum_time_series_gap_days")
                ),
            ),
            provenance=(
                None if not row[4] or not row[6] or not row[9] else _analysis_provenance(row)
            ),
            data_status=DataQualityStatus(str(row[11])),
            data_status_reasons=tuple(
                AnalysisDataStatusReason(
                    DataStatusReasonCode(str(reason["code"])),
                    tuple(cast(list[str], reason["evidence_ids"])),
                )
                for reason in cast(list[dict[str, object]], json.loads(str(row[12])))
            ),
            maturity_criteria=tuple(
                ModelMaturityCriterion(
                    ModelMaturityCriterionCode(str(criterion["code"])),
                    bool(criterion["passed"]),
                    cast(float | str, criterion["observed_value"]),
                    cast(float | str, criterion["threshold"]),
                )
                for criterion in cast(list[dict[str, object]], json.loads(str(row[13])))
            ),
            freshness=freshness,
            reproducibility=ReproducibilityStatus(str(row[14])),
            completed_at=datetime.fromisoformat(str(row[15])),
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
                for row in self._metadata.execute("SELECT snapshot_id FROM dataset_snapshots")
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
