"""Milestone 7 Layer 5: promotion gate + rollback -- the machinery, not the trigger.

This module decides whether an already-existing ``SHADOW_CHALLENGER`` should
become the new champion, given the real shadow-vs-champion comparisons
``app.modeling.shadow_evaluation.compute_shadow_challenger_evaluation`` has
already persisted to ``model_challenger_evaluations``. It does NOT decide
*when* to run a training pass, or automatically call this module after every
``GAMEWEEK_REVIEW`` -- that wiring (a retraining trigger reacting to
``ROLLING_PROJECTION_MAE``'s ``threshold_breached``, and a scheduled or
job-driven call into ``evaluate_promotion``/``decide_and_record_promotion``)
is explicitly Milestone 7 Layer 6's concern, not this one's. This module only
builds the promotion machinery Layer 6 will eventually call.

Three gates ARE genuinely implementable from data this codebase actually
persists, and are the only ones ``evaluate_promotion`` checks for real:

- ``minimum_sample_size``: at least ``min_evaluation_window`` distinct
  gameweeks of shadow evaluation exist for this exact (challenger, champion)
  pair. Never force a verdict from insufficient evidence -- fewer than that
  many observations always yields ``RETAINED_INCONCLUSIVE``, regardless of
  how good the available numbers look.
- ``beats_champion_by_margin``: the challenger's MEAN MAE across every
  qualifying evaluation (a simple, unweighted mean across gameweeks --
  mirrors ``app.modeling.champion.aggregate_mae``'s own "each fold/gameweek
  counts equally" philosophy, not weighted by any per-gameweek observation
  count) beats the champion's own mean MAE over that SAME set of gameweeks by
  at least ``mae_improvement_margin`` (default 5%) -- a deliberately
  non-hair-trigger margin so a challenger that's merely "not worse" doesn't
  get promoted on noise.
- ``per_position_segmentation``: see its own dedicated section below -- an
  architecture-level, offline diagnostic rather than a per-gameweek shadow
  comparison, so it works differently enough from the two above to warrant
  its own explanation rather than a bullet here.

One gate the master spec's own vocabulary suggests (historical backtest
replay against multiple past seasons) is NOT implemented here -- it would
need its own harness (arguably Layer 6's ``BACKTEST`` job) rather than living
inside a promotion-decision function. It is represented in
``PromotionGateResult.gate_checks`` as always-``True``/not-applicable,
documented here and in ``evaluate_promotion``'s own docstring, rather than
silently omitted -- an operator reading ``gate_results_json`` on a
``model_promotions`` row should be able to see exactly what was and wasn't
actually checked.

``per_position_segmentation`` (also part of the master spec's vocabulary) WAS
in that same not-implemented category until this session's
``app.modeling.position_segmentation``/``POSITION_SEGMENTATION_VALIDATION``
job closed the underlying gap it named: "there is no per-position actuals
breakdown computed anywhere in this codebase yet." It is now a REAL,
computed gate -- see ``_latest_position_segmentation_gaps``/
``evaluate_promotion`` below. What it checks is deliberately narrower than
"does the challenger itself beat the champion per-position" (the live
shadow-challenger comparison this module otherwise runs on is a SINGLE
fitted artifact per side -- ``app.modeling.champion_loader.LoadedChampion``
-- and retrofitting it to juggle 4 separate position-specific artifacts per
side is a much larger, riskier change than this gate needs): instead it
checks "does the CURRENT single-global-model architecture have a
demonstrated, meaningful per-position blind spot that a position-specific
approach would fix" -- an offline, architecture-level diagnostic computed
via real walk-forward backtesting (``POSITION_SEGMENTATION_VALIDATION``),
not a per-promotion recomputation. Because both the current champion and any
challenger trained via ``MODEL_TRAINING`` share that same single-global-model
architecture, a real blind spot found here is equally relevant to any
promotion decision, not specific to one challenger -- which is also why,
unlike ``beats_champion_by_margin``, ABSENCE of a validation run never blocks
a promotion (``gate_checks["per_position_segmentation"] = True`` when no run
exists yet): it is informational until real evidence exists, exactly like
``historical_backtest_replay`` remains, not a hard precondition the way
``minimum_sample_size`` is for the challenger-specific MAE comparison.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modeling.champion import HasIsChampion, set_exactly_one_champion
from packages.database.models.gameweek import Gameweek
from packages.database.models.model_challenger_evaluation import ModelChallengerEvaluation
from packages.database.models.model_promotion import ModelPromotion
from packages.database.models.model_version import ModelVersion
from packages.database.models.position_segmentation_validation_run import PositionSegmentationValidationRun
from packages.domain.learning import (
    ModelLifecycleState,
    PositionSegmentationValidationRunStatus,
    PromotionVerdict,
)

__all__ = [
    "PromotionGateResult",
    "evaluate_promotion",
    "decide_and_record_promotion",
    "rollback_to_previous_champion",
]

# --- Named thresholds/constants (see module docstring for the full rationale) ------------------------
_DEFAULT_MIN_EVALUATION_WINDOW_GAMEWEEKS = 6
_DEFAULT_MAE_IMPROVEMENT_MARGIN = 0.05

# Gates the master spec's own vocabulary names but this codebase cannot yet actually compute --
# always reported as passing/not-applicable, never silently omitted. See module docstring.
# per_position_segmentation is deliberately NOT in this tuple any more -- see module docstring.
_NOT_IMPLEMENTED_GATE_NAMES = ("historical_backtest_replay",)


@dataclass(frozen=True)
class PromotionGateResult:
    """One promotion-gate decision, pure and directly testable (see ``_evaluate_gate``)."""

    verdict: PromotionVerdict
    gate_checks: dict[str, bool]
    rationale: str
    n_observations: int


def _per_position_gate_passes(
    per_position_mae_gap_pct: dict[str, float | None] | None, mae_improvement_margin: float
) -> bool:
    """True unless a real ``POSITION_SEGMENTATION_VALIDATION`` run found a position where a
    position-specific model would beat the current single-global-model architecture by at least
    ``mae_improvement_margin`` -- the same margin ``beats_champion_by_margin`` uses, so "a
    meaningful improvement" means the same thing in both gates.

    ``per_position_mae_gap_pct`` is ``None`` (gate passes) whenever no validation run exists yet
    for the relevant ``feature_version`` -- see module docstring for why absence of evidence never
    blocks a promotion here, unlike ``minimum_sample_size``. A per-position value of ``None``
    (that specific position had no defined gap -- e.g. zero validation rows for it) is likewise
    never treated as a failure; only a real, positive, large-enough gap does.
    """
    if not per_position_mae_gap_pct:
        return True
    return all(
        gap is None or gap < mae_improvement_margin for gap in per_position_mae_gap_pct.values()
    )


def _evaluate_gate(
    challenger_maes: list[float],
    champion_maes: list[float],
    min_evaluation_window: int,
    mae_improvement_margin: float,
    per_position_mae_gap_pct: dict[str, float | None] | None = None,
) -> PromotionGateResult:
    """Pure decision logic, factored out of ``evaluate_promotion``'s DB query for direct unit testing.

    ``challenger_maes``/``champion_maes`` must already be the SAME length,
    index-aligned (one pair per qualifying gameweek, chronological order is
    irrelevant here since the aggregation is an unweighted mean, not a
    sustained-window check the way ``app.learning.signals``'s degradation
    detection is).

    ``per_position_mae_gap_pct`` (position -> ``mae_gap_pct`` from the latest
    ``PositionSegmentationValidationRun``, or ``None`` if no run exists yet) drives the real
    ``per_position_segmentation`` gate -- see ``_per_position_gate_passes`` and this module's own
    docstring for exactly what it checks and why it can never, by itself, force a
    ``RETAINED_INCONCLUSIVE``/``REJECTED`` verdict the way ``beats_champion_by_margin`` can (it is
    reported in ``gate_checks`` either way, informationally, but a promotion whose challenger
    legitimately beats the champion by margin is not blocked by an architecture-level finding that
    applies equally to both sides).
    """
    n = len(challenger_maes)
    gate_checks: dict[str, bool] = {name: True for name in _NOT_IMPLEMENTED_GATE_NAMES}
    gate_checks["per_position_segmentation"] = _per_position_gate_passes(
        per_position_mae_gap_pct, mae_improvement_margin
    )
    gate_checks["minimum_sample_size"] = n >= min_evaluation_window

    if n < min_evaluation_window:
        gate_checks["beats_champion_by_margin"] = False
        return PromotionGateResult(
            verdict=PromotionVerdict.RETAINED_INCONCLUSIVE,
            gate_checks=gate_checks,
            rationale=(
                f"Only {n} shadow evaluation(s) recorded for this challenger/champion pair; "
                f"at least {min_evaluation_window} are required before a promotion decision can be "
                "made. Retained as SHADOW_CHALLENGER pending more evidence."
            ),
            n_observations=n,
        )

    mean_challenger_mae = sum(challenger_maes) / n
    mean_champion_mae = sum(champion_maes) / n
    required_mae = mean_champion_mae * (1 - mae_improvement_margin)
    beats_margin = mean_challenger_mae <= required_mae
    gate_checks["beats_champion_by_margin"] = beats_margin

    verdict = PromotionVerdict.PROMOTED if beats_margin else PromotionVerdict.REJECTED
    rationale = (
        f"challenger mean MAE={mean_challenger_mae:.4f} vs. champion mean MAE={mean_champion_mae:.4f} "
        f"over {n} gameweek evaluation(s) (required margin={mae_improvement_margin:.0%}, "
        f"challenger must be <= {required_mae:.4f}): "
        f"{'beats' if beats_margin else 'does not beat'} the required threshold."
    )
    return PromotionGateResult(
        verdict=verdict, gate_checks=gate_checks, rationale=rationale, n_observations=n
    )


def evaluate_promotion(
    session: Session,
    challenger_model_version_id: uuid.UUID,
    champion_model_version_id: uuid.UUID,
    min_evaluation_window: int = _DEFAULT_MIN_EVALUATION_WINDOW_GAMEWEEKS,
    mae_improvement_margin: float = _DEFAULT_MAE_IMPROVEMENT_MARGIN,
) -> PromotionGateResult:
    """Load every ``model_challenger_evaluations`` row for this EXACT (challenger, champion) pair and
    apply the promotion gate -- see module docstring for the three real gates and the one
    documented not-implemented one.

    Aggregation rule: a simple, unweighted mean of ``challenger_mae``/
    ``champion_mae`` across EVERY qualifying evaluation found (never just the
    most recent N) -- once the minimum-sample-size bar is cleared, using more
    history is strictly more robust for a "should we permanently switch"
    decision than truncating to a small recent window would be (unlike
    ``app.learning.signals``'s sustained-BREACH detection, which deliberately
    only looks at the most recent consecutive entries because it cares about
    a CURRENT, still-active degradation pattern -- promotion is the opposite
    question: "has this challenger been consistently better across the whole
    evaluation history", where more data is better, not worse).

    Rows with a NULL ``challenger_mae``/``champion_mae`` (should not happen in
    practice -- ``compute_shadow_challenger_evaluation`` never persists one --
    but defensively excluded here) are skipped.

    ``per_position_segmentation`` is resolved automatically from whatever the latest
    ``POSITION_SEGMENTATION_VALIDATION`` run found for the challenger's own ``feature_version``
    (see ``_latest_position_segmentation_gaps``) -- callers never need to pass this in themselves.
    """
    rows = (
        session.execute(
            select(ModelChallengerEvaluation)
            .join(Gameweek, Gameweek.id == ModelChallengerEvaluation.gameweek_id)
            .where(
                ModelChallengerEvaluation.challenger_model_version_id == challenger_model_version_id,
                ModelChallengerEvaluation.champion_model_version_id == champion_model_version_id,
                ModelChallengerEvaluation.challenger_mae.isnot(None),
                ModelChallengerEvaluation.champion_mae.isnot(None),
            )
            .order_by(Gameweek.fpl_event_id.asc())
        )
        .scalars()
        .all()
    )
    challenger_maes = [float(row.challenger_mae) for row in rows]  # type: ignore[arg-type]
    champion_maes = [float(row.champion_mae) for row in rows]  # type: ignore[arg-type]

    challenger = session.get(ModelVersion, challenger_model_version_id)
    per_position_mae_gap_pct = (
        _latest_position_segmentation_gaps(session, challenger.feature_version) if challenger else None
    )

    return _evaluate_gate(
        challenger_maes,
        champion_maes,
        min_evaluation_window,
        mae_improvement_margin,
        per_position_mae_gap_pct,
    )


def _latest_position_segmentation_gaps(
    session: Session, feature_version: int
) -> dict[str, float | None] | None:
    """The most recent SUCCEEDED ``POSITION_SEGMENTATION_VALIDATION`` run's ``mae_gap_pct`` per
    position, for this exact ``feature_version`` -- ``None`` if no such run exists yet (see
    ``_per_position_gate_passes``/module docstring for why absence of a run never blocks a
    promotion). Matching by ``feature_version`` (not just "most recent overall") because a
    position-segmentation finding computed against an OLDER feature set may no longer be relevant
    once features have changed.
    """
    run = session.execute(
        select(PositionSegmentationValidationRun)
        .where(
            PositionSegmentationValidationRun.feature_version == feature_version,
            PositionSegmentationValidationRun.run_status == PositionSegmentationValidationRunStatus.SUCCEEDED,
        )
        .order_by(PositionSegmentationValidationRun.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run is None:
        return None
    return {position: values["mae_gap_pct"] for position, values in run.per_position_json.items()}


def _current_champion_row(session: Session) -> ModelVersion | None:
    return session.execute(
        select(ModelVersion).where(ModelVersion.lifecycle_state == ModelLifecycleState.CHAMPION)
    ).scalar_one_or_none()


def decide_and_record_promotion(
    session: Session,
    gate_result: PromotionGateResult,
    challenger_model_version_id: uuid.UUID,
    decided_by_job_id: uuid.UUID | None = None,
) -> ModelPromotion:
    """Persist ``gate_result``'s decision and apply its lifecycle-state/``is_champion`` consequences.

    A ``model_promotions`` row is always persisted, for all three verdicts
    reachable here -- this module's own audit trail is "every gate evaluation
    that ran", not just the ones that changed something, so a
    ``RETAINED_INCONCLUSIVE`` decision is itself recorded (with no
    lifecycle-state side effects) rather than left invisible between the
    ``model_challenger_evaluations`` rows that fed it and whatever the NEXT
    gate evaluation eventually decides.

    - ``PROMOTED``: calls ``set_exactly_one_champion`` (existing function,
      UNMODIFIED) with the challenger as the new champion across every
      ``model_versions`` row ever inserted, then sets the challenger's own
      ``lifecycle_state = CHAMPION`` and the PREVIOUS champion's (if any) to
      ``RETIRED_CHAMPION`` -- never deleted, per this system's own audit-trail
      philosophy.
    - ``REJECTED``: the challenger's ``lifecycle_state = REJECTED_CHALLENGER``;
      ``is_champion``/the current champion are untouched.
    - ``RETAINED_INCONCLUSIVE``: no lifecycle_state change at all (the
      challenger stays ``SHADOW_CHALLENGER``, to be re-evaluated again once
      more evidence exists).
    """
    challenger = session.get(ModelVersion, challenger_model_version_id)
    if challenger is None:
        raise ValueError(f"model_versions row {challenger_model_version_id} not found.")

    previous_champion = _current_champion_row(session)

    if gate_result.verdict == PromotionVerdict.PROMOTED:
        all_versions: list[HasIsChampion] = list(session.execute(select(ModelVersion)).scalars().all())
        set_exactly_one_champion(all_versions, challenger)
        challenger.lifecycle_state = ModelLifecycleState.CHAMPION
        if previous_champion is not None and previous_champion.id != challenger.id:
            previous_champion.lifecycle_state = ModelLifecycleState.RETIRED_CHAMPION
    elif gate_result.verdict == PromotionVerdict.REJECTED:
        challenger.lifecycle_state = ModelLifecycleState.REJECTED_CHALLENGER
    elif gate_result.verdict == PromotionVerdict.RETAINED_INCONCLUSIVE:
        pass  # no lifecycle_state change -- stays SHADOW_CHALLENGER, re-evaluated later
    else:  # pragma: no cover - defensive; PromotionVerdict has no other member reachable here
        raise ValueError(f"Unexpected verdict for decide_and_record_promotion: {gate_result.verdict}")

    promotion = ModelPromotion(
        challenger_model_version_id=challenger.id,
        previous_champion_model_version_id=(previous_champion.id if previous_champion is not None else None),
        verdict=gate_result.verdict,
        gate_results_json=gate_result.gate_checks,
        evaluation_window_gameweeks=gate_result.n_observations,
        n_shadow_evaluations=gate_result.n_observations,
        decided_at=datetime.now(UTC),
        decided_by_job_id=decided_by_job_id,
        report_markdown=gate_result.rationale,
    )
    session.add(promotion)
    session.commit()
    session.refresh(promotion)
    return promotion


def rollback_to_previous_champion(
    session: Session,
    target_model_version_id: uuid.UUID,
    decided_by_job_id: uuid.UUID | None = None,
) -> ModelPromotion:
    """Reinstate a ``RETIRED_CHAMPION`` as the live champion again -- an operator decision, not a
    gate-evaluated promotion (no ``model_challenger_evaluations``/``PromotionGateResult`` involved).

    ``target_model_version_id`` MUST currently be ``lifecycle_state ==
    RETIRED_CHAMPION`` (never deleted, per this system's own audit-trail
    philosophy -- a rollback target always still exists to roll back to).
    Reuses ``set_exactly_one_champion`` exactly as ``decide_and_record_promotion``'s
    ``PROMOTED`` branch does -- verified by reading that function's own
    implementation (``app/modeling/champion.py``): it is a pure "flip
    ``is_champion`` on exactly the given row, off on every other" operation
    over an arbitrary set of candidates, with no assumption anywhere that the
    new champion must be freshly trained or have never been champion before,
    so reinstating a former, still-present ``ModelVersion`` row works
    identically to promoting a brand-new challenger, with zero changes needed
    to that function.

    A rollback doesn't delete anything -- it just flips which row is live: the
    target becomes ``CHAMPION``, and whichever row was ``CHAMPION`` right
    before this call becomes ``RETIRED_CHAMPION`` in turn (so a rollback can
    itself later be rolled back).
    """
    target = session.get(ModelVersion, target_model_version_id)
    if target is None:
        raise ValueError(f"model_versions row {target_model_version_id} not found.")
    if target.lifecycle_state != ModelLifecycleState.RETIRED_CHAMPION:
        raise ValueError(
            f"rollback target must have lifecycle_state=RETIRED_CHAMPION, got "
            f"{target.lifecycle_state.value!r} for model_versions row {target_model_version_id}."
        )

    current_champion = _current_champion_row(session)

    all_versions: list[HasIsChampion] = list(session.execute(select(ModelVersion)).scalars().all())
    set_exactly_one_champion(all_versions, target)
    target.lifecycle_state = ModelLifecycleState.CHAMPION
    if current_champion is not None and current_champion.id != target.id:
        current_champion.lifecycle_state = ModelLifecycleState.RETIRED_CHAMPION

    promotion = ModelPromotion(
        challenger_model_version_id=target.id,
        previous_champion_model_version_id=(current_champion.id if current_champion is not None else None),
        verdict=PromotionVerdict.ROLLED_BACK,
        gate_results_json=None,
        evaluation_window_gameweeks=None,
        n_shadow_evaluations=None,
        decided_at=datetime.now(UTC),
        decided_by_job_id=decided_by_job_id,
        report_markdown=(
            f"Manual rollback: reinstated model_versions row {target.id} (previously "
            f"RETIRED_CHAMPION) as champion."
        ),
    )
    session.add(promotion)
    session.commit()
    session.refresh(promotion)
    return promotion
