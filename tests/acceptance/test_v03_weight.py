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

_NUTRITION_TYPES = (
    ("Biotin", "dietary_biotin"),
    ("Caffeine", "dietary_caffeine"),
    ("Calcium", "dietary_calcium"),
    ("Carbohydrates", "dietary_carbohydrates"),
    ("Chloride", "dietary_chloride"),
    ("Cholesterol", "dietary_cholesterol"),
    ("Chromium", "dietary_chromium"),
    ("Copper", "dietary_copper"),
    ("EnergyConsumed", "dietary_energy_consumed"),
    ("FatMonounsaturated", "dietary_fat_monounsaturated"),
    ("FatPolyunsaturated", "dietary_fat_polyunsaturated"),
    ("FatSaturated", "dietary_fat_saturated"),
    ("FatTotal", "dietary_fat_total"),
    ("Fiber", "dietary_fiber"),
    ("Folate", "dietary_folate"),
    ("Iodine", "dietary_iodine"),
    ("Iron", "dietary_iron"),
    ("Magnesium", "dietary_magnesium"),
    ("Manganese", "dietary_manganese"),
    ("Molybdenum", "dietary_molybdenum"),
    ("Niacin", "dietary_niacin"),
    ("PantothenicAcid", "dietary_pantothenic_acid"),
    ("Phosphorus", "dietary_phosphorus"),
    ("Potassium", "dietary_potassium"),
    ("Protein", "dietary_protein"),
    ("Riboflavin", "dietary_riboflavin"),
    ("Selenium", "dietary_selenium"),
    ("Sodium", "dietary_sodium"),
    ("Sugar", "dietary_sugar"),
    ("Thiamin", "dietary_thiamin"),
    ("VitaminA", "dietary_vitamin_a"),
    ("VitaminB12", "dietary_vitamin_b12"),
    ("VitaminB6", "dietary_vitamin_b6"),
    ("VitaminC", "dietary_vitamin_c"),
    ("VitaminD", "dietary_vitamin_d"),
    ("VitaminE", "dietary_vitamin_e"),
    ("VitaminK", "dietary_vitamin_k"),
    ("Water", "dietary_water"),
    ("Zinc", "dietary_zinc"),
)


def _package(path: Path, xml: str) -> Path:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    return path


def _import(health_lab: HealthLab, package: Path) -> None:
    request = ImportHealthExport(package)
    health_lab.execute_write(request, expected_plan=health_lab.preview_write(request).fingerprint)


def _nutrition_xml(
    *,
    export_date: str = "2024-01-10 12:00:00 +0100",
    negative_carbohydrates: bool = False,
    negative_protein: bool = False,
    protein_mg: str = "1000",
    zero_sugar: bool = False,
) -> str:
    records = []
    for suffix, _ in _NUTRITION_TYPES:
        units = (
            (("kcal", "1"), ("kJ", "4.184"))
            if suffix == "EnergyConsumed"
            else (("mL", "1"), ("L", "0.001"))
            if suffix == "Water"
            else (("g", "1"), ("mg", "1000"), ("mcg", "1000000"))
        )
        day = {
            "EnergyConsumed": 1,
            "Protein": 1,
            "Carbohydrates": 2,
            "FatTotal": 3,
        }.get(suffix, 4)
        for unit_index, (unit, value) in enumerate(units):
            hour = 20 + unit_index
            end = (
                "2024-01-02 01:00:00 +0100"
                if suffix == "EnergyConsumed"
                else f"2024-01-{day:02d} {hour:02d}:00:00 +0100"
            )
            if negative_protein and suffix == "Protein" and unit == "g":
                value = "-1"
            if negative_carbohydrates and suffix == "Carbohydrates" and unit == "g":
                value = "-1"
            if suffix == "Protein" and unit == "mg":
                value = protein_mg
            if zero_sugar and suffix == "Sugar" and unit == "g":
                value = "0"
            records.append(
                f'<Record type="HKQuantityTypeIdentifierDietary{suffix}" '
                f'sourceName="Food" sourceVersion="1" device="Nutrition App" unit="{unit}" '
                f'value="{value}" creationDate="2024-01-{day:02d} {hour:02d}:01:00 +0100" '
                f'startDate="2024-01-{day:02d} {hour:02d}:00:00 +0100" endDate="{end}">'
                f'<MetadataEntry key="HKMetadataKeySyncIdentifier" value="{suffix}-{unit}"/>'
                "</Record>"
            )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><HealthData>'
        f'<ExportDate value="{export_date}"/>' + "".join(records) + "</HealthData>"
    )


