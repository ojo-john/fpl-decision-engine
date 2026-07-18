"""``ingest_outcome_for_audit``: Milestone 7 Layer 2's DB-session-taking orchestrator.

Given an already-resolved ``recommendation_audits`` row whose
``target_gameweek_id`` is confirmed ``finished=True``, this module resolves
the real outcome (what actually happened) and computes whichever of the 8
``CounterfactualComparisonType``s have enough real data available, persisting
one ``recommendation_outcomes`` row plus its ``counterfactual_results``
children.

This is a plain, directly-callable function -- NOT wired into a
``JobType``/registered handler (see module's own docstring in
``app/learning/__init__.py``). Milestone 7 Layer 4's ``GAMEWEEK_REVIEW`` job
will call ``ingest_outcome_for_audit(session, recommendation_audit_id)`` as
its own building block once it exists; until then this is tested and used
directly.

Idempotency: UPSERT-IN-PLACE. See ``RecommendationOutcome``'s own docstring
for the full rationale -- a second call for the same audit updates the
existing row's scalar fields and wholesale-replaces its
``counterfactual_results`` children (delete then re-insert), rather than
refusing or accumulating a duplicate.

Component availability -- not all 8 comparisons apply every gameweek:

- RECOMMENDED_VS_ACTUAL_TEAM / RECOMMENDED_VS_ACTUAL_CAPTAIN need BOTH
  ``optimization_run_id`` (the recommended XI/captain) AND a resolvable real
  ``fpl_manager_id`` with a post-gameweek ``MANAGER_SYNC`` snapshot for this
  exact gameweek (the "actual" side) -- a ``MANUAL_ENTRY``-sourced squad (no
  real ``fpl_manager_id``) or a manager who never synced after this gameweek
  finished skips both, a real and common gap, never fabricated.
- TRANSFER_VS_NO_TRANSFER / TRANSFER_VS_TOP_ALTERNATIVE / HIT_VS_NO_HIT need
  ``transfer_optimization_run_id``; each additionally needs its own specific
  scenario shape to exist (see ``_build_transfer_comparisons``) -- e.g.
  HIT_VS_NO_HIT only when the recommended scenario actually took a real hit.
- CHIP_VS_HOLD needs ``chip_optimization_run_id`` AND a resolvable actual
  snapshot showing the user genuinely played a chip this gameweek that this
  system had recommended USE_NOW for, AND that chip being BENCH_BOOST or
  TRIPLE_CAPTAIN specifically -- see its own section below for why Wildcard/
  Free Hit are out of scope for a cheap, honestly-computed realized gain.
- OVERALL_VS_H2H_ADJUSTED / CONSERVATIVE_VS_AGGRESSIVE need
  ``h2h_simulation_run_id`` with a resolvable recommended candidate / a
  usable conservative-aggressive pair respectively.

Known, documented scope limitations (see also each helper's own docstring):

- Actual XI scoring never models real FPL automatic substitutions -- it sums
  whichever 11 players sat at picks positions 1-11 as originally submitted.
  A blanking starter whose autosub covered them well is not credited here;
  this mirrors ``app.h2h.simulate``'s own documented, simplified autosub
  scope decision (a full autosub replay is a real, nontrivial resolution
  path already implemented ONLY inside ``app.jobs.handlers.h2h_simulation``,
  private to that module -- duplicating it here is future work, not silently
  skipped).
- H2H strategy candidates' "realized" points (OVERALL_VS_H2H_ADJUSTED /
  CONSERVATIVE_VS_AGGRESSIVE) are computed over the SAME base starting XI
  ``h2h_simulation`` started from, varying ONLY the captain identity per
  candidate -- a ``NO_HIT``/``CALCULATED_HIT`` candidate's own real squad
  change (via its ``transfer_optimization_run_scenario_id``) is NOT replayed
  into the realized XI here. Modeling that correctly would require the same
  autosub/bench-order replay noted above; documented as a known
  simplification, not silently wrong.
- Transfer scenario "realized" net gain only ever covers the ONE gameweek
  that has actually finished (this audit's own target gameweek), not the
  full multi-gameweek horizon the original Milestone 4 projection covered --
  a true full-horizon realized comparison needs every gameweek in that
  horizon to finish first, which is Layer 4's accumulation job to do, not
  this one-shot ingestion call.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.learning.actuals import sum_actual_points
from app.learning.counterfactuals import (
    CounterfactualResultData,
    compare_chip_vs_hold,
    compare_conservative_vs_aggressive,
    compare_overall_vs_h2h_adjusted,
    compare_recommended_vs_actual_captain,
    compare_recommended_vs_actual_team,
    compare_transfer_scenarios,
)
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.chip_recommendation import ChipRecommendation
from packages.database.models.chip_window_candidate import ChipWindowCandidate
from packages.database.models.counterfactual_result import CounterfactualResult
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_match import H2HMatch
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.h2h_strategy_candidate import H2HStrategyCandidate
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.player import Player
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.recommendation_outcome import RecommendationOutcome
from packages.database.models.rival_gameweek_snapshot import RivalGameweekSnapshot
from packages.database.models.rival_profile import RivalProfile
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.database.models.transfer_optimization_run_move import TransferOptimizationRunMove
from packages.database.models.transfer_optimization_run_scenario import TransferOptimizationRunScenario
from packages.domain.chips import ChipType, ChipVerdict
from packages.domain.h2h import H2HStrategyLabel
from packages.domain.learning import CounterfactualComparisonType
from packages.domain.transfers import ManagerSquadSnapshotSource

__all__ = ["ingest_outcome_for_audit", "RecommendationAuditNotFoundError", "GameweekNotFinishedError"]

_STARTING_XI_MAX_POSITION = 11
_CAPTAIN_MULTIPLIER_DEFAULT = 2

# Chips this layer can compute a cheap, honestly-scoped REALIZED gain for --
# see module docstring's "CHIP_VS_HOLD" section for why Wildcard/Free Hit
# are excluded (a squad rebuild has no simple incremental-points formula the
# way Bench Boost/Triple Captain do).
_CHIP_VS_HOLD_SUPPORTED_CHIP_TYPES = (ChipType.BBOOST, ChipType.TRIPLE_CAPTAIN)


class RecommendationAuditNotFoundError(Exception):
    """Raised when ``recommendation_audit_id`` doesn't resolve to a real row."""


