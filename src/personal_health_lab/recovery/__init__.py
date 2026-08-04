"""Point-in-time metadata backup policy."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

from personal_health_lab.data_quality import load_review_backup_facts
from personal_health_lab.health_data import (
    CanonicalHealthRecord,
    LogicalMeasurementId,
    MeasurementVersionId,
)
from personal_health_lab.migration import plan_backup_migration
from personal_health_lab.storage import (
    CapacityCheck,
    DataMode,
    ExportFact,
    LocalStore,
    MeasurementVersionFact,
    OpenDataReviewCase,
    PlausibilityRuleRecord,
    ResolvedMeasurement,
    RestoreSessionFacts,
    SourceOccurrenceFact,
    SourceResolution,
    SourceResolver,
    StoreError,
    StoreId,
    current_store_schema_version,
    probe_capacity,
)

_BACKUP_SCHEMA_VERSION = 3
_METHOD_ID = "metadata-backup/v1"
_RESTORE_START_METHOD_ID = "restore-start/v1"
_RESTORE_SOURCE_METHOD_ID = "restore-source-import/v1"
_RESTORE_ACTIVATE_METHOD_ID = "restore-activate/v1"
_IDENTITY_RULE_VERSION = "healthkit-identity/v3"
_SUPPORTED_IDENTITY_RULE_VERSIONS = {"healthkit-natural/v2", _IDENTITY_RULE_VERSION}
_MAPPING_RULE_VERSION = "healthkit-canonical/v2"
_SUPPORTED_MAPPING_RULE_VERSIONS = {"healthkit-canonical/v1", _MAPPING_RULE_VERSION}
_DIRECTORY_OVERHEAD = 64 * 1024
_MINIMUM_ESTIMATE = 2 * 1024**2
_RESTORE_DIRECTORY = "recovery"
_EMPTY_RESTORE_STORE_PATHS = {
    ".writer.lock",
    "metadata.sqlite3",
    "parquet",
    "query.duckdb",
}

_METADATA_TABLES = (
    "write_operations",
    "exports",
    "decision_refs",
    "rule_version_refs",
    "plausibility_rule_versions",
    "source_type_catalog",
    "review_cycles",
    "review_cycle_cases",
    "historical_review_cycles",
    "audit_events",
    "import_publications",
    "migration_publications",
    "data_review_decisions",
    "data_review_batch_actions",
    "data_review_batch_members",
    "metadata_tombstones",
    "source_absence_suppressions",
    "restored_publications",
)
_CONTENT_TABLES = (
    *_METADATA_TABLES,
    "import_refs",
    "snapshot_refs",
    "review_case_facts",
    "review_case_reasons",
    "required_source_refs",
    "resolved_overlay_facts",
    "open_review_overlay_facts",
)
_LEGACY_CONTENT_TABLES = tuple(
    table
    for table in _CONTENT_TABLES
    if table
    not in {
        "source_absence_suppressions",
        "restored_publications",
        "required_source_refs",
        "resolved_overlay_facts",
        "open_review_overlay_facts",
    }
)


@dataclass(frozen=True, slots=True)
class BackupId:
    _value: str

    def __post_init__(self) -> None:
        if len(self._value) != 32 or not set(self._value) <= set("0123456789abcdef"):
            raise ValueError("Sicherungs-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self._value


class MetadataBackupStatus(StrEnum):
    COMPLETED = "completed"
    NO_OP = "no_op"


class MetadataRestoreStatus(StrEnum):
    PENDING = "pending"
    ABORTED = "aborted"
    NO_OP = "no_op"


@dataclass(frozen=True, slots=True)
class RestoreId:
    _value: str

    def __post_init__(self) -> None:
        if len(self._value) != 32 or not set(self._value) <= set("0123456789abcdef"):
            raise ValueError("Wiederherstellungs-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self._value


@dataclass(frozen=True, slots=True)
class MetadataRestoreInspection:
    restore_id: RestoreId
    backup_id: BackupId
    source_store_id: StoreId
    original_backup_sha256: str
    canonical_content_sha256: str
    working_copy_sha256: str | None
    audit_max_position: int
    source_schema_version: int
    target_schema_version: int
    migration_steps: tuple[tuple[int, int], ...]
    status: MetadataRestoreStatus = MetadataRestoreStatus.PENDING


@dataclass(frozen=True, slots=True)
class MetadataBackup:
    backup_id: BackupId
    canonical_content_sha256: str
    audit_max_position: int
    created_at_utc: datetime
    target_file: str
    status: MetadataBackupStatus


@dataclass(frozen=True, slots=True)
class RestoreSourceInspection:
    complete: bool
    state_hash: str
    required_count: int
    matched_count: int


def _payload_sha256(
    data_type: str,
    unit: str,
    value: float,
    start: str,
    end: str,
    source_name: str,
    device: str,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "type": data_type,
                "unit": unit,
                "value": value,
                "start": start,
                "end": end,
                "source": source_name,
                "device": device,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _record_payload_sha256(record: CanonicalHealthRecord) -> str:
    return _payload_sha256(
        record.data_type.value,
        record.unit.value,
        record.value,
        record.source_start.astimezone(UTC).isoformat(),
        record.source_end.astimezone(UTC).isoformat(),
        record.provenance.source_name,
        record.provenance.device,
    )


def _fact_payload_sha256(fact: MeasurementVersionFact) -> str:
    return _payload_sha256(
        fact.canonical_type,
        fact.canonical_unit,
        fact.canonical_value,
        fact.source_start_utc,
        fact.source_end_utc,
        fact.source_name,
        fact.device,
    )


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _canonical_hash(
    connection: sqlite3.Connection, tables: tuple[str, ...] = _CONTENT_TABLES
) -> str:
    digest = hashlib.sha256()
    for table in tables:
        columns = tuple(
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")
        )
        digest.update(
            json.dumps([table, *columns], separators=(",", ":"), ensure_ascii=False).encode()
        )
        order = ", ".join(_quoted(column) for column in columns)
        for row in connection.execute(
            f"SELECT {order} FROM {_quoted(table)} ORDER BY {order}"
        ).fetchall():
            digest.update(
                json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode()
            )
    return digest.hexdigest()


def _audit_is_valid(connection: sqlite3.Connection, audit_max: int) -> bool:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    restored_count = (
        "+ count(restored_publications.audit_event_id) "
        if "restored_publications" in tables
        else ""
    )
    restored_join = (
        "LEFT JOIN restored_publications USING (audit_event_id)"
        if "restored_publications" in tables
        else ""
    )
    audit_query = (
        "SELECT count(*), COALESCE(MIN(audit_position), 1), "
        "COALESCE(MAX(audit_position), 0), "
        "count(import_publications.audit_event_id) "
        "+ count(data_review_decisions.audit_event_id) "
        "+ count(metadata_tombstones.audit_event_id) "
        "+ count(migration_publications.audit_event_id) "
        + restored_count
        + "FROM audit_events "
        "LEFT JOIN import_publications USING (audit_event_id) "
        "LEFT JOIN data_review_decisions USING (audit_event_id) "
        "LEFT JOIN metadata_tombstones USING (audit_event_id) "
        "LEFT JOIN migration_publications USING (audit_event_id) "
        + restored_join
    )
    audit = connection.execute(audit_query).fetchone()
    if audit is None or tuple(map(int, audit)) != (audit_max, 1, audit_max, audit_max):
        return False
    invalid_tombstone = connection.execute(
        "SELECT 1 FROM metadata_tombstones tombstone "
        "JOIN audit_events event ON event.audit_event_id = tombstone.audit_event_id "
        "JOIN audit_events target ON target.audit_event_id = tombstone.target_audit_event_id "
        "LEFT JOIN audit_events replacement "
        "ON replacement.audit_event_id = tombstone.replacement_audit_event_id "
        "WHERE target.audit_position >= event.audit_position "
        "OR replacement.audit_position >= event.audit_position LIMIT 1"
    ).fetchone()
    missing_reference = connection.execute(
        "SELECT 1 FROM import_publications publication "
        "LEFT JOIN import_refs import_ref ON import_ref.import_id = publication.import_id "
        "LEFT JOIN snapshot_refs snapshot_ref "
        "ON snapshot_ref.snapshot_id = publication.snapshot_id "
        "WHERE import_ref.import_id IS NULL OR snapshot_ref.snapshot_id IS NULL "
        "UNION ALL "
        "SELECT 1 FROM migration_publications publication "
        "LEFT JOIN snapshot_refs snapshot_ref "
        "ON snapshot_ref.snapshot_id = publication.snapshot_id "
        "WHERE publication.snapshot_id IS NOT NULL AND snapshot_ref.snapshot_id IS NULL "
        "UNION ALL "
        "SELECT 1 FROM review_cycle_cases cycle_case "
        "JOIN review_cycles cycle USING (cycle_id) "
        "LEFT JOIN review_case_facts fact "
        "ON fact.snapshot_id = cycle.snapshot_id "
        "AND fact.review_case_id = cycle_case.review_case_id "
        "WHERE fact.review_case_id IS NULL LIMIT 1"
    ).fetchone()
    return invalid_tombstone is None and missing_reference is None


def _source(store: LocalStore) -> sqlite3.Connection:
    source = sqlite3.connect(":memory:")
    try:
        store.copy_metadata_tables(source, _METADATA_TABLES)

        source.execute(
            "CREATE TABLE import_refs ("
            "import_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, status TEXT NOT NULL, "
            "snapshot_id TEXT NOT NULL, record_count INTEGER NOT NULL, "
            "committed_at_utc TEXT NOT NULL) STRICT"
        )
        source.executemany(
            "INSERT INTO import_refs VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    str(fact.import_id),
                    str(fact.operation_id),
                    fact.status,
                    str(fact.snapshot_id),
                    fact.record_count,
                    fact.committed_at.isoformat(),
                )
                for fact in store.load_backup_import_facts()
            ),
        )
        source.execute(
            "CREATE TABLE snapshot_refs ("
            "snapshot_id TEXT PRIMARY KEY, snapshot_schema_version INTEGER NOT NULL, "
            "created_by_operation_id TEXT NOT NULL, parent_snapshot_id TEXT, "
            "created_at_utc TEXT NOT NULL) STRICT"
        )
        source.executemany(
            "INSERT INTO snapshot_refs VALUES (?, ?, ?, ?, ?)",
            (
                (
                    str(fact.snapshot_id),
                    fact.schema_version,
                    str(fact.created_by_operation_id),
                    (
                        None
                        if fact.parent_snapshot_id is None
                        else str(fact.parent_snapshot_id)
                    ),
                    fact.created_at_utc.isoformat(),
                )
                for fact in store.load_backup_snapshot_facts()
            ),
        )

        source.execute(
            "CREATE TABLE review_case_facts ("
            "snapshot_id TEXT NOT NULL, review_case_id TEXT NOT NULL, "
            "case_kind TEXT NOT NULL, "
            "logical_measurement_id TEXT, measurement_version_id TEXT, "
            "rule_version_id TEXT, evidence_fingerprint TEXT NOT NULL, "
            "source_type TEXT, measured_at_utc TEXT, effective_value REAL, "
            "effective_value_source TEXT, canonical_unit TEXT, "
            "PRIMARY KEY (snapshot_id, review_case_id)) STRICT"
        )
        source.execute(
            "CREATE TABLE review_case_reasons ("
            "snapshot_id TEXT NOT NULL, review_case_id TEXT NOT NULL, "
            "reason_ordinal INTEGER NOT NULL, "
            "reason_kind TEXT NOT NULL, lower_bound REAL NOT NULL, upper_bound REAL, "
            "canonical_unit TEXT NOT NULL, "
            "PRIMARY KEY (snapshot_id, review_case_id, reason_ordinal)) STRICT"
        )
        facts = load_review_backup_facts(store)
        source.executemany(
            "INSERT INTO review_case_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    str(fact.snapshot_id),
                    fact.review_case_id,
                    fact.case_kind,
                    (
                        None
                        if fact.logical_measurement_id is None
                        else str(fact.logical_measurement_id)
                    ),
                    (
                        None
                        if fact.measurement_version_id is None
                        else str(fact.measurement_version_id)
                    ),
                    fact.rule_version_id,
                    fact.evidence_fingerprint,
                    fact.source_type,
                    (
                        None
                        if fact.measured_at_utc is None
                        else fact.measured_at_utc.isoformat()
                    ),
                    fact.effective_value,
                    fact.effective_value_source,
                    fact.canonical_unit,
                )
                for fact in facts
            ),
        )
        source.executemany(
            "INSERT INTO review_case_reasons VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    str(fact.snapshot_id),
                    fact.review_case_id,
                    ordinal,
                    reason.code,
                    reason.lower_bound,
                    reason.upper_bound,
                    reason.unit,
                )
                for fact in facts
                for ordinal, reason in enumerate(fact.reasons)
            ),
        )
        source.execute(
            "CREATE TABLE required_source_refs ("
            "logical_measurement_id TEXT NOT NULL, measurement_version_id TEXT PRIMARY KEY, "
            "payload_sha256 TEXT NOT NULL) STRICT"
        )
        source.execute(
            "CREATE TABLE resolved_overlay_facts ("
            "logical_measurement_id TEXT PRIMARY KEY, "
            "selected_measurement_version_id TEXT NOT NULL, disposition TEXT NOT NULL, "
            "effective_value REAL, canonical_unit TEXT NOT NULL, "
            "effective_value_source TEXT NOT NULL, effective_decision_id TEXT, "
            "correction_decision_id TEXT, source_deletion_decision_id TEXT, "
            "conflict_resolution_decision_id TEXT) STRICT"
        )
        source.execute(
            "CREATE TABLE open_review_overlay_facts ("
            "review_case_id TEXT PRIMARY KEY, case_kind TEXT NOT NULL, "
            "logical_measurement_id TEXT, measurement_version_id TEXT, "
            "rule_version_id TEXT, evidence_fingerprint TEXT NOT NULL) STRICT"
        )
        active = store.load_active_snapshot_id()
        active_facts = next(
            (
                snapshot
                for snapshot in store.load_review_snapshot_facts()
                if snapshot.snapshot_id == active
            ),
            None,
        )
        if active_facts is not None:
            source.executemany(
                "INSERT INTO required_source_refs VALUES (?, ?, ?)",
                (
                    (
                        version.logical_measurement_id,
                        version.measurement_version_id,
                        _fact_payload_sha256(version),
                    )
                    for version in active_facts.versions
                ),
            )
            source.executemany(
                "INSERT INTO resolved_overlay_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
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
                    for item in active_facts.measurements
                ),
            )
            source.executemany(
                "INSERT INTO open_review_overlay_facts VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        item.review_case_id,
                        item.kind,
                        (
                            None
                            if item.logical_measurement_id is None
                            else str(item.logical_measurement_id)
                        ),
                        (
                            None
                            if item.measurement_version_id is None
                            else str(item.measurement_version_id)
                        ),
                        item.rule_version_id,
                        item.evidence_fingerprint,
                    )
                    for item in active_facts.cases
                ),
            )
        return source
    except Exception:
        source.close()
        raise


def _describe(store: LocalStore, target_path: Path, source: sqlite3.Connection) -> MetadataBackup:
    identity = store.load_identity()
    if identity.store_id is None:
        raise StoreError("Datenspeicheridentität fehlt.")
    canonical_hash = _canonical_hash(source)
    backup_id = BackupId(
        hashlib.sha256(
            f"{identity.store_id}:{target_path.resolve()}:{canonical_hash}".encode()
        ).hexdigest()[:32]
    )
    audit_max = store.load_backup_audit_position()
    return MetadataBackup(
        backup_id,
        canonical_hash,
        audit_max,
        datetime.now(UTC),
        target_path.name,
        MetadataBackupStatus.COMPLETED,
    )


def describe_metadata_backup(store: LocalStore, target_path: Path) -> MetadataBackup:
    source = _source(store)
    try:
        return _describe(store, target_path, source)
    finally:
        source.close()


def _round_up(value: int, fragment_size: int) -> int:
    return ((value + fragment_size - 1) // fragment_size) * fragment_size


def _probe_metadata_backup_capacity(target_path: Path, estimate: int | None) -> CapacityCheck:
    return probe_capacity(target_path.parent, estimate, method_id=_METHOD_ID)


def preflight_metadata_backup(store: LocalStore, target_path: Path) -> CapacityCheck:
    source: sqlite3.Connection | None = None
    try:
        source = _source(store)
        encoded_bytes = sum(
            len(json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode())
            for table in _CONTENT_TABLES
            for row in source.execute(f"SELECT * FROM {_quoted(table)}").fetchall()
        )
        fragment_size = os.statvfs(target_path.parent).f_frsize
    except (OSError, sqlite3.Error, StoreError, TypeError, ValueError):
        return _probe_metadata_backup_capacity(target_path, None)
    finally:
        if source is not None:
            source.close()
    estimate = _round_up(
        max(_MINIMUM_ESTIMATE, 2 * encoded_bytes + _DIRECTORY_OVERHEAD), fragment_size
    )
    return _probe_metadata_backup_capacity(target_path, estimate)


def _backup_file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _is_timestamp(value: str) -> bool:
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _migration_steps(source_version: int) -> tuple[tuple[int, int], ...]:
    if source_version > _BACKUP_SCHEMA_VERSION:
        raise StoreError("backup_schema_newer")
    steps = plan_backup_migration(source_version, _BACKUP_SCHEMA_VERSION)
    if steps is None:
        raise StoreError("backup_migration_missing")
    return steps


def _relative_store_paths(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def _restore_fault_point(target_root: Path, fault_point_id: str) -> None:
    """Private fault-injection seam for durable restore transitions."""


def _read_restore_backup(path: Path) -> tuple[BackupId, StoreId, str, int, int]:
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as backup:
            row = backup.execute(
                "SELECT backup_id, store_id, canonical_content_sha256, "
                "audit_max_position, backup_schema_version, source_store_schema_version, "
                "created_at_utc, identity_rule_version_id, mapping_rule_version_id "
                "FROM backup_manifest "
                "WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise StoreError("backup_manifest_invalid")
            backup_id = BackupId(str(row[0]))
            store_id = StoreId(str(row[1]))
            canonical_hash = str(row[2])
            audit_max = int(row[3])
            schema_version = int(row[4])
            if schema_version > _BACKUP_SCHEMA_VERSION:
                raise StoreError("backup_schema_newer")
            source_store_schema = int(row[5])
            tables = {
                str(item[0])
                for item in backup.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            content_tables = (
                _CONTENT_TABLES
                if set(_CONTENT_TABLES) <= tables
                else _LEGACY_CONTENT_TABLES
            )
            expected_tables = {*content_tables, "backup_manifest"}
            if schema_version >= 2:
                expected_tables.add("backup_migration_provenance")
            if (
                schema_version <= 0
                or source_store_schema <= 0
                or source_store_schema > current_store_schema_version()
                or not _is_timestamp(str(row[6]))
                or str(row[7]) not in _SUPPORTED_IDENTITY_RULE_VERSIONS
                or str(row[8]) not in _SUPPORTED_MAPPING_RULE_VERSIONS
                or len(canonical_hash) != 64
                or not set(canonical_hash) <= set("0123456789abcdef")
                or audit_max < 0
                or tables != expected_tables
                or backup.execute("PRAGMA integrity_check").fetchone() != ("ok",)
                or backup.execute("PRAGMA foreign_key_check").fetchall()
                or _canonical_hash(backup, content_tables) != canonical_hash
                or not _audit_is_valid(backup, audit_max)
            ):
                raise StoreError("backup_integrity_conflict")
            if schema_version >= 2:
                provenance = backup.execute(
                    "SELECT original_backup_id, original_content_sha256, "
                    "source_schema_version, target_schema_version "
                    "FROM backup_migration_provenance WHERE singleton = 1"
                ).fetchone()
                if (
                    provenance is None
                    or str(provenance[0]) != str(backup_id)
                    or str(provenance[1]) != canonical_hash
                    or int(provenance[2]) not in {1, 2, 3}
                    or int(provenance[3]) != schema_version
                ):
                    raise StoreError("backup_integrity_conflict")
            return backup_id, store_id, canonical_hash, audit_max, schema_version
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise StoreError("backup_integrity_conflict") from error


def _inspection_from_session(
    facts: RestoreSessionFacts, status: MetadataRestoreStatus
) -> MetadataRestoreInspection:
    return MetadataRestoreInspection(
        RestoreId(facts.restore_id),
        BackupId(facts.backup_id),
        StoreId(facts.source_store_id),
        facts.original_backup_sha256,
        facts.canonical_content_sha256,
        facts.working_copy_sha256,
        facts.audit_max_position,
        facts.source_schema_version,
        facts.target_schema_version,
        tuple(
            (version, version + 1)
            for version in range(facts.source_schema_version, facts.target_schema_version)
        ),
        status,
    )


def inspect_metadata_restore(
    store: LocalStore, backup_path: Path, target_root: Path
) -> MetadataRestoreInspection:
    if store.load_identity().mode is not DataMode.REAL:
        raise StoreError("real_store_required")
    if backup_path == target_root or target_root in backup_path.parents:
        raise StoreError("restore_backup_inside_store")
    backup_id, source_store_id, canonical_hash, audit_max, source_version = _read_restore_backup(
        backup_path
    )
    original_hash = _backup_file_sha256(backup_path)
    pending = store.load_restore_session()
    if pending is not None:
        if (
            pending.backup_id != str(backup_id)
            or pending.canonical_content_sha256 != canonical_hash
        ):
            raise StoreError("restore_backup_conflict")
        return _inspection_from_session(pending, MetadataRestoreStatus.NO_OP)
    completed = store.load_completed_restore(str(backup_id), canonical_hash)
    if completed is not None:
        return _inspection_from_session(completed, MetadataRestoreStatus.NO_OP)
    target_identity = store.load_identity()
    if target_identity.store_id == source_store_id:
        current = describe_metadata_backup(store, backup_path)
        if current.backup_id == backup_id and current.canonical_content_sha256 == canonical_hash:
            return MetadataRestoreInspection(
                RestoreId(str(backup_id)),
                backup_id,
                source_store_id,
                original_hash,
                canonical_hash,
                original_hash,
                audit_max,
                source_version,
                _BACKUP_SCHEMA_VERSION,
                _migration_steps(source_version),
                MetadataRestoreStatus.NO_OP,
            )
    steps = _migration_steps(source_version)
    assert target_identity.store_id is not None
    restore_id = RestoreId(
        hashlib.sha256(
            f"{target_identity.store_id}:{backup_id}:{canonical_hash}".encode()
        ).hexdigest()[:32]
    )
    if not store.is_empty_for_restore():
        raise StoreError("restore_store_not_empty")
    restore_root = f"{_RESTORE_DIRECTORY}/{restore_id}"
    base = _EMPTY_RESTORE_STORE_PATHS
    recovery = {_RESTORE_DIRECTORY, restore_root}
    accepted_paths = (
        base,
        base | {_RESTORE_DIRECTORY},
        base | recovery,
        base | recovery | {f"{restore_root}/working.sqlite3.tmp"},
        base | recovery | {f"{restore_root}/working.sqlite3"},
    )
    if _relative_store_paths(target_root) not in accepted_paths:
        raise StoreError("restore_store_not_empty")
    return MetadataRestoreInspection(
        restore_id,
        backup_id,
        source_store_id,
        original_hash,
        canonical_hash,
        original_hash if not steps else None,
        audit_max,
        source_version,
        _BACKUP_SCHEMA_VERSION,
        steps,
    )


def preflight_metadata_restore_start(backup_path: Path, target_root: Path) -> CapacityCheck:
    try:
        allocated = backup_path.stat().st_blocks * 512
        fragment_size = os.statvfs(target_root).f_frsize
        estimate = _round_up(
            max(_MINIMUM_ESTIMATE, 2 * allocated + 256 * 1024 + _DIRECTORY_OVERHEAD),
            fragment_size,
        )
    except (OSError, TypeError, ValueError):
        estimate = None
    return probe_capacity(target_root, estimate, method_id=_RESTORE_START_METHOD_ID)


def load_restore_working_copy(store: LocalStore, target_root: Path) -> Path:
    session = load_metadata_restore(store)
    working = target_root / _RESTORE_DIRECTORY / str(session.restore_id) / "working.sqlite3"
    if (
        session.working_copy_sha256 is None
        or _backup_file_sha256(working) != session.working_copy_sha256
    ):
        raise StoreError("restore_working_copy_changed")
    return working


def load_restore_source_packages(store: LocalStore, target_root: Path) -> tuple[Path, ...]:
    source_root = load_restore_working_copy(store, target_root).parent / "sources"
    return tuple(sorted(source_root.glob("*.zip"))) if source_root.exists() else ()


def inspect_restore_sources(
    store: LocalStore,
    target_root: Path,
    records: tuple[CanonicalHealthRecord, ...],
) -> RestoreSourceInspection:
    working = load_restore_working_copy(store, target_root)
    with sqlite3.connect(f"{working.resolve().as_uri()}?mode=ro", uri=True) as backup:
        required = {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in backup.execute(
                "SELECT logical_measurement_id, measurement_version_id, payload_sha256 "
                "FROM required_source_refs"
            )
        }
    available = {
        (str(record.logical_measurement_id), str(version_id), _record_payload_sha256(record))
        for record in records
        for version_id in (
            record.measurement_version_id,
            *(
                ()
                if record.legacy_measurement_version_id is None
                else (record.legacy_measurement_version_id,)
            ),
        )
    }
    matched = required & available
    state_hash = hashlib.sha256(
        json.dumps(
            {"available": sorted(available), "required": sorted(required)},
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return RestoreSourceInspection(
        complete=matched == required,
        state_hash=state_hash,
        required_count=len(required),
        matched_count=len(matched),
    )


def select_restore_source_versions(
    store: LocalStore,
    target_root: Path,
    records: tuple[CanonicalHealthRecord, ...],
) -> tuple[CanonicalHealthRecord, ...]:
    working = load_restore_working_copy(store, target_root)
    with sqlite3.connect(f"{working.resolve().as_uri()}?mode=ro", uri=True) as backup:
        required = {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in backup.execute(
                "SELECT logical_measurement_id, measurement_version_id, payload_sha256 "
                "FROM required_source_refs"
            )
        }
    selected = []
    for record in records:
        payload = _record_payload_sha256(record)
        candidates = (
            record.measurement_version_id,
            *(
                ()
                if record.legacy_measurement_version_id is None
                else (record.legacy_measurement_version_id,)
            ),
        )
        version_id = next(
            (
                candidate
                for candidate in candidates
                if (str(record.logical_measurement_id), str(candidate), payload) in required
            ),
            record.measurement_version_id,
        )
        selected.append(replace(record, measurement_version_id=version_id))
    return tuple(selected)


def preflight_restore_source_import(
    store: LocalStore,
    target_root: Path,
    *,
    input_bytes: int,
    record_count: int,
    complete: bool,
) -> CapacityCheck:
    if complete:
        snapshot = store.preflight_full_snapshot_import(input_bytes, record_count)
        try:
            working = load_restore_working_copy(store, target_root)
            overlay_allocation = 2 * working.stat().st_blocks * 512
            fragment_size = os.statvfs(target_root).f_frsize
            estimate = (
                None
                if snapshot.estimate_bytes is None
                else _round_up(
                    snapshot.estimate_bytes
                    + overlay_allocation
                    + _DIRECTORY_OVERHEAD,
                    fragment_size,
                )
            )
        except (OSError, StoreError, TypeError, ValueError):
            estimate = None
        return probe_capacity(
            target_root,
            estimate,
            method_id=_RESTORE_ACTIVATE_METHOD_ID,
        )
    return probe_capacity(
        target_root,
        max(input_bytes, _MINIMUM_ESTIMATE),
        method_id=_RESTORE_SOURCE_METHOD_ID,
    )


def stage_restore_source_package(
    store: LocalStore,
    target_root: Path,
    package_path: Path,
    package_hash: str,
) -> None:
    # ponytail: keep validated ZIPs until activation; stage normalized rows only if
    # duplicate package allocation becomes material in real restore workloads.
    working_directory = load_restore_working_copy(store, target_root).parent
    source_root = working_directory / "sources"
    source_directory_created = not source_root.exists()
    source_root.mkdir(exist_ok=True)
    if source_directory_created:
        parent = os.open(working_directory, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    target = source_root / f"{package_hash}.zip"
    if target.exists():
        return
    temporary = source_root / f".{package_hash}.tmp"
    temporary.unlink(missing_ok=True)
    shutil.copyfile(package_path, temporary)
    with temporary.open("rb") as package:
        os.fsync(package.fileno())
    _restore_fault_point(target_root, "restore-source.before_publish/v1")
    os.replace(temporary, target)
    directory = os.open(source_root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    _restore_fault_point(target_root, "restore-source.after_publish/v1")


def restore_source_resolver(path: Path) -> SourceResolver:
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as backup:
        measurements = tuple(
            ResolvedMeasurement(*row)
            for row in backup.execute("SELECT * FROM resolved_overlay_facts").fetchall()
        )
        review_cases = tuple(
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
                    str(row[1]),
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
            for row in backup.execute("SELECT * FROM open_review_overlay_facts").fetchall()
        )
    def resolve_restored_sources(
        *,
        occurrences: tuple[SourceOccurrenceFact, ...],
        versions: tuple[MeasurementVersionFact, ...],
        exports: tuple[ExportFact, ...],
        previous_measurements: tuple[ResolvedMeasurement, ...],
        previous_review_cases: tuple[OpenDataReviewCase, ...],
        governing_export_id: str,
        imported_measurement_version_ids: tuple[str, ...] = (),
        unknown_source_types: tuple[str, ...] = (),
        suppressed_deletion_ids: frozenset[str] = frozenset(),
        plausibility_rules: tuple[PlausibilityRuleRecord, ...] = (),
    ) -> SourceResolution:
        del (
            occurrences,
            versions,
            exports,
            previous_measurements,
            previous_review_cases,
            governing_export_id,
            imported_measurement_version_ids,
            unknown_source_types,
            suppressed_deletion_ids,
            plausibility_rules,
        )
        return SourceResolution(
            measurements=measurements,
            review_cases=review_cases,
            new_review_case_ids=(),
            source_type_requests=(),
            cycle_status="open" if review_cases else "closed",
            cycle_open_case_count=len(review_cases),
            anomaly_count=0,
        )

    return resolve_restored_sources


def _migrate_restore_working_copy(path: Path, inspection: MetadataRestoreInspection) -> None:
    for source, target in inspection.migration_steps:
        with sqlite3.connect(path) as working:
            if (source, target) == (1, 2):
                working.execute(
                    "CREATE TABLE backup_migration_provenance ("
                    "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                    "original_backup_id TEXT NOT NULL, original_content_sha256 TEXT NOT NULL, "
                    "source_schema_version INTEGER NOT NULL, "
                    "target_schema_version INTEGER NOT NULL) STRICT"
                )
                working.execute(
                    "INSERT INTO backup_migration_provenance VALUES (1, ?, ?, ?, ?)",
                    (
                        str(inspection.backup_id),
                        inspection.canonical_content_sha256,
                        source,
                        target,
                    ),
                )
            elif (source, target) == (2, 3):
                for definition in (
                    "CREATE TABLE IF NOT EXISTS source_absence_suppressions ("
                    "logical_measurement_id TEXT PRIMARY KEY, "
                    "decision_id TEXT NOT NULL, "
                    "active INTEGER NOT NULL) STRICT",
                    "CREATE TABLE IF NOT EXISTS restored_publications ("
                    "audit_event_id TEXT PRIMARY KEY) STRICT",
                    "CREATE TABLE IF NOT EXISTS required_source_refs ("
                    "logical_measurement_id TEXT NOT NULL, "
                    "measurement_version_id TEXT PRIMARY KEY, "
                    "payload_sha256 TEXT NOT NULL) STRICT",
                    "CREATE TABLE IF NOT EXISTS resolved_overlay_facts ("
                    "logical_measurement_id TEXT PRIMARY KEY, "
                    "selected_measurement_version_id TEXT NOT NULL, "
                    "disposition TEXT NOT NULL, effective_value REAL, "
                    "canonical_unit TEXT NOT NULL, effective_value_source TEXT NOT NULL, "
                    "effective_decision_id TEXT, correction_decision_id TEXT, "
                    "source_deletion_decision_id TEXT, "
                    "conflict_resolution_decision_id TEXT) STRICT",
                    "CREATE TABLE IF NOT EXISTS open_review_overlay_facts ("
                    "review_case_id TEXT PRIMARY KEY, case_kind TEXT NOT NULL, "
                    "logical_measurement_id TEXT, measurement_version_id TEXT, "
                    "rule_version_id TEXT, evidence_fingerprint TEXT NOT NULL) STRICT",
                ):
                    working.execute(definition)
                provenance = working.execute(
                    "SELECT source_schema_version FROM backup_migration_provenance "
                    "WHERE singleton = 1"
                ).fetchone()
                if provenance is not None and int(provenance[0]) < 3:
                    working.execute(
                        "INSERT OR IGNORE INTO required_source_refs VALUES (?, ?, ?)",
                        (
                            "legacy-source-references-unavailable",
                            "legacy-source-references-unavailable",
                            "!",
                        ),
                    )
                migrated_hash = _canonical_hash(working)
                working.execute(
                    "UPDATE backup_manifest SET canonical_content_sha256 = ? "
                    "WHERE singleton = 1",
                    (migrated_hash,),
                )
                working.execute(
                    "UPDATE backup_migration_provenance SET target_schema_version = 3 "
                    "WHERE singleton = 1"
                )
            else:
                raise StoreError("backup_migration_missing")
            working.execute(
                "UPDATE backup_manifest SET backup_schema_version = ? WHERE singleton = 1",
                (target,),
            )
            if working.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise StoreError("backup_migration_invalid")


def _restore_allocation_checkpoint(target_root: Path, phase: str) -> None:
    """Private test seam for restore-start/v1 target allocation."""


def begin_metadata_restore(
    store: LocalStore,
    backup_path: Path,
    target_root: Path,
    expected: MetadataRestoreInspection,
    operation_id: str,
) -> MetadataRestoreInspection:
    current = inspect_metadata_restore(store, backup_path, target_root)
    if current != expected:
        raise StoreError("restore_plan_changed")
    if current.status is MetadataRestoreStatus.NO_OP:
        return current
    working_directory = target_root / _RESTORE_DIRECTORY / str(current.restore_id)
    temporary = working_directory / "working.sqlite3.tmp"
    working = working_directory / "working.sqlite3"
    if (target_root / _RESTORE_DIRECTORY).exists():
        shutil.rmtree(target_root / _RESTORE_DIRECTORY)
    working_directory.mkdir(parents=True)
    session_started = False
    try:
        shutil.copyfile(backup_path, temporary)
        if _backup_file_sha256(temporary) != current.original_backup_sha256:
            raise StoreError("restore_plan_changed")
        _restore_allocation_checkpoint(target_root, "working_copy")
        _restore_fault_point(target_root, "restore.after_working_copy/v1")
        _migrate_restore_working_copy(temporary, current)
        _restore_allocation_checkpoint(target_root, "migrated_copy")
        with temporary.open("rb") as file:
            os.fsync(file.fileno())
        os.replace(temporary, working)
        directory = os.open(working_directory, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _restore_allocation_checkpoint(target_root, "published_copy")
        _restore_fault_point(target_root, "restore.after_working_copy_publish/v1")
        working_hash = _backup_file_sha256(working)
        with sqlite3.connect(f"{working.resolve().as_uri()}?mode=ro", uri=True) as migrated:
            manifest = migrated.execute(
                "SELECT backup_schema_version, canonical_content_sha256 "
                "FROM backup_manifest WHERE singleton = 1"
            ).fetchone()
            provenance = migrated.execute(
                "SELECT original_backup_id, original_content_sha256, target_schema_version "
                "FROM backup_migration_provenance WHERE singleton = 1"
            ).fetchone()
            if (
                manifest is None
                or int(manifest[0]) != _BACKUP_SCHEMA_VERSION
                or provenance is None
                or str(provenance[0]) != str(current.backup_id)
                or str(provenance[1]) != current.canonical_content_sha256
                or int(provenance[2]) != _BACKUP_SCHEMA_VERSION
                or migrated.execute("PRAGMA integrity_check").fetchone() != ("ok",)
                or _canonical_hash(migrated) != str(manifest[1])
            ):
                raise StoreError("backup_migration_invalid")
        completed = MetadataRestoreInspection(
            current.restore_id,
            current.backup_id,
            current.source_store_id,
            current.original_backup_sha256,
            current.canonical_content_sha256,
            working_hash,
            current.audit_max_position,
            current.source_schema_version,
            current.target_schema_version,
            current.migration_steps,
        )
        store.start_restore_session(
            RestoreSessionFacts(
                str(completed.restore_id),
                operation_id,
                str(completed.backup_id),
                str(completed.source_store_id),
                completed.original_backup_sha256,
                completed.canonical_content_sha256,
                working_hash,
                completed.audit_max_position,
                completed.source_schema_version,
                completed.target_schema_version,
                None,
            )
        )
        session_started = True
        _restore_allocation_checkpoint(target_root, "pending_catalog")
        _restore_fault_point(target_root, "restore.after_pending_catalog/v1")
        return completed
    except Exception:
        if not session_started:
            shutil.rmtree(target_root / _RESTORE_DIRECTORY, ignore_errors=True)
        raise


def load_metadata_restore(store: LocalStore) -> MetadataRestoreInspection:
    facts = store.load_restore_session()
    if facts is None:
        raise StoreError("restore_not_pending")
    return _inspection_from_session(facts, MetadataRestoreStatus.PENDING)


def validate_metadata_restore_abort(
    store: LocalStore, target_root: Path
) -> MetadataRestoreInspection:
    inspection = load_metadata_restore(store)
    restore_root = f"{_RESTORE_DIRECTORY}/{inspection.restore_id}"
    expected = {
        *_EMPTY_RESTORE_STORE_PATHS,
        _RESTORE_DIRECTORY,
        restore_root,
        f"{restore_root}/working.sqlite3",
    }
    paths = _relative_store_paths(target_root)
    source_root = f"{restore_root}/sources"
    unexpected = paths - expected
    if unexpected and not (
        source_root in unexpected
        and all(
            path == source_root
            or (
                path.startswith(f"{source_root}/")
                and (
                    (path.endswith(".zip") and len(Path(path).stem) == 64)
                    or (
                        path.endswith(".tmp")
                        and Path(path).name.startswith(".")
                        and len(Path(path).name) == 69
                    )
                )
            )
            for path in unexpected
        )
    ):
        raise StoreError("restore_store_contains_unexpected_files")
    return inspection


def stage_metadata_restore_abort(target_root: Path, restore_id: RestoreId) -> Path:
    """Atomically detach the pending store while its writer lock is still held."""

    discarded = target_root.with_name(f".healthlab-quarantine-restore-{restore_id}")
    if discarded.exists():
        raise StoreError("restore_abort_staging_exists")
    try:
        os.replace(target_root, discarded)
        _restore_fault_point(discarded, "restore.after_abort_detach/v1")
        parent = os.open(target_root.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except OSError as error:
        raise StoreError("restore_abort_failed") from error
    return discarded


def _allocation_checkpoint(target_directory: Path, phase: str) -> None:
    """Private test seam for the versioned writer's live target allocation."""


