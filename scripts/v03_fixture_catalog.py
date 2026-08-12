import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]

_CORE_SLEEP_XML = """<HealthData><ExportDate value="2024-01-06 12:00:00 +0100"/>
<Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisInBed"
 sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
 creationDate="2024-01-06 06:00:00 +0100" startDate="2024-01-05 22:00:00 +0100"
 endDate="2024-01-06 06:00:00 +0100"/>
<Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisAwake"
 sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
 creationDate="2024-01-06 06:00:00 +0100" startDate="2024-01-05 22:00:00 +0100"
 endDate="2024-01-05 22:10:00 +0100"/>
<Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisAsleep"
 sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
 creationDate="2024-01-06 06:00:00 +0100" startDate="2024-01-05 22:10:00 +0100"
 endDate="2024-01-05 23:00:00 +0100"/>
<Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisAsleepCore"
 sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
 creationDate="2024-01-06 06:00:00 +0100" startDate="2024-01-05 23:00:00 +0100"
 endDate="2024-01-06 00:00:00 +0100"/>
<Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisAsleepDeep"
 sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
 creationDate="2024-01-06 06:00:00 +0100" startDate="2024-01-06 00:00:00 +0100"
 endDate="2024-01-06 01:00:00 +0100"/>
<Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisAsleepREM"
 sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
 creationDate="2024-01-06 06:00:00 +0100" startDate="2024-01-06 01:00:00 +0100"
 endDate="2024-01-06 02:00:00 +0100"/></HealthData>"""

_RECIPES: dict[str, dict[str, object]] = {
    "core-journey": {
        "scenario": "lag-signal-v1",
        "artifacts": [
            "tests/fixtures/v03/weight-edges.xml",
            "tests/fixtures/v03/unsupported-import-content.xml",
        ],
        "activity": "activity-coverage-edges",
        "sleep_xml": _CORE_SLEEP_XML,
        "nutrition": "all-supported-types",
        "imports": ["initial", "identical-repeat", "cumulative-follow-up"],
    },
    "activity-coverage-edges": {
        "workflows": [
            "all-metrics-units-boundary",
            "same-metric-overlap",
            "point-boundary",
            "negative-value",
            "watch-coverage-thresholds",
            "derivation-version",
            "workout-overlap-types",
            "workout-correction",
        ],
        "xml": """<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
        <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 08:10:00 +0100"
        startDate="2024-01-02 08:00:00 +0100" endDate="2024-01-02 08:10:00 +0100"/>
        <Record type="HKQuantityTypeIdentifierActiveEnergyBurned" unit="kcal" value="1"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 16:10:00 +0100"
        startDate="2024-01-02 16:00:00 +0100" endDate="2024-01-02 16:10:00 +0100"/>
        <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="2"
        sourceName="iPhone" sourceVersion="1" device="iPhone"
        creationDate="2024-01-02 10:10:00 +0100"
        startDate="2024-01-02 10:00:00 +0100" endDate="2024-01-02 10:10:00 +0100"/>
        <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="4"
        sourceName="iPhone" sourceVersion="1" device="iPhone"
        creationDate="2024-01-02 07:10:00 +0100"
        startDate="2024-01-02 07:00:00 +0100" endDate="2024-01-02 07:10:00 +0100"/>
        <Record type="HKQuantityTypeIdentifierAppleExerciseTime" unit="s" value="120"
        sourceName="Third Party" sourceVersion="1" device="Chest Strap"
        creationDate="2024-01-02 18:02:00 +0100"
        startDate="2024-01-02 18:00:00 +0100" endDate="2024-01-03 00:02:00 +0100"/>
        <Record type="HKQuantityTypeIdentifierDistanceWalkingRunning" unit="mi" value="1"
        sourceName="Mystery Source" sourceVersion="1" device="Unknown Device"
        creationDate="2024-01-02 17:01:00 +0100"
        startDate="2024-01-02 17:00:00 +0100" endDate="2024-01-02 17:01:00 +0100"/>
        <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="3"
        sourceName="" sourceVersion="" device=""
        creationDate="2024-01-02 14:01:00 +0100"
        startDate="2024-01-02 14:00:00 +0100" endDate="2024-01-02 14:01:00 +0100"/>
        <Record type="HKQuantityTypeIdentifierAppleExerciseTime" unit="min" value="20"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-03-31 03:10:00 +0200"
        startDate="2024-03-31 01:50:00 +0100" endDate="2024-03-31 03:10:00 +0200"/>
        <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30"
        durationUnit="min" sourceName="Third Party" sourceVersion="1" device="Other"
        creationDate="2024-01-02 19:30:00 +0100"
        startDate="2024-01-02 19:00:00 +0100" endDate="2024-01-02 19:30:00 +0100"/>
        <Workout workoutActivityType="com.example.unknown" sourceName="iPhone"
        sourceVersion="1" device="iPhone" creationDate="2024-01-02 19:46:00 +0100"
        startDate="2024-01-02 19:15:00 +0100" endDate="2024-01-02 19:45:00 +0100"/>
        <Workout workoutActivityType="HKWorkoutActivityTypeYoga"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 20:15:00 +0100"
        startDate="2024-01-02 19:45:00 +0100" endDate="2024-01-02 20:15:00 +0100"/>
        </HealthData>"""
    },
    "context-workflow": {
        "coverage_start": "2024-01-01",
        "category": "Erkältung",
        "workflows": ["coverage", "illness-collision", "stress-custom", "catalog-lifecycle"],
    },
    "medication-workflow": {
        "starts_at": "2024-03-01T00:00:00+01:00",
        "timezone": "Europe/Berlin",
        "entry_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "medication": "Ibuprofen",
        "amount": "400",
        "unit": "mg",
        "workflows": ["regime-dst", "deviation", "as-needed-catalog", "snapshot-binding"],
    },
    "persistence-lifecycle": {
        "snapshot_schema_version": 7,
        "workflows": [
            "v02-v03-migration",
            "rollback",
            "blocked-late-rollback",
            "backup-restore-rebind",
        ],
    },
    "security-boundaries": {
        "xml": "<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><HealthData>&e;</HealthData>"
    },
}


def fixture_recipe(*, seed: int, options: dict[str, object]) -> str:
    family = str(options["family"])
    recipe = json.loads(json.dumps(_RECIPES[family]))
    if family == "core-journey":
        recipe["artifacts"] = [
            {"path": path, "sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest()}
            for path in recipe["artifacts"]
        ]
        recipe["activity_sha256"] = hashlib.sha256(
            str(_RECIPES["activity-coverage-edges"]["xml"]).encode()
        ).hexdigest()
        recipe["nutrition_source_sha256"] = hashlib.sha256(
            (ROOT / "tests/acceptance/test_v03_weight.py").read_bytes()
        ).hexdigest()
    return json.dumps(
        {"family": family, "recipe": recipe, "seed": seed, "version": options["version"]},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
