"""Auto-tunes ``app.players.price_signal``'s momentum thresholds against real observed outcomes.

Pure functions only, same "no I/O, fully unit-testable" style as
``price_signal.classify_price_momentum`` itself -- the caller (
``app.jobs.handlers.gameweek_review``) is responsible for fetching
``player_price_signal_observations``/``player_price_history`` rows and
persisting the result.

Why this can't run until real gameweek data exists
----------------------------------------------------
``price_signal.py``'s two thresholds were reasoned defaults, never
calibrated against a real FPL price-change outcome, because pre-season there
IS no such outcome to calibrate against. ``player_price_signal_observations``
only starts accumulating once ``DATA_INGESTION`` runs against a live season
(see that table's own docstring), and a price change can only be observed
once FPL actually moves a price. ``MIN_CALIBRATION_SAMPLE_SIZE`` is the
gate: below it, calibration is a documented no-op, not a silent guess dressed
up as a real one.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

__all__ = [
    "MIN_CALIBRATION_SAMPLE_SIZE",
    "PlayerPriceObservationInput",
    "PlayerPriceChangeInput",
    "CalibrationSample",
    "CalibrationResult",
    "build_calibration_samples",
    "calibrate_thresholds",
]

# Below this many (signal, outcome) pairs, a grid search is just overfitting noise --
# calibrate_thresholds refuses to update anything until real evidence has accumulated.
MIN_CALIBRATION_SAMPLE_SIZE = 30

_RATIO_CANDIDATES: tuple[Decimal, ...] = (
    Decimal("0.005"),
    Decimal("0.0075"),
    Decimal("0.01"),
    Decimal("0.015"),
    Decimal("0.02"),
    Decimal("0.025"),
    Decimal("0.03"),
    Decimal("0.04"),
    Decimal("0.05"),
)
_ABSOLUTE_FLOOR_CANDIDATES: tuple[int, ...] = (200, 500, 1000, 1500, 2000, 3000)


@dataclass(frozen=True)
class PlayerPriceObservationInput:
    """One ``player_price_signal_observations`` row, reduced to what calibration needs."""

    player_id: object
    observed_at: datetime
    transfers_in_event: int | None
    transfers_out_event: int | None
    selected_by_percent: Decimal | None
    total_players: int | None


@dataclass(frozen=True)
class PlayerPriceChangeInput:
    """One ``player_price_history`` row, reduced to what calibration needs."""

    player_id: object
    observed_at: datetime


@dataclass(frozen=True)
class CalibrationSample:
    """One (signal-at-a-point-in-time, did-a-price-change-follow) pair."""

    net_transfers: int
    momentum_ratio: Decimal | None
    price_changed: bool


@dataclass(frozen=True)
class CalibrationResult:
    ratio_threshold: Decimal
    absolute_floor: int
    sample_size: int
    accuracy: Decimal


def _momentum_ratio(
    net_transfers: int, selected_by_percent: Decimal | None, total_players: int | None
) -> Decimal | None:
    if selected_by_percent is None or total_players is None or total_players <= 0:
        return None
    estimated_owners = int(selected_by_percent / Decimal("100") * total_players)
    if estimated_owners <= 0:
        return None
    return Decimal(net_transfers) / Decimal(estimated_owners)


def build_calibration_samples(
    observations: list[PlayerPriceObservationInput],
    price_changes: list[PlayerPriceChangeInput],
) -> list[CalibrationSample]:
    """Pair each observation with whether a real price change followed before the next one.

    For each player, sorts their observations by ``observed_at``; each
    consecutive pair ``(obs[i], obs[i+1])`` becomes one sample using
    ``obs[i]``'s own momentum numbers, labeled ``price_changed=True`` iff any
    price-history row for that player falls strictly between the two
    observation timestamps. A player's LAST observation never becomes a
    sample (there is no "next" observation yet to bound the window) --
    it'll be used once a further observation exists.
    """
    observations_by_player: dict[object, list[PlayerPriceObservationInput]] = {}
    for obs in observations:
        observations_by_player.setdefault(obs.player_id, []).append(obs)

    changes_by_player: dict[object, list[datetime]] = {}
    for change in price_changes:
        changes_by_player.setdefault(change.player_id, []).append(change.observed_at)

    samples: list[CalibrationSample] = []
    for player_id, player_observations in observations_by_player.items():
        ordered = sorted(player_observations, key=lambda o: o.observed_at)
        player_changes = sorted(changes_by_player.get(player_id, []))

        for current, following in zip(ordered, ordered[1:], strict=False):
            if current.transfers_in_event is None or current.transfers_out_event is None:
                continue
            net = current.transfers_in_event - current.transfers_out_event
            ratio = _momentum_ratio(net, current.selected_by_percent, current.total_players)
            price_changed = any(
                current.observed_at < changed_at < following.observed_at for changed_at in player_changes
            )
            samples.append(
                CalibrationSample(net_transfers=net, momentum_ratio=ratio, price_changed=price_changed)
            )

    return samples


def _predict_likely(sample: CalibrationSample, ratio_threshold: Decimal, absolute_floor: int) -> bool:
    if sample.momentum_ratio is None:
        return False
    return abs(sample.momentum_ratio) >= ratio_threshold and abs(sample.net_transfers) >= absolute_floor


def _accuracy(samples: list[CalibrationSample], ratio_threshold: Decimal, absolute_floor: int) -> Decimal:
    correct = sum(
        1
        for sample in samples
        if _predict_likely(sample, ratio_threshold, absolute_floor) == sample.price_changed
    )
    return Decimal(correct) / Decimal(len(samples))


def calibrate_thresholds(
    samples: list[CalibrationSample],
    *,
    current_ratio_threshold: Decimal,
    current_absolute_floor: int,
) -> CalibrationResult | None:
    """Grid-search the best (ratio_threshold, absolute_floor) pair, or ``None`` if under-sampled.

    Ties are broken toward whichever candidate combination is numerically
    closest to the CURRENT thresholds, so repeated calibration runs don't
    thrash between equally-accurate options.
    """
    if len(samples) < MIN_CALIBRATION_SAMPLE_SIZE:
        return None

    best: tuple[Decimal, int] | None = None
    best_accuracy = Decimal("-1")
    best_distance = Decimal("Infinity")

    for ratio_candidate in _RATIO_CANDIDATES:
        for floor_candidate in _ABSOLUTE_FLOOR_CANDIDATES:
            accuracy = _accuracy(samples, ratio_candidate, floor_candidate)
            distance = abs(ratio_candidate - current_ratio_threshold) + Decimal(
                abs(floor_candidate - current_absolute_floor)
            ) / Decimal(10000)
            if accuracy > best_accuracy or (accuracy == best_accuracy and distance < best_distance):
                best_accuracy = accuracy
                best_distance = distance
                best = (ratio_candidate, floor_candidate)

    assert best is not None  # candidate grids are non-empty constants, so a best always exists
    ratio_threshold, absolute_floor = best
    return CalibrationResult(
        ratio_threshold=ratio_threshold,
        absolute_floor=absolute_floor,
        sample_size=len(samples),
        accuracy=best_accuracy,
    )
