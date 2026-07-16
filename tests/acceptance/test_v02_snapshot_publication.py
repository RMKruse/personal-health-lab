from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import duckdb
import pytest

from personal_health_lab.application import (
    DataMode,
    HealthLab,
    HealthLabError,
    ImportHealthExport,
    ImportReceipt,
    ImportStatus,
    OverviewSelection,
    RuntimeConfig,
)

PARQUET_FILES = (
    "measurement_versions.parquet",
    "open_review_cases.parquet",
    "resolved_measurements.parquet",
    "source_occurrences.parquet",
)
LAST_COLUMNS = {
    "measurement_versions.parquet": "strong_source_id_hash",
    "open_review_cases.parquet": "evidence_fingerprint",
    "resolved_measurements.parquet": "conflict_resolution_decision_id",
    "source_occurrences.parquet": "occurrence_fingerprint",
}


def _package(path: Path, value: int | str = 60) -> Path:
    xml = (
        '<?xml version="1.0"?><HealthData>'
        '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
        'sourceName="Test Watch" sourceVersion="1" device="Test Device" '
        'unit="count/min" creationDate="2024-01-01 07:01:00 +0100" '
        'startDate="2024-01-01 07:00:00 +0100" '
        f'endDate="2024-01-01 07:01:00 +0100" value="{value}"/>'
        "</HealthData>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    return path


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic-store",
        real_store=tmp_path / "real-store",
    )


def _publish_snapshot(config: RuntimeConfig, package: Path) -> Path:
    request = ImportHealthExport(package)
    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert isinstance(receipt.result, ImportReceipt)
    assert receipt.result.snapshot_ref is not None
    return config.active_store / "parquet" / "snapshots" / str(receipt.result.snapshot_ref)


def _rewrite_manifest(config: RuntimeConfig, snapshot: Path, manifest: dict[str, object]) -> None:
    manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    (snapshot / "manifest.json").write_bytes(manifest_bytes)
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        metadata.execute(
            "UPDATE dataset_snapshots SET manifest_sha256 = ? WHERE snapshot_id = ?",
            (hashlib.sha256(manifest_bytes).hexdigest(), snapshot.name),
        )


def _refresh_file_entry(snapshot: Path, manifest: dict[str, object], filename: str) -> None:
    path = snapshot / filename
    escaped = str(path).replace("'", "''")
    with duckdb.connect() as query:
        description = tuple(
            (str(row[0]), str(row[1]))
            for row in query.execute(f"DESCRIBE SELECT * FROM read_parquet('{escaped}')").fetchall()
        )
        row_count = query.execute(f"SELECT count(*) FROM read_parquet('{escaped}')").fetchone()
    assert row_count is not None
    files = manifest["files"]
    assert isinstance(files, list)
    entry = next(entry for entry in files if entry["name"] == filename)
    entry.update(
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        allocated_bytes=path.stat().st_blocks * 512,
        row_count=int(row_count[0]),
        parquet_schema_fingerprint=hashlib.sha256(
            json.dumps(description, separators=(",", ":")).encode()
        ).hexdigest(),
    )


