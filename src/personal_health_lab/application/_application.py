from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

from personal_health_lab import DataMode
from personal_health_lab.health_import import (
    HealthImportError,
    ImportId,
    OperationId,
    SnapshotId,
    import_health_export,
)
from personal_health_lab.overview import Overview, OverviewReader, OverviewSelection
from personal_health_lab.resting_hr_analysis import (
    AnalysisDefinitionId,
    AnalysisError,
    AnalysisProvenance,
    AnalysisResultId,
    AnalysisRunId,
)
from personal_health_lab.resting_hr_analysis import (
    run_resting_hr_analysis as execute_analysis,
)

logger = logging.getLogger("personal_health_lab")
SnapshotRef = SnapshotId


class HealthLabError(Exception):
    """Base class for errors exposed by the application interface."""


class ConfigurationError(HealthLabError, ValueError):
    """The runtime configuration cannot safely open a HealthLab session."""


class FeatureNotAvailableError(HealthLabError):
    """The requested operation is part of the interface but not this tracer bullet."""


AnalysisResultRef = AnalysisResultId


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    mode: DataMode
    synthetic_store: Path
    real_store: Path
    schema_version: str = "1.0"
    max_import_package_bytes: int = 512 * 1024 * 1024
    max_import_entries: int = 8
    max_import_entry_bytes: int = 512 * 1024 * 1024
    max_import_uncompressed_bytes: int = 512 * 1024 * 1024
    max_import_compression_ratio: float = 200.0

    def __post_init__(self) -> None:
        if not isinstance(self.mode, DataMode):
            raise ConfigurationError("Datenmodus muss 'synthetic' oder 'real' sein.")
        if not isinstance(self.synthetic_store, Path) or not isinstance(self.real_store, Path):
            raise ConfigurationError("Datenspeicherorte müssen pathlib.Path-Werte sein.")
        synthetic_store = self.synthetic_store.expanduser().resolve()
        real_store = self.real_store.expanduser().resolve()
        object.__setattr__(self, "synthetic_store", synthetic_store)
        object.__setattr__(self, "real_store", real_store)

        if self.schema_version != "1.0":
            raise ConfigurationError("Unbekannte RuntimeConfig-Schemaversion.")
        limits = (
            self.max_import_package_bytes,
            self.max_import_entries,
            self.max_import_entry_bytes,
            self.max_import_uncompressed_bytes,
        )
        if any(type(limit) is not int or limit <= 0 for limit in limits):
            raise ConfigurationError("Importgrenzen müssen positive Ganzzahlen sein.")
        ratio = self.max_import_compression_ratio
        if type(ratio) not in (int, float) or not math.isfinite(ratio) or ratio < 1:
            raise ConfigurationError("Kompressionsverhältnis muss mindestens 1 sein.")
        if synthetic_store == real_store:
            raise ConfigurationError("Synthetischer und realer Datenspeicher müssen getrennt sein.")
        if synthetic_store in real_store.parents or real_store in synthetic_store.parents:
            raise ConfigurationError("Datenspeicher dürfen nicht ineinander liegen.")

    @property
    def active_store(self) -> Path:
        if self.mode is DataMode.SYNTHETIC:
            return self.synthetic_store
        return self.real_store


class ImportStatus(StrEnum):
    COMMITTED = "committed"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    STORE_BUSY = "store_busy"


@dataclass(frozen=True, slots=True)
class ImportReceipt:
    operation_id: OperationId
    import_id: ImportId
    status: ImportStatus
    package_hash: str
    snapshot_ref: SnapshotRef | None
    record_count: int
    anomaly_count: int
    package_record_count: int = 0
    logical_measurement_count: int = 0
    measurement_version_count: int = 0
    diagnostics: tuple[str, ...] = ()


class AnalysisStatus(StrEnum):
    COMPLETED = "completed"
    REUSED = "reused"
    INSUFFICIENT_DATA = "insufficient_data"
    UNSTABLE = "unstable"
    STORE_BUSY = "store_busy"


class ModelMaturityStatus(StrEnum):
    EXPLORATORY = "exploratory"
    ROBUST = "robust"


