"""Milestone 7 Layer 5: retroactive shadow-challenger scoring against a real, finished gameweek.

``compute_shadow_challenger_evaluation`` is the one entry point here, wired
additively into the existing ``GAMEWEEK_REVIEW`` job
(``app.jobs.handlers.gameweek_review``) — see that handler's own module
docstring for exactly where in its orchestration this gets called.

Design decision: RETROACTIVE, not prospective. The plan that originally
scoped Milestone 7 suggested "piggyback on the per-player projection loop" as
the shadow-scoring seam, modeled on how a live A/B test would work. That loop
turns out to live inside ``app.jobs.handlers.squad_optimization``/
``lineup_optimization`` (``RECOMMENDATION_GENERATION`` itself is a pure
aggregator — see that handler's own module docstring — it runs no projection
of its own), and wiring shadow scoring into it would mean: (a) touching two
more job handlers whose own correctness this layer has no reason to risk, and
(b) inventing a brand-new per-player-per-gameweek prospective logging table
just to remember what the challenger *would have* predicted, so it could be
compared against actuals once they're known days later.

That table turns out to be unnecessary. By the time a gameweek is reviewable
(``GAMEWEEK_REVIEW`` already refuses to run until ``gameweeks.finished`` is
``True``), Milestone 3 Layer 2's ``player_features`` rows for that gameweek
already carry the real ``target_total_points`` label for every player who was
playable. So both the current champion's and the current shadow challenger's
own artifacts can simply be re-run against those same, already-persisted
feature rows — exactly the same mechanism ``app/jobs/handlers/model_training
.py``'s own fold-scoring already uses — to get each model's prediction for
that gameweek, compared against the same real actual. No new prospective
logging path, no risk of ever letting a shadow prediction leak into a live
recommendation (it is never even computed until the gameweek is over), and
one paired MAE comparison per (challenger, gameweek) exactly matches what
``model_challenger_evaluations`` is shaped to store.

The one subtlety: the champion and challenger may have been trained under
different ``feature_version``s (e.g. if a ``FEATURE_GENERATION`` bump
happened between their two ``MODEL_TRAINING`` runs). Each model's own
prediction is therefore resolved against ITS OWN ``feature_version``'s
``player_features`` rows independently, then the two per-player prediction
sets are intersected by ``player_id`` — ``challenger_mae``/``champion_mae``
are always computed over that same intersected set (never each model's own,
possibly larger, individual set), so the two numbers stay directly
comparable and ``n_observations`` unambiguously describes both.
"""

import math
import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modeling import baselines
from app.modeling import candidates as candidate_models
from app.optimization.champion_loader import LoadedChampion, load_current_champion, load_shadow_challenger
from packages.database.models.model_challenger_evaluation import ModelChallengerEvaluation
from packages.database.models.player_features import PlayerFeatures
from packages.domain.modeling import BASELINE_MODEL_FAMILIES, ModelFamily

__all__ = ["compute_shadow_challenger_evaluation"]

# Deliberately duplicated (not imported) from app.optimization.projection's own private
# _BASELINE_FEATURE_ROW_PREDICT_FNS -- that name is module-private to its own module, same
# duplication precedent already used elsewhere in this codebase (e.g.
# app.jobs.handlers.chip_simulation's _split_starting_and_bench, per that module's own docstring).
_BASELINE_FEATURE_ROW_PREDICT_FNS = {
    ModelFamily.BASELINE_SEASON_AVERAGE: baselines.predict_season_average,
    ModelFamily.BASELINE_RECENT_AVERAGE: baselines.predict_recent_average,
    ModelFamily.BASELINE_WEIGHTED_RECENT_AVERAGE: baselines.predict_weighted_recent_average,
}


def _predict_batch(
    session: Session, loaded: LoadedChampion, rows: Sequence[PlayerFeatures]
) -> list[float | None]:
    """Predict ``loaded``'s output for every row in ``rows``, in the same order -- ``None`` for a row
    the model can't produce a usable prediction for (never fabricated, never dropped silently).
    """
    if not rows:
        return []

    if loaded.model_family in BASELINE_MODEL_FAMILIES:
        if loaded.model_family == ModelFamily.BASELINE_RULES_EP_NEXT:
            return [
                baselines.predict_rules_based_ep_next(session, row.player_id, row.gameweek_id) for row in rows
            ]
        predict_fn = _BASELINE_FEATURE_ROW_PREDICT_FNS[loaded.model_family]
        return [predict_fn(row) for row in rows]

    assert loaded.sklearn_model is not None  # invariant: only baselines ever have sklearn_model=None
    x, _ = candidate_models.build_feature_matrix(rows)
    if x.empty:
        return [None] * len(rows)
    x_aligned = candidate_models.align_columns(x, list(loaded.sklearn_model.feature_names_in_))
    predictions = loaded.sklearn_model.predict(x_aligned)
    return [float(value) for value in predictions]


