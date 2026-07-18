"""Pure counterfactual comparison engine (Milestone 7 Layer 2: Recursive Learning Loop).

No I/O, no DB, no ORM objects -- every input here is an already-resolved plain
value (points, player ids, labels), mirroring ``app.h2h.simulate``/
``app.chips.scenario``'s own "pure computation module" discipline. All DB
querying/resolution (loading component runs, resolving actual squads,
summing real ``player_gameweek_stats`` via ``app.learning.actuals``) is the
caller's job (``app.learning.outcome_ingestion``) -- this module only turns
already-fetched numbers into a ``CounterfactualResultData``.

Shared convention across every function below: ``baseline_points`` is always
the "road not taken" / simpler / default option, ``alternative_points`` is
always the option this system actually recommended or the user actually
followed, and ``delta = alternative_points - baseline_points``. What counts
as "value-adding" (``delta > 0``) is documented per function below, since the
real-world meaning of "the alternative beat the baseline" differs by
comparison type (see each function's own docstring) -- this is deliberately
NOT a single blind ``delta > 0`` rule applied without thought, even though
every function below happens to land on that exact same arithmetic condition
once ``baseline``/``alternative`` are assigned with the correct meaning for
that comparison.
"""

from dataclasses import dataclass

from packages.domain.learning import CounterfactualComparisonType

__all__ = [
    "CounterfactualResultData",
    "compare_recommended_vs_actual_team",
    "compare_recommended_vs_actual_captain",
    "compare_transfer_scenarios",
    "compare_chip_vs_hold",
    "compare_overall_vs_h2h_adjusted",
    "compare_conservative_vs_aggressive",
]


@dataclass(frozen=True)
class CounterfactualResultData:
    """One computed baseline-vs-alternative comparison, ready for ``CounterfactualResult`` persistence."""

    comparison_type: CounterfactualComparisonType
    baseline_points: float
    alternative_points: float
    delta: float
    is_value_adding: bool
    baseline_description: dict
    alternative_description: dict


def _build_result(
    comparison_type: CounterfactualComparisonType,
    baseline_points: float,
    alternative_points: float,
    baseline_description: dict,
    alternative_description: dict,
) -> CounterfactualResultData:
    """Shared arithmetic every comparison below reduces to, once baseline/alternative are assigned
    with the correct real-world meaning for that specific comparison type (see module docstring).
    """
    delta = alternative_points - baseline_points
    return CounterfactualResultData(
        comparison_type=comparison_type,
        baseline_points=baseline_points,
        alternative_points=alternative_points,
        delta=delta,
        is_value_adding=delta > 0,
        baseline_description=baseline_description,
        alternative_description=alternative_description,
    )


def compare_recommended_vs_actual_team(
    recommended_player_ids: list[str],
    recommended_points: float,
    actual_player_ids: list[str],
    actual_points: float,
) -> CounterfactualResultData:
    """RECOMMENDED_VS_ACTUAL_TEAM: did the RECOMMENDED starting XI's real, now-known points beat
    what the user ACTUALLY played?

    ``baseline`` = actual (what really happened -- the "did nothing
    different" reference point); ``alternative`` = recommended (the
    hypothetical "had the user followed this system's XI exactly"
    real outcome, computed from the SAME real ``player_gameweek_stats`` now
    that the gameweek is known). ``is_value_adding=True`` means the
    recommendation scored MORE than what was actually played -- i.e. the user
    should have followed it more closely. Both point totals are expected to
    already include each side's own captain doubling (the caller's job, via
    ``app.learning.actuals.sum_actual_points``).
    """
    return _build_result(
        CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_TEAM,
        baseline_points=actual_points,
        alternative_points=recommended_points,
        baseline_description={"player_ids": list(actual_player_ids)},
        alternative_description={"player_ids": list(recommended_player_ids)},
    )


def compare_recommended_vs_actual_captain(
    recommended_captain_player_id: str,
    recommended_captain_raw_points: float,
    actual_captain_player_id: str,
    actual_captain_raw_points: float,
    captain_multiplier: int = 2,
) -> CounterfactualResultData:
    """RECOMMENDED_VS_ACTUAL_CAPTAIN: same idea as the team comparison, isolated to just the
    captaincy decision.

    Callers pass each side's RAW (un-doubled) single-player actual points --
    this function applies ``captain_multiplier`` (real FPL captain doubling,
    2x) to both sides itself, so the doubling logic lives in exactly one
    place. ``is_value_adding=True`` means the recommended captain would have
    scored more (doubled) than the captain the user actually chose.
    """
    baseline = actual_captain_raw_points * captain_multiplier
    alternative = recommended_captain_raw_points * captain_multiplier
    return _build_result(
        CounterfactualComparisonType.RECOMMENDED_VS_ACTUAL_CAPTAIN,
        baseline_points=baseline,
        alternative_points=alternative,
        baseline_description={"player_id": actual_captain_player_id, "multiplier": captain_multiplier},
        alternative_description={
            "player_id": recommended_captain_player_id,
            "multiplier": captain_multiplier,
        },
    )


