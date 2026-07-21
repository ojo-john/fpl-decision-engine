"""Pure computation of one rival's DERIVED + PREDICTED state (Milestone 5 Layer 2).

No I/O, no DB, no ORM objects — every input here is a plain dataclass or the
exact ``list[dict]`` shape FPL's own API returns, so ``compute_rival_state``
is trivially unit-testable with hand-built Python objects. All DB
querying/fetching (loading ``RivalGameweekSnapshot`` rows, calling
``FPLClient.fetch_manager_history``, resolving ``Player`` rows for transfer
targets/ownership) is the caller's job (``app.jobs.handlers.league_sync``) —
this module only turns already-fetched data into a ``RivalStateResult``.

Two source-of-truth choices, made deliberately and documented here rather
than left implicit:

1. **Transfer/hit/roll tendency statistics use ``history_current_rows``
   (``fetch_manager_history``'s ``current[]``), not ``RivalGameweekSnapshot``
   rows.** ``current[]`` already contains one row per completed gameweek of
   the *entire* season in a single fetch. ``RivalGameweekSnapshot`` rows only
   accumulate one at a time, one per ``LEAGUE_SYNC`` run (currently just a
   single row for every real rival, since Layer 1 has only run once) — right
   now, and for a long time to come, ``current[]`` is the far richer sample.
   ``derive_free_transfers_available`` (reused verbatim, unmodified, from
   ``app.transfers.free_transfers``) is called against this same data.

2. **Captain-tendency statistics use ``RivalGameweekSnapshot`` rows, not
   ``current[]``.** ``current[]`` has no captain field at all — captain
   identity is only ever captured by a rival's own picks fetch, which is
   exactly what ``RivalGameweekSnapshot`` stores. This is *necessarily* the
   thinner of the two samples (one snapshot per synced gameweek), which is
   precisely why a fresh rival's captain-tendency confidence is honestly
   capped ``LOW`` even when their transfer-tendency confidence (drawn from
   the richer ``current[]`` sample) is already ``HIGH`` or ``MEDIUM``.

Population-level prior for transfer-count/hit-rate cold start: this
codebase's ``bootstrap-static`` ingestion (``packages.database.models.player``)
does not capture FPL's account-aggregate fields (``most_captained``/
``most_transferred_in``/etc — confirmed absent from both the live schema this
system ingests and the committed ``bootstrap_static_sample.json`` fixture).
With no ingested aggregate to build a real population distribution from,
``_POPULATION_PRIOR_*`` below are simple, clearly-labeled, hand-picked
defaults (same spirit as ``app/rules/constants.py``'s ``HAND_MAINTAINED_RULES``
— PROVISIONAL, never presented as measured) used ONLY when a rival's own
``current[]`` sample is too thin to support a rival-specific estimate, and
always flagged in ``unknowns``/capped at ``LOW`` confidence when used.

Confidence assignment is a judgment call, made explicit rather than arbitrary:
every sub-computation below has its own evidence-volume thresholds
(documented next to each), and ``overall_confidence`` is always the floor
(minimum) across all of them — never an average, never optimistic. This is
why a rival profiled from a single ``RivalGameweekSnapshot`` (the realistic
starting state for every real rival right now) gets ``overall_confidence=LOW``
regardless of how rich their ``current[]`` history already is.

Chip windows, not flat chip names: every 2025-26 chip (wildcard/freehit/
bboost/3xc) has TWO independent play windows, one per season half (e.g.
wildcard window 1 spans events 2-19, window 2 spans 20-38 -- see
``app.chips.windows.resolve_chip_windows``, reused here verbatim rather than
duplicated). A rival who has played "wildcard" once has used exactly one of
those two windows, not the chip entirely -- ``_derive_remaining_chips`` is
window-aware: it matches each observed play (``history_chip_rows``, which
carries FPL's own ``event`` per play) to the specific window whose
``[start_event, stop_event]`` contains that play's event (falling back to
the earliest not-yet-matched same-name window if a play's event doesn't
cleanly fall inside any declared window -- a defensive fallback, never
silently ignored, since understating a chip as played is the safe direction
here), and a chip name is only removed from "remaining" once every one of
its windows is either used or has fully lapsed (``current_event`` is past
its ``stop_event``) without being played.
"""

