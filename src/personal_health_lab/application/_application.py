from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from types import TracebackType
from typing import Literal, Self, cast, get_args
from uuid import uuid4
from zoneinfo import ZoneInfo

from personal_health_lab import DataMode
from personal_health_lab.data_quality import (
    DataQualityError,
    HistoricalReviewRequest,
    close_review_batch_cycle_updates,
    close_review_cycle_updates,
    create_plausibility_rule_version,
    load_plausibility_rule_state,
    load_review_case_detail,
    load_review_state,
    plausibility_rule_recommendations,
    reopen_batch_decision_cycle_updates,
    reopen_decision_cycle_updates,
    run_historical_review,
)
from personal_health_lab.health_data import (
    ActivitySourceClass,
    CanonicalSleepCategory,
    DataQualityStatus,
    ModelMaturityStatus,
    canonical_unit_for,
    classify_activity_source,
)
from personal_health_lab.health_import import (
    CanonicalHealthType,
    CanonicalUnit,
    HealthExportEstimate,
    HealthImportError,
    ImportId,
    LogicalMeasurementId,
    MeasurementVersionId,
    OperationId,
    SnapshotId,
    estimate_health_export,
    import_health_export,
    inspect_restore_health_export,
)
from personal_health_lab.migration import plan_migration_rollback, plan_store_migration
from personal_health_lab.overview import Overview, OverviewReader, OverviewSelection
from personal_health_lab.recovery import (
    BackupId,
    MetadataBackupStatus,
    MetadataRestoreInspection,
    MetadataRestoreStatus,
    RestoreId,
    begin_metadata_restore,
    create_metadata_backup,
    describe_metadata_backup,
    inspect_metadata_restore,
    load_metadata_restore,
    preflight_metadata_backup,
    preflight_metadata_restore_start,
    stage_metadata_restore_abort,
    validate_metadata_restore_abort,
)
from personal_health_lab.resting_hr_analysis import (
    AnalysisDefinitionId,
    AnalysisError,
    AnalysisInputChanged,
    AnalysisProvenance,
    AnalysisResultId,
    AnalysisRunId,
)
from personal_health_lab.resting_hr_analysis import (
    run_resting_hr_analysis as execute_analysis,
)
from personal_health_lab.storage import (
    AsNeededIntakePublication,
    CapacityCheck,
    CapacityStatus,
    ContextCoverageStartPublication,
    FileVaultCheck,
    FileVaultReason,
    FileVaultStatus,
    IllnessPublication,
    IntakeReasonCategoryPublication,
    LocalStore,
    MedicationDeviationPublication,
    MedicationRegimePublication,
    OpenDataReviewCase,
    PersonBindingStatus,
    PublishBatchDecisionResult,
    PublishDecisionResult,
    StoreBusyError,
    StoredContextCoverageStart,
    StoredMeasurement,
    StoreError,
    StoreId,
    probe_filevault,
)
from personal_health_lab.storage import (
    ContextLogicalId as StoredContextLogicalId,
)
from personal_health_lab.storage import (
    ContextRevisionId as StoredContextRevisionId,
)
from personal_health_lab.storage import (
    MedicationLogicalId as StoredMedicationLogicalId,
)
from personal_health_lab.storage import (
    MedicationRevisionId as StoredMedicationRevisionId,
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


class WorkspaceState(StrEnum):
    READY = "ready"
    MIGRATION_REQUIRED = "migration_required"
    RESTORE_PENDING = "restore_pending"


_READY_WRITES = (
    "import_health_export",
    "resolve_data_review_case",
    "confirm_data_review_batch",
    "revoke_data_review_decision",
    "create_plausibility_rule_version",
    "run_historical_review",
    "run_resting_heart_rate_analysis",
    "create_metadata_backup",
    "begin_metadata_restore",
    "migrate_store",
    "rollback_migration",
)
_READY_READS = (
    "workspace_status",
    "overview",
    "data_review",
    "data_review_case",
    "plausibility_rules",
    "migration_diagnostics",
    "weight_nutrition",
    "sleep_days",
)


@dataclass(frozen=True, slots=True)
class WorkspaceStatus:
    mode: DataMode
    store_id: StoreId | None
    person_binding: PersonBindingStatus
    state: WorkspaceState = WorkspaceState.READY
    allowed_reads: tuple[str, ...] = _READY_READS
    allowed_writes: tuple[str, ...] = _READY_WRITES


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
    RESTORE_PENDING = "restore_pending"
    STORE_BUSY = "store_busy"


class UnsupportedContentCategory(StrEnum):
    RECORD_TYPE = "record_type"
    SLEEP_VALUE = "sleep_value"
    TOP_LEVEL_ELEMENT = "top_level_element"
    UNIT = "unit"
    WORKOUT_ACTIVITY_TYPE = "workout_activity_type"
    WORKOUT_CHILD = "workout_child"


@dataclass(frozen=True, slots=True)
class ImportCanonicalCounts:
    package_record_count: int
    record_count: int
    logical_measurement_count: int
    measurement_version_count: int
    source_occurrence_count: int
    anomaly_count: int


@dataclass(frozen=True, slots=True)
class ImportContentCount:
    category: UnsupportedContentCategory
    external_identifier: str
    count: int


@dataclass(frozen=True, slots=True)
class ImportDetails:
    import_id: ImportId
    canonical_counts: ImportCanonicalCounts
    unsupported_content: tuple[ImportContentCount, ...]


@dataclass(frozen=True, slots=True)
class SnapshotDateSelection:
    snapshot_ref: SnapshotRef | None = None
    start_date: date | None = None
    end_date: date | None = None

    def __post_init__(self) -> None:
        if (self.start_date is None) != (self.end_date is None):
            raise ConfigurationError("Datumsbereich verlangt Start und Ende.")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ConfigurationError("Startdatum darf nicht nach dem Enddatum liegen.")


def _resolve_medication_local_datetime(day: date, local_time: time, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    local = datetime.combine(day, local_time)
    candidate = local.replace(tzinfo=zone, fold=0)
    if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == local:
        return candidate
    for minute in range(1, 181):
        candidate = (local + timedelta(minutes=minute)).replace(tzinfo=zone, fold=0)
        if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == local + timedelta(
            minutes=minute
        ):
            return candidate
    raise ConfigurationError("Lokale Dosiszeit konnte nicht aufgelöst werden.")


@dataclass(frozen=True, slots=True)
class ContextLogicalId:
    _value: str

    def __post_init__(self) -> None:
        if len(self._value) != 32 or not set(self._value) <= set("0123456789abcdef"):
            raise ValueError("Kontext-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self._value


@dataclass(frozen=True, slots=True)
class ContextRevisionId:
    _value: str

    def __post_init__(self) -> None:
        if len(self._value) != 32 or not set(self._value) <= set("0123456789abcdef"):
            raise ValueError("Kontextrevisions-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self._value


@dataclass(frozen=True, slots=True)
class MedicationLogicalId:
    _value: str

    def __post_init__(self) -> None:
        if len(self._value) != 32 or not set(self._value) <= set("0123456789abcdef"):
            raise ValueError("Medikamenten-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self._value


@dataclass(frozen=True, slots=True)
class MedicationRevisionId:
    _value: str

    def __post_init__(self) -> None:
        if len(self._value) != 32 or not set(self._value) <= set("0123456789abcdef"):
            raise ValueError("Medikamentenrevisions-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self._value


@dataclass(frozen=True, slots=True)
class MedicationPlanEntryId:
    _value: str

    def __post_init__(self) -> None:
        if len(self._value) != 32 or not set(self._value) <= set("0123456789abcdef"):
            raise ValueError("Medikamentenplaneintrags-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self._value


class Weekday(StrEnum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


@dataclass(frozen=True, slots=True)
class ScheduledDose:
    medication_name: str
    amount: Decimal
    unit: str
    local_time: time
    weekdays: frozenset[Weekday]

    def __post_init__(self) -> None:
        name = " ".join(self.medication_name.split())
        unit = " ".join(self.unit.split())
        if not (1 <= len(name) <= 120 and 1 <= len(unit) <= 32):
            raise ConfigurationError("Medikamentenname oder Einheit ist ungültig.")
        if any(ord(char) < 32 for char in name + unit) or self.amount <= 0 or not self.weekdays:
            raise ConfigurationError("Geplante Dosis ist ungültig.")
        if not all(isinstance(day, Weekday) for day in self.weekdays):
            raise ConfigurationError("Wochentage sind ungültig.")
        object.__setattr__(self, "medication_name", name)
        object.__setattr__(self, "unit", unit)


@dataclass(frozen=True, slots=True)
class AsNeededMedication:
    medication_name: str
    amount: Decimal
    unit: str
    preferred_reason_category_ids: tuple[MedicationLogicalId, ...] = ()
    entry_id: MedicationPlanEntryId | None = None

    def __post_init__(self) -> None:
        name = " ".join(self.medication_name.split())
        unit = " ".join(self.unit.split())
        if (
            not (1 <= len(name) <= 120 and 1 <= len(unit) <= 32)
            or any(ord(char) < 32 for char in name + unit)
            or self.amount <= 0
            or not all(
                isinstance(item, MedicationLogicalId) for item in self.preferred_reason_category_ids
            )
            or len(set(self.preferred_reason_category_ids))
            != len(self.preferred_reason_category_ids)
            or (self.entry_id is not None and not isinstance(self.entry_id, MedicationPlanEntryId))
        ):
            raise ConfigurationError("Bedarfsmedikation ist ungültig.")
        object.__setattr__(self, "medication_name", name)
        object.__setattr__(self, "unit", unit)


@dataclass(frozen=True, slots=True)
class MedicationRegimeCreate:
    starts_at: datetime
    timezone: str
    scheduled_doses: tuple[ScheduledDose, ...]
    as_needed_medications: tuple[AsNeededMedication, ...] = ()


@dataclass(frozen=True, slots=True)
class MedicationRegimeRevise:
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId
    starts_at: datetime
    timezone: str
    scheduled_doses: tuple[ScheduledDose, ...]
    as_needed_medications: tuple[AsNeededMedication, ...] = ()


MedicationRegimeIntent = MedicationRegimeCreate | MedicationRegimeRevise


@dataclass(frozen=True, slots=True)
class ReviseMedicationRegime:
    intent: MedicationRegimeIntent

    def __post_init__(self) -> None:
        if not isinstance(self.intent, get_args(MedicationRegimeIntent)):
            raise ConfigurationError("Unbekannte Medikamentenregimeabsicht.")
        try:
            ZoneInfo(self.intent.timezone)
        except Exception as error:
            raise ConfigurationError("Medikamentenzeitzone ist ungültig.") from error
        if self.intent.starts_at.tzinfo is None:
            raise ConfigurationError("Regimebeginn muss zeitzonenbewusst sein.")
        if len(set(self.intent.scheduled_doses)) != len(self.intent.scheduled_doses):
            raise ConfigurationError("Identische Planeinträge sind nicht zulässig.")
        entry_ids = tuple(
            item.entry_id for item in self.intent.as_needed_medications if item.entry_id is not None
        )
        if len(set(entry_ids)) != len(entry_ids):
            raise ConfigurationError("Bedarfsplaneintrags-IDs müssen eindeutig sein.")


@dataclass(frozen=True, slots=True)
class MedicationActualIntake:
    taken_at: datetime
    amount: Decimal

    def __post_init__(self) -> None:
        if self.taken_at.tzinfo is None or self.amount <= 0:
            raise ConfigurationError("Tatsächliche Einnahme ist ungültig.")


@dataclass(frozen=True, slots=True)
class MedicationDeviationCreate:
    regime_logical_id: MedicationLogicalId
    scheduled_at: datetime
    actual_intakes: tuple[MedicationActualIntake, ...]


@dataclass(frozen=True, slots=True)
class MedicationDeviationRevise:
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId
    actual_intakes: tuple[MedicationActualIntake, ...]


@dataclass(frozen=True, slots=True)
class MedicationDeviationWithdraw:
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ConfigurationError("Rücknahme verlangt einen Grund.")


@dataclass(frozen=True, slots=True)
class MedicationDeviationRestore:
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId
    actual_intakes: tuple[MedicationActualIntake, ...]


MedicationDeviationIntent = (
    MedicationDeviationCreate
    | MedicationDeviationRevise
    | MedicationDeviationWithdraw
    | MedicationDeviationRestore
)


@dataclass(frozen=True, slots=True)
class ReviseMedicationDeviation:
    intent: MedicationDeviationIntent

    def __post_init__(self) -> None:
        if not isinstance(self.intent, get_args(MedicationDeviationIntent)):
            raise ConfigurationError("Unbekannte Einnahmeabweichungsabsicht.")
        if (
            isinstance(self.intent, MedicationDeviationCreate)
            and self.intent.scheduled_at.tzinfo is None
        ):
            raise ConfigurationError("Geplantes Dosisvorkommen muss zeitzonenbewusst sein.")


@dataclass(frozen=True, slots=True)
class IntakeReasonCategoryCreate:
    name: str


@dataclass(frozen=True, slots=True)
class IntakeReasonCategoryRevise:
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId
    name: str


@dataclass(frozen=True, slots=True)
class IntakeReasonCategoryWithdraw:
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ConfigurationError("Rücknahme verlangt einen Grund.")


@dataclass(frozen=True, slots=True)
class IntakeReasonCategoryRestore(IntakeReasonCategoryRevise):
    pass


IntakeReasonCategoryIntent = (
    IntakeReasonCategoryCreate
    | IntakeReasonCategoryRevise
    | IntakeReasonCategoryWithdraw
    | IntakeReasonCategoryRestore
)


@dataclass(frozen=True, slots=True)
class ReviseIntakeReasonCategory:
    intent: IntakeReasonCategoryIntent


@dataclass(frozen=True, slots=True)
class AsNeededIntakeCreate:
    regime_logical_id: MedicationLogicalId
    entry_id: MedicationPlanEntryId
    taken_at: datetime
    amount: Decimal
    reason_category_logical_id: MedicationLogicalId | None = None


@dataclass(frozen=True, slots=True)
class AsNeededIntakeRevise:
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId
    taken_at: datetime
    amount: Decimal
    reason_category_logical_id: MedicationLogicalId | None = None


@dataclass(frozen=True, slots=True)
class AsNeededIntakeWithdraw:
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ConfigurationError("Rücknahme verlangt einen Grund.")


@dataclass(frozen=True, slots=True)
class AsNeededIntakeRestore(AsNeededIntakeRevise):
    pass


AsNeededIntakeIntent = (
    AsNeededIntakeCreate | AsNeededIntakeRevise | AsNeededIntakeWithdraw | AsNeededIntakeRestore
)


@dataclass(frozen=True, slots=True)
class ReviseAsNeededIntake:
    intent: AsNeededIntakeIntent

    def __post_init__(self) -> None:
        if not isinstance(self.intent, get_args(AsNeededIntakeIntent)):
            raise ConfigurationError("Unbekannte Bedarfseinnahmeabsicht.")
        if not isinstance(self.intent, AsNeededIntakeWithdraw) and (
            self.intent.taken_at.tzinfo is None or self.intent.amount <= 0
        ):
            raise ConfigurationError("Bedarfseinnahme ist ungültig.")


@dataclass(frozen=True, slots=True)
class ContextCoverageStartCreate:
    start_date: date


@dataclass(frozen=True, slots=True)
class ContextCoverageStartRevise:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    start_date: date


@dataclass(frozen=True, slots=True)
class ContextCoverageStartWithdraw:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ConfigurationError("Rücknahme verlangt einen Grund.")


@dataclass(frozen=True, slots=True)
class ContextCoverageStartRestore:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    start_date: date


ContextCoverageStartIntent = (
    ContextCoverageStartCreate
    | ContextCoverageStartRevise
    | ContextCoverageStartWithdraw
    | ContextCoverageStartRestore
)


@dataclass(frozen=True, slots=True)
class ReviseContextCoverageStart:
    intent: ContextCoverageStartIntent

    def __post_init__(self) -> None:
        if not isinstance(self.intent, get_args(ContextCoverageStartIntent)):
            raise ConfigurationError("Unbekannte Kontextabdeckungsabsicht.")


class IllnessSeverity(StrEnum):
    MILD = "mild"
    MODERATE = "moderate"
    SEVERE = "severe"


@dataclass(frozen=True, slots=True)
class IllnessCategoryCreate:
    name: str


@dataclass(frozen=True, slots=True)
class IllnessCategoryRevise:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    name: str


@dataclass(frozen=True, slots=True)
class IllnessCategoryWithdraw:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    reason: str


@dataclass(frozen=True, slots=True)
class IllnessCategoryRestore:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    name: str


IllnessCategoryIntent = (
    IllnessCategoryCreate | IllnessCategoryRevise | IllnessCategoryWithdraw | IllnessCategoryRestore
)


@dataclass(frozen=True, slots=True)
class ReviseIllnessCategory:
    intent: IllnessCategoryIntent


@dataclass(frozen=True, slots=True)
class IllnessPeriodCreate:
    category_logical_id: ContextLogicalId
    start_date: date
    end_date: date | None
    severity: IllnessSeverity


@dataclass(frozen=True, slots=True)
class IllnessPeriodRevise:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    category_logical_id: ContextLogicalId
    start_date: date
    end_date: date | None
    severity: IllnessSeverity


@dataclass(frozen=True, slots=True)
class IllnessPeriodWithdraw:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    reason: str


@dataclass(frozen=True, slots=True)
class IllnessPeriodRestore:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    category_logical_id: ContextLogicalId
    start_date: date
    end_date: date | None
    severity: IllnessSeverity


IllnessPeriodIntent = (
    IllnessPeriodCreate | IllnessPeriodRevise | IllnessPeriodWithdraw | IllnessPeriodRestore
)


@dataclass(frozen=True, slots=True)
class ReviseIllnessPeriod:
    intent: IllnessPeriodIntent


class StressLevel(StrEnum):
    VERY_LOW = "very_low"
    LOW = "low"
    AVERAGE = "average"
    HIGH = "high"
    VERY_HIGH = "very_high"


@dataclass(frozen=True, slots=True)
class DailyStressCreate:
    day: date
    level: StressLevel


@dataclass(frozen=True, slots=True)
class DailyStressRevise:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    day: date
    level: StressLevel


@dataclass(frozen=True, slots=True)
class DailyStressWithdraw:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    reason: str


@dataclass(frozen=True, slots=True)
class DailyStressRestore(DailyStressRevise):
    pass


DailyStressIntent = DailyStressCreate | DailyStressRevise | DailyStressWithdraw | DailyStressRestore


@dataclass(frozen=True, slots=True)
class ReviseDailyStress:
    intent: DailyStressIntent


@dataclass(frozen=True, slots=True)
class CustomContextLabelCreate:
    name: str


@dataclass(frozen=True, slots=True)
class CustomContextLabelRevise:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    name: str


@dataclass(frozen=True, slots=True)
class CustomContextLabelWithdraw:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    reason: str


@dataclass(frozen=True, slots=True)
class CustomContextLabelRestore(CustomContextLabelRevise):
    pass


CustomContextLabelIntent = (
    CustomContextLabelCreate
    | CustomContextLabelRevise
    | CustomContextLabelWithdraw
    | CustomContextLabelRestore
)


@dataclass(frozen=True, slots=True)
class ReviseCustomContextLabel:
    intent: CustomContextLabelIntent


@dataclass(frozen=True, slots=True)
class CustomContextPeriodCreate:
    label_logical_id: ContextLogicalId
    start_date: date
    end_date: date | None
    note: str | None


@dataclass(frozen=True, slots=True)
class CustomContextPeriodRevise:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    label_logical_id: ContextLogicalId
    start_date: date
    end_date: date | None
    note: str | None


@dataclass(frozen=True, slots=True)
class CustomContextPeriodWithdraw:
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId
    reason: str


@dataclass(frozen=True, slots=True)
class CustomContextPeriodRestore(CustomContextPeriodRevise):
    pass


CustomContextPeriodIntent = (
    CustomContextPeriodCreate
    | CustomContextPeriodRevise
    | CustomContextPeriodWithdraw
    | CustomContextPeriodRestore
)


@dataclass(frozen=True, slots=True)
class ReviseCustomContextPeriod:
    intent: CustomContextPeriodIntent


class ContextIllnessOrigin(StrEnum):
    UNKNOWN = "unknown"
    ASSUMED_NONE = "assumed_none"
    OBSERVED = "observed"


class ContextStressOrigin(StrEnum):
    UNKNOWN = "unknown"
    ASSUMED_AVERAGE = "assumed_average"
    OBSERVED = "observed"


@dataclass(frozen=True, slots=True)
class DailyContextDay:
    day: date
    illness_origin: ContextIllnessOrigin
    stress_origin: ContextStressOrigin
    highest_illness_severity: IllnessSeverity | None = None
    stress_level: StressLevel | None = None
    custom_context_labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DailyContext:
    snapshot_ref: SnapshotRef | None
    context_as_of_date: date | None
    context_timezone: str
    days: tuple[DailyContextDay, ...]


@dataclass(frozen=True, slots=True)
class ContextCoverageStartRecord:
    logical_id: ContextLogicalId
    revision_id: ContextRevisionId
    start_date: date


@dataclass(frozen=True, slots=True)
class CustomContextLabelRecord:
    logical_id: ContextLogicalId
    revision_id: ContextRevisionId
    name: str


@dataclass(frozen=True, slots=True)
class CustomContextPeriodRecord:
    logical_id: ContextLogicalId
    revision_id: ContextRevisionId
    label_logical_id: ContextLogicalId
    start_date: date
    end_date: date | None
    note: str | None


@dataclass(frozen=True, slots=True)
class ContextRecords:
    snapshot_ref: SnapshotRef | None
    coverage_start: ContextCoverageStartRecord | None
    custom_labels: tuple[CustomContextLabelRecord, ...] = ()
    custom_periods: tuple[CustomContextPeriodRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextAuditRevision:
    revision_id: ContextRevisionId
    previous_revision_id: ContextRevisionId | None
    state: Literal["active", "withdrawn"]
    start_date: date | None


@dataclass(frozen=True, slots=True)
class ContextAudit:
    logical_id: ContextLogicalId
    revisions: tuple[ContextAuditRevision, ...]


@dataclass(frozen=True, slots=True)
class MedicationDoseOccurrence:
    medication_name: str
    amount: Decimal
    unit: str
    scheduled_at: datetime
    status: Literal["assumed_as_planned", "deviated"] = "assumed_as_planned"
    actual_intakes: tuple[MedicationActualIntake, ...] = ()


@dataclass(frozen=True, slots=True)
class MedicationAsNeededIntake:
    logical_id: MedicationLogicalId
    taken_at: datetime
    medication_name: str
    amount: Decimal
    unit: str
    reason_category_name: str | None


@dataclass(frozen=True, slots=True)
class MedicationDay:
    day: date
    status: Literal["unknown", "empty", "planned"]
    occurrences: tuple[MedicationDoseOccurrence, ...]
    as_needed_intakes: tuple[MedicationAsNeededIntake, ...] = ()


@dataclass(frozen=True, slots=True)
class MedicationDays:
    snapshot_ref: SnapshotRef | None
    medication_as_of: datetime | None
    timezone: str | None
    days: tuple[MedicationDay, ...]


@dataclass(frozen=True, slots=True)
class MedicationRegimeRecord:
    logical_id: MedicationLogicalId
    revision_id: MedicationRevisionId
    starts_at: datetime
    timezone: str
    scheduled_doses: tuple[ScheduledDose, ...]
    as_needed_medications: tuple[AsNeededMedication, ...] = ()


@dataclass(frozen=True, slots=True)
class IntakeReasonCategoryRecord:
    logical_id: MedicationLogicalId
    revision_id: MedicationRevisionId
    name: str


@dataclass(frozen=True, slots=True)
class MedicationPlan:
    snapshot_ref: SnapshotRef | None
    medication_as_of: datetime | None
    regimes: tuple[MedicationRegimeRecord, ...]
    intake_reason_categories: tuple[IntakeReasonCategoryRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class MedicationAuditRevision:
    revision_id: MedicationRevisionId
    previous_revision_id: MedicationRevisionId | None
    starts_at: datetime
    timezone: str
    scheduled_doses: tuple[ScheduledDose, ...]


@dataclass(frozen=True, slots=True)
class MedicationDeviationAuditRevision:
    revision_id: MedicationRevisionId
    previous_revision_id: MedicationRevisionId | None
    state: Literal["active", "withdrawn"]
    regime_logical_id: MedicationLogicalId
    scheduled_at: datetime
    actual_intakes: tuple[MedicationActualIntake, ...]


@dataclass(frozen=True, slots=True)
class AsNeededIntakeAuditRevision:
    revision_id: MedicationRevisionId
    previous_revision_id: MedicationRevisionId | None
    state: Literal["active", "withdrawn"]
    regime_logical_id: MedicationLogicalId
    entry_id: MedicationPlanEntryId
    taken_at: datetime
    amount: Decimal
    reason_category_logical_id: MedicationLogicalId | None


@dataclass(frozen=True, slots=True)
class IntakeReasonCategoryAuditRevision:
    revision_id: MedicationRevisionId
    previous_revision_id: MedicationRevisionId | None
    state: Literal["active", "withdrawn"]
    name: str | None


@dataclass(frozen=True, slots=True)
class MedicationAudit:
    logical_id: MedicationLogicalId
    revisions: tuple[
        MedicationAuditRevision
        | MedicationDeviationAuditRevision
        | AsNeededIntakeAuditRevision
        | IntakeReasonCategoryAuditRevision,
        ...,
    ]


class WeightDayStatus(StrEnum):
    OBSERVED = "observed"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class WeightMeasurement:
    logical_measurement_id: LogicalMeasurementId
    measurement_version_id: MeasurementVersionId
    value_kg: float
    effective_value_kg: float | None
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
    review_case_ids: tuple[DataReviewCaseId, ...]


@dataclass(frozen=True, slots=True)
class HealthKitNutritionSample:
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
    review_case_ids: tuple[DataReviewCaseId, ...]


@dataclass(frozen=True, slots=True)
class PreferredDailyWeight:
    day: date
    status: WeightDayStatus
    value_kg: float | None
    logical_measurement_ids: tuple[LogicalMeasurementId, ...] = ()
    measurement_version_ids: tuple[MeasurementVersionId, ...] = ()
    review_case_ids: tuple[DataReviewCaseId, ...] = ()
    quality_status: DataQualityStatus = DataQualityStatus.REVIEWED


@dataclass(frozen=True, slots=True)
class DailyNutritionFeature:
    data_type: CanonicalHealthType
    unit: CanonicalUnit
    value: float | None
    logical_measurement_ids: tuple[LogicalMeasurementId, ...] = ()
    measurement_version_ids: tuple[MeasurementVersionId, ...] = ()
    review_case_ids: tuple[DataReviewCaseId, ...] = ()
    quality_status: DataQualityStatus = DataQualityStatus.REVIEWED


@dataclass(frozen=True, slots=True)
class DailyNutrition:
    day: date
    energy: DailyNutritionFeature
    protein: DailyNutritionFeature
    carbohydrates: DailyNutritionFeature
    total_fat: DailyNutritionFeature


@dataclass(frozen=True, slots=True)
class WeightNutrition:
    snapshot_ref: SnapshotRef | None
    status: DataQualityStatus
    days: tuple[PreferredDailyWeight, ...]
    weight_measurements: tuple[WeightMeasurement, ...]
    nutrition_days: tuple[DailyNutrition, ...]
    healthkit_nutrition_samples: tuple[HealthKitNutritionSample, ...]


class SleepCategory(StrEnum):
    IN_BED = "in_bed"
    AWAKE = "awake"
    ASLEEP_UNSPECIFIED = "asleep_unspecified"
    ASLEEP_CORE = "asleep_core"
    ASLEEP_DEEP = "asleep_deep"
    ASLEEP_REM = "asleep_rem"


class SleepSourceClass(StrEnum):
    WATCH = "watch"
    IPHONE = "iphone"
    MANUAL = "manual"
    OTHER = "other"
    UNKNOWN = "unknown"


class SleepObservationStatus(StrEnum):
    UNOBSERVED = "unobserved"
    PARTIAL = "partial"
    OBSERVED = "observed"


@dataclass(frozen=True, slots=True)
class SleepInterval:
    logical_measurement_id: LogicalMeasurementId
    measurement_version_id: MeasurementVersionId
    original_category: str
    canonical_category: SleepCategory
    source_class: SleepSourceClass
    is_selected: bool
    source_start: datetime
    source_end: datetime
    source_updated_at: datetime
    source_name: str
    source_version: str
    device: str


@dataclass(frozen=True, slots=True)
class SleepEpisode:
    start: datetime
    end: datetime
    first_observed_asleep: datetime | None
    last_observed_asleep: datetime | None
    observed_sleep: timedelta
    observed_awake: timedelta
    in_bed: timedelta | None
    asleep_core: timedelta
    asleep_deep: timedelta
    asleep_rem: timedelta
    asleep_unspecified: timedelta
    stage_ambiguous: timedelta
    uncovered_gap: timedelta
    removed_same_state_overlap: timedelta
    asleep_awake_conflict: timedelta
    observed_coverage_ratio: float | None
    detailed_stage_coverage_ratio: float | None
    interval_ids: tuple[MeasurementVersionId, ...]


@dataclass(frozen=True, slots=True)
class SleepSourceCount:
    source_class: SleepSourceClass
    accepted_interval_count: int
    rejected_interval_count: int


@dataclass(frozen=True, slots=True)
class SleepQuality:
    source_classifier_version: str
    derivation_version: str
    accepted_interval_count: int
    rejected_interval_count: int
    contributing_watch_source_count: int
    source_counts: tuple[SleepSourceCount, ...]
    primary_selection_ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class SleepDay:
    day: date
    status: SleepObservationStatus
    primary_episode: SleepEpisode | None
    naps: tuple[SleepEpisode, ...]
    quality: SleepQuality

    @property
    def nap_count(self) -> int:
        return len(self.naps)

    @property
    def nap_observed_sleep(self) -> timedelta:
        return sum((item.observed_sleep for item in self.naps), timedelta())


@dataclass(frozen=True, slots=True)
class SleepDays:
    snapshot_ref: SnapshotRef | None
    days: tuple[SleepDay, ...]
    accepted_intervals: tuple[SleepInterval, ...]
    rejected_intervals: tuple[SleepInterval, ...]


class ActivityMetric(StrEnum):
    EXERCISE_TIME = "apple_exercise_time"
    STEP_COUNT = "step_count"
    WALKING_RUNNING_DISTANCE = "walking_running_distance"
    ACTIVE_ENERGY = "active_energy"


@dataclass(frozen=True, slots=True)
class ActivityMeasurement:
    logical_measurement_id: LogicalMeasurementId
    measurement_version_id: MeasurementVersionId
    data_type: ActivityMetric
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
    source_class: ActivitySourceClass
    source_start: datetime
    source_end: datetime
    source_updated_at: datetime
    measurement_local_day: date
    source_name: str
    source_version: str
    device: str
    original_value: float
    original_unit: str
    review_case_ids: tuple[DataReviewCaseId, ...]
    suppression_reason: str | None = None


class ActivityCoverageKind(StrEnum):
    WATCH = "watch"
    IPHONE_FALLBACK = "iphone_fallback"
    UNOBSERVED = "unobserved"


@dataclass(frozen=True, slots=True)
class ActivityCoverageSegment:
    kind: ActivityCoverageKind
    source_start: datetime
    source_end: datetime


@dataclass(frozen=True, slots=True)
class ActivityDerivationVersion:
    version_id: str
    coverage_gap_minutes: int
    source_classifier_version: str


@dataclass(frozen=True, slots=True)
class ActivitySettings:
    active_version: ActivityDerivationVersion
    recommended_version: ActivityDerivationVersion


@dataclass(frozen=True, slots=True)
class CreateActivityDerivationVersion:
    coverage_gap_minutes: int = 240

    def __post_init__(self) -> None:
        if type(self.coverage_gap_minutes) is not int or not 1 <= self.coverage_gap_minutes <= 1440:
            raise ConfigurationError("Abdeckungsschwelle muss eine ganze Zahl von 1 bis 1440 sein.")


@dataclass(frozen=True, slots=True)
class DailyActivityMetric:
    data_type: ActivityMetric
    unit: CanonicalUnit
    value: float | None
    logical_measurement_ids: tuple[LogicalMeasurementId, ...] = ()
    measurement_version_ids: tuple[MeasurementVersionId, ...] = ()
    review_case_ids: tuple[DataReviewCaseId, ...] = ()
    quality_status: DataQualityStatus = DataQualityStatus.REVIEWED
    watch_value: float | None = None
    iphone_value: float | None = None


@dataclass(frozen=True, slots=True)
class ActivityDay:
    day: date
    exercise_time: DailyActivityMetric
    step_count: DailyActivityMetric
    walking_running_distance: DailyActivityMetric
    active_energy: DailyActivityMetric
    coverage_segments: tuple[ActivityCoverageSegment, ...] = ()
    is_complete: bool = True
    incomplete_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ActivityDays:
    snapshot_ref: SnapshotRef | None
    status: DataQualityStatus
    days: tuple[ActivityDay, ...]
    measurements: tuple[ActivityMeasurement, ...]
    source_classifier_version: str = "activity-source-classification/v1"
    derivation_version: str = "activity-derivation/v1"
    coverage_gap_minutes: int = 240


@dataclass(frozen=True, slots=True)
class Workout:
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
    effective_duration_minutes: float
    distance_kilometers: float | None
    active_energy_kilocalories: float | None
    is_selected: bool
    review_case_ids: tuple[DataReviewCaseId, ...]


@dataclass(frozen=True, slots=True)
class WorkoutAggregate:
    day: date
    original_activity_type: str
    workout_count: int
    duration_minutes: float
    distance_kilometers: float | None
    active_energy_kilocalories: float | None
    review_case_ids: tuple[DataReviewCaseId, ...]


@dataclass(frozen=True, slots=True)
class Workouts:
    snapshot_ref: SnapshotRef | None
    status: DataQualityStatus
    workouts: tuple[Workout, ...]
    aggregates: tuple[WorkoutAggregate, ...]


_ACTIVITY_TYPES = {CanonicalHealthType(item.value): item for item in ActivityMetric}
_ACTIVITY_SOURCE_CLASSIFIER_VERSION = "activity-source-classification/v1"
_ACTIVITY_DERIVATION_VERSION = "activity-derivation/v1"
_ACTIVITY_COVERAGE_GAP_MINUTES = 240


_SLEEP_SOURCE_CLASSIFIER_VERSION = "sleep-source-classification/v1"
_SLEEP_DERIVATION_VERSION = "sleep-derivation/v1"
_SLEEP_CATEGORIES = {item: SleepCategory(item.value) for item in CanonicalSleepCategory}
_SLEEP_SOURCE_RULES = {
    ("Apple Watch", "Apple Watch"): SleepSourceClass.WATCH,
    ("iPhone", "iPhone"): SleepSourceClass.IPHONE,
    ("Manual entry", ""): SleepSourceClass.MANUAL,
    ("Pillow", ""): SleepSourceClass.OTHER,
}


def _classify_sleep_source(source_name: str, device: str) -> SleepSourceClass:
    if (source_class := _SLEEP_SOURCE_RULES.get((source_name, device))) is not None:
        return source_class
    return SleepSourceClass.UNKNOWN


def _sleep_episode(intervals: tuple[SleepInterval, ...]) -> SleepEpisode:
    intervals = tuple(item for item in intervals if item.source_end > item.source_start)
    starts_and_ends = sorted(
        {timestamp for item in intervals for timestamp in (item.source_start, item.source_end)}
    )
    durations = {category: timedelta() for category in SleepCategory}
    stage_ambiguous = timedelta()
    asleep_awake_conflict = timedelta()
    first_asleep: datetime | None = None
    last_asleep: datetime | None = None
    asleep_categories = {
        SleepCategory.ASLEEP_UNSPECIFIED,
        SleepCategory.ASLEEP_CORE,
        SleepCategory.ASLEEP_DEEP,
        SleepCategory.ASLEEP_REM,
    }
    for start, end in pairwise(starts_and_ends):
        active = {
            item.canonical_category
            for item in intervals
            if item.source_start <= start and item.source_end >= end
        }
        span = end - start
        asleep = active & asleep_categories
        if asleep and SleepCategory.AWAKE in active:
            asleep_awake_conflict += span
        elif len(asleep) > 1:
            stage_ambiguous += span
            first_asleep = start if first_asleep is None else first_asleep
            last_asleep = end
        elif asleep:
            category = next(iter(asleep))
            durations[category] += span
            first_asleep = start if first_asleep is None else first_asleep
            last_asleep = end
        elif SleepCategory.AWAKE in active:
            durations[SleepCategory.AWAKE] += span
        if SleepCategory.IN_BED in active:
            durations[SleepCategory.IN_BED] += span

    def union_duration(items: list[SleepInterval]) -> timedelta:
        covered = timedelta()
        end: datetime | None = None
        for item in sorted(items, key=lambda value: (value.source_start, value.source_end)):
            if end is None or item.source_start >= end:
                covered += item.source_end - item.source_start
                end = item.source_end
            elif item.source_end > end:
                covered += item.source_end - end
                end = item.source_end
        return covered

    removed_same_state_overlap = sum(
        (
            sum((item.source_end - item.source_start for item in items), timedelta())
            - union_duration(items)
            for category in SleepCategory
            if (items := [item for item in intervals if item.canonical_category is category])
        ),
        timedelta(),
    )
    known_intervals = tuple(
        item for item in intervals if item.canonical_category is not SleepCategory.IN_BED
    )
    covered_until: datetime | None = None
    uncovered_gap = timedelta()
    for item in sorted(known_intervals, key=lambda value: (value.source_start, value.source_end)):
        if covered_until is not None and item.source_start > covered_until:
            uncovered_gap += item.source_start - covered_until
        if covered_until is None or item.source_end > covered_until:
            covered_until = item.source_end
    observed_sleep = sum((durations[category] for category in asleep_categories), stage_ambiguous)
    known = observed_sleep + durations[SleepCategory.AWAKE]
    span = max(item.source_end for item in known_intervals) - min(
        item.source_start for item in known_intervals
    )
    detailed = (
        durations[SleepCategory.ASLEEP_CORE]
        + durations[SleepCategory.ASLEEP_DEEP]
        + durations[SleepCategory.ASLEEP_REM]
    )
    return SleepEpisode(
        start=min(item.source_start for item in known_intervals),
        end=max(item.source_end for item in known_intervals),
        first_observed_asleep=first_asleep,
        last_observed_asleep=last_asleep,
        observed_sleep=observed_sleep,
        observed_awake=durations[SleepCategory.AWAKE],
        in_bed=durations[SleepCategory.IN_BED] or None,
        asleep_core=durations[SleepCategory.ASLEEP_CORE],
        asleep_deep=durations[SleepCategory.ASLEEP_DEEP],
        asleep_rem=durations[SleepCategory.ASLEEP_REM],
        asleep_unspecified=durations[SleepCategory.ASLEEP_UNSPECIFIED],
        stage_ambiguous=stage_ambiguous,
        uncovered_gap=uncovered_gap,
        removed_same_state_overlap=removed_same_state_overlap,
        asleep_awake_conflict=asleep_awake_conflict,
        observed_coverage_ratio=None if not span else known / span,
        detailed_stage_coverage_ratio=None if not observed_sleep else detailed / observed_sleep,
        interval_ids=tuple(item.measurement_version_id for item in intervals),
    )


@dataclass(frozen=True, slots=True)
class PlanFingerprint:
    _value: str

    def __post_init__(self) -> None:
        if len(self._value) != 64 or not set(self._value) <= set("0123456789abcdef"):
            raise ValueError("Plan-Fingerprint muss ein SHA-256-Wert sein.")

    def __str__(self) -> str:
        return self._value


@dataclass(frozen=True, slots=True)
class ImportHealthExport:
    package_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.package_path, Path):
            raise ConfigurationError("Health-Exportpfad muss ein pathlib.Path-Wert sein.")
        object.__setattr__(self, "package_path", self.package_path.expanduser().resolve())


@dataclass(frozen=True, slots=True)
class CreateMetadataBackup:
    target_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.target_path, Path):
            raise ConfigurationError("Sicherungsziel muss ein pathlib.Path-Wert sein.")
        object.__setattr__(self, "target_path", self.target_path.expanduser().resolve())


@dataclass(frozen=True, slots=True)
class BeginMetadataRestore:
    backup_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.backup_path, Path):
            raise ConfigurationError("Wiederherstellungssicherung muss ein pathlib.Path-Wert sein.")
        object.__setattr__(self, "backup_path", self.backup_path.expanduser().resolve())


@dataclass(frozen=True, slots=True)
class AbortMetadataRestore:
    pass


@dataclass(frozen=True, slots=True)
class RecoveryStatus:
    restore_id: RestoreId
    backup_id: BackupId
    original_backup_sha256: str
    working_copy_sha256: str
    audit_max_position: int
    source_schema_version: int
    target_schema_version: int
    migration_steps: tuple[tuple[int, int], ...]
    status: MetadataRestoreStatus


@dataclass(frozen=True, slots=True)
class MigrateStore:
    pass


@dataclass(frozen=True, slots=True)
class RollbackMigration:
    pass


@dataclass(frozen=True, slots=True)
class MigrationDiagnostics:
    source_version: int | None
    target_version: int
    steps: tuple[tuple[int, int], ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlausibilityRuleSpecification:
    unit: CanonicalUnit
    fixed_lower_bound: float | None = None
    fixed_upper_bound: float | None = None
    personal_range_enabled: bool = False

    def __post_init__(self) -> None:
        bounds = (self.fixed_lower_bound, self.fixed_upper_bound)
        if not isinstance(self.unit, CanonicalUnit):
            raise ConfigurationError("Plausibilitätsregeleinheit hat einen ungültigen Typ.")
        if any(value is not None and not math.isfinite(value) for value in bounds):
            raise ConfigurationError("Plausibilitätsgrenzen müssen endlich sein.")
        if (
            self.fixed_lower_bound is not None
            and self.fixed_upper_bound is not None
            and self.fixed_lower_bound >= self.fixed_upper_bound
        ):
            raise ConfigurationError("Untere Plausibilitätsgrenze muss kleiner sein.")

    @property
    def active(self) -> bool:
        return (
            self.fixed_lower_bound is not None
            or self.fixed_upper_bound is not None
            or self.personal_range_enabled
        )


@dataclass(frozen=True, slots=True)
class CreatePlausibilityRuleVersion:
    data_type: CanonicalHealthType
    specification: PlausibilityRuleSpecification
    effective_from: datetime | None
    recommendation_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.data_type, CanonicalHealthType):
            raise ConfigurationError("Plausibilitätsregeltyp hat einen ungültigen Typ.")
        expected_unit = canonical_unit_for(self.data_type)
        if self.specification.unit is not expected_unit:
            raise ConfigurationError("Plausibilitätsregel verwendet nicht die kanonische Einheit.")
        if self.effective_from is not None and (
            self.effective_from.tzinfo is None
            or self.effective_from.weekday() != 0
            or self.effective_from.time() != datetime.min.time()
        ):
            raise ConfigurationError("Regelgültigkeit muss lokaler ISO-Montag 00:00 sein.")


@dataclass(frozen=True, slots=True)
class RunHistoricalReview:
    data_type: CanonicalHealthType
    start_date: date
    end_date: date
    rule_version_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.data_type, CanonicalHealthType):
            raise ConfigurationError("Historischer Prüftyp hat einen ungültigen Typ.")
        if self.start_date > self.end_date:
            raise ConfigurationError("Historischer Prüfzeitraum ist ungültig.")


@dataclass(frozen=True, slots=True)
class RunRestingHeartRateAnalysis:
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
class PlausibilityRuleVersion:
    version_id: str
    data_type: CanonicalHealthType
    specification: PlausibilityRuleSpecification
    effective_from: datetime | None
    created_at: datetime
    recommendation_id: str | None = None
    effective_timezone: str | None = None
    effective_offset_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class PlausibilityRuleRecommendation:
    recommendation_id: str
    specification: PlausibilityRuleSpecification


@dataclass(frozen=True, slots=True)
class PlausibilityRule:
    data_type: CanonicalHealthType
    versions: tuple[PlausibilityRuleVersion, ...]
    recommendation: PlausibilityRuleRecommendation

    @property
    def active_version(self) -> PlausibilityRuleVersion:
        return self.versions[-1]


@dataclass(frozen=True, slots=True)
class PlausibilityRules:
    rules: tuple[PlausibilityRule, ...]


class SourceDeletionVerdict(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class SourceDeletionResolution:
    verdict: SourceDeletionVerdict
    note: str | None = None


class SourceConflictStrategy(StrEnum):
    PREFER = "prefer"
    SPLIT = "split"


@dataclass(frozen=True, slots=True)
class SourceConflictResolution:
    strategy: SourceConflictStrategy
    preferred_version_id: MeasurementVersionId | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if (self.strategy is SourceConflictStrategy.PREFER) != (
            self.preferred_version_id is not None
        ):
            raise ConfigurationError("'prefer' verlangt genau eine Quellversion.")


@dataclass(frozen=True, slots=True)
class DataConfirmation:
    note: str | None = None


@dataclass(frozen=True, slots=True)
class DataCorrection:
    measurement_version_id: MeasurementVersionId
    corrected_value: float
    unit: CanonicalUnit
    reason: str
    note: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.corrected_value):
            raise ConfigurationError("Korrekturwert muss endlich sein.")
        if not self.reason.strip():
            raise ConfigurationError("Datenkorrektur verlangt einen Grund.")


@dataclass(frozen=True, slots=True)
class LocalMeasurementExclusion:
    measurement_version_id: MeasurementVersionId
    reason: str
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ConfigurationError("Lokaler Messungsausschluss verlangt einen Grund.")


@dataclass(frozen=True, slots=True)
class WorkoutCorrection:
    workout_version_id: MeasurementVersionId
    effective_duration_minutes: float
    distance_kilometers: float | None
    active_energy_kilocalories: float | None
    reason: str
    note: str | None = None

    def __post_init__(self) -> None:
        values = (
            self.effective_duration_minutes,
            self.distance_kilometers,
            self.active_energy_kilocalories,
        )
        if any(value is not None and (not math.isfinite(value) or value < 0) for value in values):
            raise ConfigurationError(
                "Korrigierte Trainingswerte müssen endlich und nichtnegativ sein."
            )
        if not self.reason.strip():
            raise ConfigurationError("Trainingskorrektur verlangt einen Grund.")


@dataclass(frozen=True, slots=True)
class LocalWorkoutExclusion:
    workout_version_id: MeasurementVersionId
    reason: str
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ConfigurationError("Lokaler Trainingsausschluss verlangt einen Grund.")


@dataclass(frozen=True, slots=True)
class SourceValueAcceptance:
    measurement_version_id: MeasurementVersionId
    note: str | None = None


DataReviewResolution = (
    DataConfirmation
    | DataCorrection
    | LocalMeasurementExclusion
    | WorkoutCorrection
    | LocalWorkoutExclusion
    | SourceValueAcceptance
    | SourceDeletionResolution
    | SourceConflictResolution
)


@dataclass(frozen=True, slots=True)
class ResolveDataReviewCase:
    case_id: DataReviewCaseId | None
    resolution: DataReviewResolution

    def __post_init__(self) -> None:
        if not isinstance(self.resolution, get_args(DataReviewResolution)):
            raise ConfigurationError("Unbekannter Datenprüfentscheid.")
        if self.case_id is None and not isinstance(self.resolution, DataCorrection):
            raise ConfigurationError("Nur eine Datenkorrektur darf ohne Prüffall erfolgen.")


@dataclass(frozen=True, slots=True)
class DataReviewDecisionId:
    _value: str

    def __post_init__(self) -> None:
        if len(self._value) != 32 or not set(self._value) <= set("0123456789abcdef"):
            raise ValueError("Entscheidungs-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self._value


@dataclass(frozen=True, slots=True)
class SingleDecisionTarget:
    decision_id: DataReviewDecisionId


@dataclass(frozen=True, slots=True)
class DataReviewBatchActionId:
    _value: str

    def __post_init__(self) -> None:
        if len(self._value) != 32 or not set(self._value) <= set("0123456789abcdef"):
            raise ValueError("Sammelaktions-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self._value


@dataclass(frozen=True, slots=True)
class BatchDecisionTarget:
    batch_action_id: DataReviewBatchActionId


DataReviewDecisionTarget = SingleDecisionTarget | BatchDecisionTarget


@dataclass(frozen=True, slots=True)
class ConfirmDataReviewBatch:
    selection: DataReviewSelection
    note: str | None = None


@dataclass(frozen=True, slots=True)
class RevokeDataReviewDecision:
    target: DataReviewDecisionTarget
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.target, get_args(DataReviewDecisionTarget)):
            raise ConfigurationError("Unbekanntes Widerrufsziel.")
        if not self.reason.strip():
            raise ConfigurationError("Widerruf verlangt einen Grund.")


WriteRequest = (
    ImportHealthExport
    | CreateMetadataBackup
    | BeginMetadataRestore
    | AbortMetadataRestore
    | MigrateStore
    | RollbackMigration
    | ResolveDataReviewCase
    | ConfirmDataReviewBatch
    | RevokeDataReviewDecision
    | CreatePlausibilityRuleVersion
    | RunHistoricalReview
    | RunRestingHeartRateAnalysis
    | ReviseContextCoverageStart
    | ReviseIllnessCategory
    | ReviseIllnessPeriod
    | ReviseDailyStress
    | ReviseCustomContextLabel
    | ReviseCustomContextPeriod
    | ReviseMedicationRegime
    | ReviseMedicationDeviation
    | ReviseAsNeededIntake
    | ReviseIntakeReasonCategory
)


class WriteApprovalStatus(StrEnum):
    READY = "ready"
    CONFIRMATION_REQUIRED = "confirmation_required"
    BLOCKED = "blocked"
    NO_CHANGE = "no_change"


class WriteConfirmation(StrEnum):
    REAL_IMPORT_SAME_PERSON = "real_import_same_person"
    FILEVAULT_UNPROTECTED = "filevault_unprotected"
    FILEVAULT_TRANSITIONING = "filevault_transitioning"
    FILEVAULT_UNKNOWN = "filevault_unknown"
    METADATA_BACKUP_POINT_IN_TIME = "metadata_backup_point_in_time"
    METADATA_RESTORE = "metadata_restore"
    STORE_MIGRATION = "store_migration"
    MIGRATION_ROLLBACK = "migration_rollback"


@dataclass(frozen=True, slots=True)
class WriteApproval:
    status: WriteApprovalStatus


@dataclass(frozen=True, slots=True)
class ImportHealthExportPlan:
    package_hash: str
    package_size: int
    input_bytes: int = 0
    record_count: int = 0
    restore_state_hash: str | None = None


@dataclass(frozen=True, slots=True)
class MetadataBackupPlan:
    backup_id: BackupId
    canonical_content_sha256: str
    audit_max_position: int
    target_file: str


@dataclass(frozen=True, slots=True)
class MetadataRestorePlan:
    restore_id: RestoreId | None
    backup_id: BackupId | None
    source_store_id: StoreId | None
    original_backup_sha256: str | None
    working_copy_sha256: str | None
    audit_max_position: int | None
    source_schema_version: int | None
    target_schema_version: int
    migration_steps: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class AbortMetadataRestorePlan:
    restore_id: RestoreId | None
    backup_id: BackupId | None


@dataclass(frozen=True, slots=True)
class StoreMigrationPlan:
    source_version: int | None
    target_version: int
    steps: tuple[tuple[int, int], ...]
    backup_file: str | None
    affected_snapshot_refs: tuple[SnapshotRef, ...]
    existing_analyses_become_stale: bool


@dataclass(frozen=True, slots=True)
class RollbackMigrationPlan:
    migration_operation_id: OperationId | None
    source_version: int | None
    target_version: int | None
    backup_file: str | None
    backup_sha256: str | None
    current_snapshot_ref: SnapshotRef | None
    restored_snapshot_ref: SnapshotRef | None


@dataclass(frozen=True, slots=True)
class DataReviewDecisionPlan:
    case_id: DataReviewCaseId | None
    active_snapshot_ref: SnapshotRef | None


@dataclass(frozen=True, slots=True)
class DataReviewBatchMatch:
    case_id: DataReviewCaseId
    kind: DataReviewCaseKind
    measurement_version_id: MeasurementVersionId | None
    rule_version_id: str | None
    evidence_fingerprint: str
    effective_value: float | None
    canonical_unit: CanonicalUnit | None


@dataclass(frozen=True, slots=True)
class DataReviewBatchPlan:
    selection: DataReviewSelection
    matches: tuple[DataReviewBatchMatch, ...]
    count: int
    active_snapshot_ref: SnapshotRef | None


@dataclass(frozen=True, slots=True)
class DataReviewBatchRevokePlan:
    batch_action_id: DataReviewBatchActionId
    decision_ids: tuple[DataReviewDecisionId, ...]
    count: int
    active_snapshot_ref: SnapshotRef | None


def _data_review_batch_match_payload(match: DataReviewBatchMatch) -> dict[str, object]:
    return {
        "case_id": str(match.case_id),
        "kind": match.kind.value,
        "measurement_version_id": (
            None if match.measurement_version_id is None else str(match.measurement_version_id)
        ),
        "rule_version_id": match.rule_version_id,
        "evidence_fingerprint": match.evidence_fingerprint,
        "effective_value": match.effective_value,
        "canonical_unit": None if match.canonical_unit is None else match.canonical_unit.value,
    }


@dataclass(frozen=True, slots=True)
class PlausibilityRuleVersionPlan:
    previous_version_id: str | None
    proposed_version_id: str
    active_snapshot_ref: SnapshotRef | None


@dataclass(frozen=True, slots=True)
class HistoricalReviewPlan:
    start_date: date
    end_date: date
    rule_version_id: str
    base_snapshot_ref: SnapshotRef | None


@dataclass(frozen=True, slots=True)
class ManualContextRevisionPlan:
    intent: Literal["create", "revise", "withdraw", "restore"]
    logical_id: ContextLogicalId
    expected_revision_id: ContextRevisionId | None
    start_date: date | None
    withdrawal_reason: str | None
    base_snapshot_ref: SnapshotRef | None
    snapshot_as_of: datetime
    context_timezone: str


@dataclass(frozen=True, slots=True)
class MedicationRegimePlan:
    intent: Literal["create", "revise"]
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId | None
    starts_at: datetime
    timezone: str
    scheduled_doses: tuple[ScheduledDose, ...]
    as_needed_medications: tuple[AsNeededMedication, ...]
    base_snapshot_ref: SnapshotRef | None
    medication_as_of: datetime


@dataclass(frozen=True, slots=True)
class MedicationDeviationPlan:
    intent: Literal["create", "revise", "withdraw", "restore"]
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId | None
    regime_logical_id: MedicationLogicalId
    scheduled_at: datetime
    actual_intakes: tuple[MedicationActualIntake, ...]
    withdrawal_reason: str | None
    base_snapshot_ref: SnapshotRef | None
    medication_as_of: datetime


@dataclass(frozen=True, slots=True)
class AsNeededIntakePlan:
    intent: Literal["create", "revise", "withdraw", "restore"]
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId | None
    regime_logical_id: MedicationLogicalId
    entry_id: MedicationPlanEntryId
    taken_at: datetime
    amount: Decimal
    reason_category_logical_id: MedicationLogicalId | None
    withdrawal_reason: str | None
    base_snapshot_ref: SnapshotRef | None
    medication_as_of: datetime


@dataclass(frozen=True, slots=True)
class IntakeReasonCategoryPlan:
    intent: Literal["create", "revise", "withdraw", "restore"]
    logical_id: MedicationLogicalId
    expected_revision_id: MedicationRevisionId | None
    name: str | None
    withdrawal_reason: str | None
    base_snapshot_ref: SnapshotRef | None
    medication_as_of: datetime


@dataclass(frozen=True, slots=True)
class IllnessRevisionPlan:
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
    severity: IllnessSeverity | None
    stress_level: StressLevel | None
    note: str | None
    withdrawal_reason: str | None
    base_snapshot_ref: SnapshotRef | None
    snapshot_as_of: datetime
    context_timezone: str


@dataclass(frozen=True, slots=True)
class RestingHeartRateAnalysisPlan:
    analysis_definition_id: AnalysisDefinitionId
    start_date: date | None
    end_date: date | None
    schema_version: str
    base_snapshot_ref: SnapshotRef | None


WritePlanDetails = (
    ImportHealthExportPlan
    | MetadataBackupPlan
    | MetadataRestorePlan
    | AbortMetadataRestorePlan
    | StoreMigrationPlan
    | RollbackMigrationPlan
    | DataReviewDecisionPlan
    | DataReviewBatchPlan
    | DataReviewBatchRevokePlan
    | PlausibilityRuleVersionPlan
    | HistoricalReviewPlan
    | RestingHeartRateAnalysisPlan
    | ManualContextRevisionPlan
    | MedicationRegimePlan
    | MedicationDeviationPlan
    | AsNeededIntakePlan
    | IntakeReasonCategoryPlan
    | IllnessRevisionPlan
)


@dataclass(frozen=True, slots=True)
class WritePreflight:
    approval: WriteApproval
    confirmations: tuple[WriteConfirmation, ...] = ()
    filevault: FileVaultCheck | None = None
    diagnostics: tuple[str, ...] = ()
    capacity: CapacityCheck | None = None


@dataclass(frozen=True, slots=True)
class WritePlan:
    fingerprint: PlanFingerprint
    details: WritePlanDetails
    preflight: WritePreflight

    @property
    def approval(self) -> WriteApproval:
        return self.preflight.approval

    @property
    def confirmations(self) -> tuple[WriteConfirmation, ...]:
        return self.preflight.confirmations

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return self.preflight.diagnostics


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
    source_occurrence_count: int = 0
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MetadataBackupReceipt:
    operation_id: OperationId
    backup_id: BackupId
    canonical_content_sha256: str
    audit_max_position: int
    created_at_utc: datetime
    target_file: str
    status: MetadataBackupStatus
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MetadataRestoreReceipt:
    operation_id: OperationId
    restore_id: RestoreId
    backup_id: BackupId
    original_backup_sha256: str
    working_copy_sha256: str
    status: MetadataRestoreStatus
    diagnostics: tuple[str, ...] = ()


class MigrationStatus(StrEnum):
    COMPLETED = "completed"
    NO_OP = "no_op"


@dataclass(frozen=True, slots=True)
class StoreMigrationReceipt:
    operation_id: OperationId
    status: MigrationStatus
    source_version: int
    target_version: int
    steps: tuple[tuple[int, int], ...]
    backup_file: str | None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RollbackMigrationReceipt:
    operation_id: OperationId
    migration_operation_id: OperationId
    status: MigrationStatus
    source_version: int
    target_version: int
    backup_file: str
    restored_snapshot_ref: SnapshotRef | None
    unreferenced_snapshot_ref: SnapshotRef | None
    diagnostics: tuple[str, ...] = ()


class DataReviewCaseKind(StrEnum):
    PLAUSIBILITY = "plausibility"
    CONTINUED_OVERRIDE = "continued_override"
    RULE_DEFINITION = "rule_definition"
    SUSPECTED_SOURCE_DELETION = "suspected_source_deletion"
    SOURCE_CONFLICT = "source_conflict"
    PREFERRED_DAILY_WEIGHT_CONFLICT = "preferred_daily_weight_conflict"
    WORKOUT_PLAUSIBILITY = "workout_plausibility"
    WORKOUT_OVERLAP = "workout_overlap"


class DataReviewAction(StrEnum):
    CONFIRM = "confirm"
    CORRECT = "correct"
    EXCLUDE_LOCAL = "exclude_local"
    ACCEPT_SOURCE = "accept_source"
    REJECT = "reject"
    PREFER = "prefer"
    SPLIT = "split"


class DataReviewCycleStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class DataReviewCycleKind(StrEnum):
    IMPORT = "import"
    RULE_VERSION = "rule_version"
    HISTORICAL = "historical"


@dataclass(frozen=True, slots=True)
class DataReviewCycleId:
    _value: str

    def __post_init__(self) -> None:
        if len(self._value) != 32 or not set(self._value) <= set("0123456789abcdef"):
            raise ValueError("Prüfzyklus-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self._value


@dataclass(frozen=True, slots=True)
class DataReviewCycle:
    cycle_id: DataReviewCycleId
    snapshot_ref: SnapshotRef
    status: DataReviewCycleStatus
    open_case_count: int
    kind: DataReviewCycleKind = DataReviewCycleKind.IMPORT
    base_snapshot_ref: SnapshotRef | None = None
    start_date: date | None = None
    end_date: date | None = None
    rule_version_id: str | None = None


class ReviewReasonCode(StrEnum):
    BELOW_FIXED_LOWER_BOUND = "below_fixed_lower_bound"
    ABOVE_FIXED_UPPER_BOUND = "above_fixed_upper_bound"
    BELOW_PERSONAL_LOWER_BOUND = "below_personal_lower_bound"
    ABOVE_PERSONAL_UPPER_BOUND = "above_personal_upper_bound"


class EffectiveValueSource(StrEnum):
    SOURCE = "source"
    CORRECTION = "correction"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class PlausibilityReason:
    code: ReviewReasonCode
    lower_bound: float
    upper_bound: float | None
    unit: str


@dataclass(frozen=True, slots=True)
class DataReviewCaseId:
    _value: str

    def __post_init__(self) -> None:
        if len(self._value) != 32 or not set(self._value) <= set("0123456789abcdef"):
            raise ValueError("Prüffall-ID muss ein 32-stelliger Hex-Wert sein.")

    def __str__(self) -> str:
        return self._value


@dataclass(frozen=True, slots=True)
class DataReviewSelection:
    kind: DataReviewCaseKind | None = None

    def __post_init__(self) -> None:
        if self.kind is not None and not isinstance(self.kind, DataReviewCaseKind):
            raise ConfigurationError("Prüffallart hat einen ungültigen Typ.")


@dataclass(frozen=True, slots=True)
class DataReviewCase:
    case_id: DataReviewCaseId
    kind: DataReviewCaseKind
    logical_measurement_id: LogicalMeasurementId | None
    measurement_version_id: MeasurementVersionId | None
    rule_version_id: str | None
    evidence_fingerprint: str
    candidate_version_ids: tuple[MeasurementVersionId, ...] = ()
    allowed_actions: tuple[DataReviewAction, ...] = ()


@dataclass(frozen=True, slots=True)
class DataReview:
    snapshot_ref: SnapshotRef | None
    cases: tuple[DataReviewCase, ...]
    status: DataQualityStatus = DataQualityStatus.REVIEWED
    cycles: tuple[DataReviewCycle, ...] = ()


@dataclass(frozen=True, slots=True)
class DataReviewCaseDetail:
    case: DataReviewCase
    source_type: str | None
    measured_at: datetime | None
    effective_value: float | None
    effective_value_source: EffectiveValueSource | None
    reasons: tuple[PlausibilityReason, ...]
    canonical_unit: CanonicalUnit | None


class WriteNotStartedStatus(StrEnum):
    PLAN_CHANGED = "plan_changed"
    BLOCKED = "blocked"
    STORE_BUSY = "store_busy"


class NoChangeStatus(StrEnum):
    NO_CHANGE = "no_change"


@dataclass(frozen=True, slots=True)
class WriteNotStarted:
    status: WriteNotStartedStatus
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WriteDecisionReceipt:
    operation_id: OperationId
    decision_id: DataReviewDecisionId
    snapshot_ref: SnapshotRef
    status: ImportStatus = ImportStatus.COMMITTED
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WriteBatchDecisionReceipt:
    operation_id: OperationId
    batch_action_id: DataReviewBatchActionId
    decision_ids: tuple[DataReviewDecisionId, ...]
    snapshot_ref: SnapshotRef
    status: ImportStatus = ImportStatus.COMMITTED
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlausibilityRuleVersionReceipt:
    operation_id: OperationId
    rule_version_id: str
    snapshot_ref: SnapshotRef | None
    status: ImportStatus = ImportStatus.COMMITTED
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HistoricalReviewReceipt:
    operation_id: OperationId
    cycle_id: DataReviewCycleId
    snapshot_ref: SnapshotRef
    open_case_count: int
    status: ImportStatus = ImportStatus.COMMITTED
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ManualContextRevisionReceipt:
    operation_id: OperationId
    logical_id: ContextLogicalId
    revision_id: ContextRevisionId
    snapshot_ref: SnapshotRef
    status: ImportStatus = ImportStatus.COMMITTED
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MedicationRegimeReceipt:
    operation_id: OperationId
    logical_id: MedicationLogicalId
    revision_id: MedicationRevisionId
    snapshot_ref: SnapshotRef
    status: ImportStatus = ImportStatus.COMMITTED
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MedicationDeviationReceipt:
    operation_id: OperationId
    logical_id: MedicationLogicalId
    revision_id: MedicationRevisionId
    snapshot_ref: SnapshotRef
    status: ImportStatus = ImportStatus.COMMITTED
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AsNeededIntakeReceipt:
    operation_id: OperationId
    logical_id: MedicationLogicalId
    revision_id: MedicationRevisionId
    snapshot_ref: SnapshotRef
    status: ImportStatus = ImportStatus.COMMITTED
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IntakeReasonCategoryReceipt:
    operation_id: OperationId
    logical_id: MedicationLogicalId
    revision_id: MedicationRevisionId
    snapshot_ref: SnapshotRef
    status: ImportStatus = ImportStatus.COMMITTED
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WriteNoChange:
    status: NoChangeStatus = NoChangeStatus.NO_CHANGE
    diagnostics: tuple[str, ...] = ()


class AnalysisStatus(StrEnum):
    COMPLETED = "completed"
    REUSED = "reused"
    INSUFFICIENT_DATA = "insufficient_data"
    UNSTABLE = "unstable"


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


WriteResult = (
    ImportReceipt
    | MetadataBackupReceipt
    | MetadataRestoreReceipt
    | StoreMigrationReceipt
    | RollbackMigrationReceipt
    | WriteDecisionReceipt
    | WriteBatchDecisionReceipt
    | PlausibilityRuleVersionReceipt
    | HistoricalReviewReceipt
    | ManualContextRevisionReceipt
    | MedicationRegimeReceipt
    | MedicationDeviationReceipt
    | AsNeededIntakeReceipt
    | IntakeReasonCategoryReceipt
    | AnalysisReceipt
    | WriteNoChange
    | WriteNotStarted
)


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    operation_id: OperationId
    plan_fingerprint: PlanFingerprint
    result: WriteResult
    final_preflight: WritePreflight
    diagnostics: tuple[str, ...] = ()


class HealthLab:
    """Small, deterministic lifecycle and operation seam for HealthLab."""

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._store: LocalStore | None = None
        self._overview_reader: OverviewReader | None = None

    @classmethod
    def open(cls, config: RuntimeConfig) -> Self:
        return cls(config)

    def __enter__(self) -> Self:
        if self._overview_reader is not None:
            raise HealthLabError("HealthLab-Sitzung ist bereits geöffnet.")
        try:
            self._store = LocalStore.open(
                root=self._config.active_store,
                mode=self._config.mode,
            )
            self._overview_reader = OverviewReader(self._store)
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
            self._store = None
        logger.info("healthlab_closed mode=%s", self._config.mode.value)

    def preview_write(self, request: WriteRequest) -> WritePlan:
        self._require_open()
        if not isinstance(request, (*get_args(WriteRequest), CreateActivityDerivationVersion)):
            raise ConfigurationError("Unbekannter Schreibauftrag.")
        if isinstance(request, BeginMetadataRestore):
            return self._build_metadata_restore_plan(request)
        if isinstance(request, AbortMetadataRestore):
            return self._build_metadata_restore_abort_plan()
        if self.load_workspace_status().state is WorkspaceState.RESTORE_PENDING and not isinstance(
            request, ImportHealthExport
        ):
            raise HealthLabError("Während der Wiederherstellung ist diese Operation gesperrt.")
        if isinstance(request, MigrateStore):
            return self._build_store_migration_plan()
        if isinstance(request, RollbackMigration):
            return self._build_migration_rollback_plan()
        if isinstance(request, CreateMetadataBackup):
            return self._build_metadata_backup_plan(request)
        if isinstance(request, ReviseContextCoverageStart):
            return self._build_context_coverage_start_plan(request)
        if isinstance(request, ReviseMedicationRegime):
            return self._build_medication_regime_plan(request)
        if isinstance(request, ReviseMedicationDeviation):
            return self._build_medication_deviation_plan(request)
        if isinstance(request, ReviseAsNeededIntake):
            return self._build_as_needed_intake_plan(request)
        if isinstance(request, ReviseIntakeReasonCategory):
            return self._build_intake_reason_category_plan(request)
        if isinstance(
            request,
            (
                ReviseIllnessCategory,
                ReviseIllnessPeriod,
                ReviseDailyStress,
                ReviseCustomContextLabel,
                ReviseCustomContextPeriod,
            ),
        ):
            return self._build_illness_plan(request)
        if isinstance(request, RunRestingHeartRateAnalysis):
            return self._build_resting_heart_rate_analysis_plan(request)
        if isinstance(request, RunHistoricalReview):
            return self._build_historical_review_plan(request)
        if isinstance(request, CreatePlausibilityRuleVersion):
            return self._build_plausibility_rule_plan(request)
        if isinstance(request, CreateActivityDerivationVersion):
            return self._build_activity_derivation_plan(request)
        if isinstance(
            request, (ResolveDataReviewCase, ConfirmDataReviewBatch, RevokeDataReviewDecision)
        ):
            return self._build_data_review_plan(request)
        assert isinstance(request, ImportHealthExport)
        filevault = (
            probe_filevault(self._config.active_store)
            if self._config.mode is DataMode.REAL
            else None
        )
        return self._build_import_plan(request, filevault)

    @staticmethod
    def _public_recovery_status(inspection: MetadataRestoreInspection) -> RecoveryStatus:
        assert inspection.working_copy_sha256 is not None
        return RecoveryStatus(
            inspection.restore_id,
            inspection.backup_id,
            inspection.original_backup_sha256,
            inspection.working_copy_sha256,
            inspection.audit_max_position,
            inspection.source_schema_version,
            inspection.target_schema_version,
            inspection.migration_steps,
            inspection.status,
        )

    def _build_metadata_restore_plan(
        self,
        request: BeginMetadataRestore,
        *,
        filevault_override: FileVaultCheck | None = None,
        capacity_override: CapacityCheck | None = None,
    ) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        inspection: MetadataRestoreInspection | None = None
        diagnostics: tuple[str, ...] = ()
        try:
            inspection = inspect_metadata_restore(
                self._store, request.backup_path, self._config.active_store
            )
        except StoreError as error:
            diagnostics = (str(error),)
        filevault = filevault_override or probe_filevault(self._config.active_store)
        capacity = capacity_override or preflight_metadata_restore_start(
            request.backup_path, self._config.active_store
        )
        blocked = inspection is None or capacity.status is not CapacityStatus.READY
        if capacity.status is not CapacityStatus.READY and not diagnostics:
            diagnostics = (
                "capacity_" + (capacity.reason.value if capacity.reason else capacity.status.value),
            )
        confirmations: list[WriteConfirmation] = []
        if inspection is not None and inspection.status is not MetadataRestoreStatus.NO_OP:
            confirmations.append(WriteConfirmation.METADATA_RESTORE)
            if filevault.status is not FileVaultStatus.PROTECTED:
                confirmations.append(WriteConfirmation("filevault_" + filevault.status.value))
        details = MetadataRestorePlan(
            None if inspection is None else inspection.restore_id,
            None if inspection is None else inspection.backup_id,
            None if inspection is None else inspection.source_store_id,
            None if inspection is None else inspection.original_backup_sha256,
            None if inspection is None else inspection.working_copy_sha256,
            None if inspection is None else inspection.audit_max_position,
            None if inspection is None else inspection.source_schema_version,
            2 if inspection is None else inspection.target_schema_version,
            () if inspection is None else inspection.migration_steps,
        )
        payload = {
            "backup_path": str(request.backup_path),
            "capacity": {
                "estimate_bytes": capacity.estimate_bytes,
                "fragment_size": capacity.fragment_size,
                "method_id": capacity.method_id,
                "status": capacity.status.value,
                "target_volume": capacity.target_volume,
            },
            "confirmations": [item.value for item in confirmations],
            "details": {
                "audit_max_position": details.audit_max_position,
                "backup_id": None if details.backup_id is None else str(details.backup_id),
                "migration_steps": details.migration_steps,
                "original_backup_sha256": details.original_backup_sha256,
                "restore_id": None if details.restore_id is None else str(details.restore_id),
                "source_store_id": (
                    None if details.source_store_id is None else str(details.source_store_id)
                ),
            },
            "diagnostics": diagnostics,
            "filevault": {
                "reason": None if filevault.reason is None else filevault.reason.value,
                "status": filevault.status.value,
                "target_volume": filevault.target_volume,
            },
            "operation": "begin_metadata_restore",
            "version": 1,
        }
        return WritePlan(
            PlanFingerprint(
                hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            ),
            details,
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED
                    if blocked
                    else (
                        WriteApprovalStatus.READY
                        if not confirmations
                        else WriteApprovalStatus.CONFIRMATION_REQUIRED
                    )
                ),
                tuple(confirmations),
                filevault,
                diagnostics,
                capacity,
            ),
        )

    def _build_metadata_restore_abort_plan(self) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        try:
            inspection = load_metadata_restore(self._store)
            details = AbortMetadataRestorePlan(inspection.restore_id, inspection.backup_id)
            diagnostics: tuple[str, ...] = ()
            approval = WriteApprovalStatus.READY
        except StoreError as error:
            details = AbortMetadataRestorePlan(None, None)
            diagnostics = (str(error),)
            approval = WriteApprovalStatus.BLOCKED
        payload = {
            "backup_id": None if details.backup_id is None else str(details.backup_id),
            "operation": "abort_metadata_restore",
            "restore_id": None if details.restore_id is None else str(details.restore_id),
        }
        return WritePlan(
            PlanFingerprint(
                hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            ),
            details,
            WritePreflight(WriteApproval(approval), diagnostics=diagnostics),
        )

    def _build_store_migration_plan(
        self,
        *,
        filevault_override: FileVaultCheck | None = None,
        capacity_override: CapacityCheck | None = None,
    ) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        diagnostics = self.load_migration_diagnostics()
        source = diagnostics.source_version
        backup_file = None
        if source is not None and diagnostics.steps:
            stem = f"metadata-v{source}-to-v{diagnostics.target_version}"
            backup_directory = self._config.active_store / "migration-backups"
            sequence = 1
            backup_file = f"{stem}.sqlite3"
            while (backup_directory / backup_file).exists() or (
                backup_directory / f"{backup_file}.tmp"
            ).exists():
                sequence += 1
                backup_file = f"{stem}-{sequence}.sqlite3"
        filevault = (
            filevault_override
            if filevault_override is not None
            else (
                probe_filevault(self._config.active_store)
                if self._config.mode is DataMode.REAL and diagnostics.steps
                else None
            )
        )
        capacity = (
            capacity_override
            if capacity_override is not None
            else (self._store.preflight_store_migration() if diagnostics.steps else None)
        )
        blocked = bool(diagnostics.diagnostics) or (
            capacity is not None and capacity.status is not CapacityStatus.READY
        )
        confirmations: tuple[WriteConfirmation, ...] = (
            () if not diagnostics.steps else (WriteConfirmation.STORE_MIGRATION,)
        )
        if filevault is not None and filevault.status is not FileVaultStatus.PROTECTED:
            confirmations += (WriteConfirmation("filevault_" + filevault.status.value),)
        plan_diagnostics = diagnostics.diagnostics
        if capacity is not None and capacity.status is not CapacityStatus.READY:
            plan_diagnostics += (
                "capacity_"
                + (capacity.reason.value if capacity.reason is not None else capacity.status.value),
            )
        active_snapshot = self._store.load_active_snapshot_id()
        affected_snapshot_refs = () if active_snapshot is None else (active_snapshot,)
        payload = {
            "affected_snapshot_refs": tuple(map(str, affected_snapshot_refs)),
            "backup_file": backup_file,
            "capacity": None
            if capacity is None
            else {
                "estimate": capacity.estimate_bytes,
                "fragment": capacity.fragment_size,
                "status": capacity.status.value,
                "target_volume": capacity.target_volume,
            },
            "filevault": None
            if filevault is None
            else {
                "reason": None if filevault.reason is None else filevault.reason.value,
                "status": filevault.status.value,
                "target_volume": filevault.target_volume,
            },
            "operation": "migrate_store",
            "source_version": source,
            "steps": diagnostics.steps,
            "target_version": diagnostics.target_version,
            "existing_analyses_become_stale": active_snapshot is not None,
            "version": 1,
        }
        return WritePlan(
            PlanFingerprint(
                hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            ),
            StoreMigrationPlan(
                source,
                diagnostics.target_version,
                diagnostics.steps,
                backup_file,
                affected_snapshot_refs,
                active_snapshot is not None,
            ),
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED
                    if blocked
                    else (
                        WriteApprovalStatus.CONFIRMATION_REQUIRED
                        if diagnostics.steps
                        else WriteApprovalStatus.READY
                    )
                ),
                confirmations,
                filevault,
                plan_diagnostics,
                capacity,
            ),
        )

    def _build_migration_rollback_plan(self) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        facts = self._store.load_migration_rollback_facts()
        diagnostics = plan_migration_rollback(facts)
        details = RollbackMigrationPlan(
            migration_operation_id=(None if facts is None else facts.migration_operation_id),
            source_version=None if facts is None else facts.post_migration_version,
            target_version=None if facts is None else facts.pre_migration_version,
            backup_file=None if facts is None else facts.backup_file,
            backup_sha256=None if facts is None else facts.backup_sha256,
            current_snapshot_ref=None if facts is None else facts.migrated_snapshot_id,
            restored_snapshot_ref=None if facts is None else facts.previous_snapshot_id,
        )
        payload = {
            "backup_file": details.backup_file,
            "backup_sha256": details.backup_sha256,
            "current_snapshot_ref": (
                None if details.current_snapshot_ref is None else str(details.current_snapshot_ref)
            ),
            "diagnostics": diagnostics,
            "migration_operation_id": (
                None
                if details.migration_operation_id is None
                else str(details.migration_operation_id)
            ),
            "operation": "rollback_migration",
            "restored_snapshot_ref": (
                None
                if details.restored_snapshot_ref is None
                else str(details.restored_snapshot_ref)
            ),
            "source_version": details.source_version,
            "target_version": details.target_version,
            "version": 1,
        }
        return WritePlan(
            PlanFingerprint(
                hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            ),
            details,
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED
                    if diagnostics
                    else WriteApprovalStatus.CONFIRMATION_REQUIRED
                ),
                () if diagnostics else (WriteConfirmation.MIGRATION_ROLLBACK,),
                diagnostics=diagnostics,
            ),
        )

    def _build_metadata_backup_plan(
        self,
        request: CreateMetadataBackup,
        *,
        filevault_override: FileVaultCheck | None = None,
        capacity_override: CapacityCheck | None = None,
    ) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        if self.load_workspace_status().state is WorkspaceState.MIGRATION_REQUIRED:
            details = MetadataBackupPlan(BackupId("0" * 32), "0" * 64, 0, request.target_path.name)
            payload = {
                "operation": "create_metadata_backup",
                "status": "migration_required",
                "target": request.target_path.name,
                "version": 1,
            }
            return WritePlan(
                PlanFingerprint(
                    hashlib.sha256(
                        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                ),
                details,
                WritePreflight(
                    WriteApproval(WriteApprovalStatus.BLOCKED),
                    diagnostics=("migration_required",),
                ),
            )
        if self._config.mode is not DataMode.REAL:
            details = MetadataBackupPlan(BackupId("0" * 32), "0" * 64, 0, request.target_path.name)
            payload = {"operation": "create_metadata_backup", "mode": "synthetic", "version": 1}
            return WritePlan(
                PlanFingerprint(
                    hashlib.sha256(
                        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                ),
                details,
                WritePreflight(
                    WriteApproval(WriteApprovalStatus.BLOCKED),
                    diagnostics=("real_store_required",),
                ),
            )
        try:
            description = describe_metadata_backup(self._store, request.target_path)
            filevault = filevault_override or probe_filevault(request.target_path.parent)
            capacity = capacity_override or preflight_metadata_backup(
                self._store, request.target_path
            )
        except StoreError as error:
            raise HealthLabError("Metadatensicherung konnte nicht geplant werden.") from error
        confirmations = [WriteConfirmation.METADATA_BACKUP_POINT_IN_TIME]
        if filevault.status is not FileVaultStatus.PROTECTED:
            confirmations.append(WriteConfirmation("filevault_" + filevault.status.value))
        diagnostics = (
            ()
            if capacity.status is CapacityStatus.READY
            else (
                "capacity_" + (capacity.reason.value if capacity.reason else capacity.status.value),
            )
        )
        blocked = capacity.status is not CapacityStatus.READY
        payload = {
            "audit_max_position": description.audit_max_position,
            "backup_id": str(description.backup_id),
            "canonical_content_sha256": description.canonical_content_sha256,
            "capacity": {
                "estimate": capacity.estimate_bytes,
                "fragment": capacity.fragment_size,
                "status": capacity.status.value,
                "target_volume": capacity.target_volume,
            },
            "filevault": {
                "reason": None if filevault.reason is None else filevault.reason.value,
                "status": filevault.status.value,
                "target_volume": filevault.target_volume,
            },
            "operation": "create_metadata_backup",
            "target_file": request.target_path.name,
            "version": 1,
        }
        return WritePlan(
            PlanFingerprint(
                hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            ),
            MetadataBackupPlan(
                description.backup_id,
                description.canonical_content_sha256,
                description.audit_max_position,
                description.target_file,
            ),
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED
                    if blocked
                    else WriteApprovalStatus.CONFIRMATION_REQUIRED
                ),
                tuple(confirmations),
                filevault,
                diagnostics,
                capacity,
            ),
        )

    def _build_resting_heart_rate_analysis_plan(
        self, request: RunRestingHeartRateAnalysis
    ) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        snapshot = self._store.load_active_snapshot_id()
        blocked = self.load_workspace_status().state is WorkspaceState.MIGRATION_REQUIRED
        payload = {
            "analysis_definition_id": str(request.analysis_definition_id),
            "base_snapshot_ref": None if snapshot is None else str(snapshot),
            "end_date": None if request.end_date is None else request.end_date.isoformat(),
            "operation": "run_resting_heart_rate_analysis",
            "schema_version": request.schema_version,
            "start_date": None if request.start_date is None else request.start_date.isoformat(),
            "version": 1,
        }
        return WritePlan(
            PlanFingerprint(
                hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            ),
            RestingHeartRateAnalysisPlan(
                request.analysis_definition_id,
                request.start_date,
                request.end_date,
                request.schema_version,
                snapshot,
            ),
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED if blocked else WriteApprovalStatus.READY
                ),
                diagnostics=("migration_required",) if blocked else (),
            ),
        )

    def _build_historical_review_plan(self, request: RunHistoricalReview) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        migration_required = self.load_workspace_status().state is WorkspaceState.MIGRATION_REQUIRED
        snapshot = self._store.load_active_snapshot_id()
        versions = next(
            (
                rule.versions
                for rule in self.load_plausibility_rules().rules
                if rule.data_type is request.data_type
            ),
            (),
        )
        selected = next(
            (version for version in versions if version.version_id == request.rule_version_id),
            versions[-1] if versions and request.rule_version_id is None else None,
        )
        blocked = (
            migration_required
            or snapshot is None
            or selected is None
            or not selected.specification.active
        )
        rule_version_id = request.rule_version_id or (
            "" if selected is None else selected.version_id
        )
        payload = {
            "base_snapshot_ref": None if snapshot is None else str(snapshot),
            "data_type": request.data_type.value,
            "end_date": request.end_date.isoformat(),
            "operation": "run_historical_review",
            "rule_version_id": rule_version_id,
            "start_date": request.start_date.isoformat(),
            "version": 1,
        }
        fingerprint = PlanFingerprint(
            hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        diagnostics = (
            ("migration_required",)
            if migration_required
            else (("historical_review_unavailable",) if blocked else ())
        )
        return WritePlan(
            fingerprint,
            HistoricalReviewPlan(
                request.start_date,
                request.end_date,
                rule_version_id,
                snapshot,
            ),
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED if blocked else WriteApprovalStatus.READY
                ),
                diagnostics=diagnostics,
            ),
        )

    def _build_activity_derivation_plan(
        self, request: CreateActivityDerivationVersion
    ) -> WritePlan:
        settings = self.load_activity_settings()
        payload = {
            "coverage_gap_minutes": request.coverage_gap_minutes,
            "previous_version_id": settings.active_version.version_id,
            "source_classifier_version": _ACTIVITY_SOURCE_CLASSIFIER_VERSION,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return WritePlan(
            PlanFingerprint(hashlib.sha256(b"plan:" + encoded).hexdigest()),
            PlausibilityRuleVersionPlan(
                settings.active_version.version_id,
                hashlib.sha256(b"activity-derivation:" + encoded).hexdigest(),
                self._store.load_active_snapshot_id() if self._store is not None else None,
            ),
            WritePreflight(WriteApproval(WriteApprovalStatus.READY)),
        )

    def _build_context_coverage_start_plan(self, request: ReviseContextCoverageStart) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        snapshot = self._store.load_active_snapshot_id()
        intent = request.intent
        kind: Literal["create", "revise", "withdraw", "restore"]
        start_date: date | None
        expected_revision_id: ContextRevisionId | None
        withdrawal_reason: str | None
        if isinstance(intent, ContextCoverageStartCreate):
            kind, start_date, expected_revision_id = "create", intent.start_date, None
            withdrawal_reason = None
            logical_id = ContextLogicalId(
                hashlib.sha256(b"context_coverage_start").hexdigest()[:32]
            )
        elif isinstance(intent, ContextCoverageStartRevise):
            kind, logical_id, start_date, expected_revision_id = (
                "revise",
                intent.logical_id,
                intent.start_date,
                intent.expected_revision_id,
            )
            withdrawal_reason = None
        elif isinstance(intent, ContextCoverageStartWithdraw):
            kind, logical_id, start_date, expected_revision_id = (
                "withdraw",
                intent.logical_id,
                None,
                intent.expected_revision_id,
            )
            withdrawal_reason = intent.reason.strip()
        else:
            assert isinstance(intent, ContextCoverageStartRestore)
            kind, logical_id, start_date, expected_revision_id = (
                "restore",
                intent.logical_id,
                intent.start_date,
                intent.expected_revision_id,
            )
            withdrawal_reason = None
        timezone = "Europe/Berlin"
        snapshot_as_of = datetime.combine(
            datetime.now(ZoneInfo(timezone)).date(), time.min, ZoneInfo(timezone)
        )
        current = None if snapshot is None else self._store.load_context_coverage_start(snapshot)
        blocked = snapshot is None or (
            start_date is not None and start_date > snapshot_as_of.date()
        )
        diagnostics = (
            ("context_requires_snapshot",)
            if snapshot is None
            else (("context_date_in_future",) if blocked else ())
        )
        if not blocked:
            if kind == "create":
                blocked = current is not None or bool(
                    self._store.load_context_coverage_audit(str(logical_id))
                )
            elif (
                (current is None and kind != "restore")
                or (current is not None and kind == "restore")
                or (
                    current is not None
                    and (
                        current.logical_id != str(logical_id)
                        or current.revision_id != str(expected_revision_id)
                    )
                )
            ):
                blocked = True
            if blocked and not diagnostics:
                diagnostics = ("context_revision_changed",)
        no_change = (
            not blocked
            and kind == "revise"
            and current is not None
            and current.start_date == start_date
        )
        payload = {
            "intent": kind,
            "logical_id": str(logical_id),
            "expected_revision_id": None
            if expected_revision_id is None
            else str(expected_revision_id),
            "start_date": None if start_date is None else start_date.isoformat(),
            "withdrawal_reason": withdrawal_reason,
            "base_snapshot_ref": None if snapshot is None else str(snapshot),
            "snapshot_as_of": snapshot_as_of.isoformat(),
            "context_timezone": timezone,
            "preflight": {
                "approval": "blocked" if blocked else ("no_change" if no_change else "ready"),
                "diagnostics": diagnostics,
            },
        }
        return WritePlan(
            PlanFingerprint(
                hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            ),
            ManualContextRevisionPlan(
                kind,
                logical_id,
                expected_revision_id,
                start_date,
                withdrawal_reason,
                snapshot,
                snapshot_as_of,
                timezone,
            ),
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED
                    if blocked
                    else (WriteApprovalStatus.NO_CHANGE if no_change else WriteApprovalStatus.READY)
                ),
                diagnostics=diagnostics,
            ),
        )

    def _build_medication_regime_plan(self, request: ReviseMedicationRegime) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        snapshot = self._store.load_active_snapshot_id()
        intent = request.intent
        if isinstance(intent, MedicationRegimeCreate):
            kind: Literal["create", "revise"] = "create"
            expected = None
            logical_id = MedicationLogicalId(
                hashlib.sha256(
                    repr((intent.starts_at, intent.timezone, intent.scheduled_doses)).encode()
                ).hexdigest()[:32]
            )
        else:
            kind, logical_id, expected = "revise", intent.logical_id, intent.expected_revision_id
        medication_as_of = self._store.load_medication_as_of(snapshot)
        current = self._store.load_medication_regime(snapshot, str(logical_id))
        as_needed_medications = tuple(
            item
            if item.entry_id is not None
            else replace(
                item,
                entry_id=MedicationPlanEntryId(
                    hashlib.sha256(repr((logical_id, index, item)).encode()).hexdigest()[:32]
                ),
            )
            for index, item in enumerate(intent.as_needed_medications)
        )
        active_regimes = self._store.load_active_medication_regimes(snapshot)
        blocked = snapshot is None or intent.starts_at.astimezone(
            UTC
        ) > medication_as_of.astimezone(UTC)
        diagnostics = ("medication_requires_snapshot",) if snapshot is None else ()
        if not blocked and (
            (kind == "create" and current is not None)
            or (kind == "revise" and (current is None or current.revision_id != str(expected)))
        ):
            blocked, diagnostics = True, ("medication_revision_changed",)
        if (
            not blocked
            and kind == "create"
            and any(regime.starts_at == intent.starts_at for regime in active_regimes)
        ):
            blocked, diagnostics = True, ("medication_start_exists",)
        if not blocked and kind == "revise":
            invalid_reference = any(
                not any(
                    _resolve_medication_local_datetime(
                        deviation.scheduled_at.astimezone(ZoneInfo(intent.timezone)).date(),
                        dose.local_time,
                        intent.timezone,
                    )
                    == deviation.scheduled_at
                    for dose in intent.scheduled_doses
                    if Weekday(
                        deviation.scheduled_at.astimezone(ZoneInfo(intent.timezone))
                        .strftime("%A")
                        .lower()
                    )
                    in dose.weekdays
                )
                for deviation in self._store.load_active_medication_deviations(snapshot)
                if deviation.regime_logical_id == str(logical_id)
            ) or any(
                not any(str(item.entry_id) == intake.entry_id for item in as_needed_medications)
                for intake in self._store.load_active_as_needed_intakes(snapshot)
                if intake.regime_logical_id == str(logical_id)
            )
            if invalid_reference:
                blocked, diagnostics = True, ("medication_reference_invalid",)
        if not blocked and any(
            self._store.load_intake_reason_category(snapshot, str(category_id)) is None
            for medication in as_needed_medications
            for category_id in medication.preferred_reason_category_ids
        ):
            blocked, diagnostics = True, ("intake_reason_category_not_active",)
        no_change = (
            not blocked
            and kind == "revise"
            and current is not None
            and current.starts_at == intent.starts_at
            and current.timezone == intent.timezone
            and current.scheduled_doses
            == tuple(
                (
                    dose.medication_name,
                    str(dose.amount),
                    dose.unit,
                    dose.local_time,
                    tuple(sorted(day.value for day in dose.weekdays)),
                )
                for dose in intent.scheduled_doses
            )
            and current.as_needed_medications
            == tuple(
                (
                    item.medication_name,
                    str(item.amount),
                    item.unit,
                    tuple(str(category_id) for category_id in item.preferred_reason_category_ids),
                    str(item.entry_id),
                )
                for item in as_needed_medications
            )
        )
        payload = {
            "intent": kind,
            "logical_id": str(logical_id),
            "expected_revision_id": None if expected is None else str(expected),
            "starts_at": intent.starts_at.isoformat(),
            "timezone": intent.timezone,
            "doses": [
                (
                    dose.medication_name,
                    str(dose.amount),
                    dose.unit,
                    dose.local_time.isoformat(),
                    sorted(dose.weekdays),
                )
                for dose in intent.scheduled_doses
            ],
            "as_needed": [
                (
                    item.medication_name,
                    str(item.amount),
                    item.unit,
                    tuple(str(category_id) for category_id in item.preferred_reason_category_ids),
                    str(item.entry_id),
                )
                for item in as_needed_medications
            ],
            "base_snapshot_ref": None if snapshot is None else str(snapshot),
            "medication_as_of": medication_as_of.isoformat(),
            "diagnostics": diagnostics,
            "approval": "no_change" if no_change else ("blocked" if blocked else "ready"),
        }
        return WritePlan(
            PlanFingerprint(
                hashlib.sha256(
                    json.dumps(payload, sort_keys=True, default=str).encode()
                ).hexdigest()
            ),
            MedicationRegimePlan(
                kind,
                logical_id,
                expected,
                intent.starts_at,
                intent.timezone,
                intent.scheduled_doses,
                as_needed_medications,
                snapshot,
                medication_as_of,
            ),
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED
                    if blocked
                    else (WriteApprovalStatus.NO_CHANGE if no_change else WriteApprovalStatus.READY)
                ),
                diagnostics=diagnostics,
            ),
        )

    def _build_medication_deviation_plan(self, request: ReviseMedicationDeviation) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        snapshot = self._store.load_active_snapshot_id()
        as_of = self._store.load_medication_as_of(snapshot)
        intent = request.intent
        if isinstance(intent, MedicationDeviationCreate):
            kind, expected = "create", None
            logical_id = MedicationLogicalId(
                hashlib.sha256(
                    repr((intent.regime_logical_id, intent.scheduled_at)).encode()
                ).hexdigest()[:32]
            )
            regime_id, scheduled_at, actual_intakes = (
                intent.regime_logical_id,
                intent.scheduled_at,
                intent.actual_intakes,
            )
            withdrawal_reason = None
        else:
            kind = (
                "revise"
                if isinstance(intent, MedicationDeviationRevise)
                else "withdraw"
                if isinstance(intent, MedicationDeviationWithdraw)
                else "restore"
            )
            logical_id, expected = intent.logical_id, intent.expected_revision_id
            current = self._store.load_medication_deviation(snapshot, str(logical_id))
            if current is None and isinstance(intent, MedicationDeviationRestore):
                audit = self._store.load_medication_deviation_audit(str(logical_id))
                current = audit[-1] if audit else None
            regime_id = MedicationLogicalId(current.regime_logical_id) if current else logical_id
            scheduled_at = current.scheduled_at if current else as_of
            actual_intakes = (
                () if isinstance(intent, MedicationDeviationWithdraw) else intent.actual_intakes
            )
            withdrawal_reason = (
                intent.reason if isinstance(intent, MedicationDeviationWithdraw) else None
            )
        current = self._store.load_medication_deviation(snapshot, str(logical_id))
        if current is None and isinstance(intent, MedicationDeviationRestore):
            audit = self._store.load_medication_deviation_audit(str(logical_id))
            current = audit[-1] if audit else None
        regimes = self._store.load_active_medication_regimes(snapshot)
        regime = next((item for item in regimes if item.logical_id == str(regime_id)), None)
        occurrence = (
            None
            if regime is None
            else next(
                (
                    (Decimal(amount), unit)
                    for _, amount, unit, local_time, weekdays in regime.scheduled_doses
                    if Weekday(
                        scheduled_at.astimezone(ZoneInfo(regime.timezone)).strftime("%A").lower()
                    )
                    in {Weekday(day) for day in weekdays}
                    and _resolve_medication_local_datetime(
                        scheduled_at.astimezone(ZoneInfo(regime.timezone)).date(),
                        local_time,
                        regime.timezone,
                    )
                    == scheduled_at
                ),
                None,
            )
        )
        blocked = (
            snapshot is None
            or occurrence is None
            or any(
                intake.taken_at.astimezone(UTC) > as_of.astimezone(UTC) for intake in actual_intakes
            )
        )
        identical_intake = (
            occurrence is not None
            and len(actual_intakes) == 1
            and actual_intakes[0].taken_at == scheduled_at
            and actual_intakes[0].amount == occurrence[0]
        )
        diagnostics = ("medication_requires_snapshot",) if snapshot is None else ()
        if not blocked and (
            (kind == "create" and current is not None and not identical_intake)
            or (kind != "create" and (current is None or current.revision_id != str(expected)))
        ):
            blocked, diagnostics = True, ("medication_revision_changed",)
        if (
            not blocked
            and kind == "create"
            and not identical_intake
            and self._store.load_medication_deviation(snapshot, str(logical_id))
        ):
            blocked, diagnostics = True, ("medication_deviation_exists",)
        no_change = not blocked and kind != "withdraw" and identical_intake
        payload = {
            "intent": kind,
            "logical_id": str(logical_id),
            "expected_revision_id": None if expected is None else str(expected),
            "regime_logical_id": str(regime_id),
            "scheduled_at": scheduled_at.isoformat(),
            "actual_intakes": [
                (item.taken_at.isoformat(), str(item.amount)) for item in actual_intakes
            ],
            "base_snapshot_ref": None if snapshot is None else str(snapshot),
            "medication_as_of": as_of.isoformat(),
            "diagnostics": diagnostics,
            "approval": "no_change" if no_change else ("blocked" if blocked else "ready"),
        }
        return WritePlan(
            PlanFingerprint(
                hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            ),
            MedicationDeviationPlan(
                cast(Literal["create", "revise", "withdraw", "restore"], kind),
                logical_id,
                expected,
                regime_id,
                scheduled_at,
                actual_intakes,
                withdrawal_reason,
                snapshot,
                as_of,
            ),
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED
                    if blocked
                    else (WriteApprovalStatus.NO_CHANGE if no_change else WriteApprovalStatus.READY)
                ),
                diagnostics=diagnostics,
            ),
        )

    def _build_intake_reason_category_plan(self, request: ReviseIntakeReasonCategory) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        snapshot = self._store.load_active_snapshot_id()
        as_of = self._store.load_medication_as_of(snapshot)
        intent = request.intent
        name: str | None
        if isinstance(intent, IntakeReasonCategoryCreate):
            kind: Literal["create", "revise", "withdraw", "restore"] = "create"
            name = " ".join(intent.name.split())
            logical_id = MedicationLogicalId(
                hashlib.sha256(name.casefold().encode()).hexdigest()[:32]
            )
            expected, withdrawal_reason = None, None
        else:
            kind = (
                "restore"
                if isinstance(intent, IntakeReasonCategoryRestore)
                else "withdraw"
                if isinstance(intent, IntakeReasonCategoryWithdraw)
                else "revise"
            )
            logical_id, expected = intent.logical_id, intent.expected_revision_id
            current = self._store.load_intake_reason_category(snapshot, str(logical_id))
            audit = self._store.load_intake_reason_category_audit(str(logical_id))
            name = (
                " ".join(intent.name.split())
                if not isinstance(intent, IntakeReasonCategoryWithdraw)
                else (current.name if current is not None else (audit[-1].name if audit else None))
            )
            withdrawal_reason = (
                intent.reason.strip() if isinstance(intent, IntakeReasonCategoryWithdraw) else None
            )
        current = self._store.load_intake_reason_category(snapshot, str(logical_id))
        audit = self._store.load_intake_reason_category_audit(str(logical_id))
        invalid_name = (
            name is None
            or not 1 <= len(name) <= 80
            or any(ord(char) < 32 for char in name)
            or name.casefold() == "sonstige"
        )
        blocked = snapshot is None or invalid_name
        diagnostics = (
            ("medication_requires_snapshot",)
            if snapshot is None
            else (("invalid_intake_reason_category_name",) if invalid_name else ())
        )
        if not blocked and (
            (kind == "create" and (current is not None or audit))
            or (
                kind == "restore"
                and (
                    not audit
                    or audit[-1].revision_id != str(expected)
                    or audit[-1].state != "withdrawn"
                )
            )
            or (
                kind in {"revise", "withdraw"}
                and (current is None or current.revision_id != str(expected))
            )
        ):
            blocked, diagnostics = True, ("medication_revision_changed",)
        if (
            not blocked
            and kind != "withdraw"
            and name is not None
            and self._store.is_intake_reason_category_name_reserved(
                name, excluding_logical_id=None if kind == "create" else str(logical_id)
            )
        ):
            blocked, diagnostics = True, ("intake_reason_category_reserved",)
        if (
            not blocked
            and kind == "withdraw"
            and any(
                str(logical_id) in entry[3]
                for regime in self._store.load_active_medication_regimes(snapshot)
                for entry in regime.as_needed_medications
            )
        ):
            blocked, diagnostics = True, ("intake_reason_category_preferred",)
        no_change = (
            not blocked and kind == "revise" and current is not None and current.name == name
        )
        payload = {
            "intent": kind,
            "logical_id": str(logical_id),
            "expected_revision_id": None if expected is None else str(expected),
            "name": name,
            "withdrawal_reason": withdrawal_reason,
            "base_snapshot_ref": None if snapshot is None else str(snapshot),
            "medication_as_of": as_of.isoformat(),
            "diagnostics": diagnostics,
            "approval": "no_change" if no_change else ("blocked" if blocked else "ready"),
        }
        return WritePlan(
            PlanFingerprint(
                hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            ),
            IntakeReasonCategoryPlan(
                kind, logical_id, expected, name, withdrawal_reason, snapshot, as_of
            ),
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED
                    if blocked
                    else (WriteApprovalStatus.NO_CHANGE if no_change else WriteApprovalStatus.READY)
                ),
                diagnostics=diagnostics,
            ),
        )

    def _build_as_needed_intake_plan(self, request: ReviseAsNeededIntake) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        snapshot = self._store.load_active_snapshot_id()
        as_of = self._store.load_medication_as_of(snapshot)
        intent = request.intent
        if isinstance(intent, AsNeededIntakeCreate):
            kind: Literal["create", "revise", "withdraw", "restore"] = "create"
            logical_id = MedicationLogicalId(
                hashlib.sha256(
                    repr((intent.regime_logical_id, intent.entry_id, intent.taken_at)).encode()
                ).hexdigest()[:32]
            )
            regime_id, entry_id, taken_at, amount, category_id = (
                intent.regime_logical_id,
                intent.entry_id,
                intent.taken_at,
                intent.amount,
                intent.reason_category_logical_id,
            )
            expected, withdrawal_reason = None, None
        else:
            kind = (
                "restore"
                if isinstance(intent, AsNeededIntakeRestore)
                else "withdraw"
                if isinstance(intent, AsNeededIntakeWithdraw)
                else "revise"
            )
            logical_id, expected = intent.logical_id, intent.expected_revision_id
            current = self._store.load_as_needed_intake(snapshot, str(logical_id))
            audit = self._store.load_as_needed_intake_audit(str(logical_id))
            source = current if current is not None else (audit[-1] if audit else None)
            regime_id = (
                MedicationLogicalId(source.regime_logical_id) if source is not None else logical_id
            )
            entry_id = (
                MedicationPlanEntryId(source.entry_id)
                if source is not None
                else MedicationPlanEntryId(str(logical_id))
            )
            if isinstance(intent, AsNeededIntakeWithdraw):
                taken_at = source.taken_at if source is not None else as_of
                amount = Decimal(source.amount) if source is not None else Decimal("1")
                category_id = (
                    None
                    if source is None or source.reason_category_logical_id is None
                    else MedicationLogicalId(source.reason_category_logical_id)
                )
                withdrawal_reason = intent.reason.strip()
            else:
                taken_at, amount, category_id = (
                    intent.taken_at,
                    intent.amount,
                    intent.reason_category_logical_id,
                )
                withdrawal_reason = None
        current = self._store.load_as_needed_intake(snapshot, str(logical_id))
        audit = self._store.load_as_needed_intake_audit(str(logical_id))
        regimes = self._store.load_active_medication_regimes(snapshot)
        regime = next((item for item in regimes if item.logical_id == str(regime_id)), None)
        next_regime = (
            None
            if regime is None
            else next((item for item in regimes if item.starts_at > regime.starts_at), None)
        )
        entry = (
            None
            if regime is None
            else next(
                (item for item in regime.as_needed_medications if item[4] == str(entry_id)), None
            )
        )
        reference_invalid = (
            regime is None
            or entry is None
            or taken_at < regime.starts_at
            or (next_regime is not None and taken_at >= next_regime.starts_at)
            or (
                category_id is not None
                and self._store.load_intake_reason_category(snapshot, str(category_id)) is None
            )
        )
        blocked = (
            snapshot is None
            or reference_invalid
            or taken_at.astimezone(UTC) > as_of.astimezone(UTC)
        )
        diagnostics = (
            ("medication_requires_snapshot",)
            if snapshot is None
            else (("as_needed_intake_reference_invalid",) if reference_invalid else ())
        )
        if not blocked and (
            (kind == "create" and (current is not None or audit))
            or (
                kind == "restore"
                and (
                    not audit
                    or audit[-1].revision_id != str(expected)
                    or audit[-1].state != "withdrawn"
                )
            )
            or (
                kind in {"revise", "withdraw"}
                and (current is None or current.revision_id != str(expected))
            )
        ):
            blocked, diagnostics = True, ("medication_revision_changed",)
        no_change = (
            not blocked
            and kind == "revise"
            and current is not None
            and current.taken_at == taken_at
            and current.amount == str(amount)
            and current.reason_category_logical_id
            == (None if category_id is None else str(category_id))
        )
        payload = {
            "intent": kind,
            "logical_id": str(logical_id),
            "expected_revision_id": None if expected is None else str(expected),
            "regime_logical_id": str(regime_id),
            "entry_id": str(entry_id),
            "taken_at": taken_at.isoformat(),
            "amount": str(amount),
            "reason_category_logical_id": None if category_id is None else str(category_id),
            "withdrawal_reason": withdrawal_reason,
            "base_snapshot_ref": None if snapshot is None else str(snapshot),
            "medication_as_of": as_of.isoformat(),
            "diagnostics": diagnostics,
            "approval": "no_change" if no_change else ("blocked" if blocked else "ready"),
        }
        return WritePlan(
            PlanFingerprint(
                hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            ),
            AsNeededIntakePlan(
                kind,
                logical_id,
                expected,
                regime_id,
                entry_id,
                taken_at,
                amount,
                category_id,
                withdrawal_reason,
                snapshot,
                as_of,
            ),
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED
                    if blocked
                    else (WriteApprovalStatus.NO_CHANGE if no_change else WriteApprovalStatus.READY)
                ),
                diagnostics=diagnostics,
            ),
        )

    def _build_illness_plan(
        self,
        request: ReviseIllnessCategory
        | ReviseIllnessPeriod
        | ReviseDailyStress
        | ReviseCustomContextLabel
        | ReviseCustomContextPeriod,
    ) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        intent = request.intent
        object_kind = (
            "illness_category"
            if isinstance(request, ReviseIllnessCategory)
            else "illness_period"
            if isinstance(request, ReviseIllnessPeriod)
            else "daily_stress"
            if isinstance(request, ReviseDailyStress)
            else "custom_context_label"
            if isinstance(request, ReviseCustomContextLabel)
            else "custom_context_period"
        )
        kind: Literal["create", "revise", "withdraw", "restore"] = (
            "create"
            if type(intent).__name__.endswith("Create")
            else "revise"
            if type(intent).__name__.endswith("Revise")
            else "withdraw"
            if type(intent).__name__.endswith("Withdraw")
            else "restore"
        )
        expected_revision_id = getattr(intent, "expected_revision_id", None)
        name = getattr(intent, "name", None)
        category_logical_id = getattr(
            intent, "category_logical_id", getattr(intent, "label_logical_id", None)
        )
        start_date = getattr(intent, "start_date", getattr(intent, "day", None))
        end_date = getattr(intent, "end_date", None)
        severity = getattr(intent, "severity", None)
        stress_level = getattr(intent, "level", None)
        note = getattr(intent, "note", None)
        withdrawal_reason = getattr(intent, "reason", None)
        if name is not None:
            name = " ".join(name.split())
        if note is not None:
            note = note.strip()
        snapshot = self._store.load_active_snapshot_id()
        timezone = "Europe/Berlin"
        snapshot_as_of = datetime.combine(
            datetime.now(ZoneInfo(timezone)).date(), time.min, ZoneInfo(timezone)
        )
        identity = json.dumps(
            [
                object_kind,
                name,
                None if category_logical_id is None else str(category_logical_id),
                None if start_date is None else start_date.isoformat(),
                None if end_date is None else end_date.isoformat(),
                None if severity is None else severity.value,
                None if stress_level is None else stress_level.value,
                note,
            ],
            sort_keys=True,
        )
        logical_id = getattr(intent, "logical_id", None) or ContextLogicalId(
            hashlib.sha256(identity.encode()).hexdigest()[:32]
        )
        active = self._store.load_active_illness(snapshot)
        current = next((value for value in active if value.logical_id == str(logical_id)), None)
        audit = self._store.load_illness_revisions(str(logical_id))
        blocked = snapshot is None
        diagnostics: tuple[str, ...] = ("context_requires_snapshot",) if blocked else ()
        if name is not None and (
            not name or len(name) > 80 or any(ord(char) < 32 for char in name)
        ):
            blocked, diagnostics = True, ("invalid_context_label_name",)
        if note is not None and (len(note) > 1000 or any(ord(char) < 32 for char in note)):
            blocked, diagnostics = True, ("invalid_context_note",)
        if (
            object_kind == "custom_context_label"
            and name is not None
            and self._store.is_custom_context_label_name_reserved(
                name,
                excluding_logical_id=None if kind == "create" else str(logical_id),
            )
        ):
            blocked, diagnostics = True, ("custom_context_label_reserved",)
        if start_date is not None and (
            start_date > snapshot_as_of.date() or (end_date is not None and start_date > end_date)
        ):
            blocked, diagnostics = True, ("invalid_illness_period_dates",)
        if object_kind in {"illness_period", "custom_context_period"} and not blocked:
            categories = {
                value.logical_id
                for value in active
                if value.object_kind
                == (
                    "illness_category"
                    if object_kind == "illness_period"
                    else "custom_context_label"
                )
            }
            if category_logical_id is None or str(category_logical_id) not in categories:
                blocked, diagnostics = True, ("context_label_not_active",)
            elif start_date is not None:
                requested_end = snapshot_as_of.date() if end_date is None else end_date
                for value in active:
                    if value.object_kind != object_kind or value.logical_id == str(logical_id):
                        continue
                    if (
                        value.category_logical_id == str(category_logical_id)
                        and value.start_date is not None
                    ):
                        value_end = (
                            snapshot_as_of.date() if value.end_date is None else value.end_date
                        )
                        if value.start_date <= requested_end and start_date <= value_end:
                            blocked, diagnostics = True, ("context_period_overlap",)
                            break
        if object_kind == "daily_stress" and not blocked:
            if stress_level is None or start_date is None:
                blocked, diagnostics = True, ("invalid_daily_stress",)
            elif any(
                value.object_kind == "daily_stress"
                and value.start_date == start_date
                and value.logical_id != str(logical_id)
                for value in active
            ):
                blocked, diagnostics = True, ("daily_stress_exists",)
        if not blocked:
            if kind == "create":
                blocked = bool(audit)
            elif not audit or audit[-1].revision_id != str(expected_revision_id):
                blocked = True
            elif kind == "withdraw" or kind == "revise":
                blocked = current is None
            else:
                blocked = current is not None
            if blocked and not diagnostics:
                diagnostics = ("context_revision_changed",)
        no_change = (
            kind == "revise"
            and current is not None
            and (
                (
                    object_kind in {"illness_category", "custom_context_label"}
                    and current.name == name
                )
                or (
                    object_kind in {"illness_period", "custom_context_period"}
                    and current.category_logical_id == str(category_logical_id)
                    and current.start_date == start_date
                    and current.end_date == end_date
                    and current.severity == (None if severity is None else severity.value)
                    and current.note == note
                )
                or (
                    object_kind == "daily_stress"
                    and current.start_date == start_date
                    and current.stress_level
                    == (None if stress_level is None else stress_level.value)
                )
            )
        )
        payload = {
            "kind": object_kind,
            "intent": kind,
            "logical_id": str(logical_id),
            "expected": None if expected_revision_id is None else str(expected_revision_id),
            "name": name,
            "category": None if category_logical_id is None else str(category_logical_id),
            "start": None if start_date is None else start_date.isoformat(),
            "end": None if end_date is None else end_date.isoformat(),
            "severity": None if severity is None else severity.value,
            "stress": None if stress_level is None else stress_level.value,
            "note": note,
            "snapshot": None if snapshot is None else str(snapshot),
        }
        return WritePlan(
            PlanFingerprint(
                hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            ),
            IllnessRevisionPlan(
                kind,
                cast(
                    Literal[
                        "illness_category",
                        "illness_period",
                        "daily_stress",
                        "custom_context_label",
                        "custom_context_period",
                    ],
                    object_kind,
                ),
                logical_id,
                expected_revision_id,
                name,
                category_logical_id,
                start_date,
                end_date,
                severity,
                stress_level,
                note,
                withdrawal_reason,
                snapshot,
                snapshot_as_of,
                timezone,
            ),
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED
                    if blocked
                    else (WriteApprovalStatus.NO_CHANGE if no_change else WriteApprovalStatus.READY)
                ),
                diagnostics=diagnostics,
            ),
        )

    def _build_plausibility_rule_plan(self, request: CreatePlausibilityRuleVersion) -> WritePlan:
        rules = self.load_plausibility_rules()
        versions = next(
            (rule.versions for rule in rules.rules if rule.data_type is request.data_type), ()
        )
        previous = versions[-1] if versions else None
        blocked = (previous is None) != (request.effective_from is None)
        recommendation = next(
            (rule.recommendation for rule in rules.rules if rule.data_type is request.data_type),
            None,
        )
        if request.recommendation_id is not None and (
            recommendation is None
            or request.recommendation_id != recommendation.recommendation_id
            or request.specification != recommendation.specification
        ):
            blocked = True
        if (
            previous is not None
            and previous.effective_from is not None
            and request.effective_from is not None
            and request.effective_from <= previous.effective_from
        ):
            blocked = True
        effective_timezone = (
            None
            if request.effective_from is None
            else getattr(
                request.effective_from.tzinfo,
                "key",
                request.effective_from.tzname(),
            )
        )
        effective_offset = (
            None if request.effective_from is None else request.effective_from.utcoffset()
        )
        payload = {
            "data_type": request.data_type.value,
            "effective_from": (
                None if request.effective_from is None else request.effective_from.isoformat()
            ),
            "lower": request.specification.fixed_lower_bound,
            "effective_offset_minutes": (
                None if effective_offset is None else int(effective_offset.total_seconds() // 60)
            ),
            "effective_timezone": effective_timezone,
            "upper": request.specification.fixed_upper_bound,
            "personal": request.specification.personal_range_enabled,
            "previous": None if previous is None else previous.version_id,
            "recommendation_id": request.recommendation_id,
            "unit": request.specification.unit.value,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        version_id = hashlib.sha256(b"plausibility-rule:" + encoded).hexdigest()
        fingerprint = PlanFingerprint(hashlib.sha256(b"plan:" + encoded).hexdigest())
        diagnostics = ("invalid_rule_boundary",) if blocked else ()
        return WritePlan(
            fingerprint,
            PlausibilityRuleVersionPlan(
                None if previous is None else previous.version_id,
                version_id,
                self._store.load_active_snapshot_id() if self._store is not None else None,
            ),
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED if blocked else WriteApprovalStatus.READY
                ),
                diagnostics=diagnostics,
            ),
        )

    def _build_data_review_plan(
        self,
        request: ResolveDataReviewCase | ConfirmDataReviewBatch | RevokeDataReviewDecision,
    ) -> WritePlan:
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        snapshot = self._store.load_active_snapshot_id()
        if snapshot is None:
            if isinstance(request, ConfirmDataReviewBatch):
                details: WritePlanDetails = DataReviewBatchPlan(request.selection, (), 0, None)
            elif isinstance(request, RevokeDataReviewDecision) and isinstance(
                request.target, BatchDecisionTarget
            ):
                details = DataReviewBatchRevokePlan(request.target.batch_action_id, (), 0, None)
            else:
                details = DataReviewDecisionPlan(None, None)
            return WritePlan(
                PlanFingerprint(hashlib.sha256(b"data_review:no_snapshot").hexdigest()),
                details,
                WritePreflight(
                    WriteApproval(WriteApprovalStatus.BLOCKED), diagnostics=("no_active_snapshot",)
                ),
            )
        if isinstance(request, ResolveDataReviewCase):
            case_id = request.case_id
            resolution = request.resolution
            if isinstance(resolution, DataConfirmation):
                resolution_payload: dict[str, object] = {
                    "type": "confirmation",
                    "note": resolution.note,
                }
            elif isinstance(resolution, DataCorrection):
                resolution_payload = {
                    "type": "correction",
                    "measurement_version_id": str(resolution.measurement_version_id),
                    "corrected_value": resolution.corrected_value,
                    "unit": resolution.unit.value,
                    "reason": resolution.reason,
                    "note": resolution.note,
                }
            elif isinstance(resolution, LocalMeasurementExclusion):
                resolution_payload = {
                    "type": "local_exclusion",
                    "measurement_version_id": str(resolution.measurement_version_id),
                    "reason": resolution.reason,
                    "note": resolution.note,
                }
            elif isinstance(resolution, WorkoutCorrection):
                resolution_payload = {
                    "type": "workout_correction",
                    "workout_version_id": str(resolution.workout_version_id),
                    "effective_duration_minutes": resolution.effective_duration_minutes,
                    "distance_kilometers": resolution.distance_kilometers,
                    "active_energy_kilocalories": resolution.active_energy_kilocalories,
                    "reason": resolution.reason,
                    "note": resolution.note,
                }
            elif isinstance(resolution, LocalWorkoutExclusion):
                resolution_payload = {
                    "type": "local_workout_exclusion",
                    "workout_version_id": str(resolution.workout_version_id),
                    "reason": resolution.reason,
                    "note": resolution.note,
                }
            elif isinstance(resolution, SourceValueAcceptance):
                resolution_payload = {
                    "type": "source_value_acceptance",
                    "measurement_version_id": str(resolution.measurement_version_id),
                    "note": resolution.note,
                }
            elif isinstance(resolution, SourceDeletionResolution):
                resolution_payload = {
                    "type": "source_deletion",
                    "verdict": resolution.verdict.value,
                    "note": resolution.note,
                }
            else:
                resolution_payload = {
                    "type": "source_conflict",
                    "strategy": resolution.strategy.value,
                    "preferred_version_id": (
                        None
                        if resolution.preferred_version_id is None
                        else str(resolution.preferred_version_id)
                    ),
                    "note": resolution.note,
                }
            request_payload: dict[str, object] = {
                "type": "resolve_data_review_case",
                "case_id": None if case_id is None else str(case_id),
                "resolution": resolution_payload,
            }
        elif isinstance(request, ConfirmDataReviewBatch):
            case_id = None
            review = self.load_data_review(request.selection)
            matches = tuple(
                DataReviewBatchMatch(
                    case.case_id,
                    case.kind,
                    case.measurement_version_id,
                    case.rule_version_id,
                    case.evidence_fingerprint,
                    detail.effective_value,
                    detail.canonical_unit,
                )
                for case in sorted(review.cases, key=lambda item: str(item.case_id))
                if DataReviewAction.CONFIRM in case.allowed_actions
                for detail in (self.load_data_review_case(case.case_id),)
            )
            request_payload = {
                "type": "confirm_data_review_batch",
                "selection": {
                    "kind": None if request.selection.kind is None else request.selection.kind.value
                },
                "note": request.note,
                "matches": [_data_review_batch_match_payload(item) for item in matches],
                "count": len(matches),
            }
        else:
            case_id = None
            revoke_decisions = (
                tuple(
                    DataReviewDecisionId(item)
                    for item in self._store.load_effective_batch_decision_ids(
                        str(request.target.batch_action_id)
                    )
                )
                if isinstance(request.target, BatchDecisionTarget)
                else ()
            )
            request_payload = {
                "type": "revoke_data_review_decision",
                "target": (
                    {"type": "single", "decision_id": str(request.target.decision_id)}
                    if isinstance(request.target, SingleDecisionTarget)
                    else {"type": "batch", "batch_action_id": str(request.target.batch_action_id)}
                ),
                "effective_decision_ids": tuple(str(item) for item in revoke_decisions),
                "reason": request.reason,
            }
        fingerprint = PlanFingerprint(
            hashlib.sha256(
                json.dumps(
                    {"request": request_payload, "snapshot": str(snapshot), "version": 1},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        if isinstance(request, ConfirmDataReviewBatch):
            details = DataReviewBatchPlan(request.selection, matches, len(matches), snapshot)
        elif isinstance(request, RevokeDataReviewDecision) and isinstance(
            request.target, BatchDecisionTarget
        ):
            details = DataReviewBatchRevokePlan(
                request.target.batch_action_id,
                revoke_decisions,
                len(revoke_decisions),
                snapshot,
            )
        else:
            details = DataReviewDecisionPlan(case_id, snapshot)
        empty_batch = isinstance(details, (DataReviewBatchPlan, DataReviewBatchRevokePlan)) and (
            details.count == 0
        )
        return WritePlan(
            fingerprint,
            details,
            WritePreflight(
                WriteApproval(
                    WriteApprovalStatus.BLOCKED if empty_batch else WriteApprovalStatus.READY
                ),
                diagnostics=("empty_batch",) if empty_batch else (),
            ),
        )

    def _build_import_plan(
        self,
        request: ImportHealthExport,
        filevault: FileVaultCheck | None,
    ) -> WritePlan:
        workspace = self.load_workspace_status()
        try:
            package_size = request.package_path.stat().st_size
            with request.package_path.open("rb") as package:
                package_hash = hashlib.file_digest(package, "sha256").hexdigest()
            unavailable = False
            if workspace.state is WorkspaceState.RESTORE_PENDING and self._store is not None:
                restore = inspect_restore_health_export(
                    request.package_path,
                    store=self._store,
                    target_root=self._config.active_store,
                    max_package_bytes=self._config.max_import_package_bytes,
                    max_entries=self._config.max_import_entries,
                    max_entry_bytes=self._config.max_import_entry_bytes,
                    max_uncompressed_bytes=self._config.max_import_uncompressed_bytes,
                    max_compression_ratio=self._config.max_import_compression_ratio,
                )
                estimate = restore.estimate
                capacity = restore.capacity
                restore_state_hash = restore.sources.state_hash
            else:
                estimate = estimate_health_export(
                    request.package_path,
                    max_package_bytes=self._config.max_import_package_bytes,
                    max_entries=self._config.max_import_entries,
                    max_entry_bytes=self._config.max_import_entry_bytes,
                    max_uncompressed_bytes=self._config.max_import_uncompressed_bytes,
                    max_compression_ratio=self._config.max_import_compression_ratio,
                )
                capacity = None
                restore_state_hash = None
        except (OSError, StoreError):
            package_size = 0
            package_hash = ""
            unavailable = True
            estimate = HealthExportEstimate(0, 0)
            capacity = None
            restore_state_hash = None
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        if capacity is None:
            capacity = self._store.preflight_full_snapshot_import(
                estimate.input_bytes, estimate.record_count
            )
        return self._compose_import_plan(
            request,
            workspace,
            filevault,
            package_size,
            package_hash,
            estimate,
            capacity,
            restore_state_hash,
            unavailable=unavailable,
        )

    def _compose_import_plan(
        self,
        request: ImportHealthExport,
        workspace: WorkspaceStatus,
        filevault: FileVaultCheck | None,
        package_size: int,
        package_hash: str,
        estimate: HealthExportEstimate,
        capacity: CapacityCheck,
        restore_state_hash: str | None,
        *,
        unavailable: bool,
    ) -> WritePlan:
        confirmations: tuple[WriteConfirmation, ...]
        diagnostics: tuple[str, ...]
        if unavailable:
            approval = WriteApproval(WriteApprovalStatus.BLOCKED)
            confirmations = ()
            diagnostics = ("health_export_unavailable",)
        else:
            approval = WriteApproval(WriteApprovalStatus.READY)
            confirmations = ()
            diagnostics = ()
        if workspace.state is WorkspaceState.MIGRATION_REQUIRED:
            approval = WriteApproval(WriteApprovalStatus.BLOCKED)
            confirmations = ()
            diagnostics = ("migration_required",)
        elif capacity.status is not CapacityStatus.READY:
            approval = WriteApproval(WriteApprovalStatus.BLOCKED)
            confirmations = ()
            diagnostics = (
                "capacity_"
                + (capacity.reason.value if capacity.reason is not None else capacity.status.value),
            )
        if (
            approval.status is not WriteApprovalStatus.BLOCKED
            and filevault is not None
            and filevault.reason is FileVaultReason.VOLUME_LOCKED
        ):
            approval = WriteApproval(WriteApprovalStatus.BLOCKED)
            confirmations = ()
            diagnostics = ("target_locked",)
        elif approval.status is not WriteApprovalStatus.BLOCKED and filevault is not None:
            confirmation_list = (
                []
                if workspace.state is WorkspaceState.RESTORE_PENDING
                else [WriteConfirmation.REAL_IMPORT_SAME_PERSON]
            )
            if filevault.status is not FileVaultStatus.PROTECTED:
                confirmation_list.append(WriteConfirmation(f"filevault_{filevault.status.value}"))
            confirmations = tuple(confirmation_list)
            diagnostics = tuple(confirmation.value for confirmation in confirmations)
            approval = WriteApproval(
                WriteApprovalStatus.CONFIRMATION_REQUIRED
                if confirmations
                else WriteApprovalStatus.READY
            )
        fingerprint = PlanFingerprint(
            hashlib.sha256(
                json.dumps(
                    {
                        "limits": {
                            "compression_ratio": self._config.max_import_compression_ratio,
                            "entries": self._config.max_import_entries,
                            "entry_bytes": self._config.max_import_entry_bytes,
                            "package_bytes": self._config.max_import_package_bytes,
                            "uncompressed_bytes": self._config.max_import_uncompressed_bytes,
                        },
                        "mode": self._config.mode.value,
                        "operation": "import_health_export",
                        "package_hash": package_hash,
                        "package_path": str(request.package_path),
                        "package_size": package_size,
                        "store": str(self._config.active_store),
                        "store_id": str(workspace.store_id),
                        "person_binding": workspace.person_binding.value,
                        "restore_state_hash": restore_state_hash,
                        "confirmations": [item.value for item in confirmations],
                        "filevault": (
                            None
                            if filevault is None
                            else {
                                "status": filevault.status.value,
                                "target_volume": filevault.target_volume,
                                "reason": (
                                    None if filevault.reason is None else filevault.reason.value
                                ),
                            }
                        ),
                        "capacity": {
                            "status": capacity.status.value,
                            "target_volume": capacity.target_volume,
                            "method_id": capacity.method_id,
                            "estimate_bytes": capacity.estimate_bytes,
                            "safety_margin_bytes": capacity.safety_margin_bytes,
                            "minimum_remaining_bytes": capacity.minimum_remaining_bytes,
                            "required_bytes": capacity.required_bytes,
                            "fragment_size": capacity.fragment_size,
                            "reason": None if capacity.reason is None else capacity.reason.value,
                        },
                        "version": 1,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        return WritePlan(
            fingerprint=fingerprint,
            details=ImportHealthExportPlan(
                package_hash,
                package_size,
                estimate.input_bytes,
                estimate.record_count,
                restore_state_hash,
            ),
            preflight=WritePreflight(approval, confirmations, filevault, diagnostics, capacity),
        )

    @staticmethod
    def _filevault_allows_execution(expected: FileVaultCheck, actual: FileVaultCheck) -> bool:
        return expected.target_volume == actual.target_volume and (
            expected == actual or actual.status is FileVaultStatus.PROTECTED
        )

    def _authorization_plan(
        self,
        request: WriteRequest,
        current_plan: WritePlan,
        expected_plan: PlanFingerprint,
    ) -> WritePlan | None:
        if current_plan.fingerprint == expected_plan:
            return current_plan
        if isinstance(request, CreateMetadataBackup):
            filevault = current_plan.preflight.filevault
            capacity = current_plan.preflight.capacity
            if (
                filevault is None
                or filevault.status is not FileVaultStatus.PROTECTED
                or capacity is None
            ):
                return None
            candidates = [
                self._build_metadata_backup_plan(
                    request,
                    filevault_override=FileVaultCheck(status, filevault.target_volume),
                    capacity_override=capacity,
                )
                for status in (FileVaultStatus.UNPROTECTED, FileVaultStatus.TRANSITIONING)
            ]
            candidates.extend(
                self._build_metadata_backup_plan(
                    request,
                    filevault_override=FileVaultCheck(
                        FileVaultStatus.UNKNOWN, filevault.target_volume, reason
                    ),
                    capacity_override=capacity,
                )
                for reason in FileVaultReason
            )
            return next(
                (candidate for candidate in candidates if candidate.fingerprint == expected_plan),
                None,
            )
        if isinstance(request, BeginMetadataRestore):
            filevault = current_plan.preflight.filevault
            capacity = current_plan.preflight.capacity
            if (
                filevault is None
                or filevault.status is not FileVaultStatus.PROTECTED
                or capacity is None
            ):
                return None
            candidates = [
                self._build_metadata_restore_plan(
                    request,
                    filevault_override=FileVaultCheck(status, filevault.target_volume),
                    capacity_override=capacity,
                )
                for status in (FileVaultStatus.UNPROTECTED, FileVaultStatus.TRANSITIONING)
            ]
            candidates.extend(
                self._build_metadata_restore_plan(
                    request,
                    filevault_override=FileVaultCheck(
                        FileVaultStatus.UNKNOWN, filevault.target_volume, reason
                    ),
                    capacity_override=capacity,
                )
                for reason in FileVaultReason
            )
            return next(
                (candidate for candidate in candidates if candidate.fingerprint == expected_plan),
                None,
            )
        if not isinstance(request, ImportHealthExport):
            return None
        if not isinstance(current_plan.details, ImportHealthExportPlan):
            return None
        filevault = current_plan.preflight.filevault
        capacity = current_plan.preflight.capacity
        if (
            filevault is None
            or filevault.status is not FileVaultStatus.PROTECTED
            or capacity is None
        ):
            return None
        workspace = self.load_workspace_status()
        candidates = [
            self._compose_import_plan(
                request,
                workspace,
                FileVaultCheck(status, filevault.target_volume),
                current_plan.details.package_size,
                current_plan.details.package_hash,
                HealthExportEstimate(
                    current_plan.details.input_bytes, current_plan.details.record_count
                ),
                capacity,
                current_plan.details.restore_state_hash,
                unavailable=False,
            )
            for status in (FileVaultStatus.UNPROTECTED, FileVaultStatus.TRANSITIONING)
        ]
        candidates.extend(
            self._compose_import_plan(
                request,
                workspace,
                FileVaultCheck(FileVaultStatus.UNKNOWN, filevault.target_volume, reason),
                current_plan.details.package_size,
                current_plan.details.package_hash,
                HealthExportEstimate(
                    current_plan.details.input_bytes, current_plan.details.record_count
                ),
                capacity,
                current_plan.details.restore_state_hash,
                unavailable=False,
            )
            for reason in FileVaultReason
        )
        return next(
            (candidate for candidate in candidates if candidate.fingerprint == expected_plan),
            None,
        )

    def execute_write(
        self,
        request: WriteRequest,
        *,
        expected_plan: PlanFingerprint,
    ) -> WriteReceipt:
        current_plan = self.preview_write(request)
        authorization_plan = self._authorization_plan(request, current_plan, expected_plan)
        if authorization_plan is None:
            if current_plan.approval.status is WriteApprovalStatus.BLOCKED:
                return self._not_started(
                    current_plan,
                    WriteNotStartedStatus.BLOCKED,
                    current_plan.diagnostics,
                    expected_plan,
                )
            return self._not_started(
                current_plan,
                WriteNotStartedStatus.PLAN_CHANGED,
                ("plan_changed",),
                expected_plan,
            )
        if current_plan.approval.status is WriteApprovalStatus.BLOCKED:
            return self._not_started(
                current_plan,
                WriteNotStartedStatus.BLOCKED,
                current_plan.diagnostics,
                expected_plan,
            )
        if isinstance(request, BeginMetadataRestore):
            return self._execute_metadata_restore(request, authorization_plan, expected_plan)
        if isinstance(request, AbortMetadataRestore):
            return self._execute_metadata_restore_abort(authorization_plan, expected_plan)
        if isinstance(request, MigrateStore):
            return self._execute_store_migration(authorization_plan, expected_plan)
        if isinstance(request, RollbackMigration):
            return self._execute_migration_rollback(authorization_plan, expected_plan)
        if isinstance(request, CreateMetadataBackup):
            return self._execute_metadata_backup(request, authorization_plan, expected_plan)
        if isinstance(request, CreatePlausibilityRuleVersion):
            return self._execute_plausibility_rule_write(request, authorization_plan, expected_plan)
        if isinstance(request, CreateActivityDerivationVersion):
            return self._execute_activity_derivation_write(
                request, authorization_plan, expected_plan
            )
        if isinstance(request, ReviseContextCoverageStart):
            return self._execute_context_coverage_start_write(
                request, authorization_plan, expected_plan
            )
        if isinstance(request, ReviseMedicationRegime):
            return self._execute_medication_regime_write(request, authorization_plan, expected_plan)
        if isinstance(request, ReviseMedicationDeviation):
            return self._execute_medication_deviation_write(
                request, authorization_plan, expected_plan
            )
        if isinstance(request, ReviseAsNeededIntake):
            return self._execute_as_needed_intake_write(request, authorization_plan, expected_plan)
        if isinstance(request, ReviseIntakeReasonCategory):
            return self._execute_intake_reason_category_write(
                request, authorization_plan, expected_plan
            )
        if isinstance(
            request,
            (
                ReviseIllnessCategory,
                ReviseIllnessPeriod,
                ReviseDailyStress,
                ReviseCustomContextLabel,
                ReviseCustomContextPeriod,
            ),
        ):
            return self._execute_illness_write(request, authorization_plan, expected_plan)
        if isinstance(request, RunHistoricalReview):
            return self._execute_historical_review_write(request, authorization_plan, expected_plan)
        if isinstance(request, RunRestingHeartRateAnalysis):
            return self._execute_resting_heart_rate_analysis(
                request, authorization_plan, expected_plan
            )
        if not isinstance(request, ImportHealthExport):
            return self._execute_data_review_write(request, authorization_plan, expected_plan)
        if not isinstance(authorization_plan.details, ImportHealthExportPlan):
            return self._not_started(
                authorization_plan,
                WriteNotStartedStatus.PLAN_CHANGED,
                ("plan_changed",),
                expected_plan,
            )
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                authorization_plan,
                WriteNotStartedStatus.STORE_BUSY,
                ("store_busy",),
                expected_plan,
            )
        except StoreError as error:
            if self._store is not None:
                final_filevault = (
                    probe_filevault(self._config.active_store)
                    if authorization_plan.preflight.filevault is not None
                    else None
                )
                if (
                    final_filevault is not None
                    and final_filevault.reason is FileVaultReason.VOLUME_LOCKED
                ):
                    return self._not_started_with_preflight(
                        authorization_plan,
                        WriteNotStartedStatus.BLOCKED,
                        ("target_locked",),
                        expected_plan,
                        filevault=final_filevault,
                    )
                final_capacity = self._store.preflight_full_snapshot_import(
                    authorization_plan.details.input_bytes,
                    authorization_plan.details.record_count,
                )
                if final_capacity.status is not CapacityStatus.READY:
                    diagnostic = "capacity_" + (
                        final_capacity.reason.value
                        if final_capacity.reason is not None
                        else final_capacity.status.value
                    )
                    return self._not_started_with_preflight(
                        authorization_plan,
                        WriteNotStartedStatus.BLOCKED,
                        (diagnostic,),
                        expected_plan,
                        filevault=final_filevault,
                        capacity=final_capacity,
                    )
            raise HealthLabError("Health-Export konnte nicht importiert werden.") from error
        try:
            expected_filevault = authorization_plan.preflight.filevault
            final_filevault = (
                probe_filevault(self._config.active_store)
                if expected_filevault is not None
                else None
            )
            if (
                expected_filevault is not None
                and final_filevault is not None
                and not self._filevault_allows_execution(expected_filevault, final_filevault)
            ):
                if final_filevault.reason is FileVaultReason.VOLUME_LOCKED:
                    return self._not_started_with_preflight(
                        authorization_plan,
                        WriteNotStartedStatus.BLOCKED,
                        ("target_locked",),
                        expected_plan,
                        filevault=final_filevault,
                    )
                return self._not_started_with_preflight(
                    authorization_plan,
                    WriteNotStartedStatus.PLAN_CHANGED,
                    ("plan_changed",),
                    expected_plan,
                    filevault=final_filevault,
                )
            if authorization_plan.details.restore_state_hash is not None:
                restore = inspect_restore_health_export(
                    request.package_path,
                    store=writer,
                    target_root=self._config.active_store,
                    max_package_bytes=self._config.max_import_package_bytes,
                    max_entries=self._config.max_import_entries,
                    max_entry_bytes=self._config.max_import_entry_bytes,
                    max_uncompressed_bytes=self._config.max_import_uncompressed_bytes,
                    max_compression_ratio=self._config.max_import_compression_ratio,
                )
                if restore.sources.state_hash != authorization_plan.details.restore_state_hash:
                    return self._not_started_with_preflight(
                        authorization_plan,
                        WriteNotStartedStatus.PLAN_CHANGED,
                        ("plan_changed",),
                        expected_plan,
                        filevault=final_filevault,
                        capacity=restore.capacity,
                    )
                final_capacity = restore.capacity
            else:
                final_capacity = writer.preflight_full_snapshot_import(
                    authorization_plan.details.input_bytes,
                    authorization_plan.details.record_count,
                )
            if final_capacity.status is not CapacityStatus.READY:
                diagnostic = "capacity_" + (
                    final_capacity.reason.value
                    if final_capacity.reason is not None
                    else final_capacity.status.value
                )
                return self._not_started_with_preflight(
                    authorization_plan,
                    WriteNotStartedStatus.BLOCKED,
                    (diagnostic,),
                    expected_plan,
                    filevault=final_filevault,
                    capacity=final_capacity,
                )
            result = import_health_export(
                request.package_path,
                store=writer,
                max_package_bytes=self._config.max_import_package_bytes,
                max_entries=self._config.max_import_entries,
                max_entry_bytes=self._config.max_import_entry_bytes,
                max_uncompressed_bytes=self._config.max_import_uncompressed_bytes,
                max_compression_ratio=self._config.max_import_compression_ratio,
                target_root=self._config.active_store,
            )
        except HealthImportError as error:
            raise HealthLabError("Health-Export konnte nicht importiert werden.") from error
        finally:
            writer.close()
        final_preflight = WritePreflight(
            authorization_plan.approval,
            authorization_plan.confirmations,
            final_filevault or current_plan.preflight.filevault,
            result.diagnostics,
            final_capacity,
        )
        import_result = ImportReceipt(
            operation_id=result.operation_id,
            import_id=result.import_id,
            status=ImportStatus(result.status),
            package_hash=result.package_hash,
            snapshot_ref=result.snapshot_id,
            record_count=result.record_count,
            anomaly_count=result.anomaly_count,
            package_record_count=result.package_record_count,
            logical_measurement_count=result.logical_measurement_count,
            measurement_version_count=result.measurement_version_count,
            source_occurrence_count=result.source_occurrence_count,
            diagnostics=result.diagnostics,
        )
        return WriteReceipt(
            operation_id=result.operation_id,
            plan_fingerprint=expected_plan,
            result=import_result,
            final_preflight=final_preflight,
            diagnostics=result.diagnostics,
        )

    def _execute_store_migration(
        self,
        plan: WritePlan,
        expected_plan: PlanFingerprint,
    ) -> WriteReceipt:
        if not isinstance(plan.details, StoreMigrationPlan) or plan.details.source_version is None:
            return self._not_started(
                plan, WriteNotStartedStatus.BLOCKED, plan.diagnostics, expected_plan
            )
        operation_id = OperationId(uuid4().hex)
        if not plan.details.steps:
            result = StoreMigrationReceipt(
                operation_id,
                MigrationStatus.NO_OP,
                plan.details.source_version,
                plan.details.target_version,
                (),
                None,
            )
            return WriteReceipt(operation_id, expected_plan, result, plan.preflight)
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        try:
            final_filevault = (
                probe_filevault(self._config.active_store)
                if plan.preflight.filevault is not None
                else None
            )
            expected_filevault = plan.preflight.filevault
            if (
                expected_filevault is not None
                and final_filevault is not None
                and not self._filevault_allows_execution(expected_filevault, final_filevault)
            ):
                return self._not_started_with_preflight(
                    plan,
                    WriteNotStartedStatus.PLAN_CHANGED,
                    ("plan_changed",),
                    expected_plan,
                    filevault=final_filevault,
                )
            final_capacity = writer.preflight_store_migration()
            if final_capacity.status is not CapacityStatus.READY:
                diagnostic = "capacity_" + (
                    final_capacity.reason.value
                    if final_capacity.reason is not None
                    else final_capacity.status.value
                )
                return self._not_started_with_preflight(
                    plan,
                    WriteNotStartedStatus.BLOCKED,
                    (diagnostic,),
                    expected_plan,
                    filevault=final_filevault,
                    capacity=final_capacity,
                )
            assert plan.details.backup_file is not None
            writer.migrate_store_schema(plan.details.steps, plan.details.backup_file, operation_id)
        except StoreError as error:
            diagnostic = str(error)
            if diagnostic not in {"migration_backup_failed", "migration_validation_failed"}:
                diagnostic = "migration_failed"
            return self._not_started(
                plan,
                WriteNotStartedStatus.BLOCKED,
                (diagnostic,),
                expected_plan,
            )
        finally:
            writer.close()
        result = StoreMigrationReceipt(
            operation_id,
            MigrationStatus.COMPLETED,
            plan.details.source_version,
            plan.details.target_version,
            plan.details.steps,
            plan.details.backup_file,
        )
        return WriteReceipt(
            operation_id,
            expected_plan,
            result,
            WritePreflight(
                plan.approval,
                plan.confirmations,
                final_filevault,
                (),
                final_capacity,
            ),
        )

    def _execute_migration_rollback(
        self,
        plan: WritePlan,
        expected_plan: PlanFingerprint,
    ) -> WriteReceipt:
        details = plan.details
        if (
            not isinstance(details, RollbackMigrationPlan)
            or details.migration_operation_id is None
            or details.source_version is None
            or details.target_version is None
            or details.backup_file is None
        ):
            return self._not_started(
                plan, WriteNotStartedStatus.BLOCKED, plan.diagnostics, expected_plan
            )
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        operation_id = OperationId(uuid4().hex)
        try:
            facts = writer.load_migration_rollback_facts()
            if (
                facts is None
                or plan_migration_rollback(facts)
                or facts.migration_operation_id != details.migration_operation_id
                or facts.backup_file != details.backup_file
                or facts.backup_sha256 != details.backup_sha256
                or facts.post_migration_version != details.source_version
                or facts.pre_migration_version != details.target_version
                or facts.migrated_snapshot_id != details.current_snapshot_ref
                or facts.previous_snapshot_id != details.restored_snapshot_ref
            ):
                return self._not_started(
                    plan,
                    WriteNotStartedStatus.PLAN_CHANGED,
                    ("plan_changed",),
                    expected_plan,
                )
            writer.rollback_store_migration(facts, operation_id)
        except StoreError as error:
            status = (
                WriteNotStartedStatus.PLAN_CHANGED
                if str(error) == "migration_rollback_changed"
                else WriteNotStartedStatus.BLOCKED
            )
            diagnostic = (
                "plan_changed" if status is WriteNotStartedStatus.PLAN_CHANGED else str(error)
            )
            return self._not_started(plan, status, (diagnostic,), expected_plan)
        finally:
            writer.close()
        result = RollbackMigrationReceipt(
            operation_id,
            details.migration_operation_id,
            MigrationStatus.COMPLETED,
            details.source_version,
            details.target_version,
            details.backup_file,
            details.restored_snapshot_ref,
            details.current_snapshot_ref,
        )
        return WriteReceipt(operation_id, expected_plan, result, plan.preflight)

    def _execute_metadata_backup(
        self,
        request: CreateMetadataBackup,
        plan: WritePlan,
        expected_plan: PlanFingerprint,
    ) -> WriteReceipt:
        if not isinstance(plan.details, MetadataBackupPlan):
            return self._not_started(
                plan,
                WriteNotStartedStatus.PLAN_CHANGED,
                ("plan_changed",),
                expected_plan,
            )
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        try:
            final_filevault = probe_filevault(request.target_path.parent)
            expected_filevault = plan.preflight.filevault
            if expected_filevault is None or not self._filevault_allows_execution(
                expected_filevault, final_filevault
            ):
                return self._not_started_with_preflight(
                    plan,
                    WriteNotStartedStatus.PLAN_CHANGED,
                    ("plan_changed",),
                    expected_plan,
                    filevault=final_filevault,
                )
            final_capacity = preflight_metadata_backup(writer, request.target_path)
            if final_capacity.status is not CapacityStatus.READY:
                diagnostic = "capacity_" + (
                    final_capacity.reason.value
                    if final_capacity.reason is not None
                    else final_capacity.status.value
                )
                return self._not_started_with_preflight(
                    plan,
                    WriteNotStartedStatus.BLOCKED,
                    (diagnostic,),
                    expected_plan,
                    filevault=final_filevault,
                    capacity=final_capacity,
                )
            current = describe_metadata_backup(writer, request.target_path)
            if (
                current.backup_id != plan.details.backup_id
                or current.canonical_content_sha256 != plan.details.canonical_content_sha256
                or current.audit_max_position != plan.details.audit_max_position
            ):
                return self._not_started_with_preflight(
                    plan,
                    WriteNotStartedStatus.PLAN_CHANGED,
                    ("plan_changed",),
                    expected_plan,
                    filevault=final_filevault,
                    capacity=final_capacity,
                )
            result = create_metadata_backup(writer, request.target_path)
        except StoreError:
            return self._not_started(
                plan,
                WriteNotStartedStatus.BLOCKED,
                ("backup_integrity_conflict",),
                expected_plan,
            )
        except OSError as error:
            raise HealthLabError("Metadatensicherung konnte nicht geschrieben werden.") from error
        finally:
            writer.close()
        operation_id = OperationId(uuid4().hex)
        backup_receipt = MetadataBackupReceipt(
            operation_id,
            result.backup_id,
            result.canonical_content_sha256,
            result.audit_max_position,
            result.created_at_utc,
            result.target_file,
            result.status,
        )
        return WriteReceipt(
            operation_id,
            expected_plan,
            backup_receipt,
            WritePreflight(
                plan.approval,
                plan.confirmations,
                final_filevault,
                (),
                final_capacity,
            ),
        )

    def _execute_metadata_restore(
        self,
        request: BeginMetadataRestore,
        plan: WritePlan,
        expected_plan: PlanFingerprint,
    ) -> WriteReceipt:
        details = plan.details
        if (
            not isinstance(details, MetadataRestorePlan)
            or details.restore_id is None
            or details.backup_id is None
            or details.source_store_id is None
            or details.original_backup_sha256 is None
            or details.audit_max_position is None
            or details.source_schema_version is None
        ):
            return self._not_started(
                plan,
                WriteNotStartedStatus.PLAN_CHANGED,
                ("plan_changed",),
                expected_plan,
            )
        operation_id = OperationId(uuid4().hex)
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        try:
            final_filevault = probe_filevault(self._config.active_store)
            expected_filevault = plan.preflight.filevault
            if expected_filevault is None or not self._filevault_allows_execution(
                expected_filevault, final_filevault
            ):
                return self._not_started_with_preflight(
                    plan,
                    WriteNotStartedStatus.PLAN_CHANGED,
                    ("plan_changed",),
                    expected_plan,
                    filevault=final_filevault,
                )
            final_capacity = preflight_metadata_restore_start(
                request.backup_path, self._config.active_store
            )
            if final_capacity.status is not CapacityStatus.READY:
                diagnostic = "capacity_" + (
                    final_capacity.reason.value
                    if final_capacity.reason is not None
                    else final_capacity.status.value
                )
                return self._not_started_with_preflight(
                    plan,
                    WriteNotStartedStatus.BLOCKED,
                    (diagnostic,),
                    expected_plan,
                    filevault=final_filevault,
                    capacity=final_capacity,
                )
            current = inspect_metadata_restore(
                writer, request.backup_path, self._config.active_store
            )
            expected = MetadataRestoreInspection(
                details.restore_id,
                details.backup_id,
                details.source_store_id,
                details.original_backup_sha256,
                current.canonical_content_sha256,
                details.working_copy_sha256,
                details.audit_max_position,
                details.source_schema_version,
                details.target_schema_version,
                details.migration_steps,
                current.status,
            )
            result = begin_metadata_restore(
                writer,
                request.backup_path,
                self._config.active_store,
                expected,
                str(operation_id),
            )
        except StoreError as error:
            return self._not_started(
                plan, WriteNotStartedStatus.BLOCKED, (str(error),), expected_plan
            )
        finally:
            writer.close()
        assert result.working_copy_sha256 is not None
        restore_receipt = MetadataRestoreReceipt(
            operation_id,
            result.restore_id,
            result.backup_id,
            result.original_backup_sha256,
            result.working_copy_sha256,
            result.status,
        )
        return WriteReceipt(
            operation_id,
            expected_plan,
            restore_receipt,
            WritePreflight(
                plan.approval,
                plan.confirmations,
                final_filevault,
                (),
                final_capacity,
            ),
        )

    def _execute_metadata_restore_abort(
        self, plan: WritePlan, expected_plan: PlanFingerprint
    ) -> WriteReceipt:
        if not isinstance(plan.details, AbortMetadataRestorePlan):
            return self._not_started(
                plan,
                WriteNotStartedStatus.PLAN_CHANGED,
                ("plan_changed",),
                expected_plan,
            )
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        try:
            inspection = validate_metadata_restore_abort(writer, self._config.active_store)
            if (
                plan.details.restore_id != inspection.restore_id
                or plan.details.backup_id != inspection.backup_id
            ):
                return self._not_started(
                    plan,
                    WriteNotStartedStatus.PLAN_CHANGED,
                    ("plan_changed",),
                    expected_plan,
                )
            discarded = stage_metadata_restore_abort(
                self._config.active_store, inspection.restore_id
            )
        except StoreError as error:
            return self._not_started(
                plan, WriteNotStartedStatus.BLOCKED, (str(error),), expected_plan
            )
        finally:
            writer.close()
        try:
            shutil.rmtree(discarded)
        except OSError as error:
            raise HealthLabError("Wiederherstellung konnte nicht verworfen werden.") from error
        if self._overview_reader is not None:
            self._overview_reader.close()
            self._overview_reader = None
            self._store = None
        operation_id = OperationId(uuid4().hex)
        assert inspection.working_copy_sha256 is not None
        result = MetadataRestoreReceipt(
            operation_id,
            inspection.restore_id,
            inspection.backup_id,
            inspection.original_backup_sha256,
            inspection.working_copy_sha256,
            MetadataRestoreStatus.ABORTED,
        )
        return WriteReceipt(operation_id, expected_plan, result, plan.preflight)

    def _execute_activity_derivation_write(
        self,
        request: CreateActivityDerivationVersion,
        plan: WritePlan,
        expected_plan: PlanFingerprint,
    ) -> WriteReceipt:
        if not isinstance(plan.details, PlausibilityRuleVersionPlan):
            return self._not_started(
                plan,
                WriteNotStartedStatus.PLAN_CHANGED,
                ("plan_changed",),
                expected_plan,
            )
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        try:
            operation_id = OperationId(uuid4().hex)
            snapshot_ref = writer.create_activity_derivation_version(
                operation_id=operation_id,
                version_id=plan.details.proposed_version_id,
                coverage_gap_minutes=request.coverage_gap_minutes,
                source_classifier_version=_ACTIVITY_SOURCE_CLASSIFIER_VERSION,
                expected_snapshot_id=plan.details.active_snapshot_ref,
            )
        except StoreError as error:
            raise HealthLabError("Aktivitätsableitung konnte nicht gespeichert werden.") from error
        finally:
            writer.close()
        return WriteReceipt(
            operation_id,
            expected_plan,
            PlausibilityRuleVersionReceipt(
                operation_id, plan.details.proposed_version_id, snapshot_ref
            ),
            plan.preflight,
        )

    def _execute_context_coverage_start_write(
        self,
        request: ReviseContextCoverageStart,
        plan: WritePlan,
        expected_plan: PlanFingerprint,
    ) -> WriteReceipt:
        if not isinstance(plan.details, ManualContextRevisionPlan):
            return self._not_started(
                plan, WriteNotStartedStatus.PLAN_CHANGED, ("plan_changed",), expected_plan
            )
        if plan.approval.status is WriteApprovalStatus.NO_CHANGE:
            return WriteReceipt(
                OperationId(uuid4().hex), expected_plan, WriteNoChange(), plan.preflight
            )
        if plan.details.base_snapshot_ref is None:
            return self._not_started(
                plan, WriteNotStartedStatus.BLOCKED, plan.diagnostics, expected_plan
            )
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        try:
            operation_id = OperationId(uuid4().hex)
            publication = writer.publish_context_coverage_start(
                publication=ContextCoverageStartPublication(
                    operation_id,
                    plan.details.intent,
                    StoredContextLogicalId(str(plan.details.logical_id)),
                    (
                        None
                        if plan.details.expected_revision_id is None
                        else StoredContextRevisionId(str(plan.details.expected_revision_id))
                    ),
                    plan.details.start_date,
                    plan.details.withdrawal_reason,
                    plan.details.base_snapshot_ref,
                    plan.details.snapshot_as_of.date(),
                    plan.details.context_timezone,
                )
            )
        except StoreError:
            return self._not_started(
                plan, WriteNotStartedStatus.PLAN_CHANGED, ("plan_changed",), expected_plan
            )
        finally:
            writer.close()
        return WriteReceipt(
            operation_id,
            expected_plan,
            ManualContextRevisionReceipt(
                operation_id,
                ContextLogicalId(str(publication.logical_id)),
                ContextRevisionId(str(publication.revision_id)),
                publication.snapshot_id,
            ),
            plan.preflight,
        )

    def _execute_medication_regime_write(
        self, request: ReviseMedicationRegime, plan: WritePlan, expected_plan: PlanFingerprint
    ) -> WriteReceipt:
        if (
            not isinstance(plan.details, MedicationRegimePlan)
            or plan.details.base_snapshot_ref is None
        ):
            return self._not_started(
                plan, WriteNotStartedStatus.BLOCKED, plan.diagnostics, expected_plan
            )
        if plan.approval.status is WriteApprovalStatus.NO_CHANGE:
            return WriteReceipt(
                OperationId(uuid4().hex), expected_plan, WriteNoChange(), plan.preflight
            )
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        try:
            operation_id = OperationId(uuid4().hex)
            publication = writer.publish_medication_regime(
                MedicationRegimePublication(
                    operation_id,
                    plan.details.intent,
                    StoredMedicationLogicalId(str(plan.details.logical_id)),
                    None
                    if plan.details.expected_revision_id is None
                    else StoredMedicationRevisionId(str(plan.details.expected_revision_id)),
                    plan.details.starts_at,
                    plan.details.timezone,
                    tuple(
                        (
                            dose.medication_name,
                            str(dose.amount),
                            dose.unit,
                            dose.local_time,
                            tuple(sorted(day.value for day in dose.weekdays)),
                        )
                        for dose in plan.details.scheduled_doses
                    ),
                    tuple(
                        (
                            item.medication_name,
                            str(item.amount),
                            item.unit,
                            tuple(
                                str(category_id)
                                for category_id in item.preferred_reason_category_ids
                            ),
                            str(item.entry_id),
                        )
                        for item in plan.details.as_needed_medications
                    ),
                    plan.details.base_snapshot_ref,
                    plan.details.medication_as_of,
                )
            )
        except StoreError:
            return self._not_started(
                plan, WriteNotStartedStatus.PLAN_CHANGED, ("plan_changed",), expected_plan
            )
        finally:
            writer.close()
        return WriteReceipt(
            operation_id,
            expected_plan,
            MedicationRegimeReceipt(
                operation_id,
                MedicationLogicalId(str(publication.logical_id)),
                MedicationRevisionId(str(publication.revision_id)),
                publication.snapshot_id,
            ),
            plan.preflight,
        )

    def _execute_medication_deviation_write(
        self, request: ReviseMedicationDeviation, plan: WritePlan, expected_plan: PlanFingerprint
    ) -> WriteReceipt:
        if (
            not isinstance(plan.details, MedicationDeviationPlan)
            or plan.details.base_snapshot_ref is None
        ):
            return self._not_started(
                plan, WriteNotStartedStatus.BLOCKED, plan.diagnostics, expected_plan
            )
        if plan.approval.status is WriteApprovalStatus.NO_CHANGE:
            return WriteReceipt(
                OperationId(uuid4().hex), expected_plan, WriteNoChange(), plan.preflight
            )
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        try:
            operation_id = OperationId(uuid4().hex)
            publication = writer.publish_medication_deviation(
                MedicationDeviationPublication(
                    operation_id,
                    plan.details.intent,
                    StoredMedicationLogicalId(str(plan.details.logical_id)),
                    None
                    if plan.details.expected_revision_id is None
                    else StoredMedicationRevisionId(str(plan.details.expected_revision_id)),
                    StoredMedicationLogicalId(str(plan.details.regime_logical_id)),
                    plan.details.scheduled_at,
                    tuple(
                        (item.taken_at, str(item.amount)) for item in plan.details.actual_intakes
                    ),
                    plan.details.withdrawal_reason,
                    plan.details.base_snapshot_ref,
                    plan.details.medication_as_of,
                )
            )
        except StoreError:
            return self._not_started(
                plan, WriteNotStartedStatus.PLAN_CHANGED, ("plan_changed",), expected_plan
            )
        finally:
            writer.close()
        return WriteReceipt(
            operation_id,
            expected_plan,
            MedicationDeviationReceipt(
                operation_id,
                MedicationLogicalId(str(publication.logical_id)),
                MedicationRevisionId(str(publication.revision_id)),
                publication.snapshot_id,
            ),
            plan.preflight,
        )

    def _execute_intake_reason_category_write(
        self, request: ReviseIntakeReasonCategory, plan: WritePlan, expected_plan: PlanFingerprint
    ) -> WriteReceipt:
        if (
            not isinstance(plan.details, IntakeReasonCategoryPlan)
            or plan.details.base_snapshot_ref is None
        ):
            return self._not_started(
                plan, WriteNotStartedStatus.BLOCKED, plan.diagnostics, expected_plan
            )
        if plan.approval.status is WriteApprovalStatus.NO_CHANGE:
            return WriteReceipt(
                OperationId(uuid4().hex), expected_plan, WriteNoChange(), plan.preflight
            )
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        try:
            operation_id = OperationId(uuid4().hex)
            publication = writer.publish_intake_reason_category(
                IntakeReasonCategoryPublication(
                    operation_id,
                    plan.details.intent,
                    StoredMedicationLogicalId(str(plan.details.logical_id)),
                    None
                    if plan.details.expected_revision_id is None
                    else StoredMedicationRevisionId(str(plan.details.expected_revision_id)),
                    plan.details.name,
                    plan.details.withdrawal_reason,
                    plan.details.base_snapshot_ref,
                    plan.details.medication_as_of,
                )
            )
        except StoreError:
            return self._not_started(
                plan, WriteNotStartedStatus.PLAN_CHANGED, ("plan_changed",), expected_plan
            )
        finally:
            writer.close()
        return WriteReceipt(
            operation_id,
            expected_plan,
            IntakeReasonCategoryReceipt(
                operation_id,
                MedicationLogicalId(str(publication.logical_id)),
                MedicationRevisionId(str(publication.revision_id)),
                publication.snapshot_id,
            ),
            plan.preflight,
        )

    def _execute_as_needed_intake_write(
        self, request: ReviseAsNeededIntake, plan: WritePlan, expected_plan: PlanFingerprint
    ) -> WriteReceipt:
        if (
            not isinstance(plan.details, AsNeededIntakePlan)
            or plan.details.base_snapshot_ref is None
        ):
            return self._not_started(
                plan, WriteNotStartedStatus.BLOCKED, plan.diagnostics, expected_plan
            )
        if plan.approval.status is WriteApprovalStatus.NO_CHANGE:
            return WriteReceipt(
                OperationId(uuid4().hex), expected_plan, WriteNoChange(), plan.preflight
            )
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        try:
            operation_id = OperationId(uuid4().hex)
            publication = writer.publish_as_needed_intake(
                AsNeededIntakePublication(
                    operation_id,
                    plan.details.intent,
                    StoredMedicationLogicalId(str(plan.details.logical_id)),
                    None
                    if plan.details.expected_revision_id is None
                    else StoredMedicationRevisionId(str(plan.details.expected_revision_id)),
                    StoredMedicationLogicalId(str(plan.details.regime_logical_id)),
                    str(plan.details.entry_id),
                    plan.details.taken_at,
                    str(plan.details.amount),
                    None
                    if plan.details.reason_category_logical_id is None
                    else StoredMedicationLogicalId(str(plan.details.reason_category_logical_id)),
                    plan.details.withdrawal_reason,
                    plan.details.base_snapshot_ref,
                    plan.details.medication_as_of,
                )
            )
        except StoreError:
            return self._not_started(
                plan, WriteNotStartedStatus.PLAN_CHANGED, ("plan_changed",), expected_plan
            )
        finally:
            writer.close()
        return WriteReceipt(
            operation_id,
            expected_plan,
            AsNeededIntakeReceipt(
                operation_id,
                MedicationLogicalId(str(publication.logical_id)),
                MedicationRevisionId(str(publication.revision_id)),
                publication.snapshot_id,
            ),
            plan.preflight,
        )

    def _execute_illness_write(
        self,
        request: ReviseIllnessCategory
        | ReviseIllnessPeriod
        | ReviseDailyStress
        | ReviseCustomContextLabel
        | ReviseCustomContextPeriod,
        plan: WritePlan,
        expected_plan: PlanFingerprint,
    ) -> WriteReceipt:
        if not isinstance(plan.details, IllnessRevisionPlan):
            return self._not_started(
                plan, WriteNotStartedStatus.PLAN_CHANGED, ("plan_changed",), expected_plan
            )
        if plan.approval.status is WriteApprovalStatus.NO_CHANGE:
            return WriteReceipt(
                OperationId(uuid4().hex), expected_plan, WriteNoChange(), plan.preflight
            )
        if plan.details.base_snapshot_ref is None:
            return self._not_started(
                plan, WriteNotStartedStatus.BLOCKED, plan.diagnostics, expected_plan
            )
        details = plan.details
        assert details.base_snapshot_ref is not None
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        try:
            operation_id = OperationId(uuid4().hex)
            publication = writer.publish_illness(
                publication=IllnessPublication(
                    operation_id,
                    details.intent,
                    details.object_kind,
                    StoredContextLogicalId(str(details.logical_id)),
                    None
                    if details.expected_revision_id is None
                    else StoredContextRevisionId(str(details.expected_revision_id)),
                    details.name,
                    None
                    if details.category_logical_id is None
                    else StoredContextLogicalId(str(details.category_logical_id)),
                    details.start_date,
                    details.end_date,
                    None if details.severity is None else details.severity.value,
                    None if details.stress_level is None else details.stress_level.value,
                    details.note,
                    details.withdrawal_reason,
                    details.base_snapshot_ref,
                    details.snapshot_as_of.date(),
                    details.context_timezone,
                )
            )
        except StoreError:
            return self._not_started(
                plan, WriteNotStartedStatus.PLAN_CHANGED, ("plan_changed",), expected_plan
            )
        finally:
            writer.close()
        return WriteReceipt(
            operation_id,
            expected_plan,
            ManualContextRevisionReceipt(
                operation_id,
                ContextLogicalId(str(publication.logical_id)),
                ContextRevisionId(str(publication.revision_id)),
                publication.snapshot_id,
            ),
            plan.preflight,
        )

    def _execute_plausibility_rule_write(
        self,
        request: CreatePlausibilityRuleVersion,
        plan: WritePlan,
        expected_plan: PlanFingerprint,
    ) -> WriteReceipt:
        if not isinstance(plan.details, PlausibilityRuleVersionPlan):
            return self._not_started(
                plan,
                WriteNotStartedStatus.PLAN_CHANGED,
                ("plan_changed",),
                expected_plan,
            )
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        try:
            operation_id = OperationId(uuid4().hex)
            record, snapshot_ref = create_plausibility_rule_version(
                writer,
                operation_id=operation_id,
                version_id=plan.details.proposed_version_id,
                data_type=request.data_type.value,
                unit=request.specification.unit.value,
                fixed_lower_bound=request.specification.fixed_lower_bound,
                fixed_upper_bound=request.specification.fixed_upper_bound,
                personal_range_enabled=request.specification.personal_range_enabled,
                effective_from=request.effective_from,
                recommendation_id=request.recommendation_id,
            )
        except StoreError as error:
            raise HealthLabError("Plausibilitätsregel konnte nicht gespeichert werden.") from error
        finally:
            writer.close()
        result = PlausibilityRuleVersionReceipt(
            operation_id,
            record.version_id,
            snapshot_ref,
        )
        return WriteReceipt(operation_id, expected_plan, result, plan.preflight)

    def _execute_historical_review_write(
        self,
        request: RunHistoricalReview,
        plan: WritePlan,
        expected_plan: PlanFingerprint,
    ) -> WriteReceipt:
        if (
            not isinstance(plan.details, HistoricalReviewPlan)
            or plan.details.base_snapshot_ref is None
        ):
            return self._not_started(
                plan,
                WriteNotStartedStatus.PLAN_CHANGED,
                ("plan_changed",),
                expected_plan,
            )
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        try:
            if writer.load_active_snapshot_id() != plan.details.base_snapshot_ref:
                return self._not_started(
                    plan,
                    WriteNotStartedStatus.PLAN_CHANGED,
                    ("plan_changed",),
                    expected_plan,
                )
            result = run_historical_review(
                writer,
                HistoricalReviewRequest(
                    operation_id=OperationId(uuid4().hex),
                    data_type=request.data_type.value,
                    start_date=request.start_date,
                    end_date=request.end_date,
                    rule_version_id=plan.details.rule_version_id,
                    base_snapshot_id=plan.details.base_snapshot_ref,
                ),
            )
        except (DataQualityError, StoreError) as error:
            raise HealthLabError(
                "Historische Datenprüfung konnte nicht ausgeführt werden."
            ) from error
        finally:
            writer.close()
        receipt = HistoricalReviewReceipt(
            result.operation_id,
            DataReviewCycleId(str(result.cycle_id)),
            result.snapshot_id,
            result.open_case_count,
        )
        return WriteReceipt(result.operation_id, expected_plan, receipt, plan.preflight)

    def _execute_data_review_write(
        self,
        request: ResolveDataReviewCase | ConfirmDataReviewBatch | RevokeDataReviewDecision,
        plan: WritePlan,
        expected_plan: PlanFingerprint,
    ) -> WriteReceipt:
        try:
            writer = LocalStore.open_writer(root=self._config.active_store, mode=self._config.mode)
        except StoreBusyError:
            return self._not_started(
                plan, WriteNotStartedStatus.STORE_BUSY, ("store_busy",), expected_plan
            )
        operation_id = OperationId(uuid4().hex)
        result: PublishDecisionResult | PublishBatchDecisionResult
        try:
            if isinstance(request, ResolveDataReviewCase):
                review = self.load_data_review(DataReviewSelection())
                case = (
                    None
                    if request.case_id is None
                    else next(
                        (item for item in review.cases if item.case_id == request.case_id),
                        None,
                    )
                )
                if case is None and request.case_id is not None:
                    return self._not_started(
                        plan,
                        WriteNotStartedStatus.PLAN_CHANGED,
                        ("plan_changed",),
                        expected_plan,
                    )
                resolution = request.resolution
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
                ]
                corrected_value: float | None = None
                canonical_unit: str | None = None
                corrected_distance_kilometers: float | None = None
                corrected_active_energy_kilocalories: float | None = None
                reason: str | None = None
                direct_logical_id: str | None = None
                if isinstance(resolution, DataConfirmation):
                    assert case is not None
                    if case.kind not in {
                        DataReviewCaseKind.PLAUSIBILITY,
                        DataReviewCaseKind.CONTINUED_OVERRIDE,
                    }:
                        return self._not_started(
                            plan,
                            WriteNotStartedStatus.BLOCKED,
                            ("resolution_not_allowed",),
                            expected_plan,
                        )
                    action = "confirm"
                    selected = None
                    note = resolution.note
                    if case.kind is DataReviewCaseKind.CONTINUED_OVERRIDE:
                        selected = (
                            None
                            if case.measurement_version_id is None
                            else str(case.measurement_version_id)
                        )
                        if not self.load_data_review_case(case.case_id).reasons:
                            return self._not_started(
                                plan,
                                WriteNotStartedStatus.BLOCKED,
                                ("resolution_not_allowed",),
                                expected_plan,
                            )
                elif isinstance(resolution, DataCorrection):
                    version = writer.load_measurement_version_fact(
                        resolution.measurement_version_id
                    )
                    if (
                        version is None
                        or (
                            case is not None
                            and case.measurement_version_id != resolution.measurement_version_id
                        )
                        or resolution.unit.value != version.canonical_unit
                    ):
                        return self._not_started(
                            plan,
                            WriteNotStartedStatus.BLOCKED,
                            ("resolution_not_allowed",),
                            expected_plan,
                        )
                    action = "correct"
                    selected = str(resolution.measurement_version_id)
                    corrected_value = resolution.corrected_value
                    canonical_unit = resolution.unit.value
                    direct_logical_id = version.logical_measurement_id
                    reason = resolution.reason
                    note = resolution.note
                elif isinstance(resolution, LocalMeasurementExclusion):
                    assert case is not None
                    if case.measurement_version_id != resolution.measurement_version_id:
                        return self._not_started(
                            plan,
                            WriteNotStartedStatus.BLOCKED,
                            ("resolution_not_allowed",),
                            expected_plan,
                        )
                    action = "exclude_local"
                    selected = str(resolution.measurement_version_id)
                    reason = resolution.reason
                    note = resolution.note
                elif isinstance(resolution, WorkoutCorrection):
                    assert case is not None
                    if (
                        case.kind
                        not in {
                            DataReviewCaseKind.WORKOUT_PLAUSIBILITY,
                            DataReviewCaseKind.WORKOUT_OVERLAP,
                        }
                        or resolution.workout_version_id
                        not in writer.load_workout_review_case_versions(str(case.case_id))
                    ):
                        return self._not_started(
                            plan,
                            WriteNotStartedStatus.BLOCKED,
                            ("resolution_not_allowed",),
                            expected_plan,
                        )
                    action = "correct_workout"
                    selected = str(resolution.workout_version_id)
                    corrected_value = resolution.effective_duration_minutes
                    corrected_distance_kilometers = resolution.distance_kilometers
                    corrected_active_energy_kilocalories = resolution.active_energy_kilocalories
                    direct_logical_id = writer.load_workout_logical_id(
                        resolution.workout_version_id
                    )
                    if direct_logical_id is None:
                        return self._not_started(
                            plan,
                            WriteNotStartedStatus.BLOCKED,
                            ("resolution_not_allowed",),
                            expected_plan,
                        )
                    reason = resolution.reason
                    note = resolution.note
                elif isinstance(resolution, LocalWorkoutExclusion):
                    assert case is not None
                    if (
                        case.kind
                        not in {
                            DataReviewCaseKind.WORKOUT_PLAUSIBILITY,
                            DataReviewCaseKind.WORKOUT_OVERLAP,
                        }
                        or resolution.workout_version_id
                        not in writer.load_workout_review_case_versions(str(case.case_id))
                    ):
                        return self._not_started(
                            plan,
                            WriteNotStartedStatus.BLOCKED,
                            ("resolution_not_allowed",),
                            expected_plan,
                        )
                    action = "exclude_workout_local"
                    selected = str(resolution.workout_version_id)
                    direct_logical_id = writer.load_workout_logical_id(
                        resolution.workout_version_id
                    )
                    if direct_logical_id is None:
                        return self._not_started(
                            plan,
                            WriteNotStartedStatus.BLOCKED,
                            ("resolution_not_allowed",),
                            expected_plan,
                        )
                    reason = resolution.reason
                    note = resolution.note
                elif isinstance(resolution, SourceValueAcceptance):
                    assert case is not None
                    if (
                        case.kind is not DataReviewCaseKind.CONTINUED_OVERRIDE
                        or case.measurement_version_id != resolution.measurement_version_id
                        or self.load_data_review_case(case.case_id).reasons
                    ):
                        return self._not_started(
                            plan,
                            WriteNotStartedStatus.BLOCKED,
                            ("resolution_not_allowed",),
                            expected_plan,
                        )
                    action = "accept_source"
                    selected = str(resolution.measurement_version_id)
                    note = resolution.note
                elif isinstance(resolution, SourceDeletionResolution):
                    assert case is not None
                    if case.kind is not DataReviewCaseKind.SUSPECTED_SOURCE_DELETION:
                        return self._not_started(
                            plan,
                            WriteNotStartedStatus.BLOCKED,
                            ("resolution_not_allowed",),
                            expected_plan,
                        )
                    action = resolution.verdict.value
                    selected = None
                    note = resolution.note
                else:
                    assert case is not None
                    if case.kind is not DataReviewCaseKind.SOURCE_CONFLICT:
                        return self._not_started(
                            plan,
                            WriteNotStartedStatus.BLOCKED,
                            ("resolution_not_allowed",),
                            expected_plan,
                        )
                    action = resolution.strategy.value
                    selected = (
                        None
                        if resolution.preferred_version_id is None
                        else str(resolution.preferred_version_id)
                    )
                    note = resolution.note
                result = writer.publish_data_review_resolution(
                    operation_id=operation_id,
                    snapshot_id=SnapshotId(uuid4().hex),
                    review_case_id=None if case is None else str(case.case_id),
                    logical_measurement_id=(
                        direct_logical_id
                        if case is None or action in {"correct_workout", "exclude_workout_local"}
                        else (
                            None
                            if case.logical_measurement_id is None
                            else str(case.logical_measurement_id)
                        )
                    ),
                    action=action,
                    selected_measurement_version_id=selected,
                    candidate_version_ids=(
                        ()
                        if case is None
                        else tuple(str(item) for item in case.candidate_version_ids)
                    ),
                    note=note,
                    reason=reason,
                    corrected_value=corrected_value,
                    canonical_unit=canonical_unit,
                    corrected_distance_kilometers=corrected_distance_kilometers,
                    corrected_active_energy_kilocalories=corrected_active_energy_kilocalories,
                    cycle_updates=(
                        ()
                        if case is None
                        else close_review_cycle_updates(writer, str(case.case_id))
                    ),
                )
            elif isinstance(request, ConfirmDataReviewBatch):
                if not isinstance(plan.details, DataReviewBatchPlan):
                    return self._not_started(
                        plan,
                        WriteNotStartedStatus.PLAN_CHANGED,
                        ("plan_changed",),
                        expected_plan,
                    )
                current_matches = tuple(
                    DataReviewBatchMatch(
                        DataReviewCaseId(case.review_case_id),
                        DataReviewCaseKind(case.kind),
                        case.measurement_version_id,
                        case.rule_version_id,
                        case.evidence_fingerprint,
                        detail.effective_value,
                        None
                        if detail.canonical_unit is None
                        else CanonicalUnit(detail.canonical_unit),
                    )
                    for case in sorted(
                        writer.load_open_data_review_cases(),
                        key=lambda item: item.review_case_id,
                    )
                    if (request.selection.kind is None or case.kind == request.selection.kind.value)
                    and case.kind in {"plausibility", "continued_override"}
                    for detail in (load_review_case_detail(writer, case.review_case_id),)
                    if case.kind == "plausibility" or detail.reasons
                )
                if current_matches != plan.details.matches:
                    return self._not_started(
                        plan,
                        WriteNotStartedStatus.PLAN_CHANGED,
                        ("plan_changed",),
                        expected_plan,
                    )
                result = writer.publish_data_review_batch(
                    operation_id=operation_id,
                    snapshot_id=SnapshotId(uuid4().hex),
                    selection_kind=(
                        None if request.selection.kind is None else request.selection.kind.value
                    ),
                    materialized_matches=json.dumps(
                        [_data_review_batch_match_payload(item) for item in plan.details.matches],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    case_ids=tuple(str(item.case_id) for item in plan.details.matches),
                    note=request.note,
                    cycle_updates=close_review_batch_cycle_updates(
                        writer,
                        tuple(str(item.case_id) for item in plan.details.matches),
                    ),
                )
            else:
                if isinstance(request.target, BatchDecisionTarget):
                    if not isinstance(plan.details, DataReviewBatchRevokePlan):
                        raise StoreError("Sammelwiderrufsplan fehlt.")
                    result = writer.revoke_data_review_batch(
                        operation_id=operation_id,
                        snapshot_id=SnapshotId(uuid4().hex),
                        batch_action_id=str(request.target.batch_action_id),
                        expected_decision_ids=tuple(
                            str(item) for item in plan.details.decision_ids
                        ),
                        reason=request.reason,
                        cycle_updates=reopen_batch_decision_cycle_updates(
                            writer,
                            tuple(str(item) for item in plan.details.decision_ids),
                        ),
                    )
                else:
                    decision_id = str(request.target.decision_id)
                    result = writer.revoke_data_review_decision(
                        operation_id=operation_id,
                        snapshot_id=SnapshotId(uuid4().hex),
                        decision_id=decision_id,
                        reason=request.reason,
                        cycle_updates=reopen_decision_cycle_updates(writer, decision_id),
                    )
        except StoreError:
            return self._not_started(
                plan,
                WriteNotStartedStatus.PLAN_CHANGED,
                ("plan_changed",),
                expected_plan,
            )
        finally:
            writer.close()
        receipt: WriteResult = (
            WriteBatchDecisionReceipt(
                result.operation_id,
                DataReviewBatchActionId(result.batch_action_id),
                tuple(DataReviewDecisionId(item) for item in result.decision_ids),
                result.snapshot_id,
            )
            if isinstance(result, PublishBatchDecisionResult)
            else WriteDecisionReceipt(
                operation_id=result.operation_id,
                decision_id=DataReviewDecisionId(result.decision_id),
                snapshot_ref=result.snapshot_id,
            )
        )
        return WriteReceipt(
            operation_id=result.operation_id,
            plan_fingerprint=expected_plan,
            result=receipt,
            final_preflight=plan.preflight,
        )

    @staticmethod
    def _not_started(
        plan: WritePlan,
        status: WriteNotStartedStatus,
        diagnostics: tuple[str, ...],
        plan_fingerprint: PlanFingerprint | None = None,
    ) -> WriteReceipt:
        return WriteReceipt(
            operation_id=OperationId(uuid4().hex),
            plan_fingerprint=plan_fingerprint or plan.fingerprint,
            result=WriteNotStarted(status, diagnostics),
            final_preflight=plan.preflight,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _not_started_with_preflight(
        plan: WritePlan,
        status: WriteNotStartedStatus,
        diagnostics: tuple[str, ...],
        plan_fingerprint: PlanFingerprint,
        *,
        filevault: FileVaultCheck | None = None,
        capacity: CapacityCheck | None = None,
    ) -> WriteReceipt:
        return WriteReceipt(
            operation_id=OperationId(uuid4().hex),
            plan_fingerprint=plan_fingerprint,
            result=WriteNotStarted(status, diagnostics),
            final_preflight=WritePreflight(
                (
                    WriteApproval(WriteApprovalStatus.BLOCKED)
                    if status is WriteNotStartedStatus.BLOCKED
                    else plan.approval
                ),
                () if status is WriteNotStartedStatus.BLOCKED else plan.confirmations,
                filevault or plan.preflight.filevault,
                diagnostics,
                capacity or plan.preflight.capacity,
            ),
            diagnostics=diagnostics,
        )

    def _execute_resting_heart_rate_analysis(
        self,
        request: RunRestingHeartRateAnalysis,
        plan: WritePlan,
        expected_plan: PlanFingerprint,
    ) -> WriteReceipt:
        if not isinstance(plan.details, RestingHeartRateAnalysisPlan):
            return self._not_started(
                plan,
                WriteNotStartedStatus.PLAN_CHANGED,
                ("plan_changed",),
                expected_plan,
            )
        try:
            result = execute_analysis(
                root=self._config.active_store,
                mode=self._config.mode,
                analysis_definition_id=request.analysis_definition_id,
                start_date=request.start_date,
                end_date=request.end_date,
                config_schema_version=request.schema_version,
                expected_snapshot_id=plan.details.base_snapshot_ref,
                open_review_case_ids=tuple(
                    str(case.case_id) for case in self.load_data_review(DataReviewSelection()).cases
                ),
            )
        except AnalysisInputChanged:
            return self._not_started(
                plan,
                WriteNotStartedStatus.PLAN_CHANGED,
                ("plan_changed",),
                expected_plan,
            )
        except ValueError as error:
            raise ConfigurationError(str(error)) from error
        except AnalysisError as error:
            raise HealthLabError(str(error)) from error
        if result.status == "store_busy":
            return self._not_started(
                plan,
                WriteNotStartedStatus.STORE_BUSY,
                ("store_busy",),
                expected_plan,
            )
        receipt = AnalysisReceipt(
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
        return WriteReceipt(
            result.operation_id,
            expected_plan,
            receipt,
            plan.preflight,
            result.diagnostics,
        )

    def load_overview(self, selection: OverviewSelection) -> Overview:
        self._require_ready()
        reader = self._require_open()
        return reader.load(selection)

    def load_import_details(self, import_id: ImportId) -> ImportDetails:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        try:
            stored = self._store.load_import_details(import_id)
        except StoreError as error:
            raise HealthLabError("Importdetails sind nicht verfügbar.") from error
        if stored is None:
            raise ConfigurationError("Import-ID ist unbekannt.")
        return ImportDetails(
            import_id,
            ImportCanonicalCounts(
                stored.package_record_count,
                stored.record_count,
                stored.logical_measurement_count,
                stored.measurement_version_count,
                stored.source_occurrence_count,
                stored.anomaly_count,
            ),
            tuple(
                ImportContentCount(
                    UnsupportedContentCategory(item.category),
                    item.external_identifier,
                    item.count,
                )
                for item in stored.unsupported_content
            ),
        )

    def load_weight_nutrition(self, selection: SnapshotDateSelection) -> WeightNutrition:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        try:
            snapshot_ref, stored_measurements = self._store.load_weight_nutrition_measurements(
                selection.snapshot_ref, selection.start_date, selection.end_date
            )
        except StoreError as error:
            raise HealthLabError(
                "Gewichts- und Ernährungsprojektion ist nicht verfügbar."
            ) from error

        def public_weight(item: StoredMeasurement) -> WeightMeasurement:
            return WeightMeasurement(
                logical_measurement_id=item.logical_measurement_id,
                measurement_version_id=item.measurement_version_id,
                value_kg=item.value,
                effective_value_kg=item.effective_value,
                disposition=item.disposition,
                is_selected=item.is_selected,
                source_start=item.source_start,
                source_end=item.source_end,
                source_updated_at=item.source_updated_at,
                measurement_local_day=item.measurement_local_day,
                source_name=item.source_name,
                source_version=item.source_version,
                device=item.device,
                original_value=item.original_value,
                original_unit=item.original_unit,
                review_case_ids=tuple(
                    DataReviewCaseId(str(value)) for value in item.review_case_ids
                ),
            )

        def public_nutrition_sample(
            item: StoredMeasurement,
        ) -> HealthKitNutritionSample:
            return HealthKitNutritionSample(
                logical_measurement_id=item.logical_measurement_id,
                measurement_version_id=item.measurement_version_id,
                data_type=item.data_type,
                unit=item.unit,
                value=item.value,
                effective_value=item.effective_value,
                disposition=item.disposition,
                is_selected=item.is_selected,
                source_start=item.source_start,
                source_end=item.source_end,
                source_updated_at=item.source_updated_at,
                measurement_local_day=item.measurement_local_day,
                source_name=item.source_name,
                source_version=item.source_version,
                device=item.device,
                original_value=item.original_value,
                original_unit=item.original_unit,
                review_case_ids=tuple(
                    DataReviewCaseId(str(value)) for value in item.review_case_ids
                ),
            )

        weight_measurements = tuple(
            public_weight(item)
            for item in stored_measurements
            if item.data_type is CanonicalHealthType.BODY_MASS
        )
        healthkit_nutrition_samples = tuple(
            public_nutrition_sample(item)
            for item in stored_measurements
            if item.data_type.value.startswith("dietary_")
        )
        selected_weights = tuple(
            item
            for item in weight_measurements
            if item.is_selected
            and item.effective_value_kg is not None
            and item.disposition in {"included_source", "included_correction"}
        )
        selected_nutrition_samples = tuple(
            item
            for item in healthkit_nutrition_samples
            if item.is_selected
            and item.effective_value is not None
            and item.disposition in {"included_source", "included_correction"}
        )
        selected_days = tuple(item.measurement_local_day for item in selected_weights) + tuple(
            item.measurement_local_day for item in selected_nutrition_samples
        )
        start_date: date | None
        end_date: date | None
        if selection.start_date is not None and selection.end_date is not None:
            start_date, end_date = selection.start_date, selection.end_date
        elif selected_days:
            start_date = min(selected_days)
            end_date = max(selected_days)
        else:
            start_date = end_date = None

        by_day: dict[date, list[WeightMeasurement]] = {}
        for item in selected_weights:
            by_day.setdefault(item.measurement_local_day, []).append(item)
        days = []
        nutrition_days = []
        current = start_date
        while current is not None and end_date is not None and current <= end_date:
            candidates = by_day.get(current, [])
            if not candidates:
                days.append(PreferredDailyWeight(current, WeightDayStatus.MISSING, None))
            else:
                latest_at = max(item.source_start for item in candidates)
                latest = tuple(item for item in candidates if item.source_start == latest_at)
                values = {item.effective_value_kg for item in latest}
                review_case_ids = tuple(
                    sorted(
                        {case_id for item in latest for case_id in item.review_case_ids},
                        key=str,
                    )
                )
                observed = len(values) == 1
                days.append(
                    PreferredDailyWeight(
                        day=current,
                        status=(
                            WeightDayStatus.OBSERVED if observed else WeightDayStatus.AMBIGUOUS
                        ),
                        value_kg=next(iter(values)) if observed else None,
                        logical_measurement_ids=tuple(
                            item.logical_measurement_id for item in latest
                        ),
                        measurement_version_ids=tuple(
                            item.measurement_version_id for item in latest
                        ),
                        review_case_ids=review_case_ids,
                        quality_status=(
                            DataQualityStatus.PROVISIONAL
                            if review_case_ids
                            else DataQualityStatus.REVIEWED
                        ),
                    )
                )

            def feature(data_type: CanonicalHealthType, day: date) -> DailyNutritionFeature:
                contributors = tuple(
                    item
                    for item in selected_nutrition_samples
                    if item.measurement_local_day == day and item.data_type is data_type
                )
                review_case_ids = tuple(
                    sorted(
                        {case_id for item in contributors for case_id in item.review_case_ids},
                        key=str,
                    )
                )
                return DailyNutritionFeature(
                    data_type=data_type,
                    unit=canonical_unit_for(data_type),
                    value=(
                        sum(
                            item.effective_value
                            for item in contributors
                            if item.effective_value is not None
                        )
                        if contributors
                        else None
                    ),
                    logical_measurement_ids=tuple(
                        item.logical_measurement_id for item in contributors
                    ),
                    measurement_version_ids=tuple(
                        item.measurement_version_id for item in contributors
                    ),
                    review_case_ids=review_case_ids,
                    quality_status=(
                        DataQualityStatus.PROVISIONAL
                        if review_case_ids
                        else DataQualityStatus.REVIEWED
                    ),
                )

            nutrition_days.append(
                DailyNutrition(
                    day=current,
                    energy=feature(CanonicalHealthType.DIETARY_ENERGY_CONSUMED, current),
                    protein=feature(CanonicalHealthType.DIETARY_PROTEIN, current),
                    carbohydrates=feature(CanonicalHealthType.DIETARY_CARBOHYDRATES, current),
                    total_fat=feature(CanonicalHealthType.DIETARY_FAT_TOTAL, current),
                )
            )
            current += timedelta(days=1)
        return WeightNutrition(
            snapshot_ref=snapshot_ref,
            status=(
                DataQualityStatus.PROVISIONAL
                if any(item.review_case_ids for item in selected_weights)
                or any(item.review_case_ids for item in selected_nutrition_samples)
                else DataQualityStatus.REVIEWED
            ),
            days=tuple(days),
            weight_measurements=weight_measurements,
            nutrition_days=tuple(nutrition_days),
            healthkit_nutrition_samples=healthkit_nutrition_samples,
        )

    def load_activity_settings(self) -> ActivitySettings:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        record = self._store.load_activity_derivation_version(None)
        active = ActivityDerivationVersion(
            record.version_id, record.coverage_gap_minutes, record.source_classifier_version
        )
        recommendation = ActivityDerivationVersion(
            _ACTIVITY_DERIVATION_VERSION,
            _ACTIVITY_COVERAGE_GAP_MINUTES,
            _ACTIVITY_SOURCE_CLASSIFIER_VERSION,
        )
        return ActivitySettings(active, recommendation)

    @staticmethod
    def _context_record(
        value: StoredContextCoverageStart | None,
    ) -> ContextCoverageStartRecord | None:
        if value is None or value.start_date is None:
            return None
        return ContextCoverageStartRecord(
            ContextLogicalId(value.logical_id),
            ContextRevisionId(value.revision_id),
            value.start_date,
        )

    def load_daily_context(self, selection: SnapshotDateSelection) -> DailyContext:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        snapshot_ref = (
            self._store.load_active_snapshot_id()
            if selection.snapshot_ref is None
            else selection.snapshot_ref
        )
        if snapshot_ref is None:
            return DailyContext(None, None, "Europe/Berlin", ())
        coverage = self._store.load_context_coverage_start(snapshot_ref)
        context_as_of, timezone = self._store.load_context_as_of_date(snapshot_ref)
        if selection.start_date is None:
            start = context_as_of if coverage is None else coverage.start_date or context_as_of
            end = context_as_of
        else:
            selected_start = selection.start_date
            selected_end = selection.end_date
            assert selected_start is not None
            assert selected_end is not None
            start = selected_start
            end = min(selected_end, context_as_of)
        if start > end:
            return DailyContext(snapshot_ref, context_as_of, timezone, ())
        values = self._store.load_active_illness(snapshot_ref)
        periods = tuple(
            value
            for value in values
            if value.object_kind == "illness_period" and value.start_date is not None
        )
        stress_days = {
            value.start_date: value
            for value in values
            if value.object_kind == "daily_stress" and value.start_date is not None
        }
        custom_periods = tuple(
            value
            for value in values
            if value.object_kind == "custom_context_period" and value.start_date is not None
        )
        labels = {
            value.logical_id: value.name
            for value in values
            if value.object_kind == "custom_context_label" and value.name is not None
        }

        def illness_for(current: date) -> tuple[ContextIllnessOrigin, IllnessSeverity | None]:
            active = tuple(
                value
                for value in periods
                if value.start_date is not None
                and value.start_date <= current
                and (value.end_date is None or current <= value.end_date)
            )
            if active:
                return ContextIllnessOrigin.OBSERVED, max(
                    (
                        IllnessSeverity(value.severity)
                        for value in active
                        if value.severity is not None
                    ),
                    key=(
                        IllnessSeverity.MILD,
                        IllnessSeverity.MODERATE,
                        IllnessSeverity.SEVERE,
                    ).index,
                )
            return (
                ContextIllnessOrigin.ASSUMED_NONE
                if coverage is not None
                and coverage.start_date is not None
                and current >= coverage.start_date
                else ContextIllnessOrigin.UNKNOWN,
                None,
            )

        days = tuple(
            DailyContextDay(
                current,
                illness_for(current)[0],
                (
                    ContextStressOrigin.OBSERVED
                    if current in stress_days
                    else ContextStressOrigin.ASSUMED_AVERAGE
                    if coverage is not None
                    and coverage.start_date is not None
                    and current >= coverage.start_date
                    else ContextStressOrigin.UNKNOWN
                ),
                illness_for(current)[1],
                (
                    StressLevel(cast(str, stress_days[current].stress_level))
                    if current in stress_days and stress_days[current].stress_level is not None
                    else StressLevel.AVERAGE
                    if coverage is not None
                    and coverage.start_date is not None
                    and current >= coverage.start_date
                    else None
                ),
                tuple(
                    sorted(
                        labels[value.category_logical_id]
                        for value in custom_periods
                        if value.category_logical_id in labels
                        and value.start_date is not None
                        and value.start_date <= current
                        and (value.end_date is None or current <= value.end_date)
                    )
                ),
            )
            for current in (
                start + timedelta(days=index) for index in range((end - start).days + 1)
            )
        )
        return DailyContext(snapshot_ref, context_as_of, timezone, days)

    def load_context_records(self, snapshot_ref: SnapshotRef | None = None) -> ContextRecords:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        selected = self._store.load_active_snapshot_id() if snapshot_ref is None else snapshot_ref
        values = self._store.load_active_illness(selected)
        return ContextRecords(
            selected,
            self._context_record(self._store.load_context_coverage_start(selected)),
            tuple(
                CustomContextLabelRecord(
                    ContextLogicalId(value.logical_id),
                    ContextRevisionId(value.revision_id),
                    value.name,
                )
                for value in values
                if value.object_kind == "custom_context_label" and value.name is not None
            ),
            tuple(
                CustomContextPeriodRecord(
                    ContextLogicalId(value.logical_id),
                    ContextRevisionId(value.revision_id),
                    ContextLogicalId(value.category_logical_id),
                    value.start_date,
                    value.end_date,
                    value.note,
                )
                for value in values
                if value.object_kind == "custom_context_period"
                and value.category_logical_id is not None
                and value.start_date is not None
            ),
        )

    def load_context_audit(self, logical_id: ContextLogicalId) -> ContextAudit:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        coverage_revisions = self._store.load_context_coverage_audit(str(logical_id))
        revisions = (
            coverage_revisions
            if coverage_revisions
            else self._store.load_illness_revisions(str(logical_id))
        )
        return ContextAudit(
            logical_id,
            tuple(
                ContextAuditRevision(
                    ContextRevisionId(value.revision_id),
                    None
                    if value.previous_revision_id is None
                    else ContextRevisionId(value.previous_revision_id),
                    value.state,
                    value.start_date,
                )
                for value in revisions
            ),
        )

    def load_medication_days(self, selection: SnapshotDateSelection) -> MedicationDays:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        snapshot = (
            self._store.load_active_snapshot_id()
            if selection.snapshot_ref is None
            else selection.snapshot_ref
        )
        if snapshot is None:
            return MedicationDays(None, None, None, ())
        as_of = self._store.load_medication_as_of(snapshot)
        regimes = self._store.load_active_medication_regimes(snapshot)
        timezone = regimes[0].timezone if regimes else None
        if selection.start_date is None:
            start = as_of.astimezone(ZoneInfo(timezone or "Europe/Berlin")).date()
            end = start
        else:
            start = selection.start_date
            end = min(
                cast(date, selection.end_date),
                as_of.astimezone(ZoneInfo(timezone or "Europe/Berlin")).date(),
            )
        if start > end:
            return MedicationDays(snapshot, as_of, timezone, ())
        active = tuple(sorted(regimes, key=lambda regime: regime.starts_at))
        deviations = {
            (item.regime_logical_id, item.scheduled_at): item
            for item in self._store.load_active_medication_deviations(snapshot)
        }
        categories = {
            item.logical_id: item.name
            for item in self._store.load_active_intake_reason_categories(snapshot)
        }
        as_needed = self._store.load_active_as_needed_intakes(snapshot)
        days: list[MedicationDay] = []
        for offset in range((end - start).days + 1):
            current_day = start + timedelta(days=offset)
            regime = next(
                (item for item in reversed(active) if item.starts_at.date() <= current_day), None
            )
            if regime is None:
                days.append(MedicationDay(current_day, "unknown", ()))
                continue
            next_regime = next((item for item in active if item.starts_at > regime.starts_at), None)
            occurrences = tuple(
                MedicationDoseOccurrence(
                    name,
                    Decimal(amount),
                    unit,
                    scheduled,
                    "deviated"
                    if (regime.logical_id, scheduled) in deviations
                    else "assumed_as_planned",
                    tuple(
                        MedicationActualIntake(taken_at, Decimal(amount))
                        for taken_at, amount in deviations[
                            (regime.logical_id, scheduled)
                        ].actual_intakes
                    )
                    if (regime.logical_id, scheduled) in deviations
                    else (),
                )
                for name, amount, unit, local_time, weekdays in regime.scheduled_doses
                if Weekday(current_day.strftime("%A").lower()) in {Weekday(day) for day in weekdays}
                for scheduled in (
                    _resolve_medication_local_datetime(current_day, local_time, regime.timezone),
                )
                if scheduled >= regime.starts_at
                and scheduled <= as_of
                and (next_regime is None or scheduled < next_regime.starts_at)
            )
            as_needed_intakes = tuple(
                MedicationAsNeededIntake(
                    MedicationLogicalId(item.logical_id),
                    item.taken_at,
                    next(
                        entry[0]
                        for stored_regime in active
                        if stored_regime.logical_id == item.regime_logical_id
                        for entry in stored_regime.as_needed_medications
                        if entry[4] == item.entry_id
                    ),
                    Decimal(item.amount),
                    next(
                        entry[2]
                        for stored_regime in active
                        if stored_regime.logical_id == item.regime_logical_id
                        for entry in stored_regime.as_needed_medications
                        if entry[4] == item.entry_id
                    ),
                    None
                    if item.reason_category_logical_id is None
                    else categories.get(item.reason_category_logical_id),
                )
                for item in as_needed
                if item.taken_at.date() == current_day
            )
            days.append(
                MedicationDay(
                    current_day,
                    "planned" if occurrences else "empty",
                    occurrences,
                    as_needed_intakes,
                )
            )
        return MedicationDays(snapshot, as_of, timezone, tuple(days))

    def load_medication_plan(self, snapshot_ref: SnapshotRef | None = None) -> MedicationPlan:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        selected = self._store.load_active_snapshot_id() if snapshot_ref is None else snapshot_ref
        if selected is None:
            return MedicationPlan(None, None, ())
        return MedicationPlan(
            selected,
            self._store.load_medication_as_of(selected),
            tuple(
                MedicationRegimeRecord(
                    MedicationLogicalId(value.logical_id),
                    MedicationRevisionId(value.revision_id),
                    value.starts_at,
                    value.timezone,
                    tuple(
                        ScheduledDose(
                            name,
                            Decimal(amount),
                            unit,
                            local_time,
                            frozenset(Weekday(day) for day in weekdays),
                        )
                        for name, amount, unit, local_time, weekdays in value.scheduled_doses
                    ),
                    tuple(
                        AsNeededMedication(
                            name,
                            Decimal(amount),
                            unit,
                            tuple(MedicationLogicalId(item) for item in preferred),
                            MedicationPlanEntryId(entry_id),
                        )
                        for name, amount, unit, preferred, entry_id in value.as_needed_medications
                    ),
                )
                for value in self._store.load_active_medication_regimes(selected)
            ),
            tuple(
                IntakeReasonCategoryRecord(
                    MedicationLogicalId(value.logical_id),
                    MedicationRevisionId(value.revision_id),
                    value.name,
                )
                for value in self._store.load_active_intake_reason_categories(selected)
                if value.name is not None
            ),
        )

    def load_medication_audit(self, logical_id: MedicationLogicalId) -> MedicationAudit:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        regimes = self._store.load_medication_regime_audit(str(logical_id))
        if regimes:
            return MedicationAudit(
                logical_id,
                tuple(
                    MedicationAuditRevision(
                        MedicationRevisionId(value.revision_id),
                        None
                        if value.previous_revision_id is None
                        else MedicationRevisionId(value.previous_revision_id),
                        value.starts_at,
                        value.timezone,
                        tuple(
                            ScheduledDose(
                                name,
                                Decimal(amount),
                                unit,
                                local_time,
                                frozenset(Weekday(day) for day in weekdays),
                            )
                            for name, amount, unit, local_time, weekdays in value.scheduled_doses
                        ),
                    )
                    for value in regimes
                ),
            )
        categories = self._store.load_intake_reason_category_audit(str(logical_id))
        if categories:
            return MedicationAudit(
                logical_id,
                tuple(
                    IntakeReasonCategoryAuditRevision(
                        MedicationRevisionId(value.revision_id),
                        None
                        if value.previous_revision_id is None
                        else MedicationRevisionId(value.previous_revision_id),
                        value.state,
                        value.name,
                    )
                    for value in categories
                ),
            )
        as_needed = self._store.load_as_needed_intake_audit(str(logical_id))
        if as_needed:
            return MedicationAudit(
                logical_id,
                tuple(
                    AsNeededIntakeAuditRevision(
                        MedicationRevisionId(value.revision_id),
                        None
                        if value.previous_revision_id is None
                        else MedicationRevisionId(value.previous_revision_id),
                        value.state,
                        MedicationLogicalId(value.regime_logical_id),
                        MedicationPlanEntryId(value.entry_id),
                        value.taken_at,
                        Decimal(value.amount),
                        None
                        if value.reason_category_logical_id is None
                        else MedicationLogicalId(value.reason_category_logical_id),
                    )
                    for value in as_needed
                ),
            )
        return MedicationAudit(
            logical_id,
            tuple(
                MedicationDeviationAuditRevision(
                    MedicationRevisionId(value.revision_id),
                    None
                    if value.previous_revision_id is None
                    else MedicationRevisionId(value.previous_revision_id),
                    value.state,
                    MedicationLogicalId(value.regime_logical_id),
                    value.scheduled_at,
                    tuple(
                        MedicationActualIntake(taken_at, Decimal(amount))
                        for taken_at, amount in value.actual_intakes
                    ),
                )
                for value in self._store.load_medication_deviation_audit(str(logical_id))
            ),
        )

    def load_activity_days(self, selection: SnapshotDateSelection) -> ActivityDays:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        try:
            snapshot_ref, stored_measurements = self._store.load_activity_measurements(
                selection.snapshot_ref, selection.start_date, selection.end_date
            )
        except StoreError as error:
            raise HealthLabError("Aktivitätstagsprojektion ist nicht verfügbar.") from error

        def public_measurements(
            values: tuple[StoredMeasurement, ...],
        ) -> tuple[ActivityMeasurement, ...]:
            return tuple(
                ActivityMeasurement(
                    logical_measurement_id=item.logical_measurement_id,
                    measurement_version_id=item.measurement_version_id,
                    data_type=_ACTIVITY_TYPES[item.data_type],
                    unit=item.unit,
                    value=item.value,
                    effective_value=item.effective_value,
                    disposition=item.disposition,
                    is_selected=item.is_selected,
                    source_class=classify_activity_source(item.source_name, item.device),
                    source_start=item.source_start,
                    source_end=item.source_end,
                    source_updated_at=item.source_updated_at,
                    measurement_local_day=item.measurement_local_day,
                    source_name=item.source_name,
                    source_version=item.source_version,
                    device=item.device,
                    original_value=item.original_value,
                    original_unit=item.original_unit,
                    review_case_ids=tuple(
                        DataReviewCaseId(str(value)) for value in item.review_case_ids
                    ),
                )
                for item in values
            )

        measurements = public_measurements(stored_measurements)
        all_snapshot_ref, all_stored_measurements = self._store.load_activity_measurements(
            snapshot_ref, None, None
        )
        assert all_snapshot_ref == snapshot_ref
        all_measurements = public_measurements(all_stored_measurements)
        record = self._store.load_activity_derivation_version(snapshot_ref)
        derivation_version = ActivityDerivationVersion(
            record.version_id, record.coverage_gap_minutes, record.source_classifier_version
        )
        selected = tuple(
            item
            for item in all_measurements
            if item.is_selected
            and item.effective_value is not None
            and item.disposition in {"included_source", "included_correction"}
        )
        interval_selected = tuple(item for item in selected if item.source_end > item.source_start)

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

        def bridge(
            intervals: tuple[tuple[datetime, datetime], ...],
        ) -> tuple[tuple[datetime, datetime], ...]:
            merged: list[tuple[datetime, datetime]] = []
            for start, end in intervals:
                if (
                    merged
                    and start - merged[-1][1]
                    < timedelta(minutes=derivation_version.coverage_gap_minutes)
                    and merged[-1][1].utcoffset() == start.utcoffset()
                ):
                    merged[-1] = (merged[-1][0], end)
                else:
                    merged.append((start, end))
            return tuple(merged)

        watch_intervals = [
            (item.source_start, item.source_end)
            for item in interval_selected
            if item.source_class is ActivitySourceClass.WATCH
        ]
        _, workouts = self._store.load_workouts(snapshot_ref, None, None)
        watch_intervals.extend(
            (item.source_start, item.source_end)
            for item in workouts
            if item.is_selected
            and item.source_end > item.source_start
            and classify_activity_source(item.source_name, item.device) is ActivitySourceClass.WATCH
        )
        _, sleep_intervals = self._store.load_sleep_measurements(snapshot_ref)
        watch_intervals.extend(
            (item.source_start, item.source_end)
            for item in sleep_intervals
            if item.is_selected
            and item.source_end > item.source_start
            and _classify_sleep_source(item.source_name, item.device) is SleepSourceClass.WATCH
        )
        watch_coverage = bridge(union(tuple(watch_intervals)))
        watch_gaps = tuple(
            (left[1], right[0]) for left, right in pairwise(watch_coverage) if left[1] < right[0]
        )
        iphone_fallback_ids = {
            item.measurement_version_id
            for item in interval_selected
            if item.source_class is ActivitySourceClass.IPHONE
            and any(
                start <= item.source_start and item.source_end <= end for start, end in watch_gaps
            )
        }
        iphone_coverage: list[tuple[datetime, datetime]] = []
        for gap_start, gap_end in watch_gaps:
            iphone_coverage.extend(
                bridge(
                    union(
                        tuple(
                            (item.source_start, item.source_end)
                            for item in interval_selected
                            if item.measurement_version_id in iphone_fallback_ids
                            and gap_start <= item.source_start
                            and item.source_end <= gap_end
                        )
                    )
                )
            )

        def suppression_reason(item: ActivityMeasurement) -> str | None:
            if item not in selected:
                return None
            if (
                item.source_class is ActivitySourceClass.IPHONE
                and item.measurement_version_id not in iphone_fallback_ids
            ):
                return "iphone_outside_watch_gap"
            if item.source_class in {ActivitySourceClass.OTHER, ActivitySourceClass.UNKNOWN}:
                return "ineligible_source"
            return None

        measurements = tuple(
            replace(item, suppression_reason=suppression_reason(item)) for item in measurements
        )
        eligible = tuple(
            item
            for item in selected
            if item.source_class is ActivitySourceClass.WATCH
            or item.measurement_version_id in iphone_fallback_ids
        )
        start_date: date | None
        end_date: date | None
        if selection.start_date is not None and selection.end_date is not None:
            start_date, end_date = selection.start_date, selection.end_date
        elif eligible:
            start_date = min(item.measurement_local_day for item in eligible)
            end_date = max(item.measurement_local_day for item in eligible)
        else:
            start_date = end_date = None

        def feature(metric: ActivityMetric, day: date) -> DailyActivityMetric:
            contributors = tuple(
                item
                for item in eligible
                if item.data_type is metric and item.measurement_local_day == day
            )
            review_case_ids = tuple(
                sorted(
                    {case_id for item in contributors for case_id in item.review_case_ids}, key=str
                )
            )

            def source_total(source_class: ActivitySourceClass) -> float | None:
                values = tuple(
                    item.effective_value
                    for item in contributors
                    if item.source_class is source_class and item.effective_value is not None
                )
                return sum(values) if values else None

            return DailyActivityMetric(
                data_type=metric,
                unit=canonical_unit_for(CanonicalHealthType(metric.value)),
                value=(
                    sum(
                        item.effective_value
                        for item in contributors
                        if item.effective_value is not None
                    )
                    if contributors
                    else None
                ),
                logical_measurement_ids=tuple(item.logical_measurement_id for item in contributors),
                measurement_version_ids=tuple(item.measurement_version_id for item in contributors),
                review_case_ids=review_case_ids,
                quality_status=(
                    DataQualityStatus.PROVISIONAL if review_case_ids else DataQualityStatus.REVIEWED
                ),
                watch_value=source_total(ActivitySourceClass.WATCH),
                iphone_value=source_total(ActivitySourceClass.IPHONE),
            )

        coverage_segments: dict[date, list[ActivityCoverageSegment]] = {}
        coverage_candidates = tuple(
            item
            for item in selected
            if item.source_class in {ActivitySourceClass.WATCH, ActivitySourceClass.IPHONE}
            and item.source_end > item.source_start
        )
        if coverage_candidates:
            first = min(coverage_candidates, key=lambda item: item.measurement_local_day)
            last = max(coverage_candidates, key=lambda item: item.measurement_local_day)
            range_start = datetime.combine(
                first.measurement_local_day, time.min, first.source_start.tzinfo
            )
            range_end = datetime.combine(
                last.measurement_local_day + timedelta(days=1), time.min, last.source_start.tzinfo
            )
            covered = tuple(
                (start, end, ActivityCoverageKind.WATCH) for start, end in watch_coverage
            ) + tuple(
                (start, end, ActivityCoverageKind.IPHONE_FALLBACK) for start, end in iphone_coverage
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
                    ActivityCoverageKind.UNOBSERVED,
                )
                current_start = start
                while current_start < end:
                    midnight = datetime.combine(
                        current_start.date() + timedelta(days=1), time.min, current_start.tzinfo
                    )
                    current_end = min(end, midnight)
                    coverage_segments.setdefault(current_start.date(), []).append(
                        ActivityCoverageSegment(kind, current_start, current_end)
                    )
                    current_start = current_end

        days = []
        current = start_date
        while current is not None and end_date is not None and current <= end_date:
            days.append(
                ActivityDay(
                    day=current,
                    exercise_time=feature(ActivityMetric.EXERCISE_TIME, current),
                    step_count=feature(ActivityMetric.STEP_COUNT, current),
                    walking_running_distance=feature(
                        ActivityMetric.WALKING_RUNNING_DISTANCE, current
                    ),
                    active_energy=feature(ActivityMetric.ACTIVE_ENERGY, current),
                    coverage_segments=tuple(coverage_segments.get(current, ())),
                    is_complete=not any(
                        segment.kind is ActivityCoverageKind.UNOBSERVED
                        for segment in coverage_segments.get(current, ())
                    ),
                    incomplete_reasons=(
                        ("unobserved_coverage",)
                        if any(
                            segment.kind is ActivityCoverageKind.UNOBSERVED
                            for segment in coverage_segments.get(current, ())
                        )
                        else ()
                    ),
                )
            )
            current += timedelta(days=1)
        return ActivityDays(
            snapshot_ref=snapshot_ref,
            status=(
                DataQualityStatus.PROVISIONAL
                if any(item.review_case_ids for item in eligible)
                or any(not item.is_complete for item in days)
                else DataQualityStatus.REVIEWED
            ),
            days=tuple(days),
            measurements=measurements,
            source_classifier_version=derivation_version.source_classifier_version,
            derivation_version=derivation_version.version_id,
            coverage_gap_minutes=derivation_version.coverage_gap_minutes,
        )

    def load_workouts(self, selection: SnapshotDateSelection) -> Workouts:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        try:
            snapshot_ref, stored = self._store.load_workouts(
                selection.snapshot_ref, selection.start_date, selection.end_date
            )
        except StoreError as error:
            raise HealthLabError("Trainingsprojektion ist nicht verfügbar.") from error
        workouts = tuple(
            Workout(
                item.logical_workout_id,
                item.workout_version_id,
                item.original_activity_type,
                item.source_start,
                item.source_end,
                item.source_updated_at,
                item.measurement_local_day,
                item.source_name,
                item.source_version,
                item.device,
                item.strong_source_id_hash,
                item.reported_duration_minutes,
                item.effective_duration_minutes
                if item.effective_duration_minutes is not None
                else (item.source_end - item.source_start).total_seconds() / 60
                if item.reported_duration_minutes is None
                else item.reported_duration_minutes,
                item.distance_kilometers,
                item.active_energy_kilocalories,
                item.is_selected,
                tuple(DataReviewCaseId(str(case_id)) for case_id in item.review_case_ids),
            )
            for item in stored
        )
        aggregates = []
        for day, activity_type in sorted(
            {(item.measurement_local_day, item.original_activity_type) for item in workouts}
        ):
            contributors = tuple(
                item
                for item in workouts
                if item.is_selected
                and (item.measurement_local_day, item.original_activity_type)
                == (day, activity_type)
            )
            if contributors:
                aggregates.append(
                    WorkoutAggregate(
                        day,
                        activity_type,
                        len(contributors),
                        sum(item.effective_duration_minutes for item in contributors),
                        (
                            sum(
                                item.distance_kilometers
                                for item in contributors
                                if item.distance_kilometers is not None
                            )
                            if any(item.distance_kilometers is not None for item in contributors)
                            else None
                        ),
                        (
                            sum(
                                item.active_energy_kilocalories
                                for item in contributors
                                if item.active_energy_kilocalories is not None
                            )
                            if any(
                                item.active_energy_kilocalories is not None for item in contributors
                            )
                            else None
                        ),
                        tuple(
                            sorted(
                                {case for item in contributors for case in item.review_case_ids},
                                key=str,
                            )
                        ),
                    )
                )
        return Workouts(
            snapshot_ref,
            DataQualityStatus.PROVISIONAL
            if any(item.review_case_ids for item in workouts)
            else DataQualityStatus.REVIEWED,
            workouts,
            tuple(aggregates),
        )

    def load_sleep_days(self, selection: SnapshotDateSelection) -> SleepDays:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        try:
            snapshot_ref, stored = self._store.load_sleep_measurements(selection.snapshot_ref)
        except StoreError as error:
            raise HealthLabError("Schlafprojektion ist nicht verfügbar.") from error
        intervals = tuple(
            SleepInterval(
                logical_measurement_id=item.logical_measurement_id,
                measurement_version_id=item.measurement_version_id,
                original_category=item.original_category,
                canonical_category=_SLEEP_CATEGORIES[item.canonical_category],
                source_class=_classify_sleep_source(item.source_name, item.device),
                is_selected=item.is_selected,
                source_start=item.source_start,
                source_end=item.source_end,
                source_updated_at=item.source_updated_at,
                source_name=item.source_name,
                source_version=item.source_version,
                device=item.device,
            )
            for item in stored
            if (selection.start_date is None or item.source_end.date() >= selection.start_date)
            and (selection.end_date is None or item.source_start.date() <= selection.end_date)
        )
        accepted = tuple(item for item in intervals if item.source_class is SleepSourceClass.WATCH)
        rejected = tuple(
            item for item in intervals if item.source_class is not SleepSourceClass.WATCH
        )
        selected = tuple(item for item in accepted if item.is_selected)
        groups: list[list[SleepInterval]] = []
        for interval in selected:
            if interval.canonical_category is SleepCategory.IN_BED:
                continue
            if interval.source_end <= interval.source_start:
                continue
            if not groups or interval.source_start - max(
                item.source_end for item in groups[-1]
            ) > timedelta(minutes=90):
                groups.append([interval])
            else:
                groups[-1].append(interval)
        episodes_by_day: dict[date, list[SleepEpisode]] = {}
        for group in groups:
            start = min(item.source_start for item in group)
            end = max(item.source_end for item in group)
            in_bed = tuple(
                replace(
                    item,
                    source_start=max(item.source_start, start),
                    source_end=min(item.source_end, end),
                )
                for item in selected
                if item.canonical_category is SleepCategory.IN_BED
                and item.source_start < end
                and item.source_end > start
            )
            episode = _sleep_episode(tuple(group) + in_bed)
            episodes_by_day.setdefault(episode.end.date(), []).append(episode)
        if selection.start_date is not None and selection.end_date is not None:
            dates = tuple(
                selection.start_date + timedelta(days=offset)
                for offset in range((selection.end_date - selection.start_date).days + 1)
            )
        elif selected:
            first_day = min(item.source_end.date() for item in selected)
            last_day = max(item.source_end.date() for item in selected)
            dates = tuple(
                first_day + timedelta(days=offset)
                for offset in range((last_day - first_day).days + 1)
            )
        else:
            dates = ()

        def quality(day: date, ambiguous: bool = False) -> SleepQuality:
            day_intervals = tuple(item for item in intervals if item.source_end.date() == day)
            selected_day = tuple(item for item in day_intervals if item.is_selected)
            return SleepQuality(
                source_classifier_version=_SLEEP_SOURCE_CLASSIFIER_VERSION,
                derivation_version=_SLEEP_DERIVATION_VERSION,
                accepted_interval_count=sum(
                    item.source_class is SleepSourceClass.WATCH for item in selected_day
                ),
                rejected_interval_count=sum(
                    item.source_class is not SleepSourceClass.WATCH for item in selected_day
                ),
                contributing_watch_source_count=len(
                    {
                        item.source_name
                        for item in selected_day
                        if item.source_class is SleepSourceClass.WATCH
                    }
                ),
                source_counts=tuple(
                    SleepSourceCount(
                        source_class,
                        sum(
                            item.source_class is source_class
                            and source_class is SleepSourceClass.WATCH
                            for item in selected_day
                        ),
                        sum(
                            item.source_class is source_class
                            and source_class is not SleepSourceClass.WATCH
                            for item in selected_day
                        ),
                    )
                    for source_class in SleepSourceClass
                ),
                primary_selection_ambiguous=ambiguous,
            )

        days = []
        for day in dates:
            candidates = tuple(
                item for item in episodes_by_day.get(day, ()) if item.observed_sleep > timedelta()
            )
            if not candidates:
                status = (
                    SleepObservationStatus.PARTIAL
                    if any(
                        item.is_selected and item.source_class is SleepSourceClass.WATCH
                        for item in intervals
                        if item.source_end.date() == day
                    )
                    else SleepObservationStatus.UNOBSERVED
                )
                days.append(SleepDay(day, status, None, (), quality(day)))
                continue
            largest = max(item.observed_sleep for item in candidates)
            primary = tuple(item for item in candidates if item.observed_sleep == largest)
            if len(primary) != 1:
                days.append(
                    SleepDay(
                        day, SleepObservationStatus.PARTIAL, None, candidates, quality(day, True)
                    )
                )
                continue
            selected_episode = primary[0]
            complete = not (
                selected_episode.uncovered_gap
                or selected_episode.stage_ambiguous
                or selected_episode.asleep_awake_conflict
            )
            days.append(
                SleepDay(
                    day,
                    SleepObservationStatus.OBSERVED if complete else SleepObservationStatus.PARTIAL,
                    selected_episode,
                    tuple(item for item in candidates if item is not selected_episode),
                    quality(day),
                )
            )
        return SleepDays(snapshot_ref, tuple(days), accepted, rejected)

    def load_data_review(self, selection: DataReviewSelection) -> DataReview:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        snapshot, stored_cases, stored_cycles = load_review_state(self._store)
        store = self._store

        def allowed_actions(case: OpenDataReviewCase) -> tuple[DataReviewAction, ...]:
            kind = case.kind
            if kind == "plausibility":
                return (
                    DataReviewAction.CONFIRM,
                    DataReviewAction.CORRECT,
                    DataReviewAction.EXCLUDE_LOCAL,
                )
            if kind == "continued_override":
                detail = load_review_case_detail(store, case.review_case_id)
                first = (
                    DataReviewAction.CONFIRM if detail.reasons else DataReviewAction.ACCEPT_SOURCE
                )
                return (
                    first,
                    DataReviewAction.CORRECT,
                    DataReviewAction.EXCLUDE_LOCAL,
                )
            if kind == "suspected_source_deletion":
                return (DataReviewAction.CONFIRM, DataReviewAction.REJECT)
            if kind == "source_conflict":
                return (DataReviewAction.PREFER, DataReviewAction.SPLIT)
            if kind == "preferred_daily_weight_conflict":
                return (DataReviewAction.CORRECT, DataReviewAction.EXCLUDE_LOCAL)
            if kind in {"workout_plausibility", "workout_overlap"}:
                return (DataReviewAction.CORRECT, DataReviewAction.EXCLUDE_LOCAL)
            return ()

        cases = tuple(
            DataReviewCase(
                case_id=DataReviewCaseId(case.review_case_id),
                kind=DataReviewCaseKind(case.kind),
                logical_measurement_id=case.logical_measurement_id,
                measurement_version_id=case.measurement_version_id,
                rule_version_id=case.rule_version_id,
                evidence_fingerprint=case.evidence_fingerprint,
                candidate_version_ids=(
                    self._store.load_source_conflict_candidates(case.logical_measurement_id)
                    if case.kind == "source_conflict" and case.logical_measurement_id is not None
                    else ()
                ),
                allowed_actions=allowed_actions(case),
            )
            for case in stored_cases
            if selection.kind is None or case.kind == selection.kind.value
        )
        return DataReview(
            snapshot_ref=snapshot,
            cases=cases,
            status=(DataQualityStatus.PROVISIONAL if stored_cases else DataQualityStatus.REVIEWED),
            cycles=tuple(
                DataReviewCycle(
                    cycle_id=DataReviewCycleId(str(cycle.cycle_id)),
                    snapshot_ref=cycle.snapshot_id,
                    status=DataReviewCycleStatus(cycle.status),
                    open_case_count=cycle.open_case_count,
                    kind=DataReviewCycleKind(cycle.kind),
                    base_snapshot_ref=cycle.base_snapshot_id,
                    start_date=cycle.start_date,
                    end_date=cycle.end_date,
                    rule_version_id=cycle.rule_version_id,
                )
                for cycle in stored_cycles
            ),
        )

    def load_data_review_case(self, case_id: DataReviewCaseId) -> DataReviewCaseDetail:
        review = self.load_data_review(DataReviewSelection())
        case = next((item for item in review.cases if item.case_id == case_id), None)
        if case is None or self._store is None:
            raise HealthLabError("Datenprüffall ist nicht offen.")
        try:
            detail = load_review_case_detail(self._store, str(case_id))
        except ValueError as error:
            raise HealthLabError(str(error)) from error
        return DataReviewCaseDetail(
            case=case,
            source_type=detail.source_type,
            measured_at=detail.measured_at,
            effective_value=detail.effective_value,
            effective_value_source=(
                None
                if detail.effective_value_source is None
                else EffectiveValueSource(detail.effective_value_source)
            ),
            reasons=tuple(
                PlausibilityReason(
                    code=ReviewReasonCode(reason.code),
                    lower_bound=reason.lower_bound,
                    upper_bound=reason.upper_bound,
                    unit=reason.unit,
                )
                for reason in detail.reasons
            ),
            canonical_unit=(
                None if detail.canonical_unit is None else CanonicalUnit(detail.canonical_unit)
            ),
        )

    def load_workspace_status(self) -> WorkspaceStatus:
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        identity = self._store.load_identity()
        if identity.store_id is None or not identity.is_current:
            state = WorkspaceState.MIGRATION_REQUIRED
        elif self._store.load_restore_session() is not None:
            state = WorkspaceState.RESTORE_PENDING
        else:
            state = WorkspaceState.READY
        return WorkspaceStatus(
            mode=identity.mode,
            store_id=identity.store_id,
            person_binding=identity.person_binding,
            state=state,
            allowed_reads=(
                ("workspace_status", "migration_diagnostics")
                if state is WorkspaceState.MIGRATION_REQUIRED
                else (
                    ("workspace_status", "recovery_status")
                    if state is WorkspaceState.RESTORE_PENDING
                    else _READY_READS
                )
            ),
            allowed_writes=(
                ("migrate_store",)
                if state is WorkspaceState.MIGRATION_REQUIRED
                else (
                    ("import_health_export", "abort_metadata_restore")
                    if state is WorkspaceState.RESTORE_PENDING
                    else (
                        _READY_WRITES
                        if identity.mode is DataMode.REAL
                        else tuple(
                            item
                            for item in _READY_WRITES
                            if item not in {"create_metadata_backup", "begin_metadata_restore"}
                        )
                    )
                )
            ),
        )

    def load_recovery_status(self) -> RecoveryStatus:
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        try:
            return self._public_recovery_status(load_metadata_restore(self._store))
        except StoreError as error:
            raise HealthLabError("Wiederherstellungsstatus ist nicht verfügbar.") from error

    def load_migration_diagnostics(self) -> MigrationDiagnostics:
        self._require_open()
        if self.load_workspace_status().state is WorkspaceState.RESTORE_PENDING:
            raise HealthLabError("Während der Wiederherstellung ist Migration gesperrt.")
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        raw_source = self._store.load_identity().schema_version
        target, steps, diagnostics = plan_store_migration(raw_source)
        try:
            source = int(raw_source)
        except ValueError:
            source = None
        return MigrationDiagnostics(source, target, steps, diagnostics)

    def load_plausibility_rules(self) -> PlausibilityRules:
        self._require_ready()
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        grouped: dict[CanonicalHealthType, list[PlausibilityRuleVersion]] = {}
        for record in load_plausibility_rule_state(self._store):
            data_type = CanonicalHealthType(record.data_type)
            grouped.setdefault(data_type, []).append(
                PlausibilityRuleVersion(
                    record.version_id,
                    data_type,
                    PlausibilityRuleSpecification(
                        CanonicalUnit(record.unit),
                        record.fixed_lower_bound,
                        record.fixed_upper_bound,
                        record.personal_range_enabled,
                    ),
                    record.effective_from,
                    record.created_at,
                    record.recommendation_id,
                    record.effective_timezone,
                    record.effective_offset_minutes,
                )
            )
        recommendations = {
            CanonicalHealthType(record.data_type): PlausibilityRuleRecommendation(
                record.recommendation_id or "builtin-plausibility/v1",
                PlausibilityRuleSpecification(
                    CanonicalUnit(record.unit),
                    record.fixed_lower_bound,
                    record.fixed_upper_bound,
                    record.personal_range_enabled,
                ),
            )
            for record in plausibility_rule_recommendations()
        }
        return PlausibilityRules(
            tuple(
                PlausibilityRule(
                    data_type,
                    tuple(versions),
                    recommendations[data_type],
                )
                for data_type in sorted(
                    grouped.keys() | recommendations.keys(), key=lambda x: x.value
                )
                for versions in (grouped.get(data_type, []),)
            )
        )

    def _require_open(self) -> OverviewReader:
        if self._overview_reader is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        return self._overview_reader

    def _require_ready(self) -> None:
        state = self.load_workspace_status().state
        if state is WorkspaceState.MIGRATION_REQUIRED:
            raise HealthLabError("Datenspeicher benötigt zuerst eine Migration.")
        if state is WorkspaceState.RESTORE_PENDING:
            raise HealthLabError("Datenspeicher befindet sich in Wiederherstellung.")