def _predictions_by_player(
    session: Session, loaded: LoadedChampion, season_id: uuid.UUID, gameweek_id: uuid.UUID
) -> dict[uuid.UUID, tuple[float, float]]:
    """``player_id`` -> ``(predicted, actual)`` for every player ``loaded`` can produce a usable
    prediction for, against ``loaded``'s OWN ``feature_version``'s ``player_features`` rows for this
    gameweek. Only rows with a known label (``target_total_points IS NOT NULL`` -- guaranteed once the
    gameweek is finished, but defensively filtered here too) are considered.
    """
    rows = (
        session.execute(
            select(PlayerFeatures).where(
                PlayerFeatures.season_id == season_id,
                PlayerFeatures.gameweek_id == gameweek_id,
                PlayerFeatures.feature_version == loaded.feature_version,
                PlayerFeatures.target_total_points.isnot(None),
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {}

    predictions = _predict_batch(session, loaded, rows)
    result: dict[uuid.UUID, tuple[float, float]] = {}
    for row, prediction in zip(rows, predictions, strict=True):
        if prediction is None or math.isnan(prediction):
            continue
        assert row.target_total_points is not None  # guaranteed by the query filter above
        result[row.player_id] = (float(prediction), float(row.target_total_points))
    return result


def compute_shadow_challenger_evaluation(
    session: Session, season_id: uuid.UUID, gameweek_id: uuid.UUID
) -> ModelChallengerEvaluation | None:
    """Score the current shadow challenger (if any) against the current champion (if any) for one
    already-finished gameweek, persisting one ``model_challenger_evaluations`` row.

    Returns ``None`` (persisting nothing) whenever there is nothing meaningful
    to compare: no shadow challenger currently exists, no champion currently
    exists, or the two models' prediction sets for this gameweek don't
    overlap on a single player -- never fabricating a comparison from zero
    genuinely-shared observations.

    UPSERTS in place on the ``(challenger_model_version_id, gameweek_id)``
    pair -- a ``GAMEWEEK_REVIEW`` rerun for the same gameweek (e.g. after a
    late bonus-points correction) converges this row to the corrected number
    rather than violating the table's own uniqueness constraint or leaving a
    stale duplicate behind. See ``ModelChallengerEvaluation``'s own docstring.
    """
    challenger = load_shadow_challenger(session)
    if challenger is None:
        return None
    champion = load_current_champion(session)
    if champion is None:
        return None

    challenger_predictions = _predictions_by_player(session, challenger, season_id, gameweek_id)
    champion_predictions = _predictions_by_player(session, champion, season_id, gameweek_id)
    common_player_ids = set(challenger_predictions) & set(champion_predictions)
    if not common_player_ids:
        return None

    challenger_mae = sum(
        abs(challenger_predictions[pid][0] - challenger_predictions[pid][1]) for pid in common_player_ids
    ) / len(common_player_ids)
    champion_mae = sum(
        abs(champion_predictions[pid][0] - champion_predictions[pid][1]) for pid in common_player_ids
    ) / len(common_player_ids)

    existing = session.execute(
        select(ModelChallengerEvaluation).where(
            ModelChallengerEvaluation.challenger_model_version_id == challenger.model_version_id,
            ModelChallengerEvaluation.gameweek_id == gameweek_id,
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.champion_model_version_id = champion.model_version_id
        existing.season_id = season_id
        existing.challenger_mae = Decimal(str(round(challenger_mae, 4)))
        existing.champion_mae = Decimal(str(round(champion_mae, 4)))
        existing.n_observations = len(common_player_ids)
        row = existing
    else:
        row = ModelChallengerEvaluation(
            challenger_model_version_id=challenger.model_version_id,
            champion_model_version_id=champion.model_version_id,
            season_id=season_id,
            gameweek_id=gameweek_id,
            challenger_mae=Decimal(str(round(challenger_mae, 4))),
            champion_mae=Decimal(str(round(champion_mae, 4))),
            n_observations=len(common_player_ids),
        )
        session.add(row)

    session.commit()
    session.refresh(row)
    return row
