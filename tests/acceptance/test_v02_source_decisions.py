from __future__ import annotations

import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import duckdb

from personal_health_lab.application import (
    DataMode,
    DataReviewCaseKind,
    DataReviewSelection,
    HealthLab,
    ImportHealthExport,
    ImportReceipt,
    ResolveDataReviewCase,
    RevokeDataReviewDecision,
    RuntimeConfig,
    SingleDecisionTarget,
    SourceConflictResolution,
    SourceConflictStrategy,
    SourceDeletionResolution,
    SourceDeletionVerdict,
    WriteDecisionReceipt,
)


def _record(value: int, start: str, *, sync_id: str | None = None) -> str:
    end = start.replace(":00 +0100", ":01 +0100")
    metadata = (
        ""
        if sync_id is None
        else f'<MetadataEntry key="HKMetadataKeySyncIdentifier" value="{sync_id}"/>'
    )
    return (
        '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
        'sourceName="Test Watch" device="Test Device" unit="count/min" '
        f'sourceVersion="1" creationDate="{end}" startDate="{start}" '
        f'endDate="{end}" value="{value}">{metadata}</Record>'
    )


def _package(path: Path, export_date: str | None, *records: str) -> Path:
    date_element = "" if export_date is None else f'<ExportDate value="{export_date}"/>'
    xml = f'<?xml version="1.0"?><HealthData>{date_element}{"".join(records)}</HealthData>'
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    return path


def _config(tmp_path: Path, name: str = "store") -> RuntimeConfig:
    return RuntimeConfig(DataMode.SYNTHETIC, tmp_path / name, tmp_path / "real")


def _execute(health_lab: HealthLab, request: object) -> object:
    plan = health_lab.preview_write(request)  # type: ignore[arg-type]
    return health_lab.execute_write(  # type: ignore[arg-type]
        request, expected_plan=plan.fingerprint
    ).result


def _deletion_case(health_lab: HealthLab) -> object:
    return next(
        case
        for case in health_lab.load_data_review(DataReviewSelection()).cases
        if case.kind is DataReviewCaseKind.SUSPECTED_SOURCE_DELETION
    )


def _resolved_rows(config: RuntimeConfig, snapshot_ref: object) -> list[tuple[object, ...]]:
    path = (
        config.synthetic_store
        / "parquet/snapshots"
        / str(snapshot_ref)
        / "resolved_measurements.parquet"
    )
    return (
        duckdb.connect()
        .execute(
            "SELECT logical_measurement_id, selected_measurement_version_id, disposition, "
            "conflict_resolution_decision_id FROM read_parquet(?) ORDER BY logical_measurement_id",
            (str(path),),
        )
        .fetchall()
    )


def test_comparable_exports_open_one_deletion_case_per_absence_phase(tmp_path: Path) -> None:
    missing = _record(60, "2024-01-01 07:00:00 +0100")
    anchor = _record(61, "2024-01-02 07:00:00 +0100")
    with HealthLab.open(_config(tmp_path)) as health_lab:
        _execute(
            health_lab,
            ImportHealthExport(
                _package(tmp_path / "base.zip", "2024-01-03 12:00:00 +0100", missing, anchor)
            ),
        )
        _execute(
            health_lab,
            ImportHealthExport(_package(tmp_path / "partial.zip", None, anchor)),
        )
        assert health_lab.load_data_review(DataReviewSelection()).cases == ()
        _execute(
            health_lab,
            ImportHealthExport(
                _package(tmp_path / "absent.zip", "2024-01-04 12:00:00 +0100", anchor)
            ),
        )
        first = _deletion_case(health_lab)
        _execute(
            health_lab,
            ImportHealthExport(
                _package(tmp_path / "still-absent.zip", "2024-01-05 12:00:00 +0100", anchor)
            ),
        )
        deletion_cases = tuple(
            case
            for case in health_lab.load_data_review(DataReviewSelection()).cases
            if case.kind is DataReviewCaseKind.SUSPECTED_SOURCE_DELETION
        )

    assert len(deletion_cases) == 1
    assert deletion_cases[0].case_id == first.case_id


def test_deletion_decisions_and_reappearance_are_forward_only(tmp_path: Path) -> None:
    missing = _record(60, "2024-01-01 07:00:00 +0100")
    anchor = _record(61, "2024-01-02 07:00:00 +0100")
    config = _config(tmp_path)
    with HealthLab.open(config) as health_lab:
        first = _execute(
            health_lab,
            ImportHealthExport(
                _package(tmp_path / "base.zip", "2024-01-03 12:00:00 +0100", missing, anchor)
            ),
        )
        _execute(
            health_lab,
            ImportHealthExport(
                _package(tmp_path / "absent.zip", "2024-01-04 12:00:00 +0100", anchor)
            ),
        )
        case = _deletion_case(health_lab)
        confirmed = _execute(
            health_lab,
            ResolveDataReviewCase(
                case.case_id,
                SourceDeletionResolution(SourceDeletionVerdict.CONFIRM),
            ),
        )
        assert isinstance(confirmed, WriteDecisionReceipt)
        assert health_lab.load_data_review(DataReviewSelection()).cases == ()
        assert any(
            row[2] == "excluded_source_deletion"
            for row in _resolved_rows(config, confirmed.snapshot_ref)
        )
        reappeared = _execute(
            health_lab,
            ImportHealthExport(
                _package(
                    tmp_path / "reappeared.zip",
                    "2024-01-05 12:00:00 +0100",
                    missing,
                    anchor,
                )
            ),
        )
        assert health_lab.load_data_review(DataReviewSelection()).cases == ()
        assert all(
            row[2] == "included_source" for row in _resolved_rows(config, reappeared.snapshot_ref)
        )
        _execute(
            health_lab,
            ImportHealthExport(
                _package(tmp_path / "absent-again.zip", "2024-01-06 12:00:00 +0100", anchor)
            ),
        )
        second_phase = _deletion_case(health_lab)
        rejected = _execute(
            health_lab,
            ResolveDataReviewCase(
                second_phase.case_id,
                SourceDeletionResolution(SourceDeletionVerdict.REJECT),
            ),
        )
        assert isinstance(rejected, WriteDecisionReceipt)
        assert all(
            row[2] == "included_source" for row in _resolved_rows(config, rejected.snapshot_ref)
        )
        still_absent = _execute(
            health_lab,
            ImportHealthExport(
                _package(tmp_path / "still-absent.zip", "2024-01-07 12:00:00 +0100", anchor)
            ),
        )
        assert health_lab.load_data_review(DataReviewSelection()).cases == ()
        assert all(
            row[2] == "included_source" for row in _resolved_rows(config, still_absent.snapshot_ref)
        )

    assert isinstance(first, ImportReceipt)
    first_manifest = (
        config.synthetic_store / "parquet/snapshots" / str(first.snapshot_ref) / "manifest.json"
    )
    before = hashlib.sha256(first_manifest.read_bytes()).hexdigest()
    with HealthLab.open(config):
        pass
    assert hashlib.sha256(first_manifest.read_bytes()).hexdigest() == before


