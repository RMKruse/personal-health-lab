import hashlib
import importlib.util
import json
import tomllib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
import test_v02_metadata_restore as restore
import test_v02_migration as v02_migration
import test_v03_activity as activity_tests
import test_v03_context as context
import test_v03_medication as medication_tests
import test_v03_migration as migration
import test_v03_weight as weight
import test_v03_workouts as workout_tests

from personal_health_lab.application import (
    AsNeededMedication,
    ConfigurationError,
    ContextCoverageStartCreate,
    DataMode,
    HealthLab,
    IllnessCategoryCreate,
    IllnessPeriodCreate,
    IllnessSeverity,
    ImportHealthExport,
    MedicationPlanEntryId,
    MedicationRegimeCreate,
    MigrateStore,
    OverviewSelection,
    ReviseContextCoverageStart,
    ReviseIllnessCategory,
    ReviseIllnessPeriod,
    ReviseMedicationRegime,
    RuntimeConfig,
    SnapshotDateSelection,
)
from personal_health_lab.synthetic_export import generate_export

_CATALOG_PATH = Path(__file__).parents[2] / "scripts/v03_fixture_catalog.py"
_CATALOG_SPEC = importlib.util.spec_from_file_location("v03_fixture_catalog", _CATALOG_PATH)
assert _CATALOG_SPEC is not None and _CATALOG_SPEC.loader is not None
_CATALOG = importlib.util.module_from_spec(_CATALOG_SPEC)
_CATALOG_SPEC.loader.exec_module(_CATALOG)
fixture_recipe = _CATALOG.fixture_recipe
_MATRIX_PATH = Path(__file__).with_name("v03_matrix.toml")


def _fixture(fixture_id: str) -> dict[str, object]:
    matrix = tomllib.loads(_MATRIX_PATH.read_text(encoding="utf-8"))
    row = next(item for item in matrix["fixture"] if item["id"] == fixture_id)
    generated = fixture_recipe(seed=row["seed"], options=row["options"])
    assert hashlib.sha256(generated.encode()).hexdigest() == row["sha256"]
    payload = json.loads(generated)
    assert payload["seed"] == row["seed"]
    assert payload["version"] == row["options"]["version"]
    return payload


def _package(path: Path, xml: str) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    return path


def _combined_xml(documents: list[str]) -> str:
    root = ElementTree.Element("HealthData")
    ElementTree.SubElement(root, "ExportDate", value="2024-01-10 12:00:00 +0100")
    for document in documents:
        for child in ElementTree.fromstring(document):
            if child.tag != "ExportDate":
                root.append(child)
    return ElementTree.tostring(root, encoding="unicode")


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")


def test_core_journey_fixture_is_consumed_by_the_import_path(tmp_path: Path) -> None:
    payload = _fixture("V03-F-CORE-JOURNEY-V1")
    recipe = payload["recipe"]
    assert isinstance(recipe, dict)
    seed = int(payload["seed"])
    activity_recipe = _fixture("V03-F-ACTIVITY-COVERAGE-EDGES-V1")["recipe"]
    assert isinstance(activity_recipe, dict)
    artifacts = recipe["artifacts"]
    assert isinstance(artifacts, list)
    artifact_paths = []
    for artifact in artifacts:
        assert isinstance(artifact, dict)
        artifact_path = Path(__file__).parents[2] / str(artifact["path"])
        assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == artifact["sha256"]
        artifact_paths.append(artifact_path)
    assert hashlib.sha256(str(activity_recipe["xml"]).encode()).hexdigest() == recipe[
        "activity_sha256"
    ]
    nutrition_source = Path(__file__).with_name("test_v03_weight.py")
    assert hashlib.sha256(nutrition_source.read_bytes()).hexdigest() == recipe[
        "nutrition_source_sha256"
    ]
    documents = [
        *(path.read_text(encoding="utf-8") for path in artifact_paths),
        str(recipe["sleep_xml"]),
        str(activity_recipe["xml"]),
        weight._nutrition_xml(),
    ]
    initial = _package(tmp_path / f"golden-{seed}.zip", _combined_xml(documents))
    cumulative = _package(
        tmp_path / f"golden-{seed + 1}.zip",
        _combined_xml(
            [
                *documents,
                """<HealthData><Record type="HKQuantityTypeIdentifierStepCount"
                unit="count" value="10" sourceName="Apple Watch" sourceVersion="1"
                device="Apple Watch" creationDate="2024-01-09 12:01:00 +0100"
                startDate="2024-01-09 12:00:00 +0100"
                endDate="2024-01-09 12:01:00 +0100"/></HealthData>""",
            ]
        ),
    )
    sources = [initial, initial, cumulative]
    with HealthLab.open(_config(tmp_path)) as health_lab:
        statuses = []
        receipts = []
        for source in sources:
            request = ImportHealthExport(source)
            result = health_lab.execute_write(
                request, expected_plan=health_lab.preview_write(request).fingerprint
            )
            receipts.append(result.result)
            statuses.append(result.result.status.value)
        activity_projection = health_lab.load_activity_days(SnapshotDateSelection())
        core_projection = health_lab.load_weight_nutrition(SnapshotDateSelection())
        sleep_projection = health_lab.load_sleep_days(SnapshotDateSelection())
        workout_projection = health_lab.load_workouts(SnapshotDateSelection())
        details = health_lab.load_import_details(receipts[0].import_id)
    assert statuses[0:2] == ["committed", "duplicate"]
    assert statuses[-1] == "committed"
    assert activity_projection.measurements
    assert {item.data_type.value for item in activity_projection.measurements} == {
        "active_energy",
        "apple_exercise_time",
        "step_count",
        "walking_running_distance",
    }
    assert {item.source_class.value for item in activity_projection.measurements} == {
        "iphone",
        "other",
        "unknown",
        "watch",
    }
    assert core_projection.weight_measurements
    assert len(sleep_projection.accepted_intervals) + len(sleep_projection.rejected_intervals) == 6
    assert {item.original_activity_type for item in workout_projection.workouts} == {
        "HKWorkoutActivityTypeRunning",
        "HKWorkoutActivityTypeYoga",
        "com.example.unknown",
    }
    assert len({sample.data_type for sample in core_projection.healthkit_nutrition_samples}) == 39
    assert details.unsupported_content


