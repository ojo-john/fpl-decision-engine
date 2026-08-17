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
from app.modeling.cold_start_validation import PriceComparableBucket, lookup_price_comparable_bucket
from app.optimization.champion_loader import LoadedChampion
from packages.database.models.cold_start_validation_run import ColdStartValidationRun
from packages.database.models.gameweek import Gameweek
from packages.database.models.player import Player
from packages.database.models.player_availability_risk_note import PlayerAvailabilityRiskNote
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.player_role_note import PlayerRoleNote
from packages.domain.modeling import BASELINE_MODEL_FAMILIES, ModelFamily

logger = logging.getLogger(__name__)

# v2: project_player_with_champion now falls back to project_player (FPL's own ep_next) for a
# cold-start season (see _is_cold_start_season) instead of trusting the champion model's raw
# prediction, which is systematically too low for that specific input shape -- a real behavior
# change to the formula's output, not an internal refactor.
# v3: the cold-start decision moved from _is_cold_start_season (whole-season, zero
# player_gameweek_stats rows ANYWHERE) to _is_cold_start_player (this specific player, zero
# player_gameweek_stats rows of their OWN) -- see _is_cold_start_player's own docstring. The old
# season-wide check meant the moment even one player anywhere had one gameweek of real data, every
# OTHER player switched to the champion model too, including ones still sitting on the exact same
# all-NaN-rolling-features input shape v2 was written to route around. Another real behavior
# change, not a refactor.
# v4: a true rookie (career_avg is None -- zero prior-season history at all, not just a missing
# early-season window) now gets _price_informed_true_rookie_projection's price-comparable blend
# instead of ep_next returned completely untouched -- see that function's own docstring and
# COLD_START_PRICE_PRIOR_BLEND_WEIGHT's real-evidence comment below. Another real behavior change.
PROJECTION_FORMULA_VERSION = 4

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

# _cold_start_projection's own two tunable constants -- both empirically validated against real
# historical seasons by app.jobs.handlers.cold_start_projection_validation (see
# app.modeling.cold_start_validation for the leakage-safe evaluation harness); real sweep results
# are persisted per-run in cold_start_validation_runs, and ARCHITECTURE.md's cold-start-validation
# section records the most recent run's actual numbers.
#
# Weight on the career_avg component in the ep_next/career_avg blend (ep_next gets
# 1 - COLD_START_BLEND_CAREER_AVG_WEIGHT). Swept against a documented, imperfect proxy for
# historical ep_next (see sweep_blend_ratio_candidates's own docstring for the caveat) -- treated as
# directional evidence only, not conclusive, given that caveat.
#
# Empirically checked (2026-08-17) against the real dev database's full historical dataset
# (n=2348 real (player, season) pairs, {0.3, 0.4, 0.5, 0.6, 0.7} swept): real MAE was LOWEST at
# 0.5 itself (1.1043), against 1.1170/1.1057/1.1225 for 0.4/0.6/0.7 and 1.1427 for 0.3 -- despite
# the proxy caveat above, the sweep did not surface a case to move away from 0.5, so it was left
# unchanged. See ARCHITECTURE.md's cold-start-validation section for the full real numbers.
COLD_START_BLEND_CAREER_AVG_WEIGHT = 0.5

# Fixture-difficulty adjustment coefficient: +/- this fraction per point of next_fixture_difficulty
# away from neutral (3) -- e.g. 0.075 means +/-15% across the full 1-5 range. Swept at real
# per-gameweek granularity using real historical fixture difficulty ratings and real actual points
# (no ep_next dependency, so no proxy caveat applies to this one).
#
# Empirically validated (2026-08-17) against the real dev database (n=13954 real per-gameweek
# observations, {0.0, 0.025, 0.05, 0.075, 0.1, 0.15} swept): real MAE bottomed out at 0.05/0.075
# (1.65455/1.65454, effectively tied) versus 1.65603 for the 0.0 (no-adjustment) control -- 0.075
# is already at the empirical minimum, so it was left unchanged. See ARCHITECTURE.md's
# cold-start-validation section for the full real numbers.
COLD_START_DIFFICULTY_ADJUSTMENT_COEFFICIENT = 0.075

