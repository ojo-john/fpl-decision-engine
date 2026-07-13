"""Unit tests for app.modeling.walk_forward's fold-construction logic.

Only ``build_expanding_window_folds`` is covered here — it takes a plain
list of season labels and needs no database. ``usable_season_labels_with_features``
(the DB-querying half of this module) is covered instead by the integration
test (``tests/integration/test_model_training_job.py``).
"""

from app.modeling.walk_forward import build_expanding_window_folds


def test_zero_seasons_returns_no_folds():
    assert build_expanding_window_folds([]) == []


def test_single_season_gracefully_degrades_to_one_in_sample_fold():
    folds = build_expanding_window_folds(["2023-24"])

    assert len(folds) == 1
    fold = folds[0]
    assert fold.fold_index == 0
    assert fold.train_season_labels == ("2023-24",)
    assert fold.validation_season_label == "2023-24"
    assert fold.is_degenerate_single_season is True


def test_multiple_seasons_builds_one_expanding_window_fold_per_later_season():
    folds = build_expanding_window_folds(["2020-21", "2021-22", "2022-23", "2023-24"])

    assert len(folds) == 3

    assert folds[0].fold_index == 0
    assert folds[0].train_season_labels == ("2020-21",)
    assert folds[0].validation_season_label == "2021-22"
    assert folds[0].is_degenerate_single_season is False

    assert folds[1].fold_index == 1
    assert folds[1].train_season_labels == ("2020-21", "2021-22")
    assert folds[1].validation_season_label == "2022-23"

    assert folds[2].fold_index == 2
    assert folds[2].train_season_labels == ("2020-21", "2021-22", "2022-23")
    assert folds[2].validation_season_label == "2023-24"


def test_two_seasons_builds_exactly_one_real_fold():
    folds = build_expanding_window_folds(["2022-23", "2023-24"])

    assert len(folds) == 1
    assert folds[0].train_season_labels == ("2022-23",)
    assert folds[0].validation_season_label == "2023-24"
    assert folds[0].is_degenerate_single_season is False


def test_never_shuffles_respects_given_order():
    # A caller passing labels out of chronological order gets folds built
    # from that same (wrong) order -- this function trusts its input's
    # ordering rather than re-sorting, matching the "never shuffles"
    # guarantee described in its docstring.
    folds = build_expanding_window_folds(["2023-24", "2020-21"])

    assert folds[0].train_season_labels == ("2023-24",)
    assert folds[0].validation_season_label == "2020-21"
