"""The basic per-player points projection formula used by Milestone 2's optimizers.

No dependency on optimizer internals — Milestone 3 will later swap in real
model-generated projections behind this exact same ``PlayerProjection``
shape, so this stays a small, pure, deterministic function.

Milestone 3 Layer 4 adds ``project_player_with_champion`` alongside it (see
that function's own docstring) — ``project_player`` itself is left
completely unchanged so every pre-existing caller/test keeps working
verbatim.
"""

import logging
import math
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features.service import compute_features_for_player_gameweek
from app.modeling import baselines
from app.modeling.candidates import align_columns, build_feature_row
from app.optimization.champion_loader import LoadedChampion
from packages.database.models.player import Player
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.domain.modeling import BASELINE_MODEL_FAMILIES, ModelFamily

logger = logging.getLogger(__name__)

# v2: project_player_with_champion now falls back to project_player (FPL's own ep_next) for a
# cold-start season (see _is_cold_start_season) instead of trusting the champion model's raw
# prediction, which is systematically too low for that specific input shape -- a real behavior
# change to the formula's output, not an internal refactor.
PROJECTION_FORMULA_VERSION = 2

# Baseline families whose prediction is a direct, one-argument lookup against
# an already-computed feature row. BASELINE_RULES_EP_NEXT is deliberately
# excluded from this dict — it needs a DB lookup (session + gameweek_id), not
# just the feature row, so it's handled as its own branch below.
_BASELINE_FEATURE_ROW_PREDICT_FNS = {
    ModelFamily.BASELINE_SEASON_AVERAGE: baselines.predict_season_average,
    ModelFamily.BASELINE_RECENT_AVERAGE: baselines.predict_recent_average,
    ModelFamily.BASELINE_WEIGHTED_RECENT_AVERAGE: baselines.predict_weighted_recent_average,
}

# Injured / suspended / unavailable — never sound advice to recommend, so
# these statuses get no decision variable at all in the optimizer's
# candidate pool (hard exclusion, not a down-weight).
_HARD_EXCLUDE_STATUSES = {"i", "s", "u"}

# chance_of_playing_next_round (%) below which we down-weight the projection.
# Exactly 75 is NOT scaled (only strictly below it) — a player is presumed
# fit enough at 75% to be worth full consideration.
_AVAILABILITY_SCALE_THRESHOLD = 75


@dataclass(frozen=True)
class PlayerProjection:
    player_id: uuid.UUID
    projected_points: float
    source: str
    is_eligible: bool


def project_player(player: Player) -> PlayerProjection:
    """Project a single player's expected points for the target gameweek.

    ``ep_next`` (FPL's own forward-looking estimate) is preferred over
    ``points_per_game`` (backward-looking season average) as the primary
    source; ``points_per_game`` is the fallback when ``ep_next`` is null.
    Statuses ``i``/``s``/``u`` are hard-excluded (``is_eligible=False``,
    ``projected_points=0.0``) regardless of any other field. Statuses ``d``
    (doubtful) and ``a`` (available) are never hard-excluded — only
    down-weighted via the availability multiplier below the 75% threshold.
    """
    if player.status in _HARD_EXCLUDE_STATUSES:
        return PlayerProjection(player.id, 0.0, "unavailable_status_excluded", is_eligible=False)

    chance = player.chance_of_playing_next_round
    multiplier = (chance / 100) if chance is not None and chance < _AVAILABILITY_SCALE_THRESHOLD else 1.0

    if player.ep_next is not None:
        base = float(player.ep_next)
        source = "ep_next_availability_scaled" if multiplier < 1.0 else "ep_next"
    elif player.points_per_game is not None:
        base = float(player.points_per_game)
        source = "points_per_game_availability_scaled" if multiplier < 1.0 else "points_per_game"
    else:
        return PlayerProjection(player.id, 0.0, "no_data_zero", is_eligible=True)

    return PlayerProjection(player.id, base * multiplier, source, is_eligible=True)


