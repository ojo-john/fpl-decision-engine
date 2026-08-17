"""Loads whichever ``start_probability_models`` row is currently flagged ``is_active``, for live
optimizer use -- mirrors ``app.optimization.champion_loader.load_current_champion``'s own
graceful-degradation discipline exactly (a missing or unloadable model is a normal, expected
state, never an error that could block a projection).

Every row is inserted with ``is_active=False`` by ``START_PROBABILITY_MODEL_TRAINING`` (see that
job's own module docstring) -- so, as of this being built, this loader will always return ``None``
in practice until a human deliberately flips one row's ``is_active`` to ``True`` after reviewing
that run's own real backtest metrics. That is the intended, safe default: this whole mechanism is
inert (zero effect on any live projection) until that explicit activation decision is made.
"""

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.database.models.start_probability_model import StartProbabilityModel

logger = logging.getLogger(__name__)

__all__ = ["LoadedStartProbabilityModel", "load_active_start_probability_model"]


@dataclass(frozen=True)
class LoadedStartProbabilityModel:
    start_probability_model_id: uuid.UUID
    feature_version: int
    sklearn_model: Any


def load_active_start_probability_model(session: Session) -> LoadedStartProbabilityModel | None:
    """Load the current ``is_active=True`` ``start_probability_models`` row, if any exists.

    Returns ``None`` if no row is active -- the normal, default state (see module docstring), or
    if the flagged row's artifact is missing/corrupted/fails to deserialize (logged, never
    raises) -- exactly mirrors ``load_current_champion``'s own "a broken artifact degrades
    gracefully rather than blocking optimization" contract. If more than one row is somehow
    flagged active at once (this table's ``is_active`` "at most one true" invariant is
    application-enforced, not a DB CHECK constraint -- see that model's own docstring for why),
    the most recently trained one wins, mirroring
    ``app.optimization.champion_loader.load_shadow_challenger``'s own identical tie-break
    precedent for a should-be-unique-but-not-DB-enforced flag.
    """
    version = session.execute(
        select(StartProbabilityModel)
        .where(StartProbabilityModel.is_active.is_(True))
        .order_by(StartProbabilityModel.trained_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if version is None:
        return None

    if version.artifact_uri is None:
        logger.warning(
            "start_probability_models row=%s is flagged active but has no artifact_uri; "
            "no start-probability adjustment will be applied.",
            version.id,
        )
        return None

    try:
        sklearn_model = joblib.load(Path(version.artifact_uri))
    except Exception:  # noqa: BLE001 - loading must never block a live projection
        logger.warning(
            "Active start_probability_models row=%s artifact at %r failed to load; "
            "no start-probability adjustment will be applied.",
            version.id,
            version.artifact_uri,
            exc_info=True,
        )
        return None

    return LoadedStartProbabilityModel(
        start_probability_model_id=version.id,
        feature_version=version.feature_version,
        sklearn_model=sklearn_model,
    )
