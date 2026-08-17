"""Unit tests for app.learning.note_effectiveness.evaluate_note_effectiveness (pure, no DB/network).

Mirrors tests/unit/test_counterfactuals.py's own approach: exercises the pure arithmetic
directly, covering both directions (the note moved the projection closer to/further from the
real outcome) plus the exact-tie boundary the function's own docstring calls out explicitly
(``moved_closer_to_actual=None``), a role-boost (severity > 1.0) case (not just an
availability-risk discount), and a stacked-note-style call (dividing out just one note's own
multiplier while a hypothetical other multiplier is implicitly still baked into
``projected_points_with_note``, i.e. the derivation makes no assumption there was only ever one
multiplier applied).
"""

from app.learning.note_effectiveness import evaluate_note_effectiveness


def test_evaluate_note_effectiveness_discount_note_moved_projection_closer_to_actual():
    # with_note = 6.5 (10.0 * 0.65), actual = 6.0 -> error_with_note = 0.5.
    # without_note = 10.0, actual = 6.0 -> error_without_note = 4.0. Discounting genuinely helped.
    result = evaluate_note_effectiveness(
        projected_points_with_note=6.5, note_severity=0.65, actual_points=6.0
    )
    assert result.projected_points_without_note == 10.0
    assert result.error_with_note == 0.5
    assert result.error_without_note == 4.0
    assert result.moved_closer_to_actual is True


def test_evaluate_note_effectiveness_discount_note_moved_projection_further_from_actual():
    # with_note = 6.5, actual = 10.0 -> error_with_note = 3.5.
    # without_note = 10.0, actual = 10.0 -> error_without_note = 0.0. Discounting hurt accuracy --
    # the player actually delivered their full, un-discounted projection this gameweek.
    result = evaluate_note_effectiveness(
        projected_points_with_note=6.5, note_severity=0.65, actual_points=10.0
    )
    assert result.error_with_note == 3.5
    assert result.error_without_note == 0.0
    assert result.moved_closer_to_actual is False


def test_evaluate_note_effectiveness_role_boost_note_moved_projection_closer_to_actual():
    # with_note = 11.5 (10.0 * 1.15), actual = 12.0 -> error_with_note = 0.5.
    # without_note = 10.0, actual = 12.0 -> error_without_note = 2.0. The boost helped.
    result = evaluate_note_effectiveness(
        projected_points_with_note=11.5, note_severity=1.15, actual_points=12.0
    )
    assert result.projected_points_without_note == 10.0
    assert result.error_with_note == 0.5
    assert result.error_without_note == 2.0
    assert result.moved_closer_to_actual is True


def test_evaluate_note_effectiveness_exact_tie_is_none_not_coerced_to_true_or_false():
    # with_note = 8.0, without_note = 10.0 (severity 0.8), actual = 9.0 -> both errors are 1.0.
    result = evaluate_note_effectiveness(projected_points_with_note=8.0, note_severity=0.8, actual_points=9.0)
    assert result.error_with_note == 1.0
    assert result.error_without_note == 1.0
    assert result.moved_closer_to_actual is None


def test_evaluate_note_effectiveness_isolates_one_note_when_another_multiplier_already_baked_in():
    # projected_points_with_note here is understood to already reflect BOTH a role-boost note
    # (1.15) AND an availability-risk note (0.65) stacked on a base of 10.0: 10.0 * 1.15 * 0.65 =
    # 7.475. Isolating just the role-boost note's own contribution means dividing out ONLY 1.15,
    # leaving the risk note's own 0.65 still baked into the "without" counterfactual -- this is
    # the correct question ("what if just THIS note hadn't applied, holding everything else
    # fixed"), not "what if no notes had applied at all".
    result = evaluate_note_effectiveness(
        projected_points_with_note=7.475, note_severity=1.15, actual_points=6.5
    )
    assert round(result.projected_points_without_note, 3) == 6.5
    assert round(result.error_with_note, 3) == 0.975
    assert result.error_without_note == 0.0
    assert result.moved_closer_to_actual is False
