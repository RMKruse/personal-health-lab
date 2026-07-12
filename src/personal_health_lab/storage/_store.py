from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Self

import duckdb

from personal_health_lab import DataMode
from personal_health_lab.health_data import (
    CanonicalHealthRecord,
    CanonicalHealthType,
    CanonicalUnit,
    DailyHealthSeries,
    DailyHealthValue,
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
                    record_count INTEGER NOT NULL,
                    committed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS active_snapshot (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    snapshot_id TEXT NOT NULL
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
    ) -> None:
        self._require_open()
        try:
            self._publish_import(
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
    ) -> None:
        staging = self._root / "staging" / str(import_id)
        snapshot = self._root / _PARQUET_DIRECTORY / "snapshots" / str(snapshot_id)
        staging.mkdir(parents=True)
        parquet_path = staging / "samples.parquet"
        self._query.execute(
            """
            CREATE OR REPLACE TEMP TABLE staged_samples (
                data_type VARCHAR,
                canonical_unit VARCHAR,
                canonical_value DOUBLE,
                measurement_local_date DATE,
                source_start VARCHAR,
                source_end VARCHAR,
                source_name VARCHAR,
                source_version VARCHAR,
                device VARCHAR,
                original_value DOUBLE,
                original_unit VARCHAR
            )
            """
        )
        self._query.executemany(
            "INSERT INTO staged_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    record.data_type.value,
                    record.unit.value,
                    record.value,
                    record.measurement_local_day,
                    record.source_start.isoformat(),
                    record.source_end.isoformat(),
                    record.provenance.source_name,
                    record.provenance.source_version,
                    record.provenance.device,
                    record.provenance.original_value,
                    record.provenance.original_unit,
                )
                for record in records
            ],
        )
        escaped_path = str(parquet_path).replace("'", "''")
        self._query.execute(f"COPY staged_samples TO '{escaped_path}' (FORMAT PARQUET)")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(snapshot)
        with self._metadata:
            self._metadata.execute(
                """
                INSERT INTO imports VALUES (?, ?, ?, 'committed', ?, ?, ?)
                """,
                (
                    str(import_id),
                    str(operation_id),
                    package_hash,
                    str(snapshot_id),
                    len(records),
                    datetime.now().astimezone().isoformat(),
                ),
            )
            self._metadata.execute(
                """
                INSERT INTO active_snapshot(singleton, snapshot_id) VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET snapshot_id = excluded.snapshot_id
                """,
                (str(snapshot_id),),
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
            SELECT data_type, canonical_unit, measurement_local_date,
                   SUM(canonical_value),
                   list(source_start ORDER BY source_start),
                   list(DISTINCT source_name ORDER BY source_name)
            FROM read_parquet('{escaped_path}')
            {where}
            GROUP BY data_type, canonical_unit, measurement_local_date
            ORDER BY data_type, measurement_local_date
            """,
            parameters,
        ).fetchall()
        grouped: dict[tuple[CanonicalHealthType, CanonicalUnit], list[DailyHealthValue]] = {}
        for data_type, unit, day, value, source_starts, source_names in rows:
            key = (CanonicalHealthType(data_type), CanonicalUnit(unit))
            grouped.setdefault(key, []).append(
                DailyHealthValue(
                    day=day,
                    value=value,
                    source_starts=tuple(datetime.fromisoformat(item) for item in source_starts),
                    source_names=tuple(source_names),
                )
            )
        return tuple(
            DailyHealthSeries(data_type=data_type, unit=unit, values=tuple(values))
            for (data_type, unit), values in grouped.items()
        )

    def close(self) -> None:
        if not self._closed:
            self._query.close()
            self._metadata.close()
            self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Datenspeicher ist geschlossen.")
