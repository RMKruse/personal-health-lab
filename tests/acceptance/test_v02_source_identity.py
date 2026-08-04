from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import personal_health_lab.health_import as health_import
from personal_health_lab.application import (
    DataMode,
    DataReviewCaseKind,
    DataReviewSelection,
    HealthLab,
    ImportHealthExport,
    ImportReceipt,
    ImportStatus,
    OverviewSelection,
    RuntimeConfig,
)
from personal_health_lab.health_data import CanonicalHealthType


def _record(
    value: int,
    start: str,
    *,
    sync_id: str | None = None,
    source_version: str = "1",
    creation_date: str | None = None,
) -> str:
    end = start.replace(":00 +0100", ":01 +0100")
    metadata = (
        ""
        if sync_id is None
        else f'<MetadataEntry key="HKMetadataKeySyncIdentifier" value="{sync_id}"/>'
    )
    return (
        '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
        'sourceName="Test Watch" device="Test Device" unit="count/min" '
        f'sourceVersion="{source_version}" creationDate="{creation_date or end}" '
        f'startDate="{start}" endDate="{end}" value="{value}">{metadata}</Record>'
    )


def _package(path: Path, export_date: str | None, *records: str) -> Path:
    date_element = "" if export_date is None else f'<ExportDate value="{export_date}"/>'
    xml = f'<?xml version="1.0"?><HealthData>{date_element}{"".join(records)}</HealthData>'
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    return path


def _config(tmp_path: Path, name: str = "store") -> RuntimeConfig:
    return RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / name,
        real_store=tmp_path / "real-store",
    )


def _execute(health_lab: HealthLab, package: Path) -> ImportReceipt:
    request = ImportHealthExport(package)
    plan = health_lab.preview_write(request)
    result = health_lab.execute_write(request, expected_plan=plan.fingerprint).result
    assert isinstance(result, ImportReceipt)
    return result


def test_package_occurrences_and_measurement_versions_remain_separate(
    tmp_path: Path,
) -> None:
    first_record = _record(60, "2024-01-01 07:00:00 +0100")
    second_record = _record(70, "2024-01-02 07:00:00 +0100")
    first_package = _package(tmp_path / "first.zip", "2024-01-02 12:00:00 +0100", first_record)
    repeated_export = _package(tmp_path / "repeat.zip", "2024-01-03 12:00:00 +0100", first_record)
    additive_export = _package(
        tmp_path / "additive.zip",
        "2024-01-04 12:00:00 +0100",
        first_record,
        second_record,
    )

    with HealthLab.open(_config(tmp_path)) as health_lab:
        first = _execute(health_lab, first_package)
        duplicate = _execute(health_lab, first_package)
        repeat = _execute(health_lab, repeated_export)
        additive = _execute(health_lab, additive_export)

    assert first.status is ImportStatus.COMMITTED
    assert duplicate.status is ImportStatus.DUPLICATE
    assert duplicate.snapshot_ref == first.snapshot_ref
    assert repeat.status is ImportStatus.COMMITTED
    assert repeat.source_occurrence_count == 2
    assert repeat.logical_measurement_count == repeat.measurement_version_count == 1
    assert additive.source_occurrence_count == 4
    assert additive.logical_measurement_count == additive.measurement_version_count == 2
    with sqlite3.connect(tmp_path / "store" / "metadata.sqlite3") as metadata:
        assert metadata.execute("SELECT count(*) FROM audit_events").fetchone() == (3,)


def test_only_the_newest_ordered_export_governs_source_versions(tmp_path: Path) -> None:
    start = "2024-01-01 07:00:00 +0100"
    packages = (
        _package(
            tmp_path / "base.zip",
            "2024-01-02 12:00:00 +0100",
            _record(60, start),
        ),
        _package(
            tmp_path / "newer.zip",
            "2024-01-04 12:00:00 +0100",
            _record(64, start),
        ),
        _package(
            tmp_path / "older-late.zip",
            "2024-01-03 12:00:00 +0100",
            _record(62, start),
        ),
        _package(tmp_path / "unordered.zip", None, _record(80, start)),
    )

    with HealthLab.open(_config(tmp_path)) as health_lab:
        receipts = tuple(_execute(health_lab, package) for package in packages)
        overview = health_lab.load_overview(OverviewSelection())
        review = health_lab.load_data_review(DataReviewSelection())

    assert all(receipt.status is ImportStatus.COMMITTED for receipt in receipts)
    resting = next(
        series
        for series in overview.daily_series
        if series.data_type is CanonicalHealthType.APPLE_RESTING_HEART_RATE
    )
    assert resting.values[0].value == 64
    assert review.cases == ()


def test_equal_dated_exports_resolve_identically_in_either_import_order(
    tmp_path: Path,
) -> None:
    start = "2024-01-01 07:00:00 +0100"
    first = _package(
        tmp_path / "equal-first.zip",
        "2024-01-02 12:00:00 +0100",
        _record(60, start),
    )
    second = _package(
        tmp_path / "equal-second.zip",
        "2024-01-02 12:00:00 +0100",
        _record(64, start),
    )

    values: list[float] = []
    for store_name, packages in (
        ("forward", (first, second)),
        ("reverse", (second, first)),
    ):
        with HealthLab.open(_config(tmp_path, store_name)) as health_lab:
            for package in packages:
                _execute(health_lab, package)
            overview = health_lab.load_overview(OverviewSelection())
        resting = next(
            series
            for series in overview.daily_series
            if series.data_type is CanonicalHealthType.APPLE_RESTING_HEART_RATE
        )
        values.append(resting.values[0].value)

    assert values[0] == values[1]


