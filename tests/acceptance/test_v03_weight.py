import sqlite3
from datetime import date, timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from personal_health_lab.application import (
    CanonicalHealthType,
    CanonicalUnit,
    CreatePlausibilityRuleVersion,
    DataCorrection,
    DataMode,
    DataQualityStatus,
    DataReviewCaseKind,
    DataReviewSelection,
    HealthLab,
    ImportHealthExport,
    LocalMeasurementExclusion,
    MigrateStore,
    ResolveDataReviewCase,
    RuntimeConfig,
    SnapshotDateSelection,
    WeightDayStatus,
    WorkspaceState,
)

_FIXTURE = Path(__file__).parents[1] / "fixtures/v03/weight-edges.xml"


def _package(path: Path, xml: str) -> Path:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    return path


def _import(health_lab: HealthLab, package: Path) -> None:
    request = ImportHealthExport(package)
    health_lab.execute_write(request, expected_plan=health_lab.preview_write(request).fingerprint)


def _restore_schema_6_weight_constraints(config: RuntimeConfig) -> None:
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        schema_version = int(metadata.execute("PRAGMA schema_version").fetchone()[0])
        metadata.execute("PRAGMA writable_schema = ON")
        metadata.execute(
            "UPDATE sqlite_schema SET sql = replace(replace(sql, ?, ?), ?, ?) WHERE name = ?",
            (
                "'active_energy', 'apple_resting_heart_rate', 'body_mass')",
                "'active_energy', 'apple_resting_heart_rate')",
                "canonical_unit IN ('kcal', 'count/min', 'kg'))",
                "canonical_unit IN ('kcal', 'count/min'))",
                "plausibility_rule_versions",
            ),
        )
        metadata.execute(
            "UPDATE sqlite_schema SET sql = replace(sql, ?, ?) WHERE name = ?",
            (
                "'source_conflict', 'preferred_daily_weight_conflict', 'direct_correction'",
                "'source_conflict', 'direct_correction'",
                "data_review_decisions",
            ),
        )
        metadata.execute("UPDATE store_identity SET schema_version = 6 WHERE singleton = 1")
        metadata.execute("PRAGMA writable_schema = OFF")
        metadata.execute(f"PRAGMA schema_version = {schema_version + 1}")


