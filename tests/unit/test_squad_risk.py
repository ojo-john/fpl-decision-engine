"""Unit tests for app.optimization.squad_risk: composition-risk signals over a solved squad."""

import uuid

from app.optimization.candidates import OptimizationCandidate
from app.optimization.constraints import OptimizationConstraints, PositionRequirement
from app.optimization.squad_risk import compute_bench_depth_warnings, compute_budget_concentration


def _candidate(
    points: float, cost: int, position: str = "MID", team_id: uuid.UUID | None = None
) -> OptimizationCandidate:
    return OptimizationCandidate(
        player_id=uuid.uuid4(),
        projected_points=points,
        projection_source="test",
        position_short_name=position,
        team_id=team_id or uuid.uuid4(),
        now_cost=cost,
    )


def _constraints(squad_size: int = 15, total_spend_limit: int = 1000) -> OptimizationConstraints:
    return OptimizationConstraints(
        rules_config_id=uuid.uuid4(),
        squad_size=squad_size,
        team_limit=3,
        total_spend_limit=total_spend_limit,
        currency_multiplier=10,
        position_requirements={
            "GKP": PositionRequirement(uuid.uuid4(), "GKP", 2, 1, 1),
        },
    )


class TestComputeBudgetConcentration:
    def test_compute_budget_concentration_empty_squad_returns_none(self):
        result = compute_budget_concentration([], _constraints())
        assert result is None

    def test_compute_budget_concentration_flat_squad_not_notably_concentrated(self):
        # 15 players at an even 1000/15 ~ 66.67 each -- every share is exactly the
        # "even" share, well below the 1.5x threshold.
        members = [_candidate(points=5.0, cost=1000 // 15) for _ in range(15)]
        result = compute_budget_concentration(members, _constraints())
        assert result is not None
        assert result.is_notably_concentrated is False

    def test_compute_budget_concentration_single_premium_player_flagged(self):
        # squad_size=15, total_spend_limit=1000 -> even share = 6.667%.
        # One player at 155/1000 = 15.5% is well over 1.5x that (10.0%).
        members = [_candidate(points=30.0, cost=155)] + [_candidate(points=5.0, cost=60) for _ in range(14)]
        result = compute_budget_concentration(members, _constraints())
        assert result is not None
        assert result.most_expensive_player_cost == 155
        assert result.budget_share_pct == 15.5
        assert result.is_notably_concentrated is True

    def test_compute_budget_concentration_picks_the_single_most_expensive_when_tied_costs_exist(self):
        a = _candidate(points=10.0, cost=100)
        b = _candidate(points=20.0, cost=100)
        result = compute_budget_concentration([a, b], _constraints(squad_size=2, total_spend_limit=200))
        assert result is not None
        assert result.most_expensive_player_id in {a.player_id, b.player_id}
        assert result.budget_share_pct == 50.0


class TestComputeBenchDepthWarnings:
    def test_compute_bench_depth_warnings_balanced_squad_has_no_warnings(self):
        # DEF has 2 starters -- never evaluated regardless of bench strength (see module
        # docstring: only single-starter positions are checked).
        starters = [_candidate(20.0, 60, "DEF"), _candidate(18.0, 55, "DEF")]
        bench = [_candidate(0.5, 40, "DEF")]
        warnings = compute_bench_depth_warnings(starters, bench, {})
        assert warnings == []

    def test_compute_bench_depth_warnings_single_starter_weak_bench_fires(self):
        starter = _candidate(20.0, 80, "FWD")
        weak_bench = _candidate(3.0, 45, "FWD")  # 3/20 = 0.15 < 0.25 threshold
        names = {starter.player_id: "Watkins", weak_bench.player_id: "Destan"}
        warnings = compute_bench_depth_warnings([starter], [weak_bench], names)
        assert len(warnings) == 1
        assert "FWD" in warnings[0]
        assert "Watkins" in warnings[0]
        assert "Destan" in warnings[0]

    def test_compute_bench_depth_warnings_single_starter_strong_bench_does_not_fire(self):
        starter = _candidate(20.0, 80, "FWD")
        strong_bench = _candidate(15.0, 70, "FWD")  # 15/20 = 0.75 >= 0.25 threshold
        warnings = compute_bench_depth_warnings([starter], [strong_bench], {})
        assert warnings == []

    def test_compute_bench_depth_warnings_single_starter_no_bench_at_all_fires_distinct_message(self):
        starter = _candidate(20.0, 80, "GKP")
        names = {starter.player_id: "Raya"}
        warnings = compute_bench_depth_warnings([starter], [], names)
        assert len(warnings) == 1
        assert "GKP" in warnings[0]
        assert "no bench cover at all" in warnings[0]

    def test_compute_bench_depth_warnings_ignores_other_positions_bench(self):
        fwd_starter = _candidate(20.0, 80, "FWD")
        def_bench = _candidate(0.1, 40, "DEF")  # irrelevant position, must not affect FWD's check
        warnings = compute_bench_depth_warnings([fwd_starter], [def_bench], {})
        # FWD has no bench at all (the only bench player is DEF) -- still fires.
        assert len(warnings) == 1
        assert "FWD" in warnings[0]

    def test_compute_bench_depth_warnings_zero_projected_starter_is_skipped_not_divided_by_zero(self):
        starter = _candidate(0.0, 80, "FWD")
        bench = _candidate(0.0, 40, "FWD")
        warnings = compute_bench_depth_warnings([starter], [bench], {})
        assert warnings == []

    def test_compute_bench_depth_warnings_empty_squad_returns_empty_list(self):
        assert compute_bench_depth_warnings([], [], {}) == []
