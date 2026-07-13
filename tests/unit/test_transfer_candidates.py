"""Unit tests for the pure (DB-free) parts of app.transfers.candidates.

build_out_candidates and top_k_candidates_by_position operate entirely on
plain in-memory data -- no session required -- mirroring this codebase's
pure-computation/DB-orchestration split (e.g. app.features.engineering vs
app.features.service). build_in_shortlists itself (the DB-querying half) is
NOT tested here -- it needs a real Player/Position pool and is exercised
indirectly via the optimizer job integration tests in Layer 3.
"""

import uuid

from app.transfers.candidates import (
    IN_CANDIDATE_SHORTLIST_SIZE,
    SquadPlayerInfo,
    TransferCandidate,
    build_out_candidates,
    top_k_candidates_by_position,
)

_TEAM_A = uuid.uuid4()
_TEAM_B = uuid.uuid4()


def _transfer_candidate(position: str, horizon_points: float) -> TransferCandidate:
    return TransferCandidate(
        player_id=uuid.uuid4(),
        position_short_name=position,
        team_id=_TEAM_A,
        now_cost=50,
        horizon_points=horizon_points,
        is_eligible=True,
    )


# --- build_out_candidates ----------------------------------------------------


def test_build_out_candidates_wraps_all_squad_members_with_no_filtering():
    player_ids = [uuid.uuid4() for _ in range(15)]
    players_by_id = {
        pid: SquadPlayerInfo(position_short_name="MID", team_id=_TEAM_A, now_cost=55) for pid in player_ids
    }
    horizon_points_by_id = {pid: float(index) for index, pid in enumerate(player_ids)}

    candidates = build_out_candidates(player_ids, players_by_id, horizon_points_by_id)

    assert len(candidates) == 15
    assert {c.player_id for c in candidates} == set(player_ids)
    assert all(c.is_eligible for c in candidates)
    assert all(c.now_cost == 55 for c in candidates)
    assert all(c.position_short_name == "MID" for c in candidates)
    assert {c.horizon_points for c in candidates} == set(horizon_points_by_id.values())


def test_build_out_candidates_is_eligible_always_true_regardless_of_status():
    """OUT-candidate eligibility is deliberately unconditional -- a squad member (even one
    who is currently injured/suspended) can always legitimately be transferred OUT.
    """
    player_id = uuid.uuid4()
    players_by_id = {player_id: SquadPlayerInfo(position_short_name="FWD", team_id=_TEAM_B, now_cost=70)}
    horizon_points_by_id = {player_id: 0.0}  # e.g. hard-excluded in projection terms

    candidates = build_out_candidates([player_id], players_by_id, horizon_points_by_id)

    assert candidates[0].is_eligible is True


# --- top_k_candidates_by_position --------------------------------------------


def test_top_k_candidates_by_position_sorts_descending_and_truncates():
    def_candidates = [_transfer_candidate("DEF", points) for points in [10.0, 30.0, 20.0, 5.0]]
    mid_candidates = [_transfer_candidate("MID", points) for points in [40.0, 41.0]]

    result = top_k_candidates_by_position(def_candidates + mid_candidates, k=2)

    assert [c.horizon_points for c in result["DEF"]] == [30.0, 20.0]
    assert [c.horizon_points for c in result["MID"]] == [41.0, 40.0]


def test_top_k_candidates_by_position_keeps_all_when_fewer_than_k():
    candidates = [_transfer_candidate("GKP", points) for points in [5.0, 3.0]]

    result = top_k_candidates_by_position(candidates, k=IN_CANDIDATE_SHORTLIST_SIZE)

    assert len(result["GKP"]) == 2
    assert [c.horizon_points for c in result["GKP"]] == [5.0, 3.0]


def test_top_k_candidates_by_position_tie_break_is_reproducible_by_player_id():
    # Two candidates tied on horizon_points -- must always resolve the same way (lower id string first).
    tied_low = TransferCandidate(
        player_id=uuid.UUID(int=1),
        position_short_name="DEF",
        team_id=_TEAM_A,
        now_cost=50,
        horizon_points=10.0,
        is_eligible=True,
    )
    tied_high = TransferCandidate(
        player_id=uuid.UUID(int=2),
        position_short_name="DEF",
        team_id=_TEAM_A,
        now_cost=50,
        horizon_points=10.0,
        is_eligible=True,
    )

    result_a = top_k_candidates_by_position([tied_high, tied_low], k=2)
    result_b = top_k_candidates_by_position([tied_low, tied_high], k=2)

    assert [c.player_id for c in result_a["DEF"]] == [c.player_id for c in result_b["DEF"]]
    assert result_a["DEF"][0].player_id == tied_low.player_id  # "1" sorts before "2" as a string