def create_metadata_backup(store: LocalStore, target_path: Path) -> MetadataBackup:
    identity = store.validate_recovery_writer()
    source = _source(store)
    try:
        description = _describe(store, target_path, source)
        source_hash = description.canonical_content_sha256
        backup_id = str(description.backup_id)
        if target_path.exists():
            try:
                with sqlite3.connect(
                    f"{target_path.resolve().as_uri()}?mode=ro", uri=True
                ) as existing:
                    row = existing.execute(
                        "SELECT backup_id, canonical_content_sha256, audit_max_position, "
                        "created_at_utc, backup_schema_version, source_store_schema_version, "
                        "store_id, identity_rule_version_id, mapping_rule_version_id "
                        "FROM backup_manifest WHERE singleton = 1"
                    ).fetchone()
                    valid = (
                        row is not None
                        and tuple(map(str, row[:2])) == (backup_id, source_hash)
                        and int(row[4]) == _BACKUP_SCHEMA_VERSION
                        and str(row[5]) == identity.schema_version
                        and str(row[6]) == str(identity.store_id)
                        and str(row[7]) == _IDENTITY_RULE_VERSION
                        and str(row[8]) == _MAPPING_RULE_VERSION
                        and existing.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                        and _canonical_hash(existing) == source_hash
                        and _audit_is_valid(existing, int(row[2]))
                    )
                    if valid:
                        assert row is not None
                        return MetadataBackup(
                            description.backup_id,
                            source_hash,
                            int(row[2]),
                            datetime.fromisoformat(str(row[3])),
                            target_path.name,
                            MetadataBackupStatus.NO_OP,
                        )
            except sqlite3.Error:
                pass
            raise StoreError("Sicherungsziel enthält widersprüchlichen Inhalt.")

        created_at = datetime.now(UTC)
        temporary = target_path.with_name(f".{target_path.name}.{backup_id}.tmp")
        if temporary.exists():
            raise StoreError("Sicherungszwischenstand existiert bereits.")
        try:
            with sqlite3.connect(temporary) as backup:
                backup.execute("PRAGMA journal_mode = OFF")
                backup.execute("PRAGMA synchronous = OFF")
                for table in _CONTENT_TABLES:
                    columns = tuple(source.execute(f"PRAGMA table_info({_quoted(table)})"))
                    declaration = ", ".join(
                        f"{_quoted(str(column[1]))} {str(column[2]) or 'BLOB'}"
                        for column in columns
                    )
                    backup.execute(f"CREATE TABLE {_quoted(table)} ({declaration}) STRICT")
                    rows = source.execute(f"SELECT * FROM {_quoted(table)}").fetchall()
                    if rows:
                        placeholders = ", ".join("?" for _ in columns)
                        backup.executemany(
                            f"INSERT INTO {_quoted(table)} VALUES ({placeholders})", rows
                        )
                backup.execute(
                    "CREATE TABLE backup_manifest ("
                    "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                    "backup_id TEXT NOT NULL, backup_schema_version INTEGER NOT NULL, "
                    "source_store_schema_version INTEGER NOT NULL, store_id TEXT NOT NULL, "
                    "created_at_utc TEXT NOT NULL, audit_max_position INTEGER NOT NULL, "
                    "identity_rule_version_id TEXT NOT NULL, "
                    "mapping_rule_version_id TEXT NOT NULL, "
                    "canonical_content_sha256 TEXT NOT NULL) STRICT"
                )
                backup.execute(
                    "CREATE TABLE backup_migration_provenance ("
                    "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                    "original_backup_id TEXT NOT NULL, "
                    "original_content_sha256 TEXT NOT NULL, "
                    "source_schema_version INTEGER NOT NULL, "
                    "target_schema_version INTEGER NOT NULL) STRICT"
                )
                if _canonical_hash(backup) != source_hash:
                    raise StoreError("Kanonischer Sicherungsinhalt ist unvollständig.")
                backup.execute(
                    "INSERT INTO backup_manifest VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        backup_id,
                        _BACKUP_SCHEMA_VERSION,
                        int(identity.schema_version),
                        str(identity.store_id),
                        created_at.isoformat(),
                        description.audit_max_position,
                        _IDENTITY_RULE_VERSION,
                        _MAPPING_RULE_VERSION,
                        source_hash,
                    ),
                )
                backup.execute(
                    "INSERT INTO backup_migration_provenance VALUES (1, ?, ?, ?, ?)",
                    (backup_id, source_hash, _BACKUP_SCHEMA_VERSION, _BACKUP_SCHEMA_VERSION),
                )
                if backup.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise StoreError("Sicherungsintegrität konnte nicht bestätigt werden.")
            with temporary.open("rb") as file:
                os.fsync(file.fileno())
            _allocation_checkpoint(target_path.parent, "temporary")
            os.replace(temporary, target_path)
            directory = os.open(target_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            _allocation_checkpoint(target_path.parent, "published")
        except (OSError, sqlite3.Error, StoreError):
            temporary.unlink(missing_ok=True)
            raise
        return MetadataBackup(
            description.backup_id,
            source_hash,
            description.audit_max_position,
            created_at,
            target_path.name,
            MetadataBackupStatus.COMPLETED,
        )
    finally:
        source.close()


__all__ = [
    "BackupId",
    "MetadataBackup",
    "MetadataBackupStatus",
    "MetadataRestoreInspection",
    "MetadataRestoreStatus",
    "RestoreId",
    "RestoreSourceInspection",
    "begin_metadata_restore",
    "create_metadata_backup",
    "describe_metadata_backup",
    "inspect_metadata_restore",
    "inspect_restore_sources",
    "load_metadata_restore",
    "load_restore_source_packages",
    "load_restore_working_copy",
    "preflight_metadata_backup",
    "preflight_metadata_restore_start",
    "preflight_restore_source_import",
    "restore_source_resolver",
    "select_restore_source_versions",
    "stage_metadata_restore_abort",
    "stage_restore_source_package",
    "validate_metadata_restore_abort",
]