def test_activity_coverage_fixture_is_consumed_by_the_import_path(tmp_path: Path) -> None:
    payload = _fixture("V03-F-ACTIVITY-COVERAGE-EDGES-V1")
    recipe = payload["recipe"]
    assert isinstance(recipe, dict)
    request = ImportHealthExport(
        _package(tmp_path / f"activity-{payload['seed']}.zip", str(recipe["xml"]))
    )
    with HealthLab.open(_config(tmp_path)) as health_lab:
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        activity = health_lab.load_activity_days(SnapshotDateSelection())
    assert {"Apple Watch", "iPhone"} <= {item.source_name for item in activity.measurements}
    assert activity.days[0].step_count.value == 3
    assert activity.days[0].is_complete is False
    assert [segment.kind.value for segment in activity.days[0].coverage_segments] == [
        "unobserved",
        "watch",
        "unobserved",
        "iphone_fallback",
        "unobserved",
        "watch",
        "unobserved",
    ]
    suppression_reasons = [
        item.suppression_reason for item in activity.measurements if item.suppression_reason
    ]
    assert suppression_reasons.count("iphone_outside_watch_gap") == 1
    assert suppression_reasons.count("ineligible_source") == 3
    workflows = {
        "all-metrics-units-boundary": (
            activity_tests.test_activity_import_preserves_typed_samples_and_assigns_cross_midnight_value_to_start_day
        ),
        "same-metric-overlap": (
            activity_tests.test_same_metric_same_source_class_overlap_opens_a_review_case
        ),
        "point-boundary": activity_tests.test_point_activity_sample_does_not_open_an_overlap_review,
        "negative-value": (
            activity_tests.test_negative_activity_value_remains_canonical_and_opens_plausibility_review
        ),
        "watch-coverage-thresholds": (
            activity_tests.test_activity_days_use_shared_watch_coverage_and_whole_interval_iphone_fallback
        ),
        "derivation-version": (
            activity_tests.test_activity_derivation_threshold_is_versioned_and_keeps_old_snapshot_readable
        ),
        "workout-overlap-types": (
            workout_tests.test_workouts_keep_types_durations_and_overlap_status
        ),
        "workout-correction": workout_tests.test_workout_correction_persists_through_data_review,
    }
    for name in recipe["workflows"]:
        root = tmp_path / str(name)
        root.mkdir()
        workflows[str(name)](root)


def test_context_workflow_fixture_drives_a_typed_revision(tmp_path: Path) -> None:
    payload = _fixture("V03-F-CONTEXT-WORKFLOW-V1")
    recipe = payload["recipe"]
    assert isinstance(recipe, dict)
    fixture = generate_export("null-v1", int(payload["seed"]), tmp_path / "fixture")
    with HealthLab.open(_config(tmp_path)) as health_lab:
        imported = ImportHealthExport(fixture.export_path)
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        request = ReviseContextCoverageStart(
            ContextCoverageStartCreate(date.fromisoformat(str(recipe["coverage_start"])))
        )
        receipt = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        category_request = ReviseIllnessCategory(IllnessCategoryCreate(str(recipe["category"])))
        category = health_lab.execute_write(
            category_request,
            expected_plan=health_lab.preview_write(category_request).fingerprint,
        ).result
        period_request = ReviseIllnessPeriod(
            IllnessPeriodCreate(
                category.logical_id,
                date.fromisoformat(str(recipe["coverage_start"])),
                date.fromisoformat(str(recipe["coverage_start"])),
                IllnessSeverity.MILD,
            )
        )
        health_lab.execute_write(
            period_request, expected_plan=health_lab.preview_write(period_request).fingerprint
        )
        overlap = health_lab.preview_write(
            ReviseIllnessPeriod(
                IllnessPeriodCreate(
                    category.logical_id,
                    date.fromisoformat(str(recipe["coverage_start"])),
                    None,
                    IllnessSeverity.MODERATE,
                )
            )
        )
        records = health_lab.load_context_records()
    assert receipt.result.status.value == "committed"
    assert records.illness_categories[0].name == recipe["category"]
    assert overlap.approval.status.value == "blocked"
    workflows = {
        "coverage": (
            context.test_context_coverage_start_publishes_an_immutable_snapshot_and_baseline
        ),
        "illness-collision": (
            context.test_illness_periods_project_active_categories_and_reject_same_category_overlap
        ),
        "stress-custom": (
            context.test_daily_stress_and_custom_contexts_share_the_revision_snapshot_contract
        ),
        "catalog-lifecycle": (
            context.test_context_catalog_revision_withdrawal_and_restore_are_audited
        ),
    }
    for name in recipe["workflows"]:
        root = tmp_path / str(name)
        root.mkdir()
        workflows[str(name)](root)


