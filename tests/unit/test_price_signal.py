"""Unit tests for app.players.price_signal: the price-momentum heuristic."""

from decimal import Decimal

from app.players.price_signal import PriceMomentum, classify_price_momentum


def test_classify_price_momentum_unknown_when_transfer_counts_missing():
    result = classify_price_momentum(
        transfers_in_event=None,
        transfers_out_event=100,
        selected_by_percent=Decimal("10.0"),
        total_players=1_000_000,
    )
    assert result.momentum == PriceMomentum.UNKNOWN
    assert result.net_transfers_event is None
    assert result.estimated_owners is None


def test_classify_price_momentum_unknown_when_ownership_context_missing():
    # net transfers ARE known even though the classification can't be made --
    # callers should still be able to display the raw number.
    result = classify_price_momentum(
        transfers_in_event=5000,
        transfers_out_event=1000,
        selected_by_percent=None,
        total_players=1_000_000,
    )
    assert result.momentum == PriceMomentum.UNKNOWN
    assert result.net_transfers_event == 4000
    assert result.estimated_owners is None


def test_classify_price_momentum_likely_rise_when_ratio_and_floor_both_cross():
    # 10% ownership of 1,000,000 managers = 100,000 estimated owners.
    # net = 9000 - 1000 = 8000; ratio = 8000/100000 = 0.08 >= 0.02 threshold,
    # and 8000 >= the 1000 absolute floor.
    result = classify_price_momentum(
        transfers_in_event=9000,
        transfers_out_event=1000,
        selected_by_percent=Decimal("10.0"),
        total_players=1_000_000,
    )
    assert result.momentum == PriceMomentum.LIKELY_RISE
    assert result.net_transfers_event == 8000
    assert result.estimated_owners == 100_000
    assert result.momentum_ratio == Decimal("0.08")


def test_classify_price_momentum_likely_fall_when_net_transfers_negative():
    result = classify_price_momentum(
        transfers_in_event=1000,
        transfers_out_event=9000,
        selected_by_percent=Decimal("10.0"),
        total_players=1_000_000,
    )
    assert result.momentum == PriceMomentum.LIKELY_FALL
    assert result.net_transfers_event == -8000


def test_classify_price_momentum_stable_when_ratio_crosses_but_absolute_floor_does_not():
    # A tiny-owned differential: 0.01% of 1,000,000 = 100 estimated owners.
    # net = 60 - 10 = 50; ratio = 50/100 = 0.5, way above the ratio threshold,
    # but 50 net transfers is below the 1000 absolute floor -- must not be
    # flagged "likely rise" off a handful of transfers.
    result = classify_price_momentum(
        transfers_in_event=60,
        transfers_out_event=10,
        selected_by_percent=Decimal("0.01"),
        total_players=1_000_000,
    )
    assert result.momentum == PriceMomentum.STABLE
    assert result.net_transfers_event == 50
    assert result.estimated_owners == 100


def test_classify_price_momentum_stable_when_ratio_below_threshold():
    # net = 1500 - 500 = 1000 (clears the absolute floor), but estimated_owners
    # is 500,000 so ratio = 1000/500000 = 0.002, well under the 0.02 threshold.
    result = classify_price_momentum(
        transfers_in_event=1500,
        transfers_out_event=500,
        selected_by_percent=Decimal("50.0"),
        total_players=1_000_000,
    )
    assert result.momentum == PriceMomentum.STABLE
    assert result.momentum_ratio == Decimal("0.002")


def test_classify_price_momentum_unknown_when_total_players_is_zero():
    result = classify_price_momentum(
        transfers_in_event=100,
        transfers_out_event=50,
        selected_by_percent=Decimal("10.0"),
        total_players=0,
    )
    assert result.momentum == PriceMomentum.UNKNOWN
    assert result.estimated_owners is None


def test_classify_price_momentum_uses_default_thresholds_when_overrides_omitted():
    # Same inputs as test_classify_price_momentum_stable_when_ratio_below_threshold: ratio 0.002
    # is stable under the 0.02 default, with no explicit ratio_threshold/absolute_floor passed.
    result = classify_price_momentum(
        transfers_in_event=1500,
        transfers_out_event=500,
        selected_by_percent=Decimal("50.0"),
        total_players=1_000_000,
    )
    assert result.momentum == PriceMomentum.STABLE


def test_classify_price_momentum_custom_ratio_threshold_flips_stable_to_likely():
    # Same inputs as the default-threshold test above (ratio=0.002, stable under 0.02) --
    # a tuned, looser ratio_threshold of 0.001 now calls it likely instead.
    result = classify_price_momentum(
        transfers_in_event=1500,
        transfers_out_event=500,
        selected_by_percent=Decimal("50.0"),
        total_players=1_000_000,
        ratio_threshold=Decimal("0.001"),
    )
    assert result.momentum == PriceMomentum.LIKELY_RISE


def test_classify_price_momentum_custom_absolute_floor_flips_likely_to_stable():
    # Same inputs as test_classify_price_momentum_likely_rise_when_ratio_and_floor_both_cross
    # (net=8000, ratio=0.08, likely under defaults) -- a tuned, stricter absolute_floor of
    # 10000 now suppresses it back to stable.
    result = classify_price_momentum(
        transfers_in_event=9000,
        transfers_out_event=1000,
        selected_by_percent=Decimal("10.0"),
        total_players=1_000_000,
        absolute_floor=10_000,
    )
    assert result.momentum == PriceMomentum.STABLE