def _predict_from_champion(
    session: Session, player: Player, target_gameweek_id: uuid.UUID, champion: LoadedChampion
) -> float | None:
    """Return a raw prediction from ``champion``, or ``None`` if it can't produce one for this player.

    Never raises — any failure while computing the live feature row or
    getting a prediction from it is the caller's job to catch (see
    ``project_player_with_champion``); this function's contract is just
    "give me a number, or tell me you can't".
    """
    feature_row = compute_features_for_player_gameweek(
        session, player.id, target_gameweek_id, champion.feature_version
    )

    if champion.model_family in BASELINE_MODEL_FAMILIES:
        if champion.model_family == ModelFamily.BASELINE_RULES_EP_NEXT:
            return baselines.predict_rules_based_ep_next(session, player.id, target_gameweek_id)
        predict_fn = _BASELINE_FEATURE_ROW_PREDICT_FNS[champion.model_family]
        return predict_fn(feature_row)

    # Tree family: build a 1-row matrix via the exact same construction logic
    # Layer 3 trains against, align it to the fitted model's own column set,
    # and predict.
    assert champion.sklearn_model is not None  # invariant: only baselines ever have sklearn_model=None
    x_row = build_feature_row(feature_row)
    x_aligned = align_columns(x_row, list(champion.sklearn_model.feature_names_in_))
    prediction = champion.sklearn_model.predict(x_aligned)[0]
    return float(prediction)


def _is_cold_start_season(session: Session, season_id: uuid.UUID) -> bool:
    """True when literally zero ``player_gameweek_stats`` rows exist for this season yet.

    Every rolling/within-season feature (``rolling_avg_points_*``,
    ``season_avg_points_to_date``, ``form_weighted_avg_points``, ...) is
    unconditionally ``None`` in this state (see
    ``app.features.engineering`` — each one returns ``None`` given an empty
    input, and a brand-new season's own gameweek 1 has no prior rows for
    ANY player to feed them). Confirmed live: for a real player with a
    strong ``career_avg_points_prior_seasons`` (a genuine cross-season
    signal, correctly populated), the champion model still predicted
    0.43 points — FPL's own ``ep_next`` for the same player was 4.0. The
    model was trained on rows where a null rolling-form feature is
    overwhelmingly a "this specific player isn't playing much" signal
    (mid-season churn/injury/rotation), not "it's literally the first
    gameweek of the season for everyone" — a real, structural blind spot,
    not a data or feature-engineering bug. See ``project_player_with_champion``.
    """
    return (
        session.execute(
            select(PlayerGameweekStats.id).where(PlayerGameweekStats.season_id == season_id).limit(1)
        ).first()
        is None
    )


def _cold_start_projection(
    session: Session, player: Player, target_gameweek_id: uuid.UUID
) -> PlayerProjection:
    """The cold-start fallback: blends ``project_player``'s own ``ep_next``-first estimate with
    ``career_avg_points_prior_seasons`` (a real, per-gameweek average computed from every
    earlier season this player has a record for, resolved cross-season via ``fpl_code`` — see
    ``app.features.engineering.career_avg_points_prior_seasons``), then adjusts by
    ``next_fixture_difficulty`` for THIS specific ``target_gameweek_id``, rather than using
    ``ep_next`` alone.

    Both ``ep_next`` and the career average are genuinely informative and genuinely different:
    ``ep_next`` is FPL's own current estimate; the career average is our own real historical
    record. Averaging them is more data-driven than picking one — confirmed on a real player:
    FPL's ep_next=4.0 for a striker whose real last-season average was 6.36 (both plausible,
    genuinely different signals; the blend is 5.18, not an arbitrary pick of either).

    Neither of those two inputs varies by ``target_gameweek_id`` at all, though (``ep_next`` is
    a single static field on ``player``; the career average is a season-spanning constant) —
    so summing this projection across a multi-gameweek horizon (see
    ``app.transfers.horizon_projection.compute_horizon_points``, used by
    ``app.jobs.handlers.squad_optimization`` for exactly this) would otherwise just be "N times
    the same number" for every player uniformly, never differentiating a player with an easy
    opening run of fixtures from one with a brutal one — the entire point of a horizon in the
    first place. ``next_fixture_difficulty`` (FPL's own 1=easiest..5=hardest rating for THIS
    target gameweek's specific fixture, already resolved home/away-aware by
    ``app.features.service``) is the one signal this fallback can compute that genuinely does
    vary per gameweek even with zero current-season history, so it's applied as a modest
    (±15% across the full 1-5 range, neutral at difficulty 3) multiplier on the blend.

    Falls back to the plain ``project_player`` estimate alone when no earlier-season history
    exists for this player at all (a brand-new-to-FPL signing — nothing to blend with) or when
    the feature row can't be computed for any reason (never allowed to fail the caller).
    """
    baseline = project_player(player)
    if not baseline.is_eligible:
        return baseline

    try:
        feature_row = compute_features_for_player_gameweek(session, player.id, target_gameweek_id)
    except Exception:  # noqa: BLE001 - this fallback must never itself fail the optimizer
        logger.warning(
            "Cold-start feature computation failed for player_id=%s target_gameweek_id=%s; "
            "using project_player() alone.",
            player.id,
            target_gameweek_id,
            exc_info=True,
        )
        return baseline

    career_avg = feature_row.career_avg_points_prior_seasons
    if career_avg is None:
        return baseline

    blended = (baseline.projected_points + float(career_avg)) / 2

    difficulty = feature_row.next_fixture_difficulty
    if difficulty is not None:
        blended *= 1 + (3 - difficulty) * 0.075

    return PlayerProjection(player.id, blended, "cold_start_blend_ep_next_and_career_avg", is_eligible=True)


