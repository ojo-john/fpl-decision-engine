"""A simple, explainable heuristic for "is this player's price about to change".

This is deliberately NOT a model, and it is NOT FPL's real price-change
algorithm -- that algorithm is undisclosed and almost certainly nonlinear
(effective thresholds are known, empirically, to tighten as a season
progresses and to vary by how heavily-owned a player already is). This
mirrors the simplified approach several community FPL price-tracking sites
publish: net transfer activity, expressed as a fraction of the player's own
estimated current owner base, crossing a conservative threshold.

Inputs come from bootstrap-static's `transfers_in_event`/`transfers_out_event`
(FPL resets both to 0 at the start of every gameweek -- this is a "since the
last price update" signal, not a season-long trend; `player_price_history`
already covers the latter) and `selected_by_percent` combined with the
season's `total_players` (total registered FPL manager accounts) to turn a
percentage into an estimated absolute owner count, since "2% swinging" means
very different things for a template pick vs. a 0.1%-owned differential.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class PriceMomentum(StrEnum):
    LIKELY_RISE = "likely_rise"
    LIKELY_FALL = "likely_fall"
    STABLE = "stable"
    UNKNOWN = "unknown"


# Net transfers must reach roughly this fraction of the player's own estimated
# owner base, AND clear the absolute floor below, before this heuristic calls
# it "likely" rather than "stable". Both are deliberately conservative
# approximations, not calibrated against real FPL price-change outcomes --
# tune from observation if this proves too noisy in practice.
_MOMENTUM_RATIO_THRESHOLD = Decimal("0.02")
_MIN_ABSOLUTE_NET_TRANSFERS = 1000


@dataclass(frozen=True)
class PriceMomentumSignal:
    """The heuristic's verdict plus the raw numbers it was computed from, for display."""

    momentum: PriceMomentum
    net_transfers_event: int | None
    estimated_owners: int | None
    momentum_ratio: Decimal | None


def classify_price_momentum(
    transfers_in_event: int | None,
    transfers_out_event: int | None,
    selected_by_percent: Decimal | None,
    total_players: int | None,
) -> PriceMomentumSignal:
    """Classify this gameweek's transfer momentum for one player.

    Returns ``UNKNOWN`` (not a guess) whenever a required input is missing --
    e.g. a brand-new season where `total_players` hasn't been captured by an
    ingestion run yet, or a player with `selected_by_percent` of exactly
    ``None``. Both ``net_transfers_event`` and ``estimated_owners`` are still
    populated wherever they *can* be computed, even if the final momentum
    classification can't be, so callers can display partial information
    rather than nothing.
    """
    if transfers_in_event is None or transfers_out_event is None:
        return PriceMomentumSignal(PriceMomentum.UNKNOWN, None, None, None)

    net = transfers_in_event - transfers_out_event

    if selected_by_percent is None or total_players is None or total_players <= 0:
        return PriceMomentumSignal(PriceMomentum.UNKNOWN, net, None, None)

    estimated_owners = int(selected_by_percent / Decimal("100") * total_players)
    if estimated_owners <= 0:
        return PriceMomentumSignal(PriceMomentum.UNKNOWN, net, estimated_owners, None)

    ratio = Decimal(net) / Decimal(estimated_owners)
    crosses_ratio_threshold = abs(ratio) >= _MOMENTUM_RATIO_THRESHOLD
    crosses_absolute_floor = abs(net) >= _MIN_ABSOLUTE_NET_TRANSFERS

    if crosses_ratio_threshold and crosses_absolute_floor:
        momentum = PriceMomentum.LIKELY_RISE if net > 0 else PriceMomentum.LIKELY_FALL
    else:
        momentum = PriceMomentum.STABLE

    return PriceMomentumSignal(momentum, net, estimated_owners, ratio)
