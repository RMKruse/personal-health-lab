import logging
from pathlib import Path
from typing import cast

import pytest

from personal_health_lab.application import (
    ConfigurationError,
    DataMode,
    HealthLab,
    RuntimeConfig,
)


def test_data_modes_require_physically_separate_stores(tmp_path: Path) -> None:
    shared_store = tmp_path / "shared"

    with pytest.raises(ConfigurationError, match="getrennt"):
        RuntimeConfig(
            mode=DataMode.REAL,
            synthetic_store=shared_store,
            real_store=shared_store,
        )


def test_unknown_data_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match=r"synthetic.*real"):
        RuntimeConfig(
            mode=cast(DataMode, "mixed"),
            synthetic_store=tmp_path / "synthetic",
            real_store=tmp_path / "real",
        )


def test_store_rejects_opening_in_a_different_mode(tmp_path: Path) -> None:
    synthetic_store = tmp_path / "synthetic"
    first_config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=synthetic_store,
        real_store=tmp_path / "real",
    )
    with HealthLab.open(first_config):
        pass

    conflicting_config = RuntimeConfig(
        mode=DataMode.REAL,
        synthetic_store=tmp_path / "other-synthetic",
        real_store=synthetic_store,
    )
    with (
        pytest.raises(ConfigurationError, match="anderen Modus"),
        HealthLab.open(conflicting_config),
    ):
        pass


def test_real_mode_logs_do_not_reveal_the_store_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = RuntimeConfig(
        mode=DataMode.REAL,
        synthetic_store=tmp_path / "synthetic-private-name",
        real_store=tmp_path / "real-private-name",
    )

    with (
        caplog.at_level(logging.INFO, logger="personal_health_lab"),
        HealthLab.open(config),
    ):
        pass

    assert "real-private-name" not in caplog.text
    assert "synthetic-private-name" not in caplog.text
    assert "healthlab_opened mode=real" in caplog.text
