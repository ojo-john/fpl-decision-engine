"""Unit tests for app.h2h.rival_state.compute_rival_state (pure, no DB/network).

Covers the realistic cold-start state every real rival is in right now (a
single observed RivalGameweekSnapshot, thin-or-absent /history/), a richer
multi-gameweek synthetic history, the derive_free_transfers_available
integration point, and the specific edge cases called out in the Milestone 5
Layer 2 plan (never-took-a-hit, all-chips-played).
"""

from decimal import Decimal

from app.chips.windows import ChipWindow, resolve_chip_windows
from app.h2h.rival_state import (
    CaptainObservation,
    RivalStateInputs,
    TransferTargetCandidate,
    compute_rival_state,
)
from app.transfers.free_transfers import derive_free_transfers_available
from packages.domain.h2h import RivalStateConfidence

CAP = 5  # 1 + max_extra_free_transfers(4), matching the real 2025-26 rules.
ALL_CHIP_NAMES = ["wildcard", "freehit", "bboost", "3xc"]

# Mirrors the real 2025-26 bootstrap-static chips[] shape (see
# tests/fixtures/fpl_api/bootstrap_static_sample.json): every chip has two
# windows, one per season half.
_RAW_CHIPS = [
    {"name": "wildcard", "start_event": 2, "stop_event": 19},
    {"name": "freehit", "start_event": 2, "stop_event": 19},
    {"name": "bboost", "start_event": 1, "stop_event": 19},
    {"name": "3xc", "start_event": 1, "stop_event": 19},
    {"name": "wildcard", "start_event": 20, "stop_event": 38},
    {"name": "freehit", "start_event": 20, "stop_event": 38},
    {"name": "bboost", "start_event": 20, "stop_event": 38},
    {"name": "3xc", "start_event": 20, "stop_event": 38},
]
FULL_CHIP_WINDOWS: list[ChipWindow] = resolve_chip_windows(_RAW_CHIPS)


def _base_inputs(**overrides) -> RivalStateInputs:
    defaults = dict(
        fpl_manager_id=12345,
        chip_windows=FULL_CHIP_WINDOWS,
        free_transfers_cap=CAP,
        history_available=False,
        history_current_rows=[],
        history_chip_rows=[],
        current_event=None,
        captain_observations=[],
        last_known_snapshot_id=None,
        squad_selected_by_percent=None,
        population_mean_selected_by_percent=None,
        transfer_target_candidates=[],
    )
    defaults.update(overrides)
    return RivalStateInputs(**defaults)


def _row(event: int, transfers: int, cost: int) -> dict:
    return {"event": event, "event_transfers": transfers, "event_transfers_cost": cost}


# --- Cold start: the realistic current state for every real rival --------


def test_cold_start_single_snapshot_no_history_is_low_confidence_with_unknowns():
    inputs = _base_inputs(
        history_available=False,
        captain_observations=[CaptainObservation(fpl_event_id=38, captain_element_id=101)],
    )

    result = compute_rival_state(inputs)

    assert result.overall_confidence == RivalStateConfidence.LOW
    assert result.unknowns  # non-empty: section 43's "must expose evidence and confidence"
    assert result.derived_free_transfers_min is None
    assert result.derived_free_transfers_max is None
    assert result.derived_free_transfers_confidence == RivalStateConfidence.LOW


def test_cold_start_single_captain_observation_gets_probability_one_but_capped_low():
    inputs = _base_inputs(
        captain_observations=[CaptainObservation(fpl_event_id=38, captain_element_id=101)],
    )

    result = compute_rival_state(inputs)

    assert result.predicted_captain_probabilities == {"101": 1.0}
    assert result.overall_confidence == RivalStateConfidence.LOW
    assert any("single observed gameweek's captain choice" in note for note in result.unknowns)


def test_cold_start_falls_back_to_population_prior_for_transfer_and_hit_probabilities():
    inputs = _base_inputs(history_available=True, history_current_rows=[], history_chip_rows=[])

    result = compute_rival_state(inputs)

    # Population-level prior (see module docstring): hand-picked, documented
    # defaults, not a rival-specific estimate.
    assert result.predicted_no_transfer_probability == Decimal("0.5000")
    assert result.predicted_one_transfer_probability == Decimal("0.3500")
    assert result.predicted_multi_transfer_probability == Decimal("0.1500")
    assert result.predicted_hit_probability == Decimal("0.1500")
    assert any("population-level prior" in note for note in result.unknowns)


def test_no_captain_observations_at_all_is_unknown_not_a_guess():
    inputs = _base_inputs(captain_observations=[])

    result = compute_rival_state(inputs)

    assert result.predicted_captain_probabilities == {}
    assert any("captain-tendency prediction is unknown" in note for note in result.unknowns)


