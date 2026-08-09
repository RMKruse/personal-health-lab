"""Source resolution and data-review policy."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Literal

from personal_health_lab.health_data import (
    CanonicalHealthType,
    LogicalMeasurementId,
    MeasurementVersionId,
    canonical_unit_for,
    classify_activity_source,
)
from personal_health_lab.storage import (
    ExportFact,
    HistoricalReviewPublication,
    HistoricalReviewResult,
    LocalStore,
    MeasurementVersionFact,
    OpenDataReviewCase,
    OperationId,
    PlausibilityRuleRecord,
    ResolvedMeasurement,
    ReviewCaseId,
    ReviewCycleRecord,
    ReviewCycleUpdate,
    SnapshotId,
    SourceOccurrenceFact,
    SourceResolution,
    SourceTypeRuleRequest,
    StoredReviewCycleId,
)


def _natural_identity(version: MeasurementVersionFact) -> tuple[str, str, str, str, str]:
    return (
        version.canonical_type,
        version.source_start_utc,
        version.source_end_utc,
        version.source_name,
        version.device,
    )


@dataclass(frozen=True, slots=True)
class ReviewReason:
    code: Literal[
        "below_fixed_lower_bound",
        "above_fixed_upper_bound",
        "below_personal_lower_bound",
        "above_personal_upper_bound",
    ]
    lower_bound: float
    upper_bound: float | None
    unit: str


_FIXED_RULES = (
    PlausibilityRuleRecord(
        "fixed-plausibility/v1",
        "apple_resting_heart_rate",
        "count/min",
        20.0,
        250.0,
        True,
        None,
        datetime(1970, 1, 1, tzinfo=UTC),
    ),
    PlausibilityRuleRecord(
        "fixed-plausibility/v1",
        "active_energy",
        "kcal",
        0.0,
        None,
        False,
        None,
        datetime(1970, 1, 1, tzinfo=UTC),
    ),
    *(
        PlausibilityRuleRecord(
            "fixed-plausibility/v1",
            data_type.value,
            canonical_unit_for(data_type).value,
            0.0,
            None,
            False,
            None,
            datetime(1970, 1, 1, tzinfo=UTC),
        )
        for data_type in (
            CanonicalHealthType.APPLE_EXERCISE_TIME,
            CanonicalHealthType.STEP_COUNT,
            CanonicalHealthType.WALKING_RUNNING_DISTANCE,
        )
    ),
)
_NUTRITION_RECOMMENDATIONS = tuple(
    PlausibilityRuleRecord(
        "fixed-plausibility/v1",
        data_type.value,
        canonical_unit_for(data_type).value,
        0.0,
        None,
        False,
        None,
        datetime(1970, 1, 1, tzinfo=UTC),
        "builtin-plausibility/v1",
    )
    for data_type in CanonicalHealthType
    if data_type.value.startswith("dietary_")
)


def _rule_at(
    source_type: str,
    measured_at: datetime,
    rules: tuple[PlausibilityRuleRecord, ...],
) -> PlausibilityRuleRecord | None:
    return max(
        (
            rule
            for rule in rules
            if rule.data_type == source_type
            and (rule.effective_from is None or rule.effective_from <= measured_at)
        ),
        key=lambda rule: (
            datetime.min.replace(tzinfo=UTC)
            if rule.effective_from is None
            else rule.effective_from.astimezone(UTC)
        ),
        default=None,
    )


def _plausibility_reasons(
    rule: PlausibilityRuleRecord,
    value: float,
    unit: str,
    personal_bounds: tuple[float, float] | None = None,
) -> tuple[ReviewReason, ...]:
    reasons = []
    if rule.fixed_lower_bound is not None and value < rule.fixed_lower_bound:
        reasons.append(
            ReviewReason(
                "below_fixed_lower_bound",
                rule.fixed_lower_bound,
                rule.fixed_upper_bound,
                unit,
            )
        )
    if rule.fixed_upper_bound is not None and value > rule.fixed_upper_bound:
        reasons.append(
            ReviewReason(
                "above_fixed_upper_bound",
                rule.fixed_lower_bound
                if rule.fixed_lower_bound is not None
                else rule.fixed_upper_bound,
                rule.fixed_upper_bound,
                unit,
            )
        )
    if personal_bounds is not None and value < personal_bounds[0]:
        reasons.append(ReviewReason("below_personal_lower_bound", *personal_bounds, unit))
    if personal_bounds is not None and value > personal_bounds[1]:
        reasons.append(ReviewReason("above_personal_upper_bound", *personal_bounds, unit))
    return tuple(reasons)


def _personal_bounds(
    rule: PlausibilityRuleRecord,
    current_day: date,
    daily_values: tuple[tuple[date, float], ...],
    unit: str,
) -> tuple[float, float] | None:
    first_day = current_day - timedelta(days=42)
    values = [
        value
        for day, value in daily_values
        if first_day <= day < current_day and not _plausibility_reasons(rule, value, unit)
    ]
    if len(values) < 28:
        return None
    center = median(values)
    mad = median(abs(value - center) for value in values)
    if mad == 0:
        return None
    width = 3.5 * mad / 0.6745
    return center - width, center + width


def _effective_daily_resting_hr(
    version_by_id: dict[str, MeasurementVersionFact],
    measurements: tuple[ResolvedMeasurement, ...],
) -> dict[date, tuple[MeasurementVersionFact, ResolvedMeasurement]]:
    daily: dict[date, tuple[MeasurementVersionFact, ResolvedMeasurement]] = {}
    for measurement in measurements:
        version = version_by_id[measurement.selected_measurement_version_id]
        if (
            version.canonical_type != "apple_resting_heart_rate"
            or measurement.effective_value is None
        ):
            continue
        current = daily.get(version.measurement_local_date)
        if current is None or (
            version.source_updated_at_utc,
            version.source_version,
            version.source_start_utc,
            version.measurement_version_id,
        ) > (
            current[0].source_updated_at_utc,
            current[0].source_version,
            current[0].source_start_utc,
            current[0].measurement_version_id,
        ):
            daily[version.measurement_local_date] = (version, measurement)
    return daily


@dataclass(frozen=True, slots=True)
class ReviewCaseDetail:
    source_type: str | None
    measured_at: datetime | None
    effective_value: float | None
    effective_value_source: str | None
    reasons: tuple[ReviewReason, ...]
    canonical_unit: str | None


@dataclass(frozen=True, slots=True)
class ReviewBackupFact:
    snapshot_id: SnapshotId
    review_case_id: str
    case_kind: str
    logical_measurement_id: LogicalMeasurementId | None
    measurement_version_id: MeasurementVersionId | None
    rule_version_id: str | None
    evidence_fingerprint: str
    source_type: str | None
    measured_at_utc: datetime | None
    effective_value: float | None
    effective_value_source: str | None
    canonical_unit: str | None
    reasons: tuple[ReviewReason, ...]


def select_governing_export(exports: tuple[ExportFact, ...]) -> str:
    """Choose one export reproducibly; unordered exports never outrank dated ones."""
    if not exports:
        raise ValueError("Mindestens ein Export wird für die Auflösung benötigt.")
    ordered = tuple(item for item in exports if item.export_date is not None)
    candidates = ordered or exports
    return max(
        candidates,
        key=lambda item: (
            datetime.min.replace(tzinfo=UTC)
            if item.export_date is None
            else item.export_date.astimezone(UTC),
            item.export_id,
        ),
    ).export_id


def resolve_sources(
    *,
    occurrences: tuple[SourceOccurrenceFact, ...],
    versions: tuple[MeasurementVersionFact, ...],
    exports: tuple[ExportFact, ...],
    previous_measurements: tuple[ResolvedMeasurement, ...],
    previous_review_cases: tuple[OpenDataReviewCase, ...],
    governing_export_id: str,
    imported_measurement_version_ids: tuple[str, ...] = (),
    unknown_source_types: tuple[str, ...] = (),
    suppressed_deletion_ids: frozenset[str] = frozenset(),
    plausibility_rules: tuple[PlausibilityRuleRecord, ...] = _FIXED_RULES,
) -> SourceResolution:
    """Resolve effective source values and expose identity contradictions."""
    version_by_id = {item.measurement_version_id: item for item in versions}
    export_dates = {item.export_id: item.export_date for item in exports}
    grouped: dict[str, list[SourceOccurrenceFact]] = defaultdict(list)
    for occurrence in occurrences:
        grouped[occurrence.logical_measurement_id].append(occurrence)

    current: list[ResolvedMeasurement] = []
    for logical_id, candidates in grouped.items():

        def preference(item: SourceOccurrenceFact) -> tuple[bool, datetime, str, int]:
            export_date = export_dates[item.export_id]
            return (
                item.export_id == governing_export_id,
                datetime.min.replace(tzinfo=UTC)
                if export_date is None
                else export_date.astimezone(UTC),
                item.export_id,
                -item.export_ordinal,
            )

        selected = max(
            candidates,
            key=preference,
        )
        version = version_by_id[selected.measurement_version_id]
        current.append(
            ResolvedMeasurement(
                logical_measurement_id=logical_id,
                selected_measurement_version_id=version.measurement_version_id,
                disposition="included_source",
                effective_value=version.canonical_value,
                canonical_unit=version.canonical_unit,
                effective_value_source="source",
                effective_decision_id=None,
                correction_decision_id=None,
                source_deletion_decision_id=None,
                conflict_resolution_decision_id=None,
            )
        )

    present_in_governing = {
        item.logical_measurement_id for item in occurrences if item.export_id == governing_export_id
    }
    conflict_overrides: list[ResolvedMeasurement] = []
    conflict_keys: set[tuple[str, str, str, str, str]] = set()
    for previous in previous_measurements:
        if previous.conflict_resolution_decision_id is None:
            continue
        selected_version = version_by_id[previous.selected_measurement_version_id]
        natural_key = _natural_identity(selected_version)
        conflict_keys.add(natural_key)
        governing_versions = [
            version_by_id[item.measurement_version_id]
            for item in occurrences
            if item.export_id == governing_export_id
            and version_by_id[item.measurement_version_id].strong_source_id_hash
            == selected_version.strong_source_id_hash
            and selected_version.strong_source_id_hash is not None
        ]
        newest = max(
            governing_versions,
            key=lambda item: item.measurement_version_id,
            default=selected_version,
        )
        conflict_overrides.append(
            replace(
                previous,
                selected_measurement_version_id=newest.measurement_version_id,
                effective_value=newest.canonical_value,
                canonical_unit=newest.canonical_unit,
            )
        )
    current = [
        item
        for item in current
        if _natural_identity(version_by_id[item.selected_measurement_version_id])
        not in conflict_keys
    ]
    overrides = {
        item.logical_measurement_id: item
        for item in previous_measurements
        if item.disposition != "included_source"
        and not (
            item.disposition == "excluded_source_deletion"
            and item.logical_measurement_id in present_in_governing
        )
    }
    measurements = tuple(
        sorted(
            (
                *overrides.values(),
                *conflict_overrides,
                *(item for item in current if item.logical_measurement_id not in overrides),
            ),
            key=lambda item: item.logical_measurement_id,
        )
    )

    continued_overrides = tuple(
        _continued_override_case(previous, source)
        for previous in previous_measurements
        if previous.disposition in {"included_correction", "excluded_local"}
        for source in current
        if source.logical_measurement_id == previous.logical_measurement_id
        and source.selected_measurement_version_id != previous.selected_measurement_version_id
    )
    continued_version_ids = {str(case.measurement_version_id) for case in continued_overrides}
    plausibility_cases = tuple(
        case
        for case in _plausibility_cases(
            version_by_id,
            measurements,
            imported_measurement_version_ids,
            plausibility_rules,
        )
        if str(case.measurement_version_id) not in continued_version_ids
    )

    generated_cases = (
        *_source_conflicts(occurrences, version_by_id, conflict_keys),
        *_activity_overlap_conflicts(version_by_id, measurements),
        *_source_deletions(
            occurrences,
            version_by_id,
            exports,
            previous_review_cases,
            governing_export_id,
            suppressed_deletion_ids,
        ),
        *continued_overrides,
        *plausibility_cases,
        *_preferred_daily_weight_conflicts(version_by_id, measurements),
        *(_unknown_rule_case(source_type) for source_type in sorted(set(unknown_source_types))),
    )
    previous_case_ids = {item.review_case_id for item in previous_review_cases}
    continued_logical_ids = {str(case.logical_measurement_id) for case in continued_overrides}
    cases_by_id = {
        item.review_case_id: item
        for item in previous_review_cases
        if item.kind not in {"suspected_source_deletion", "preferred_daily_weight_conflict"}
        and not (
            item.kind == "continued_override"
            and str(item.logical_measurement_id) in continued_logical_ids
        )
    }
    cases_by_id.update((item.review_case_id, item) for item in generated_cases)
    source_type_requests = tuple(
        SourceTypeRuleRequest(
            source_type, ReviewCaseId(_unknown_rule_case(source_type).review_case_id)
        )
        for source_type in sorted(set(unknown_source_types))
    )
    new_case_ids = tuple(
        ReviewCaseId(case_id)
        for case_id in sorted(
            case.review_case_id
            for case in generated_cases
            if case.review_case_id not in previous_case_ids
        )
    )
    new_case_id_set = {str(case_id) for case_id in new_case_ids}
    return SourceResolution(
        measurements=measurements,
        review_cases=tuple(cases_by_id[key] for key in sorted(cases_by_id)),
        new_review_case_ids=new_case_ids,
        source_type_requests=source_type_requests,
        cycle_status="open" if new_case_ids else "closed",
        cycle_open_case_count=len(new_case_ids),
        anomaly_count=sum(
            case.review_case_id in new_case_id_set and case.kind == "plausibility"
            for case in generated_cases
        ),
    )


def _continued_override_case(
    previous: ResolvedMeasurement, source: ResolvedMeasurement
) -> OpenDataReviewCase:
    case_key = (
        f"continued_override:{previous.effective_decision_id}:"
        f"{source.selected_measurement_version_id}"
    )
    return OpenDataReviewCase(
        review_case_id=hashlib.sha256(case_key.encode()).hexdigest()[:32],
        kind="continued_override",
        logical_measurement_id=LogicalMeasurementId(previous.logical_measurement_id),
        measurement_version_id=MeasurementVersionId(source.selected_measurement_version_id),
        rule_version_id=None,
        evidence_fingerprint=hashlib.sha256(f"{case_key}:evidence".encode()).hexdigest(),
    )


def _plausibility_cases(
    version_by_id: dict[str, MeasurementVersionFact],
    measurements: tuple[ResolvedMeasurement, ...],
    imported_measurement_version_ids: tuple[str, ...],
    rules: tuple[PlausibilityRuleRecord, ...] = _FIXED_RULES,
) -> tuple[OpenDataReviewCase, ...]:
    cases = []
    daily = _effective_daily_resting_hr(version_by_id, measurements)
    daily_values = tuple(
        (day, measurement.effective_value)
        for day, (_, measurement) in sorted(daily.items())
        if measurement.effective_value is not None
    )
    measurement_by_version = {
        measurement.selected_measurement_version_id: measurement for measurement in measurements
    }
    for version_id in sorted(set(imported_measurement_version_ids)):
        version = version_by_id[version_id]
        measured_at = datetime.fromisoformat(version.source_start_utc)
        rule = _rule_at(version.canonical_type, measured_at, rules)
        if rule is None:
            continue
        measurement = measurement_by_version.get(version_id)
        effective_value = (
            version.canonical_value
            if measurement is None or measurement.effective_value is None
            else measurement.effective_value
        )
        reasons = list(_plausibility_reasons(rule, effective_value, version.canonical_unit))
        if rule.personal_range_enabled and version.canonical_type == "apple_resting_heart_rate":
            personal_bounds = _personal_bounds(
                rule,
                version.measurement_local_date,
                daily_values,
                version.canonical_unit,
            )
            reasons.extend(
                reason
                for reason in _plausibility_reasons(
                    rule,
                    effective_value,
                    version.canonical_unit,
                    personal_bounds,
                )
                if reason.code in {"below_personal_lower_bound", "above_personal_upper_bound"}
            )
        if not reasons:
            continue
        evidence = hashlib.sha256(
            repr(
                tuple(
                    (reason.code, reason.lower_bound, reason.upper_bound, reason.unit)
                    for reason in reasons
                )
            ).encode()
        ).hexdigest()
        case_key = f"{version_id}:{rule.version_id}:{evidence}"
        cases.append(
            OpenDataReviewCase(
                review_case_id=hashlib.sha256(case_key.encode()).hexdigest()[:32],
                kind="plausibility",
                logical_measurement_id=LogicalMeasurementId(version.logical_measurement_id),
                measurement_version_id=MeasurementVersionId(version_id),
                rule_version_id=rule.version_id,
                evidence_fingerprint=evidence,
            )
        )
    return tuple(cases)


def _preferred_daily_weight_conflicts(
    version_by_id: dict[str, MeasurementVersionFact],
    measurements: tuple[ResolvedMeasurement, ...],
) -> tuple[OpenDataReviewCase, ...]:
    by_day: dict[date, list[tuple[MeasurementVersionFact, ResolvedMeasurement]]] = defaultdict(list)
    for measurement in measurements:
        if measurement.effective_value is None:
            continue
        version = version_by_id[measurement.selected_measurement_version_id]
        if version.canonical_type == "body_mass":
            by_day[version.measurement_local_date].append((version, measurement))
    cases = []
    for day, candidates in sorted(by_day.items()):
        latest_at = max(
            datetime.fromisoformat(version.source_start_utc) for version, _ in candidates
        )
        latest = tuple(
            (version, measurement)
            for version, measurement in candidates
            if datetime.fromisoformat(version.source_start_utc) == latest_at
        )
        if len({measurement.effective_value for _, measurement in latest}) < 2:
            continue
        evidence = hashlib.sha256(
            repr(
                tuple(
                    sorted(
                        (version.measurement_version_id, measurement.effective_value)
                        for version, measurement in latest
                    )
                )
            ).encode()
        ).hexdigest()
        version, _ = min(
            latest,
            key=lambda item: (
                item[1].effective_value,
                item[0].measurement_version_id,
            ),
        )
        cases.append(
            OpenDataReviewCase(
                review_case_id=hashlib.sha256(
                    f"preferred_daily_weight:{day}:{evidence}".encode()
                ).hexdigest()[:32],
                kind="preferred_daily_weight_conflict",
                logical_measurement_id=LogicalMeasurementId(version.logical_measurement_id),
                measurement_version_id=MeasurementVersionId(version.measurement_version_id),
                rule_version_id=None,
                evidence_fingerprint=evidence,
            )
        )
    return tuple(cases)


def evaluate_plausibility_cases(
    versions: tuple[MeasurementVersionFact, ...],
    measurements: tuple[ResolvedMeasurement, ...],
    measurement_version_ids: tuple[str, ...],
    rules: tuple[PlausibilityRuleRecord, ...],
) -> tuple[OpenDataReviewCase, ...]:
    return _plausibility_cases(
        {version.measurement_version_id: version for version in versions},
        measurements,
        measurement_version_ids,
        rules,
    )


def plausibility_rule_recommendations() -> tuple[PlausibilityRuleRecord, ...]:
    return (
        *(replace(rule, recommendation_id="builtin-plausibility/v1") for rule in _FIXED_RULES),
        *_NUTRITION_RECOMMENDATIONS,
        PlausibilityRuleRecord(
            "fixed-plausibility/v1",
            "body_mass",
            "kg",
            1.0,
            None,
            True,
            None,
            datetime(1970, 1, 1, tzinfo=UTC),
            "builtin-plausibility/v1",
        ),
    )


def load_plausibility_rule_state(store: LocalStore) -> tuple[PlausibilityRuleRecord, ...]:
    return store.load_plausibility_rule_versions()


def create_plausibility_rule_version(
    store: LocalStore,
    *,
    operation_id: OperationId,
    version_id: str,
    data_type: str,
    unit: str,
    fixed_lower_bound: float | None,
    fixed_upper_bound: float | None,
    personal_range_enabled: bool,
    effective_from: datetime | None,
    recommendation_id: str | None,
) -> tuple[PlausibilityRuleRecord, SnapshotId | None]:
    timezone_name = (
        None
        if effective_from is None
        else getattr(effective_from.tzinfo, "key", effective_from.tzname())
    )
    offset = None if effective_from is None else effective_from.utcoffset()
    _active, versions, measurements = store.load_active_plausibility_facts()
    selected_ids = {
        measurement.selected_measurement_version_id
        for measurement in measurements
        if measurement.disposition.startswith("included")
        and measurement.effective_value is not None
    }
    target_ids = tuple(
        version.measurement_version_id
        for version in versions
        if version.measurement_version_id in selected_ids
        and version.canonical_type == data_type
        and (
            effective_from is None
            or datetime.fromisoformat(version.source_start_utc) >= effective_from
        )
    )
    cases = evaluate_plausibility_cases(
        versions,
        measurements,
        target_ids,
        (
            *store.load_plausibility_rule_versions(),
            PlausibilityRuleRecord(
                version_id=version_id,
                data_type=data_type,
                unit=unit,
                fixed_lower_bound=fixed_lower_bound,
                fixed_upper_bound=fixed_upper_bound,
                personal_range_enabled=personal_range_enabled,
                effective_from=effective_from,
                created_at=datetime.now(UTC),
                recommendation_id=recommendation_id,
                effective_timezone=timezone_name,
                effective_offset_minutes=(
                    None if offset is None else int(offset.total_seconds() // 60)
                ),
            ),
        ),
    )
    return store.create_plausibility_rule_version(
        operation_id=operation_id,
        version_id=version_id,
        data_type=data_type,
        unit=unit,
        fixed_lower_bound=fixed_lower_bound,
        fixed_upper_bound=fixed_upper_bound,
        personal_range_enabled=personal_range_enabled,
        effective_from=effective_from,
        effective_timezone=timezone_name,
        effective_offset_minutes=(None if offset is None else int(offset.total_seconds() // 60)),
        recommendation_id=recommendation_id,
        replaced_measurement_version_ids=target_ids,
        cases=cases,
        cycle_status="open" if cases else "closed",
    )


class DataQualityError(ValueError):
    """A data-quality operation cannot preserve its requested basis."""


@dataclass(frozen=True, slots=True)
class HistoricalReviewRequest:
    operation_id: OperationId
    data_type: str
    start_date: date
    end_date: date
    rule_version_id: str
    base_snapshot_id: SnapshotId


def run_historical_review(
    store: LocalStore, request: HistoricalReviewRequest
) -> HistoricalReviewResult:
    active, versions, measurements = store.load_active_plausibility_facts()
    if active != request.base_snapshot_id:
        raise DataQualityError("Historische Reproduktionsbasis ist nicht mehr aktiv.")
    rule = next(
        (
            item
            for item in store.load_plausibility_rule_versions()
            if item.data_type == request.data_type and item.version_id == request.rule_version_id
        ),
        None,
    )
    if rule is None:
        raise DataQualityError("Historische Regelversion fehlt.")
    effective_ids = {
        item.selected_measurement_version_id
        for item in measurements
        if item.disposition.startswith("included") and item.effective_value is not None
    }
    target_ids = tuple(
        item.measurement_version_id
        for item in versions
        if item.measurement_version_id in effective_ids
        and item.canonical_type == request.data_type
        and request.start_date <= item.measurement_local_date <= request.end_date
    )
    confirmed_case_ids = store.load_effective_confirmation_case_ids()
    reproduced_cases = evaluate_plausibility_cases(
        versions,
        measurements,
        target_ids,
        (replace(rule, effective_from=None),),
    )
    open_cases = tuple(
        case for case in reproduced_cases if case.review_case_id not in confirmed_case_ids
    )
    return store.publish_historical_review(
        HistoricalReviewPublication(
            operation_id=request.operation_id,
            base_snapshot_id=request.base_snapshot_id,
            start_date=request.start_date,
            end_date=request.end_date,
            rule_version_id=request.rule_version_id,
            reproduced_cases=reproduced_cases,
            open_cases=open_cases,
            cycle_status="open" if open_cases else "closed",
        )
    )


def close_review_cycle_updates(
    store: LocalStore, review_case_id: str
) -> tuple[ReviewCycleUpdate, ...]:
    return tuple(
        ReviewCycleUpdate(
            cycle.cycle_id,
            cycle.open_case_count - 1,
            "closed" if cycle.open_case_count == 1 else "open",
        )
        for cycle in store.load_review_cycles_for_case(review_case_id)
        if cycle.status == "open"
    )


def close_review_batch_cycle_updates(
    store: LocalStore, review_case_ids: tuple[str, ...]
) -> tuple[ReviewCycleUpdate, ...]:
    affected: dict[StoredReviewCycleId, tuple[ReviewCycleRecord, int]] = {}
    for review_case_id in review_case_ids:
        for cycle in store.load_review_cycles_for_case(review_case_id):
            current, count = affected.get(cycle.cycle_id, (cycle, 0))
            affected[cycle.cycle_id] = (current, count + 1)
    return tuple(
        ReviewCycleUpdate(
            cycle.cycle_id,
            cycle.open_case_count - count,
            "closed" if cycle.open_case_count == count else "open",
        )
        for cycle, count in affected.values()
        if cycle.status == "open"
    )


def reopen_review_cycle_updates(
    store: LocalStore, review_case_id: str
) -> tuple[ReviewCycleUpdate, ...]:
    return tuple(
        ReviewCycleUpdate(cycle.cycle_id, cycle.open_case_count + 1, "open")
        for cycle in store.load_review_cycles_for_case(review_case_id)
    )


def reopen_decision_cycle_updates(
    store: LocalStore, decision_id: str
) -> tuple[ReviewCycleUpdate, ...]:
    review_case_id = store.load_effective_decision_case_id(decision_id)
    if review_case_id is None:
        return ()
    return reopen_review_cycle_updates(store, review_case_id)


def reopen_batch_decision_cycle_updates(
    store: LocalStore, decision_ids: tuple[str, ...]
) -> tuple[ReviewCycleUpdate, ...]:
    case_ids = tuple(
        case_id
        for decision_id in decision_ids
        if (case_id := store.load_effective_decision_case_id(decision_id)) is not None
    )
    affected: dict[StoredReviewCycleId, tuple[ReviewCycleRecord, int]] = {}
    for case_id in case_ids:
        for cycle in store.load_review_cycles_for_case(case_id):
            current, count = affected.get(cycle.cycle_id, (cycle, 0))
            affected[cycle.cycle_id] = (current, count + 1)
    return tuple(
        ReviewCycleUpdate(cycle.cycle_id, cycle.open_case_count + count, "open")
        for cycle, count in affected.values()
    )


def _unknown_rule_case(source_type: str) -> OpenDataReviewCase:
    case_key = f"rule_definition:{source_type}"
    return OpenDataReviewCase(
        review_case_id=hashlib.sha256(case_key.encode()).hexdigest()[:32],
        kind="rule_definition",
        logical_measurement_id=None,
        measurement_version_id=None,
        rule_version_id=None,
        evidence_fingerprint=hashlib.sha256(f"{case_key}:evidence".encode()).hexdigest(),
    )


def _source_deletions(
    occurrences: tuple[SourceOccurrenceFact, ...],
    version_by_id: dict[str, MeasurementVersionFact],
    exports: tuple[ExportFact, ...],
    previous_review_cases: tuple[OpenDataReviewCase, ...],
    governing_export_id: str,
    suppressed_deletion_ids: frozenset[str],
) -> tuple[OpenDataReviewCase, ...]:
    export_by_id = {item.export_id: item for item in exports}
    governing = export_by_id[governing_export_id]
    governing_date = governing.export_date
    if governing_date is None:
        return ()
    present = {
        item.logical_measurement_id for item in occurrences if item.export_id == governing_export_id
    }
    previous = {
        str(item.logical_measurement_id): item
        for item in previous_review_cases
        if item.kind == "suspected_source_deletion" and item.logical_measurement_id is not None
    }
    conflicted_logical_ids: set[str] = set()
    identities: dict[tuple[str, str, str, str, str], set[str]] = defaultdict(set)
    for occurrence in occurrences:
        version = version_by_id[occurrence.measurement_version_id]
        identities[
            (
                version.canonical_type,
                version.source_start_utc,
                version.source_end_utc,
                version.source_name,
                version.device,
            )
        ].add(occurrence.logical_measurement_id)
    for logical_ids in identities.values():
        if len(logical_ids) > 1:
            conflicted_logical_ids.update(logical_ids)
    missing: list[OpenDataReviewCase] = []

    def is_earlier(export_id: str) -> bool:
        export_date = export_by_id[export_id].export_date
        return export_date is not None and export_date < governing_date

    for logical_id in sorted({item.logical_measurement_id for item in occurrences} - present):
        if logical_id in suppressed_deletion_ids:
            continue
        if logical_id in conflicted_logical_ids:
            continue
        earlier = [
            export_by_id[item.export_id]
            for item in occurrences
            if item.logical_measurement_id == logical_id and is_earlier(item.export_id)
        ]
        if not earlier:
            continue
        last_present = max(earlier, key=lambda item: (item.export_date, item.export_id))
        if last_present.covered_types != governing.covered_types:
            continue
        if logical_id in previous:
            missing.append(previous[logical_id])
            continue
        phase = f"suspected_source_deletion:{logical_id}:{governing_export_id}"
        missing.append(
            OpenDataReviewCase(
                review_case_id=hashlib.sha256(phase.encode()).hexdigest()[:32],
                kind="suspected_source_deletion",
                logical_measurement_id=LogicalMeasurementId(logical_id),
                measurement_version_id=None,
                rule_version_id=None,
                evidence_fingerprint=hashlib.sha256(f"{phase}:evidence".encode()).hexdigest(),
            )
        )
    return tuple(missing)


def _activity_overlap_conflicts(
    version_by_id: dict[str, MeasurementVersionFact],
    measurements: tuple[ResolvedMeasurement, ...],
) -> tuple[OpenDataReviewCase, ...]:
    activity_types = {
        "apple_exercise_time",
        "step_count",
        "walking_running_distance",
        "active_energy",
    }
    grouped: dict[tuple[str, str], list[MeasurementVersionFact]] = defaultdict(list)
    for measurement in measurements:
        if measurement.effective_value is None:
            continue
        version = version_by_id[measurement.selected_measurement_version_id]
        if version.canonical_type not in activity_types:
            continue
        source_class = classify_activity_source(version.source_name, version.device).value
        grouped[(version.canonical_type, source_class)].append(version)
    cases = []
    for (data_type, source_class), versions in sorted(grouped.items()):
        active: MeasurementVersionFact | None = None
        active_end: datetime | None = None
        for version in sorted(
            versions,
            key=lambda item: (
                item.source_start_utc,
                item.source_end_utc,
                item.measurement_version_id,
            ),
        ):
            start = datetime.fromisoformat(version.source_start_utc)
            end = datetime.fromisoformat(version.source_end_utc)
            if start >= end:
                continue
            if (
                active is not None
                and active_end is not None
                and start < active_end
                and active.logical_measurement_id != version.logical_measurement_id
            ):
                logical_ids = tuple(
                    sorted((active.logical_measurement_id, version.logical_measurement_id))
                )
                case_key = f"activity_overlap:{data_type}:{source_class}:{':'.join(logical_ids)}"
                cases.append(
                    OpenDataReviewCase(
                        review_case_id=hashlib.sha256(case_key.encode()).hexdigest()[:32],
                        kind="source_conflict",
                        logical_measurement_id=LogicalMeasurementId(logical_ids[0]),
                        measurement_version_id=None,
                        rule_version_id=None,
                        evidence_fingerprint=hashlib.sha256(
                            f"{case_key}:evidence".encode()
                        ).hexdigest(),
                    )
                )
            if active_end is None or end > active_end:
                active, active_end = version, end
    return tuple(cases)


def _source_conflicts(
    occurrences: tuple[SourceOccurrenceFact, ...],
    version_by_id: dict[str, MeasurementVersionFact],
    resolved_natural_keys: set[tuple[str, str, str, str, str]],
) -> tuple[OpenDataReviewCase, ...]:
    joined = tuple((item, version_by_id[item.measurement_version_id]) for item in occurrences)
    collisions: set[tuple[str, str, str | None]] = set()

    natural_groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for occurrence, version in joined:
        if (
            version.strong_source_id_hash is None
            and _natural_identity(version) not in resolved_natural_keys
        ):
            natural_groups[(occurrence.logical_measurement_id, occurrence.export_id)].add(
                occurrence.measurement_version_id
            )
    for (logical_id, export_id), version_ids in natural_groups.items():
        if len(version_ids) > 1:
            collisions.add((logical_id, logical_id, export_id))

    identity_groups: dict[tuple[str, str, str, str, str], list[tuple[str, str]]] = defaultdict(list)
    for occurrence, version in joined:
        natural_key = _natural_identity(version)
        if natural_key in resolved_natural_keys:
            continue
        identity_groups[natural_key].append(
            (
                occurrence.logical_measurement_id,
                version.strong_source_id_hash or "natural",
            )
        )
    for natural_key, identities in identity_groups.items():
        if len({identity for _, identity in identities}) > 1:
            collision_key = hashlib.sha256(":".join(natural_key).encode()).hexdigest()
            collisions.add((min(logical_id for logical_id, _ in identities), collision_key, None))

    return tuple(
        _source_conflict(logical_id, collision_key, export_id)
        for logical_id, collision_key, export_id in sorted(collisions)
    )


def _source_conflict(
    logical_id: str, collision_key: str, export_id: str | None
) -> OpenDataReviewCase:
    scope = "all_exports" if export_id is None else export_id
    case_key = f"source_conflict:{collision_key}:{scope}"
    return OpenDataReviewCase(
        review_case_id=hashlib.sha256(case_key.encode()).hexdigest()[:32],
        kind="source_conflict",
        logical_measurement_id=LogicalMeasurementId(logical_id),
        measurement_version_id=None,
        rule_version_id=None,
        evidence_fingerprint=hashlib.sha256(f"{case_key}:evidence".encode()).hexdigest(),
    )


def load_review_state(
    store: LocalStore,
) -> tuple[
    SnapshotId | None,
    tuple[OpenDataReviewCase, ...],
    tuple[ReviewCycleRecord, ...],
]:
    """Load the active review projection through its owning module."""
    return (
        store.load_active_snapshot_id(),
        store.load_open_data_review_cases(),
        store.load_review_cycles(),
    )


def load_review_case_detail(store: LocalStore, review_case_id: str) -> ReviewCaseDetail:
    case = next(
        (
            item
            for item in store.load_open_data_review_cases()
            if item.review_case_id == review_case_id
        ),
        None,
    )
    if case is None:
        raise ValueError("Datenprüffall ist nicht offen.")
    if case.kind == "rule_definition":
        return ReviewCaseDetail(
            source_type=store.load_source_type_for_review_case(review_case_id),
            measured_at=None,
            effective_value=None,
            effective_value_source=None,
            reasons=(),
            canonical_unit=None,
        )
    if case.kind == "continued_override" and case.measurement_version_id is not None:
        version = store.load_measurement_version_fact(case.measurement_version_id)
        if version is None:
            raise ValueError("Quellmessungsversion des Datenprüffalls fehlt.")
        rule = _rule_at(
            version.canonical_type,
            datetime.fromisoformat(version.source_start_utc),
            store.load_plausibility_rule_versions(),
        )
        resolved = store.load_resolved_measurement(version.logical_measurement_id)
        reasons = (
            []
            if rule is None
            else list(_plausibility_reasons(rule, version.canonical_value, version.canonical_unit))
        )
        if rule is not None and version.canonical_type == "apple_resting_heart_rate":
            series = next(
                (
                    item
                    for item in store.load_daily_series(
                        version.measurement_local_date - timedelta(days=42),
                        version.measurement_local_date,
                    )
                    if item.data_type.value == "apple_resting_heart_rate"
                ),
                None,
            )
            personal_bounds = _personal_bounds(
                rule,
                version.measurement_local_date,
                () if series is None else tuple((item.day, item.value) for item in series.values),
                version.canonical_unit,
            )
            reasons.extend(
                reason
                for reason in _plausibility_reasons(
                    rule,
                    version.canonical_value,
                    version.canonical_unit,
                    personal_bounds,
                )
                if reason.code
                in {
                    "below_personal_lower_bound",
                    "above_personal_upper_bound",
                }
            )
        return ReviewCaseDetail(
            source_type=version.canonical_type,
            measured_at=datetime.fromisoformat(version.source_start_utc),
            effective_value=None if resolved is None else resolved.effective_value,
            effective_value_source=(None if resolved is None else resolved.effective_value_source),
            reasons=tuple(reasons),
            canonical_unit=version.canonical_unit,
        )
    if case.kind != "plausibility" or case.measurement_version_id is None:
        return ReviewCaseDetail(None, None, None, None, (), None)
    version = store.load_measurement_version_fact(case.measurement_version_id)
    if version is None:
        raise ValueError("Quellmessungsversion des Datenprüffalls fehlt.")
    rule = next(
        (
            item
            for item in store.load_plausibility_rule_versions()
            if item.data_type == version.canonical_type and item.version_id == case.rule_version_id
        ),
        None,
    )
    if rule is None:
        raise ValueError("Plausibilitätsregel des Datenprüffalls fehlt.")
    resolved = store.load_resolved_measurement(version.logical_measurement_id)
    effective_value = (
        version.canonical_value
        if resolved is None or resolved.effective_value is None
        else resolved.effective_value
    )
    reasons = list(_plausibility_reasons(rule, effective_value, version.canonical_unit))
    if version.canonical_type == "apple_resting_heart_rate":
        series = next(
            (
                item
                for item in store.load_daily_series(
                    version.measurement_local_date - timedelta(days=42),
                    version.measurement_local_date,
                )
                if item.data_type.value == "apple_resting_heart_rate"
            ),
            None,
        )
        daily_values = (
            () if series is None else tuple((item.day, item.value) for item in series.values)
        )
        is_effective_day_value = series is not None and any(
            item.day == version.measurement_local_date
            and MeasurementVersionId(version.measurement_version_id) in item.measurement_version_ids
            for item in series.values
        )
        if is_effective_day_value and resolved is not None and resolved.effective_value is not None:
            personal_bounds = _personal_bounds(
                rule, version.measurement_local_date, daily_values, version.canonical_unit
            )
            reasons.extend(
                reason
                for reason in _plausibility_reasons(
                    rule,
                    resolved.effective_value,
                    version.canonical_unit,
                    personal_bounds,
                )
                if reason.code in {"below_personal_lower_bound", "above_personal_upper_bound"}
            )
    return ReviewCaseDetail(
        source_type=version.canonical_type,
        measured_at=datetime.fromisoformat(version.source_start_utc),
        effective_value=None if resolved is None else resolved.effective_value,
        effective_value_source=None if resolved is None else resolved.effective_value_source,
        reasons=tuple(reasons),
        canonical_unit=version.canonical_unit,
    )


def _snapshot_review_detail(
    store: LocalStore,
    case: OpenDataReviewCase,
    versions: tuple[MeasurementVersionFact, ...],
    measurements: tuple[ResolvedMeasurement, ...],
    rules: tuple[PlausibilityRuleRecord, ...],
) -> ReviewCaseDetail:
    if case.kind == "rule_definition":
        return ReviewCaseDetail(
            store.load_source_type_for_review_case(case.review_case_id),
            None,
            None,
            None,
            (),
            None,
        )
    if case.measurement_version_id is None or case.kind not in {
        "plausibility",
        "continued_override",
    }:
        return ReviewCaseDetail(None, None, None, None, (), None)
    version_by_id = {item.measurement_version_id: item for item in versions}
    version = version_by_id.get(str(case.measurement_version_id))
    if version is None:
        raise ValueError("Quellmessungsversion des Datenprüffalls fehlt.")
    resolved = next(
        (
            item
            for item in measurements
            if item.logical_measurement_id == version.logical_measurement_id
        ),
        None,
    )
    rule = (
        _rule_at(version.canonical_type, datetime.fromisoformat(version.source_start_utc), rules)
        if case.kind == "continued_override"
        else next(
            (
                item
                for item in rules
                if item.data_type == version.canonical_type
                and item.version_id == case.rule_version_id
            ),
            None,
        )
    )
    if rule is None:
        raise ValueError("Plausibilitätsregel des Datenprüffalls fehlt.")
    effective_value = (
        version.canonical_value
        if resolved is None or resolved.effective_value is None
        else resolved.effective_value
    )
    reason_value = version.canonical_value if case.kind == "continued_override" else effective_value
    reasons = list(_plausibility_reasons(rule, reason_value, version.canonical_unit))
    if version.canonical_type == "apple_resting_heart_rate":
        daily = _effective_daily_resting_hr(version_by_id, measurements)
        daily_values = tuple(
            (day, measurement.effective_value)
            for day, (_, measurement) in sorted(daily.items())
            if measurement.effective_value is not None
        )
        selected_on_day = daily.get(version.measurement_local_date)
        eligible = case.kind == "continued_override" or (
            selected_on_day is not None
            and selected_on_day[1].selected_measurement_version_id == version.measurement_version_id
        )
        if eligible:
            personal_bounds = _personal_bounds(
                rule,
                version.measurement_local_date,
                daily_values,
                version.canonical_unit,
            )
            reasons.extend(
                reason
                for reason in _plausibility_reasons(
                    rule, reason_value, version.canonical_unit, personal_bounds
                )
                if reason.code in {"below_personal_lower_bound", "above_personal_upper_bound"}
            )
    return ReviewCaseDetail(
        version.canonical_type,
        datetime.fromisoformat(version.source_start_utc),
        None if resolved is None else resolved.effective_value,
        None if resolved is None else resolved.effective_value_source,
        tuple(reasons),
        version.canonical_unit,
    )


def load_review_backup_facts(store: LocalStore) -> tuple[ReviewBackupFact, ...]:
    """Freeze all historical and active review facts through data-quality policy."""
    rules = store.load_plausibility_rule_versions()
    return tuple(
        ReviewBackupFact(
            snapshot_id=snapshot.snapshot_id,
            review_case_id=case.review_case_id,
            case_kind=case.kind,
            logical_measurement_id=case.logical_measurement_id,
            measurement_version_id=case.measurement_version_id,
            rule_version_id=case.rule_version_id,
            evidence_fingerprint=case.evidence_fingerprint,
            source_type=detail.source_type,
            measured_at_utc=detail.measured_at,
            effective_value=detail.effective_value,
            effective_value_source=detail.effective_value_source,
            canonical_unit=detail.canonical_unit,
            reasons=detail.reasons,
        )
        for snapshot in store.load_review_snapshot_facts()
        for case in sorted(snapshot.cases, key=lambda item: item.review_case_id)
        for detail in (
            _snapshot_review_detail(store, case, snapshot.versions, snapshot.measurements, rules),
        )
    )


__all__ = [
    "ReviewBackupFact",
    "ReviewCaseDetail",
    "ReviewReason",
    "load_review_backup_facts",
    "load_review_case_detail",
    "load_review_state",
    "resolve_sources",
    "select_governing_export",
]
