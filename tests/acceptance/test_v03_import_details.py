import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from personal_health_lab.application import (
    DataMode,
    HealthLab,
    HealthLabError,
    ImportCanonicalCounts,
    ImportContentCount,
    ImportHealthExport,
    RuntimeConfig,
    UnsupportedContentCategory,
)

_FIXTURE = Path(__file__).parents[1] / "fixtures/v03/unsupported-import-content.xml"


def _package(path: Path, xml: str) -> Path:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    return path


def _import(config: RuntimeConfig, package: Path):
    request = ImportHealthExport(package)
    with HealthLab.open(config) as health_lab:
        receipt = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    return receipt.result


def test_import_details_catalog_unsupported_content_across_reimports(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    xml = _FIXTURE.read_text(encoding="utf-8")
    first = _import(config, _package(tmp_path / "first.zip", xml))
    identical = _import(config, tmp_path / "first.zip")
    cumulative_xml = xml.replace("  <ClinicalRecord/>", "  <ClinicalRecord/>\n  <ClinicalRecord/>")
    cumulative = _import(config, _package(tmp_path / "cumulative.zip", cumulative_xml))

    expected_catalog = (
        ImportContentCount(
            UnsupportedContentCategory.RECORD_TYPE,
            "HKQuantityTypeIdentifierMystery",
            1,
        ),
        ImportContentCount(
            UnsupportedContentCategory.SLEEP_VALUE,
            "HKCategoryValueSleepAnalysisDreaming",
            1,
        ),
        ImportContentCount(
            UnsupportedContentCategory.TOP_LEVEL_ELEMENT,
            "ActivitySummary",
            1,
        ),
        ImportContentCount(
            UnsupportedContentCategory.TOP_LEVEL_ELEMENT,
            "ClinicalRecord",
            1,
        ),
        ImportContentCount(
            UnsupportedContentCategory.UNIT,
            json.dumps(
                ["HKQuantityTypeIdentifierActiveEnergyBurned", "J"],
                separators=(",", ":"),
            ),
            1,
        ),
        ImportContentCount(
            UnsupportedContentCategory.WORKOUT_ACTIVITY_TYPE,
            "HKWorkoutActivityTypeMoonWalking",
            1,
        ),
        ImportContentCount(
            UnsupportedContentCategory.WORKOUT_CHILD,
            "WorkoutEvent",
            2,
        ),
    )

    with HealthLab.open(config) as health_lab:
        first_details = health_lab.load_import_details(first.import_id)
        identical_details = health_lab.load_import_details(identical.import_id)
        cumulative_details = health_lab.load_import_details(cumulative.import_id)

    assert first_details.canonical_counts == ImportCanonicalCounts(1, 1, 1, 1, 1, 0)
    assert first_details.unsupported_content == expected_catalog
    assert identical_details.canonical_counts == ImportCanonicalCounts(1, 0, 1, 1, 1, 0)
    assert identical_details.unsupported_content == expected_catalog
    assert cumulative_details.canonical_counts == ImportCanonicalCounts(1, 0, 1, 1, 2, 0)
    assert cumulative_details.unsupported_content == tuple(
        ImportContentCount(item.category, item.external_identifier, 2)
        if item.category is UnsupportedContentCategory.TOP_LEVEL_ELEMENT
        and item.external_identifier == "ClinicalRecord"
        else item
        for item in expected_catalog
    )


def test_import_details_hide_storage_failures(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    imported = _import(config, _package(tmp_path / "health.zip", _FIXTURE.read_text()))

    with HealthLab.open(config) as health_lab:
        assert health_lab._store is not None
        health_lab._store._metadata.execute("DROP TABLE import_canonical_counts")
        with pytest.raises(HealthLabError, match="Importdetails sind nicht verfügbar"):
            health_lab.load_import_details(imported.import_id)
