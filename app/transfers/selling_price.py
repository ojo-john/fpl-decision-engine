"""Pure derivation of purchase prices (from transfer history) and selling prices (FPL sell-on-fee rule).

No FPL endpoint reports a player's original purchase price directly — this
module reconstructs it by replaying ``/transfers/`` (every transfer the
manager has ever made, live-verified to return a flat list, possibly empty).
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PurchasePriceInfo:
    """The derived purchase price for one currently-owned squad element.

    ``is_estimated=True`` means no ``element_in`` transfer event was found
    for this element at all — it was held since the manager's original squad
    selection (not itself a "transfer" event in FPL's data model), so there
    is no derivable purchase price. ``value`` is ``None`` in that case; the
    caller (not this function) is responsible for falling back to the
    player's current ``now_cost`` rather than silently assuming a value with
    false confidence.
    """

    value: int | None
    is_estimated: bool


def derive_purchase_prices(
    transfer_events: list[dict], owned_element_ids: set[int]
) -> dict[int, PurchasePriceInfo]:
    """Replay ``/transfers/`` in ascending time order to derive each owned element's purchase price.

    Purchase price = ``element_in_cost`` of the MOST RECENT ``element_in``
    transfer for that element id (a player can be bought, sold, and
    re-bought across a season — only the latest acquisition determines the
    price currently "locked in"). An owned element that never appears as
    ``element_in`` anywhere in ``transfer_events`` gets
    ``PurchasePriceInfo(value=None, is_estimated=True)``.
    """
    ordered_events = sorted(transfer_events, key=lambda t: t["time"])

    latest_cost_by_element: dict[int, int] = {}
    for event in ordered_events:
        element_in = event["element_in"]
        latest_cost_by_element[element_in] = event["element_in_cost"]

    result: dict[int, PurchasePriceInfo] = {}
    for element_id in owned_element_ids:
        if element_id in latest_cost_by_element:
            result[element_id] = PurchasePriceInfo(
                value=latest_cost_by_element[element_id], is_estimated=False
            )
        else:
            result[element_id] = PurchasePriceInfo(value=None, is_estimated=True)
    return result


def compute_selling_price(purchase_price: int, now_cost: int, sell_on_fee: Decimal) -> int:
    """FPL sell-on-fee rule: no penalty if price fell/held; half-profit haircut (rounded down) if risen.

    Hand-verifiable cases: ``(50, 60, 0.5) -> 55``; ``(50, 45, 0.5) -> 45``;
    ``(50, 51, 0.5) -> 50`` (profit=1, half=0.5, floored to 0).
    """
    if now_cost <= purchase_price:
        return now_cost
    profit = now_cost - purchase_price
    return purchase_price + int(profit * sell_on_fee)
