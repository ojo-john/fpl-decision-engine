"""Note-severity outcome tracking (Milestone 7 extension: Recursive Learning Loop).

Every ``player_availability_risk_notes``/``player_role_notes`` row is a one-off, unrepeatable
judgment call (a specific injury, a specific role change) -- there is no repeated historical
sample to backtest any SINGLE note against, unlike ``app.features.engineering``'s cold-start
half-life constant (which sweeps a real multi-season sample). What IS honestly buildable is a
running record of, for every reviewed gameweek, every note that was actually applied to a
recommended-squad projection: did applying it move the projection CLOSER to or FURTHER FROM the
real outcome than leaving it out would have? No single row here proves anything about a note's
own quality in isolation -- the point is accumulating enough of these across many one-off notes
and many real gameweeks that a future session can notice a PATTERN (e.g. "risk notes in the
0.60-0.75 band have tended to run too conservative, based on N real outcomes") worth acting on.
See ``apps.api.schemas.players.AvailabilityRiskNoteCreate``/``RoleNoteCreate`` for the severity-
selection rubric this feedback loop is meant to eventually inform, and
``packages/database/models/note_effectiveness_evaluation.py`` for the full persisted shape.

Two layers, mirroring ``app.learning.attribution``'s own "pure classification + DB resolution +
persist + convenience entrypoint, all in one module" structure (chosen over
``app.learning.counterfactuals``'/``outcome_ingestion.py``'s split-module precedent because this
module's own resolution step is small and self-contained, unlike outcome_ingestion's much larger
squad/scenario resolution work):

- ``evaluate_note_effectiveness``: pure, no I/O -- given an already-known with-note projection, a
  note's own severity multiplier, and the real outcome, reconstructs the without-note projection
  by DIVIDING OUT the note's own multiplier (the note-application step in
  ``app.optimization.projection`` is purely multiplicative, so this exactly recovers what the
  SAME already-persisted projection would have been without this one note -- not a fresh,
  independently-recomputed projection, which could drift from what was actually decided on if
  underlying data changed since generation time) and compares each side's absolute error against
  the real outcome.
- ``evaluate_note_effectiveness_for_outcome``: the DB-facing convenience entrypoint, mirroring
  ``app.learning.attribution.attribute_failures_for_outcome``'s own role -- resolves one
  ``recommendation_outcomes`` row's own recommended-squad ``optimization_run_players`` rows,
  finds which ones had a note applied (via ``app.optimization.projection.
  AVAILABILITY_RISK_NOTED_SUFFIX``/``ROLE_BOOSTED_SUFFIX`` on ``projection_source`` -- an
  immutable fact about what was actually applied at generation time, not a fresh re-lookup that
  could disagree if the note has since been edited... though notes are never edited in place,
  only created/deleted, see those models' own docstrings), re-resolves each note's own severity
  via ``get_active_availability_risk_note``/``get_active_role_boost_note`` (safe to call again
  since severity is immutable for the lifetime of a note row), computes the pure comparison, and
  persists via ``persist_note_effectiveness_evaluations``.

A player with BOTH an active risk note and an active role note stacked on the same projection at
once produces TWO rows, one per note type -- removing "just the risk note" and removing "just the
role note" are two independently meaningful questions even when both applied to the exact same
number (see ``NoteEffectivenessEvaluation``'s own docstring).

If a note referenced by ``projection_source`` has since been DELETED (the one real way a note's
data can vanish, since edits don't exist), its own severity can no longer be resolved -- that
player/note-type combination is silently skipped (nothing to compute), not an error, since this
is expected in the ordinary lifecycle of a note being cleared out after its situation resolves.
"""

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.learning.actuals import sum_actual_points
from app.optimization.projection import (
    AVAILABILITY_RISK_NOTED_SUFFIX,
    ROLE_BOOSTED_SUFFIX,
    get_active_availability_risk_note,
    get_active_role_boost_note,
)
from packages.database.models.note_effectiveness_evaluation import NoteEffectivenessEvaluation
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.recommendation_outcome import RecommendationOutcome

logger = logging.getLogger(__name__)

__all__ = [
    "NoteEffectivenessResult",
    "NoteEffectivenessData",
    "evaluate_note_effectiveness",
    "persist_note_effectiveness_evaluations",
    "evaluate_note_effectiveness_for_outcome",
]