class GameweekNotFinishedError(Exception):
    """Raised when the audit's own target gameweek is not yet ``finished=True``.

    Outcomes must never be computed against a still-in-progress gameweek --
    ``player_gameweek_stats``/``h2h_matches`` are still actively changing, so
    any comparison computed now would be silently wrong once the gameweek
    actually completes.
    """


# --- Small, self-contained parsing helpers (deliberately duplicated, not imported -- these mirror
# --- app.jobs.handlers.h2h_simulation's own private _split_starting_and_bench, which is private to
# --- that module; same documented duplication precedent as e.g. app.chips.scenario's own
# --- _build_full_candidate_pool vs. app.jobs.handlers.squad_optimization._build_candidates --------


def _split_starting_and_bench(raw_picks_json: dict | None) -> tuple[list[dict], list[dict]]:
    """Split a picks payload into (starting XI, bench-in-priority-order)."""
    if not raw_picks_json or not raw_picks_json.get("picks"):
        return [], []
    picks = raw_picks_json["picks"]
    starters = [p for p in picks if p.get("position", 99) <= _STARTING_XI_MAX_POSITION]
    bench = sorted(
        (p for p in picks if p.get("position", 0) > _STARTING_XI_MAX_POSITION),
        key=lambda p: p.get("position", 99),
    )
    return starters, bench


def _resolve_elements_to_player_ids(
    session: Session, season_id: uuid.UUID, element_ids: set[int]
) -> dict[int, uuid.UUID]:
    if not element_ids:
        return {}
    rows = session.execute(
        select(Player.fpl_element_id, Player.id).where(
            Player.season_id == season_id, Player.fpl_element_id.in_(element_ids)
        )
    ).all()
    return {element_id: player_id for element_id, player_id in rows}


@dataclass(frozen=True)
class _ResolvedSquad:
    """One resolved (starting XI, bench, captain) triple from a snapshot's own raw_picks_json."""

    starting_player_ids: list[uuid.UUID]
    bench_player_ids: list[uuid.UUID]
    captain_player_id: uuid.UUID | None
    captain_multiplier: int


def _resolve_squad_from_raw_picks(
    session: Session, season_id: uuid.UUID, raw_picks_json: dict | None
) -> _ResolvedSquad | None:
    """Resolve a snapshot's own picks into internal Player ids. ``None`` if unresolvable (no picks
    data at all -- e.g. a hand-built squad with nothing recorded, mirrors
    ``app.jobs.handlers.h2h_simulation``'s own "cannot simulate without raw_picks_json" precedent).

    Never models autosub -- see module docstring's known-limitations section.
    """
    starters, bench = _split_starting_and_bench(raw_picks_json)
    if not starters:
        return None
    element_ids = {p["element"] for p in (starters + bench) if "element" in p}
    by_element = _resolve_elements_to_player_ids(session, season_id, element_ids)

    starting_ids = [by_element[p["element"]] for p in starters if p["element"] in by_element]
    bench_ids = [by_element[p["element"]] for p in bench if p["element"] in by_element]

    captain_pick = next((p for p in starters if p.get("is_captain")), None)
    captain_id: uuid.UUID | None = None
    captain_multiplier = _CAPTAIN_MULTIPLIER_DEFAULT
    if captain_pick is not None and captain_pick.get("element") in by_element:
        captain_id = by_element[captain_pick["element"]]
        captain_multiplier = int(captain_pick.get("multiplier", _CAPTAIN_MULTIPLIER_DEFAULT)) or 1

    return _ResolvedSquad(
        starting_player_ids=starting_ids,
        bench_player_ids=bench_ids,
        captain_player_id=captain_id,
        captain_multiplier=captain_multiplier,
    )


