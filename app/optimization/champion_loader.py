"""Loads whichever ``model_versions`` row is currently flagged champion, for live optimizer use.

Milestone 3 Layer 4: this is the one place the optimizer reaches into
Layer 3's output. Deliberately cheap and side-effect-free to call once per
optimization job run (see ``app.jobs.handlers.squad_optimization``/
``lineup_optimization``, both of which call ``load_current_champion`` exactly
ONCE before their per-player projection loop, never once per player).

The single most important design property here: champion loading must NEVER
be able to block the optimizer. A missing champion (fresh environment, no
``MODEL_TRAINING`` run has ever succeeded) is a normal, expected state, not
an error — ``None`` is returned, and callers fall back to the existing
``app.optimization.projection.project_player`` formula entirely. A present
champion whose artifact is missing/corrupted/fails to deserialize is treated
exactly the same way (logged, ``None`` returned) rather than raising —
graceful degradation only, never an exception that could take down an
optimization run.

A second, related degradation: if the flagged champion's family is in
``packages.domain.modeling.LIVE_INCAPABLE_MODEL_FAMILIES`` (today, only
``BASELINE_RULES_EP_NEXT`` — see that constant's docstring for exactly why),
it is *structurally* incapable of ever producing a live prediction, not just
occasionally unavailable. Loading it as-is would mean the live optimizer
gets zero benefit from an entire successful MODEL_TRAINING run, 100% of the
time, even though a perfectly usable sibling candidate (e.g. a tree model)
from that same run might score only marginally worse historically. So in
that one case, this module substitutes the best-aggregate-MAE sibling from
the SAME training run whose family isn't live-incapable, and loads that
instead — falling back to ``None`` only if no such sibling exists.

Milestone 7 Layer 5 (Recursive Learning Loop: Challenger Training/Shadow-Mode
Evaluation/Promotion Gates/Rollback) adds ``load_shadow_challenger`` below,
purely additively — ``load_current_champion`` itself is completely
unchanged, still queries ``is_champion IS TRUE`` and nothing else, and every
existing caller/test keeps working verbatim. See ``load_shadow_challenger``'s
own docstring for how it differs from ``load_current_champion`` (no
live-incapable substitution — shadow evaluation is always retroactive, never
live).
"""

import io
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import joblib
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.database.models.model_metric import ModelMetric
from packages.database.models.model_version import ModelVersion
from packages.domain.learning import ModelLifecycleState
from packages.domain.modeling import LIVE_INCAPABLE_MODEL_FAMILIES, ModelFamily

logger = logging.getLogger(__name__)

__all__ = ["LoadedChampion", "load_current_champion", "load_shadow_challenger"]


@dataclass(frozen=True)
class LoadedChampion:
    model_version_id: uuid.UUID
    model_family: ModelFamily
    feature_version: int
    # None for a baseline family (pure lookup/formula, no artifact to load —
    # see app.modeling.baselines and packages.database.models.model_version's
    # own docstring on artifact_uri always being NULL for those 4 families).
    sklearn_model: Any | None


def load_current_champion(session: Session) -> LoadedChampion | None:
    """Load the current champion ``model_versions`` row, if any exists.

    Returns ``None`` if no champion has ever been selected — a normal,
    expected state for a fresh environment, not an error. For a tree family,
    deserializes the joblib artifact from ``artifact_bytes`` (the sole
    authoritative source — see ``ModelVersion.artifact_bytes``'s own
    docstring for why ``artifact_uri``'s local filesystem path is not used
    here); if those bytes are missing, corrupted, or fail to load for any
    reason, logs a warning and returns ``None`` rather than raising, so a
    broken/missing artifact degrades gracefully to the existing fallback
    instead of blocking optimization.
    """
    version = session.execute(
        select(ModelVersion).where(ModelVersion.is_champion.is_(True))
    ).scalar_one_or_none()
    if version is None:
        return None

    if version.model_family in LIVE_INCAPABLE_MODEL_FAMILIES:
        substitute = _best_live_capable_sibling(session, version)
        if substitute is None:
            logger.warning(
                "Champion model_version_id=%s (family=%s) cannot produce a live "
                "prediction (see LIVE_INCAPABLE_MODEL_FAMILIES) and no live-capable "
                "sibling from the same training run (training_job_id=%s) was found; "
                "optimization will fall back to the non-ML projection formula for "
                "this run.",
                version.id,
                version.model_family.value,
                version.training_job_id,
            )
            return None
        logger.info(
            "Champion model_version_id=%s (family=%s) cannot produce a live "
            "prediction; substituting best live-capable sibling from the same "
            "training run: model_version_id=%s (family=%s).",
            version.id,
            version.model_family.value,
            substitute.id,
            substitute.model_family.value,
        )
        version = substitute

    if version.artifact_uri is None:
        # Baseline family: pure lookup/formula, nothing to deserialize.
        return LoadedChampion(
            model_version_id=version.id,
            model_family=version.model_family,
            feature_version=version.feature_version,
            sklearn_model=None,
        )

    if version.artifact_bytes is None:
        logger.warning(
            "Champion model_version_id=%s (family=%s) has no stored artifact_bytes (trained "
            "before that column existed, or the artifact was never persisted); optimization "
            "will fall back to the non-ML projection formula for this run.",
            version.id,
            version.model_family.value,
        )
        return None

    try:
        sklearn_model = joblib.load(io.BytesIO(version.artifact_bytes))
    except Exception:  # noqa: BLE001 - champion loading must never block the optimizer
        logger.warning(
            "Champion model_version_id=%s (family=%s) artifact_bytes failed to load; "
            "optimization will fall back to the non-ML projection formula for this run.",
            version.id,
            version.model_family.value,
            exc_info=True,
        )
        return None

    return LoadedChampion(
        model_version_id=version.id,
        model_family=version.model_family,
        feature_version=version.feature_version,
        sklearn_model=sklearn_model,
    )


