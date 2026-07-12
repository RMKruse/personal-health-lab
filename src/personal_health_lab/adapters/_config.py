from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from personal_health_lab.application import ConfigurationError, DataMode, RuntimeConfig

_DEFAULT_CONFIG_FILE = Path.home() / ".config/healthlab/config.json"
_DEFAULT_SYNTHETIC_STORE = Path.home() / ".local/share/healthlab/synthetic"
_DEFAULT_REAL_STORE = Path.home() / ".local/share/healthlab/real"


def _read_local_config(path: Path) -> Mapping[str, object]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError("Lokale HealthLab-Konfiguration ist nicht lesbar.") from error
    if not isinstance(parsed, dict):
        raise ConfigurationError("Lokale HealthLab-Konfiguration muss ein JSON-Objekt sein.")
    return parsed


def _text_value(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"Konfigurationswert {name} muss Text sein.")
    return value


def load_runtime_config(
    *,
    explicit_mode: DataMode | None = None,
    explicit_synthetic_store: Path | None = None,
    explicit_real_store: Path | None = None,
    config_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Resolve explicit, environment, local-file, then versioned-default values."""

    environment = os.environ if environ is None else environ
    selected_config_file = config_file or Path(
        environment.get("HEALTHLAB_CONFIG", _DEFAULT_CONFIG_FILE)
    )
    local = _read_local_config(selected_config_file.expanduser())

    mode_value: object = (
        explicit_mode
        or environment.get("HEALTHLAB_MODE")
        or local.get("mode")
        or DataMode.SYNTHETIC
    )
    synthetic_value: object = (
        explicit_synthetic_store
        or environment.get("HEALTHLAB_SYNTHETIC_STORE")
        or local.get("synthetic_store")
        or _DEFAULT_SYNTHETIC_STORE
    )
    real_value: object = (
        explicit_real_store
        or environment.get("HEALTHLAB_REAL_STORE")
        or local.get("real_store")
        or _DEFAULT_REAL_STORE
    )

    try:
        mode = (
            mode_value
            if isinstance(mode_value, DataMode)
            else DataMode(_text_value(mode_value, "mode"))
        )
    except ValueError as error:
        raise ConfigurationError("Datenmodus muss 'synthetic' oder 'real' sein.") from error
    synthetic_store = (
        synthetic_value
        if isinstance(synthetic_value, Path)
        else Path(_text_value(synthetic_value, "synthetic_store"))
    )
    real_store = (
        real_value
        if isinstance(real_value, Path)
        else Path(_text_value(real_value, "real_store"))
    )
    return RuntimeConfig(mode=mode, synthetic_store=synthetic_store, real_store=real_store)
