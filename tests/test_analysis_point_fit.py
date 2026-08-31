import random
from datetime import date, timedelta

import numpy as np
import pytest

import personal_health_lab.analysis as analysis_module
from personal_health_lab.analysis import (
    AnalysisBootstrapVariant,
    AnalysisInput,
    AnalysisInputBundle,
    AnalysisInputValue,
    AnalysisMissingness,
    AnalysisScaling,
    AnalysisScalingStatus,
    LagProfileMethodFacts,
    analysis_definitions,
)
from personal_health_lab.storage import (
    AnalysisDefinitionId,
    AnalysisRunId,
    CanonicalUnit,
    SnapshotId,
)


def _lag_bundle_and_method(horizon: int) -> tuple[AnalysisInputBundle, LagProfileMethodFacts]:
    rng = random.Random(118)
    calendar = tuple(
        date(2024, 1, 1) + timedelta(days=offset) for offset in range(horizon + 120)
    )
    values = tuple(
        value
        for day in calendar
        for value in (
            AnalysisInputValue(
                day,
                AnalysisInput.ACTIVE_ENERGY,
                None,
                CanonicalUnit.KILOCALORIE,
                rng.uniform(200, 800),
                AnalysisMissingness.OBSERVED,
            ),
            AnalysisInputValue(
                day,
                AnalysisInput.APPLE_RESTING_HEART_RATE,
                None,
                CanonicalUnit.BEATS_PER_MINUTE,
                rng.uniform(55, 75),
                AnalysisMissingness.OBSERVED,
            ),
        )
    )
    definition_id = AnalysisDefinitionId(f"rhr-activity-lag-1-{horizon}-v1")
    bundle = AnalysisInputBundle(
        AnalysisRunId("run"),
        SnapshotId("snapshot"),
        definition_id,
        calendar[0],
        calendar[-1],
        calendar,
        values,
        (),
        (),
        (
            AnalysisScaling(
                AnalysisInput.ACTIVE_ENERGY,
                None,
                None,
                AnalysisScalingStatus.UNAVAILABLE,
            ),
            AnalysisScaling(
                AnalysisInput.APPLE_RESTING_HEART_RATE,
                None,
                None,
                AnalysisScalingStatus.UNAVAILABLE,
            ),
        ),
    )
    method = next(
        definition.method_facts
        for definition in analysis_definitions()
        if definition.analysis_definition_id == definition_id
    )
    assert isinstance(method, LagProfileMethodFacts)
    return bundle, method


def test_short_lag_fit_rejects_non_finite_cumulative_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, method = _lag_bundle_and_method(7)
    original = analysis_module.np.linalg.lstsq

    def overflowing_lstsq(*args: object, **kwargs: object):
        coefficients, residuals, rank, singular_values = original(*args, **kwargs)
        coefficients[:7] = np.finfo(float).max / 2
        return coefficients, residuals, rank, singular_values

    monkeypatch.setattr(analysis_module.np.linalg, "lstsq", overflowing_lstsq)

    result = analysis_module._lag_point_fit(bundle, method)

    assert result.status == "unstable"
    assert result.diagnostics == ("non_finite_point_estimate",)
    assert result.lag_estimates == ()
    assert result.contrasts == ()


def test_long_lag_bootstrap_classifies_underfulfillment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, method = _lag_bundle_and_method(30)
    original = analysis_module._lag_coefficients
    calls = 0

    def fail_bootstrap(*args: object):
        nonlocal calls
        calls += 1
        if calls > 3:
            raise np.linalg.LinAlgError("synthetic bootstrap failure")
        return original(*args)

    monkeypatch.setattr(analysis_module, "_lag_coefficients", fail_bootstrap)

    result = analysis_module._lag_point_fit(bundle, method)

    assert result.status == "unstable"
    assert result.lag_estimates == ()
    assert result.contrasts == ()
    assert result.diagnostics[0] == "bootstrap_underfulfilled"
    assert tuple(fact.variant for fact in result.bootstrap_facts) == (
        AnalysisBootstrapVariant.PRIMARY,
        AnalysisBootstrapVariant.SENSITIVITY,
    )
    assert all(fact.attempts == 2_020 for fact in result.bootstrap_facts)
    assert all(fact.successful_refits == 0 for fact in result.bootstrap_facts)
    assert all(
        fact.failure_counts == (("linear_algebra", 2_020),)
        for fact in result.bootstrap_facts
    )
