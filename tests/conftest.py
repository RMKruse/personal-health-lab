import tomllib
from collections.abc import Callable
from enum import Enum
from inspect import isclass
from pathlib import Path
from typing import Any, get_args, get_type_hints
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from acceptance.v03_matrix_validator import validate_matrix as validate_v03_matrix

import personal_health_lab.application as application
from personal_health_lab.application import HealthLab, WritePlanDetails, WriteRequest, WriteResult

_V02_MATRIX = Path(__file__).parent / "acceptance/v02_matrix.toml"
_V03_MATRIX = Path(__file__).parent / "acceptance/v03_matrix.toml"
_PROHIBITED_ACTIVE_MARKERS = {"skip", "skipif", "xfail", "flaky", "rerun", "reruns"}
_V02_READ_PROJECTION_METHODS = {
    "load_data_review",
    "load_data_review_case",
    "load_context_audit",
    "load_context_records",
    "load_daily_context",
    "load_migration_diagnostics",
    "load_overview",
    "load_plausibility_rules",
    "load_recovery_status",
    "load_workspace_status",
}
_V03_VARIANTS = {
    "ActivityDays",
    "ActivityDerivationPlan",
    "ActivityDerivationReceipt",
    "ActivitySettings",
    "CreateActivityDerivationVersion",
    "ContextAudit",
    "ContextRecords",
    "DailyContext",
    "ImportDetails",
    "MedicationAudit",
    "MedicationDays",
    "MedicationPlan",
    "MedicationRevisionPlan",
    "MedicationRevisionReceipt",
    "NoChangeStatus",
    "SleepDays",
    "WeightNutrition",
    "Workouts",
    "WriteNoChange",
}
_V03_VARIANTS |= {
    "ReviseContextCoverageStart",
    "ReviseIllnessCategory",
    "ReviseCustomContextLabel",
    "ReviseIllnessPeriod",
    "ReviseDailyStress",
    "ReviseCustomContextPeriod",
    "ReviseMedicationRegime",
    "ReviseMedicationDeviation",
    "ReviseAsNeededIntake",
    "ReviseIntakeReasonCategory",
    "ManualContextRevisionPlan",
    "ManualContextRevisionReceipt",
    "WorkoutCorrection",
    "LocalWorkoutExclusion",
}
_V03_VARIANTS |= {
    name
    for name in application.__all__
    if name.endswith(("Create", "Revise", "Withdraw", "Restore"))
    and name.startswith(
        (
            "ContextCoverageStart", "IllnessCategory", "CustomContextLabel",
            "IllnessPeriod", "DailyStress", "CustomContextPeriod", "MedicationRegime",
            "MedicationDeviation", "AsNeededIntake", "IntakeReasonCategory",
        )
    )
}
_PUBLIC_ADAPTER_VARIANTS = {
    variant.__name__
    for union in (WriteRequest, WritePlanDetails, WriteResult)
    for variant in get_args(union)
}
_PUBLIC_ADAPTER_VARIANTS |= {
    get_type_hints(getattr(HealthLab, method))["return"].__name__
    for method in _V02_READ_PROJECTION_METHODS
}
_PUBLIC_ADAPTER_VARIANTS |= {
    name
    for name in application.__all__
    if (
        isclass(value := getattr(application, name))
        and (
            (
                issubclass(value, Enum)
                and ("Status" in name or "Reason" in name or name == "DataReviewAction")
            )
            or issubclass(value, Exception)
        )
    )
}
_V02_ADAPTER_VARIANTS = _PUBLIC_ADAPTER_VARIANTS - _V03_VARIANTS
_V02_ADAPTER_VARIANTS |= {
    "AsNeededIntakePlan",
    "AsNeededIntakeReceipt",
    "IllnessRevisionPlan",
    "IntakeReasonCategoryPlan",
    "IntakeReasonCategoryReceipt",
    "MedicationDeviationPlan",
    "MedicationDeviationReceipt",
    "MedicationRegimePlan",
    "MedicationRegimeReceipt",
    "NoChangeStatus",
    "WriteNoChange",
}
_ACTIVE_V02_NODES: set[str] = set()
_ACTIVE_V03_NODES: set[str] = set()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "v02_adapter(side, *variants): behavioral evidence for public adapter variants",
    )
    config.addinivalue_line(
        "markers",
        "v03_adapter(side, *variants): behavioral evidence for V0.3 adapter variants",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if any(not argument.startswith("-") for argument in config.invocation_params.args):
        return
    with _V02_MATRIX.open("rb") as source:
        cases = tomllib.load(source)["case"]
    runners = {case["runner"] for case in cases}
    with _V03_MATRIX.open("rb") as source:
        v03_matrix = tomllib.load(source)
    v03_runners = {case["runner"] for case in v03_matrix["case"]}
    collected: dict[str, set[str]] = {}
    parity = {"cli": set(), "streamlit": set()}
    v03_parity = {"cli": set(), "streamlit": set()}
    for item in items:
        runner = getattr(item, "originalname", None) or item.name.split("[", 1)[0]
        node_id = item.nodeid.split("[", 1)[0]
        collected.setdefault(runner, set()).add(node_id)
        if runner in runners:
            _ACTIVE_V02_NODES.add(item.nodeid)
            prohibited = _PROHIBITED_ACTIVE_MARKERS & {
                marker.name for marker in item.iter_markers()
            }
            if prohibited:
                raise pytest.UsageError(
                    f"active V0.2 runner {runner} uses prohibited markers: {sorted(prohibited)}"
                )
        if node_id in v03_runners:
            _ACTIVE_V03_NODES.add(item.nodeid)
            prohibited = _PROHIBITED_ACTIVE_MARKERS & {
                marker.name for marker in item.iter_markers()
            }
            if prohibited:
                raise pytest.UsageError(
                    f"active V0.3 runner {node_id} uses prohibited markers: {sorted(prohibited)}"
                )
        for marker in item.iter_markers("v02_adapter"):
            side, *variants = marker.args
            if side not in parity:
                raise pytest.UsageError(f"unknown V0.2 adapter side: {side}")
            parity[side].update(variants)
        for marker in item.iter_markers("v03_adapter"):
            side, *variants = marker.args
            if side not in v03_parity:
                raise pytest.UsageError(f"unknown V0.3 adapter side: {side}")
            v03_parity[side].update(variants)

    missing = runners - collected.keys()
    duplicate = {
        runner: nodes for runner, nodes in collected.items() if runner in runners and len(nodes) > 1
    }
    if missing or duplicate:
        raise pytest.UsageError(
            f"invalid V0.2 runner registry; missing={sorted(missing)}, duplicate={duplicate}"
        )
    try:
        validate_v03_matrix(
            v03_matrix,
            root=Path(__file__).parents[1],
            collected_node_ids=[item.nodeid for item in items],
        )
    except AssertionError as error:
        raise pytest.UsageError(str(error)) from error
    for side, variants in parity.items():
        if variants != _V02_ADAPTER_VARIANTS:
            raise pytest.UsageError(
                f"incomplete {side} V0.2 adapter parity; "
                f"missing={sorted(_V02_ADAPTER_VARIANTS - variants)}, "
                f"unexpected={sorted(variants - _V02_ADAPTER_VARIANTS)}"
            )
    for side, variants in v03_parity.items():
        if variants != _V03_VARIANTS:
            raise pytest.UsageError(
                f"incomplete {side} V0.3 adapter parity; "
                f"missing={sorted(_V03_VARIANTS - variants)}, "
                f"unexpected={sorted(variants - _V03_VARIANTS)}"
            )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    outcome = yield
    report = outcome.get_result()
    if item.nodeid in _ACTIVE_V02_NODES | _ACTIVE_V03_NODES and report.skipped:
        report.outcome = "failed"
        report.longrepr = f"active matrix runner skipped during {report.when}: {item.nodeid}"


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