import uuid
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.chips.windows import ChipWindow
from app.transfers.free_transfers import _CHIP_NAMES_FREEZING_TRANSFERS, derive_free_transfers_available
from packages.domain.h2h import RivalStateConfidence

# --- Population-level prior (cold-start fallback only, see module docstring) ---
_POPULATION_PRIOR_NO_TRANSFER_PROBABILITY = 0.50
_POPULATION_PRIOR_ONE_TRANSFER_PROBABILITY = 0.35
_POPULATION_PRIOR_MULTI_TRANSFER_PROBABILITY = 0.15
_POPULATION_PRIOR_HIT_PROBABILITY = 0.15

# --- Confidence thresholds (documented judgment calls, not arbitrary) ---
# Free-transfer replay: HIGH once at least 10 completed-gameweek current[] rows
# are observed (roughly a quarter of a season) -- below that, the replay is
# still computed (it's a deterministic forward-replay, not a statistical
# estimate) but the reported range is widened by +/-1 to honestly reflect
# that an unobserved earlier gameweek could shift the true count.
_MIN_HISTORY_ROWS_FOR_HIGH_FREE_TRANSFER_CONFIDENCE = 10
# Own-history transfer/hit distribution: need at least 6 non-chip observed
# gameweeks before treating rival-specific frequencies as more informative
# than the population prior; HIGH once at least 15 (~40% of a season).
_MIN_HISTORY_ROWS_FOR_OWN_TRANSFER_DISTRIBUTION = 6
_HIGH_HISTORY_ROWS_FOR_TRANSFER_DISTRIBUTION = 15
# Captain tendency: a single observation is definitionally not a tendency
# (always LOW). MEDIUM from 2-4 observed gameweeks, HIGH from 5+.
_MIN_CAPTAIN_OBSERVATIONS_FOR_MEDIUM_CONFIDENCE = 2
_MIN_CAPTAIN_OBSERVATIONS_FOR_HIGH_CONFIDENCE = 5
# Chip scheduling windows split the season into two halves at this boundary
# (matches the real bootstrap-static chips[] window split, e.g. wildcard
# window 1 stop_event=19 / window 2 start_event=20 in 2025-26).
_SEASON_HALF_EVENT_CUTOFF = 19

_TOP_TRANSFER_TARGET_CANDIDATES = 5

_CONFIDENCE_RANK = {
    RivalStateConfidence.LOW: 0,
    RivalStateConfidence.MEDIUM: 1,
    RivalStateConfidence.HIGH: 2,
}


def _min_confidence(*confidences: RivalStateConfidence) -> RivalStateConfidence:
    return min(confidences, key=lambda c: _CONFIDENCE_RANK[c])