def test_conflicts_can_be_preferred_split_and_revoked(tmp_path: Path) -> None:
    start = "2024-01-01 07:00:00 +0100"
    config = _config(tmp_path)
    with HealthLab.open(config) as health_lab:
        _execute(
            health_lab,
            ImportHealthExport(
                _package(
                    tmp_path / "conflict.zip",
                    "2024-01-02 12:00:00 +0100",
                    _record(60, start),
                    _record(61, start),
                )
            ),
        )
        conflict = health_lab.load_data_review(DataReviewSelection()).cases[0]
        preferred = _execute(
            health_lab,
            ResolveDataReviewCase(
                conflict.case_id,
                SourceConflictResolution(
                    SourceConflictStrategy.PREFER,
                    preferred_version_id=conflict.candidate_version_ids[-1],
                ),
            ),
        )
        assert isinstance(preferred, WriteDecisionReceipt)
        assert health_lab.load_data_review(DataReviewSelection()).cases == ()
        preferred_rows = _resolved_rows(config, preferred.snapshot_ref)
        assert len(preferred_rows) == 1
        assert preferred_rows[0][1] == str(conflict.candidate_version_ids[-1])
        revoked = _execute(
            health_lab,
            RevokeDataReviewDecision(
                SingleDecisionTarget(preferred.decision_id), "wrong candidate"
            ),
        )
        assert len(_resolved_rows(config, revoked.snapshot_ref)) == 1
        reopened = health_lab.load_data_review(DataReviewSelection()).cases[0]
        split = _execute(
            health_lab,
            ResolveDataReviewCase(
                reopened.case_id,
                SourceConflictResolution(SourceConflictStrategy.SPLIT),
            ),
        )
        assert isinstance(split, WriteDecisionReceipt)
        assert health_lab.load_data_review(DataReviewSelection()).cases == ()
        split_rows = _resolved_rows(config, split.snapshot_ref)
        assert len(split_rows) == 2
        assert len({row[0] for row in split_rows}) == 2
        assert {row[1] for row in split_rows} == {
            str(item) for item in reopened.candidate_version_ids
        }
        _execute(
            health_lab,
            ImportHealthExport(
                _package(
                    tmp_path / "after-split.zip",
                    "2024-01-03 12:00:00 +0100",
                    _record(62, start),
                    _record(63, start),
                )
            ),
        )
        assert health_lab.load_data_review(DataReviewSelection()).cases == ()


def test_a_newer_unambiguous_version_supersedes_conflict_preference(tmp_path: Path) -> None:
    start = "2024-01-01 07:00:00 +0100"
    with HealthLab.open(_config(tmp_path)) as health_lab:
        _execute(
            health_lab,
            ImportHealthExport(
                _package(
                    tmp_path / "a.zip",
                    "2024-01-02 12:00:00 +0100",
                    _record(60, start, sync_id="source-a"),
                )
            ),
        )
        _execute(
            health_lab,
            ImportHealthExport(
                _package(
                    tmp_path / "b.zip",
                    "2024-01-03 12:00:00 +0100",
                    _record(61, start, sync_id="source-b"),
                )
            ),
        )
        conflict = health_lab.load_data_review(DataReviewSelection()).cases[0]
        preferred = _execute(
            health_lab,
            ResolveDataReviewCase(
                conflict.case_id,
                SourceConflictResolution(
                    SourceConflictStrategy.PREFER,
                    preferred_version_id=conflict.candidate_version_ids[0],
                ),
            ),
        )
        assert isinstance(preferred, WriteDecisionReceipt)
        preferred_version = _resolved_rows(_config(tmp_path), preferred.snapshot_ref)[0][1]
        newer = _execute(
            health_lab,
            ImportHealthExport(
                _package(
                    tmp_path / "new.zip",
                    "2024-01-04 12:00:00 +0100",
                    _record(62, start, sync_id="source-a"),
                    _record(63, start, sync_id="source-b"),
                )
            ),
        )
        assert health_lab.load_data_review(DataReviewSelection()).cases == ()
        newer_rows = _resolved_rows(_config(tmp_path), newer.snapshot_ref)
        assert len(newer_rows) == 1
        assert newer_rows[0][1] != preferred_version
        assert newer_rows[0][3] == str(preferred.decision_id)
