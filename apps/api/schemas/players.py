"""Pydantic response schemas for the /players endpoints."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.players.price_signal import PriceMomentum


class PlayerPriceMomentumOut(BaseModel):
    """See ``app.players.price_signal`` -- an explainable heuristic, not FPL's real algorithm."""

    momentum: PriceMomentum
    net_transfers_event: int | None
    estimated_owners: int | None
    momentum_ratio: Decimal | None


class PlayerSummary(BaseModel):
    """One row in a /players list response.

    Built manually from a joined (Player, Team, Position) row rather than via
    ``from_attributes`` on a bare ``Player`` — see
    ``apps.api.routers.players.list_players``.
    """

    id: uuid.UUID
    fpl_element_id: int
    web_name: str | None
    team_id: uuid.UUID | None
    team_short_name: str | None
    position: str | None
    now_cost: int
    status: str | None
    total_points: int
    form: Decimal | None
    points_per_game: Decimal | None
    ep_next: Decimal | None
    is_quarantined: bool
    # Same heuristic as PlayerDetail.price_momentum, computed once per request against the
    # active PriceSignalCalibration (see apps.api.routers.players.list_players) -- surfaced
    # here too so a manager scanning the whole list doesn't have to open each player individually
    # to see who's trending before locking in a squad.
    price_momentum: PlayerPriceMomentumOut | None = None


class PlayerListResponse(BaseModel):
    items: list[PlayerSummary]
    page: int
    page_size: int
    total: int


