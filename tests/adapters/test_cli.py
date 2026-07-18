import fcntl
import json
import platform
import subprocess
import sys
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from zipfile import ZipFile

import pytest
from jsonschema import Draft202012Validator

from personal_health_lab.adapters.cli import main
from personal_health_lab.application import DataMode, HealthLab, ImportHealthExport, RuntimeConfig
from personal_health_lab.synthetic_export import GenerationOptions, generate_export

_EXPECTED_RUNTIME_CONFIG = {
    "max_import_compression_ratio": 200.0,
    "max_import_entries": 8,
    "max_import_entry_bytes": 512 * 1024 * 1024,
    "max_import_package_bytes": 512 * 1024 * 1024,
    "max_import_uncompressed_bytes": 512 * 1024 * 1024,
    "mode": "synthetic",
    "real_store": "<redacted>",
    "schema_version": "1.0",
    "synthetic_store": "<redacted>",
}


def _assert_json_contract(output: object) -> None:
    assert isinstance(output, dict)
    schema = json.loads(
        files("personal_health_lab.adapters.cli")
        .joinpath(f"schemas/output-{output['schema_version']}.schema.json")
        .read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(output)


def _execute_json_import(
    common_args: list[str],
    package: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, object]]:
    assert main([*common_args, "import", str(package), "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(plan)
    assert plan["kind"] == "write_plan"
    exit_code = main(
        [
            *common_args,
            "import",
            str(package),
            "--json",
            "--execute",
            "--expect-plan",
            plan["fingerprint"],
        ]
    )
    receipt = json.loads(capsys.readouterr().out)
    _assert_json_contract(receipt)
    assert receipt["kind"] == "write_receipt"
    assert receipt["plan_fingerprint"] == plan["fingerprint"]
    return exit_code, receipt


def _execute_json_analysis(
    common_args: list[str],
    capsys: pytest.CaptureFixture[str],
    *analysis_args: str,
) -> tuple[int, dict[str, object]]:
    assert main([*common_args, "analyze", *analysis_args, "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(plan)
    assert plan["details"]["type"] == "run_resting_heart_rate_analysis"
    exit_code = main(
        [
            *common_args,
            "analyze",
            *analysis_args,
            "--json",
            "--execute",
            "--expect-plan",
            plan["fingerprint"],
        ]
    )
    receipt = json.loads(capsys.readouterr().out)
    _assert_json_contract(receipt)
    assert receipt["kind"] == "write_receipt"
    return exit_code, receipt


def test_cli_maps_metadata_backup_without_exposing_its_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = [
        "--mode",
        "real",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    target = tmp_path / "private" / "metadata.sqlite3"
    target.parent.mkdir()
    with HealthLab.open(
        RuntimeConfig(DataMode.REAL, tmp_path / "synthetic", tmp_path / "real")
    ):
        pass
    args = [*common, "backup", str(target), "--json"]
    assert main(args) == 0
    plan_text = capsys.readouterr().out
    plan = json.loads(plan_text)
    assert plan["details"]["type"] == "create_metadata_backup"
    assert plan["request"]["target"] == "<redacted>"
    assert str(tmp_path) not in plan_text

    assert main([*args, "--execute", "--expect-plan", plan["fingerprint"]]) == 0
    receipt_text = capsys.readouterr().out
    receipt = json.loads(receipt_text)
    assert receipt["result"]["status"] == "completed"
    assert receipt["result"]["target_file"] == "metadata.sqlite3"
    assert str(tmp_path) not in receipt_text


def test_cli_projects_source_conflicts_from_the_shared_data_review(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    source_conflict_package: Callable[[Path], Path],
) -> None:
    config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic",
        real_store=tmp_path / "real",
    )
    package = source_conflict_package(tmp_path / "conflict.zip")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert (
        main(
            [
                "--mode",
                "synthetic",
                "--synthetic-store",
                str(config.synthetic_store),
                "--real-store",
                str(config.real_store),
                "review",
                "--json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    _assert_json_contract(output)
    assert output["cases"][0]["kind"] == "source_conflict"
    assert output["cases"][0]["logical_measurement_id"] is not None
    assert output["cases"][0]["allowed_actions"] == ["prefer", "split"]
    assert len(output["cases"][0]["candidate_version_ids"]) == 2
    assert output["status"] == "provisional"
    assert output["cycles"][0]["status"] == "open"
    assert output["cases"][0]["detail"]["reasons"] == []


def test_cli_maps_data_review_plan_receipt_and_revocation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    source_conflict_package: Callable[[Path], Path],
) -> None:
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    package = source_conflict_package(tmp_path / "conflict.zip")
    assert _execute_json_import(common, package, capsys)[0] == 0
    assert main([*common, "review", "--json"]) == 0
    case = json.loads(capsys.readouterr().out)["cases"][0]
    args = [
        *common,
        "review-resolve",
        case["case_id"],
        "--conflict-strategy",
        "prefer",
        "--preferred-version",
        case["candidate_version_ids"][0],
        "--json",
    ]
    assert main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(plan)
    assert plan["details"]["type"] == "data_review_decision"
    assert main([*args, "--execute", "--expect-plan", plan["fingerprint"]]) == 0
    receipt = json.loads(capsys.readouterr().out)
    _assert_json_contract(receipt)
    decision_id = receipt["result"]["decision_id"]

    revoke = [
        *common,
        "review-revoke",
        decision_id,
        "--reason",
        "test",
        "--json",
    ]
    assert main(revoke) == 0
    plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(plan)
    assert main([*revoke, "--execute", "--expect-plan", plan["fingerprint"]]) == 0
    _assert_json_contract(json.loads(capsys.readouterr().out))


def test_json_cli_keeps_the_complete_batch_and_reports_plan_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    records = "".join(
        '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
        'sourceName="Test Watch" device="Test Device" unit="count/min" '
        'sourceVersion="1" creationDate="2024-01-01 07:00:00 +0000" '
        'startDate="2024-01-01 07:00:00 +0000" '
        f'endDate="2024-01-01 07:00:00 +0000" value="{251 + index}">'
        f'<MetadataEntry key="HKMetadataKeySyncIdentifier" value="batch-{index}"/>'
        "</Record>"
        for index in range(100)
    )
    package = tmp_path / "large-batch.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            '<?xml version="1.0"?><HealthData>'
            '<ExportDate value="2024-02-01 12:00:00 +0000"/>'
            f"{records}</HealthData>",
        )
    assert _execute_json_import(common, package, capsys)[0] == 0

    batch = [
        *common,
        "review-confirm-batch",
        "--kind",
        "plausibility",
        "--json",
    ]
    assert main(batch) == 0
    batch_plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(batch_plan)
    assert batch_plan["details"]["count"] == 100
    assert len(batch_plan["details"]["matches"]) == 100

    single = [
        *common,
        "review-resolve",
        batch_plan["details"]["matches"][0]["case_id"],
        "--confirm",
        "--json",
    ]
    assert main(single) == 0
    single_plan = json.loads(capsys.readouterr().out)
    assert main([*single, "--execute", "--expect-plan", single_plan["fingerprint"]]) == 0
    capsys.readouterr()

    assert main([*batch, "--execute", "--expect-plan", batch_plan["fingerprint"]]) == 3
    changed = json.loads(capsys.readouterr().out)
    _assert_json_contract(changed)
    assert changed["result"]["status"] == "plan_changed"


def test_human_batch_cli_displays_every_match_before_confirmation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    package = tmp_path / "plausibility.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            '<?xml version="1.0"?><HealthData>'
            '<ExportDate value="2024-02-01 12:00:00 +0000"/>'
            '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
            'sourceName="Test Watch" device="Test Device" unit="count/min" '
            'sourceVersion="1" creationDate="2024-01-01 07:00:00 +0000" '
            'startDate="2024-01-01 07:00:00 +0000" '
            'endDate="2024-01-01 07:00:00 +0000" value="251">'
            '<MetadataEntry key="HKMetadataKeySyncIdentifier" value="batch-human"/>'
            "</Record></HealthData>",
        )
    assert _execute_json_import(common, package, capsys)[0] == 0
    displayed: list[str] = []
    monkeypatch.setattr("personal_health_lab.adapters.cli._cli.pydoc.pager", displayed.append)

    def decline(_prompt: str) -> str:
        assert displayed
        return "n"

    monkeypatch.setattr("builtins.input", decline)
    assert main([*common, "review-confirm-batch", "--kind", "plausibility"]) == 0
    assert displayed[0]


def test_cli_maps_correction_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    personal_range_package: Callable[[Path], Path],
) -> None:
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    assert _execute_json_import(
        common, personal_range_package(tmp_path / "personal.zip"), capsys
    )[0] == 0
    assert main([*common, "review", "--json"]) == 0
    case = json.loads(capsys.readouterr().out)["cases"][0]
    args = [
        *common,
        "review-resolve",
        case["case_id"],
        "--correct",
        "61",
        "--unit",
        "count/min",
        "--measurement-version",
        case["measurement_version_id"],
        "--reason",
        "falscher Wert",
        "--json",
    ]
    assert main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    assert main([*args, "--execute", "--expect-plan", plan["fingerprint"]]) == 0
    _assert_json_contract(json.loads(capsys.readouterr().out))


def test_cli_projects_the_personal_range_finding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    personal_range_package: Callable[[Path], Path],
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    package = personal_range_package(tmp_path / "personal.zip")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert (
        main(
            [
                "--mode",
                "synthetic",
                "--synthetic-store",
                str(config.synthetic_store),
                "--real-store",
                str(config.real_store),
                "review",
                "--json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    _assert_json_contract(output)
    reason = output["cases"][0]["detail"]["reasons"][0]
    assert reason["code"] == "above_personal_upper_bound"
    assert reason["lower_bound"] < 64 < reason["upper_bound"] + 1


def test_cli_maps_the_pinned_historical_review_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        plan = health_lab.preview_write(request)
        imported = health_lab.execute_write(request, expected_plan=plan.fingerprint).result

    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
        "historical-review",
        "apple_resting_heart_rate",
        "--start-date",
        "2024-01-01",
        "--end-date",
        "2024-01-31",
        "--json",
    ]
    assert main(common) == 0
    output = json.loads(capsys.readouterr().out)

    _assert_json_contract(output)
    assert output["details"]["type"] == "run_historical_review"
    assert output["details"]["base_snapshot_ref"] == str(imported.snapshot_ref)
    assert output["details"]["rule_version_id"] == "fixed-plausibility/v1"

    assert main([*common, "--execute", "--expect-plan", output["fingerprint"]]) == 0
    receipt = json.loads(capsys.readouterr().out)
    _assert_json_contract(receipt)
    assert receipt["result"]["type"] == "run_historical_review"


def test_real_json_import_renders_shared_confirmation_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    assert (
        main(
            [
                "--mode",
                "real",
                "--synthetic-store",
                str(tmp_path / "synthetic"),
                "--real-store",
                str(tmp_path / "real"),
                "import",
                str(fixture.export_path),
                "--json",
            ]
        )
        == 0
    )
    plan = json.loads(capsys.readouterr().out)

    _assert_json_contract(plan)
    assert plan["approval"]["status"] == "confirmation_required"
    assert plan["confirmations"] == ["real_import_same_person", "filevault_unknown"]
    assert plan["preflight"]["filevault"] == {
        "status": "unknown",
        "target_volume": "unresolved",
        "reason": "unsupported_platform",
    }
    capacity = plan["preflight"]["capacity"]
    assert capacity["status"] == "ready"
    assert capacity["method_id"] == "full-snapshot-import/v1"
    assert capacity["target_volume"].startswith("volume-")
    assert str(tmp_path) not in json.dumps(capacity)
    assert plan["workspace"]["mode"] == "real"
    assert plan["workspace"]["person_binding"] == "unbound"
    assert len(plan["workspace"]["store_id"]) == 32
    assert plan["workspace"]["allowed_writes"] == [
        "import_health_export",
        "resolve_data_review_case",
        "confirm_data_review_batch",
        "revoke_data_review_decision",
            "create_plausibility_rule_version",
            "run_historical_review",
            "run_resting_heart_rate_analysis",
            "create_metadata_backup",
        ]


def test_cli_projects_and_creates_plausibility_rule_versions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    assert main([*common, "rules", "--json"]) == 0
    rules = json.loads(capsys.readouterr().out)
    _assert_json_contract(rules)
    assert len(rules["rules"]) == 2

    args = [
        *common,
        "rule",
        "apple_resting_heart_rate",
        "--unit",
        "count/min",
        "--lower",
        "30",
        "--upper",
        "200",
        "--effective-from",
        "2024-02-12T00:00:00+01:00",
        "--json",
    ]
    assert main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(plan)
    assert plan["details"]["type"] == "create_plausibility_rule_version"
    assert main([*args, "--execute", "--expect-plan", plan["fingerprint"]]) == 0
    receipt = json.loads(capsys.readouterr().out)
    _assert_json_contract(receipt)
    assert receipt["result"]["status"] == "committed"


def test_cli_returns_expected_incomplete_for_rejected_import(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "unsafe.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr("apple_health_export/export.xml", "<HealthData/>")
        archive.writestr("../private-health-value", "181")

    exit_code, output = _execute_json_import(
        [
            "--mode",
            "synthetic",
            "--synthetic-store",
            str(tmp_path / "synthetic"),
            "--real-store",
            str(tmp_path / "real"),
        ],
        package,
        capsys,
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert output["result"]["status"] == "rejected"
    assert captured.err == ""


def test_cli_returns_expected_incomplete_while_store_is_busy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common_args = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    assert main([*common_args, "overview"]) == 0
    capsys.readouterr()
    package = tmp_path / "health.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr("apple_health_export/export.xml", "<HealthData/>")

    with (tmp_path / "synthetic" / ".writer.lock").open("a+b") as writer_lock:
        fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exit_code, import_receipt = _execute_json_import(common_args, package, capsys)
        assert exit_code == 3
        analysis_exit, analysis_receipt = _execute_json_analysis(common_args, capsys)
        assert analysis_exit == 3

    _assert_json_contract(import_receipt)
    _assert_json_contract(analysis_receipt)
    assert import_receipt["result"]["status"] == "store_busy"
    assert analysis_receipt["result"]["type"] == "write_not_started"
    assert analysis_receipt["result"]["status"] == "store_busy"


def test_cli_returns_expected_incomplete_for_unstable_analysis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = generate_export(
        "null-v1",
        42,
        tmp_path / "fixture",
        options=GenerationOptions(resting_heart_rate_noise_standard_deviation=0.0),
    )
    common_args = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    assert _execute_json_import(common_args, fixture.export_path, capsys)[0] == 0

    exit_code, receipt = _execute_json_analysis(common_args, capsys)
    assert exit_code == 3
    _assert_json_contract(receipt)
    assert receipt["result"]["status"] == "unstable"


def test_json_cli_analysis_reports_plan_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = generate_export("null-v1", 42, tmp_path / "first")
    second = generate_export("null-v1", 43, tmp_path / "second")
    common_args = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    assert _execute_json_import(common_args, first.export_path, capsys)[0] == 0
    assert main([*common_args, "analyze", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert _execute_json_import(common_args, second.export_path, capsys)[0] == 0

    assert (
        main(
            [
                *common_args,
                "analyze",
                "--json",
                "--execute",
                "--expect-plan",
                plan["fingerprint"],
            ]
        )
        == 3
    )
    receipt = json.loads(capsys.readouterr().out)

    _assert_json_contract(receipt)
    assert receipt["result"]["type"] == "write_not_started"
    assert receipt["result"]["status"] == "plan_changed"


def test_installed_cli_contracts_streams_and_redacts_technical_errors(tmp_path: Path) -> None:
    executable = Path(sys.executable).with_name("healthlab")
    common_args = [
        str(executable),
        "--mode",
        "synthetic",
        "--real-store",
        str(tmp_path / "real"),
    ]

    success = subprocess.run(
        [
            *common_args,
            "--synthetic-store",
            str(tmp_path / "synthetic"),
            "overview",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert success.returncode == 0
    _assert_json_contract(json.loads(success.stdout))
    assert success.stderr == ""

    sensitive_store = tmp_path / "resting-heart-rate-181"
    sensitive_store.write_text("not a directory", encoding="utf-8")

    failure = subprocess.run(
        [
            *common_args,
            "--synthetic-store",
            str(sensitive_store),
            "overview",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert failure.returncode == 1
    assert failure.stdout == ""
    assert failure.stderr == (
        "ERROR healthlab_failed error_class=HealthLabError\nTechnischer HealthLab-Fehler.\n"
    )


def test_cli_returns_usage_error_for_unknown_arguments_and_invalid_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid_config = tmp_path / "config.json"
    invalid_config.write_text('{"mode": "unsupported"}', encoding="utf-8")
    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")

    for args in (
        ["--unknown"],
        [
            "--mode",
            "synthetic",
            "--synthetic-store",
            str(tmp_path / "synthetic"),
            "--real-store",
            str(tmp_path / "real"),
            "import",
            str(tmp_path / "health.zip"),
            "--json",
            "--execute",
            "--expect-plan",
            "not-a-fingerprint",
        ],
        ["--config", str(invalid_config), "overview"],
        ["--config", str(invalid_utf8), "overview"],
        ["--config", str(tmp_path / "missing.json"), "overview"],
    ):
        with pytest.raises(SystemExit) as exit_info:
            main(args)

        captured = capsys.readouterr()
        assert exit_info.value.code == 2
        assert captured.out == ""
        assert captured.err.startswith("usage: healthlab")


def test_cli_prints_empty_overview_as_versioned_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "--mode",
            "synthetic",
            "--synthetic-store",
            str(tmp_path / "synthetic"),
            "--real-store",
            str(tmp_path / "real"),
            "overview",
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    _assert_json_contract(output)
    assert output == {
        "analysis_history": [],
        "daily_series": [],
        "last_reviewed_analysis": None,
        "message": "Keine Gesundheitsdaten vorhanden.",
        "resting_hr_analysis": None,
        "provenance": {
            "import_count": 0,
            "logical_measurement_count": 0,
            "measurement_version_count": 0,
            "package_count": 0,
            "quarantined_import_count": 0,
            "snapshot_count": 0,
        },
        "runtime_config": _EXPECTED_RUNTIME_CONFIG,
        "schema_version": "1.0",
        "selection": {"end_date": None, "start_date": None},
        "status": "empty",
    }


def test_cli_imports_export_and_prints_daily_series(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    common_args = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]

    result = _execute_json_import(common_args, fixture.export_path, capsys)
    assert result[0] == 0
    receipt = result[1]
    assert receipt["result"]["status"] == "committed"
    assert receipt["result"]["anomaly_count"] == 1
    assert receipt["result"]["record_count"] == 1_825
    assert receipt["runtime_config"] == _EXPECTED_RUNTIME_CONFIG

    result = _execute_json_import(common_args, fixture.export_path, capsys)
    assert result[0] == 0
    duplicate = result[1]
    assert duplicate["result"]["status"] == "duplicate"

    assert main([*common_args, "overview", "--json"]) == 0
    overview = json.loads(capsys.readouterr().out)
    _assert_json_contract(overview)
    assert [(series["data_type"], series["unit"]) for series in overview["daily_series"]] == [
        ("active_energy", "kcal"),
        ("apple_resting_heart_rate", "count/min"),
    ]
    assert len(overview["daily_series"][0]["values"]) == 365
    assert overview["provenance"]["quarantined_import_count"] == 0


def test_cli_runs_and_exposes_the_built_in_lag_analysis(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    common_args = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    monkeypatch.setattr("builtins.input", lambda _: "j")
    assert main([*common_args, "import", str(fixture.export_path)]) == 0
    human_import = capsys.readouterr().out
    assert "Konfiguration:" in human_import
    assert str(tmp_path) not in human_import

    analysis_exit, receipt = _execute_json_analysis(common_args, capsys)
    assert analysis_exit == 0
    _assert_json_contract(receipt)
    analysis_receipt = receipt["result"]
    assert analysis_receipt["status"] == "completed"
    assert analysis_receipt["analysis_definition_id"] == "lag-signal-v2"
    analysis_result = analysis_receipt["analysis"]
    assert len(analysis_result["lag_associations"]) == 7
    assert analysis_result["model_maturity"] == "robust"
    assert analysis_result["methodology"]["bootstrap_method"] == "moving_block"
    assert analysis_result["diagnostics"]
    provenance = analysis_receipt["provenance"]
    assert provenance == analysis_result["provenance"]
    assert set(provenance) == {
        "analysis_definition_id",
        "analysis_run_id",
        "code_commit",
        "code_diff_hash",
        "code_dirty",
        "config_hash",
        "config_schema_version",
        "environment_lock_hash",
        "result_ref",
        "snapshot_ref",
    }
    assert len(provenance["config_hash"]) == 64
    assert len(provenance["environment_lock_hash"]) == 64
    assert all(
        item["pointwise_interval"] and item["simultaneous_band"]
        for item in analysis_result["lag_associations"]
    )

    structured_result = json.dumps(analysis_result, ensure_ascii=False).lower()
    assert all(
        forbidden not in structured_result
        for forbidden in (
            "*",
            "significant",
            "signifikant",
            "causal",
            "kausal",
            "medical",
            "medizin",
            "recommend",
            "empfehl",
            "therap",
        )
    )

    assert main([*common_args, "overview", "--json"]) == 0
    overview = json.loads(capsys.readouterr().out)
    _assert_json_contract(overview)
    result = overview["resting_hr_analysis"]
    assert len(result["lag_associations"]) == 7
    assert result["lag_associations"][0]["direction"] == "negative"
    assert result["provenance"] == provenance

    assert main([*common_args, "overview"]) == 0
    human_overview = capsys.readouterr().out
    assert "Konfiguration:" in human_overview
    assert str(tmp_path) not in human_overview

    reused_exit, reused = _execute_json_analysis(common_args, capsys)
    assert reused_exit == 0
    _assert_json_contract(reused)
    assert reused["result"]["status"] == "reused"

    assert main([*common_args, "analyze"]) == 0
    human_output = capsys.readouterr().out
    assert "Konfiguration:" in human_output
    assert str(tmp_path) not in human_output
    assert "simultan" in human_output
    assert "Diagnosen:" in human_output

    insufficient_exit, insufficient = _execute_json_analysis(
        common_args, capsys, "--start-date", "2024-12-20"
    )
    assert insufficient_exit == 3
    _assert_json_contract(insufficient)
    assert insufficient["result"]["status"] == "insufficient_data"
    assert insufficient["result"]["analysis"] is None
    assert insufficient["result"]["provenance"]["snapshot_ref"] == provenance["snapshot_ref"]
    assert insufficient["result"]["provenance"]["result_ref"] is None
