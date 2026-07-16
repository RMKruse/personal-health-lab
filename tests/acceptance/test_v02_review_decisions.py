from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import duckdb
import pytest

from personal_health_lab.application import (
    AnalysisDefinitionId,
    BatchDecisionTarget,
    CanonicalUnit,
    ConfigurationError,
    ConfirmDataReviewBatch,
    DataConfirmation,
    DataCorrection,
    DataMode,
    DataReviewCaseKind,
    DataReviewSelection,
    HealthLab,
    ImportHealthExport,
    ImportReceipt,
    LocalMeasurementExclusion,
    OverviewSelection,
    ResolveDataReviewCase,
    RestingHeartRateAnalysisConfig,
    RevokeDataReviewDecision,
    RuntimeConfig,
    SingleDecisionTarget,
    SourceValueAcceptance,
    WriteBatchDecisionReceipt,
    WriteDecisionReceipt,
    WriteNotStarted,
    WriteNotStartedStatus,
)
from personal_health_lab.synthetic_export import generate_export


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")


def _package(
    path: Path,
    records: list[tuple[str, str, float, str]],
    *,
    export_date: datetime,
) -> Path:
    rows = [
        f'<Record type="{source_type}" sourceName="Test Watch" sourceVersion="1" '
        f'device="Test Device" unit="{unit}" '
        'creationDate="2024-01-01 07:00:00 +0000" '
        'startDate="2024-01-01 07:00:00 +0000" '
        f'endDate="2024-01-01 07:00:00 +0000" value="{value}">'
        f'<MetadataEntry key="HKMetadataKeySyncIdentifier" value="{sync_id}"/>'
        "</Record>"
        for source_type, unit, value, sync_id in records
    ]
    xml = (
        '<?xml version="1.0"?><HealthData>'
        f'<ExportDate value="{export_date:%Y-%m-%d %H:%M:%S} +0000"/>'
        f"{''.join(rows)}</HealthData>"
    )
    entry = ZipInfo("apple_health_export/export.xml", date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = ZIP_DEFLATED
    with ZipFile(path, "w") as archive:
        archive.writestr(entry, xml)
    return path


def _execute_import(health_lab: HealthLab, package: Path) -> ImportReceipt:
    request = ImportHealthExport(package)
    plan = health_lab.preview_write(request)
    result = health_lab.execute_write(request, expected_plan=plan.fingerprint).result
    assert isinstance(result, ImportReceipt)
    return result


def _execute(health_lab: HealthLab, request: object) -> WriteDecisionReceipt:
    plan = health_lab.preview_write(request)  # type: ignore[arg-type]
    result = health_lab.execute_write(  # type: ignore[arg-type]
        request, expected_plan=plan.fingerprint
    ).result
    assert isinstance(result, WriteDecisionReceipt)
    return result


def _resolved(config: RuntimeConfig, snapshot: object) -> tuple[object, ...]:
    path = (
        config.synthetic_store
        / "parquet/snapshots"
        / str(snapshot)
        / "resolved_measurements.parquet"
    )
    return duckdb.connect().execute("SELECT * FROM read_parquet(?)", (str(path),)).fetchone()


def test_review_decision_requests_enforce_mandatory_reasons() -> None:
    with pytest.raises(ConfigurationError):
        DataCorrection("a" * 64, 61, CanonicalUnit.BEATS_PER_MINUTE, " ")
    with pytest.raises(ConfigurationError):
        LocalMeasurementExclusion("a" * 64, " ")
    with pytest.raises(ConfigurationError):
        RevokeDataReviewDecision(SingleDecisionTarget("a" * 32), " ")


def test_batch_confirmation_materializes_every_value_and_revokes_only_effective_members(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    package = _package(
        tmp_path / "batch.zip",
        [
            ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", value, f"hr-{index}")
            for index, value in enumerate((251, 252, 253))
        ],
        export_date=datetime(2024, 2, 1, tzinfo=UTC),
    )
    with HealthLab.open(config) as health_lab:
        _execute_import(health_lab, package)
        request = ConfirmDataReviewBatch(
            DataReviewSelection(DataReviewCaseKind.PLAUSIBILITY), "gemeinsam geprüft"
        )
        plan = health_lab.preview_write(request)
        assert plan.details.count == 3
        assert sorted(item.effective_value for item in plan.details.matches) == [251, 252, 253]
        assert tuple(sorted(str(item.case_id) for item in plan.details.matches)) == tuple(
            str(item.case_id) for item in plan.details.matches
        )

        result = health_lab.execute_write(request, expected_plan=plan.fingerprint).result
        assert isinstance(result, WriteBatchDecisionReceipt)
        assert len(result.decision_ids) == 3
        assert health_lab.load_data_review(request.selection).cases == ()

        _execute(
            health_lab,
            RevokeDataReviewDecision(
                SingleDecisionTarget(result.decision_ids[0]), "einzeln erneut prüfen"
            ),
        )
        reopened = health_lab.load_data_review(request.selection).cases
        assert len(reopened) == 1
        _execute(
            health_lab,
            ResolveDataReviewCase(reopened[0].case_id, DataConfirmation("später einzeln")),
        )
        revoked = health_lab.execute_write(
            RevokeDataReviewDecision(
                BatchDecisionTarget(result.batch_action_id), "Sammelaktion zurücknehmen"
            ),
            expected_plan=health_lab.preview_write(
                RevokeDataReviewDecision(
                    BatchDecisionTarget(result.batch_action_id), "Sammelaktion zurücknehmen"
                )
            ).fingerprint,
        ).result
        assert isinstance(revoked, WriteBatchDecisionReceipt)
        assert len(revoked.decision_ids) == 2
        assert len(health_lab.load_data_review(request.selection).cases) == 2


def test_batch_revoke_preserves_a_later_individual_correction(tmp_path: Path) -> None:
    config = _config(tmp_path)
    package = _package(
        tmp_path / "batch-correction.zip",
        [
            ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", value, f"hr-{index}")
            for index, value in enumerate((251, 252))
        ],
        export_date=datetime(2024, 2, 1, tzinfo=UTC),
    )
    with HealthLab.open(config) as health_lab:
        _execute_import(health_lab, package)
        request = ConfirmDataReviewBatch(
            DataReviewSelection(DataReviewCaseKind.PLAUSIBILITY), "gemeinsam geprüft"
        )
        plan = health_lab.preview_write(request)
        result = health_lab.execute_write(request, expected_plan=plan.fingerprint).result
        assert isinstance(result, WriteBatchDecisionReceipt)

        corrected_match = plan.details.matches[0]
        _execute(
            health_lab,
            ResolveDataReviewCase(
                None,
                DataCorrection(
                    corrected_match.measurement_version_id,
                    61,
                    CanonicalUnit.BEATS_PER_MINUTE,
                    "spätere Einzelkorrektur",
                ),
            ),
        )
        revoke_request = RevokeDataReviewDecision(
            BatchDecisionTarget(result.batch_action_id), "Sammelaktion zurücknehmen"
        )
        revoke_plan = health_lab.preview_write(revoke_request)
        assert revoke_plan.details.count == 1
        revoked = health_lab.execute_write(
            revoke_request, expected_plan=revoke_plan.fingerprint
        ).result

        assert isinstance(revoked, WriteBatchDecisionReceipt)
        assert len(revoked.decision_ids) == 1
        reopened = health_lab.load_data_review(request.selection).cases
        assert len(reopened) == 1
        assert reopened[0].measurement_version_id != corrected_match.measurement_version_id


def test_batch_confirmation_fails_atomically_when_the_match_set_changes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with HealthLab.open(config) as health_lab:
        _execute_import(
            health_lab,
            _package(
                tmp_path / "batch-change.zip",
                [
                    (
                        "HKQuantityTypeIdentifierRestingHeartRate",
                        "count/min",
                        value,
                        f"hr-{index}",
                    )
                    for index, value in enumerate((251, 252, 253))
                ],
                export_date=datetime(2024, 2, 1, tzinfo=UTC),
            ),
        )
        batch = ConfirmDataReviewBatch(DataReviewSelection(DataReviewCaseKind.PLAUSIBILITY))
        plan = health_lab.preview_write(batch)
        first = health_lab.load_data_review(batch.selection).cases[0]
        _execute(
            health_lab,
            ResolveDataReviewCase(first.case_id, DataConfirmation("einzeln geprüft")),
        )

        result = health_lab.execute_write(batch, expected_plan=plan.fingerprint).result
        assert isinstance(result, WriteNotStarted)
        assert result.status is WriteNotStartedStatus.PLAN_CHANGED
        assert len(health_lab.load_data_review(batch.selection).cases) == 2


def test_large_batch_plan_remains_complete(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with HealthLab.open(config) as health_lab:
        _execute_import(
            health_lab,
            _package(
                tmp_path / "large-batch.zip",
                [
                    (
                        "HKQuantityTypeIdentifierRestingHeartRate",
                        "count/min",
                        251 + index,
                        f"batch-{index}",
                    )
                    for index in range(100)
                ],
                export_date=datetime(2024, 2, 1, tzinfo=UTC),
            ),
        )
        plan = health_lab.preview_write(
            ConfirmDataReviewBatch(DataReviewSelection(DataReviewCaseKind.PLAUSIBILITY))
        )

    assert plan.details.count == 100
    assert len(plan.details.matches) == 100


def test_corrections_supersede_forward_and_revocation_uses_source(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    package = _package(
        tmp_path / "flagged.zip",
        [("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 251, "hr-1")],
        export_date=datetime(2024, 2, 1, tzinfo=UTC),
    )
    with HealthLab.open(config) as health_lab:
        imported = _execute_import(health_lab, package)
        case = health_lab.load_data_review(DataReviewSelection()).cases[0]
        version = case.measurement_version_id
        assert version is not None
        first = _execute(
            health_lab,
            ResolveDataReviewCase(
                case.case_id,
                DataCorrection(version, 61, CanonicalUnit.BEATS_PER_MINUTE, "falscher Wert"),
            ),
        )
        second = _execute(
            health_lab,
            ResolveDataReviewCase(
                None,
                DataCorrection(version, 62, CanonicalUnit.BEATS_PER_MINUTE, "genauer geprüft"),
            ),
        )
        revoked = _execute(
            health_lab,
            RevokeDataReviewDecision(SingleDecisionTarget(second.decision_id), "zurückgenommen"),
        )
        review = health_lab.load_data_review(DataReviewSelection())

    assert _resolved(config, imported.snapshot_ref)[3:7] == (251, "count/min", "source", None)
    assert _resolved(config, first.snapshot_ref)[2:9] == (
        "included_correction",
        61,
        "count/min",
        "correction",
        str(first.decision_id),
        str(first.decision_id),
        None,
    )
    assert _resolved(config, revoked.snapshot_ref)[2:7] == (
        "included_source",
        251,
        "count/min",
        "source",
        None,
    )
    assert len(review.cases) == 1
    assert review.cases[0].kind is DataReviewCaseKind.PLAUSIBILITY
    assert review.cycles[0].status.value == "open"


def test_correction_without_a_case_can_be_revoked_to_the_clean_source(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with HealthLab.open(config) as health_lab:
        _execute_import(
            health_lab,
            _package(
                tmp_path / "clean.zip",
                [("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 60, "hr-1")],
                export_date=datetime(2024, 2, 1, tzinfo=UTC),
            ),
        )
        version = health_lab.load_overview(OverviewSelection()).daily_series[0].values[
            0
        ].measurement_version_ids[0]
        corrected = _execute(
            health_lab,
            ResolveDataReviewCase(
                None,
                DataCorrection(
                    version, 61, CanonicalUnit.BEATS_PER_MINUTE, "manuell geprüft"
                ),
            ),
        )
        revoked = _execute(
            health_lab,
            RevokeDataReviewDecision(
                SingleDecisionTarget(corrected.decision_id), "zurückgenommen"
            ),
        )
        review = health_lab.load_data_review(DataReviewSelection())

    assert _resolved(config, revoked.snapshot_ref)[2:7] == (
        "included_source",
        60,
        "count/min",
        "source",
        None,
    )
    assert review.cases == ()


def test_new_clean_source_version_can_replace_a_continued_correction(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with HealthLab.open(config) as health_lab:
        _execute_import(
            health_lab,
            _package(
                tmp_path / "old.zip",
                [("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 251, "hr-1")],
                export_date=datetime(2024, 2, 1, tzinfo=UTC),
            ),
        )
        case = health_lab.load_data_review(DataReviewSelection()).cases[0]
        old_version = case.measurement_version_id
        assert old_version is not None
        corrected = _execute(
            health_lab,
            ResolveDataReviewCase(
                case.case_id,
                DataCorrection(
                    old_version, 61, CanonicalUnit.BEATS_PER_MINUTE, "falscher Wert"
                ),
            ),
        )
        _execute_import(
            health_lab,
            _package(
                tmp_path / "new.zip",
                [("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 62, "hr-1")],
                export_date=datetime(2024, 2, 2, tzinfo=UTC),
            ),
        )
        continued = health_lab.load_data_review(DataReviewSelection()).cases[0]
        assert continued.kind is DataReviewCaseKind.CONTINUED_OVERRIDE
        assert continued.allowed_actions == ("accept_source", "correct", "exclude_local")
        accepted = _execute(
            health_lab,
            ResolveDataReviewCase(
                continued.case_id,
                SourceValueAcceptance(continued.measurement_version_id, "Apple korrigiert"),
            ),
        )

    assert _resolved(config, corrected.snapshot_ref)[3:7] == (
        61,
        "count/min",
        "correction",
        str(corrected.decision_id),
    )
    assert _resolved(config, accepted.snapshot_ref)[2:7] == (
        "included_source",
        62,
        "count/min",
        "source",
        None,
    )


def test_new_flagged_source_version_requires_confirmation_after_local_exclusion(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with HealthLab.open(config) as health_lab:
        _execute_import(
            health_lab,
            _package(
                tmp_path / "old.zip",
                [("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 251, "hr-1")],
                export_date=datetime(2024, 2, 1, tzinfo=UTC),
            ),
        )
        case = health_lab.load_data_review(DataReviewSelection()).cases[0]
        old_version = case.measurement_version_id
        assert old_version is not None
        _execute(
            health_lab,
            ResolveDataReviewCase(
                case.case_id,
                LocalMeasurementExclusion(old_version, "sicher falsch"),
            ),
        )
        _execute_import(
            health_lab,
            _package(
                tmp_path / "new.zip",
                [("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 252, "hr-1")],
                export_date=datetime(2024, 2, 2, tzinfo=UTC),
            ),
        )
        continued = health_lab.load_data_review(DataReviewSelection()).cases[0]
        assert continued.allowed_actions == ("confirm", "correct", "exclude_local")
        confirmed = _execute(
            health_lab,
            ResolveDataReviewCase(continued.case_id, DataConfirmation("bewusst bestätigt")),
        )

    assert _resolved(config, confirmed.snapshot_ref)[2:7] == (
        "included_source",
        252,
        "count/min",
        "source",
        None,
    )


def test_confirmation_stales_but_never_rewrites_an_existing_analysis(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    analysis = RestingHeartRateAnalysisConfig(AnalysisDefinitionId("lag-signal-v1"))
    with HealthLab.open(config) as health_lab:
        _execute_import(health_lab, fixture.export_path)
        first = health_lab.run_resting_hr_analysis(analysis)
        old_result = health_lab.load_overview(OverviewSelection()).resting_hr_analysis
        case = next(
            item
            for item in health_lab.load_data_review(DataReviewSelection()).cases
            if item.kind is DataReviewCaseKind.PLAUSIBILITY
        )
        decision = _execute(
            health_lab,
            ResolveDataReviewCase(case.case_id, DataConfirmation("geprüft")),
        )
        stale = health_lab.load_overview(OverviewSelection())
        rerun = health_lab.run_resting_hr_analysis(analysis)

    assert old_result is not None
    assert stale.resting_hr_analysis is None
    assert old_result.provenance is not None
    assert old_result.provenance.analysis_run_id == first.analysis_run_id
    assert old_result.provenance.snapshot_id == first.snapshot_ref
    assert rerun.analysis_run_id != first.analysis_run_id
    assert rerun.snapshot_ref == decision.snapshot_ref
