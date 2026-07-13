from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, Literal, Self, cast

import duckdb

from personal_health_lab import DataMode
from personal_health_lab.health_data import (
    CanonicalHealthRecord,
    CanonicalHealthType,
    CanonicalUnit,
    DailyHealthSeries,
    DailyHealthValue,
    MeasurementVersionId,
)

_METADATA_FILE = "metadata.sqlite3"
_PARQUET_DIRECTORY = "parquet"
_QUERY_FILE = "query.duckdb"
_STORE_SCHEMA_VERSION = "1.1"
_WRITER_LOCK_FILE = ".writer.lock"


class StoreError(ValueError):
    """A local store cannot be opened safely."""


class StoreBusyError(StoreError):
    """Another process owns the store's writer lock."""


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
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProvenanceCounts:
    import_count: int
    package_count: int
    snapshot_count: int
    logical_measurement_count: int
    measurement_version_count: int
    quarantined_import_count: int


@dataclass(slots=True)
class LocalStore:
    _root: Path
    _mode: DataMode
    _metadata: sqlite3.Connection
    _query: duckdb.DuckDBPyConnection
    _writer_lock: IO[bytes] | None = None
    _closed: bool = False

    @classmethod
    def open(cls, root: Path, mode: DataMode) -> Self:
        writer_lock = cls._try_writer_lock(root)
        if writer_lock is None:
            return cls._open(root, mode, initialize=False)
        store: Self | None = None
        try:
            store = cls._open(root, mode, initialize=True)
            store._recover_imports()
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
            raise StoreError("Datenmodus muss 'synthetic' oder 'real' sein.")

        metadata: sqlite3.Connection | None = None
        try:
            metadata_path = root / _METADATA_FILE
            metadata = sqlite3.connect(
                metadata_path if initialize else f"{metadata_path.resolve().as_uri()}?mode=ro",
                uri=not initialize,
            )
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
                if existing_mode != mode.value or existing_schema not in {"1.0", "1.1"}:
                    raise StoreError("Datenspeicher gehört zu einem anderen Modus oder Schema.")
                if existing_schema == "1.0":
                    if not initialize:
                        raise StoreError("Datenspeicher benötigt eine Schema-Migration.")
                    backup_directory = root / "migration-backups"
                    backup_directory.mkdir(exist_ok=True)
                    backup_path = backup_directory / (
                        f"metadata-v1.0-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
                    )
                    with sqlite3.connect(backup_path) as backup:
                        metadata.backup(backup)
            if initialize:
                (root / _PARQUET_DIRECTORY).mkdir(exist_ok=True)
                metadata.execute(
                    """
                    CREATE TABLE IF NOT EXISTS store_identity (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        mode TEXT NOT NULL,
                        schema_version TEXT NOT NULL
                    )
                    """
                )
                metadata.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS imports (
                        import_id TEXT PRIMARY KEY,
                        operation_id TEXT NOT NULL,
                        package_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        snapshot_id TEXT NOT NULL,
                        package_record_count INTEGER NOT NULL,
                        record_count INTEGER NOT NULL,
                        committed_at TEXT NOT NULL,
                        diagnostics TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE IF NOT EXISTS active_snapshot (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        snapshot_id TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS import_measurement_versions (
                        import_id TEXT NOT NULL,
                        measurement_version_id TEXT NOT NULL,
                        PRIMARY KEY (import_id, measurement_version_id)
                    );
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
                columns = {
                    str(row[1]) for row in metadata.execute("PRAGMA table_info(imports)").fetchall()
                }
                if "diagnostics" not in columns:
                    metadata.execute(
                        "ALTER TABLE imports ADD COLUMN diagnostics TEXT NOT NULL DEFAULT ''"
                    )
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
                else:
                    analysis_columns = {
                        str(row[1]) for row in metadata.execute("PRAGMA table_info(analysis_runs)")
                    }
                    analysis_column_migrations = {
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
                    for column, declaration in analysis_column_migrations.items():
                        if column not in analysis_columns:
                            metadata.execute(
                                f"ALTER TABLE analysis_runs ADD COLUMN {column} {declaration}"
                            )
                if existing_identity is not None and str(existing_identity[1]) == "1.0":
                    metadata.execute(
                        "UPDATE store_identity SET schema_version = ? WHERE singleton = 1",
                        (_STORE_SCHEMA_VERSION,),
                    )
                metadata.commit()
            identity = metadata.execute(
                "SELECT mode, schema_version FROM store_identity WHERE singleton = 1"
            ).fetchone()
            expected_identity = (mode.value, _STORE_SCHEMA_VERSION)
            if identity is None:
                if not initialize:
                    raise StoreError("Datenspeicher ist noch nicht initialisiert.")
                metadata.execute(
                    "INSERT INTO store_identity(singleton, mode, schema_version) VALUES (1, ?, ?)",
                    expected_identity,
                )
                metadata.commit()
            elif identity != expected_identity:
                raise StoreError("Datenspeicher gehört zu einem anderen Modus oder Schema.")
            query_path = root / _QUERY_FILE
            if initialize and not query_path.exists():
                duckdb.connect(str(query_path)).close()
            query = duckdb.connect(str(query_path), config={"access_mode": "READ_ONLY"})
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
        with self._metadata:
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
        try:
            staging.mkdir(parents=True)
            self._write_manifest(
                staging,
                operation_id=operation_id,
                import_id=import_id,
                snapshot_id=snapshot_id,
                status="running",
            )
        except OSError as error:
            raise StoreError("Import-Staging konnte nicht angelegt werden.") from error

    def reject_import(self, import_id: ImportId, package_hash: str) -> None:
        self._require_writer()
        with self._metadata:
            self._metadata.execute(
                """
                UPDATE imports
                SET package_hash = ?, status = 'rejected', diagnostics = 'invalid_health_export'
                WHERE import_id = ?
                """,
                (package_hash, str(import_id)),
            )
        staging = self._root / "staging" / str(import_id)
        if staging.exists():
            self._remove_tree(staging)

    def publish_import(
        self,
        *,
        operation_id: OperationId,
        import_id: ImportId,
        package_hash: str,
        snapshot_id: SnapshotId,
        records: tuple[CanonicalHealthRecord, ...],
    ) -> PublishImportResult:
        self._require_open()
        self._require_writer()
        try:
            return self._publish_import(
                operation_id=operation_id,
                import_id=import_id,
                package_hash=package_hash,
                snapshot_id=snapshot_id,
                records=records,
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
        records: tuple[CanonicalHealthRecord, ...],
    ) -> PublishImportResult:
        observed_at = datetime.now().astimezone()
        duplicate = self._metadata.execute(
            """
            SELECT active_snapshot.snapshot_id
            FROM active_snapshot
            WHERE singleton = 1 AND EXISTS (
                SELECT 1 FROM imports
                WHERE package_hash = ? AND status = 'committed'
            )
            """,
            (package_hash,),
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
                logical_measurement_id VARCHAR,
                measurement_version_id VARCHAR,
                data_type VARCHAR,
                canonical_unit VARCHAR,
                canonical_value DOUBLE,
                measurement_local_date DATE,
                source_start VARCHAR,
                source_end VARCHAR,
                source_updated_at VARCHAR,
                source_name VARCHAR,
                source_version VARCHAR,
                device VARCHAR,
                original_value DOUBLE,
                original_unit VARCHAR,
                first_import_id VARCHAR,
                first_observed_at VARCHAR
            )
            """
        )
        self._query.executemany(
            """
            INSERT INTO staged_samples
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(record.logical_measurement_id),
                    str(record.measurement_version_id),
                    record.data_type.value,
                    record.unit.value,
                    record.value,
                    record.measurement_local_day,
                    record.source_start.isoformat(),
                    record.source_end.isoformat(),
                    record.source_updated_at.astimezone(UTC).isoformat(),
                    record.provenance.source_name,
                    record.provenance.source_version,
                    record.provenance.device,
                    record.provenance.original_value,
                    record.provenance.original_unit,
                    str(import_id),
                    observed_at.astimezone(UTC).isoformat(),
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
                self._root / _PARQUET_DIRECTORY / "snapshots" / str(active[0]) / "samples.parquet"
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
            "SELECT count(*), count(DISTINCT logical_measurement_id) FROM combined_samples"
        ).fetchone()
        assert count_row is not None
        version_count, logical_count = map(int, count_row)
        new_record_count = version_count - previous_count
        if new_record_count == 0:
            assert active is not None
            current_snapshot = SnapshotId(str(active[0]))
            result = PublishImportResult(
                status="duplicate",
                snapshot_id=current_snapshot,
                record_count=0,
                logical_measurement_count=logical_count,
                measurement_version_count=version_count,
                diagnostics=("no_new_measurement_versions",),
            )
            with self._metadata:
                self._record_import(
                    operation_id=operation_id,
                    import_id=import_id,
                    package_hash=package_hash,
                    snapshot_id=current_snapshot,
                    status=result.status,
                    package_record_count=len(records),
                    record_count=0,
                    records=records,
                )
            shutil.rmtree(self._root / "staging" / str(import_id), ignore_errors=True)
            return result

        parquet_path = staging / "samples.parquet"
        escaped_path = str(parquet_path).replace("'", "''")
        self._query.execute(
            f"""
            COPY (SELECT * EXCLUDE(source_priority) FROM combined_samples)
            TO '{escaped_path}' (FORMAT PARQUET)
            """
        )
        validation = self._query.execute(
            f"SELECT count(*) FROM read_parquet('{escaped_path}')"
        ).fetchone()
        if validation is None or int(validation[0]) != version_count:
            raise StoreError("Staging-Snapshot konnte nicht validiert werden.")
        self._write_manifest(
            staging,
            operation_id=operation_id,
            import_id=import_id,
            snapshot_id=snapshot_id,
            status="published",
            record_count=version_count,
        )
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(snapshot)
        with self._metadata:
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
                """
                INSERT INTO active_snapshot(singleton, snapshot_id) VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET snapshot_id = excluded.snapshot_id
                """,
                (str(snapshot_id),),
            )
        return PublishImportResult(
            status="committed",
            snapshot_id=snapshot_id,
            record_count=new_record_count,
            logical_measurement_count=logical_count,
            measurement_version_count=version_count,
        )

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
            self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id) / "samples.parquet"
        )
        escaped_path = str(parquet_path).replace("'", "''")
        count_row = self._query.execute(
            f"""
            SELECT count(*), count(DISTINCT logical_measurement_id)
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
            diagnostics=diagnostics,
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
        parquet_path = (
            self._root / _PARQUET_DIRECTORY / "snapshots" / str(row[0]) / "samples.parquet"
        )
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
        rows = self._query.execute(
            f"""
            WITH preferred_versions AS (
                SELECT * FROM read_parquet('{escaped_path}')
                QUALIFY row_number() OVER (
                    PARTITION BY logical_measurement_id
                    ORDER BY source_updated_at DESC, first_observed_at DESC,
                             source_version DESC, measurement_version_id DESC
                ) = 1
            ), projected_samples AS (
                SELECT * FROM preferred_versions WHERE data_type = 'active_energy'
                UNION ALL
                SELECT * FROM preferred_versions
                WHERE data_type = 'apple_resting_heart_rate'
                QUALIFY row_number() OVER (
                    PARTITION BY measurement_local_date
                    ORDER BY source_updated_at DESC, first_observed_at DESC,
                             source_version DESC, source_start DESC,
                             measurement_version_id DESC
                ) = 1
            )
            SELECT data_type, canonical_unit, measurement_local_date,
                   SUM(canonical_value),
                   list(source_start ORDER BY source_start),
                   list(DISTINCT source_name ORDER BY source_name),
                   list(measurement_version_id ORDER BY source_start),
                   list(source_updated_at ORDER BY source_start),
                   list(source_version ORDER BY source_start)
            FROM projected_samples
            {where}
            GROUP BY data_type, canonical_unit, measurement_local_date
            ORDER BY data_type, measurement_local_date
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
        ) in rows:
            key = (CanonicalHealthType(data_type), CanonicalUnit(unit))
            grouped.setdefault(key, []).append(
                DailyHealthValue(
                    day=day,
                    value=value,
                    source_starts=tuple(datetime.fromisoformat(item) for item in source_starts),
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
        ) = row
        result_id = str(result_id)
        snapshot_id = str(snapshot_id)
        definition_id = str(definition_id)
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
                None
                if not config_hash or not code_commit or not environment_lock_hash
                else AnalysisProvenance(
                    analysis_run_id=AnalysisRunId(str(analysis_run_id)),
                    result_id=AnalysisResultId(result_id),
                    snapshot_id=SnapshotId(snapshot_id),
                    analysis_definition_id=AnalysisDefinitionId(definition_id),
                    config_hash=str(config_hash),
                    config_schema_version=str(config_schema_version),
                    code_commit=str(code_commit),
                    code_dirty=bool(code_dirty),
                    code_diff_hash=None if code_diff_hash is None else str(code_diff_hash),
                    environment_lock_hash=str(environment_lock_hash),
                )
            ),
        )

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
        running = self._metadata.execute(
            "SELECT import_id, snapshot_id FROM imports WHERE status = 'running'"
        ).fetchall()
        for import_id, snapshot_id in running:
            staging = self._root / "staging" / str(import_id)
            if staging.exists():
                self._remove_tree(staging)
            if str(snapshot_id) != active_snapshot:
                snapshot = self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id)
                if snapshot.exists():
                    self._remove_tree(snapshot)
        if running:
            with self._metadata:
                self._metadata.execute(
                    """
                    UPDATE imports
                    SET status = 'quarantined', diagnostics = 'interrupted_before_publish'
                    WHERE status = 'running'
                    """
                )
        staging_root = self._root / "staging"
        if staging_root.exists():
            known_imports = {
                str(row[0]) for row in self._metadata.execute("SELECT import_id FROM imports")
            }
            for path in staging_root.iterdir():
                if path.is_dir():
                    if path.name not in known_imports:
                        self._quarantine_orphaned_staging(path)
                        continue
                    self._remove_tree(path)

    def _quarantine_orphaned_staging(self, path: Path) -> None:
        recovered_id = f"recovered-{path.name}"
        operation_id = snapshot_id = recovered_id
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            if isinstance(manifest, dict):
                operation_id = str(manifest.get("operation_id", recovered_id))
                snapshot_id = str(manifest.get("snapshot_id", recovered_id))
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        with self._metadata:
            self._metadata.execute(
                "INSERT INTO imports VALUES (?, ?, '', 'running', ?, 0, 0, ?, ?)",
                (
                    path.name,
                    operation_id,
                    snapshot_id,
                    datetime.now().astimezone().isoformat(),
                    "orphaned_staging_detected",
                ),
            )
        self._remove_tree(path)
        with self._metadata:
            self._metadata.execute(
                """
                UPDATE imports
                SET status = 'quarantined', diagnostics = 'orphaned_staging'
                WHERE import_id = ?
                """,
                (path.name,),
            )

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
