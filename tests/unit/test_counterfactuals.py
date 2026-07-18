"""Unit tests for app.learning.counterfactuals (pure, no DB/network).

Every comparison function reduces to the exact same ``_build_result`` arithmetic
(``delta = alternative_points - baseline_points``, ``is_value_adding = delta > 0``)
once ``baseline``/``alternative`` are assigned with that function's own
real-world meaning -- see the module's own docstring. Each function below is
exercised for: the "recommendation/action was better" direction
(``is_value_adding=True``), the "worse" direction (``False``), and a
tie/zero-delta edge case where the module docstring calls one out explicitly
(``OVERALL_VS_H2H_ADJUSTED``'s "no adjustment, no difference" case) or where a
tie is otherwise a meaningful, reachable state.
"""

from app.learning.counterfactuals import (
    compare_chip_vs_hold,
    compare_conservative_vs_aggressive,
    compare_overall_vs_h2h_adjusted,
    compare_recommended_vs_actual_captain,
    compare_recommended_vs_actual_team,
    compare_transfer_scenarios,
)
from packages.domain.learning import CounterfactualComparisonType

# --- compare_recommended_vs_actual_team -----------------------------------------------------------


def test_compare_recommended_vs_actual_team_recommendation_scored_more_is_value_adding():
    result = compare_recommended_vs_actual_team(
        recommended_player_ids=["p1", "p2"],
        recommended_points=75.0,
        actual_player_ids=["p1", "p3"],
        actual_points=60.0,
    )
    assert result.comparison_type == CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_TEAM
    assert result.baseline_points == 60.0
    assert result.alternative_points == 75.0
    assert result.delta == 15.0
    assert result.is_value_adding is True
    assert result.baseline_description == {"player_ids": ["p1", "p3"]}
    assert result.alternative_description == {"player_ids": ["p1", "p2"]}


def test_compare_recommended_vs_actual_team_actual_scored_more_is_not_value_adding():
    result = compare_recommended_vs_actual_team(
        recommended_player_ids=["p1", "p2"],
        recommended_points=50.0,
        actual_player_ids=["p1", "p3"],
        actual_points=65.0,
    )
    assert result.delta == -15.0
    assert result.is_value_adding is False


def test_compare_recommended_vs_actual_team_identical_totals_is_zero_delta_tie():
    result = compare_recommended_vs_actual_team(
        recommended_player_ids=["p1", "p2"],
        recommended_points=60.0,
        actual_player_ids=["p1", "p3"],
        actual_points=60.0,
    )
    assert result.delta == 0.0
    assert result.is_value_adding is False


# --- compare_recommended_vs_actual_captain ---------------------------------------------------------


def test_compare_recommended_vs_actual_captain_recommended_captain_scored_more_is_value_adding():
    result = compare_recommended_vs_actual_captain(
        recommended_captain_player_id="rec-captain",
        recommended_captain_raw_points=10.0,
        actual_captain_player_id="actual-captain",
        actual_captain_raw_points=4.0,
    )
    assert result.comparison_type == CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_CAPTAIN
    assert result.baseline_points == 8.0  # 4.0 * default multiplier 2
    assert result.alternative_points == 20.0  # 10.0 * default multiplier 2
    assert result.delta == 12.0
    assert result.is_value_adding is True
    assert result.baseline_description == {"player_id": "actual-captain", "multiplier": 2}
    assert result.alternative_description == {"player_id": "rec-captain", "multiplier": 2}


def test_compare_recommended_vs_actual_captain_actual_captain_scored_more_is_not_value_adding():
    result = compare_recommended_vs_actual_captain(
        recommended_captain_player_id="rec-captain",
        recommended_captain_raw_points=3.0,
        actual_captain_player_id="actual-captain",
        actual_captain_raw_points=9.0,
    )
    assert result.baseline_points == 18.0
    assert result.alternative_points == 6.0
    assert result.delta == -12.0
    assert result.is_value_adding is False