def test_strong_and_natural_identity_create_only_payload_versions(tmp_path: Path) -> None:
    with HealthLab.open(_config(tmp_path, "strong-store")) as health_lab:
        _execute(
            health_lab,
            _package(
                tmp_path / "strong-1.zip",
                "2024-01-02 12:00:00 +0100",
                _record(60, "2024-01-01 07:00:00 +0100", sync_id="source-42"),
            ),
        )
        strong_changed = _execute(
            health_lab,
            _package(
                tmp_path / "strong-2.zip",
                "2024-01-03 12:00:00 +0100",
                _record(61, "2024-01-01 08:00:00 +0100", sync_id="source-42"),
            ),
        )

    assert strong_changed.logical_measurement_count == 1
    assert strong_changed.measurement_version_count == 2

    start = "2024-01-01 07:00:00 +0100"
    with HealthLab.open(_config(tmp_path, "natural-store")) as health_lab:
        _execute(
            health_lab,
            _package(
                tmp_path / "natural-1.zip",
                "2024-01-02 12:00:00 +0100",
                _record(60, start),
            ),
        )
        natural_changed = _execute(
            health_lab,
            _package(
                tmp_path / "natural-2.zip",
                "2024-01-03 12:00:00 +0100",
                _record(61, start),
            ),
        )
        provenance_only = _execute(
            health_lab,
            _package(
                tmp_path / "natural-3.zip",
                "2024-01-04 12:00:00 +0100",
                _record(
                    61,
                    start,
                    source_version="2",
                    creation_date="2024-01-02 09:00:00 +0100",
                ),
            ),
        )

    assert natural_changed.logical_measurement_count == 1
    assert natural_changed.measurement_version_count == 2
    assert provenance_only.source_occurrence_count == 3
    assert provenance_only.measurement_version_count == 3


def test_pre_payload_v2_snapshot_does_not_gain_a_spurious_unchanged_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_version_ids = health_import._measurement_version_ids

    def legacy_version_ids(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
        _, legacy = original_version_ids(*args, **kwargs)
        return legacy, legacy

    start = "2024-01-01 07:00:00 +0100"
    config = _config(tmp_path, "payload-transition-store")
    monkeypatch.setattr(health_import, "_measurement_version_ids", legacy_version_ids)
    with HealthLab.open(config) as health_lab:
        _execute(
            health_lab,
            _package(
                tmp_path / "legacy-payload.zip",
                "2024-01-02 12:00:00 +0100",
                _record(60, start),
            ),
        )
    monkeypatch.setattr(health_import, "_measurement_version_ids", original_version_ids)

    with HealthLab.open(config) as health_lab:
        unchanged = _execute(
            health_lab,
            _package(
                tmp_path / "current-unchanged.zip",
                "2024-01-03 12:00:00 +0100",
                _record(60, start),
            ),
        )
        source_revision = _execute(
            health_lab,
            _package(
                tmp_path / "current-source-revision.zip",
                "2024-01-04 12:00:00 +0100",
                _record(
                    60,
                    start,
                    source_version="2",
                    creation_date="2024-01-02 09:00:00 +0100",
                ),
            ),
        )

    assert unchanged.measurement_version_count == 1
    assert source_revision.measurement_version_count == 2


def test_identity_collision_is_visible_in_the_public_data_review_projection(
    tmp_path: Path,
) -> None:
    start = "2024-01-01 07:00:00 +0100"
    collision = _package(
        tmp_path / "collision.zip",
        "2024-01-02 12:00:00 +0100",
        _record(60, start),
        _record(61, start),
    )

    with HealthLab.open(_config(tmp_path)) as health_lab:
        receipt = _execute(health_lab, collision)
        review = health_lab.load_data_review(DataReviewSelection())

    assert receipt.status is ImportStatus.COMMITTED
    assert len(review.cases) == 1
    assert review.cases[0].kind is DataReviewCaseKind.SOURCE_CONFLICT
    assert review.cases[0].logical_measurement_id is not None

    strong_collision_a = _package(
        tmp_path / "strong-collision-a.zip",
        "2024-01-02 12:00:00 +0100",
        _record(60, start, sync_id="source-a"),
    )
    strong_collision_b = _package(
        tmp_path / "strong-collision-b.zip",
        "2024-01-03 12:00:00 +0100",
        _record(61, start, sync_id="source-b"),
    )
    with HealthLab.open(_config(tmp_path, "strong-collision-store")) as health_lab:
        _execute(health_lab, strong_collision_a)
        _execute(health_lab, strong_collision_b)
        strong_review = health_lab.load_data_review(DataReviewSelection())

    assert len(strong_review.cases) == 1
    assert strong_review.cases[0].kind is DataReviewCaseKind.SOURCE_CONFLICT