def test_weight_import_projects_units_versions_missingness_and_daily_preference(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    first_xml = _FIXTURE.read_text(encoding="utf-8")
    later_xml = first_xml.replace(
        '<ExportDate value="2024-01-10 12:00:00 +0100"/>',
        '<ExportDate value="2024-02-10 12:00:00 +0100"/>',
    ).replace('unit="kg" value="70"', 'unit="kg" value="71"', 1)
    source_revision_xml = later_xml.replace('sourceVersion="1"', 'sourceVersion="2"', 1).replace(
        'creationDate="2024-01-01 07:01:00 +0100"',
        'creationDate="2024-01-01 07:02:00 +0100"',
        1,
    )

    with HealthLab.open(config) as health_lab:
        _import(health_lab, _package(tmp_path / "first.zip", first_xml))
        _import(health_lab, _package(tmp_path / "later.zip", later_xml))
        _import(health_lab, _package(tmp_path / "source-revision.zip", source_revision_xml))
        projection = health_lab.load_weight_nutrition(
            SnapshotDateSelection(start_date=date(2024, 1, 1), end_date=date(2024, 1, 6))
        )
        review = health_lab.load_data_review(
            DataReviewSelection(DataReviewCaseKind.PREFERRED_DAILY_WEIGHT_CONFLICT)
        )

    assert projection.snapshot_ref is not None
    assert projection.status is DataQualityStatus.PROVISIONAL
    assert tuple(day.day for day in projection.days) == tuple(
        date(2024, 1, 1) + timedelta(days=offset) for offset in range(6)
    )
    assert [day.status for day in projection.days] == [
        WeightDayStatus.OBSERVED,
        WeightDayStatus.MISSING,
        WeightDayStatus.OBSERVED,
        WeightDayStatus.OBSERVED,
        WeightDayStatus.AMBIGUOUS,
        WeightDayStatus.MISSING,
    ]
    assert projection.days[0].value_kg == 71
    assert projection.days[1].value_kg is None
    assert projection.days[2].value_kg == pytest.approx(45.359237)
    assert projection.days[3].value_kg == 80
    assert len(projection.days[3].measurement_version_ids) == 2
    assert projection.days[4].value_kg is None
    assert projection.days[4].review_case_ids == tuple(case.case_id for case in review.cases)
    assert len(review.cases) == 1

    versioned = [
        measurement
        for measurement in projection.weight_measurements
        if measurement.source_name == "Scale"
        and measurement.source_start.date() == date(2024, 1, 1)
    ]
    assert len(versioned) == 3
    assert len({measurement.logical_measurement_id for measurement in versioned}) == 1
    assert len({measurement.measurement_version_id for measurement in versioned}) == 3
    assert sorted((item.original_value, item.original_unit) for item in versioned) == [
        (70, "kg"),
        (71, "kg"),
        (71, "kg"),
    ]
    selected_version = next(item for item in versioned if item.is_selected)
    assert selected_version.original_value == 71
    assert selected_version.source_version == "2"
    assert selected_version.source_updated_at.minute == 2

    grams = next(item for item in projection.weight_measurements if item.original_unit == "g")
    pounds = next(item for item in projection.weight_measurements if item.original_unit == "lb")
    assert grams.value_kg == 70
    assert pounds.value_kg == pytest.approx(45.359237)
    assert grams.source_start.utcoffset() == timedelta(hours=-5)
    assert grams.source_name == "Scale"
    assert grams.source_version == "1"
    assert grams.device == "Scale A"


def test_weight_correction_resolves_an_ambiguous_preferred_daily_weight(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        _import(
            health_lab,
            _package(tmp_path / "weights.zip", _FIXTURE.read_text(encoding="utf-8")),
        )
        conflict = health_lab.load_data_review(
            DataReviewSelection(DataReviewCaseKind.PREFERRED_DAILY_WEIGHT_CONFLICT)
        ).cases[0]
        assert conflict.measurement_version_id is not None
        request = ResolveDataReviewCase(
            conflict.case_id,
            DataCorrection(
                conflict.measurement_version_id,
                82,
                CanonicalUnit.KILOGRAM,
                "Waage falsch abgelesen",
            ),
        )
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        projection = health_lab.load_weight_nutrition(
            SnapshotDateSelection(start_date=date(2024, 1, 5), end_date=date(2024, 1, 5))
        )
        review = health_lab.load_data_review(
            DataReviewSelection(DataReviewCaseKind.PREFERRED_DAILY_WEIGHT_CONFLICT)
        )

    assert review.cases == ()
    assert projection.days[0].status is WeightDayStatus.OBSERVED
    assert projection.days[0].value_kg == 82
    assert len(projection.days[0].measurement_version_ids) == 2
    corrected = next(
        item
        for item in projection.weight_measurements
        if item.measurement_version_id == conflict.measurement_version_id
    )
    assert corrected.original_value == 81
    assert corrected.effective_value_kg == 82
    assert corrected.disposition == "included_correction"


def test_weight_exclusion_resolves_an_ambiguous_preferred_daily_weight(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        _import(
            health_lab,
            _package(tmp_path / "weights.zip", _FIXTURE.read_text(encoding="utf-8")),
        )
        conflict = health_lab.load_data_review(
            DataReviewSelection(DataReviewCaseKind.PREFERRED_DAILY_WEIGHT_CONFLICT)
        ).cases[0]
        assert conflict.measurement_version_id is not None
        request = ResolveDataReviewCase(
            conflict.case_id,
            LocalMeasurementExclusion(
                conflict.measurement_version_id,
                "Doppelte Fehlmessung",
            ),
        )
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        projection = health_lab.load_weight_nutrition(
            SnapshotDateSelection(start_date=date(2024, 1, 5), end_date=date(2024, 1, 5))
        )

    assert projection.days[0].status is WeightDayStatus.OBSERVED
    assert projection.days[0].value_kg == 82
    excluded = next(
        item
        for item in projection.weight_measurements
        if item.measurement_version_id == conflict.measurement_version_id
    )
    assert excluded.original_value == 81
    assert excluded.effective_value_kg is None
    assert excluded.disposition == "excluded_local"


def test_weight_plausibility_recommendation_is_adopted_through_the_v02_workflow(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    xml = _FIXTURE.read_text(encoding="utf-8").replace(
        'unit="kg" value="70"', 'unit="kg" value="0.5"', 1
    )
    with HealthLab.open(config) as health_lab:
        rule = next(
            item
            for item in health_lab.load_plausibility_rules().rules
            if item.data_type is CanonicalHealthType.BODY_MASS
        )
        assert rule.versions == ()
        assert rule.recommendation.specification.unit is CanonicalUnit.KILOGRAM
        assert rule.recommendation.specification.fixed_lower_bound == 1
        assert rule.recommendation.specification.fixed_upper_bound is None
        assert rule.recommendation.specification.personal_range_enabled
        adopt = CreatePlausibilityRuleVersion(
            CanonicalHealthType.BODY_MASS,
            rule.recommendation.specification,
            None,
            rule.recommendation.recommendation_id,
        )
        health_lab.execute_write(
            adopt, expected_plan=health_lab.preview_write(adopt).fingerprint
        )
        _import(health_lab, _package(tmp_path / "implausible.zip", xml))
        review = health_lab.load_data_review(DataReviewSelection(DataReviewCaseKind.PLAUSIBILITY))

    assert len(review.cases) == 1


def test_schema_6_store_migrates_weight_rule_and_review_constraints(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config):
        pass
    _restore_schema_6_weight_constraints(config)

    with HealthLab.open(config) as health_lab:
        assert health_lab.load_workspace_status().state is WorkspaceState.MIGRATION_REQUIRED
        migration = MigrateStore()
        health_lab.execute_write(
            migration, expected_plan=health_lab.preview_write(migration).fingerprint
        )

    with HealthLab.open(config) as health_lab:
        rule = next(
            item
            for item in health_lab.load_plausibility_rules().rules
            if item.data_type is CanonicalHealthType.BODY_MASS
        )
        adopt = CreatePlausibilityRuleVersion(
            CanonicalHealthType.BODY_MASS,
            rule.recommendation.specification,
            None,
            rule.recommendation.recommendation_id,
        )
        health_lab.execute_write(
            adopt, expected_plan=health_lab.preview_write(adopt).fingerprint
        )
        _import(
            health_lab,
            _package(tmp_path / "weights.zip", _FIXTURE.read_text(encoding="utf-8")),
        )
        conflict = health_lab.load_data_review(
            DataReviewSelection(DataReviewCaseKind.PREFERRED_DAILY_WEIGHT_CONFLICT)
        ).cases[0]
        assert conflict.measurement_version_id is not None
        resolution = ResolveDataReviewCase(
            conflict.case_id,
            LocalMeasurementExclusion(conflict.measurement_version_id, "Fehlmessung"),
        )
        health_lab.execute_write(
            resolution,
            expected_plan=health_lab.preview_write(resolution).fingerprint,
        )
