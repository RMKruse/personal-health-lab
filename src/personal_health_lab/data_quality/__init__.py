"""Source resolution and data-review policy."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime

from personal_health_lab.health_data import LogicalMeasurementId
from personal_health_lab.storage import (
    ExportFact,
    LocalStore,
    MeasurementVersionFact,
    OpenDataReviewCase,
    ResolvedMeasurement,
    SnapshotId,
    SourceOccurrenceFact,
    SourceResolution,
)


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

    overrides = {
        item.logical_measurement_id: item
        for item in previous_measurements
        if item.disposition != "included_source"
    }
    measurements = tuple(
        sorted(
            (
                *overrides.values(),
                *(item for item in current if item.logical_measurement_id not in overrides),
            ),
            key=lambda item: item.logical_measurement_id,
        )
    )

    new_cases = _source_conflicts(occurrences, version_by_id)
    cases_by_id = {item.review_case_id: item for item in previous_review_cases}
    cases_by_id.update((item.review_case_id, item) for item in new_cases)
    return SourceResolution(
        measurements=measurements,
        review_cases=tuple(cases_by_id[key] for key in sorted(cases_by_id)),
    )


def _source_conflicts(
    occurrences: tuple[SourceOccurrenceFact, ...],
    version_by_id: dict[str, MeasurementVersionFact],
) -> tuple[OpenDataReviewCase, ...]:
    joined = tuple((item, version_by_id[item.measurement_version_id]) for item in occurrences)
    collisions: set[tuple[str, str, str | None]] = set()

    natural_groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for occurrence, version in joined:
        if version.strong_source_id_hash is None:
            natural_groups[(occurrence.logical_measurement_id, occurrence.export_id)].add(
                occurrence.measurement_version_id
            )
    for (logical_id, export_id), version_ids in natural_groups.items():
        if len(version_ids) > 1:
            collisions.add((logical_id, logical_id, export_id))

    identity_groups: dict[tuple[str, str, str, str, str], list[tuple[str, str]]] = defaultdict(list)
    for occurrence, version in joined:
        natural_key = (
            version.canonical_type,
            version.source_start_utc,
            version.source_end_utc,
            version.source_name,
            version.device,
        )
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
) -> tuple[SnapshotId | None, tuple[OpenDataReviewCase, ...]]:
    """Load the active review projection through its owning module."""
    return store.load_active_snapshot_id(), store.load_open_data_review_cases()


__all__ = [
    "load_review_state",
    "resolve_sources",
    "select_governing_export",
]