# Weight on the price-comparable-bucket estimate for a TRUE ROOKIE (career_avg is None -- zero
# strictly-prior-season history at all, not just a missing early-season window) in its own blend
# with baseline.projected_points (ep_next gets 1 - weight). 0.0 means "use ep_next alone,
# unchanged" -- today's actual behavior, since this population previously got no independent
# signal at all. See app.modeling.cold_start_validation.sweep_price_prior_blend_weight_candidates
# for the real sweep and its own documented, WORSE-than-COLD_START_BLEND_CAREER_AVG_WEIGHT's caveat
# (a true rookie has no prior-season ep_next to fall back on for a leakage-safe historical proxy,
# unlike every other cold-start player).
#
# Empirically checked (2026-08-17) against the real dev database's true-rookie population (n=735
# real observations, {0.0, 0.3, 0.5, 0.7, 1.0} swept): real MAE was 1.0067 at weight=0.0 (pure
# ep_next -- today's PRE-this-feature behavior for this population) versus 0.8673/0.8178/0.8031/
# 0.8417 for 0.3/0.5/0.7/1.0 -- LOWEST at 0.7, a ~20% MAE reduction over ep_next alone. Given
# sweep_price_prior_blend_weight_candidates's own documented caveat that weight=0.0's MAE here is
# ALREADY optimistically biased in ep_next's favor (its own proxy has more hindsight than the live
# formula's genuinely pre-season ep_next would), this is if anything a CONSERVATIVE case for
# enabling the price prior, not an inflated one. See ARCHITECTURE.md's cold-start-validation
# section for the full real numbers.
COLD_START_PRICE_PRIOR_BLEND_WEIGHT = 0.7


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
    not a data or feature-engineering bug.

    NOT consulted by ``project_player_with_champion`` any more (see
    ``_is_cold_start_player``, which subsumes this check at a finer grain) —
    kept as its own function/public wrapper because it still answers a real,
    distinct, season-wide question other callers legitimately need: "has
    this season produced any data at all yet" (e.g. ``POST /optimize/preview``'s
    ``is_cold_start_gameweek`` informational flag, `apps.api.routers.optimization
    .preview_squad`). A season-wide "yes" always implies every individual
    player is also individually cold-start (there are zero rows for anyone,
    so certainly zero for any one player) — the reverse is not true, which is
    exactly the gap ``_is_cold_start_player`` closes.
    """
    return (
        session.execute(
            select(PlayerGameweekStats.id).where(PlayerGameweekStats.season_id == season_id).limit(1)
        ).first()
        is None
    )


def is_cold_start_season(session: Session, season_id: uuid.UUID) -> bool:
    """Public wrapper around ``_is_cold_start_season`` for callers outside this module that need
    to know whether cold-start logic applies without duplicating the query — e.g. the
    ``/players/{player_id}/projection`` endpoint's "why this projection" breakdown
    (``apps.api.routers.players.get_player_projection``), which needs this alongside
    ``project_player_with_champion``'s own output to explain which code path produced it."""
    return _is_cold_start_season(session, season_id)


def _is_cold_start_player(session: Session, player_id: uuid.UUID) -> bool:
    """True when literally zero ``player_gameweek_stats`` rows exist for THIS SPECIFIC player
    this season yet — the same "zero rows means every rolling/within-season feature is
    unconditionally None" predicate ``_is_cold_start_season`` applies, just scoped to one player
    instead of the whole season.

    This is the real fix for a gap in ``_is_cold_start_season`` alone: that check is season-wide,
    so the moment even ONE player anywhere in the season has one gameweek of real data ingested,
    EVERY player switches from the cold-start blend to the champion model's raw prediction —
    including players who themselves still have zero or almost no current-season history (a
    summer signing who debuts a few gameweeks late, a player returning from a long-term injury
    mid-season, a player whose own team had a postponed opening fixture). Those players sit on
    exactly the same all-rolling-features-NaN input shape ``_is_cold_start_season`` was written to
    route around — the champion was never meaningfully trained on that shape, and nothing about
    *other* players having data changes that for *this* one.

    Threshold is a hard cutover at exactly 1 row, deliberately mirroring ``_is_cold_start_season``'s
    own all-or-nothing threshold rather than inventing a new "how many games is enough" rule: one
    real ``player_gameweek_stats`` row is enough to make every rolling-window feature
    (``rolling_avg_points_3gw``/``5gw``/``10gw``, ``season_avg_points_to_date``,
    ``form_weighted_avg_points``, one of ``home_avg_points``/``away_avg_points``) non-null instead
    of uniformly ``NaN`` — see ``app.features.engineering``, every one of those functions returns a
    real average (over however many rows exist, even just one) rather than ``None`` once given any
    row at all. That NaN-vs-real-value distinction, not sample size/noise, is the documented root
    cause of the champion's systematic under-prediction, so it's also the correct cutover point
    here — a smoothed multi-gameweek transition would be solving a different (noise-reduction)
    problem this fix isn't scoped to.
    """
    return (
        session.execute(
            select(PlayerGameweekStats.id).where(PlayerGameweekStats.player_id == player_id).limit(1)
        ).first()
        is None
    )


