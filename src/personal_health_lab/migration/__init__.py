"""Registered adjacent store-schema migration policy."""

from personal_health_lab.storage import (
    MigrationRollbackFacts,
    current_snapshot_schema_version,
    current_store_schema_version,
)

_REGISTERED_STEPS = (
    (1, 2),
    (2, 3),
    (3, 4),
    (4, 5),
    (5, 6),
    (6, 7),
    (7, 8),
    (8, 9),
    (9, 10),
    (10, 11),
    (11, 12),
)
_REGISTERED_SNAPSHOT_STEPS = ((1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7))
_REGISTERED_BACKUP_STEPS = ((1, 2), (2, 3), (3, 4))


def _plan_adjacent_migration(
    source: int,
    target: int,
    registered_steps: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...] | None:
    steps: list[tuple[int, int]] = []
    current = source
    while current < target:
        step = next((item for item in registered_steps if item[0] == current), None)
        if step is None or step[1] != current + 1:
            return None
        steps.append(step)
        current = step[1]
    return tuple(steps)


def plan_store_migration(
    source_version: str,
) -> tuple[int, tuple[tuple[int, int], ...], tuple[str, ...]]:
    target = current_store_schema_version()
    try:
        source = int(source_version)
    except ValueError:
        return target, (), ("invalid_schema_version",)
    if source <= 0:
        return target, (), ("invalid_schema_version",)
    if source == target:
        return target, (), ()
    if source > target:
        return target, (), ("newer_schema",)
    steps = _plan_adjacent_migration(source, target, _REGISTERED_STEPS)
    if steps is None:
        return target, (), ("missing_migration_step",)
    return target, steps, ()


def plan_backup_migration(
    source_version: int,
    target_version: int,
) -> tuple[tuple[int, int], ...] | None:
    """Return the registered backup-schema path, or ``None`` when unavailable."""

    if source_version <= 0 or source_version > target_version:
        return None
    return _plan_adjacent_migration(source_version, target_version, _REGISTERED_BACKUP_STEPS)


def plan_snapshot_migration(
    source_version: int,
) -> tuple[int, tuple[tuple[int, int], ...], tuple[str, ...]]:
    target = current_snapshot_schema_version()
    if source_version <= 0:
        return target, (), ("invalid_snapshot_schema_version",)
    if source_version == target:
        return target, (), ()
    if source_version > target:
        return target, (), ("newer_snapshot_schema",)
    steps = _plan_adjacent_migration(source_version, target, _REGISTERED_SNAPSHOT_STEPS)
    if steps is None:
        return target, (), ("missing_snapshot_migration_step",)
    return target, steps, ()


def plan_migration_rollback(facts: MigrationRollbackFacts | None) -> tuple[str, ...]:
    if facts is None:
        return ("migration_rollback_unavailable",)
    if (
        facts.latest_state_change_operation_id != facts.migration_operation_id
        or facts.active_snapshot_id != facts.migrated_snapshot_id
    ):
        return ("migration_rollback_superseded",)
    if not facts.backup_exists:
        return ("migration_rollback_backup_missing",)
    if not facts.backup_matches_migration:
        return ("migration_rollback_backup_invalid",)
    return ()


__all__ = [
    "plan_backup_migration",
    "plan_migration_rollback",
    "plan_snapshot_migration",
    "plan_store_migration",
]
