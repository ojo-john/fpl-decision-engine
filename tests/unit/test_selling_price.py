"""Unit tests for app.transfers.selling_price: purchase-price replay and the sell-on-fee formula."""

from decimal import Decimal

from app.transfers.selling_price import compute_selling_price, derive_purchase_prices

SELL_ON_FEE = Decimal("0.5")


def test_selling_price_no_penalty_when_price_rose_is_wrong_case_price_fell():
    # (50, 45, 0.5) -> 45: price fell, no penalty, sell at current cost.
    assert compute_selling_price(50, 45, SELL_ON_FEE) == 45


def test_selling_price_price_held_steady():
    assert compute_selling_price(50, 50, SELL_ON_FEE) == 50


def test_selling_price_half_profit_haircut_when_price_rose():
    # (50, 60, 0.5) -> 55: profit=10, half=5, purchase+5=55.
    assert compute_selling_price(50, 60, SELL_ON_FEE) == 55


def test_selling_price_rounds_down_fractional_half_profit():
    # (50, 51, 0.5) -> 50: profit=1, half=0.5, floored to 0 -> purchase+0=50.
    assert compute_selling_price(50, 51, SELL_ON_FEE) == 50


def test_derive_purchase_prices_most_recent_transfer_wins():
    transfer_events = [
        {
            "element_in": 100,
            "element_in_cost": 50,
            "element_out": 1,
            "element_out_cost": 40,
            "time": "2025-08-01T00:00:00Z",
        },
        {
            "element_in": 200,
            "element_in_cost": 55,
            "element_out": 100,
            "element_out_cost": 52,
            "time": "2025-09-01T00:00:00Z",
        },
        {
            "element_in": 100,
            "element_in_cost": 58,
            "element_out": 200,
            "element_out_cost": 56,
            "time": "2025-10-01T00:00:00Z",
        },
    ]
    result = derive_purchase_prices(transfer_events, owned_element_ids={100})

    assert result[100].value == 58  # the later (2025-10-01) element_in event wins, not the earlier 50
    assert result[100].is_estimated is False


def test_derive_purchase_prices_never_transferred_in_is_estimated():
    transfer_events = [
        {
            "element_in": 200,
            "element_in_cost": 55,
            "element_out": 1,
            "element_out_cost": 40,
            "time": "2025-08-01T00:00:00Z",
        },
    ]
    result = derive_purchase_prices(transfer_events, owned_element_ids={999})

    assert result[999].value is None
    assert result[999].is_estimated is True