def _round4(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class CaptainObservation:
    """One gameweek's captain choice, sourced from a single ``RivalGameweekSnapshot`` row.

    ``captain_element_id`` mirrors that model's own field: the raw FPL
    element id, never resolved to an internal ``Player`` row (captain
    identity is meaningful for frequency counting even if the player no
    longer resolves to a current-season ``Player``).
    """

    fpl_event_id: int
    captain_element_id: int | None


@dataclass(frozen=True)
class TransferTargetCandidate:
    """One heuristic replacement-player candidate, already resolved/ranked by the caller.

    ``rival_state`` does not itself query the ``Player`` table (this module
    is DB-free) — the caller ranks candidates against the live player pool
    and hands over only the plain data needed to report them.
    """

    player_id: str
    web_name: str | None
    position: str | None
    projected_points: float
    selected_by_percent: float | None


@dataclass(frozen=True)
class RivalStateInputs:
    """Everything ``compute_rival_state`` needs for one rival, as plain data."""

    fpl_manager_id: int
    chip_windows: list[ChipWindow]  # every playable chip window this season (2 per chip -- see ChipWindow)
    free_transfers_cap: int
    # Whether /history/ was fetched successfully this run at all (independent
    # of whether *this run's* picks fetch succeeded -- Layer 2 reuses
    # whatever history exists regardless of this run's picks outcome).
    history_available: bool
    history_current_rows: list[dict]  # FPL's history().current[] shape, verbatim
    history_chip_rows: list[dict]  # FPL's history().chips[] shape, verbatim
    # The most recently finished gameweek's raw FPL event number, if any --
    # used only to tell a lapsed-unplayed chip window apart from one still
    # open (see _derive_remaining_chips). None if no gameweek has finished
    # yet this season.
    current_event: int | None = None
    captain_observations: list[CaptainObservation] = field(default_factory=list)
    last_known_snapshot_id: uuid.UUID | None = None
    squad_selected_by_percent: list[float] | None = None
    population_mean_selected_by_percent: float | None = None
    transfer_target_candidates: list[TransferTargetCandidate] = field(default_factory=list)


@dataclass(frozen=True)
class RivalStateResult:
    """Everything a ``RivalProfile`` row needs, computed and ready to persist."""

    last_known_snapshot_id: uuid.UUID | None
    derived_free_transfers_min: int | None
    derived_free_transfers_max: int | None
    derived_free_transfers_confidence: RivalStateConfidence
    derived_likely_remaining_chips: list[str]
    predicted_no_transfer_probability: Decimal
    predicted_one_transfer_probability: Decimal
    predicted_multi_transfer_probability: Decimal
    predicted_hit_probability: Decimal
    predicted_captain_probabilities: dict[str, float]
    predicted_likely_transfer_targets: list[dict]
    tendency_features: dict
    unknowns: list[str]
    evidence: dict
    overall_confidence: RivalStateConfidence


def _non_chip_rows(history_current_rows: list[dict], history_chip_rows: list[dict]) -> list[dict]:
    """``current[]`` rows minus any gameweek a wildcard/free-hit was played.

    A chip gameweek's ``event_transfers`` is unlimited/free and not
    representative of ordinary transfer behavior -- including it would wildly
    skew both the transfer-count distribution and the hit-rate estimate.
    """
    chip_events = {c["event"] for c in history_chip_rows if c.get("name") in _CHIP_NAMES_FREEZING_TRANSFERS}
    return [row for row in history_current_rows if row.get("event") not in chip_events]


def _derive_free_transfers(
    history_available: bool,
    history_current_rows: list[dict],
    history_chip_rows: list[dict],
    free_transfers_cap: int,
) -> tuple[int | None, int | None, RivalStateConfidence]:
    if not history_available:
        return None, None, RivalStateConfidence.LOW

    value = derive_free_transfers_available(history_current_rows, history_chip_rows, free_transfers_cap)
    row_count = len(history_current_rows)

    if row_count >= _MIN_HISTORY_ROWS_FOR_HIGH_FREE_TRANSFER_CONFIDENCE:
        return value, value, RivalStateConfidence.HIGH

    # Thin (or empty) history: the forward-replay itself is still exact given
    # its inputs, but a genuinely earlier, unobserved gameweek could shift the
    # true value by one -- widen the reported range rather than overclaim a
    # single confident number.
    confidence = RivalStateConfidence.MEDIUM if row_count > 0 else RivalStateConfidence.LOW
    return max(value - 1, 0), min(value + 1, free_transfers_cap), confidence


def _derive_remaining_chips(
    history_available: bool,
    chip_windows: list[ChipWindow],
    history_chip_rows: list[dict],
    current_event: int | None,
) -> tuple[list[str], RivalStateConfidence]:
    # De-duplicate while preserving first-seen order (a chip typically has two
    # windows -- see module docstring).
    names_in_order = list(dict.fromkeys(window.chip_type.value for window in chip_windows))
    if not history_available:
        # Honesty over convenience: with no play-history at all, nothing can
        # be ruled OUT, so the safest non-overclaiming answer is "assume
        # nothing has been played yet" (every chip name), not an empty
        # list -- an empty list would silently claim every chip is spent.
        return names_in_order, RivalStateConfidence.LOW

    windows_by_name: dict[str, list[ChipWindow]] = {}
    for window in chip_windows:
        windows_by_name.setdefault(window.chip_type.value, []).append(window)

    used_windows: set[ChipWindow] = set()
    for row in history_chip_rows:
        name = row.get("name")
        if name is None:
            continue
        candidates = [w for w in windows_by_name.get(name, []) if w not in used_windows]
        if not candidates:
            continue
        event = row.get("event")
        exact_match = next(
            (w for w in candidates if event is not None and w.start_event <= event <= w.stop_event), None
        )
        # Defensive fallback (not the expected case -- see module docstring):
        # a play whose event doesn't cleanly fall inside any declared window
        # still counts against the earliest not-yet-matched window of that
        # name, rather than being silently ignored.
        used_windows.add(exact_match if exact_match is not None else candidates[0])

    def _window_still_playable(window: ChipWindow) -> bool:
        if window in used_windows:
            return False
        return current_event is None or current_event <= window.stop_event

    remaining = [
        name for name in names_in_order if any(_window_still_playable(w) for w in windows_by_name[name])
    ]
    return remaining, RivalStateConfidence.HIGH


def _own_transfer_distribution(observed: list[dict]) -> tuple[float, float, float, float] | None:
    if len(observed) < _MIN_HISTORY_ROWS_FOR_OWN_TRANSFER_DISTRIBUTION:
        return None
    n = len(observed)
    no_transfer = sum(1 for r in observed if r.get("event_transfers", 0) == 0) / n
    one_transfer = sum(1 for r in observed if r.get("event_transfers", 0) == 1) / n
    multi_transfer = sum(1 for r in observed if r.get("event_transfers", 0) >= 2) / n
    hit_taken = sum(1 for r in observed if (r.get("event_transfers_cost") or 0) < 0) / n
    return no_transfer, one_transfer, multi_transfer, hit_taken


def _transfer_and_hit_probabilities(
    observed: list[dict],
) -> tuple[Decimal, Decimal, Decimal, Decimal, RivalStateConfidence, bool]:
    own = _own_transfer_distribution(observed)
    if own is None:
        return (
            _round4(_POPULATION_PRIOR_NO_TRANSFER_PROBABILITY),
            _round4(_POPULATION_PRIOR_ONE_TRANSFER_PROBABILITY),
            _round4(_POPULATION_PRIOR_MULTI_TRANSFER_PROBABILITY),
            _round4(_POPULATION_PRIOR_HIT_PROBABILITY),
            RivalStateConfidence.LOW,
            True,
        )
    no_t, one_t, multi_t, hit_t = own
    confidence = (
        RivalStateConfidence.HIGH
        if len(observed) >= _HIGH_HISTORY_ROWS_FOR_TRANSFER_DISTRIBUTION
        else RivalStateConfidence.MEDIUM
    )
    return _round4(no_t), _round4(one_t), _round4(multi_t), _round4(hit_t), confidence, False


def _captain_probabilities(
    captain_observations: list[CaptainObservation],
) -> tuple[dict[str, float], RivalStateConfidence, int]:
    valid = [obs for obs in captain_observations if obs.captain_element_id is not None]
    if not valid:
        return {}, RivalStateConfidence.LOW, 0

    counts: dict[int, int] = {}
    for obs in valid:
        assert obs.captain_element_id is not None
        counts[obs.captain_element_id] = counts.get(obs.captain_element_id, 0) + 1

    n = len(valid)
    probabilities = {str(element_id): round(count / n, 4) for element_id, count in counts.items()}

    if n < _MIN_CAPTAIN_OBSERVATIONS_FOR_MEDIUM_CONFIDENCE:
        confidence = RivalStateConfidence.LOW
    elif n < _MIN_CAPTAIN_OBSERVATIONS_FOR_HIGH_CONFIDENCE:
        confidence = RivalStateConfidence.MEDIUM
    else:
        confidence = RivalStateConfidence.HIGH
    return probabilities, confidence, n


def _chip_timing(history_chip_rows: list[dict]) -> str:
    if not history_chip_rows:
        return "none_played_yet"
    events = [c["event"] for c in history_chip_rows if c.get("event") is not None]
    if not events:
        return "unknown"
    avg_event = sum(events) / len(events)
    return "first_half" if avg_event <= _SEASON_HALF_EVENT_CUTOFF else "second_half"


def _template_vs_differential_lean(
    squad_selected_by_percent: list[float] | None, population_mean_selected_by_percent: float | None
) -> float | None:
    if not squad_selected_by_percent or population_mean_selected_by_percent is None:
        return None
    squad_avg = sum(squad_selected_by_percent) / len(squad_selected_by_percent)
    return round(squad_avg - population_mean_selected_by_percent, 2)


def _tendency_features(
    observed: list[dict],
    history_chip_rows: list[dict],
    captain_probabilities: dict[str, float],
    captain_observation_count: int,
    squad_selected_by_percent: list[float] | None,
    population_mean_selected_by_percent: float | None,
) -> dict:
    if observed:
        transfers_per_gameweek: float | None = sum(r.get("event_transfers", 0) for r in observed) / len(
            observed
        )
        roll_frequency: float | None = sum(1 for r in observed if r.get("event_transfers", 0) == 0) / len(
            observed
        )
        hit_tolerance: float | None = sum(
            1 for r in observed if (r.get("event_transfers_cost") or 0) < 0
        ) / len(observed)
    else:
        transfers_per_gameweek = None
        roll_frequency = None
        hit_tolerance = None

    captain_repeat_rate = (
        max(captain_probabilities.values())
        if captain_probabilities and captain_observation_count >= 2
        else None
    )

    return {
        "transfers_per_gameweek": round(transfers_per_gameweek, 3)
        if transfers_per_gameweek is not None
        else None,
        "roll_frequency": round(roll_frequency, 3) if roll_frequency is not None else None,
        "hit_tolerance": round(hit_tolerance, 3) if hit_tolerance is not None else None,
        "captain_repeat_rate": round(captain_repeat_rate, 3) if captain_repeat_rate is not None else None,
        "chip_timing": _chip_timing(history_chip_rows),
        "template_vs_differential_lean_delta": _template_vs_differential_lean(
            squad_selected_by_percent, population_mean_selected_by_percent
        ),
        "observed_non_chip_gameweeks": len(observed),
    }


def _predicted_transfer_targets(candidates: list[TransferTargetCandidate]) -> list[dict]:
    top = candidates[:_TOP_TRANSFER_TARGET_CANDIDATES]
    return [
        {
            "player_id": c.player_id,
            "web_name": c.web_name,
            "position": c.position,
            "projected_points": round(c.projected_points, 2),
            "selected_by_percent": c.selected_by_percent,
        }
        for c in top
    ]


def compute_rival_state(inputs: RivalStateInputs) -> RivalStateResult:
    """Compute the full DERIVED + PREDICTED state for one rival. Never raises.

    Every sub-computation degrades gracefully to an honest "unknown"/``LOW``
    confidence rather than erroring on thin/missing data — see the module
    docstring for the confidence-assignment rationale behind each threshold.
    """
    unknowns: list[str] = []

    ft_min, ft_max, ft_confidence = _derive_free_transfers(
        inputs.history_available,
        inputs.history_current_rows,
        inputs.history_chip_rows,
        inputs.free_transfers_cap,
    )
    if not inputs.history_available:
        unknowns.append(
            f"/history/ was unavailable for manager {inputs.fpl_manager_id} this run; free-transfer "
            "count, remaining-chip derivation, and transfer/hit-probability predictions could not be "
            "computed from this rival's own data this run."
        )
    elif ft_confidence != RivalStateConfidence.HIGH:
        unknowns.append(
            f"only {len(inputs.history_current_rows)} gameweek(s) of /history/ current[] observed for "
            f"manager {inputs.fpl_manager_id}; free-transfer estimate widened to a range rather than "
            "reported as a single confident value."
        )

    remaining_chips, chip_confidence = _derive_remaining_chips(
        inputs.history_available, inputs.chip_windows, inputs.history_chip_rows, inputs.current_event
    )
    if not inputs.history_available:
        unknowns.append(
            "chip-play history unavailable this run; the remaining-chips list assumes none observed as "
            "played, which may be stale or incomplete."
        )

    observed = _non_chip_rows(inputs.history_current_rows, inputs.history_chip_rows)
    no_t, one_t, multi_t, hit_t, transfer_confidence, used_prior = _transfer_and_hit_probabilities(observed)
    if used_prior:
        unknowns.append(
            f"fewer than {_MIN_HISTORY_ROWS_FOR_OWN_TRANSFER_DISTRIBUTION} non-chip gameweek(s) of own "
            "/history/ observed; transfer-count and hit probabilities fall back to a population-level "
            "prior (hand-picked, documented defaults -- this codebase does not ingest bootstrap-static's "
            "account-aggregate fields such as most_captained/most_transferred_in) rather than a "
            "rival-specific estimate."
        )

    captain_probabilities, captain_confidence, captain_n = _captain_probabilities(inputs.captain_observations)
    if captain_n == 0:
        unknowns.append(
            f"no available picks snapshot with a resolved captain exists for manager "
            f"{inputs.fpl_manager_id}; captain-tendency prediction is unknown."
        )
    elif captain_n == 1:
        unknowns.append(
            "only a single observed gameweek's captain choice exists for this rival; the one captain "
            "seen is reported at probability 1.0, but a single observation is not evidence of a stable "
            "tendency -- confidence is capped LOW."
        )

    transfer_targets = _predicted_transfer_targets(inputs.transfer_target_candidates)
    if not transfer_targets:
        unknowns.append(
            f"no candidate replacement targets available for manager {inputs.fpl_manager_id} (requires "
            "a resolvable current squad from an AVAILABLE picks snapshot); likely-transfer-targets is "
            "empty."
        )

    tendency = _tendency_features(
        observed,
        inputs.history_chip_rows,
        captain_probabilities,
        captain_n,
        inputs.squad_selected_by_percent,
        inputs.population_mean_selected_by_percent,
    )
    if inputs.squad_selected_by_percent is None or inputs.population_mean_selected_by_percent is None:
        unknowns.append(
            "template-vs-differential ownership lean could not be computed (missing resolvable squad "
            "ownership data or a population average)."
        )

    overall_confidence = _min_confidence(
        ft_confidence, chip_confidence, transfer_confidence, captain_confidence
    )

    evidence = {
        "history_available": inputs.history_available,
        "history_current_row_count": len(inputs.history_current_rows),
        "history_chip_row_count": len(inputs.history_chip_rows),
        "captain_observation_count": captain_n,
        "gameweek_event_range_observed": (
            [
                min(r["event"] for r in inputs.history_current_rows),
                max(r["event"] for r in inputs.history_current_rows),
            ]
            if inputs.history_current_rows
            else None
        ),
    }

    return RivalStateResult(
        last_known_snapshot_id=inputs.last_known_snapshot_id,
        derived_free_transfers_min=ft_min,
        derived_free_transfers_max=ft_max,
        derived_free_transfers_confidence=ft_confidence,
        derived_likely_remaining_chips=remaining_chips,
        predicted_no_transfer_probability=no_t,
        predicted_one_transfer_probability=one_t,
        predicted_multi_transfer_probability=multi_t,
        predicted_hit_probability=hit_t,
        predicted_captain_probabilities=captain_probabilities,
        predicted_likely_transfer_targets=transfer_targets,
        tendency_features=tendency,
        unknowns=unknowns,
        evidence=evidence,
        overall_confidence=overall_confidence,
    )
