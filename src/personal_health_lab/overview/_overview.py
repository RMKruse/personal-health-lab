from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from personal_health_lab import DataMode
from personal_health_lab.health_data import DailyHealthSeries
from personal_health_lab.storage import LocalStore


class OverviewStatus(StrEnum):
    EMPTY = "empty"
    READY = "ready"
    PROVISIONAL = "provisional"


@dataclass(frozen=True, slots=True)
class OverviewSelection:
    start_date: date | None = None
    end_date: date | None = None

    def __post_init__(self) -> None:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("Startdatum darf nicht nach dem Enddatum liegen.")


@dataclass(frozen=True, slots=True)
class Overview:
    status: OverviewStatus
    selection: OverviewSelection
    message: str
    daily_series: tuple[DailyHealthSeries, ...] = ()
    import_count: int = 0
    package_count: int = 0
    snapshot_count: int = 0
    logical_measurement_count: int = 0
    measurement_version_count: int = 0
    schema_version: Literal["1.0"] = "1.0"


class OverviewReader:
    """Loads the only read projection available to production adapters."""

    def __init__(self, store: LocalStore) -> None:
        self._store = store

    @classmethod
    def open(cls, root: Path, mode: DataMode) -> Self:
        return cls(LocalStore.open(root=root, mode=mode))

    def close(self) -> None:
        self._store.close()

    def load(self, selection: OverviewSelection) -> Overview:
        daily_series = self._store.load_daily_series(selection.start_date, selection.end_date)
        counts = self._store.load_provenance_counts()
        if not daily_series:
            return Overview(
                status=OverviewStatus.EMPTY,
                selection=selection,
                message="Keine Gesundheitsdaten vorhanden.",
                import_count=counts.import_count,
                package_count=counts.package_count,
                snapshot_count=counts.snapshot_count,
                logical_measurement_count=counts.logical_measurement_count,
                measurement_version_count=counts.measurement_version_count,
            )
        return Overview(
            status=OverviewStatus.READY,
            selection=selection,
            message="Importierte tägliche Gesundheitsdaten sind verfügbar.",
            daily_series=daily_series,
            import_count=counts.import_count,
            package_count=counts.package_count,
            snapshot_count=counts.snapshot_count,
            logical_measurement_count=counts.logical_measurement_count,
            measurement_version_count=counts.measurement_version_count,
        )