def test_import_publishes_one_validated_four_file_snapshot(tmp_path: Path) -> None:
    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "health.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(receipt.result, ImportReceipt)
    assert receipt.result.status is ImportStatus.COMMITTED
    assert receipt.result.snapshot_ref is not None
    snapshot = config.active_store / "parquet" / "snapshots" / str(receipt.result.snapshot_ref)
    assert tuple(sorted(path.name for path in snapshot.iterdir())) == (
        "manifest.json",
        *PARQUET_FILES,
    )

    manifest_bytes = (snapshot / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert (
        manifest_bytes
        == json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    )
    assert "active" not in manifest
    assert "path" not in manifest
    assert tuple(entry["name"] for entry in manifest["files"]) == PARQUET_FILES
    assert manifest["validation_counts"] == {
        "excluded": 0,
        "exports": 1,
        "included": 1,
        "logical_measurements": 1,
        "measurement_versions": 1,
        "open_review_cases": 0,
        "source_occurrences": 1,
    }

    with duckdb.connect() as query:
        for entry in manifest["files"]:
            parquet = snapshot / entry["name"]
            escaped = str(parquet).replace("'", "''")
            assert hashlib.sha256(parquet.read_bytes()).hexdigest() == entry["sha256"]
            assert parquet.stat().st_blocks * 512 == entry["allocated_bytes"]
            assert query.execute(f"SELECT count(*) FROM read_parquet('{escaped}')").fetchone() == (
                entry["row_count"],
            )
            assert len(entry["parquet_schema_fingerprint"]) == 64

    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        metadata.execute("PRAGMA foreign_keys = ON")
        assert metadata.execute("PRAGMA foreign_key_check").fetchall() == []
        assert metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone() == (str(receipt.result.snapshot_ref),)
        assert metadata.execute("SELECT count(*) FROM dataset_snapshots").fetchone() == (1,)
        assert metadata.execute("SELECT count(*) FROM snapshot_activations").fetchone() == (1,)
        assert metadata.execute("SELECT count(*) FROM write_operations").fetchone() == (1,)
        assert metadata.execute(
            "SELECT audit_position FROM audit_events ORDER BY audit_position"
        ).fetchall() == [(1,)]
        schema = "\n".join(
            str(row[0])
            for row in metadata.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
            ).fetchall()
        )
    assert "canonical_value" not in schema
    assert "original_value" not in schema


def test_active_snapshot_hash_mismatch_blocks_open(tmp_path: Path) -> None:
    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "health.zip"))
    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert isinstance(receipt.result, ImportReceipt)
    assert receipt.result.snapshot_ref is not None
    snapshot = config.active_store / "parquet" / "snapshots" / str(receipt.result.snapshot_ref)
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    manifest["files"][0]["sha256"] = "0" * 64
    (snapshot / "manifest.json").write_bytes(
        json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    )

    with (
        pytest.raises(HealthLabError, match="Datenspeicher konnte nicht geöffnet"),
        HealthLab.open(config),
    ):
        pass


