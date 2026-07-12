from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, Self

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


class StoreError(ValueError):
    """A local store cannot be opened safely."""


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


@dataclass(slots=True)
class LocalStore:
    _root: Path
    _mode: DataMode
    _metadata: sqlite3.Connection
    _query: duckdb.DuckDBPyConnection
    _closed: bool = False

    @classmethod
    def open(cls, root: Path, mode: DataMode) -> Self:
        if not isinstance(mode, DataMode):
            raise StoreError("Datenmodus muss 'synthetic' oder 'real' sein.")

        metadata: sqlite3.Connection | None = None
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / _PARQUET_DIRECTORY).mkdir(exist_ok=True)
            metadata = sqlite3.connect(root / _METADATA_FILE)
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
                    committed_at TEXT NOT NULL
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
                """
            )
            identity = metadata.execute(
                "SELECT mode, schema_version FROM store_identity WHERE singleton = 1"
            ).fetchone()
            expected_identity = (mode.value, "1.0")
            if identity is None:
                metadata.execute(
                    "INSERT INTO store_identity(singleton, mode, schema_version) VALUES (1, ?, ?)",
                    expected_identity,
                )
                metadata.commit()
            elif identity != expected_identity:
                raise StoreError("Datenspeicher gehört zu einem anderen Modus oder Schema.")
            query = duckdb.connect(":memory:")
        except StoreError:
            if metadata is not None:
                metadata.close()
            raise
        except (OSError, sqlite3.Error, duckdb.Error) as error:
            if metadata is not None:
                metadata.close()
            raise StoreError("Datenspeicher konnte nicht geöffnet werden.") from error

        return cls(_root=root, _mode=mode, _metadata=metadata, _query=query)

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
                self._root
                / _PARQUET_DIRECTORY
                / "snapshots"
                / str(active[0])
                / "samples.parquet"
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
            return result

        staging.mkdir(parents=True)
        parquet_path = staging / "samples.parquet"
        escaped_path = str(parquet_path).replace("'", "''")
        self._query.execute(
            f"""
            COPY (SELECT * EXCLUDE(source_priority) FROM combined_samples)
            TO '{escaped_path}' (FORMAT PARQUET)
            """
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
            "INSERT INTO imports VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(import_id),
                str(operation_id),
                package_hash,
                status,
                str(snapshot_id),
                package_record_count,
                record_count,
                datetime.now().astimezone().isoformat(),
            ),
        )
        self._metadata.executemany(
            "INSERT OR IGNORE INTO import_measurement_versions VALUES (?, ?)",
            {
                (str(import_id), str(record.measurement_version_id))
                for record in records
            },
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

    def load_provenance_counts(self) -> ProvenanceCounts:
        self._require_open()
        import_count, package_count, snapshot_count = self._metadata.execute(
            """
            SELECT count(*), count(DISTINCT package_hash),
                   count(DISTINCT CASE WHEN status = 'committed' THEN snapshot_id END)
            FROM imports
            """
        ).fetchone()
        active = self._metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone()
        if active is None:
            return ProvenanceCounts(import_count, package_count, snapshot_count, 0, 0)
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
        )

    def close(self) -> None:
        if not self._closed:
            self._query.close()
            self._metadata.close()
            self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Datenspeicher ist geschlossen.")