class PlayerDetail(BaseModel):
    """Full detail for a single player, matching the ``players`` table columns.

    Always includes ``is_quarantined``/``quarantine_reason`` regardless of
    the player's status — callers must not assume a returned player is
    necessarily eligible for optimization.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    fpl_element_id: int
    fpl_code: int | None
    season_id: uuid.UUID
    team_id: uuid.UUID | None
    team_short_name: str | None = None
    position_id: uuid.UUID | None
    position: str | None = None
    first_name: str | None
    second_name: str | None
    web_name: str | None
    now_cost: int
    status: str | None
    total_points: int
    minutes: int
    goals_scored: int
    assists: int
    clean_sheets: int
    goals_conceded: int
    own_goals: int
    penalties_saved: int
    penalties_missed: int
    yellow_cards: int
    red_cards: int
    saves: int
    bonus: int
    bps: int
    selected_by_percent: Decimal | None
    transfers_in_event: int | None
    transfers_out_event: int | None
    cost_change_event: int | None
    chance_of_playing_next_round: int | None
    chance_of_playing_this_round: int | None
    points_per_game: Decimal | None
    form: Decimal | None
    ep_this: Decimal | None
    ep_next: Decimal | None
    starts: int
    news: str | None
    is_quarantined: bool
    quarantine_reason: str | None
    # Not a Player column -- computed by the router via app.players.price_signal and attached
    # after model_validate (same pattern as team_short_name/position above), since it depends on
    # the season's total_players, not anything on the player row alone.
    price_momentum: PlayerPriceMomentumOut | None = None


class PlayerPriceChangeOut(BaseModel):
    """One OBSERVED ``now_cost`` change -- see ``packages.database.models.player_price_history``."""

    old_cost: int
    new_cost: int
    observed_at: datetime


class PlayerProjectionResponse(BaseModel):
    """The live output of ``app.optimization.projection.project_player`` for one player.

    The fields from ``is_cold_start_season`` onward are the "why this
    projection" breakdown (added post-review) -- the real inputs already
    computed behind ``projected_points``, not a new explainability system.
    See ``apps.api.routers.players.get_player_projection``.
    """

    player_id: uuid.UUID
    projected_points: float
    source: str
    is_eligible: bool
    projection_formula_version: int
    chance_of_playing_next_round: int | None
    status: str | None

    is_cold_start_season: bool
    # Distinct from is_cold_start_season above: True whenever THIS player individually has zero
    # player_gameweek_stats rows of their own this season (a late debut, a return from long-term
    # injury, ...), even if the season overall has moved on and is_cold_start_season is False --
    # see app.optimization.projection._is_cold_start_player. is_cold_start_season True always
    # implies this is also True (zero rows for everyone means zero for this player too); the
    # reverse is not true, which is exactly why both fields are surfaced separately rather than
    # collapsed into one.
    is_cold_start_player: bool
    # Echoes the raw inputs app.optimization.projection._cold_start_projection blends when
    # is_cold_start_season or is_cold_start_player is True -- None when not applicable (warm
    # player in a warm season, or no prior-season history for a brand-new player) rather than a
    # stale/misleading number.
    ep_next: Decimal | None
    # The plain full-season average, always shown for reference even when the blend actually
    # used the early-season-specific average instead (see career_avg_early_season_points_prior_seasons).
    career_avg_points_prior_seasons: Decimal | None
    # Average points across only each prior season's first few gameweeks (see
    # app.features.engineering.career_avg_early_season_points_prior_seasons) -- _cold_start_projection
    # prefers THIS over the plain full-season average whenever it exists, since a player's season-opening
    # form can differ meaningfully from their season-long average (well-known slow/fast starters).
    # None when the player has no early-season history across any prior season.
    career_avg_early_season_points_prior_seasons: Decimal | None
    next_fixture_difficulty: int | None
    # Set only when projected_points actually came from a champion model prediction (source
    # starts with "ml_champion_") -- None for every fallback/cold-start/no-champion path.
    champion_model_family: str | None
    champion_model_version_id: uuid.UUID | None
    # Set only when a player_availability_risk_notes row is active for this player and gameweek --
    # see app.optimization.projection.get_active_availability_risk_note. Distinct from FPL's own
    # official chance_of_playing_next_round-driven scaling (always reflected in `source` via
    # "_availability_scaled"): this is judgment-based, pre-official risk a human/research agent
    # flagged (e.g. a player short on match fitness after a long international tournament).
    availability_risk_reason: str | None = None
    availability_risk_severity: Decimal | None = None
    # Set only when a player_role_notes row is active for this player and gameweek -- see
    # app.optimization.projection.get_active_role_boost_note. A separate, independent mechanism
    # from availability_risk_* above (a boost, not a discount -- both can be active at once and
    # both apply, see that function's own docstring for why they're kept as two mechanisms).
    role_boost_reason: str | None = None
    role_boost_severity: Decimal | None = None
    # DEF/MID only -- this player's real historical rate of hitting FPL's 2025-26+ CBIT/CBIRT
    # defensive-contribution bonus threshold across every defensive_contribution-carrying gameweek
    # strictly before this projection's own target gameweek (see app.modeling.cold_start_validation
    # .defensive_contribution_hit_rate_prior_seasons). None for GKP/FWD, or when this player has no
    # such gameweek at all. PURELY INFORMATIONAL -- NOT used anywhere in projected_points; there is
    # currently no real, leakage-safe evidence to justify wiring it into the formula (see that
    # function's own docstring for the honest reason). Surfaced so a reader can see the same real
    # signal directly instead of hand-querying player_gameweek_stats.defensive_contribution.
    defensive_contribution_hit_rate_prior_seasons: float | None = None
    defensive_contribution_hit_rate_prior_games: int | None = None
    explanation: str


class AvailabilityRiskNoteCreate(BaseModel):
    """Body for ``POST /players/{player_id}/availability-risk-notes``.

    Severity-selection rubric, grounded in the real availability-risk notes created this
    season (read live via ``GET /players/notes/active``, not invented categories). Every note
    created so far falls into one of four bands, by how CONFIRMED and how LONG the reduced/
    absent involvement is expected to be. ``severity`` is a discount multiplier in ``(0, 1]``;
    LOWER means MORE severe (see ``app.optimization.projection.get_active_availability_risk_note``'s
    own "most severe wins" tie-break when several notes are active for the same player).

    - **0.0-0.10 -- confirmed out for an extended, specific period (weeks), club- or
      multi-source-confirmed injury/suspension with a defined-ish recovery timeline.** Leave
      literal ``0.0`` to the hard-exclude statuses ``i``/``s``/``u`` this table never needs to
      duplicate (see ``app.optimization.projection._HARD_EXCLUDE_STATUSES``) -- this band is
      for "realistically not featuring at normal involvement for multiple gameweeks", not "will
      not play at all this window".
      - Kroupi.Jr, 0.05: confirmed fifth-metatarsal-fracture surgery, multiple independent
        outlets agreeing on a 3-4 month layoff, for a genuinely popular (~185k owners) asset.
      - Minteh, 0.05: forced off with a leg injury in a pre-season friendly; club-communicated
        2-3 month expectation, on an already-depleted position group.
      - Madjo, 0.10: confirmed out for a named run of specific fixtures (opener plus 4 more,
        ~6-week timeline) after a debut injury -- slightly higher than the two above because the
        return point is more concretely bounded (a named fixture list, not "2-3 months").

    - **0.60-0.75 -- significant, sourced fatigue/match-fitness concern, not yet officially
      listed injured/doubtful by FPL, genuinely uncertain whether it costs the player a start.**
      The largest real category so far: players who went deep into a summer international
      tournament and return to club training only days before a season opener. Within this
      band, rank by tournament depth/minutes actually played and how late the return-to-
      training date is relative to the opener.
      - Senesi, 0.60: played the actual FINAL (~120 minutes in that match alone) -- the latest
        realistic return-to-club window of anyone noted so far.
      - Rice / Saka, 0.65 each: lost in the semifinal, confirmed not returning to club training
        until 11 days before the opener; Saka additionally reported playing through pain for
        much of the tournament.
      - Guehi, 0.65: same semifinal-loss tournament length as Rice/Saka, plus 120 minutes of a
        quarterfinal while managing a knock.

    - **0.80-0.95 -- a milder or more uncertain version of the 0.60-0.75 band, OR a genuine
      rotation/competition-for-minutes risk that is not injury-driven at all.** Real concern,
      but with a mitigating factor (an earlier partial return, a shorter tournament run, or
      simply "this player has to win a starting spot, not recover from anything") that argues
      for a smaller discount than the band above.
      - Rogers, 0.75: same semifinal-length tournament as Rice/Saka/Guehi, but also just
        completed a mid-tournament transfer to a new club -- kept at the low end of this band,
        not lower, because he'd already done one individual training session ahead of
        Rice/Saka's fully-absent status.
      - Lacroix, 0.85: real tournament minutes (150, including a 30th-minute sub appearance in
        the semifinal) but as a squad-depth/injury-cover option, not a full-tournament nailed
        starter -- milder than the heaviest-minutes cases.
      - Woltemade, 0.85: not a tournament-fatigue case at all -- a genuine three-way rotation
        competition for the same striker slot under a new manager, plus a real, quantified
        second-half-of-last-season decline (minutes ~79/game to ~41/game). Discount reflects
        real competition for a starting shirt, not an injury/unavailability concern -- still the
        same numeric band as the milder fatigue cases because the underlying uncertainty ("will
        this player start") is comparably real, just for a different reason.

    A genuinely new situation that doesn't fit one of these four bands (e.g. a confirmed,
    club-announced multi-MONTH injury for a nailed starter with no realistic return this
    season) should not be force-fit into the closest existing band -- go lower (nearer 0.0)
    than any of the above, same principle as the 0.05-0.10 band: the more confirmed and the
    longer the expected absence, the closer to 0. Cite the real note(s) above most similar to
    the new situation in the new note's own ``reason`` text -- this rubric is only useful if a
    future note-writer (human, Scout Watch's automated proposals, or a future session) can
    point at precedent, not just pick a number in isolation.
    """

    reason: str
    # Projection multiplier in (0, 1] -- e.g. 0.65 = discount to 65% of what the projection would
    # otherwise be. Validated at the boundary (see the router), not re-validated internally.
    severity: Decimal
    valid_from_fpl_event_id: int | None = None
    valid_until_fpl_event_id: int | None = None
    created_by: str = "manual"


class AvailabilityRiskNoteOut(BaseModel):
    """See ``packages.database.models.player_availability_risk_note``."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    player_id: uuid.UUID
    reason: str
    severity: Decimal
    valid_from_fpl_event_id: int | None
    valid_until_fpl_event_id: int | None
    created_at: datetime
    created_by: str


class RoleNoteCreate(BaseModel):
    """Body for ``POST /players/{player_id}/role-notes``.

    Severity-selection rubric, grounded in the real role-boost notes created this season (read
    live via ``GET /players/notes/active``, not invented categories). Every note created so far
    falls into one of four bands, by how CONFIRMED and how VALUABLE the specific role fact is.
    ``severity`` is a boost multiplier, normally ``> 1.0`` (see ``app.optimization.projection.
    get_active_role_boost_note``'s own "strongest confirmed claim wins" tie-break -- the INVERSE
    tie-break direction from availability-risk notes, since a role fact is usually unambiguous
    once properly sourced).

    - **1.05-1.10 -- confirmed for ONE real but narrower sub-role, while the MORE valuable
      sub-roles for the same player remain genuinely undecided.** Don't inflate severity to
      reflect a role the player might ALSO pick up later but hasn't been confirmed for yet.
      - Szoboszlai, 1.08: real, confirmed, well-sourced primary free-kick taker -- but penalty
        and corner duty are explicitly reported as undecided following a teammate's departure
        ("could be on everything", not yet confirmed). Kept modest specifically because the more
        valuable duties (penalties/corners) are still open.

    - **1.10-1.20 -- confirmed, cross-sourced primary set-piece duty (penalties and/or
      corners/free-kicks), continuing or newly assigned, with multiple independent trackers
      agreeing.** The clearest, most straightforwardly-verifiable category -- "who takes this
      team's penalties/corners" is close to a fact once 2-3 independent trackers agree.
      - Gross, 1.15: confirmed main penalty AND corner/free-kick taker, cross-verified by 3
        independent set-piece trackers.
      - Igor Thiago, 1.15: confirmed RETAINING first-choice penalty duty (not a new assignment)
        after a 22-goal season, again cross-verified by multiple independent trackers.

    - **1.30-1.40 -- a verified, QUANTITATIVE pattern in this app's OWN historical data that the
      automated projection formula demonstrably underweights.** Distinct from the two bands
      above (which are about a role FACT reported externally) -- this band is for when the
      evidence is a real, checked number already sitting in this app's own database, not outside
      reporting.
      - Joao Pedro, 1.35: early-season points-per-game improving every single season on record
        (0.17 -> 3.00 -> 3.17 -> 6.17), where the recency-weighted multi-season blend (half-life
        2.0 seasons) still lands well below the most recent, most representative season alone --
        a genuinely different justification from a "who takes penalties" role note, hence the
        higher band: it's correcting a demonstrated formula blind spot, not just encoding an
        external fact.

    - **1.45-1.65 -- a confirmed MAJOR status/role upgrade: promotion from a backup/fringe role
      to genuinely expected first-choice at a position of real attacking importance.** Reserved
      for the single biggest category of role change (not a set-piece duty, a straight
      playing-time/status upgrade) -- use sparingly, since this is the strongest boost in the
      table and should only apply when the OLD historical data is genuinely stale/misleading for
      what the player's role is NOW.
      - Osula, 1.6: historical ``career_avg_points_prior_seasons`` (~1.06) reflects an old
        fringe-backup role that real, current reporting says no longer applies -- reported as the
        CURRENT leading option for a starting striker slot under a new manager. The highest
        severity of any note so far, specifically because the gap between what the historical
        average implies and what current reporting says is the largest of any note created.

    As with availability-risk notes, cite the closest real precedent above in the new note's own
    ``reason`` text when creating a new one.
    """

    role: str
    reason: str
    # Projection multiplier -- normally > 1.0 (a boost), e.g. 1.15 = increase to 115% of what the
    # projection would otherwise be. Not hard-validated at the boundary (unlike
    # AvailabilityRiskNoteCreate's own (0, 1] convention) -- see
    # packages.database.models.player_role_note's own column comment for why this stays generic.
    severity: Decimal
    valid_from_fpl_event_id: int | None = None
    valid_until_fpl_event_id: int | None = None
    created_by: str = "manual"


class RoleNoteOut(BaseModel):
    """See ``packages.database.models.player_role_note``."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    player_id: uuid.UUID
    role: str
    reason: str
    severity: Decimal
    valid_from_fpl_event_id: int | None
    valid_until_fpl_event_id: int | None
    created_at: datetime
    created_by: str


class ActiveNoteOut(BaseModel):
    """One row in ``GET /players/notes/active`` -- every currently-active
    ``player_availability_risk_notes``/``player_role_notes`` row across every player, for the
    CURRENT/next unfinished gameweek (not player-scoped like the existing per-player note
    endpoints). Built for external, read-only consumers that can't reach this app's database
    directly (e.g. ``.github/workflows/scout-watch.yml``'s dedup step) to know what's already
    known before proposing something as new.

    ``category`` is ``"availability_risk"`` or ``"role_boost"`` -- a plain str, not an enum,
    mirroring ``player_role_notes.role``'s own "nothing branches on the exact string" reasoning.
    ``role`` is only ever set for ``role_boost`` rows (``None`` for ``availability_risk``, which
    has no equivalent column).
    """

    player_id: uuid.UUID
    web_name: str | None
    category: str
    role: str | None
    reason: str
    severity: Decimal
    valid_from_fpl_event_id: int | None
    valid_until_fpl_event_id: int | None
    created_at: datetime
    created_by: str
