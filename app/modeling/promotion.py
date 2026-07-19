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

Two gates ARE genuinely implementable from data this codebase actually
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

Two gates the master spec's own vocabulary suggests (per-position
segmentation, historical backtest replay against multiple past seasons) are
NOT implemented here -- there is no per-position actuals breakdown computed
anywhere in this codebase yet, and a historical backtest replay would need
its own harness (arguably Layer 6's ``BACKTEST`` job) rather than living
inside a promotion-decision function. Both are represented in
``PromotionGateResult.gate_checks`` as always-``True``/not-applicable,
documented here and in ``evaluate_promotion``'s own docstring, rather than
silently omitted -- an operator reading ``gate_results_json`` on a
``model_promotions`` row should be able to see exactly what was and wasn't
actually checked.
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
from packages.domain.learning import ModelLifecycleState, PromotionVerdict

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
_NOT_IMPLEMENTED_GATE_NAMES = ("per_position_segmentation", "historical_backtest_replay")


@dataclass(frozen=True)
class PromotionGateResult:
    """One promotion-gate decision, pure and directly testable (see ``_evaluate_gate``)."""

    verdict: PromotionVerdict
    gate_checks: dict[str, bool]
    rationale: str
    n_observations: int


def _evaluate_gate(
    challenger_maes: list[float],
    champion_maes: list[float],
    min_evaluation_window: int,
    mae_improvement_margin: float,
) -> PromotionGateResult:
    """Pure decision logic, factored out of ``evaluate_promotion``'s DB query for direct unit testing.

    ``challenger_maes``/``champion_maes`` must already be the SAME length,
    index-aligned (one pair per qualifying gameweek, chronological order is
    irrelevant here since the aggregation is an unweighted mean, not a
    sustained-window check the way ``app.learning.signals``'s degradation
    detection is).
    """
    n = len(challenger_maes)
    gate_checks: dict[str, bool] = {name: True for name in _NOT_IMPLEMENTED_GATE_NAMES}
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
    apply the promotion gate -- see module docstring for the two real gates and the two documented
    not-implemented ones.

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

    return _evaluate_gate(challenger_maes, champion_maes, min_evaluation_window, mae_improvement_margin)


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