def test_manifest_rejects_nested_non_schema_fields(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _publish_snapshot(config, _package(tmp_path / "health.zip"))
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    manifest["resolution_basis"]["path"] = str(snapshot)
    _rewrite_manifest(config, snapshot, manifest)

    with (
        pytest.raises(HealthLabError, match="Datenspeicher konnte nicht geöffnet"),
        HealthLab.open(config),
    ):
        pass


def test_legacy_versioned_store_with_identity_still_validates_snapshot(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _publish_snapshot(config, _package(tmp_path / "health.zip"))
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        mode, store_id, binding = metadata.execute(
            "SELECT mode, store_id, person_binding FROM store_identity"
        ).fetchone()
        metadata.execute("ALTER TABLE store_identity RENAME TO current_store_identity")
        metadata.execute(
            "CREATE TABLE store_identity (singleton INTEGER PRIMARY KEY, mode TEXT NOT NULL, "
            "schema_version TEXT NOT NULL, store_id TEXT, person_binding TEXT NOT NULL)"
        )
        metadata.execute(
            "INSERT INTO store_identity VALUES (1, ?, '1.2', ?, ?)",
            (mode, store_id, binding),
        )
        metadata.execute("DROP TABLE current_store_identity")
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    manifest["validation_counts"]["source_occurrences"] += 1
    _rewrite_manifest(config, snapshot, manifest)

    with (
        pytest.raises(HealthLabError, match="Datenspeicher konnte nicht geöffnet"),
        HealthLab.open(config),
    ):
        pass


@pytest.mark.parametrize("filename", PARQUET_FILES)
@pytest.mark.parametrize("corruption", ("hash", "row_count", "schema"))
def test_each_snapshot_file_rejects_hash_schema_and_row_count_corruption(
    filename: str,
    corruption: str,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "health.zip"))
    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert isinstance(receipt.result, ImportReceipt)
    assert receipt.result.snapshot_ref is not None
    snapshot = config.active_store / "parquet" / "snapshots" / str(receipt.result.snapshot_ref)
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    entry = next(entry for entry in manifest["files"] if entry["name"] == filename)
    if corruption == "hash":
        entry["sha256"] = "0" * 64
    elif corruption == "row_count":
        entry["row_count"] += 1
    else:
        source = snapshot / filename
        changed = snapshot / "changed.parquet"
        escaped_source = str(source).replace("'", "''")
        escaped_changed = str(changed).replace("'", "''")
        with duckdb.connect() as query:
            query.execute(
                f"COPY (SELECT * EXCLUDE ({LAST_COLUMNS[filename]}) "
                f"FROM read_parquet('{escaped_source}')) TO '{escaped_changed}' "
                "(FORMAT PARQUET)"
            )
        changed.replace(source)
        _refresh_file_entry(snapshot, manifest, filename)
    _rewrite_manifest(config, snapshot, manifest)

    with (
        pytest.raises(HealthLabError, match="Datenspeicher konnte nicht geöffnet"),
        HealthLab.open(config),
    ):
        pass


@pytest.mark.parametrize(
    "fault_point",
    (
        "import.before_snapshot_move/v1",
        "import.after_snapshot_move/v1",
        "import.before_sqlite_commit/v1",
    ),
)
def test_publication_fault_keeps_old_snapshot_active_and_quarantines_remainder(
    fault_point: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    first_request = ImportHealthExport(_package(tmp_path / "first.zip"))
    second_request = ImportHealthExport(_package(tmp_path / "second.zip", 61))
    with HealthLab.open(config) as health_lab:
        first_plan = health_lab.preview_write(first_request)
        first = health_lab.execute_write(first_request, expected_plan=first_plan.fingerprint)
        assert isinstance(first.result, ImportReceipt)
        assert first.result.snapshot_ref is not None
        second_plan = health_lab.preview_write(second_request)

        def fail_at(root: Path, current: str) -> None:
            if current == fault_point:
                raise RuntimeError(f"fault at {current}")

        monkeypatch.setattr("personal_health_lab.storage._store._publication_fault_point", fail_at)
        with pytest.raises(RuntimeError, match="fault at"):
            health_lab.execute_write(second_request, expected_plan=second_plan.fingerprint)

    monkeypatch.undo()
    with HealthLab.open(config) as health_lab:
        overview = health_lab.load_overview(OverviewSelection())

    assert overview.snapshot_count == 1
    assert overview.measurement_version_count == 1
    assert overview.quarantined_import_count == 1
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone() == (str(first.result.snapshot_ref),)
        assert metadata.execute("SELECT count(*) FROM write_operations").fetchone() == (1,)
        assert metadata.execute("SELECT count(*) FROM audit_events").fetchone() == (1,)


def test_active_snapshot_with_unclosed_measurement_reference_blocks_open(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "health.zip"))
    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert isinstance(receipt.result, ImportReceipt)
    assert receipt.result.snapshot_ref is not None
    snapshot = config.active_store / "parquet" / "snapshots" / str(receipt.result.snapshot_ref)
    occurrences = snapshot / "source_occurrences.parquet"
    changed = snapshot / "changed.parquet"
    escaped_occurrences = str(occurrences).replace("'", "''")
    escaped_changed = str(changed).replace("'", "''")
    with duckdb.connect() as query:
        query.execute(
            f"COPY (SELECT * REPLACE ('{'0' * 64}' AS measurement_version_id) "
            f"FROM read_parquet('{escaped_occurrences}')) TO '{escaped_changed}' "
            "(FORMAT PARQUET)"
        )
    changed.replace(occurrences)
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    _refresh_file_entry(snapshot, manifest, "source_occurrences.parquet")
    _rewrite_manifest(config, snapshot, manifest)

    with (
        pytest.raises(HealthLabError, match="Datenspeicher konnte nicht geöffnet"),
        HealthLab.open(config),
    ):
        pass


@pytest.mark.parametrize(
    ("filename", "replacement"),
    (
        ("source_occurrences.parquet", "NULL::VARCHAR AS occurrence_id"),
        ("measurement_versions.parquet", "'kg'::VARCHAR AS canonical_unit"),
        ("measurement_versions.parquet", "'Infinity'::DOUBLE AS canonical_value"),
        ("resolved_measurements.parquet", "'none'::VARCHAR AS effective_value_source"),
    ),
)
def test_snapshot_payload_constraint_violation_blocks_open(
    filename: str, replacement: str, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    snapshot = _publish_snapshot(config, _package(tmp_path / "health.zip"))
    source = snapshot / filename
    changed = snapshot / "changed.parquet"
    escaped_source = str(source).replace("'", "''")
    escaped_changed = str(changed).replace("'", "''")
    with duckdb.connect() as query:
        query.execute(
            f"COPY (SELECT * REPLACE ({replacement}) FROM read_parquet('{escaped_source}')) "
            f"TO '{escaped_changed}' (FORMAT PARQUET)"
        )
    changed.replace(source)
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    _refresh_file_entry(snapshot, manifest, filename)
    _rewrite_manifest(config, snapshot, manifest)

    with (
        pytest.raises(HealthLabError, match="Datenspeicher konnte nicht geöffnet"),
        HealthLab.open(config),
    ):
        pass


def test_snapshot_duplicate_key_blocks_open(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _publish_snapshot(config, _package(tmp_path / "health.zip"))
    occurrences = snapshot / "source_occurrences.parquet"
    changed = snapshot / "changed.parquet"
    escaped_occurrences = str(occurrences).replace("'", "''")
    escaped_changed = str(changed).replace("'", "''")
    with duckdb.connect() as query:
        query.execute(
            f"COPY (SELECT * FROM read_parquet('{escaped_occurrences}') "
            f"UNION ALL SELECT * FROM read_parquet('{escaped_occurrences}')) "
            f"TO '{escaped_changed}' (FORMAT PARQUET)"
        )
    changed.replace(occurrences)
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    _refresh_file_entry(snapshot, manifest, "source_occurrences.parquet")
    manifest["validation_counts"]["source_occurrences"] = 2
    _rewrite_manifest(config, snapshot, manifest)

    with (
        pytest.raises(HealthLabError, match="Datenspeicher konnte nicht geöffnet"),
        HealthLab.open(config),
    ):
        pass


def test_snapshot_validation_rejects_every_closed_contract_violation(tmp_path: Path) -> None:
    for filename in PARQUET_FILES:
        for corruption in ("hash", "row_count", "schema"):
            case = tmp_path / f"{filename}-{corruption}"
            case.mkdir()
            test_each_snapshot_file_rejects_hash_schema_and_row_count_corruption(
                filename, corruption, case
            )
    for index, (filename, replacement) in enumerate(
        (
            ("source_occurrences.parquet", "NULL::VARCHAR AS occurrence_id"),
            ("measurement_versions.parquet", "'kg'::VARCHAR AS canonical_unit"),
            ("measurement_versions.parquet", "'Infinity'::DOUBLE AS canonical_value"),
            ("resolved_measurements.parquet", "'none'::VARCHAR AS effective_value_source"),
        )
    ):
        case = tmp_path / f"payload-{index}"
        case.mkdir()
        test_snapshot_payload_constraint_violation_blocks_open(filename, replacement, case)
    for name, test in (
        ("id-closure", test_active_snapshot_with_unclosed_measurement_reference_blocks_open),
        ("uniqueness", test_snapshot_duplicate_key_blocks_open),
        ("review-xor", test_open_review_case_xor_violation_blocks_open),
    ):
        case = tmp_path / name
        case.mkdir()
        test(case)


def test_open_review_case_xor_violation_blocks_open(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _publish_snapshot(config, _package(tmp_path / "health.zip"))
    reviews = snapshot / "open_review_cases.parquet"
    changed = snapshot / "changed.parquet"
    escaped_changed = str(changed).replace("'", "''")
    with duckdb.connect() as query:
        query.execute(
            "COPY (SELECT 'b' || repeat('b', 31) AS review_case_id, "
            "'plausibility'::VARCHAR AS case_kind, NULL::VARCHAR AS logical_measurement_id, "
            "NULL::VARCHAR AS measurement_version_id, NULL::VARCHAR AS rule_version_id, "
            f"repeat('c', 64)::VARCHAR AS evidence_fingerprint) TO '{escaped_changed}' "
            "(FORMAT PARQUET)"
        )
    changed.replace(reviews)
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    _refresh_file_entry(snapshot, manifest, "open_review_cases.parquet")
    manifest["validation_counts"]["open_review_cases"] = 1
    _rewrite_manifest(config, snapshot, manifest)

    with (
        pytest.raises(HealthLabError, match="Datenspeicher konnte nicht geöffnet"),
        HealthLab.open(config),
    ):
        pass


@pytest.mark.parametrize(
    ("disposition", "effective_value", "value_source", "decision", "correction", "deletion"),
    (
        ("included_source", "canonical_value", "source", "NULL", "NULL", "NULL"),
        ("included_correction", "61.0", "correction", "'a'", "'a'", "NULL"),
        ("excluded_local", "NULL::DOUBLE", "none", "'a'", "NULL", "NULL"),
        ("excluded_source_deletion", "NULL::DOUBLE", "none", "'a'", "NULL", "'a'"),
    ),
)
def test_resolved_measurement_disposition_xor_accepts_closed_variants(
    disposition: str,
    effective_value: str,
    value_source: str,
    decision: str,
    correction: str,
    deletion: str,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    snapshot = _publish_snapshot(config, _package(tmp_path / "health.zip"))
    resolved = snapshot / "resolved_measurements.parquet"
    changed = snapshot / "changed.parquet"
    escaped_resolved = str(resolved).replace("'", "''")
    escaped_changed = str(changed).replace("'", "''")
    decision_id = "a" * 32
    replacements = {
        "'a'": f"'{decision_id}'",
        "canonical_value": "effective_value",
    }
    effective_value = replacements.get(effective_value, effective_value)
    decision = replacements.get(decision, decision)
    correction = replacements.get(correction, correction)
    deletion = replacements.get(deletion, deletion)
    with duckdb.connect() as query:
        query.execute(
            f"COPY (SELECT * REPLACE ("
            f"'{disposition}' AS disposition, "
            f"CAST({effective_value} AS DOUBLE) AS effective_value, "
            f"'{value_source}' AS effective_value_source, "
            f"CAST({decision} AS VARCHAR) AS effective_decision_id, "
            f"CAST({correction} AS VARCHAR) AS correction_decision_id, "
            f"CAST({deletion} AS VARCHAR) AS source_deletion_decision_id) "
            f"FROM read_parquet('{escaped_resolved}')) TO '{escaped_changed}' (FORMAT PARQUET)"
        )
    changed.replace(resolved)
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    _refresh_file_entry(snapshot, manifest, "resolved_measurements.parquet")
    manifest["validation_counts"]["included"] = int(disposition.startswith("included"))
    manifest["validation_counts"]["excluded"] = int(disposition.startswith("excluded"))
    _rewrite_manifest(config, snapshot, manifest)
    decision_kind = {
        "included_correction": "correction",
        "excluded_local": "local_exclusion",
        "excluded_source_deletion": "source_deletion",
    }.get(disposition)
    if decision_kind is not None:
        with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
            metadata.execute(
                "INSERT INTO decision_refs VALUES (?, ?)", (decision_id, decision_kind)
            )

    with HealthLab.open(config):
        pass


def test_uncataloged_decision_reference_blocks_open(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _publish_snapshot(config, _package(tmp_path / "health.zip"))
    resolved = snapshot / "resolved_measurements.parquet"
    changed = snapshot / "changed.parquet"
    decision_id = "a" * 32
    with duckdb.connect() as query:
        query.execute(
            f"COPY (SELECT * REPLACE ('included_correction'::VARCHAR AS disposition, "
            "61.0::DOUBLE AS effective_value, 'correction'::VARCHAR AS effective_value_source, "
            f"'{decision_id}'::VARCHAR AS effective_decision_id, "
            f"'{decision_id}'::VARCHAR AS correction_decision_id) "
            f"FROM read_parquet('{resolved}')) TO '{changed}' (FORMAT PARQUET)"
        )
    changed.replace(resolved)
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    _refresh_file_entry(snapshot, manifest, "resolved_measurements.parquet")
    _rewrite_manifest(config, snapshot, manifest)

    with (
        pytest.raises(HealthLabError, match="Datenspeicher konnte nicht geöffnet"),
        HealthLab.open(config),
    ):
        pass


def test_valid_open_review_case_is_closed_over_snapshot_ids(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _publish_snapshot(config, _package(tmp_path / "health.zip"))
    versions = snapshot / "measurement_versions.parquet"
    reviews = snapshot / "open_review_cases.parquet"
    changed = snapshot / "changed.parquet"
    escaped_changed = str(changed).replace("'", "''")
    with duckdb.connect() as query:
        version_id = query.execute(
            f"SELECT measurement_version_id FROM read_parquet('{versions}')"
        ).fetchone()[0]
        query.execute(
            "COPY (SELECT ?::VARCHAR AS review_case_id, 'plausibility'::VARCHAR AS case_kind, "
            "NULL::VARCHAR AS logical_measurement_id, ?::VARCHAR AS measurement_version_id, "
            "'plausibility/v1'::VARCHAR AS rule_version_id, "
            f"?::VARCHAR AS evidence_fingerprint) TO '{escaped_changed}' (FORMAT PARQUET)",
            ["b" * 32, version_id, "c" * 64],
        )
    changed.replace(reviews)
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    _refresh_file_entry(snapshot, manifest, "open_review_cases.parquet")
    manifest["validation_counts"]["open_review_cases"] = 1
    _rewrite_manifest(config, snapshot, manifest)
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        metadata.execute(
            "INSERT INTO rule_version_refs VALUES ('plausibility/v1', 'plausibility')"
        )

    with HealthLab.open(config):
        pass


def test_next_import_carries_forward_resolution_and_open_review_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _publish_snapshot(config, _package(tmp_path / "first.zip"))
    resolved = snapshot / "resolved_measurements.parquet"
    reviews = snapshot / "open_review_cases.parquet"
    changed = snapshot / "changed.parquet"
    with duckdb.connect() as query:
        version_id = query.execute(
            f"SELECT selected_measurement_version_id FROM read_parquet('{resolved}')"
        ).fetchone()[0]
        query.execute(
            f"COPY (SELECT * REPLACE ('excluded_local'::VARCHAR AS disposition, "
            "NULL::DOUBLE AS effective_value, 'none'::VARCHAR AS effective_value_source, "
            f"repeat('a', 32)::VARCHAR AS effective_decision_id) FROM read_parquet('{resolved}')) "
            f"TO '{changed}' (FORMAT PARQUET)"
        )
    changed.replace(resolved)
    with duckdb.connect() as query:
        query.execute(
            "COPY (SELECT repeat('b', 32)::VARCHAR AS review_case_id, "
            "'plausibility'::VARCHAR AS case_kind, NULL::VARCHAR AS logical_measurement_id, "
            f"'{version_id}'::VARCHAR AS measurement_version_id, "
            "'plausibility/v1'::VARCHAR AS rule_version_id, "
            f"repeat('c', 64)::VARCHAR AS evidence_fingerprint) TO '{changed}' (FORMAT PARQUET)"
        )
    changed.replace(reviews)
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    _refresh_file_entry(snapshot, manifest, "resolved_measurements.parquet")
    _refresh_file_entry(snapshot, manifest, "open_review_cases.parquet")
    manifest["validation_counts"].update(included=0, excluded=1, open_review_cases=1)
    _rewrite_manifest(config, snapshot, manifest)
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        metadata.execute(
            "INSERT INTO decision_refs VALUES (?, 'local_exclusion')", ("a" * 32,)
        )
        metadata.execute(
            "INSERT INTO rule_version_refs VALUES ('plausibility/v1', 'plausibility')"
        )

    next_snapshot = _publish_snapshot(config, _package(tmp_path / "second.zip", 61))
    next_resolved = next_snapshot / "resolved_measurements.parquet"
    next_reviews = next_snapshot / "open_review_cases.parquet"
    with duckdb.connect() as query:
        assert query.execute(
            f"SELECT disposition FROM read_parquet('{next_resolved}')"
        ).fetchall() == [("excluded_local",)]
        assert query.execute(
            f"SELECT case_kind FROM read_parquet('{next_reviews}')"
        ).fetchall() == [("plausibility",)]


def test_sqlite_catalog_and_audit_constraints_are_hard(tmp_path: Path) -> None:
    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "health.zip"))
    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert isinstance(receipt.result, ImportReceipt)
    assert receipt.result.snapshot_ref is not None

    metadata_path = config.active_store / "metadata.sqlite3"
    with sqlite3.connect(metadata_path) as metadata:
        metadata.execute("PRAGMA foreign_keys = ON")
        operation_id = metadata.execute("SELECT operation_id FROM write_operations").fetchone()[0]
        strict_tables = {
            row[0]
            for row in metadata.execute(
                "SELECT name FROM pragma_table_list WHERE strict = 1"
            ).fetchall()
        }
        assert {
            "store_identity",
            "write_operations",
            "dataset_snapshots",
            "snapshot_activations",
            "active_snapshot",
            "audit_events",
            "import_publications",
            "metadata_tombstones",
            "decision_refs",
            "rule_version_refs",
        } <= strict_tables
        assert metadata.execute(
            "SELECT schema_version, typeof(schema_version) FROM store_identity"
        ).fetchone() == (4, "integer")

        with pytest.raises(sqlite3.IntegrityError):
            metadata.execute(
                "UPDATE write_operations SET state_changed = 2 WHERE operation_id = ?",
                (operation_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            metadata.execute(
                "UPDATE write_operations SET completed_at_utc = 'not-a-time' "
                "WHERE operation_id = ?",
                (operation_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            metadata.execute(
                "UPDATE active_snapshot SET snapshot_id = ? WHERE singleton = 1",
                ("0" * 32,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            metadata.execute(
                "INSERT INTO audit_events VALUES (3, ?, ?, 'import_published', ?)",
                ("1" * 32, operation_id, "2024-01-01T00:00:00+00:00"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            metadata.execute("UPDATE audit_events SET occurred_at_utc = occurred_at_utc")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            metadata.execute("DELETE FROM audit_events")

        metadata.execute("SAVEPOINT forward_tombstone")
        metadata.execute(
            "INSERT INTO audit_events VALUES (2, ?, ?, 'metadata_tombstone', ?)",
            ("2" * 32, operation_id, "2024-01-01T00:00:00+00:00"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            metadata.execute(
                "INSERT INTO metadata_tombstones VALUES (?, ?, ?, 'deleted', NULL, NULL)",
                ("3" * 32, "2" * 32, "2" * 32),
            )
        metadata.execute("ROLLBACK TO forward_tombstone")
        metadata.execute("RELEASE forward_tombstone")

        metadata.execute(
            "INSERT INTO audit_events VALUES (2, ?, ?, 'import_published', ?)",
            ("2" * 32, operation_id, "2024-01-01T00:00:00+00:00"),
        )

    with (
        pytest.raises(HealthLabError, match="Datenspeicher konnte nicht geöffnet"),
        HealthLab.open(config),
    ):
        pass


def test_recognized_package_validation_failure_is_quarantined(tmp_path: Path) -> None:
    config = _config(tmp_path)
    base_request = ImportHealthExport(_package(tmp_path / "base.zip"))
    invalid_request = ImportHealthExport(_package(tmp_path / "invalid.zip", "NaN"))
    with HealthLab.open(config) as health_lab:
        base_plan = health_lab.preview_write(base_request)
        base = health_lab.execute_write(base_request, expected_plan=base_plan.fingerprint)
        assert isinstance(base.result, ImportReceipt)
        invalid_plan = health_lab.preview_write(invalid_request)
        invalid = health_lab.execute_write(invalid_request, expected_plan=invalid_plan.fingerprint)
        overview = health_lab.load_overview(OverviewSelection())

    assert isinstance(invalid.result, ImportReceipt)
    assert invalid.result.status is ImportStatus.QUARANTINED
    assert invalid.result.snapshot_ref is None
    assert invalid.result.diagnostics == ("invalid_health_data",)
    assert overview.snapshot_count == 1
    assert overview.measurement_version_count == 1
    assert overview.quarantined_import_count == 1
