"""Registered adjacent store-schema migration policy."""

from personal_health_lab.storage import MigrationRollbackFacts, current_store_schema_version

_REGISTERED_STEPS = ((1, 2), (2, 3), (3, 4))


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
    steps: list[tuple[int, int]] = []
    current = source
    while current < target:
        step = next((item for item in _REGISTERED_STEPS if item[0] == current), None)
        if step is None or step[1] != current + 1:
            return target, (), ("missing_migration_step",)
        steps.append(step)
        current = step[1]
    return target, tuple(steps), ()


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


__all__ = ["plan_migration_rollback", "plan_store_migration"]