def load_shadow_challenger(session: Session) -> LoadedChampion | None:
    """Load the current shadow challenger ``model_versions`` row, if any exists (Milestone 7 Layer 5).

    Mirrors ``load_current_champion``'s own loading mechanics (same
    ``LoadedChampion`` shape, same graceful-degrade-to-``None`` discipline for
    a missing/corrupt artifact) but queries ``lifecycle_state ==
    SHADOW_CHALLENGER`` instead of ``is_champion IS TRUE``. If more than one
    row is somehow flagged ``SHADOW_CHALLENGER`` at once (e.g. ``MODEL_TRAINING``
    ran again before the previous challenger's promotion gate was decided),
    the most recently trained one wins — mirrors
    ``app.jobs.handlers.recommendation_generation._current_champion_model_version_id``'s
    own "most recent by ``trained_at``" precedent for resolving a
    should-be-unique-but-not-DB-enforced flag.

    Deliberately does NOT apply ``LIVE_INCAPABLE_MODEL_FAMILIES`` substitution
    the way ``load_current_champion`` does: that substitution exists because a
    LIVE optimizer run needs a prediction for a player who hasn't played yet,
    and ``BASELINE_RULES_EP_NEXT`` structurally cannot produce one in that
    context. Shadow-challenger evaluation is the opposite situation — it is
    always computed RETROACTIVELY, against an already-finished gameweek whose
    real ``player_gameweek_stats``/``player_features`` rows already exist (see
    ``app.modeling.shadow_evaluation``), so ``BASELINE_RULES_EP_NEXT`` is
    perfectly usable there and must not be silently swapped out.
    """
    version = session.execute(
        select(ModelVersion)
        .where(ModelVersion.lifecycle_state == ModelLifecycleState.SHADOW_CHALLENGER)
        .order_by(ModelVersion.trained_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if version is None:
        return None

    if version.artifact_uri is None:
        # Baseline family: pure lookup/formula, nothing to deserialize.
        return LoadedChampion(
            model_version_id=version.id,
            model_family=version.model_family,
            feature_version=version.feature_version,
            sklearn_model=None,
        )

    if version.artifact_bytes is None:
        logger.warning(
            "Shadow challenger model_version_id=%s (family=%s) has no stored artifact_bytes; "
            "no shadow evaluation will be produced this run.",
            version.id,
            version.model_family.value,
        )
        return None

    try:
        sklearn_model = joblib.load(io.BytesIO(version.artifact_bytes))
    except Exception:  # noqa: BLE001 - shadow-challenger loading must never raise into a caller
        logger.warning(
            "Shadow challenger model_version_id=%s (family=%s) artifact_bytes failed to load; "
            "no shadow evaluation will be produced this run.",
            version.id,
            version.model_family.value,
            exc_info=True,
        )
        return None

    return LoadedChampion(
        model_version_id=version.id,
        model_family=version.model_family,
        feature_version=version.feature_version,
        sklearn_model=sklearn_model,
    )


def _best_live_capable_sibling(session: Session, champion: ModelVersion) -> ModelVersion | None:
    """The best-aggregate-MAE ``ModelVersion`` from ``champion``'s own training run, excluding
    ``LIVE_INCAPABLE_MODEL_FAMILIES`` — used only when the flagged champion itself can't run live.

    Scoped to the SAME ``training_job_id`` (not "the best live-capable
    ModelVersion ever recorded") so this always compares candidates that were
    actually validated together, on the same feature version and seasons —
    never mixing results across different training runs. Returns ``None`` if
    ``training_job_id`` is unset (should not happen for a real training run —
    see ``app.jobs.handlers.model_training``, which always sets it — but this
    is exactly the kind of thing that must degrade quietly rather than raise)
    or if every sibling from that run is also live-incapable.
    """
    if champion.training_job_id is None:
        return None
    rows = session.execute(
        select(ModelVersion, ModelMetric.mae)
        .join(ModelMetric, ModelMetric.model_version_id == ModelVersion.id)
        .where(
            ModelVersion.training_job_id == champion.training_job_id,
            ModelMetric.fold_index.is_(None),
            ModelVersion.model_family.notin_(LIVE_INCAPABLE_MODEL_FAMILIES),
        )
    ).all()
    if not rows:
        return None
    best_version, _ = min(rows, key=lambda row: row[1])
    return best_version