def test_compare_recommended_vs_actual_captain_same_raw_points_is_zero_delta_tie():
    result = compare_recommended_vs_actual_captain(
        recommended_captain_player_id="rec-captain",
        recommended_captain_raw_points=7.0,
        actual_captain_player_id="actual-captain",
        actual_captain_raw_points=7.0,
    )
    assert result.delta == 0.0
    assert result.is_value_adding is False


def test_compare_recommended_vs_actual_captain_applies_custom_multiplier_to_both_sides():
    # e.g. Triple Captain active for the "actual" side -- caller passes a non-default multiplier
    # and it is applied identically to both raw inputs.
    result = compare_recommended_vs_actual_captain(
        recommended_captain_player_id="rec-captain",
        recommended_captain_raw_points=5.0,
        actual_captain_player_id="actual-captain",
        actual_captain_raw_points=4.0,
        captain_multiplier=3,
    )
    assert result.baseline_points == 12.0
    assert result.alternative_points == 15.0
    assert result.delta == 3.0
    assert result.is_value_adding is True


# --- compare_transfer_scenarios (shared by TRANSFER_VS_NO_TRANSFER / TRANSFER_VS_TOP_ALTERNATIVE / ---
# --- HIT_VS_NO_HIT) ----------------------------------------------------------------------------------


def test_compare_transfer_scenarios_recommended_scenario_realized_more_is_value_adding():
    result = compare_transfer_scenarios(
        recommended_scenario_points=6.0,
        recommended_scenario_label="2-transfer scenario (hit_cost=4)",
        comparison_scenario_points=0.0,
        comparison_scenario_label="0-transfer scenario (hit_cost=0)",
        comparison_type=CounterfactualComparisonType.TRANSFER_VS_NO_TRANSFER,
    )
    assert result.comparison_type == CounterfactualComparisonType.TRANSFER_VS_NO_TRANSFER
    assert result.baseline_points == 0.0
    assert result.alternative_points == 6.0
    assert result.delta == 6.0
    assert result.is_value_adding is True
    assert result.baseline_description == {"label": "0-transfer scenario (hit_cost=0)"}
    assert result.alternative_description == {"label": "2-transfer scenario (hit_cost=4)"}


def test_compare_transfer_scenarios_recommended_scenario_realized_less_is_not_value_adding():
    result = compare_transfer_scenarios(
        recommended_scenario_points=2.0,
        recommended_scenario_label="1-transfer scenario (hit_cost=0)",
        comparison_scenario_points=9.0,
        comparison_scenario_label="best other scenario",
        comparison_type=CounterfactualComparisonType.TRANSFER_VS_TOP_ALTERNATIVE,
    )
    assert result.comparison_type == CounterfactualComparisonType.TRANSFER_VS_TOP_ALTERNATIVE
    assert result.delta == -7.0
    assert result.is_value_adding is False


def test_compare_transfer_scenarios_equal_realized_gain_is_zero_delta_tie():
    result = compare_transfer_scenarios(
        recommended_scenario_points=5.0,
        recommended_scenario_label="recommended",
        comparison_scenario_points=5.0,
        comparison_scenario_label="best zero-hit alternative",
        comparison_type=CounterfactualComparisonType.HIT_VS_NO_HIT,
    )
    assert result.comparison_type == CounterfactualComparisonType.HIT_VS_NO_HIT
    assert result.delta == 0.0
    assert result.is_value_adding is False


def test_compare_transfer_scenarios_hit_vs_no_hit_direction():
    # HIT_VS_NO_HIT: the hit was worth it (recommended's realized net gain, already net of hit cost,
    # beat the best zero-hit-cost alternative's own realized gain).
    result = compare_transfer_scenarios(
        recommended_scenario_points=6.0,
        recommended_scenario_label="2-transfer scenario (hit_cost=4)",
        comparison_scenario_points=5.0,
        comparison_scenario_label="1-transfer scenario (hit_cost=0)",
        comparison_type=CounterfactualComparisonType.HIT_VS_NO_HIT,
    )
    assert result.delta == 1.0
    assert result.is_value_adding is True


# --- compare_chip_vs_hold --------------------------------------------------------------------------