def test_medication_workflow_fixture_drives_a_typed_revision(tmp_path: Path) -> None:
    payload = _fixture("V03-F-MEDICATION-WORKFLOW-V1")
    recipe = payload["recipe"]
    assert isinstance(recipe, dict)
    fixture = generate_export("null-v1", int(payload["seed"]), tmp_path / "fixture")
    entry_id = MedicationPlanEntryId(str(recipe["entry_id"]))
    as_needed = AsNeededMedication(
        str(recipe["medication"]),
        Decimal(str(recipe["amount"])),
        str(recipe["unit"]),
        entry_id=entry_id,
    )
    with HealthLab.open(_config(tmp_path)) as health_lab:
        imported = ImportHealthExport(fixture.export_path)
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        request = ReviseMedicationRegime(
            MedicationRegimeCreate(
                datetime.fromisoformat(str(recipe["starts_at"])),
                str(recipe["timezone"]),
                (),
                (as_needed,),
            )
        )
        receipt = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        plan = health_lab.load_medication_plan()
    assert receipt.result.status.value == "committed"
    assert plan.regimes[0].as_needed_medications[0].entry_id == entry_id
    with pytest.raises(ConfigurationError):
        ReviseMedicationRegime(
            MedicationRegimeCreate(
                datetime.fromisoformat(str(recipe["starts_at"])),
                str(recipe["timezone"]),
                (),
                (as_needed, as_needed),
            )
        )
    workflows = {
        "regime-dst": (
            medication_tests.test_medication_regime_projects_dst_and_keeps_old_snapshot_stable
        ),
        "deviation": (
            medication_tests.test_medication_deviation_binds_one_occurrence_and_keeps_old_snapshot_stable
        ),
        "as-needed-catalog": (
            medication_tests.test_as_needed_intakes_and_reason_categories_are_snapshot_bound
        ),
        "snapshot-binding": (
            medication_tests.test_import_carries_every_effective_manual_revision_binding_forward
        ),
    }
    for name in recipe["workflows"]:
        root = tmp_path / str(name)
        root.mkdir()
        workflows[str(name)](root)


def test_persistence_lifecycle_fixture_pins_the_target_version(tmp_path: Path) -> None:
    payload = _fixture("V03-F-PERSISTENCE-LIFECYCLE-V1")
    recipe = payload["recipe"]
    assert isinstance(recipe, dict)
    fixture = generate_export("null-v1", int(payload["seed"]), tmp_path / "fixture")
    config = migration._config(tmp_path / "store")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    migration._set_schema_versions(config, store=11, snapshot=6)
    with HealthLab.open(config) as health_lab:
        request = MigrateStore()
        receipt = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    assert receipt.result.snapshot_steps == ((6, int(str(recipe["snapshot_schema_version"]))),)
    workflows = {
        "v02-v03-migration": migration.test_v02_store_migrates_to_one_complete_v03_snapshot,
        "rollback": v02_migration.test_direct_migration_rollback_restores_backup_and_old_snapshot,
        "blocked-late-rollback": (
            v02_migration.test_later_successful_state_change_blocks_migration_rollback
        ),
        "backup-restore-rebind": (
            restore.test_v03_restore_preserves_manual_revisions_and_rebinds_one_new_snapshot
        ),
    }
    for name in recipe["workflows"]:
        root = tmp_path / str(name)
        root.mkdir()
        workflows[str(name)](root)


def test_security_boundary_fixture_is_rejected_without_publication(tmp_path: Path) -> None:
    payload = _fixture("V03-F-SECURITY-BOUNDARIES-V1")
    recipe = payload["recipe"]
    assert isinstance(recipe, dict)
    request = ImportHealthExport(
        _package(tmp_path / f"attack-{payload['seed']}.zip", str(recipe["xml"]))
    )
    with HealthLab.open(_config(tmp_path)) as health_lab:
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        assert receipt.result.status.value == "rejected"
        assert health_lab.load_overview(OverviewSelection()).snapshot_count == 0
