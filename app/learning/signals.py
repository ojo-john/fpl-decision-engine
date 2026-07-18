"""Rolling learning signals (Milestone 7 Layer 3: Recursive Learning Loop -- Failure Attribution/
Rolling Learning Signals).

Four ``compute_*`` functions, each producing one ``LearningSignalData`` ready
for ``LearningSignal`` persistence (via ``persist_learning_signal``). Every
function is a plain, directly-callable, DB-session-taking query + aggregation
-- there is no separate pure/orchestrator split here the way
``app.learning.attribution`` has one, EXCEPT for the "is this degradation
pattern sustained, not a one-gameweek blip" decision, which IS factored out
into the pure, directly-unit-testable ``_is_sustained_breach`` below (shared
by ``compute_rolling_projection_mae``/``compute_rolling_h2h_calibration``) and
``_reason_code_concentration`` (used by
``compute_failure_reason_concentration``).

Sustained-window design (``compute_rolling_projection_mae``/
``compute_rolling_h2h_calibration``): ``threshold_breached`` is deliberately
NOT "does the aggregate over the whole window exceed a bar" -- a single bad
gameweek could drag an otherwise-healthy aggregate over almost any fixed bar,
and the master spec is explicit that one bad H2H matchup (or one bad
projection gameweek) must never, by itself, look like a retraining-worthy
trend. Instead, each function builds a PER-GAMEWEEK (or per-match) series and
``threshold_breached`` is ``True`` only when the MOST RECENT
``_MIN_CONSECUTIVE_DEGRADED_*`` entries of that series are ALL individually
over the bar -- a genuinely persistent, current pattern, not a blip that has
since resolved and not a stale one from several gameweeks ago.

``compute_rolling_chip_recommendation_value`` is explicitly the ODD ONE OUT:
chips are rare (at most a handful of uses across a whole season), so there is
no meaningful weekly rolling window for it at all -- ``window_size_gameweeks``
honestly reports the full season-to-date span instead of a small fixed N, and
``threshold_breached`` is ALWAYS ``False``. This is deliberate and documented
on the function itself: this signal is informational only and must NEVER be
treated as a modeling-decay/retraining signal the same way
``ROLLING_PROJECTION_MAE`` is -- a later challenger/retraining-trigger layer
(Milestone 7 Layers 5-6, not built yet) must not wire this signal's
``threshold_breached`` into any promotion/retraining decision.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.learning.actuals import sum_actual_points
from packages.database.models.counterfactual_result import CounterfactualResult
from packages.database.models.failure_attribution import FailureAttribution
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.learning_signal import LearningSignal
from packages.database.models.model_metric import ModelMetric
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.recommendation_outcome import RecommendationOutcome
from packages.domain.learning import CounterfactualComparisonType, FailureReasonCode, LearningSignalType

__all__ = [
    "LearningSignalData",
    "compute_rolling_projection_mae",
    "compute_rolling_h2h_calibration",
    "compute_rolling_chip_recommendation_value",
    "compute_failure_reason_concentration",
    "persist_learning_signal",
]

# --- Named thresholds/constants ---------------------------------------------------------------------
# 25% worse than the champion's own walk-forward-validation aggregate MAE (packages.database.models
# .model_metric, fold_index IS NULL row) -- a meaningful, but not hair-trigger, degradation bar.
_PROJECTION_MAE_DEGRADATION_THRESHOLD = 1.25
_MIN_CONSECUTIVE_DEGRADED_GAMEWEEKS = 3

# A well-calibrated binary win-probability predictor keeps Brier score comfortably below 0.25 (the
# score a constant "always predict 0.5" guesser gets) -- 0.30 is deliberately set ABOVE that random-
# guessing baseline, so this only breaches for a predictor that is genuinely worse than a coin flip,
# not merely "imperfectly calibrated".
_H2H_BRIER_DEGRADATION_THRESHOLD = 0.30
_MIN_CONSECUTIVE_DEGRADED_MATCHES = 3

# compute_failure_reason_concentration: the same FailureReasonCode appearing in at least this many of
# the last `lookback_batches` batches signals a persistent, non-random pattern (vs. one-off variance).
_FAILURE_REASON_CONCENTRATION_MIN_BATCHES = 3

_DEFAULT_PROJECTION_MAE_WINDOW_GAMEWEEKS = 6
_DEFAULT_H2H_CALIBRATION_WINDOW_GAMEWEEKS = 8
_DEFAULT_FAILURE_REASON_LOOKBACK_BATCHES = 5


@dataclass(frozen=True)
class LearningSignalData:
    """One computed rolling-signal result, ready for ``LearningSignal`` persistence."""

    signal_type: LearningSignalType
    window_size_gameweeks: int
    value: Decimal
    sample_count: int
    threshold_breached: bool
    threshold_config_snapshot: dict | None


def _is_sustained_breach(per_period_values: Sequence[float], threshold: float, min_consecutive: int) -> bool:
    """``True`` only when the MOST RECENT ``min_consecutive`` entries of ``per_period_values``
    (chronologically ordered oldest-first) are ALL ``>= threshold`` -- a genuinely sustained, still-
    current pattern.

    ``False`` whenever there isn't even enough history to judge (fewer than
    ``min_consecutive`` entries) -- an absence of evidence is never treated as
    a breach. A single degraded entry anywhere in the series, including the
    very last one, is NOT enough on its own; only ``min_consecutive``
    *consecutive*, *recent* entries all breaching together counts.
    """
    if len(per_period_values) < min_consecutive:
        return False
    return all(value >= threshold for value in per_period_values[-min_consecutive:])


def _reason_code_concentration(
    batches: Sequence[set[FailureReasonCode]],
) -> tuple[FailureReasonCode | None, int]:
    """The most-repeated ``FailureReasonCode`` across ``batches`` (each element = the distinct set of
    reason codes attached to one ``recommendation_outcome``) and how many batches it appeared in.

    ``RECOMMENDATION_WAS_SOUND`` is never counted here -- callers are expected
    to have already excluded it (it isn't a failure), but this function
    defensively ignores it too so a hand-built test set doesn't need to
    remember that exclusion itself.
    """
    counts: dict[FailureReasonCode, int] = {}
    for batch in batches:
        for reason_code in batch:
            if reason_code == FailureReasonCode.RECOMMENDATION_WAS_SOUND:
                continue
            counts[reason_code] = counts.get(reason_code, 0) + 1
    if not counts:
        return None, 0
    top_code = max(counts, key=lambda code: counts[code])
    return top_code, counts[top_code]


def _resolve_fpl_event_id(session: Session, gameweek_id: uuid.UUID) -> int:
    gameweek = session.get(Gameweek, gameweek_id)
    if gameweek is None:
        raise ValueError(f"gameweeks row {gameweek_id} not found.")
    return gameweek.fpl_event_id


def compute_rolling_projection_mae(
    session: Session,
    season_id: uuid.UUID,
    as_of_gameweek_id: uuid.UUID,
    window_size_gameweeks: int = _DEFAULT_PROJECTION_MAE_WINDOW_GAMEWEEKS,
) -> LearningSignalData:
    """Mean absolute error of the current champion's ``optimization_run_players.projected_points``
    vs. real ``player_gameweek_stats`` points, over the last ``window_size_gameweeks`` authoritative,
    resolved gameweeks (whichever ``optimization_run_id`` each gameweek's own
    ``recommendation_audits`` row referenced).

    Gracefully returns ``sample_count=0`` (never fabricating a signal) when
    too little history exists yet. ``threshold_breached`` uses the sustained-
    window logic documented on the module -- see ``_is_sustained_breach``.
    """
    as_of_fpl_event_id = _resolve_fpl_event_id(session, as_of_gameweek_id)

    audits = (
        session.execute(
            select(RecommendationAudit)
            .join(
                RecommendationOutcome,
                RecommendationOutcome.recommendation_audit_id == RecommendationAudit.id,
            )
            .where(
                RecommendationAudit.season_id == season_id,
                RecommendationAudit.is_authoritative.is_(True),
                RecommendationAudit.optimization_run_id.isnot(None),
                RecommendationAudit.target_fpl_event_id <= as_of_fpl_event_id,
            )
            .order_by(RecommendationAudit.target_fpl_event_id.desc())
            .limit(window_size_gameweeks)
        )
        .scalars()
        .all()
    )

    if not audits:
        return LearningSignalData(
            signal_type=LearningSignalType.ROLLING_PROJECTION_MAE,
            window_size_gameweeks=0,
            value=Decimal("0.0000"),
            sample_count=0,
            threshold_breached=False,
            threshold_config_snapshot={
                "degradation_threshold": _PROJECTION_MAE_DEGRADATION_THRESHOLD,
                "min_consecutive_degraded_gameweeks": _MIN_CONSECUTIVE_DEGRADED_GAMEWEEKS,
                "baseline_mae": None,
            },
        )

    per_gameweek: list[tuple[int, float]] = []  # (target_fpl_event_id, this gameweek's own MAE)
    all_abs_errors: list[float] = []
    for audit in audits:
        run_players = (
            session.execute(
                select(OptimizationRunPlayer).where(
                    OptimizationRunPlayer.optimization_run_id == audit.optimization_run_id
                )
            )
            .scalars()
            .all()
        )
        gameweek_abs_errors = []
        for run_player in run_players:
            actual_points = sum_actual_points(session, [run_player.player_id], audit.target_gameweek_id)
            gameweek_abs_errors.append(abs(actual_points - float(run_player.projected_points)))
        if gameweek_abs_errors:
            per_gameweek.append(
                (audit.target_fpl_event_id, sum(gameweek_abs_errors) / len(gameweek_abs_errors))
            )
            all_abs_errors.extend(gameweek_abs_errors)

    per_gameweek.sort(key=lambda item: item[0])  # chronological, oldest-first
    per_gameweek_maes = [mae for _, mae in per_gameweek]

    # Baseline: the champion's own walk-forward-validation aggregate MAE, from the MOST RECENT
    # audit in this window (audits[0], since the query above is already ordered descending).
    most_recent_audit = audits[0]
    baseline_mae: float | None = None
    if most_recent_audit.champion_model_version_id is not None:
        baseline_row = session.execute(
            select(ModelMetric).where(
                ModelMetric.model_version_id == most_recent_audit.champion_model_version_id,
                ModelMetric.fold_index.is_(None),
            )
        ).scalar_one_or_none()
        if baseline_row is not None:
            baseline_mae = float(baseline_row.mae)

    threshold_breached = False
    if baseline_mae is not None and baseline_mae > 0:
        degraded_threshold = baseline_mae * _PROJECTION_MAE_DEGRADATION_THRESHOLD
        threshold_breached = _is_sustained_breach(
            per_gameweek_maes, degraded_threshold, _MIN_CONSECUTIVE_DEGRADED_GAMEWEEKS
        )

    overall_mae = sum(all_abs_errors) / len(all_abs_errors) if all_abs_errors else 0.0

    return LearningSignalData(
        signal_type=LearningSignalType.ROLLING_PROJECTION_MAE,
        window_size_gameweeks=len(per_gameweek),
        value=Decimal(str(round(overall_mae, 4))),
        sample_count=len(all_abs_errors),
        threshold_breached=threshold_breached,
        threshold_config_snapshot={
            "degradation_threshold": _PROJECTION_MAE_DEGRADATION_THRESHOLD,
            "min_consecutive_degraded_gameweeks": _MIN_CONSECUTIVE_DEGRADED_GAMEWEEKS,
            "baseline_mae": baseline_mae,
        },
    )


def compute_rolling_h2h_calibration(
    session: Session,
    season_id: uuid.UUID,
    as_of_gameweek_id: uuid.UUID,
    window_size_gameweeks: int = _DEFAULT_H2H_CALIBRATION_WINDOW_GAMEWEEKS,
) -> LearningSignalData:
    """Brier score of ``h2h_simulation_runs.win_probability`` vs. the actual W/D/L outcome
    (``recommendation_outcomes.h2h_result``, already resolved by Layer 2), over the last
    ``window_size_gameweeks`` authoritative, resolved H2H matches.

    Deliberately scored against the single "win" event only (predicted
    ``win_probability`` vs. ``1`` if the match was won else ``0``) -- not a
    full 3-outcome win/draw/loss multi-class Brier score, since
    ``win_probability`` is the one output ``H2HSimulationRun`` and its own
    callers treat as the primary decision-relevant number (see that model's
    docstring). ``threshold_breached`` uses the same sustained-window logic
    as ``compute_rolling_projection_mae`` -- see ``_is_sustained_breach``.
    """
    as_of_fpl_event_id = _resolve_fpl_event_id(session, as_of_gameweek_id)

    rows = session.execute(
        select(RecommendationAudit, RecommendationOutcome)
        .join(
            RecommendationOutcome,
            RecommendationOutcome.recommendation_audit_id == RecommendationAudit.id,
        )
        .where(
            RecommendationAudit.season_id == season_id,
            RecommendationAudit.is_authoritative.is_(True),
            RecommendationAudit.h2h_simulation_run_id.isnot(None),
            RecommendationOutcome.h2h_result.isnot(None),
            RecommendationAudit.target_fpl_event_id <= as_of_fpl_event_id,
        )
        .order_by(RecommendationAudit.target_fpl_event_id.desc())
        .limit(window_size_gameweeks)
    ).all()

    per_match: list[tuple[int, float]] = []
    for audit, outcome in rows:
        h2h_run = session.get(H2HSimulationRun, audit.h2h_simulation_run_id)
        if h2h_run is None or h2h_run.win_probability is None:
            continue
        actual_win = 1.0 if outcome.h2h_result == "win" else 0.0
        brier = (float(h2h_run.win_probability) - actual_win) ** 2
        per_match.append((audit.target_fpl_event_id, brier))

    if not per_match:
        return LearningSignalData(
            signal_type=LearningSignalType.ROLLING_H2H_WIN_PROBABILITY_CALIBRATION,
            window_size_gameweeks=0,
            value=Decimal("0.0000"),
            sample_count=0,
            threshold_breached=False,
            threshold_config_snapshot={
                "degradation_threshold": _H2H_BRIER_DEGRADATION_THRESHOLD,
                "min_consecutive_degraded_matches": _MIN_CONSECUTIVE_DEGRADED_MATCHES,
            },
        )

    per_match.sort(key=lambda item: item[0])  # chronological, oldest-first
    brier_scores = [brier for _, brier in per_match]

    threshold_breached = _is_sustained_breach(
        brier_scores, _H2H_BRIER_DEGRADATION_THRESHOLD, _MIN_CONSECUTIVE_DEGRADED_MATCHES
    )

    return LearningSignalData(
        signal_type=LearningSignalType.ROLLING_H2H_WIN_PROBABILITY_CALIBRATION,
        window_size_gameweeks=len(per_match),
        value=Decimal(str(round(sum(brier_scores) / len(brier_scores), 4))),
        sample_count=len(brier_scores),
        threshold_breached=threshold_breached,
        threshold_config_snapshot={
            "degradation_threshold": _H2H_BRIER_DEGRADATION_THRESHOLD,
            "min_consecutive_degraded_matches": _MIN_CONSECUTIVE_DEGRADED_MATCHES,
        },
    )


def compute_rolling_chip_recommendation_value(
    session: Session, season_id: uuid.UUID, as_of_gameweek_id: uuid.UUID
) -> LearningSignalData:
    """Mean ``CHIP_VS_HOLD`` counterfactual delta, ALL-TIME (season-to-date) -- chips are rare (at
    most a handful of uses across a whole season), so there is no meaningful WEEKLY rolling window
    the way projection MAE/H2H calibration have; ``window_size_gameweeks`` honestly reports the full
    season-to-date span instead of a small fixed N.

    ``threshold_breached`` is ALWAYS ``False`` here -- deliberately informational
    only, per the module's own docstring. A later challenger/retraining-trigger
    layer must not wire this into any promotion/retraining decision the way it
    would ``ROLLING_PROJECTION_MAE``'s ``threshold_breached``.
    """
    as_of_fpl_event_id = _resolve_fpl_event_id(session, as_of_gameweek_id)

    deltas = (
        session.execute(
            select(CounterfactualResult.delta)
            .join(
                RecommendationOutcome,
                RecommendationOutcome.id == CounterfactualResult.recommendation_outcome_id,
            )
            .join(
                RecommendationAudit,
                RecommendationAudit.id == RecommendationOutcome.recommendation_audit_id,
            )
            .where(
                RecommendationAudit.season_id == season_id,
                RecommendationAudit.is_authoritative.is_(True),
                CounterfactualResult.comparison_type == CounterfactualComparisonType.CHIP_VS_HOLD,
                RecommendationAudit.target_fpl_event_id <= as_of_fpl_event_id,
            )
        )
        .scalars()
        .all()
    )

    value = sum(float(delta) for delta in deltas) / len(deltas) if deltas else 0.0

    return LearningSignalData(
        signal_type=LearningSignalType.ROLLING_CHIP_RECOMMENDATION_VALUE,
        window_size_gameweeks=as_of_fpl_event_id,
        value=Decimal(str(round(value, 4))),
        sample_count=len(deltas),
        threshold_breached=False,
        threshold_config_snapshot={
            "informational_only": True,
            "note": (
                "chips are rare -- no meaningful weekly rolling window; never treated as a "
                "modeling-decay/retraining signal"
            ),
        },
    )


def compute_failure_reason_concentration(
    session: Session,
    season_id: uuid.UUID,
    as_of_gameweek_id: uuid.UUID,
    lookback_batches: int = _DEFAULT_FAILURE_REASON_LOOKBACK_BATCHES,
) -> LearningSignalData:
    """Whether the SAME ``FailureReasonCode`` appears in at least
    ``_FAILURE_REASON_CONCENTRATION_MIN_BATCHES`` of the last ``lookback_batches`` batches (a batch =
    the distinct set of reason codes attached to one ``recommendation_outcome_id``, most recent by
    ``finalized_at``) -- a persistent, non-random pattern worth surfacing, distinct from a single-
    gameweek blip.
    """
    as_of_fpl_event_id = _resolve_fpl_event_id(session, as_of_gameweek_id)

    outcome_rows = session.execute(
        select(RecommendationOutcome.id, RecommendationOutcome.finalized_at)
        .join(
            RecommendationAudit,
            RecommendationAudit.id == RecommendationOutcome.recommendation_audit_id,
        )
        .where(
            RecommendationAudit.season_id == season_id,
            RecommendationAudit.is_authoritative.is_(True),
            RecommendationAudit.target_fpl_event_id <= as_of_fpl_event_id,
        )
        .order_by(RecommendationOutcome.finalized_at.desc())
        .limit(lookback_batches)
    ).all()

    batches: list[set[FailureReasonCode]] = []
    for outcome_id, _finalized_at in outcome_rows:
        reason_codes = (
            session.execute(
                select(FailureAttribution.reason_code).where(
                    FailureAttribution.recommendation_outcome_id == outcome_id
                )
            )
            .scalars()
            .all()
        )
        batches.append(set(reason_codes))

    top_code, top_count = _reason_code_concentration(batches)

    value = Decimal(str(round(top_count / len(batches), 4))) if batches else Decimal("0.0000")
    threshold_breached = top_count >= _FAILURE_REASON_CONCENTRATION_MIN_BATCHES

    return LearningSignalData(
        signal_type=LearningSignalType.FAILURE_REASON_CONCENTRATION,
        window_size_gameweeks=len(batches),
        value=value,
        sample_count=len(batches),
        threshold_breached=threshold_breached,
        threshold_config_snapshot={
            "min_batches_for_concentration": _FAILURE_REASON_CONCENTRATION_MIN_BATCHES,
            "lookback_batches_requested": lookback_batches,
            "most_concentrated_reason_code": top_code.value if top_code is not None else None,
        },
    )


def persist_learning_signal(
    session: Session, season_id: uuid.UUID, as_of_gameweek_id: uuid.UUID, signal: LearningSignalData
) -> LearningSignal:
    """Insert one fresh ``learning_signals`` row for ``signal`` -- never upserted, see that model's
    own docstring for why each computation is its own recordable event.
    """
    row = LearningSignal(
        computed_at=datetime.now(UTC),
        season_id=season_id,
        as_of_gameweek_id=as_of_gameweek_id,
        signal_type=signal.signal_type,
        window_size_gameweeks=signal.window_size_gameweeks,
        value=signal.value,
        sample_count=signal.sample_count,
        threshold_breached=signal.threshold_breached,
        threshold_config_snapshot_json=signal.threshold_config_snapshot,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
