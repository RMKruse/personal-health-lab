"""Registered adjacent store-schema migration policy."""

from personal_health_lab.storage import current_store_schema_version

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


__all__ = ["plan_store_migration"]