@dataclass(frozen=True)
class NoteEffectivenessResult:
    """Pure result of comparing one note's own with/without-note projections against the real
    outcome -- see ``evaluate_note_effectiveness``'s own docstring for the derivation.
    """

    projected_points_with_note: float
    projected_points_without_note: float
    actual_points: float
    error_with_note: float
    error_without_note: float
    # None only on an exact tie (both errors identical) -- a genuine "no difference" result,
    # distinct from True/False, not coerced into either.
    moved_closer_to_actual: bool | None


def evaluate_note_effectiveness(
    projected_points_with_note: float, note_severity: float, actual_points: float
) -> NoteEffectivenessResult:
    """Reconstruct the without-note projection by dividing out ``note_severity``, then compare
    both projections' absolute error against ``actual_points``.

    ``projected_points_with_note`` must already be the note-applied number
    ``optimization_run_players.projected_points`` actually persisted (post EVERY multiplier that
    was active at generation time, not just this one note) -- dividing by this one note's own
    ``note_severity`` isolates exactly this note's own multiplicative contribution, holding every
    other factor (including any OTHER stacked note) fixed, which is the correct question when a
    player had more than one note active at once (see module docstring).

    ``moved_closer_to_actual=True`` means applying the note produced a MORE accurate projection
    than leaving it out would have (the note earned its keep); ``False`` means the un-noted
    projection would have been more accurate (the note, in this one real instance, moved the
    projection the wrong way) -- this is a single-gameweek, single-note read, not a claim about
    that note's category in general (see module docstring for why only an accumulated pattern
    across many such rows is informative).
    """
    projected_points_without_note = projected_points_with_note / note_severity
    error_with_note = abs(projected_points_with_note - actual_points)
    error_without_note = abs(projected_points_without_note - actual_points)
    if error_with_note == error_without_note:
        moved_closer_to_actual = None
    else:
        moved_closer_to_actual = error_with_note < error_without_note
    return NoteEffectivenessResult(
        projected_points_with_note=projected_points_with_note,
        projected_points_without_note=projected_points_without_note,
        actual_points=actual_points,
        error_with_note=error_with_note,
        error_without_note=error_without_note,
        moved_closer_to_actual=moved_closer_to_actual,
    )


@dataclass(frozen=True)
class NoteEffectivenessData:
    """Plain data ready for ``NoteEffectivenessEvaluation`` persistence -- pairs one
    ``NoteEffectivenessResult`` with the identifying/provenance fields the pure function itself
    has no reason to know about (which player, which note, which table it came from).
    """

    player_id: uuid.UUID
    note_type: str  # "availability_risk" | "role_boost"
    availability_risk_note_id: uuid.UUID | None
    role_boost_note_id: uuid.UUID | None
    note_severity: Decimal
    result: NoteEffectivenessResult


def persist_note_effectiveness_evaluations(
    session: Session, recommendation_outcome_id: uuid.UUID, evaluations: list[NoteEffectivenessData]
) -> list[NoteEffectivenessEvaluation]:
    """Persist ``evaluations`` for one ``recommendation_outcome_id``, wiping any prior rows first.

    Mirrors ``app.learning.attribution.persist_failure_attributions``'s own delete-then-reinsert
    convention for a rerun of the same parent outcome -- individual rows are never UPDATEd in
    place, only deleted and freshly inserted. An empty ``evaluations`` list still runs the delete
    (harmless no-op the first time; correctly clears stale rows on a rerun where a previously-
    noted player's note has since expired/been deleted) and returns ``[]`` without inserting
    anything -- the common case for most reviewed gameweeks, never an error.
    """
    session.execute(
        delete(NoteEffectivenessEvaluation).where(
            NoteEffectivenessEvaluation.recommendation_outcome_id == recommendation_outcome_id
        )
    )
    rows = [
        NoteEffectivenessEvaluation(
            recommendation_outcome_id=recommendation_outcome_id,
            player_id=evaluation.player_id,
            note_type=evaluation.note_type,
            availability_risk_note_id=evaluation.availability_risk_note_id,
            role_boost_note_id=evaluation.role_boost_note_id,
            note_severity=evaluation.note_severity,
            projected_points_with_note=Decimal(str(evaluation.result.projected_points_with_note)),
            projected_points_without_note=Decimal(str(evaluation.result.projected_points_without_note)),
            actual_points=Decimal(str(evaluation.result.actual_points)),
            error_with_note=Decimal(str(evaluation.result.error_with_note)),
            error_without_note=Decimal(str(evaluation.result.error_without_note)),
            moved_closer_to_actual=evaluation.result.moved_closer_to_actual,
        )
        for evaluation in evaluations
    ]
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)
    return rows


