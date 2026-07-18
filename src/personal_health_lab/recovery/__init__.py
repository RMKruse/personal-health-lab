"""Point-in-time metadata backup policy."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from personal_health_lab.data_quality import load_review_backup_facts
from personal_health_lab.storage import CapacityCheck, LocalStore, StoreError, probe_capacity

_BACKUP_SCHEMA_VERSION = 1
_METHOD_ID = "metadata-backup/v1"
_IDENTITY_RULE_VERSION = "healthkit-natural/v2"
_MAPPING_RULE_VERSION = "healthkit-canonical/v1"
_DIRECTORY_OVERHEAD = 64 * 1024
_MINIMUM_ESTIMATE = 2 * 1024**2

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
)
_CONTENT_TABLES = (
    *_METADATA_TABLES,
    "import_refs",
    "snapshot_refs",
    "review_case_facts",
    "review_case_reasons",
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


@dataclass(frozen=True, slots=True)
class MetadataBackup:
    backup_id: BackupId
    canonical_content_sha256: str
    audit_max_position: int
    created_at_utc: datetime
    target_file: str
    status: MetadataBackupStatus


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _canonical_hash(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table in _CONTENT_TABLES:
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
    audit = connection.execute(
        "SELECT count(*), COALESCE(MIN(audit_position), 1), "
        "COALESCE(MAX(audit_position), 0), "
        "count(import_publications.audit_event_id) "
        "+ count(data_review_decisions.audit_event_id) "
        "+ count(metadata_tombstones.audit_event_id) "
        "+ count(migration_publications.audit_event_id) "
        "FROM audit_events "
        "LEFT JOIN import_publications USING (audit_event_id) "
        "LEFT JOIN data_review_decisions USING (audit_event_id) "
        "LEFT JOIN metadata_tombstones USING (audit_event_id) "
        "LEFT JOIN migration_publications USING (audit_event_id)"
    ).fetchone()
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
    "create_metadata_backup",
    "describe_metadata_backup",
    "preflight_metadata_backup",
]