def _nutrition_fixture_bundle(*, seed: int, options: dict[str, object]) -> str:
    variants = options.get("variants")
    assert isinstance(variants, list) and all(isinstance(item, str) for item in variants)
    builders = {
        "baseline": _nutrition_xml,
        "later-protein-version": lambda: _nutrition_xml(
            export_date="2024-02-10 12:00:00 +0100", protein_mg="2000"
        ),
        "negative-protein": lambda: _nutrition_xml(negative_protein=True),
        "zero-sugar": lambda: _nutrition_xml(zero_sugar=True),
        "negative-protein-carbohydrates": lambda: _nutrition_xml(
            negative_carbohydrates=True, negative_protein=True
        ),
    }
    return "\n".join(
        (f"seed={seed}", *(f"variant={variant}\n{builders[variant]()}" for variant in variants))
    )


def _restore_rule_constraints(
    config: RuntimeConfig,
    *,
    schema_version: int,
    data_types: tuple[CanonicalHealthType, ...],
    units: tuple[CanonicalUnit, ...],
) -> None:
    current_types = ", ".join(f"'{item.value}'" for item in CanonicalHealthType)
    current_units = ", ".join(f"'{item.value}'" for item in CanonicalUnit)
    old_types = ", ".join(f"'{item.value}'" for item in data_types)
    old_units = ", ".join(f"'{item.value}'" for item in units)
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        sqlite_schema_version = int(metadata.execute("PRAGMA schema_version").fetchone()[0])
        metadata.execute("PRAGMA writable_schema = ON")
        metadata.execute(
            "UPDATE sqlite_schema SET sql = replace(replace(sql, ?, ?), ?, ?) WHERE name = ?",
            (
                f"data_type IN ({current_types})",
                f"data_type IN ({old_types})",
                f"canonical_unit IN ({current_units})",
                f"canonical_unit IN ({old_units})",
                "plausibility_rule_versions",
            ),
        )
        if schema_version == 6:
            metadata.execute(
                "UPDATE sqlite_schema SET sql = replace(sql, ?, ?) WHERE name = ?",
                (
                    "'source_conflict', 'preferred_daily_weight_conflict', 'direct_correction'",
                    "'source_conflict', 'direct_correction'",
                    "data_review_decisions",
                ),
            )
        metadata.execute(
            "UPDATE store_identity SET schema_version = ? WHERE singleton = 1",
            (schema_version,),
        )
        metadata.execute("PRAGMA writable_schema = OFF")
        metadata.execute(f"PRAGMA schema_version = {sqlite_schema_version + 1}")
        rule_sql = str(
            metadata.execute(
                "SELECT sql FROM sqlite_schema WHERE name = 'plausibility_rule_versions'"
            ).fetchone()[0]
        )
        assert f"data_type IN ({old_types})" in rule_sql
        assert f"canonical_unit IN ({old_units})" in rule_sql


def _restore_schema_6_weight_constraints(config: RuntimeConfig) -> None:
    _restore_rule_constraints(
        config,
        schema_version=6,
        data_types=(
            CanonicalHealthType.ACTIVE_ENERGY,
            CanonicalHealthType.APPLE_RESTING_HEART_RATE,
        ),
        units=(CanonicalUnit.KILOCALORIE, CanonicalUnit.BEATS_PER_MINUTE),
    )


def _restore_schema_7_weight_constraints(config: RuntimeConfig) -> None:
    _restore_rule_constraints(
        config,
        schema_version=7,
        data_types=(
            CanonicalHealthType.ACTIVE_ENERGY,
            CanonicalHealthType.APPLE_RESTING_HEART_RATE,
            CanonicalHealthType.BODY_MASS,
        ),
        units=(
            CanonicalUnit.KILOCALORIE,
            CanonicalUnit.BEATS_PER_MINUTE,
            CanonicalUnit.KILOGRAM,
        ),
    )


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