def _xi_total_with_captain(
    session: Session,
    starting_player_ids: list[uuid.UUID],
    captain_player_id: uuid.UUID | None,
    gameweek_id: uuid.UUID,
    captain_multiplier: int = _CAPTAIN_MULTIPLIER_DEFAULT,
) -> float:
    """Real total for a starting XI, with the captain's own real points counted an extra
    ``captain_multiplier - 1`` times -- mirrors ``app.chips.scenario._sum_horizon_points_for_xi``'s
    exact "sum once, then add the extra multiplier unit for the captain" technique.
    """
    total = sum_actual_points(session, starting_player_ids, gameweek_id)
    if captain_player_id is not None and captain_player_id in starting_player_ids:
        captain_points = sum_actual_points(session, [captain_player_id], gameweek_id)
        total += (captain_multiplier - 1) * captain_points
    return total


# --- Component resolution: which manager, which "actual" post-gameweek snapshot ------------------


def _resolve_relevant_manager_squad_snapshot(
    session: Session, audit: RecommendationAudit
) -> ManagerSquadSnapshot | None:
    """The manager-scoped snapshot the audit's OWN component runs were generated against, if any.

    Checked in transfer -> H2H -> chip priority order (whichever FK is
    present first) -- ``RecommendationAudit`` has no ``manager_squad_snapshot_id``
    of its own (only ``optimization_runs`` is never manager-scoped at all,
    see that model's own docstring), so this is the only way to learn WHICH
    real manager (if any) this audit concerns.
    """
    candidate_snapshot_ids: list[uuid.UUID | None] = []
    if audit.transfer_optimization_run_id is not None:
        transfer_run = session.get(TransferOptimizationRun, audit.transfer_optimization_run_id)
        candidate_snapshot_ids.append(
            transfer_run.manager_squad_snapshot_id if transfer_run is not None else None
        )
    if audit.h2h_simulation_run_id is not None:
        h2h_run = session.get(H2HSimulationRun, audit.h2h_simulation_run_id)
        candidate_snapshot_ids.append(h2h_run.manager_squad_snapshot_id if h2h_run is not None else None)
    if audit.chip_optimization_run_id is not None:
        chip_run = session.get(ChipOptimizationRun, audit.chip_optimization_run_id)
        candidate_snapshot_ids.append(chip_run.manager_squad_snapshot_id if chip_run is not None else None)

    for snapshot_id in candidate_snapshot_ids:
        if snapshot_id is None:
            continue
        snapshot = session.get(ManagerSquadSnapshot, snapshot_id)
        if snapshot is not None:
            return snapshot
    return None