def evaluate_note_effectiveness_for_outcome(
    session: Session, recommendation_outcome_id: uuid.UUID
) -> list[NoteEffectivenessEvaluation]:
    """Convenience entry point: load a ``recommendation_outcome`` and its audit's own recommended
    squad, find every note that was actually applied, evaluate, and persist -- the single callable
    ``GameweekReviewJobRunner`` uses as its own building block, mirroring
    ``app.learning.attribution.attribute_failures_for_outcome``'s own "directly callable today"
    role for failure attribution.

    Returns ``[]`` (after clearing any stale prior rows via
    ``persist_note_effectiveness_evaluations``) whenever the audit has no ``optimization_run_id``
    at all (nothing was recommended to have a note applied to in the first place) or none of the
    recommended players had a note actually applied at generation time -- both are the ordinary,
    expected case for most reviewed gameweeks, not an error condition.
    """
    outcome = session.get(RecommendationOutcome, recommendation_outcome_id)
    if outcome is None:
        raise ValueError(f"recommendation_outcomes row {recommendation_outcome_id} not found.")
    audit = session.get(RecommendationAudit, outcome.recommendation_audit_id)
    if audit is None:
        raise ValueError(
            f"recommendation_audits row {outcome.recommendation_audit_id} not found for "
            f"recommendation_outcome {recommendation_outcome_id}."
        )

    if audit.optimization_run_id is None:
        return persist_note_effectiveness_evaluations(session, recommendation_outcome_id, [])

    run_players = (
        session.execute(
            select(OptimizationRunPlayer).where(
                OptimizationRunPlayer.optimization_run_id == audit.optimization_run_id
            )
        )
        .scalars()
        .all()
    )

    evaluations: list[NoteEffectivenessData] = []
    for run_player in run_players:
        source = run_player.projection_source
        had_risk_note = AVAILABILITY_RISK_NOTED_SUFFIX in source
        had_role_note = ROLE_BOOSTED_SUFFIX in source
        if not had_risk_note and not had_role_note:
            continue

        actual_points = sum_actual_points(session, [run_player.player_id], audit.target_gameweek_id)
        projected_with_note = float(run_player.projected_points)

        if had_risk_note:
            risk_note = get_active_availability_risk_note(
                session, run_player.player_id, audit.target_gameweek_id
            )
            if risk_note is None:
                logger.info(
                    "note_effectiveness.availability_risk_note_no_longer_resolvable",
                    extra={
                        "player_id": str(run_player.player_id),
                        "recommendation_outcome_id": str(recommendation_outcome_id),
                    },
                )
            elif risk_note.severity == 0:
                logger.warning(
                    "note_effectiveness.zero_severity_note_skipped",
                    extra={"player_id": str(run_player.player_id), "note_id": str(risk_note.id)},
                )
            else:
                result = evaluate_note_effectiveness(
                    projected_with_note, float(risk_note.severity), actual_points
                )
                evaluations.append(
                    NoteEffectivenessData(
                        player_id=run_player.player_id,
                        note_type="availability_risk",
                        availability_risk_note_id=risk_note.id,
                        role_boost_note_id=None,
                        note_severity=risk_note.severity,
                        result=result,
                    )
                )

        if had_role_note:
            role_note = get_active_role_boost_note(session, run_player.player_id, audit.target_gameweek_id)
            if role_note is None:
                logger.info(
                    "note_effectiveness.role_boost_note_no_longer_resolvable",
                    extra={
                        "player_id": str(run_player.player_id),
                        "recommendation_outcome_id": str(recommendation_outcome_id),
                    },
                )
            elif role_note.severity == 0:
                logger.warning(
                    "note_effectiveness.zero_severity_note_skipped",
                    extra={"player_id": str(run_player.player_id), "note_id": str(role_note.id)},
                )
            else:
                result = evaluate_note_effectiveness(
                    projected_with_note, float(role_note.severity), actual_points
                )
                evaluations.append(
                    NoteEffectivenessData(
                        player_id=run_player.player_id,
                        note_type="role_boost",
                        availability_risk_note_id=None,
                        role_boost_note_id=role_note.id,
                        note_severity=role_note.severity,
                        result=result,
                    )
                )

    return persist_note_effectiveness_evaluations(session, recommendation_outcome_id, evaluations)
