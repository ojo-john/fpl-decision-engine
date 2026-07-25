"""Unit tests for app.players.price_signal_calibration: auto-tuning the price-momentum heuristic.

Priority order per this repo's own testing conventions: failure paths/boundary conditions
first (empty inputs, under-sampled calibration, a player with only one observation), then
the core business logic (pairing, grid search, tie-break), then happy path.
"""

from datetime import UTC, datetime
from decimal import Decimal

from app.players.price_signal_calibration import (
    MIN_CALIBRATION_SAMPLE_SIZE,
    CalibrationSample,
    PlayerPriceChangeInput,
    PlayerPriceObservationInput,
    build_calibration_samples,
    calibrate_thresholds,
)

_T0 = datetime(2026, 8, 1, tzinfo=UTC)
_T1 = datetime(2026, 8, 2, tzinfo=UTC)
_T2 = datetime(2026, 8, 3, tzinfo=UTC)


def _obs(
    player_id: str,
    observed_at: datetime,
    transfers_in: int | None = 0,
    transfers_out: int | None = 0,
    selected_by_percent: str = "10.0",
    total_players: int | None = 1_000_000,
) -> PlayerPriceObservationInput:
    return PlayerPriceObservationInput(
        player_id=player_id,
        observed_at=observed_at,
        transfers_in_event=transfers_in,
        transfers_out_event=transfers_out,
        selected_by_percent=Decimal(selected_by_percent) if selected_by_percent is not None else None,
        total_players=total_players,
    )


# --- build_calibration_samples: boundary conditions ------------------------------------------


def test_build_calibration_samples_returns_empty_for_no_observations():
    assert build_calibration_samples([], []) == []


def test_build_calibration_samples_produces_nothing_for_a_players_only_observation():
    # No "next" observation to bound a window -- this player contributes zero samples yet.
    samples = build_calibration_samples([_obs("p1", _T0)], [])
    assert samples == []


def test_build_calibration_samples_skips_observation_with_missing_transfer_counts():
    observations = [
        _obs("p1", _T0, transfers_in=None, transfers_out=None),
        _obs("p1", _T1),
    ]
    assert build_calibration_samples(observations, []) == []


# --- build_calibration_samples: business logic ------------------------------------------------


def test_build_calibration_samples_marks_price_changed_true_when_a_change_falls_in_the_window():
    observations = [_obs("p1", _T0, transfers_in=9000, transfers_out=1000), _obs("p1", _T2)]
    price_changes = [PlayerPriceChangeInput(player_id="p1", observed_at=_T1)]

    samples = build_calibration_samples(observations, price_changes)

    assert len(samples) == 1
    assert samples[0].price_changed is True
    assert samples[0].net_transfers == 8000


def test_build_calibration_samples_marks_price_changed_false_when_no_change_falls_in_window():
    observations = [_obs("p1", _T0, transfers_in=9000, transfers_out=1000), _obs("p1", _T2)]

    samples = build_calibration_samples(observations, [])

    assert len(samples) == 1
    assert samples[0].price_changed is False


def test_build_calibration_samples_ignores_a_price_change_outside_the_observation_window():
    # The change happened AFTER the second observation -- it belongs to a later window, not this one.
    observations = [_obs("p1", _T0), _obs("p1", _T1)]
    price_changes = [PlayerPriceChangeInput(player_id="p1", observed_at=_T2)]

    samples = build_calibration_samples(observations, price_changes)

    assert len(samples) == 1
    assert samples[0].price_changed is False


def test_build_calibration_samples_handles_multiple_players_independently():
    observations = [
        _obs("p1", _T0, transfers_in=9000, transfers_out=1000),
        _obs("p1", _T1),
        _obs("p2", _T0, transfers_in=100, transfers_out=100),
        _obs("p2", _T1),
    ]
    price_changes = [PlayerPriceChangeInput(player_id="p1", observed_at=datetime(2026, 8, 1, 12, tzinfo=UTC))]

    samples = build_calibration_samples(observations, price_changes)

    assert len(samples) == 2
    by_net = {s.net_transfers: s.price_changed for s in samples}
    assert by_net[8000] is True
    assert by_net[0] is False


# --- calibrate_thresholds: boundary conditions --------------------------------------------------


def test_calibrate_thresholds_returns_none_when_under_minimum_sample_size():
    samples = [CalibrationSample(net_transfers=8000, momentum_ratio=Decimal("0.08"), price_changed=True)] * (
        MIN_CALIBRATION_SAMPLE_SIZE - 1
    )
    result = calibrate_thresholds(
        samples, current_ratio_threshold=Decimal("0.02"), current_absolute_floor=1000
    )
    assert result is None


def test_calibrate_thresholds_returns_none_for_empty_samples():
    result = calibrate_thresholds([], current_ratio_threshold=Decimal("0.02"), current_absolute_floor=1000)
    assert result is None


# --- calibrate_thresholds: business logic --------------------------------------------------------


def test_calibrate_thresholds_picks_a_perfectly_separating_threshold():
    # Half the samples are a clear, large spike that always precedes a price change; half are
    # dead quiet and never do. Any threshold between the two groups classifies perfectly.
    spikes = [
        CalibrationSample(net_transfers=50_000, momentum_ratio=Decimal("0.5"), price_changed=True)
        for _ in range(20)
    ]
    quiet = [
        CalibrationSample(net_transfers=10, momentum_ratio=Decimal("0.0001"), price_changed=False)
        for _ in range(20)
    ]
    result = calibrate_thresholds(
        spikes + quiet, current_ratio_threshold=Decimal("0.02"), current_absolute_floor=1000
    )

    assert result is not None
    assert result.sample_size == 40
    assert result.accuracy == Decimal("1")


def test_calibrate_thresholds_breaks_ties_toward_the_current_threshold():
    # Every candidate scores identically (no samples at all discriminate net_transfers==0,
    # momentum_ratio==0 always fails ANY positive ratio_threshold) -- every combination
    # predicts STABLE for every sample, so accuracy is identical everywhere price_changed=False,
    # and identical (but lower) everywhere price_changed=True. The tie-break must land on
    # whichever grid candidate is closest to current_ratio_threshold/current_absolute_floor.
    samples = [
        CalibrationSample(net_transfers=0, momentum_ratio=Decimal("0"), price_changed=False)
        for _ in range(MIN_CALIBRATION_SAMPLE_SIZE)
    ]
    result = calibrate_thresholds(
        samples, current_ratio_threshold=Decimal("0.03"), current_absolute_floor=2000
    )

    assert result is not None
    assert result.ratio_threshold == Decimal("0.03")
    assert result.absolute_floor == 2000
    assert result.accuracy == Decimal("1")


def test_calibrate_thresholds_sample_missing_ratio_is_never_predicted_likely():
    # momentum_ratio=None (e.g. selected_by_percent was missing at observation time) must never
    # be misclassified as "likely" regardless of net_transfers -- classify_price_momentum's own
    # UNKNOWN-when-ratio-unavailable contract, mirrored here.
    samples = [
        CalibrationSample(net_transfers=50_000, momentum_ratio=None, price_changed=False)
        for _ in range(MIN_CALIBRATION_SAMPLE_SIZE)
    ]
    result = calibrate_thresholds(
        samples, current_ratio_threshold=Decimal("0.02"), current_absolute_floor=1000
    )

    assert result is not None
    assert result.accuracy == Decimal("1")
