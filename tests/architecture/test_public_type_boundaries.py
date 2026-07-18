import inspect
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import ModuleType
from typing import Any, cast, get_args, get_type_hints

import pytest

import personal_health_lab as package_root
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


def test_external_runtime_config_is_validated_at_runtime(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        RuntimeConfig(
            mode=cast(DataMode, "mixed"),
            synthetic_store=tmp_path / "synthetic",
            real_store=tmp_path / "real",
        )
