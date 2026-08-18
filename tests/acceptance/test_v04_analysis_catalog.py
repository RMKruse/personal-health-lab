from dataclasses import FrozenInstanceError, asdict

import pytest

from personal_health_lab.application import (
    DataMode,
    HealthLab,
    LagProfileMethodFacts,
    OutcomeAssociationMethodFacts,
    RuntimeConfig,
    WeightCoreMethodFacts,
)


def test_analysis_catalog_projects_four_fixed_definitions_without_a_snapshot(tmp_path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")

    with HealthLab.open(config) as health_lab:
        catalog = health_lab.load_analysis_catalog()

    assert catalog.projection_id == "analysis-catalog"
    assert catalog.projection_version > 0
    assert tuple(str(item.analysis_definition_id) for item in catalog.definitions) == (
        "rhr-activity-lag-1-7-v1",
        "rhr-activity-lag-1-30-v1",
        "weight-core-7-14-30-90-v1",
        "rhr-weight-association-7-14-30-90-v1",
    )
    assert tuple(item.definition_version for item in catalog.definitions) == (1, 1, 1, 1)
    assert tuple(item.result_family.value for item in catalog.definitions) == (
        "rhr_activity_lag_1_7",
        "rhr_activity_lag_1_30",
        "weight_core",
        "rhr_weight_association",
    )
    assert tuple(item.horizon_days for item in catalog.definitions) == (
        (7,),
        (30,),
        (7, 14, 30, 90),
        (7, 14, 30, 90),
    )
    short, long, weight, association = (item.method_facts for item in catalog.definitions)
    assert isinstance(short, LagProfileMethodFacts)
    assert isinstance(long, LagProfileMethodFacts)
    assert (
        short.lag_start_day,
        tuple((contrast.start_day, contrast.end_day) for contrast in short.contrasts),
        short.lag_basis_nodes,
        short.smoothing_penalty,
        short.ridge_penalty,
        short.simultaneous_critical_floor,
    ) == (1, ((1, 7),), 7, 3.0, 1.0, 5.708)
    assert (
        long.lag_start_day,
        tuple((contrast.start_day, contrast.end_day) for contrast in long.contrasts),
        long.lag_basis_nodes,
        long.smoothing_penalty,
        long.ridge_penalty,
        long.simultaneous_critical_floor,
    ) == (1, ((1, 7), (8, 30), (1, 30)), 9, 300.0, 1.0, 6.454)
    expected_bootstrap = {
        "method": "moving_block_full_refit",
        "interval_method": "pointwise_and_studentized_simultaneous_band",
        "block_length": {
            "sample_count_basis": "fit_rows",
            "minimum_days": None,
            "horizon_divisor": 3,
            "sample_count_root": 3,
            "rounds_up": True,
            "takes_maximum": True,
            "sensitivity_factor": 2,
        },
        "successful_refits": 2_000,
        "maximum_attempts": 2_020,
        "confidence_level": 0.95,
    }
    assert asdict(short.bootstrap) == asdict(long.bootstrap) == expected_bootstrap
    assert tuple(value.value for value in short.inputs) == (
        "active_energy",
        "training_time",
        "steps",
        "walking_running_distance",
        "workout_duration_by_type",
        "workout_energy_by_type",
        "calendar",
        "annual_seasonality",
        "weekday",
        "outcome_day_context",
    )
    assert tuple(value.value for value in short.diagnostics) == (
        "input",
        "missingness",
        "fit",
        "residual",
        "sensitivity",
        "bootstrap",
    )
    assert short.diagnostics == long.diagnostics
    assert asdict(short.maturity) == {
        "minimum_fit_rows": 100,
        "minimum_input_completeness": 0.60,
        "minimum_effective_blocks": 18,
        "minimum_positive_training_days": 10,
        "maximum_unpenalized_condition_number": 55.0,
        "maximum_augmented_condition_number": 55.0,
        "maximum_residual_acf": 0.35,
        "maximum_ljung_box": 5.0,
        "maximum_context_sensitivity_bpm_per_sd": 0.5,
        "maximum_gap_days": 35,
        "requires_full_rank": True,
    }
    assert asdict(long.maturity) == {
        "minimum_fit_rows": 120,
        "minimum_input_completeness": 0.65,
        "minimum_effective_blocks": 11,
        "minimum_positive_training_days": 10,
        "maximum_unpenalized_condition_number": 500.0,
        "maximum_augmented_condition_number": 450.0,
        "maximum_residual_acf": 0.60,
        "maximum_ljung_box": 28.0,
        "maximum_context_sensitivity_bpm_per_sd": 0.5,
        "maximum_gap_days": 14,
        "requires_full_rank": True,
    }

    assert isinstance(weight, WeightCoreMethodFacts)
    assert tuple(value.value for value in weight.inputs) == (
        "preferred_daily_weight",
        "nutrition-day-v1",
        "active_energy",
        "resting-energy-day-allocation/v1",
    )
    assert tuple(
        (
            facts.window_days,
            facts.energy_components_ridge_penalty,
            facts.energy_macros_ridge_penalty,
            facts.energy_components_absolute_bias_floor,
            facts.energy_macros_absolute_bias_floor,
        )
        for facts in weight.windows
    ) == (
        (7, 100.0, 100.0, 0.059519, 0.132232),
        (14, 100.0, 100.0, 0.043473, 0.142449),
        (30, 100.0, 100.0, 0.044135, 0.197123),
        (90, 30.0, 1.0, 0.045717, 0.105168),
    )
    assert (
        weight.trend_kernel.value,
        weight.trend_uncertainty.value,
        weight.minimum_local_observations_floor,
        weight.minimum_local_observations_fraction,
        weight.minimum_scaled_pivot,
        weight.minimum_model_anchor_floor,
        weight.minimum_model_anchor_fraction,
        weight.energy_components_family_multiplier,
        weight.energy_macros_family_multiplier,
    ) == (
        "triangular_local_linear",
        "no_calibrated_interval",
        4,
        0.22,
        1e-6,
        3,
        0.12,
        1.454414,
        1.120367,
    )
    assert asdict(weight.bootstrap) == {
        **expected_bootstrap,
        "interval_method": "pointwise_and_simultaneous_family_band",
        "block_length": {
            **expected_bootstrap["block_length"],
            "sample_count_basis": "observed_weight_days",
        },
    }
    assert tuple(value.value for value in weight.diagnostics) == (
        "input",
        "missingness",
        "support",
        "fit",
        "residual",
        "prediction",
        "sensitivity",
        "bootstrap",
    )
    assert asdict(weight.maturity) == {
        "minimum_common_complete_fraction": 0.35,
        "maximum_common_gap_days": 35,
        "minimum_model_anchors": 100,
        "maximum_unpenalized_condition_number": 1_000.0,
        "maximum_ridge_sensitivity_kg_per_week_per_sd": 0.025,
        "maximum_residual_acf": 0.8,
        "minimum_blocked_prediction_gain": -0.5,
        "maximum_mean_band_halfwidth_kg_per_week_per_sd": 0.25,
        "requires_full_rank": True,
        "requires_primary_and_sensitivity_bootstraps": True,
    }

    assert isinstance(association, OutcomeAssociationMethodFacts)
    assert tuple(value.value for value in association.inputs) == (
        "apple_resting_heart_rate",
        "preferred_daily_weight",
    )
    assert (
        association.trend_measure.value,
        association.deviation_measure.value,
        association.decomposition_kernel.value,
        association.minimum_pairs,
        asdict(association.maturity),
    ) == (
        "pearson_local_slopes",
        "pearson_paired_deviations",
        "triangular_local_linear",
        4,
        {
            "minimum_calendar_days": 365,
            "minimum_pair_density": 0.40,
            "maximum_observed_gap_days": 18,
            "maximum_residual_acf": 0.55,
            "maximum_rhr_measurement_error_sd": 0.35,
            "maximum_weight_measurement_error_sd": 0.084,
            "rejects_structural_break_or_source_change": True,
        },
    )
    assert asdict(association.bootstrap) == {
        **expected_bootstrap,
        "method": "paired_circular_residual_moving_block_full_refit",
        "interval_method": "fisher_z_bias_corrected_studentized_simultaneous_band",
        "block_length": {
            **expected_bootstrap["block_length"],
            "sample_count_basis": "paired_days",
            "minimum_days": 3,
            "horizon_divisor": None,
        },
    }
    assert tuple(value.value for value in association.diagnostics) == (
        "input",
        "missingness",
        "fit",
        "influence",
        "structural_break",
        "sensitivity",
        "bootstrap",
    )

    with pytest.raises(FrozenInstanceError):
        catalog.projection_version = 2  # type: ignore[misc]
