from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import uuid4

from personal_health_lab import DataMode
from personal_health_lab.health_import import (
    HealthExportEstimate,
    HealthImportError,
    ImportId,
    OperationId,
    SnapshotId,
    estimate_health_export,
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
from personal_health_lab.storage import (
    CapacityCheck,
    CapacityStatus,
    FileVaultCheck,
    FileVaultReason,
    FileVaultStatus,
    LocalStore,
    PersonBindingStatus,
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


@dataclass(frozen=True, slots=True)
class WorkspaceStatus:
    mode: DataMode
    store_id: StoreId | None
    person_binding: PersonBindingStatus
    state: WorkspaceState = WorkspaceState.READY
    allowed_reads: tuple[str, ...] = ("workspace_status", "overview")
    allowed_writes: tuple[str, ...] = ("import_health_export",)


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


WriteRequest = ImportHealthExport


class WriteApprovalStatus(StrEnum):
    READY = "ready"
    CONFIRMATION_REQUIRED = "confirmation_required"
    BLOCKED = "blocked"


class WriteConfirmation(StrEnum):
    REAL_IMPORT_SAME_PERSON = "real_import_same_person"
    FILEVAULT_UNPROTECTED = "filevault_unprotected"
    FILEVAULT_TRANSITIONING = "filevault_transitioning"
    FILEVAULT_UNKNOWN = "filevault_unknown"
    LEGACY_STORE_MIGRATION = "legacy_store_migration"


@dataclass(frozen=True, slots=True)
class WriteApproval:
    status: WriteApprovalStatus


@dataclass(frozen=True, slots=True)
class ImportHealthExportPlan:
    package_hash: str
    package_size: int
    input_bytes: int = 0
    record_count: int = 0


WritePlanDetails = ImportHealthExportPlan


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
    diagnostics: tuple[str, ...] = ()


class WriteNotStartedStatus(StrEnum):
    PLAN_CHANGED = "plan_changed"
    BLOCKED = "blocked"
    STORE_BUSY = "store_busy"


@dataclass(frozen=True, slots=True)
class WriteNotStarted:
    status: WriteNotStartedStatus
    diagnostics: tuple[str, ...] = ()


WriteResult = ImportReceipt | WriteNotStarted


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    operation_id: OperationId
    plan_fingerprint: PlanFingerprint
    result: WriteResult
    final_preflight: WritePreflight
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
        filevault = (
            probe_filevault(self._config.active_store)
            if self._config.mode is DataMode.REAL
            else None
        )
        return self._build_import_plan(request, filevault)

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
            estimate = estimate_health_export(
                request.package_path,
                max_package_bytes=self._config.max_import_package_bytes,
                max_entries=self._config.max_import_entries,
                max_entry_bytes=self._config.max_import_entry_bytes,
                max_uncompressed_bytes=self._config.max_import_uncompressed_bytes,
                max_compression_ratio=self._config.max_import_compression_ratio,
            )
        except OSError:
            package_size = 0
            package_hash = ""
            unavailable = True
            estimate = HealthExportEstimate(0, 0)
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
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
        if capacity.status is not CapacityStatus.READY:
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
            confirmation_list = [WriteConfirmation.REAL_IMPORT_SAME_PERSON]
            if workspace.store_id is None:
                confirmation_list.append(WriteConfirmation.LEGACY_STORE_MIGRATION)
            if filevault.status is not FileVaultStatus.PROTECTED:
                confirmation_list.append(WriteConfirmation(f"filevault_{filevault.status.value}"))
            confirmations = tuple(confirmation_list)
            diagnostics = tuple(confirmation.value for confirmation in confirmations)
            approval = WriteApproval(WriteApprovalStatus.CONFIRMATION_REQUIRED)
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
                package_hash, package_size, estimate.input_bytes, estimate.record_count
            ),
            preflight=WritePreflight(
                approval, confirmations, filevault, diagnostics, capacity
            ),
        )

    @staticmethod
    def _filevault_allows_execution(
        expected: FileVaultCheck, actual: FileVaultCheck
    ) -> bool:
        return expected.target_volume == actual.target_volume and (
            expected == actual or actual.status is FileVaultStatus.PROTECTED
        )

    def _authorization_plan(
        self,
        request: ImportHealthExport,
        current_plan: WritePlan,
        expected_plan: PlanFingerprint,
    ) -> WritePlan | None:
        if current_plan.fingerprint == expected_plan:
            return current_plan
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
        try:
            workspace = self.load_workspace_status()
            writer = LocalStore.open_writer(
                root=self._config.active_store, mode=self._config.mode
            )
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
            if workspace.store_id is None:
                writer.initialize_identity(
                    confirm_existing_person=self._config.mode is DataMode.REAL
                )
            result = import_health_export(
                request.package_path,
                store=writer,
                max_package_bytes=self._config.max_import_package_bytes,
                max_entries=self._config.max_import_entries,
                max_entry_bytes=self._config.max_import_entry_bytes,
                max_uncompressed_bytes=self._config.max_import_uncompressed_bytes,
                max_compression_ratio=self._config.max_import_compression_ratio,
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
            anomaly_count=0,
            package_record_count=result.package_record_count,
            logical_measurement_count=result.logical_measurement_count,
            measurement_version_count=result.measurement_version_count,
            diagnostics=result.diagnostics,
        )
        return WriteReceipt(
            operation_id=result.operation_id,
            plan_fingerprint=expected_plan,
            result=import_result,
            final_preflight=final_preflight,
            diagnostics=result.diagnostics,
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

    def load_workspace_status(self) -> WorkspaceStatus:
        self._require_open()
        if self._store is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        identity = self._store.load_identity()
        return WorkspaceStatus(
            mode=identity.mode,
            store_id=identity.store_id,
            person_binding=identity.person_binding,
            state=(
                WorkspaceState.READY
                if identity.store_id is not None
                else WorkspaceState.MIGRATION_REQUIRED
            ),
        )

    def _require_open(self) -> OverviewReader:
        if self._overview_reader is None:
            raise HealthLabError("HealthLab muss als Context Manager geöffnet werden.")
        return self._overview_reader