def test_compare_chip_vs_hold_positive_realized_gain_is_value_adding():
    result = compare_chip_vs_hold(chip_gain_realized=12.0)
    assert result.comparison_type == CounterfactualComparisonType.CHIP_VS_HOLD
    assert result.baseline_points == 0.0
    assert result.alternative_points == 12.0
    assert result.delta == 12.0
    assert result.is_value_adding is True
    assert result.baseline_description == {"label": "hold"}
    assert result.alternative_description == {"label": "use_now"}


def test_compare_chip_vs_hold_negative_realized_gain_is_not_value_adding():
    # e.g. Bench Boost played on a gameweek where the bench blanked/was negative.
    result = compare_chip_vs_hold(chip_gain_realized=-3.0)
    assert result.baseline_points == 0.0
    assert result.alternative_points == -3.0
    assert result.delta == -3.0
    assert result.is_value_adding is False


def test_compare_chip_vs_hold_zero_realized_gain_is_zero_delta_tie():
    result = compare_chip_vs_hold(chip_gain_realized=0.0)
    assert result.delta == 0.0
    assert result.is_value_adding is False


def test_compare_chip_vs_hold_custom_hold_baseline_is_respected():
    result = compare_chip_vs_hold(chip_gain_realized=5.0, hold_baseline=2.0)
    assert result.baseline_points == 2.0
    assert result.delta == 3.0
    assert result.is_value_adding is True


# --- compare_overall_vs_h2h_adjusted ----------------------------------------------------------------


def test_compare_overall_vs_h2h_adjusted_h2h_captain_scored_more_is_value_adding():
    result = compare_overall_vs_h2h_adjusted(ep_optimal_realized=60.0, h2h_recommended_realized=68.0)
    assert result.comparison_type == CounterfactualComparisonType.OVERALL_VS_H2H_ADJUSTED
    assert result.baseline_points == 60.0
    assert result.alternative_points == 68.0
    assert result.delta == 8.0
    assert result.is_value_adding is True
    assert result.baseline_description == {"label": "ep_optimal"}
    assert result.alternative_description == {"label": "h2h_recommended"}


def test_compare_overall_vs_h2h_adjusted_ep_optimal_scored_more_is_not_value_adding():
    result = compare_overall_vs_h2h_adjusted(ep_optimal_realized=71.0, h2h_recommended_realized=66.0)
    assert result.delta == -5.0
    assert result.is_value_adding is False


def test_compare_overall_vs_h2h_adjusted_no_adjustment_made_is_zero_delta_not_skipped():
    # The module docstring explicitly calls this out: when h2h_adjustment_materially_changed was
    # False, both inputs are identical and this is still a valid, informative "no difference"
    # result, not skipped.
    result = compare_overall_vs_h2h_adjusted(ep_optimal_realized=71.0, h2h_recommended_realized=71.0)
    assert result.delta == 0.0
    assert result.is_value_adding is False


# --- compare_conservative_vs_aggressive -------------------------------------------------------------


def test_compare_conservative_vs_aggressive_aggressive_scored_more_is_value_adding():
    result = compare_conservative_vs_aggressive(conservative_realized=55.0, aggressive_realized=70.0)
    assert result.comparison_type == CounterfactualComparisonType.CONSERVATIVE_VS_AGGRESSIVE
    assert result.baseline_points == 55.0
    assert result.alternative_points == 70.0
    assert result.delta == 15.0
    assert result.is_value_adding is True
    assert result.baseline_description == {"label": "conservative"}
    assert result.alternative_description == {"label": "aggressive"}


def test_compare_conservative_vs_aggressive_conservative_scored_more_is_not_value_adding():
    result = compare_conservative_vs_aggressive(conservative_realized=71.0, aggressive_realized=66.0)
    assert result.delta == -5.0
    assert result.is_value_adding is False


def test_compare_conservative_vs_aggressive_identical_realized_is_zero_delta_tie():
    # e.g. the aggressive candidate's differing captain choice blanked identically to the
    # conservative one's -- a real, reachable single-gameweek coincidence.
    result = compare_conservative_vs_aggressive(conservative_realized=64.0, aggressive_realized=64.0)
    assert result.delta == 0.0
    assert result.is_value_adding is False