def test_nutrition_imports_every_type_and_unit_and_projects_independent_days(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        _import(health_lab, _package(tmp_path / "nutrition.zip", _nutrition_xml()))
        _import(
            health_lab,
            _package(
                tmp_path / "nutrition-later.zip",
                _nutrition_xml(
                    export_date="2024-02-10 12:00:00 +0100",
                    protein_mg="2000",
                ),
            ),
        )
        projection = health_lab.load_weight_nutrition(
            SnapshotDateSelection(start_date=date(2024, 1, 1), end_date=date(2024, 1, 4))
        )

    measurements = projection.healthkit_nutrition_samples
    assert len(measurements) == 116
    assert sum(item.is_selected for item in measurements) == 115
    assert {item.data_type.value for item in measurements} == {
        canonical_type for _, canonical_type in _NUTRITION_TYPES
    }
    assert all(
        item.value == pytest.approx(1)
        for item in measurements
        if not (
            item.data_type is CanonicalHealthType.DIETARY_PROTEIN and item.original_unit == "mg"
        )
    )
    assert all(item.measurement_version_id for item in measurements)
    assert all(
        item.original_unit in {"kcal", "kJ", "mL", "L", "g", "mg", "mcg"} for item in measurements
    )
    assert all(item.source_name == "Food" and item.source_version == "1" for item in measurements)
    assert all(item.device == "Nutrition App" for item in measurements)
    versioned_protein = tuple(
        item
        for item in measurements
        if item.data_type is CanonicalHealthType.DIETARY_PROTEIN and item.original_unit == "mg"
    )
    assert len(versioned_protein) == 2
    assert len({item.logical_measurement_id for item in versioned_protein}) == 1
    assert len({item.measurement_version_id for item in versioned_protein}) == 2
    assert {item.original_value for item in versioned_protein} == {1000, 2000}
    selected_protein = next(item for item in versioned_protein if item.is_selected)
    assert selected_protein.value == pytest.approx(2)
    assert selected_protein.effective_value == pytest.approx(2)

    assert [item.day for item in projection.nutrition_days] == [
        date(2024, 1, 1),
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
    ]
    first, second, third, fourth = projection.nutrition_days
    assert first.energy.value == pytest.approx(2)
    assert first.protein.value == pytest.approx(4)
    assert first.carbohydrates.value is None
    assert first.total_fat.value is None
    assert second.energy.value is None
    assert second.carbohydrates.value == pytest.approx(3)
    assert third.total_fat.value == pytest.approx(3)
    assert fourth.energy.value is None
    assert fourth.protein.value is None
    assert fourth.carbohydrates.value is None
    assert fourth.total_fat.value is None
    assert len(first.energy.measurement_version_ids) == 2
    assert all(
        item.measurement_local_day == date(2024, 1, 1)
        for item in measurements
        if item.data_type.value == "dietary_energy_consumed"
    )


def test_negative_nutrition_opens_one_source_review_without_derived_recheck(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        nutrition_rules = tuple(
            item
            for item in health_lab.load_plausibility_rules().rules
            if item.data_type.value.startswith("dietary_")
        )
        assert len(nutrition_rules) == 39
        assert all(rule.versions == () for rule in nutrition_rules)
        assert all(
            rule.recommendation.specification.fixed_lower_bound == 0 for rule in nutrition_rules
        )
        assert all(
            rule.recommendation.specification.fixed_upper_bound is None for rule in nutrition_rules
        )
        assert all(
            not rule.recommendation.specification.personal_range_enabled for rule in nutrition_rules
        )
        protein_rule = next(
            item
            for item in nutrition_rules
            if item.data_type is CanonicalHealthType.DIETARY_PROTEIN
        )
        adopt = CreatePlausibilityRuleVersion(
            protein_rule.data_type,
            protein_rule.recommendation.specification,
            None,
            protein_rule.recommendation.recommendation_id,
        )
        health_lab.execute_write(adopt, expected_plan=health_lab.preview_write(adopt).fingerprint)
        _import(
            health_lab,
            _package(
                tmp_path / "negative-nutrition.zip",
                _nutrition_xml(negative_protein=True),
            ),
        )
        review = health_lab.load_data_review(DataReviewSelection(DataReviewCaseKind.PLAUSIBILITY))
        projection = health_lab.load_weight_nutrition(
            SnapshotDateSelection(start_date=date(2024, 1, 1), end_date=date(2024, 1, 1))
        )

    assert len(review.cases) == 1
    assert projection.nutrition_days[0].protein.value == pytest.approx(1)
    assert projection.nutrition_days[0].protein.quality_status is DataQualityStatus.PROVISIONAL
    assert projection.nutrition_days[0].protein.review_case_ids == (review.cases[0].case_id,)


def test_zero_nutrition_value_is_a_valid_observed_boundary(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        sugar_rule = next(
            item
            for item in health_lab.load_plausibility_rules().rules
            if item.data_type is CanonicalHealthType.DIETARY_SUGAR
        )
        adopt = CreatePlausibilityRuleVersion(
            sugar_rule.data_type,
            sugar_rule.recommendation.specification,
            None,
            sugar_rule.recommendation.recommendation_id,
        )
        health_lab.execute_write(adopt, expected_plan=health_lab.preview_write(adopt).fingerprint)
        _import(
            health_lab,
            _package(tmp_path / "zero-nutrition.zip", _nutrition_xml(zero_sugar=True)),
        )
        projection = health_lab.load_weight_nutrition(
            SnapshotDateSelection(start_date=date(2024, 1, 4), end_date=date(2024, 1, 4))
        )
        review = health_lab.load_data_review(DataReviewSelection(DataReviewCaseKind.PLAUSIBILITY))

    zero = next(
        item
        for item in projection.healthkit_nutrition_samples
        if item.data_type is CanonicalHealthType.DIETARY_SUGAR and item.original_unit == "g"
    )
    assert zero.value == zero.effective_value == 0
    assert review.cases == ()


def test_nutrition_correction_and_exclusion_preserve_source_samples(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        rules = health_lab.load_plausibility_rules().rules
        for data_type in (
            CanonicalHealthType.DIETARY_PROTEIN,
            CanonicalHealthType.DIETARY_CARBOHYDRATES,
        ):
            rule = next(item for item in rules if item.data_type is data_type)
            adopt = CreatePlausibilityRuleVersion(
                data_type,
                rule.recommendation.specification,
                None,
                rule.recommendation.recommendation_id,
            )
            health_lab.execute_write(
                adopt, expected_plan=health_lab.preview_write(adopt).fingerprint
            )
        _import(
            health_lab,
            _package(
                tmp_path / "negative-nutrition.zip",
                _nutrition_xml(negative_carbohydrates=True, negative_protein=True),
            ),
        )
        before = health_lab.load_weight_nutrition(
            SnapshotDateSelection(start_date=date(2024, 1, 1), end_date=date(2024, 1, 2))
        )
        cases = health_lab.load_data_review(
            DataReviewSelection(DataReviewCaseKind.PLAUSIBILITY)
        ).cases
        negative_samples = {
            item.data_type: item
            for item in before.healthkit_nutrition_samples
            if item.original_unit == "g" and item.original_value == -1
        }
        case_by_type = {
            data_type: next(
                case
                for case in cases
                if case.measurement_version_id == sample.measurement_version_id
            )
            for data_type, sample in negative_samples.items()
        }
        protein = negative_samples[CanonicalHealthType.DIETARY_PROTEIN]
        correction = ResolveDataReviewCase(
            case_by_type[CanonicalHealthType.DIETARY_PROTEIN].case_id,
            DataCorrection(
                protein.measurement_version_id,
                2,
                CanonicalUnit.GRAM,
                "Proteinwert korrigiert",
            ),
        )
        health_lab.execute_write(
            correction, expected_plan=health_lab.preview_write(correction).fingerprint
        )
        carbohydrates = negative_samples[CanonicalHealthType.DIETARY_CARBOHYDRATES]
        exclusion = ResolveDataReviewCase(
            case_by_type[CanonicalHealthType.DIETARY_CARBOHYDRATES].case_id,
            LocalMeasurementExclusion(
                carbohydrates.measurement_version_id,
                "Kohlenhydratwert ausgeschlossen",
            ),
        )
        health_lab.execute_write(
            exclusion, expected_plan=health_lab.preview_write(exclusion).fingerprint
        )
        after = health_lab.load_weight_nutrition(
            SnapshotDateSelection(start_date=date(2024, 1, 1), end_date=date(2024, 1, 2))
        )

    corrected = next(
        item
        for item in after.healthkit_nutrition_samples
        if item.measurement_version_id == protein.measurement_version_id
    )
    excluded = next(
        item
        for item in after.healthkit_nutrition_samples
        if item.measurement_version_id == carbohydrates.measurement_version_id
    )
    assert corrected.original_value == -1
    assert corrected.effective_value == 2
    assert corrected.disposition == "included_correction"
    assert excluded.original_value == -1
    assert excluded.effective_value is None
    assert excluded.disposition == "excluded_local"
    assert after.nutrition_days[0].protein.value == pytest.approx(4)
    assert after.nutrition_days[1].carbohydrates.value == pytest.approx(2)


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
        health_lab.execute_write(adopt, expected_plan=health_lab.preview_write(adopt).fingerprint)
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
        health_lab.execute_write(adopt, expected_plan=health_lab.preview_write(adopt).fingerprint)
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


def test_schema_7_store_migrates_nutrition_rule_constraints(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config):
        pass
    _restore_schema_7_weight_constraints(config)

    with HealthLab.open(config) as health_lab:
        assert health_lab.load_workspace_status().state is WorkspaceState.MIGRATION_REQUIRED
        migration = MigrateStore()
        health_lab.execute_write(
            migration, expected_plan=health_lab.preview_write(migration).fingerprint
        )

    with HealthLab.open(config) as health_lab:
        protein = next(
            item
            for item in health_lab.load_plausibility_rules().rules
            if item.data_type is CanonicalHealthType.DIETARY_PROTEIN
        )
        adopt = CreatePlausibilityRuleVersion(
            protein.data_type,
            protein.recommendation.specification,
            None,
            protein.recommendation.recommendation_id,
        )
        health_lab.execute_write(adopt, expected_plan=health_lab.preview_write(adopt).fingerprint)
