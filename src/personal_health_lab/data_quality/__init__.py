"""Source resolution and data-review policy."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Literal

from personal_health_lab.health_data import LogicalMeasurementId, MeasurementVersionId
from personal_health_lab.storage import (
    ExportFact,
    LocalStore,
    MeasurementVersionFact,
    OpenDataReviewCase,
    ResolvedMeasurement,
    ReviewCaseId,
    ReviewCycleRecord,
    SnapshotId,
    SourceOccurrenceFact,
    SourceResolution,
    SourceTypeRuleRequest,
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


@dataclass(frozen=True, slots=True)
class _FixedPlausibilityRule:
    version_id: str
    source_type: str
    effective_from: datetime
    lower_bound: float
    upper_bound: float | None


_FIXED_RULES = (
    _FixedPlausibilityRule(
        "fixed-plausibility/v1",
        "apple_resting_heart_rate",
        datetime.min.replace(tzinfo=UTC),
        20.0,
        250.0,
    ),
    _FixedPlausibilityRule(
        "fixed-plausibility/v1",
        "active_energy",
        datetime.min.replace(tzinfo=UTC),
        0.0,
        None,
    ),
)


def _rule_at(source_type: str, measured_at: datetime) -> _FixedPlausibilityRule | None:
    return max(
        (
            rule
            for rule in _FIXED_RULES
            if rule.source_type == source_type and rule.effective_from <= measured_at
        ),
        key=lambda rule: rule.effective_from,
        default=None,
    )


def _plausibility_reasons(
    rule: _FixedPlausibilityRule,
    value: float,
    unit: str,
    personal_bounds: tuple[float, float] | None = None,
) -> tuple[ReviewReason, ...]:
    reasons = []
    if value < rule.lower_bound:
        reasons.append(
            ReviewReason("below_fixed_lower_bound", rule.lower_bound, rule.upper_bound, unit)
        )
    if rule.upper_bound is not None and value > rule.upper_bound:
        reasons.append(
            ReviewReason("above_fixed_upper_bound", rule.lower_bound, rule.upper_bound, unit)
        )
    if personal_bounds is not None and value < personal_bounds[0]:
        reasons.append(ReviewReason("below_personal_lower_bound", *personal_bounds, unit))
    if personal_bounds is not None and value > personal_bounds[1]:
        reasons.append(ReviewReason("above_personal_upper_bound", *personal_bounds, unit))
    return tuple(reasons)


def _personal_bounds(
    rule: _FixedPlausibilityRule,
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

    generated_cases = (
        *_source_conflicts(occurrences, version_by_id, conflict_keys),
        *_source_deletions(
            occurrences,
            version_by_id,
            exports,
            previous_review_cases,
            governing_export_id,
            suppressed_deletion_ids,
        ),
        *_plausibility_cases(version_by_id, measurements, imported_measurement_version_ids),
        *(_unknown_rule_case(source_type) for source_type in sorted(set(unknown_source_types))),
    )
    previous_case_ids = {item.review_case_id for item in previous_review_cases}
    cases_by_id = {
        item.review_case_id: item
        for item in previous_review_cases
        if item.kind != "suspected_source_deletion"
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


def _plausibility_cases(
    version_by_id: dict[str, MeasurementVersionFact],
    measurements: tuple[ResolvedMeasurement, ...],
    imported_measurement_version_ids: tuple[str, ...],
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
        rule = _rule_at(version.canonical_type, measured_at)
        if rule is None:
            continue
        reasons = list(_plausibility_reasons(rule, version.canonical_value, version.canonical_unit))
        measurement = measurement_by_version.get(version_id)
        selected = daily.get(version.measurement_local_date)
        if (
            version.canonical_type == "apple_resting_heart_rate"
            and measurement is not None
            and measurement.effective_value is not None
            and selected is not None
            and selected[0].measurement_version_id == version_id
        ):
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
                    measurement.effective_value,
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
        )
    if case.kind != "plausibility" or case.measurement_version_id is None:
        return ReviewCaseDetail(None, None, None, None, ())
    version = store.load_measurement_version_fact(case.measurement_version_id)
    if version is None:
        raise ValueError("Quellmessungsversion des Datenprüffalls fehlt.")
    rule = _rule_at(version.canonical_type, datetime.fromisoformat(version.source_start_utc))
    if rule is None or rule.version_id != case.rule_version_id:
        raise ValueError("Plausibilitätsregel des Datenprüffalls fehlt.")
    resolved = store.load_resolved_measurement(version.logical_measurement_id)
    reasons = list(_plausibility_reasons(rule, version.canonical_value, version.canonical_unit))
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
    )


__all__ = [
    "ReviewCaseDetail",
    "ReviewReason",
    "load_review_case_detail",
    "load_review_state",
    "resolve_sources",
    "select_governing_export",
]
