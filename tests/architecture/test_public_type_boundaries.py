import inspect
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, is_dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any, cast, get_args, get_origin, get_type_hints

import pytest

import personal_health_lab as package_root
import personal_health_lab.analysis as analysis
import personal_health_lab.application as application
import personal_health_lab.health_data as health_data
import personal_health_lab.health_import as health_import
import personal_health_lab.overview as overview_module
import personal_health_lab.recovery as recovery
import personal_health_lab.storage as storage
import personal_health_lab.synthetic_export as synthetic_export
from personal_health_lab.adapters import cli, dev_cli
from personal_health_lab.application import (
    ConfigurationError,
    DataMode,
    Overview,
    OverviewSelection,
    OverviewStatus,
    RuntimeConfig,
    WritePlanDetails,
    WriteRequest,
    WriteResult,
)


def _contains_any(annotation: object) -> bool:
    return annotation is Any or any(_contains_any(argument) for argument in get_args(annotation))


def _assert_callable_is_typed(value: object) -> None:
    signature = inspect.signature(value)
    type_hints = get_type_hints(value)
    assert "return" in type_hints
    assert not _contains_any(type_hints["return"])
    for parameter in signature.parameters.values():
        if parameter.name in {"self", "cls"}:
            continue
        assert parameter.name in type_hints
        assert not _contains_any(type_hints[parameter.name])


def _assert_module_exports_are_typed(module: ModuleType) -> None:
    for export_name in module.__all__:  # type: ignore[attr-defined]
        exported = getattr(module, export_name)
        if inspect.isfunction(exported):
            _assert_callable_is_typed(exported)
        elif inspect.isclass(exported):
            for annotation in get_type_hints(exported).values():
                assert not _contains_any(annotation)
            for method_name, method in inspect.getmembers(exported, inspect.isfunction):
                is_public_method = not method_name.startswith("_")
                if method.__qualname__.startswith(exported.__name__) and is_public_method:
                    _assert_callable_is_typed(method)


def test_public_module_exports_are_fully_typed_without_any() -> None:
    for module in (
        package_root,
        analysis,
        application,
        health_data,
        health_import,
        overview_module,
        recovery,
        storage,
        synthetic_export,
        cli,
        dev_cli,
    ):
        _assert_module_exports_are_typed(module)


def test_public_overview_values_are_immutable() -> None:
    overview = Overview(
        status=OverviewStatus.EMPTY,
        selection=OverviewSelection(),
        message="leer",
    )

    with pytest.raises(FrozenInstanceError):
        overview.message = "verändert"  # type: ignore[misc]


def test_public_application_values_are_immutable_and_storage_neutral() -> None:
    projection_methods = (
        "load_analysis_catalog",
        "load_data_review",
        "load_data_review_case",
        "load_activity_days",
        "load_migration_diagnostics",
        "load_overview",
        "load_plausibility_rules",
        "load_recovery_status",
        "load_sleep_days",
        "load_workouts",
        "load_workspace_status",
    )
    request_values = set(get_args(WriteRequest))
    public_values = set(get_args(WritePlanDetails) + get_args(WriteResult))
    public_values.update(
        get_type_hints(getattr(application.HealthLab, method))["return"]
        for method in projection_methods
    )
    visited: set[tuple[object, bool]] = set()

    def inspect_boundary(value: object, *, allow_input_path: bool = False) -> None:
        visit = (value, allow_input_path)
        if visit in visited:
            return
        visited.add(visit)
        origin = get_origin(value)
        if origin is not None:
            assert origin not in {dict, list}
            assert not (isinstance(origin, type) and issubclass(origin, Mapping))
            for argument in get_args(value):
                inspect_boundary(argument, allow_input_path=allow_input_path)
            return
        if value in {str, int, float, bool, bytes, type(None), Any}:
            return
        if not inspect.isclass(value):
            return
        assert value is not Path or allow_input_path
        assert value.__module__.split(".", 1)[0] not in {
            "duckdb",
            "pandas",
            "pyarrow",
            "sqlite3",
        }
        assert value.__name__.lower() not in {"dataframe", "repository", "row", "table", "port"}
        assert not value.__name__.endswith("Repository")
        if issubclass(value, Enum):
            return
        if not value.__module__.startswith("personal_health_lab"):
            return
        assert is_dataclass(value), f"mutable or opaque public boundary value: {value}"
        assert value.__dataclass_params__.frozen
        for annotation in get_type_hints(value).values():
            inspect_boundary(annotation, allow_input_path=allow_input_path)

    for public_value in public_values:
        inspect_boundary(public_value)
    for request_value in request_values:
        inspect_boundary(request_value, allow_input_path=True)


def test_external_runtime_config_is_validated_at_runtime(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        RuntimeConfig(
            mode=cast(DataMode, "mixed"),
            synthetic_store=tmp_path / "synthetic",
            real_store=tmp_path / "real",
        )
