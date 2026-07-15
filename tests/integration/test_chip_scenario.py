"""Integration tests for Wildcard/Free Hit scenario evaluation (app.chips.scenario) against a real DB.

Wildcard/Free Hit are the only two chip evaluators needing a real ``Session``
(a genuine MIP solve over the live candidate pool -- see
``app.chips.scenario``'s own module docstring for why this can't be mocked
as "just take these already-computed numbers" the way Bench Boost/Triple
Captain can) -- so, mirroring how ``tests/unit/test_chip_scenario.py`` covers
every OTHER evaluator with hand-built inputs, these two live here instead.

Reuses ``build_optimization_bootstrap_json``'s ~60-player synthetic pool (see
conftest.py) and ``test_transfer_optimization_job.py``'s own hand-picked
15-element legal squad -- same squad shape (2 GKP/5 DEF/5 MID/3 FWD), just
also split into an explicit 11-player starting XI here (1 GKP/4 DEF/4 MID/2
FWD) since Wildcard/Free Hit need a starting XI + captain, not just a squad.
"""

import uuid

import pytest
from sqlalchemy import select, update

from app.chips.scenario import (
    _WILDCARD_MAX_CANDIDATE_GAMEWEEKS,
    evaluate_free_hit,
    evaluate_free_hit_candidate,
    evaluate_wildcard,
    evaluate_wildcard_candidate,
)
from app.jobs.service import execute_job
from app.optimization.constraints import get_captain_multiplier, load_optimization_constraints
from app.optimization.gameweek_resolution import resolve_gameweeks_within_window
from packages.database.models.gameweek import Gameweek
from packages.database.models.player import Player
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType

# GKP: 30001, 30002 | DEF: 30010, 30011, 30019, 30014, 30016 | MID: 30028, 30029, 30030, 30031, 30032 |
# FWD: 30048, 30049, 30050 -- same 15-element legal squad as test_transfer_optimization_job.py.
_SQUAD_ELEMENT_IDS = [
    30001,
    30002,
    30010,
    30011,
    30019,
    30014,
    30016,
    30028,
    30029,
    30030,
    30031,
    30032,
    30048,
    30049,
    30050,
]
# 1 GKP + 4 DEF + 4 MID + 2 FWD = 11, a legal starting formation under this season's
# DEF 3-5/MID 2-5/FWD 1-3 requirements.
_STARTING_ELEMENT_IDS = [30001, 30010, 30011, 30019, 30014, 30028, 30029, 30030, 30031, 30048, 30049]
_CAPTAIN_ELEMENT_ID = 30048
_BANK = 20
_TRAILING_UNFINISHED_GAMEWEEKS = 6  # deliberately > _WILDCARD_MAX_CANDIDATE_GAMEWEEKS (4)


def _seed_season_with_several_unfinished_gameweeks(
    db_session, patched_fpl_client_optimization_with_active_gameweek
) -> tuple[uuid.UUID, int]:
    """Ingest the ~60-player optimization pool, then mark the trailing
    ``_TRAILING_UNFINISHED_GAMEWEEKS`` gameweeks unfinished (the fixture itself only marks the very
    last one) -- so the Wildcard/Free Hit candidate-gameweek CAP is actually exercised by a real
    multi-gameweek window, not a trivially single-candidate one.

    Returns ``(season_id, stop_event)`` where ``stop_event`` is this season's final ``fpl_event_id``.
    """
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    season_id = db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()

    trailing = db_session.execute(
        select(Gameweek.id, Gameweek.fpl_event_id)
        .where(Gameweek.season_id == season_id)
        .order_by(Gameweek.fpl_event_id.desc())
        .limit(_TRAILING_UNFINISHED_GAMEWEEKS)
    ).all()
    assert len(trailing) == _TRAILING_UNFINISHED_GAMEWEEKS
    for gameweek_id, _ in trailing:
        db_session.execute(update(Gameweek).where(Gameweek.id == gameweek_id).values(finished=False))
    db_session.commit()

    stop_event = max(fpl_event_id for _, fpl_event_id in trailing)
    return season_id, stop_event


def _player_id_by_element(db_session, season_id, element_id: int) -> uuid.UUID:
    return db_session.execute(
        select(Player.id).where(Player.season_id == season_id, Player.fpl_element_id == element_id)
    ).scalar_one()