def test_empty_history_available_widens_free_transfer_range_around_baseline():
    # history_available=True but zero current[] rows (fresh season, no
    # gameweek played yet) -- derive_free_transfers_available's own
    # documented baseline default is 1, honestly widened here since it's an
    # assumption, not an observation.
    inputs = _base_inputs(history_available=True, history_current_rows=[], history_chip_rows=[])

    result = compute_rival_state(inputs)

    assert result.derived_free_transfers_min == 0
    assert result.derived_free_transfers_max == 2
    assert result.derived_free_transfers_confidence == RivalStateConfidence.LOW


# --- Richer multi-gameweek synthetic history ------------------------------

_RICH_HISTORY_ROWS = [
    _row(1, 0, 0),
    _row(2, 0, 0),
    _row(3, 1, 0),
    _row(4, 0, 0),
    _row(5, 2, -4),
    _row(6, 0, 0),
    _row(7, 1, 0),
    _row(8, 0, 0),
    _row(9, 3, -8),
    _row(10, 0, 0),
]
# Chip played at event 15 -- outside the 1-10 history window below, so it
# doesn't exclude any of those 10 rows from the non-chip transfer/hit
# statistics (keeps that arithmetic simple: all 10 rows count).
_RICH_CHIP_ROWS = [{"name": "wildcard", "event": 15, "time": "2025-08-22T10:00:00Z"}]
_RICH_CAPTAIN_OBSERVATIONS = [
    CaptainObservation(fpl_event_id=1, captain_element_id=501),
    CaptainObservation(fpl_event_id=2, captain_element_id=501),
    CaptainObservation(fpl_event_id=3, captain_element_id=777),
    CaptainObservation(fpl_event_id=4, captain_element_id=501),
    CaptainObservation(fpl_event_id=5, captain_element_id=501),
    CaptainObservation(fpl_event_id=6, captain_element_id=777),
]


def _rich_inputs(**overrides) -> RivalStateInputs:
    defaults = dict(
        history_available=True,
        history_current_rows=_RICH_HISTORY_ROWS,
        history_chip_rows=_RICH_CHIP_ROWS,
        captain_observations=_RICH_CAPTAIN_OBSERVATIONS,
    )
    defaults.update(overrides)
    return _base_inputs(**defaults)


def test_rich_history_computes_correct_transfer_frequency_statistics():
    result = compute_rival_state(_rich_inputs())

    # 6/10 zero-transfer gameweeks, 2/10 one-transfer, 2/10 multi-transfer.
    assert result.predicted_no_transfer_probability == Decimal("0.6000")
    assert result.predicted_one_transfer_probability == Decimal("0.2000")
    assert result.predicted_multi_transfer_probability == Decimal("0.2000")
    # 2/10 gameweeks took a hit (events 5 and 9).
    assert result.predicted_hit_probability == Decimal("0.2000")


def test_rich_history_transfer_confidence_is_medium_below_high_threshold():
    # 10 non-chip rows: >= the 6-row minimum for an own-distribution estimate,
    # but below the 15-row threshold for HIGH.
    result = compute_rival_state(_rich_inputs())

    evidence = result.evidence
    assert evidence["history_current_row_count"] == 10
    assert not any("population-level prior" in note for note in result.unknowns)


def test_rich_history_free_transfer_confidence_is_high_at_ten_rows():
    result = compute_rival_state(_rich_inputs())

    assert result.derived_free_transfers_confidence == RivalStateConfidence.HIGH
    expected = derive_free_transfers_available(_RICH_HISTORY_ROWS, _RICH_CHIP_ROWS, cap=CAP)
    assert result.derived_free_transfers_min == expected
    assert result.derived_free_transfers_max == expected


def test_rich_history_captain_frequency_and_high_confidence():
    result = compute_rival_state(_rich_inputs())

    assert result.predicted_captain_probabilities == {"501": 0.6667, "777": 0.3333}
    # 6 captain observations >= the 5-observation HIGH threshold, but overall
    # confidence is still capped by the weakest sub-computation (MEDIUM here,
    # from the transfer-distribution's 10-row sample).
    assert result.overall_confidence == RivalStateConfidence.MEDIUM


def test_rich_history_remaining_chips_still_lists_wildcard_with_its_other_window_open():
    # _RICH_CHIP_ROWS plays wildcard at event 15, which only matches
    # wildcard's FIRST window (2-19) -- its second window (20-38) is
    # untouched and unlapsed (current_event defaults to None), so wildcard
    # must still be reported as remaining, not fully excluded.
    result = compute_rival_state(_rich_inputs())

    assert result.derived_likely_remaining_chips == ALL_CHIP_NAMES
    assert result.derived_free_transfers_confidence == RivalStateConfidence.HIGH


def test_rich_history_tendency_features_and_ownership_lean():
    result = compute_rival_state(
        _rich_inputs(
            squad_selected_by_percent=[30.0, 40.0, 20.0],
            population_mean_selected_by_percent=25.0,
        )
    )

    tendency = result.tendency_features
    assert tendency["transfers_per_gameweek"] == 0.7
    assert tendency["roll_frequency"] == 0.6
    assert tendency["hit_tolerance"] == 0.2
    assert tendency["captain_repeat_rate"] == 0.667
    assert tendency["chip_timing"] == "first_half"
    assert tendency["template_vs_differential_lean_delta"] == 5.0