def compare_transfer_scenarios(
    recommended_scenario_points: float,
    recommended_scenario_label: str,
    comparison_scenario_points: float,
    comparison_scenario_label: str,
    comparison_type: CounterfactualComparisonType,
) -> CounterfactualResultData:
    """One reusable function for the 3 "compare 2 already-persisted transfer_optimization_run_scenarios'
    REALIZED (not projected) net gain" comparisons: TRANSFER_VS_NO_TRANSFER, TRANSFER_VS_TOP_ALTERNATIVE,
    HIT_VS_NO_HIT -- these are all the exact same arithmetic, differing only in WHICH 2 scenarios the
    caller resolves and hands in, and which ``comparison_type`` it tags the result with.

    ``baseline`` = ``comparison_scenario_points`` (the road not taken: rolling
    the transfer, the best non-recommended alternative, or the best
    zero-hit-cost alternative -- whichever this call represents).
    ``alternative`` = ``recommended_scenario_points`` (the scenario this
    system actually recommended). ``is_value_adding=True`` means the
    recommended scenario's REALIZED net gain (using real, now-known points,
    not Milestone 4's ex-ante projection) was at least as good as the
    comparison scenario -- i.e. the recommendation held up in hindsight.
    """
    return _build_result(
        comparison_type,
        baseline_points=comparison_scenario_points,
        alternative_points=recommended_scenario_points,
        baseline_description={"label": comparison_scenario_label},
        alternative_description={"label": recommended_scenario_label},
    )


def compare_chip_vs_hold(chip_gain_realized: float, hold_baseline: float = 0.0) -> CounterfactualResultData:
    """CHIP_VS_HOLD: did actually playing the chip this gameweek beat holding it?

    ``baseline`` = ``hold_baseline`` (holding the chip changes nothing this
    gameweek, so this is 0.0 by construction for every real caller today --
    exposed as a parameter rather than hardcoded purely so a future caller
    with a genuinely different notion of "the do-nothing baseline" isn't
    blocked). ``alternative`` = ``chip_gain_realized`` (the REAL incremental
    points the chip's own effect actually contributed this gameweek -- e.g.
    the bench's real total for Bench Boost, or the extra multiplier unit's
    real points for Triple Captain; see ``app.learning.outcome_ingestion`` for
    exactly how this is computed and which chips it's scoped to).
    ``is_value_adding=True`` means playing the chip now genuinely paid off
    versus holding it.
    """
    return _build_result(
        CounterfactualComparisonType.CHIP_VS_HOLD,
        baseline_points=hold_baseline,
        alternative_points=chip_gain_realized,
        baseline_description={"label": "hold"},
        alternative_description={"label": "use_now"},
    )


def compare_overall_vs_h2h_adjusted(
    ep_optimal_realized: float, h2h_recommended_realized: float
) -> CounterfactualResultData:
    """OVERALL_VS_H2H_ADJUSTED: did adjusting the recommendation for THIS specific H2H matchup
    (rather than just taking the plain overall-points-optimal captain/XI) actually score more, in
    real hindsight?

    ``baseline`` = ``ep_optimal_realized`` (the plain, opponent-blind optimal
    candidate's real points this gameweek). ``alternative`` =
    ``h2h_recommended_realized`` (whichever candidate
    ``h2h_strategy_candidates.is_recommended`` actually pointed to, using the
    same real points). ``is_value_adding=True`` means the H2H-aware
    adjustment was worth making -- if the audit's own
    ``h2h_adjustment_materially_changed`` was ``False`` (the recommended
    candidate WAS ``EP_OPTIMAL``), these two inputs are identical and
    ``delta`` is exactly 0 (still a valid, informative "no adjustment, no
    difference" result, not skipped).
    """
    return _build_result(
        CounterfactualComparisonType.OVERALL_VS_H2H_ADJUSTED,
        baseline_points=ep_optimal_realized,
        alternative_points=h2h_recommended_realized,
        baseline_description={"label": "ep_optimal"},
        alternative_description={"label": "h2h_recommended"},
    )


def compare_conservative_vs_aggressive(
    conservative_realized: float, aggressive_realized: float
) -> CounterfactualResultData:
    """CONSERVATIVE_VS_AGGRESSIVE: in hindsight, would the more aggressive/upside-seeking strategy
    candidate have scored more than the more conservative/floor-protecting one?

    ``baseline`` = ``conservative_realized`` (``LOW_VARIANCE`` if available,
    else ``NO_HIT`` -- see ``app.learning.outcome_ingestion`` for the exact
    candidate-pair selection rule). ``alternative`` = ``aggressive_realized``
    (``HIGH_UPSIDE``/``CALCULATED_HIT`` respectively). ``is_value_adding=True``
    means the aggressive strategy would have paid off this particular
    gameweek -- this is a single-gameweek hindsight read, NOT a claim that
    aggression is generally the better strategy (a stable "should I lean
    aggressive" signal only emerges once many gameweeks of this comparison
    accumulate, which is Layer 3's concern, not this one's).
    """
    return _build_result(
        CounterfactualComparisonType.CONSERVATIVE_VS_AGGRESSIVE,
        baseline_points=conservative_realized,
        alternative_points=aggressive_realized,
        baseline_description={"label": "conservative"},
        alternative_description={"label": "aggressive"},
    )