def is_cold_start_player(session: Session, player_id: uuid.UUID) -> bool:
    """Public wrapper around ``_is_cold_start_player``, mirroring ``is_cold_start_season``'s own
    "public wrapper around a private helper" precedent — for callers outside this module (e.g.
    the ``/players/{player_id}/projection`` "why this projection" breakdown) that need to know
    whether THIS player individually triggered the cold-start blend, distinct from
    ``is_cold_start_season``'s whole-season question."""
    return _is_cold_start_player(session, player_id)


def _load_current_price_comparable_buckets(session: Session) -> list[PriceComparableBucket]:
    """The most recently computed ``price_comparable_buckets_json`` artifact from
    ``cold_start_validation_runs`` -- mirrors ``app.optimization.champion_loader
    .load_current_champion``'s own "query the most recent real artifact, degrade gracefully if
    none exists yet" precedent. Returns ``[]`` (never raises) if no run has ever populated this
    column yet -- a normal, expected state (fresh environment, ``COLD_START_PROJECTION_VALIDATION``
    never run) exactly like a missing champion, not an error.
    """
    row_json = session.execute(
        select(ColdStartValidationRun.price_comparable_buckets_json)
        .where(ColdStartValidationRun.price_comparable_buckets_json.is_not(None))
        .order_by(ColdStartValidationRun.completed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not row_json:
        return []
    return [PriceComparableBucket(**bucket) for bucket in row_json]


def _price_informed_true_rookie_projection(
    session: Session, player: Player, feature_row: PlayerFeatures, baseline: PlayerProjection
) -> PlayerProjection:
    """The fallback for a player with LITERALLY ZERO prior-season history (``career_avg is None``
    in ``_cold_start_projection`` -- both the early-season and full-season career averages are
    unavailable, not just the early-season one). Previously returned ``baseline`` (``ep_next``)
    completely untouched; this is the single least-informed population getting the least scrutiny.

    Blends a price-comparable-bucket estimate (see ``app.modeling.cold_start_validation
    .compute_price_comparable_buckets`` for how the lookup table itself is built, and
    ``sweep_price_prior_blend_weight_candidates`` for the real historical evidence behind
    ``COLD_START_PRICE_PRIOR_BLEND_WEIGHT``) with ``baseline`` at that validated weight, then
    applies the SAME fixture-difficulty adjustment ``_cold_start_projection`` already applies to
    every other cold-start player -- this population deserves that adjustment just as much, it was
    simply unreachable before because the function returned early.

    Falls back to ``baseline`` untouched (never raises, never degrades the caller) if this
    player's price/position can't be resolved from ``feature_row``, or if no price-comparable
    lookup table has ever been computed yet (``COLD_START_PROJECTION_VALIDATION`` never run) --
    exactly mirroring every other graceful-degradation branch in this module.
    """
    if feature_row.price_at_time is None or feature_row.position_short_name is None:
        return baseline

    buckets = _load_current_price_comparable_buckets(session)
    bucket = lookup_price_comparable_bucket(
        buckets, feature_row.position_short_name, int(feature_row.price_at_time)
    )
    if bucket is None:
        return baseline

    blended = (
        COLD_START_PRICE_PRIOR_BLEND_WEIGHT * bucket.avg_early_season_points
        + (1 - COLD_START_PRICE_PRIOR_BLEND_WEIGHT) * baseline.projected_points
    )

    difficulty = feature_row.next_fixture_difficulty
    if difficulty is not None:
        blended *= 1 + (3 - difficulty) * COLD_START_DIFFICULTY_ADJUSTMENT_COEFFICIENT

    return PlayerProjection(
        player.id, blended, "cold_start_price_informed_true_rookie_blend", is_eligible=True
    )


def _cold_start_projection(
    session: Session, player: Player, target_gameweek_id: uuid.UUID
) -> PlayerProjection:
    """The cold-start fallback: blends ``project_player``'s own ``ep_next``-first estimate with a
    real cross-season points average for this player, resolved via ``fpl_code`` — preferring
    ``career_avg_early_season_points_prior_seasons`` (average points across only each prior
    season's own EARLY gameweeks — see
    ``app.features.engineering.career_avg_early_season_points_prior_seasons``) over
    ``career_avg_points_prior_seasons`` (a plain full-season average) when the player has any
    early-season history at all, then adjusts by ``next_fixture_difficulty`` for THIS specific
    ``target_gameweek_id``, rather than using ``ep_next`` alone.

    Why early-season specifically, not full-season: a full-season average conflates a player's
    September form with their April form into one number that represents neither, and some
    players are well-established slow starters (need match fitness/rhythm) or fast starters
    (thrive on a pre-season rebuild) relative to their own season-long average — exactly the
    thing this projection is trying to answer when it's projecting a fresh season's own opening
    gameweeks. Falls back to the full-season career average when no early-season-specific
    history exists (e.g. a player who only ever joined the league mid-season in every prior
    season they have a record for) — still more informative than nothing, just less precise
    than the early-season-specific number when that's available.

    Both ``ep_next`` and the (early-season-preferring) career average are genuinely informative
    and genuinely different: ``ep_next`` is FPL's own current estimate; the career average is
    our own real historical record. Averaging them is more data-driven than picking one —
    confirmed on a real player: FPL's ep_next=4.0 for a striker whose real last-season average
    was 6.36 (both plausible, genuinely different signals; the blend is 5.18, not an arbitrary
    pick of either).

    Neither of those two inputs varies by ``target_gameweek_id`` at all, though (``ep_next`` is
    a single static field on ``player``; the career average — either version — is a
    season-spanning constant) — so summing this projection across a multi-gameweek horizon (see
    ``app.transfers.horizon_projection.compute_horizon_points``, used by
    ``app.jobs.handlers.squad_optimization`` for exactly this) would otherwise just be "N times
    the same number" for every player uniformly, never differentiating a player with an easy
    opening run of fixtures from one with a brutal one — the entire point of a horizon in the
    first place. ``next_fixture_difficulty`` (FPL's own 1=easiest..5=hardest rating for THIS
    target gameweek's specific fixture, already resolved home/away-aware by
    ``app.features.service``) is the one signal this fallback can compute that genuinely does
    vary per gameweek even with zero current-season history, so it's applied as a modest
    (±15% across the full 1-5 range, neutral at difficulty 3) multiplier on the blend.

    A player with NO earlier-season history at all (``career_avg`` — both the early-season and
    full-season versions — is ``None``: a genuinely new-to-FPL signing, not just missing an
    early-season window) is handed off to ``_price_informed_true_rookie_projection`` instead of
    returning ``project_player``'s estimate untouched — see that function's own docstring. Falls
    back to the plain ``project_player`` estimate alone only when the feature row can't be
    computed for any reason (never allowed to fail the caller).
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

    career_avg = (
        feature_row.career_avg_early_season_points_prior_seasons
        if feature_row.career_avg_early_season_points_prior_seasons is not None
        else feature_row.career_avg_points_prior_seasons
    )
    if career_avg is None:
        return _price_informed_true_rookie_projection(session, player, feature_row, baseline)

    blended = (
        COLD_START_BLEND_CAREER_AVG_WEIGHT * float(career_avg)
        + (1 - COLD_START_BLEND_CAREER_AVG_WEIGHT) * baseline.projected_points
    )

    difficulty = feature_row.next_fixture_difficulty
    if difficulty is not None:
        blended *= 1 + (3 - difficulty) * COLD_START_DIFFICULTY_ADJUSTMENT_COEFFICIENT

    return PlayerProjection(player.id, blended, "cold_start_blend_ep_next_and_career_avg", is_eligible=True)


# Appended to PlayerProjection.source whenever _apply_availability_risk_notes discounts it --
# callers that branch on exact source strings (e.g. apps.api.routers.players._explain_projection)
# must strip this suffix before matching, and use it to also surface the note's own reason text.
AVAILABILITY_RISK_NOTED_SUFFIX = "_availability_risk_noted"


def get_active_availability_risk_note(
    session: Session, player_id: uuid.UUID, target_gameweek_id: uuid.UUID
) -> PlayerAvailabilityRiskNote | None:
    """The single most-severe active ``player_availability_risk_notes`` row for this player and
    gameweek, or ``None`` if none applies. Public so callers outside this module (e.g. the "why
    this projection" breakdown) can find the same note ``_apply_availability_risk_notes`` would
    apply, without duplicating the gameweek-number resolution + active-window filtering logic --
    mirrors ``is_cold_start_season``'s own "public wrapper around a private helper" precedent.

    Looks up by this gameweek's ``fpl_event_id`` (not ``target_gameweek_id`` directly) since notes
    are authored in terms of gameweek NUMBERS ("valid from GW1 to GW3"), not opaque row ids. If
    more than one note is active for the same player and gameweek, the MOST severe (lowest
    multiplier) wins — understating a real risk is worse than overstating it here.
    """
    fpl_event_id = session.execute(
        select(Gameweek.fpl_event_id).where(Gameweek.id == target_gameweek_id)
    ).scalar_one_or_none()
    if fpl_event_id is None:
        return None

    notes = (
        session.execute(
            select(PlayerAvailabilityRiskNote).where(PlayerAvailabilityRiskNote.player_id == player_id)
        )
        .scalars()
        .all()
    )
    active = [
        note
        for note in notes
        if (note.valid_from_fpl_event_id is None or fpl_event_id >= note.valid_from_fpl_event_id)
        and (note.valid_until_fpl_event_id is None or fpl_event_id <= note.valid_until_fpl_event_id)
    ]
    if not active:
        return None
    return min(active, key=lambda note: note.severity)


def _apply_availability_risk_notes(
    session: Session, target_gameweek_id: uuid.UUID, projection: PlayerProjection
) -> PlayerProjection:
    """Apply the active ``player_availability_risk_notes`` row (if any) as an additional multiplier.

    Stacks with (does not replace) the ``chance_of_playing_next_round``-based multiplier
    ``project_player`` already applies — these are two independent, legitimate reasons a
    projection might be too high: FPL's own official injury/doubt data (refreshed every
    ``DATA_INGESTION`` run) versus a judgment-based, pre-official risk a human or research agent
    has flagged (e.g. a player who went deep into an international tournament and returns to club
    training only days before a season opener, without yet being listed injured/doubtful by FPL).

    A no-op for an already-ineligible projection (``is_eligible=False``, e.g. hard-excluded by
    status) — there's nothing to discount further, and appending a note-driven source suffix to
    ``"unavailable_status_excluded"`` would be misleading.
    """
    if not projection.is_eligible:
        return projection

    note = get_active_availability_risk_note(session, projection.player_id, target_gameweek_id)
    if note is None:
        return projection

    return PlayerProjection(
        projection.player_id,
        projection.projected_points * float(note.severity),
        f"{projection.source}{AVAILABILITY_RISK_NOTED_SUFFIX}",
        is_eligible=True,
    )


# Appended to PlayerProjection.source whenever _apply_role_boost_notes boosts it -- mirrors
# AVAILABILITY_RISK_NOTED_SUFFIX; both suffixes can be present at once (order: risk suffix first,
# then role suffix -- see _apply_role_boost_notes' own docstring for why order doesn't affect the
# math but is fixed for a deterministic, greppable source string).
ROLE_BOOSTED_SUFFIX = "_role_boosted"


def get_active_role_boost_note(
    session: Session, player_id: uuid.UUID, target_gameweek_id: uuid.UUID
) -> PlayerRoleNote | None:
    """The single strongest active ``player_role_notes`` row for this player and gameweek, or
    ``None`` if none applies. Public so callers outside this module (e.g. the "why this
    projection" breakdown) can find the same note ``_apply_role_boost_notes`` would apply,
    without duplicating the gameweek-number resolution + active-window filtering logic --
    mirrors ``get_active_availability_risk_note``'s own "public wrapper" precedent.

    Looks up by this gameweek's ``fpl_event_id`` (not ``target_gameweek_id`` directly), exactly
    like ``get_active_availability_risk_note``. If more than one role note is active for the same
    player and gameweek, the HIGHEST severity (strongest boost) wins -- the INVERSE tie-break
    direction from risk notes, deliberately: a risk note's underlying fact is often uncertain
    (a rotation doubt, not a confirmed absence), so preferring the most cautious read make sense
    there. A role note's underlying fact ("who currently takes penalties") is usually unambiguous
    once properly sourced, so preferring the strongest/most specific confirmed claim is the more
    sensible default here -- see this table's own module docstring for the fuller reasoning.
    """
    fpl_event_id = session.execute(
        select(Gameweek.fpl_event_id).where(Gameweek.id == target_gameweek_id)
    ).scalar_one_or_none()
    if fpl_event_id is None:
        return None

    notes = (
        session.execute(select(PlayerRoleNote).where(PlayerRoleNote.player_id == player_id)).scalars().all()
    )
    active = [
        note
        for note in notes
        if (note.valid_from_fpl_event_id is None or fpl_event_id >= note.valid_from_fpl_event_id)
        and (note.valid_until_fpl_event_id is None or fpl_event_id <= note.valid_until_fpl_event_id)
    ]
    if not active:
        return None
    return max(active, key=lambda note: note.severity)


def _apply_role_boost_notes(
    session: Session, target_gameweek_id: uuid.UUID, projection: PlayerProjection
) -> PlayerProjection:
    """Apply the active ``player_role_notes`` row (if any) as an additional multiplier.

    Stacks with (does not replace) ``_apply_availability_risk_notes`` -- these are two
    independent, legitimate reasons a projection might need adjusting in OPPOSITE directions at
    once (e.g. a player carrying a minor knock who has ALSO just been handed penalty duty). Called
    strictly AFTER ``_apply_availability_risk_notes`` in ``project_player_with_champion`` so both
    multipliers compound (multiplication is commutative, so the order doesn't change the final
    number -- but the ORDER OF SUFFIXES on the source string is fixed by this call order, risk
    suffix first, for a deterministic, greppable string).

    Same no-op-for-ineligible guard as ``_apply_availability_risk_notes`` -- there's nothing to
    boost on a hard-excluded (0-point) projection.
    """
    if not projection.is_eligible:
        return projection

    note = get_active_role_boost_note(session, projection.player_id, target_gameweek_id)
    if note is None:
        return projection

    return PlayerProjection(
        projection.player_id,
        projection.projected_points * float(note.severity),
        f"{projection.source}{ROLE_BOOSTED_SUFFIX}",
        is_eligible=True,
    )


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
    3. If THIS PLAYER is individually cold-start (see ``_is_cold_start_player``
       — zero ``player_gameweek_stats`` rows of their own this season, so
       every rolling/within-season feature is ``NaN``, regardless of whether
       other players elsewhere in the season already have data): the
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
    7. Whatever the above produces (steps 2-6), two final passes apply any active
       ``player_availability_risk_notes`` (a discount — see ``_apply_availability_risk_notes``)
       and any active ``player_role_notes`` (a boost — see ``_apply_role_boost_notes``) for this
       player/gameweek, in that order, so both can compound if both are active at once (e.g. a
       player carrying a minor knock who has ALSO just been handed penalty duty). Neither step is
       skipped for the ``project_player`` fallback branches (2, 4, 5): a flagged risk or a
       confirmed role change is just as real whether or not a champion model happened to be
       available.
    """
    if player.status in _HARD_EXCLUDE_STATUSES:
        return PlayerProjection(player.id, 0.0, "unavailable_status_excluded", is_eligible=False)

    if champion is None:
        projection = project_player(player)
    elif _is_cold_start_player(session, player.id):
        projection = _cold_start_projection(session, player, target_gameweek_id)
    else:
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
            projection = project_player(player)
        else:
            if prediction is None or math.isnan(prediction):
                projection = project_player(player)
            else:
                projection = PlayerProjection(
                    player.id, prediction, f"ml_champion_{champion.model_family.value}", is_eligible=True
                )

    projection = _apply_availability_risk_notes(session, target_gameweek_id, projection)
    return _apply_role_boost_notes(session, target_gameweek_id, projection)