def _current_squad(db_session, season_id):
    """(starting_player_ids, captain_player_id, selling_prices) for the hand-picked legal squad.

    Selling price = ``now_cost`` for every squad member (no profit assumed) -- same conservative
    convention ``test_transfer_optimization_job.py`` uses for a player with no derivable purchase
    price.
    """
    players_by_element = {
        eid: db_session.execute(
            select(Player).where(Player.season_id == season_id, Player.fpl_element_id == eid)
        ).scalar_one()
        for eid in _SQUAD_ELEMENT_IDS
    }
    starting_player_ids = [players_by_element[eid].id for eid in _STARTING_ELEMENT_IDS]
    captain_player_id = players_by_element[_CAPTAIN_ELEMENT_ID].id
    selling_prices = {p.id: p.now_cost for p in players_by_element.values()}
    return starting_player_ids, captain_player_id, selling_prices


def test_evaluate_wildcard_respects_the_candidate_gameweek_cap(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id, stop_event = _seed_season_with_several_unfinished_gameweeks(
        db_session, patched_fpl_client_optimization_with_active_gameweek
    )
    window_gameweek_ids = resolve_gameweeks_within_window(db_session, season_id, stop_event)
    assert len(window_gameweek_ids) > _WILDCARD_MAX_CANDIDATE_GAMEWEEKS  # the cap is actually exercised

    constraints = load_optimization_constraints(db_session, season_id)
    captain_multiplier = get_captain_multiplier(db_session, constraints.rules_config_id)
    starting_player_ids, captain_player_id, selling_prices = _current_squad(db_session, season_id)

    results = evaluate_wildcard(
        db_session,
        season_id,
        window_gameweek_ids,
        starting_player_ids,
        captain_player_id,
        _BANK,
        selling_prices,
        constraints,
        captain_multiplier,
        champion=None,
    )

    # Never more MIP solves than the named cap, however many unfinished gameweeks exist in the window.
    assert 0 < len(results) <= _WILDCARD_MAX_CANDIDATE_GAMEWEEKS
    # The immediate (soonest) gameweek is always one of the evaluated candidates.
    assert results[0].gameweek_id == window_gameweek_ids[0]


def test_evaluate_free_hit_also_respects_the_candidate_gameweek_cap(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    season_id, stop_event = _seed_season_with_several_unfinished_gameweeks(
        db_session, patched_fpl_client_optimization_with_active_gameweek
    )
    window_gameweek_ids = resolve_gameweeks_within_window(db_session, season_id, stop_event)

    constraints = load_optimization_constraints(db_session, season_id)
    captain_multiplier = get_captain_multiplier(db_session, constraints.rules_config_id)
    starting_player_ids, captain_player_id, selling_prices = _current_squad(db_session, season_id)

    results = evaluate_free_hit(
        db_session,
        season_id,
        window_gameweek_ids,
        starting_player_ids,
        captain_player_id,
        _BANK,
        selling_prices,
        constraints,
        captain_multiplier,
        champion=None,
    )

    assert 0 < len(results) <= _WILDCARD_MAX_CANDIDATE_GAMEWEEKS


def test_evaluate_wildcard_candidate_returns_none_when_budget_is_far_too_small(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """Confirms the budget is genuinely wired into the MIP solve (not silently ignored): a budget of
    0 cannot afford even the cheapest legal 15-player squad, so the rebuild is INFEASIBLE and the
    candidate is excluded (``None``), never a crash or a bogus squad over budget.
    """
    season_id, stop_event = _seed_season_with_several_unfinished_gameweeks(
        db_session, patched_fpl_client_optimization_with_active_gameweek
    )
    window_gameweek_ids = resolve_gameweeks_within_window(db_session, season_id, stop_event)
    target_gameweek_id = window_gameweek_ids[0]
    fpl_event_id = db_session.execute(
        select(Gameweek.fpl_event_id).where(Gameweek.id == target_gameweek_id)
    ).scalar_one()

    constraints = load_optimization_constraints(db_session, season_id)
    captain_multiplier = get_captain_multiplier(db_session, constraints.rules_config_id)
    starting_player_ids, captain_player_id, _selling_prices = _current_squad(db_session, season_id)

    result = evaluate_wildcard_candidate(
        db_session,
        season_id,
        target_gameweek_id,
        fpl_event_id,
        [target_gameweek_id],
        starting_player_ids,
        captain_player_id,
        bank=0,
        selling_prices={},
        constraints=constraints,
        captain_multiplier=captain_multiplier,
        champion=None,
    )

    assert result is None


def test_wildcard_sums_horizon_points_while_free_hit_uses_only_the_single_target_gameweek(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """Wildcard's gain over a horizon of EXACTLY the one target gameweek must equal Free Hit's own
    single-gameweek gain for that same gameweek (Free Hit IS the one-gameweek special case of
    Wildcard's horizon-summed formula) -- but Wildcard's gain over a genuinely LONGER horizon must
    differ, proving the horizon-summing is real, not a no-op.
    """
    season_id, stop_event = _seed_season_with_several_unfinished_gameweeks(
        db_session, patched_fpl_client_optimization_with_active_gameweek
    )
    window_gameweek_ids = resolve_gameweeks_within_window(db_session, season_id, stop_event)
    assert len(window_gameweek_ids) >= 2
    gw1, gw2 = window_gameweek_ids[0], window_gameweek_ids[1]
    fpl_event_1 = db_session.execute(select(Gameweek.fpl_event_id).where(Gameweek.id == gw1)).scalar_one()

    constraints = load_optimization_constraints(db_session, season_id)
    captain_multiplier = get_captain_multiplier(db_session, constraints.rules_config_id)
    starting_player_ids, captain_player_id, selling_prices = _current_squad(db_session, season_id)

    free_hit_result = evaluate_free_hit_candidate(
        db_session,
        season_id,
        gw1,
        fpl_event_1,
        starting_player_ids,
        captain_player_id,
        _BANK,
        selling_prices,
        constraints,
        captain_multiplier,
        champion=None,
    )
    wildcard_single_gw_result = evaluate_wildcard_candidate(
        db_session,
        season_id,
        gw1,
        fpl_event_1,
        [gw1],
        starting_player_ids,
        captain_player_id,
        _BANK,
        selling_prices,
        constraints,
        captain_multiplier,
        champion=None,
    )

    assert free_hit_result is not None
    assert wildcard_single_gw_result is not None
    assert wildcard_single_gw_result.gain_mean == pytest.approx(free_hit_result.gain_mean)

    wildcard_two_gw_result = evaluate_wildcard_candidate(
        db_session,
        season_id,
        gw1,
        fpl_event_1,
        [gw1, gw2],
        starting_player_ids,
        captain_player_id,
        _BANK,
        selling_prices,
        constraints,
        captain_multiplier,
        champion=None,
    )

    assert wildcard_two_gw_result is not None
    # A second gameweek's worth of (rebuilt - current) delta is added on top -- a real, structural
    # difference from the single-gameweek case, not just floating point noise.
    assert wildcard_two_gw_result.gain_mean != pytest.approx(wildcard_single_gw_result.gain_mean)


def test_evaluate_wildcard_and_free_hit_candidates_expose_their_winning_solution(
    db_session, patched_fpl_client_optimization_with_active_gameweek
):
    """Milestone 6 Layer 4 (league-context refinement) needs the actual rebuilt squad/XI behind an
    OPTIMAL Wildcard/Free Hit candidate, not just its gain -- ``ChipWindowCandidateResult.solution``
    (a purely additive field) must carry a real, legal ``JointSquadSolution`` for both evaluators.
    """
    season_id, stop_event = _seed_season_with_several_unfinished_gameweeks(
        db_session, patched_fpl_client_optimization_with_active_gameweek
    )
    window_gameweek_ids = resolve_gameweeks_within_window(db_session, season_id, stop_event)
    gw1 = window_gameweek_ids[0]
    fpl_event_1 = db_session.execute(select(Gameweek.fpl_event_id).where(Gameweek.id == gw1)).scalar_one()

    constraints = load_optimization_constraints(db_session, season_id)
    captain_multiplier = get_captain_multiplier(db_session, constraints.rules_config_id)
    starting_player_ids, captain_player_id, selling_prices = _current_squad(db_session, season_id)

    wildcard_result = evaluate_wildcard_candidate(
        db_session,
        season_id,
        gw1,
        fpl_event_1,
        [gw1],
        starting_player_ids,
        captain_player_id,
        _BANK,
        selling_prices,
        constraints,
        captain_multiplier,
        champion=None,
    )
    free_hit_result = evaluate_free_hit_candidate(
        db_session,
        season_id,
        gw1,
        fpl_event_1,
        starting_player_ids,
        captain_player_id,
        _BANK,
        selling_prices,
        constraints,
        captain_multiplier,
        champion=None,
    )

    for result in (wildcard_result, free_hit_result):
        assert result is not None
        assert result.solution is not None
        assert len(result.solution.starting_player_ids) == 11
        assert result.solution.captain_player_id in result.solution.starting_player_ids
