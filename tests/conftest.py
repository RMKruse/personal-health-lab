from collections.abc import Callable
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest


@pytest.fixture
def source_conflict_package() -> Callable[[Path], Path]:
    def build(path: Path) -> Path:
        records = "".join(
            '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
            'sourceName="Test Watch" device="Test Device" unit="count/min" '
            'sourceVersion="1" creationDate="2024-01-01 07:01:00 +0100" '
            'startDate="2024-01-01 07:00:00 +0100" '
            f'endDate="2024-01-01 07:01:00 +0100" value="{value}"/>'
            for value in (60, 61)
        )
        xml = (
            '<?xml version="1.0"?><HealthData>'
            '<ExportDate value="2024-01-02 12:00:00 +0100"/>'
            f"{records}</HealthData>"
        )
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("apple_health_export/export.xml", xml)
        return path

    return build


@pytest.fixture
def personal_range_package() -> Callable[[Path], Path]:
    def build(path: Path) -> Path:
        records = "".join(
            '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
            'sourceName="Test Watch" device="Test Device" unit="count/min" '
            'sourceVersion="1" '
            f'creationDate="2024-01-{index + 1:02d} 07:00:00 +0000" '
            f'startDate="2024-01-{index + 1:02d} 07:00:00 +0000" '
            f'endDate="2024-01-{index + 1:02d} 07:00:00 +0000" value="{value}">'
            f'<MetadataEntry key="HKMetadataKeySyncIdentifier" value="hr-{index}"/>'
            "</Record>"
            for index, value in enumerate([60, 61] * 14 + [64])
        )
        xml = (
            '<?xml version="1.0"?><HealthData>'
            '<ExportDate value="2024-02-01 12:00:00 +0000"/>'
            f"{records}</HealthData>"
        )
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("apple_health_export/export.xml", xml)
        return path

    return build