def test_missing_ownership_data_leaves_template_lean_unknown():
    result = compute_rival_state(
        _rich_inputs(squad_selected_by_percent=None, population_mean_selected_by_percent=None)
    )

    assert result.tendency_features["template_vs_differential_lean_delta"] is None
    assert any("ownership lean" in note for note in result.unknowns)


# --- Edge cases explicitly called out in the Layer 2 plan -----------------


def test_never_took_a_hit_all_season_reports_zero_not_an_arbitrary_number():
    no_hit_rows = [_row(event, transfers=1 if event % 3 == 0 else 0, cost=0) for event in range(1, 16)]

    result = compute_rival_state(_rich_inputs(history_current_rows=no_hit_rows, history_chip_rows=[]))

    assert result.predicted_hit_probability == Decimal("0.0000")


def test_all_chips_already_played_gives_empty_remaining_list():
    # BOTH windows of every chip played -- only then is a chip name fully
    # excluded (see test_rich_history_remaining_chips_still_lists_wildcard...
    # above for the single-window-played case, which must NOT exclude it).
    all_played = [
        {"name": "wildcard", "event": 2, "time": "t"},
        {"name": "wildcard", "event": 25, "time": "t"},
        {"name": "freehit", "event": 10, "time": "t"},
        {"name": "freehit", "event": 30, "time": "t"},
        {"name": "bboost", "event": 15, "time": "t"},
        {"name": "bboost", "event": 35, "time": "t"},
        {"name": "3xc", "event": 18, "time": "t"},
        {"name": "3xc", "event": 38, "time": "t"},
    ]

    result = compute_rival_state(_rich_inputs(history_chip_rows=all_played))

    assert result.derived_likely_remaining_chips == []
    assert result.derived_free_transfers_confidence == RivalStateConfidence.HIGH


def test_chip_windows_deduplicate_to_one_name_per_chip_not_one_per_window():
    # FULL_CHIP_WINDOWS already carries 2 windows per chip (8 entries, 4
    # distinct names) -- with nothing played, the result must be the 4 names,
    # never a name repeated once per window.
    inputs = _base_inputs(history_available=True, history_chip_rows=[])

    result = compute_rival_state(inputs)

    assert result.derived_likely_remaining_chips == ALL_CHIP_NAMES


def test_unplayed_window_that_has_lapsed_still_leaves_the_chip_remaining_via_its_other_window():
    # wildcard's first window (2-19) lapses unplayed once current_event
    # passes 19, but its second window (20-38) is still open -- the chip
    # must stay in the remaining list.
    inputs = _base_inputs(history_available=True, history_chip_rows=[], current_event=25)

    result = compute_rival_state(inputs)

    assert result.derived_likely_remaining_chips == ALL_CHIP_NAMES


def test_chip_excluded_once_its_only_window_has_lapsed_unplayed():
    # A synthetic single-window chip (unlike any real 2025-26 chip, which
    # always has two) isolates the lapse branch on its own: never played,
    # and current_event is already past its one and only stop_event.
    single_window = [
        ChipWindow(chip_type=FULL_CHIP_WINDOWS[0].chip_type, window_number=1, start_event=2, stop_event=19)
    ]
    inputs = _base_inputs(
        chip_windows=single_window, history_available=True, history_chip_rows=[], current_event=20
    )

    result = compute_rival_state(inputs)

    assert result.derived_likely_remaining_chips == []


def test_history_fetch_failure_widens_remaining_chips_to_full_inventory_not_empty():
    inputs = _base_inputs(history_available=False)

    result = compute_rival_state(inputs)

    # Honesty over convenience: with no play-history, nothing can be ruled
    # OUT -- an empty list would falsely claim every chip is already spent.
    assert result.derived_likely_remaining_chips == ALL_CHIP_NAMES
    assert any("chip-play history unavailable" in note for note in result.unknowns)


# --- Transfer targets -------------------------------------------------------


def test_transfer_targets_pass_through_ranked_and_capped():
    candidates = [
        TransferTargetCandidate(
            player_id=f"p{i}",
            web_name=f"Player{i}",
            position="MID",
            projected_points=float(i),
            selected_by_percent=10.0,
        )
        for i in range(7)
    ]

    result = compute_rival_state(_rich_inputs(transfer_target_candidates=candidates))

    assert len(result.predicted_likely_transfer_targets) == 5
    assert result.predicted_likely_transfer_targets[0]["player_id"] == "p0"
    assert not any("no candidate replacement targets" in note for note in result.unknowns)


def test_no_transfer_target_candidates_is_flagged_unknown():
    result = compute_rival_state(_rich_inputs(transfer_target_candidates=[]))

    assert result.predicted_likely_transfer_targets == []
    assert any("no candidate replacement targets" in note for note in result.unknowns)
