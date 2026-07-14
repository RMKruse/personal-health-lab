from __future__ import annotations

import fcntl
import hashlib
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from personal_health_lab.application import (
    DataMode,
    HealthLab,
    HealthLabError,
    ImportHealthExport,
    ImportReceipt,
    ImportStatus,
    OverviewSelection,
    PlanFingerprint,
    RuntimeConfig,
    WriteApprovalStatus,
    WriteNotStarted,
    WriteNotStartedStatus,
)
from personal_health_lab.storage import LocalStore, StoreError

MATRIX_PATH = Path(__file__).with_name("v02_matrix.toml")
REGISTERED_CASES = {
    case_id: "test_v02_import_write_contract"
    for case_id in (
        "V02-A-API-004",
        "V02-A-API-005",
        "V02-A-API-006",
        "V02-A-API-007",
        "V02-A-API-010",
        "V02-A-API-011",
        "V02-A-API-012",
        "V02-A-API-013",
        "V02-A-API-014",
    )
}


def _matrix() -> dict[str, object]:
    with MATRIX_PATH.open("rb") as source:
        return tomllib.load(source)


def _validate_matrix(matrix: dict[str, object]) -> None:
    assert matrix["schema_version"] == 1
    assert matrix["release"] == "v0.2"
    sections = {
        name: matrix.get(name, []) for name in ("contract", "fixture", "fault_point", "case")
    }
    all_ids: list[str] = []
    for rows in sections.values():
        assert isinstance(rows, list)
        all_ids.extend(row["id"] for row in rows)
    assert len(all_ids) == len(set(all_ids)), "duplicate V0.2 matrix ID"

    contract_ids = {row["id"] for row in sections["contract"]}
    fixture_ids = {row["id"] for row in sections["fixture"]}
    fault_point_ids = {row["id"] for row in sections["fault_point"]}
    registered_cases = {row["id"]: row["runner"] for row in sections["case"]}
    assert all(
        len(fixture["sha256"]) == 64 and set(fixture["sha256"]) <= set("0123456789abcdef")
        for fixture in sections["fixture"]
    )
    assert registered_cases == REGISTERED_CASES, "unregistered or unclaimed V0.2 case"
    for case in sections["case"]:
        assert set(case["contracts"]) <= contract_ids, f"invalid contract reference in {case['id']}"
        assert set(case["fixtures"]) <= fixture_ids, f"invalid fixture reference in {case['id']}"
        assert set(case.get("fault_points", ())) <= fault_point_ids, (
            f"invalid fault-point reference in {case['id']}"
        )


def _package(path: Path, value: int = 60) -> Path:
    record = (
        '<Record type="HKQuantityTypeIdentifierRestingHeartRate" sourceName="Test Watch" '
        'sourceVersion="1" device="Test Device" unit="count/min" '
        'creationDate="2024-01-01 07:01:00 +0100" '
        'startDate="2024-01-01 07:00:00 +0100" '
        f'endDate="2024-01-01 07:01:00 +0100" value="{value}"/>'
    )
    export = ZipInfo("apple_health_export/export.xml", date_time=(1980, 1, 1, 0, 0, 0))
    export.compress_type = ZIP_DEFLATED
    with ZipFile(path, "w") as archive:
        archive.writestr(export, f'<?xml version="1.0"?><HealthData>{record}</HealthData>')
    return path


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic-store",
        real_store=tmp_path / "real-store",
    )


def _store_files(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_v02_matrix_is_well_formed_and_fully_registered() -> None:
    _validate_matrix(_matrix())


@pytest.mark.parametrize("case", _matrix()["case"], ids=lambda case: case["id"])
def test_v02_import_write_contract(
    case: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = case["id"]
    config = _config(tmp_path)
    package = _package(tmp_path / "health.zip")
    assert hashlib.sha256(package.read_bytes()).hexdigest() == _matrix()["fixture"][0]["sha256"]
    request = ImportHealthExport(package)

    with HealthLab.open(config) as health_lab:
        before = _store_files(config.active_store)
        plan = health_lab.preview_write(request)

        if case_id in {"V02-A-API-004", "V02-A-API-006"}:
            assert plan.approval.status is WriteApprovalStatus.READY
            assert _store_files(config.active_store) == before
        elif case_id == "V02-A-API-005":
            receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
            assert isinstance(receipt.result, ImportReceipt)
            assert receipt.result.status is ImportStatus.COMMITTED
            assert health_lab.load_overview(OverviewSelection()).snapshot_count == 1
        elif case_id == "V02-A-API-007":
            changed = health_lab.preview_write(
                ImportHealthExport(_package(tmp_path / "changed.zip", 61))
            )
            assert changed.fingerprint != plan.fingerprint
            assert _store_files(config.active_store) == before
        elif case_id == "V02-A-API-010":
            receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
            assert receipt.plan_fingerprint == plan.fingerprint
            fingerprint = str(plan.fingerprint).encode()
            assert all(
                fingerprint not in content for content in _store_files(config.active_store).values()
            )
        elif case_id == "V02-A-API-011":
            blocked = health_lab.preview_write(ImportHealthExport(tmp_path / "missing.zip"))
            assert {status.value for status in WriteApprovalStatus} == {
                "ready",
                "confirmation_required",
                "blocked",
            }
            assert plan.approval.status is WriteApprovalStatus.READY
            assert blocked.approval.status is WriteApprovalStatus.BLOCKED
        elif case_id == "V02-A-API-012":
            blocked_request = ImportHealthExport(tmp_path / "missing.zip")
            blocked_plan = health_lab.preview_write(blocked_request)
            blocked = health_lab.execute_write(
                blocked_request,
                expected_plan=blocked_plan.fingerprint,
            )
            assert isinstance(blocked.result, WriteNotStarted)
            assert blocked.result.status is WriteNotStartedStatus.BLOCKED

            changed = _package(tmp_path / "health.zip", 61)
            stale = health_lab.execute_write(request, expected_plan=plan.fingerprint)
            assert changed == request.package_path
            assert isinstance(stale.result, WriteNotStarted)
            assert stale.result.status is WriteNotStartedStatus.PLAN_CHANGED

            with (config.active_store / ".writer.lock").open("a+b") as writer_lock:
                fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy_plan = health_lab.preview_write(request)
                busy = health_lab.execute_write(request, expected_plan=busy_plan.fingerprint)
            assert isinstance(busy.result, WriteNotStarted)
            assert busy.result.status is WriteNotStartedStatus.STORE_BUSY
        elif case_id == "V02-A-API-013":
            receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
            assert isinstance(receipt.result, ImportReceipt)
            assert {status.value for status in ImportStatus} == {
                "committed",
                "duplicate",
                "rejected",
                "quarantined",
                "store_busy",
            }
        elif case_id == "V02-A-API-014":

            def fail_publish(*args: object, **kwargs: object) -> None:
                raise StoreError("injected technical defect")

            assert _store_files(config.active_store)
            monkeypatch.setattr(LocalStore, "publish_import", fail_publish)
            with pytest.raises(HealthLabError):
                health_lab.execute_write(request, expected_plan=plan.fingerprint)
        else:
            raise AssertionError(f"unhandled V0.2 case: {case_id}")

    assert isinstance(plan.fingerprint, PlanFingerprint)