def project_player_with_champion(
    session: Session,
    player: Player,
    target_gameweek_id: uuid.UUID,
    champion: LoadedChampion | None,
) -> PlayerProjection:
    """Champion-model-aware projection, falling back to ``project_player`` when unavailable.

    Order of checks:

    1. Hard-excluded statuses (``i``/``s``/``u``) short-circuit FIRST, before
       any model is even consulted — an ML model must never be able to
       resurrect a player ``project_player`` would exclude.
    2. If ``champion`` is ``None`` (no trained model exists yet): falls back
       to ``project_player(player)`` entirely, unchanged.
    3. If this is a cold-start season (see ``_is_cold_start_season``): the
       champion's own rolling-form-driven predictions are systematically,
       substantially too low for this specific input shape — falls back to
       ``_cold_start_projection(player)`` (a blend of FPL's own ``ep_next``
       and this player's real prior-season per-gameweek average) rather
       than trust a number the model was never meaningfully trained to
       produce.
    4. If a champion exists and it's not a cold-start season: attempts to
       compute this player's live feature row for ``target_gameweek_id``
       and get a prediction from it. If no feature row can be computed
       (e.g. a brand-new player with zero history/fixture context) or any
       step raises for any reason, catches it, logs a warning, and falls
       back to ``project_player(player)``.
    5. If the champion's prediction is itself unusable (``None`` or NaN —
       e.g. a baseline lookup with no season/career average to fall back on
       for a brand-new player), falls back to ``project_player(player)`` for
       this player individually — graceful degradation per-player, not
       all-or-nothing for the whole optimization run.
    6. On success, returns a ``PlayerProjection`` sourced as
       ``f"ml_champion_{champion.model_family.value}"`` so it's always
       possible to tell, after the fact, which players in a recommendation
       used the trained model versus the fallback formula.
    """
    if player.status in _HARD_EXCLUDE_STATUSES:
        return PlayerProjection(player.id, 0.0, "unavailable_status_excluded", is_eligible=False)

    if champion is None:
        return project_player(player)

    if _is_cold_start_season(session, player.season_id):
        return _cold_start_projection(session, player, target_gameweek_id)

    try:
        prediction = _predict_from_champion(session, player, target_gameweek_id, champion)
    except Exception:  # noqa: BLE001 - a champion-projection failure must never fail the optimizer
        logger.warning(
            "Champion projection failed for player_id=%s target_gameweek_id=%s "
            "model_version_id=%s; falling back to project_player().",
            player.id,
            target_gameweek_id,
            champion.model_version_id,
            exc_info=True,
        )
        return project_player(player)

    if prediction is None or math.isnan(prediction):
        return project_player(player)

    return PlayerProjection(
        player.id, prediction, f"ml_champion_{champion.model_family.value}", is_eligible=True
    )