def _resolve_actual_manager_snapshot(
    session: Session, fpl_manager_id: int | None, target_gameweek_id: uuid.UUID
) -> ManagerSquadSnapshot | None:
    """The real, post-gameweek ``MANAGER_SYNC`` snapshot for this exact gameweek, if it exists.

    ``as_of_gameweek_id`` is set to the picks-fetch's own ``current_event`` at
    sync time (see ``app.jobs.handlers.manager_sync``) -- i.e. this really is
    "what the user's picks were recorded as for this gameweek", the genuine
    "what did the user actually do" resolution (see ``ManagerSquadSnapshot``'s
    own docstring). Most recent by ``created_at`` if more than one sync
    landed on this exact gameweek (no uniqueness is enforced on
    ``(fpl_manager_id, as_of_gameweek_id)``).
    """
    if fpl_manager_id is None:
        return None
    return (
        session.execute(
            select(ManagerSquadSnapshot)
            .where(
                ManagerSquadSnapshot.fpl_manager_id == fpl_manager_id,
                ManagerSquadSnapshot.source == ManagerSquadSnapshotSource.MANAGER_SYNC,
                ManagerSquadSnapshot.as_of_gameweek_id == target_gameweek_id,
            )
            .order_by(ManagerSquadSnapshot.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


# --- RECOMMENDED_VS_ACTUAL_TEAM / RECOMMENDED_VS_ACTUAL_CAPTAIN -----------------------------------


def _build_team_and_captain_comparisons(
    session: Session,
    audit: RecommendationAudit,
    actual_squad: _ResolvedSquad,
    target_gameweek_id: uuid.UUID,
) -> tuple[list[CounterfactualResultData], int, int, int]:
    """Returns (comparisons, actual_xi_points, actual_captain_points, actual_bench_points).

    Always computes the 3 "actual" scalar totals (needed on
    ``RecommendationOutcome`` regardless of whether a recommended XI exists
    to compare against); only appends the 2 comparisons when
    ``audit.optimization_run_id`` also resolves.
    """
    actual_team_total = _xi_total_with_captain(
        session,
        actual_squad.starting_player_ids,
        actual_squad.captain_player_id,
        target_gameweek_id,
        actual_squad.captain_multiplier,
    )
    actual_xi_points = round(actual_team_total)
    actual_captain_raw = (
        sum_actual_points(session, [actual_squad.captain_player_id], target_gameweek_id)
        if actual_squad.captain_player_id is not None
        else 0.0
    )
    actual_captain_points = round(actual_captain_raw * actual_squad.captain_multiplier)
    actual_bench_points = round(sum_actual_points(session, actual_squad.bench_player_ids, target_gameweek_id))

    comparisons: list[CounterfactualResultData] = []
    if audit.optimization_run_id is None:
        return comparisons, actual_xi_points, actual_captain_points, actual_bench_points

    opt_run = session.get(OptimizationRun, audit.optimization_run_id)
    if opt_run is None or not opt_run.starting_xi_player_ids or opt_run.captain_player_id is None:
        return comparisons, actual_xi_points, actual_captain_points, actual_bench_points

    recommended_starting_ids = [uuid.UUID(p) for p in opt_run.starting_xi_player_ids]
    recommended_captain_id = opt_run.captain_player_id

    recommended_team_total = _xi_total_with_captain(
        session, recommended_starting_ids, recommended_captain_id, target_gameweek_id
    )
    comparisons.append(
        compare_recommended_vs_actual_team(
            recommended_player_ids=[str(p) for p in recommended_starting_ids],
            recommended_points=recommended_team_total,
            actual_player_ids=[str(p) for p in actual_squad.starting_player_ids],
            actual_points=actual_team_total,
        )
    )

    recommended_captain_raw = sum_actual_points(session, [recommended_captain_id], target_gameweek_id)
    comparisons.append(
        compare_recommended_vs_actual_captain(
            recommended_captain_player_id=str(recommended_captain_id),
            recommended_captain_raw_points=recommended_captain_raw,
            actual_captain_player_id=(
                str(actual_squad.captain_player_id) if actual_squad.captain_player_id else ""
            ),
            actual_captain_raw_points=actual_captain_raw,
        )
    )
    return comparisons, actual_xi_points, actual_captain_points, actual_bench_points


# --- Transfer scenarios: TRANSFER_VS_NO_TRANSFER / TRANSFER_VS_TOP_ALTERNATIVE / HIT_VS_NO_HIT ----


def _scenario_realized_net_gain(
    session: Session, scenario: TransferOptimizationRunScenario, target_gameweek_id: uuid.UUID
) -> float:
    """REALIZED net gain for ONE scenario, for the single gameweek that has actually finished --
    see module docstring's own "Transfer scenario realized net gain" known-limitation note.
    """
    moves = (
        session.execute(
            select(TransferOptimizationRunMove).where(
                TransferOptimizationRunMove.transfer_optimization_run_scenario_id == scenario.id
            )
        )
        .scalars()
        .all()
    )
    raw_gain = 0.0
    for move in moves:
        raw_gain += sum_actual_points(session, [move.player_in_id], target_gameweek_id) - sum_actual_points(
            session, [move.player_out_id], target_gameweek_id
        )
    return raw_gain - scenario.hit_cost


def _build_transfer_comparisons(
    session: Session, audit: RecommendationAudit, target_gameweek_id: uuid.UUID
) -> list[CounterfactualResultData]:
    if audit.transfer_optimization_run_id is None:
        return []
    scenarios = (
        session.execute(
            select(TransferOptimizationRunScenario).where(
                TransferOptimizationRunScenario.transfer_optimization_run_id
                == audit.transfer_optimization_run_id
            )
        )
        .scalars()
        .all()
    )
    if not scenarios:
        return []

    recommended = next((s for s in scenarios if s.is_recommended), None)
    if recommended is None:
        return []

    realized_by_scenario_id = {
        s.id: _scenario_realized_net_gain(session, s, target_gameweek_id) for s in scenarios
    }

    def label(s: TransferOptimizationRunScenario) -> str:
        return f"{s.transfers_count}-transfer scenario (hit_cost={s.hit_cost})"

    comparisons: list[CounterfactualResultData] = []
    recommended_points = realized_by_scenario_id[recommended.id]

    # TRANSFER_VS_NO_TRANSFER: only meaningful when the recommendation actually WAS a transfer
    # (recommending "roll" compared against itself is a trivial, uninformative zero delta).
    roll_scenario = next((s for s in scenarios if s.transfers_count == 0), None)
    if roll_scenario is not None and roll_scenario.id != recommended.id:
        comparisons.append(
            compare_transfer_scenarios(
                recommended_scenario_points=recommended_points,
                recommended_scenario_label=label(recommended),
                comparison_scenario_points=realized_by_scenario_id[roll_scenario.id],
                comparison_scenario_label=label(roll_scenario),
                comparison_type=CounterfactualComparisonType.TRANSFER_VS_NO_TRANSFER,
            )
        )

    # TRANSFER_VS_TOP_ALTERNATIVE: the best-realized-net-gain OTHER scenario, if any exists.
    other_scenarios = [s for s in scenarios if s.id != recommended.id]
    if other_scenarios:
        top_alternative = max(other_scenarios, key=lambda s: realized_by_scenario_id[s.id])
        comparisons.append(
            compare_transfer_scenarios(
                recommended_scenario_points=recommended_points,
                recommended_scenario_label=label(recommended),
                comparison_scenario_points=realized_by_scenario_id[top_alternative.id],
                comparison_scenario_label=label(top_alternative),
                comparison_type=CounterfactualComparisonType.TRANSFER_VS_TOP_ALTERNATIVE,
            )
        )

    # HIT_VS_NO_HIT: only when the recommended scenario actually took a real hit; comparison is the
    # best-realized zero-hit-cost alternative, if any exists.
    if recommended.hit_cost > 0:
        zero_hit_scenarios = [s for s in scenarios if s.hit_cost == 0]
        if zero_hit_scenarios:
            best_zero_hit = max(zero_hit_scenarios, key=lambda s: realized_by_scenario_id[s.id])
            comparisons.append(
                compare_transfer_scenarios(
                    recommended_scenario_points=recommended_points,
                    recommended_scenario_label=label(recommended),
                    comparison_scenario_points=realized_by_scenario_id[best_zero_hit.id],
                    comparison_scenario_label=label(best_zero_hit),
                    comparison_type=CounterfactualComparisonType.HIT_VS_NO_HIT,
                )
            )

    return comparisons


def _recommendation_net_gain(comparisons: list[CounterfactualResultData]) -> Decimal | None:
    """Convenience summary field on RecommendationOutcome -- see that model's own docstring."""
    match = next(
        (c for c in comparisons if c.comparison_type == CounterfactualComparisonType.TRANSFER_VS_NO_TRANSFER),
        None,
    )
    return Decimal(str(round(match.delta, 2))) if match is not None else None


# --- Chip: CHIP_VS_HOLD ----------------------------------------------------------------------------


def _build_chip_comparison(
    session: Session,
    audit: RecommendationAudit,
    actual_snapshot: ManagerSquadSnapshot | None,
    actual_squad: _ResolvedSquad | None,
    target_gameweek_id: uuid.UUID,
) -> tuple[list[CounterfactualResultData], str | None]:
    """Returns (comparisons, actual_chip_played). ``actual_chip_played`` is reported whenever a real
    actual snapshot exists, regardless of whether CHIP_VS_HOLD itself could be computed.
    """
    actual_chip_played = actual_snapshot.active_chip if actual_snapshot is not None else None
    comparisons: list[CounterfactualResultData] = []

    if audit.chip_optimization_run_id is None or actual_chip_played is None or actual_squad is None:
        return comparisons, actual_chip_played

    try:
        played_chip_type = ChipType(actual_chip_played)
    except ValueError:
        return comparisons, actual_chip_played
    if played_chip_type not in _CHIP_VS_HOLD_SUPPORTED_CHIP_TYPES:
        return comparisons, actual_chip_played

    recommendation = session.execute(
        select(ChipRecommendation).where(
            ChipRecommendation.chip_optimization_run_id == audit.chip_optimization_run_id,
            ChipRecommendation.chip_type == played_chip_type,
        )
    ).scalar_one_or_none()
    if recommendation is None or recommendation.verdict != ChipVerdict.USE_NOW:
        return comparisons, actual_chip_played
    if recommendation.recommended_candidate_id is None:
        return comparisons, actual_chip_played

    candidate = session.get(ChipWindowCandidate, recommendation.recommended_candidate_id)
    if candidate is None or candidate.candidate_fpl_event_id != audit.target_fpl_event_id:
        # Recommended to use it on a DIFFERENT gameweek than this one -- the user played it now
        # anyway, which is a real, interesting divergence, but not what CHIP_VS_HOLD measures.
        return comparisons, actual_chip_played

    if played_chip_type == ChipType.BBOOST:
        chip_gain_realized = sum_actual_points(session, actual_squad.bench_player_ids, target_gameweek_id)
    else:  # TRIPLE_CAPTAIN: the extra +1x multiplier unit over the normal 2x captain doubling.
        if actual_squad.captain_player_id is None:
            return comparisons, actual_chip_played
        chip_gain_realized = sum_actual_points(session, [actual_squad.captain_player_id], target_gameweek_id)

    comparisons.append(compare_chip_vs_hold(chip_gain_realized=chip_gain_realized, hold_baseline=0.0))
    return comparisons, actual_chip_played


# --- H2H: opponent resolution, match result, OVERALL_VS_H2H_ADJUSTED, CONSERVATIVE_VS_AGGRESSIVE --


@dataclass(frozen=True)
class _H2HOutcome:
    h2h_match_id: uuid.UUID | None
    h2h_result: str | None
    opponent_actual_squad_player_ids_json: list | None
    opponent_actual_captain_player_id: int | None
    comparisons: list[CounterfactualResultData]


def _resolve_h2h_match(
    session: Session, target_fpl_event_id: int, user_fpl_manager_id: int, rival_fpl_manager_id: int
) -> H2HMatch | None:
    return session.execute(
        select(H2HMatch).where(
            H2HMatch.event == target_fpl_event_id,
            H2HMatch.entry_1_id.in_((user_fpl_manager_id, rival_fpl_manager_id)),
            H2HMatch.entry_2_id.in_((user_fpl_manager_id, rival_fpl_manager_id)),
        )
    ).scalar_one_or_none()


def _build_h2h_outcome(
    session: Session,
    audit: RecommendationAudit,
    season_id: uuid.UUID,
    user_fpl_manager_id: int | None,
    target_gameweek_id: uuid.UUID,
) -> _H2HOutcome:
    empty = _H2HOutcome(None, None, None, None, [])
    if audit.h2h_simulation_run_id is None:
        return empty

    h2h_run = session.get(H2HSimulationRun, audit.h2h_simulation_run_id)
    if h2h_run is None:
        return empty
    rival_profile = session.get(RivalProfile, h2h_run.rival_profile_id)
    rival_fpl_manager_id = rival_profile.fpl_manager_id if rival_profile is not None else None

    h2h_match_id: uuid.UUID | None = None
    h2h_result: str | None = None
    if user_fpl_manager_id is not None and rival_fpl_manager_id is not None:
        match = _resolve_h2h_match(
            session, audit.target_fpl_event_id, user_fpl_manager_id, rival_fpl_manager_id
        )
        if match is not None and match.entry_1_points is not None and match.entry_2_points is not None:
            user_points = (
                match.entry_1_points if match.entry_1_id == user_fpl_manager_id else match.entry_2_points
            )
            rival_points = (
                match.entry_2_points if match.entry_1_id == user_fpl_manager_id else match.entry_1_points
            )
            h2h_match_id = match.id
            if user_points > rival_points:
                h2h_result = "win"
            elif user_points == rival_points:
                h2h_result = "draw"
            else:
                h2h_result = "loss"

    opponent_squad_ids: list | None = None
    opponent_captain_element_id: int | None = None
    if rival_fpl_manager_id is not None:
        rival_snapshot = (
            session.execute(
                select(RivalGameweekSnapshot)
                .where(
                    RivalGameweekSnapshot.fpl_manager_id == rival_fpl_manager_id,
                    RivalGameweekSnapshot.fpl_event_id == audit.target_fpl_event_id,
                )
                .order_by(RivalGameweekSnapshot.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if rival_snapshot is not None:
            opponent_squad_ids = rival_snapshot.squad_player_ids
            opponent_captain_element_id = rival_snapshot.captain_element_id

    comparisons = _build_h2h_strategy_comparisons(session, h2h_run, season_id, target_gameweek_id)

    return _H2HOutcome(
        h2h_match_id=h2h_match_id,
        h2h_result=h2h_result,
        opponent_actual_squad_player_ids_json=opponent_squad_ids,
        opponent_actual_captain_player_id=opponent_captain_element_id,
        comparisons=comparisons,
    )


def _build_h2h_strategy_comparisons(
    session: Session, h2h_run: H2HSimulationRun, season_id: uuid.UUID, target_gameweek_id: uuid.UUID
) -> list[CounterfactualResultData]:
    manager_snapshot = session.get(ManagerSquadSnapshot, h2h_run.manager_squad_snapshot_id)
    if manager_snapshot is None:
        return []
    base_squad = _resolve_squad_from_raw_picks(session, season_id, manager_snapshot.raw_picks_json)
    if base_squad is None or base_squad.captain_player_id is None:
        return []

    candidates = (
        session.execute(
            select(H2HStrategyCandidate).where(H2HStrategyCandidate.h2h_simulation_run_id == h2h_run.id)
        )
        .scalars()
        .all()
    )
    by_label = {c.strategy_label: c for c in candidates}

    def realized_for(candidate: H2HStrategyCandidate) -> float:
        # A candidate whose captain differs from EP_OPTIMAL's own final captain carries its own
        # captain_player_id; NULL means "same captain as the baseline" (EP_OPTIMAL's own, resolved
        # from the base squad's own picks) -- see H2HStrategyCandidate's own docstring. Squad
        # composition itself is NOT varied here even for NO_HIT/CALCULATED_HIT -- see module
        # docstring's known-limitations section.
        captain_id = candidate.captain_player_id or base_squad.captain_player_id
        return _xi_total_with_captain(session, base_squad.starting_player_ids, captain_id, target_gameweek_id)

    comparisons: list[CounterfactualResultData] = []

    ep_optimal = by_label.get(H2HStrategyLabel.EP_OPTIMAL)
    if ep_optimal is not None and h2h_run.recommended_candidate_id is not None:
        recommended_candidate = session.get(H2HStrategyCandidate, h2h_run.recommended_candidate_id)
        if recommended_candidate is not None:
            comparisons.append(
                compare_overall_vs_h2h_adjusted(
                    ep_optimal_realized=realized_for(ep_optimal),
                    h2h_recommended_realized=realized_for(recommended_candidate),
                )
            )

    # CONSERVATIVE_VS_AGGRESSIVE candidate-pair selection -- see
    # app.learning.counterfactuals.compare_conservative_vs_aggressive's own docstring.
    conservative_label, aggressive_label = None, None
    if H2HStrategyLabel.LOW_VARIANCE in by_label and H2HStrategyLabel.HIGH_UPSIDE in by_label:
        conservative_label, aggressive_label = H2HStrategyLabel.LOW_VARIANCE, H2HStrategyLabel.HIGH_UPSIDE
    elif H2HStrategyLabel.NO_HIT in by_label and H2HStrategyLabel.CALCULATED_HIT in by_label:
        conservative_label, aggressive_label = H2HStrategyLabel.NO_HIT, H2HStrategyLabel.CALCULATED_HIT

    if conservative_label is not None and aggressive_label is not None:
        comparisons.append(
            compare_conservative_vs_aggressive(
                conservative_realized=realized_for(by_label[conservative_label]),
                aggressive_realized=realized_for(by_label[aggressive_label]),
            )
        )

    return comparisons


# --- Actual transfers/hit-cost this gameweek (from the actual snapshot's own raw history) ---------


def _actual_transfers_and_hit_cost(
    actual_snapshot: ManagerSquadSnapshot | None, target_fpl_event_id: int
) -> tuple[list | None, int | None]:
    if actual_snapshot is None:
        return None, None
    history_current = (actual_snapshot.raw_history_json or {}).get("current") or []
    event_row = next((row for row in history_current if row.get("event") == target_fpl_event_id), None)
    hit_cost = event_row.get("event_transfers_cost") if event_row is not None else None
    transfers = [
        t for t in (actual_snapshot.raw_transfers_json or []) if t.get("event") == target_fpl_event_id
    ] or None
    return transfers, hit_cost


# --- Orchestration ----------------------------------------------------------------------------


def _persist_outcome(
    session: Session,
    audit: RecommendationAudit,
    actual_xi_points: int | None,
    actual_captain_points: int | None,
    actual_bench_points: int | None,
    h2h_outcome: _H2HOutcome,
    actual_transfers_made: list | None,
    actual_hit_cost_taken: int | None,
    actual_chip_played: str | None,
    recommendation_net_gain: Decimal | None,
    comparisons: list[CounterfactualResultData],
) -> RecommendationOutcome:
    now = datetime.now(UTC)
    existing = session.execute(
        select(RecommendationOutcome).where(RecommendationOutcome.recommendation_audit_id == audit.id)
    ).scalar_one_or_none()

    if existing is not None:
        # Upsert-in-place: wipe and replace the child comparisons, update the scalar fields --
        # see RecommendationOutcome's own docstring for why a rerun converges rather than refuses
        # or accumulates a duplicate.
        session.execute(
            delete(CounterfactualResult).where(CounterfactualResult.recommendation_outcome_id == existing.id)
        )
        outcome = existing
    else:
        outcome = RecommendationOutcome(recommendation_audit_id=audit.id)
        session.add(outcome)

    outcome.actual_xi_points = actual_xi_points
    outcome.actual_captain_points = actual_captain_points
    outcome.actual_bench_points = actual_bench_points
    outcome.h2h_match_id = h2h_outcome.h2h_match_id
    outcome.h2h_result = h2h_outcome.h2h_result
    outcome.opponent_actual_squad_player_ids_json = h2h_outcome.opponent_actual_squad_player_ids_json
    outcome.opponent_actual_captain_player_id = h2h_outcome.opponent_actual_captain_player_id
    outcome.actual_transfers_made_json = actual_transfers_made
    outcome.actual_hit_cost_taken = actual_hit_cost_taken
    outcome.actual_chip_played = actual_chip_played
    outcome.recommendation_net_gain = recommendation_net_gain
    outcome.finalized_at = now

    session.flush()  # ensures outcome.id is populated for a freshly-created row

    for comparison in comparisons:
        session.add(
            CounterfactualResult(
                recommendation_outcome_id=outcome.id,
                comparison_type=comparison.comparison_type,
                baseline_points=Decimal(str(round(comparison.baseline_points, 2))),
                alternative_points=Decimal(str(round(comparison.alternative_points, 2))),
                delta=Decimal(str(round(comparison.delta, 2))),
                is_value_adding=comparison.is_value_adding,
                baseline_description_json=comparison.baseline_description,
                alternative_description_json=comparison.alternative_description,
            )
        )

    session.commit()
    session.refresh(outcome)
    return outcome


def ingest_outcome_for_audit(session: Session, recommendation_audit_id: uuid.UUID) -> RecommendationOutcome:
    """Compute and persist the real outcome for one already-generated recommendation summary.

    Raises ``RecommendationAuditNotFoundError``/``GameweekNotFinishedError``
    for the two clean refusal cases; never raises for a partially-resolvable
    audit (missing components simply produce fewer counterfactual_results
    rows -- see module docstring).
    """
    audit = session.get(RecommendationAudit, recommendation_audit_id)
    if audit is None:
        raise RecommendationAuditNotFoundError(
            f"recommendation_audits row {recommendation_audit_id} not found."
        )

    gameweek = session.get(Gameweek, audit.target_gameweek_id)
    if gameweek is None or not gameweek.finished:
        raise GameweekNotFinishedError(
            f"Gameweek {audit.target_fpl_event_id} for recommendation_audit {recommendation_audit_id} "
            "is not yet finished; outcomes cannot be computed against incomplete data."
        )

    target_gameweek_id = audit.target_gameweek_id
    season_id = audit.season_id

    relevant_snapshot = _resolve_relevant_manager_squad_snapshot(session, audit)
    fpl_manager_id = relevant_snapshot.fpl_manager_id if relevant_snapshot is not None else None
    actual_snapshot = _resolve_actual_manager_snapshot(session, fpl_manager_id, target_gameweek_id)
    actual_squad = (
        _resolve_squad_from_raw_picks(session, season_id, actual_snapshot.raw_picks_json)
        if actual_snapshot is not None
        else None
    )

    comparisons: list[CounterfactualResultData] = []
    actual_xi_points: int | None = None
    actual_captain_points: int | None = None
    actual_bench_points: int | None = None

    if actual_squad is not None:
        team_comparisons, actual_xi_points, actual_captain_points, actual_bench_points = (
            _build_team_and_captain_comparisons(session, audit, actual_squad, target_gameweek_id)
        )
        comparisons.extend(team_comparisons)

    transfer_comparisons = _build_transfer_comparisons(session, audit, target_gameweek_id)
    comparisons.extend(transfer_comparisons)
    recommendation_net_gain = _recommendation_net_gain(transfer_comparisons)

    chip_comparisons, actual_chip_played = _build_chip_comparison(
        session, audit, actual_snapshot, actual_squad, target_gameweek_id
    )
    comparisons.extend(chip_comparisons)

    h2h_outcome = _build_h2h_outcome(session, audit, season_id, fpl_manager_id, target_gameweek_id)
    comparisons.extend(h2h_outcome.comparisons)

    actual_transfers_made, actual_hit_cost_taken = _actual_transfers_and_hit_cost(
        actual_snapshot, audit.target_fpl_event_id
    )

    return _persist_outcome(
        session,
        audit,
        actual_xi_points,
        actual_captain_points,
        actual_bench_points,
        h2h_outcome,
        actual_transfers_made,
        actual_hit_cost_taken,
        actual_chip_played,
        recommendation_net_gain,
        comparisons,
    )
