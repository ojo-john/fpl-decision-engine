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
"""

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.database.models.model_version import ModelVersion
from packages.domain.modeling import ModelFamily

logger = logging.getLogger(__name__)

__all__ = ["LoadedChampion", "load_current_champion"]


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
    deserializes the joblib artifact at ``artifact_uri``; if that file is
    missing, corrupted, or fails to load for any reason, logs a warning and
    returns ``None`` rather than raising, so a broken artifact degrades
    gracefully to the existing fallback instead of blocking optimization.
    """
    version = session.execute(
        select(ModelVersion).where(ModelVersion.is_champion.is_(True))
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

    try:
        sklearn_model = joblib.load(Path(version.artifact_uri))
    except Exception:  # noqa: BLE001 - champion loading must never block the optimizer
        logger.warning(
            "Champion model_version_id=%s (family=%s) artifact at %r failed to load; "
            "optimization will fall back to the non-ML projection formula for this run.",
            version.id,
            version.model_family.value,
            version.artifact_uri,
            exc_info=True,
        )
        return None

    return LoadedChampion(
        model_version_id=version.id,
        model_family=version.model_family,
        feature_version=version.feature_version,
        sklearn_model=sklearn_model,
    )
