from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import duckdb

from personal_health_lab import DataMode

_METADATA_FILE = "metadata.sqlite3"
_PARQUET_DIRECTORY = "parquet"


class StoreError(ValueError):
    """A local store cannot be opened safely."""


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

    def is_empty(self) -> bool:
        self._require_open()
        return not any((self._root / _PARQUET_DIRECTORY).rglob("*.parquet"))

    def close(self) -> None:
        if not self._closed:
            self._query.close()
            self._metadata.close()
            self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Datenspeicher ist geschlossen.")