@dataclass(frozen=True, slots=True)
class RestingHeartRateAnalysisConfig:
    analysis_definition_id: AnalysisDefinitionId
    start_date: date | None = None
    end_date: date | None = None
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        if not isinstance(self.analysis_definition_id, AnalysisDefinitionId):
            raise ConfigurationError("analysis_definition_id hat einen ungültigen Typ.")
        if self.schema_version != "1.0":
            raise ConfigurationError("Unbekannte Analyseschemaversion.")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ConfigurationError("Startdatum darf nicht nach dem Enddatum liegen.")


@dataclass(frozen=True, slots=True)
class AnalysisReceipt:
    operation_id: OperationId
    analysis_run_id: AnalysisRunId
    status: AnalysisStatus
    snapshot_ref: SnapshotRef | None
    analysis_definition_id: AnalysisDefinitionId
    model_maturity: ModelMaturityStatus | None
    result_ref: AnalysisResultRef | None
    diagnostics: tuple[str, ...] = ()
    provenance: AnalysisProvenance | None = None


class HealthLab:
    """Small, deterministic lifecycle and operation seam for HealthLab."""

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._overview_reader: OverviewReader | None = None

    @classmethod
    def open(cls, config: RuntimeConfig) -> Self:
        return cls(config)

    def __enter__(self) -> Self:
        if self._overview_reader is not None:
            raise HealthLabError("HealthLab-Sitzung ist bereits geöffnet.")
        try:
            self._overview_reader = OverviewReader.open(
                root=self._config.active_store,
                mode=self._config.mode,
            )
        except ValueError as error:
            raise ConfigurationError(str(error)) from error
        except RuntimeError as error:
            raise HealthLabError("Datenspeicher konnte nicht geöffnet werden.") from error
        logger.info("healthlab_opened mode=%s", self._config.mode.value)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._overview_reader is not None:
            self._overview_reader.close()
            self._overview_reader = None
        logger.info("healthlab_closed mode=%s", self._config.mode.value)

    def import_health_export(self, package_path: Path) -> ImportReceipt:
        self._require_open()
        try:
            result = import_health_export(
                package_path,
                root=self._config.active_store,
                mode=self._config.mode,
                max_package_bytes=self._config.max_import_package_bytes,
                max_entries=self._config.max_import_entries,
                max_entry_bytes=self._config.max_import_entry_bytes,
                max_uncompressed_bytes=self._config.max_import_uncompressed_bytes,
                max_compression_ratio=self._config.max_import_compression_ratio,
            )
        except HealthImportError as error:
            raise HealthLabError("Health-Export konnte nicht importiert werden.") from error
        return ImportReceipt(
            operation_id=result.operation_id,
            import_id=result.import_id,
            status=ImportStatus(result.status),
            package_hash=result.package_hash,
            snapshot_ref=result.snapshot_id,
            record_count=result.record_count,
            anomaly_count=0,
            package_record_count=result.package_record_count,
            logical_measurement_count=result.logical_measurement_count,
            measurement_version_count=result.measurement_version_count,
            diagnostics=result.diagnostics,
        )

    def run_resting_hr_analysis(self, config: RestingHeartRateAnalysisConfig) -> AnalysisReceipt:
        self._require_open()
        try:
            result = execute_analysis(
                root=self._config.active_store,
                mode=self._config.mode,
                analysis_definition_id=config.analysis_definition_id,
                start_date=config.start_date,
                end_date=config.end_date,
                config_schema_version=config.schema_version,
            )
        except ValueError as error:
            raise ConfigurationError(str(error)) from error
        except AnalysisError as error:
            raise HealthLabError(str(error)) from error
        return AnalysisReceipt(
            operation_id=result.operation_id,
            analysis_run_id=result.analysis_run_id,
            status=AnalysisStatus(result.status),
            snapshot_ref=result.snapshot_id,
            analysis_definition_id=result.analysis_definition_id,
            model_maturity=(
                None
                if result.model_maturity is None
                else ModelMaturityStatus(result.model_maturity)
            ),
            result_ref=result.result_id,
            diagnostics=result.diagnostics,
            provenance=result.provenance,
        )

    def load_overview(self, selection: OverviewSelection) -> Overview:
        reader = self._require_open()
        return reader.load(selection)

    def _require_open(self) -> OverviewReader:
        if self._overview_reader is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        return self._overview_reader
