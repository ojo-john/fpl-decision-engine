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

Auto-recalibration
--------------------
The two ``DEFAULT_*`` thresholds below were reasoned defaults, never
calibrated against a real FPL price-change outcome (there was no such
outcome to calibrate against pre-season). ``classify_price_momentum``'s
optional ``ratio_threshold``/``absolute_floor`` params let a caller override
them with a tuned ``PriceSignalCalibration`` row instead -- see
``app.players.price_signal_calibration`` (the grid-search tuning logic) and
``app.jobs.handlers.gameweek_review.GameweekReviewJobRunner
._maybe_recalibrate_price_signal`` (the automatic trigger, once real
gameweek/price-change data exists). Callers that don't pass overrides
(``None``, the default) keep getting the original hardcoded behavior.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class PriceMomentum(StrEnum):
    LIKELY_RISE = "likely_rise"
    LIKELY_FALL = "likely_fall"
    STABLE = "stable"
    UNKNOWN = "unknown"


# Fallback thresholds, used whenever no active PriceSignalCalibration row exists yet (see
# module docstring's "Auto-recalibration" section). Net transfers must reach roughly this
# fraction of the player's own estimated owner base, AND clear the absolute floor, before
# this heuristic calls it "likely" rather than "stable".
DEFAULT_MOMENTUM_RATIO_THRESHOLD = Decimal("0.02")
DEFAULT_MIN_ABSOLUTE_NET_TRANSFERS = 1000


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
    ratio_threshold: Decimal | None = None,
    absolute_floor: int | None = None,
) -> PriceMomentumSignal:
    """Classify this gameweek's transfer momentum for one player.

    Returns ``UNKNOWN`` (not a guess) whenever a required input is missing --
    e.g. a brand-new season where `total_players` hasn't been captured by an
    ingestion run yet, or a player with `selected_by_percent` of exactly
    ``None``. Both ``net_transfers_event`` and ``estimated_owners`` are still
    populated wherever they *can* be computed, even if the final momentum
    classification can't be, so callers can display partial information
    rather than nothing.

    ``ratio_threshold``/``absolute_floor`` default to the hardcoded
    ``DEFAULT_*`` constants when omitted -- pass a tuned
    ``PriceSignalCalibration`` row's values instead once one exists (see
    module docstring's "Auto-recalibration" section).
    """
    effective_ratio_threshold = (
        ratio_threshold if ratio_threshold is not None else DEFAULT_MOMENTUM_RATIO_THRESHOLD
    )
    effective_absolute_floor = (
        absolute_floor if absolute_floor is not None else DEFAULT_MIN_ABSOLUTE_NET_TRANSFERS
    )

    if transfers_in_event is None or transfers_out_event is None:
        return PriceMomentumSignal(PriceMomentum.UNKNOWN, None, None, None)

    net = transfers_in_event - transfers_out_event

    if selected_by_percent is None or total_players is None or total_players <= 0:
        return PriceMomentumSignal(PriceMomentum.UNKNOWN, net, None, None)

    estimated_owners = int(selected_by_percent / Decimal("100") * total_players)
    if estimated_owners <= 0:
        return PriceMomentumSignal(PriceMomentum.UNKNOWN, net, estimated_owners, None)

    ratio = Decimal(net) / Decimal(estimated_owners)
    crosses_ratio_threshold = abs(ratio) >= effective_ratio_threshold
    crosses_absolute_floor = abs(net) >= effective_absolute_floor

    if crosses_ratio_threshold and crosses_absolute_floor:
        momentum = PriceMomentum.LIKELY_RISE if net > 0 else PriceMomentum.LIKELY_FALL
    else:
        momentum = PriceMomentum.STABLE

    return PriceMomentumSignal(momentum, net, estimated_owners, ratio)
