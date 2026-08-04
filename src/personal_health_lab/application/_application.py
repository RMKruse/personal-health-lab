from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Literal, Self, get_args
from uuid import uuid4

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
from personal_health_lab.health_data import DataQualityStatus, ModelMaturityStatus
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
    CapacityCheck,
    CapacityStatus,
    FileVaultCheck,
    FileVaultReason,
    FileVaultStatus,
    LocalStore,
    OpenDataReviewCase,
    PersonBindingStatus,
    PublishBatchDecisionResult,
    PublishDecisionResult,
    StoreBusyError,
    StoreError,
    StoreId,
    probe_filevault,
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
        expected_unit = {
            CanonicalHealthType.ACTIVE_ENERGY: CanonicalUnit.KILOCALORIE,
            CanonicalHealthType.APPLE_RESTING_HEART_RATE: CanonicalUnit.BEATS_PER_MINUTE,
        }[self.data_type]
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
class SourceValueAcceptance:
    measurement_version_id: MeasurementVersionId
    note: str | None = None


DataReviewResolution = (
    DataConfirmation
    | DataCorrection
    | LocalMeasurementExclusion
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
)


class WriteApprovalStatus(StrEnum):
    READY = "ready"
    CONFIRMATION_REQUIRED = "confirmation_required"
    BLOCKED = "blocked"


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
    | AnalysisReceipt
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
        if not isinstance(request, get_args(WriteRequest)):
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
        if isinstance(request, RunRestingHeartRateAnalysis):
            return self._build_resting_heart_rate_analysis_plan(request)
        if isinstance(request, RunHistoricalReview):
            return self._build_historical_review_plan(request)
        if isinstance(request, CreatePlausibilityRuleVersion):
            return self._build_plausibility_rule_plan(request)
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
                ]
                corrected_value: float | None = None
                canonical_unit: str | None = None
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
                        if case is None
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
                for data_type, versions in sorted(grouped.items(), key=lambda item: item[0].value)
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
