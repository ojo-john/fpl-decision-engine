"""COLD_START_PROJECTION_VALIDATION job: historical validation of
``app.optimization.projection._cold_start_projection``'s hand-written blend formula -- the
cold-start fallback every pre-season squad-building decision this app exists to support goes
through, and which neither ``BACKTEST`` nor ``MODEL_TRAINING``'s own walk-forward validation ever
covers (both only ever evaluate a trained ``model_versions`` row; this formula is not one --
confirmed via ``grep -rn "cold_start" app/modeling/walk_forward.py app/jobs/handlers/backtest.py
app/jobs/handlers/model_training.py``, which returns zero matches).

Mechanically thin: builds the one shared, leakage-safe observation set
(``app.modeling.cold_start_validation.build_player_season_observations``) once, then runs all three
of that module's sweeps against it (half-life, fixture-difficulty coefficient, ep_next/career_avg
blend ratio) and persists one ``cold_start_validation_runs`` row. See that module's own docstring
for the full evaluation design, leakage discipline, and the ep_next proxy caveat.

Also builds a fourth, structurally independent observation set
(``build_true_rookie_observations`` -- players with literally zero prior-season history, the exact
population the main observation set excludes) and its own price-informed-fallback blend-weight
sweep, plus computes and persists the LIVE price-comparable-bucket lookup table
(``compute_price_comparable_buckets``) that ``app.optimization.projection
._price_informed_true_rookie_projection`` reads at prediction time. Never allowed to fail this
job's main three sweeps if the true-rookie population happens to be empty for the currently-usable
seasons (a real, legitimate state, not an error) -- see the ``run`` method's own handling.

A fifth, also structurally independent piece: ``build_dc_reliability_observations`` (DEF/MID only)
and its own coefficient sweep, validating whether a player's real historical rate of hitting FPL's
2025-26+ defensive-contribution bonus threshold should adjust how much their aggregate career_avg
is trusted. Same never-fails-the-main-sweeps discipline as the true-rookie population -- and, as of
this writing, this sweep ALWAYS evaluates zero observations for a real, honest, documented reason
(``defensive_contribution`` has only one real season of data; leakage-safe validation needs a
strictly-prior season's own DC data, which doesn't exist for any real season yet) -- see
``app.modeling.cold_start_validation.build_dc_reliability_observations``'s own docstring. This
signal is NOT wired into ``_cold_start_projection`` in production as a result; it's exposed as a
purely informational field on ``GET /players/{id}/projection`` instead (see
``app.modeling.cold_start_validation.defensive_contribution_hit_rate_prior_seasons``).

Persistence: "build fully in memory, insert once" (mirrors ``BACKTEST``'s own precedent exactly --
pure computation over already-persisted, read-only historical data, no other side-effecting steps to
sequence).

``ctx.parameters`` is entirely optional -- unlike ``BACKTEST`` (which requires a
``model_version_id``; "which model" has no sensible default), this job's own default candidate sets
(``app.modeling.cold_start_validation``'s own module-level constants) ARE the sensible default: an
optional ``half_life_candidates``/``difficulty_coefficient_candidates``/``blend_ratio_candidates``/
``dc_reliability_coefficient_candidates`` list of floats overrides the corresponding sweep for
ad-hoc re-checks without a code change.
"""

import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

from app.features import engineering
from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.modeling.cold_start_validation import (
    BLEND_RATIO_SWEEP_CANDIDATES,
    DC_RELIABILITY_COEFFICIENT_SWEEP_CANDIDATES,
    DIFFICULTY_COEFFICIENT_SWEEP_CANDIDATES,
    EARLY_SEASON_HALF_LIFE_SWEEP_CANDIDATES,
    PRICE_PRIOR_BLEND_WEIGHT_SWEEP_CANDIDATES,
    BlendRatioSweepResult,
    DcReliabilityCoefficientSweepResult,
    HalfLifeSweepResult,
    PricePriorSweepResult,
    build_dc_reliability_observations,
    build_player_season_observations,
    build_true_rookie_observations,
    compute_price_comparable_buckets,
    sweep_blend_ratio_candidates,
    sweep_dc_reliability_coefficient_candidates,
    sweep_difficulty_coefficient_candidates,
    sweep_half_life_candidates,
    sweep_price_prior_blend_weight_candidates,
)
from app.optimization.projection import (
    COLD_START_BLEND_CAREER_AVG_WEIGHT,
    COLD_START_DIFFICULTY_ADJUSTMENT_COEFFICIENT,
    COLD_START_PRICE_PRIOR_BLEND_WEIGHT,
)
from packages.database.models.cold_start_validation_run import ColdStartValidationRun
from packages.database.session import SessionLocal
from packages.domain.jobs import JobStatus, JobType
from packages.domain.learning import ColdStartValidationRunStatus

# Surfaced verbatim on every persisted row's own blend_ratio_caveat column (not just in code
# comments) so any reader of a real run's results -- via the API, not just this repo's source --
# sees the same caveat sweep_blend_ratio_candidates's own docstring documents: no per-gameweek
# historical ep_next snapshot exists; the proxy used here is a stale, season-end value.
_BLEND_RATIO_CAVEAT = (
    "ep_next has no true historical pre-season snapshot -- the proxy used here is the most recent "
    "strictly-prior season's own END-OF-SEASON ep_next value, not a summer-transfer/team-news-aware "
    "pre-season estimate. Treat this sweep as directional, not conclusive, evidence."
)

# Worse than _BLEND_RATIO_CAVEAT: a true rookie has no PRIOR season at all, so this sweep's own
# ep_next proxy can only be the TARGET season's own end-of-season snapshot -- see
# sweep_price_prior_blend_weight_candidates's own docstring for the full reasoning and why a result
# favoring the price prior is stronger evidence than a result favoring ep_next alone.
_PRICE_PRIOR_CAVEAT = (
    "This population (true rookies) has no prior-season ep_next to use as a proxy at all, so the "
    "ep_next side of this blend uses the TARGET season's own end-of-season snapshot -- materially "
    "more hindsight-biased than the main blend-ratio sweep's already-caveated proxy. A result "
    "favoring the price prior is strong, conservative evidence; a result favoring ep_next alone "
    "should be treated with skepticism given this bias."
)

# Real, honest caveat (see build_dc_reliability_observations's own docstring for the full reason):
# defensive_contribution only has one real season of data (2025-26), so leakage-safe validation
# currently, always, has zero observations to score any candidate against -- this sweep cannot
# currently determine even the SIGN of a real effect, and this signal is NOT wired into
# app.optimization.projection._cold_start_projection at all as a result.
_DC_RELIABILITY_CAVEAT = (
    "defensive_contribution (FPL's real 2025-26+ CBIT/CBIRT bonus stat) only has one real season of "
    "data -- leakage-safe validation needs a STRICTLY PRIOR season's own DC data to predict a later, "
    "already-known season's outcome, which does not exist for any real season yet. This sweep "
    "currently, honestly, always evaluates zero observations and is NOT wired into production. Real "
    "evidence will first become possible once 2026-27 gameweeks are ingested with their own real "
    "defensive_contribution values -- re-run this job at that point."
)

# Matches app.optimization.projection's actual current behavior: the DC-reliability adjustment
# isn't applied at all (coefficient 0.0 is a pure no-op in sweep_dc_reliability_coefficient_candidates's
# own formula) -- there is no named production constant for this the way COLD_START_BLEND_CAREER_AVG_WEIGHT
# etc. exist, precisely because this signal has never had real evidence to justify adding one.
_DC_RELIABILITY_PRODUCTION_DEFAULT = 0.0

# Sort key sentinel for min(..., key=...) below: a candidate with an undefined MAE (see each sweep
# dataclass's own "None means genuinely undefined, not 0" contract) must never be picked as
# "best" -- +inf guarantees that, and keeps each key lambda a plain float (no None) for mypy.
_UNDEFINED_MAE_SORT_VALUE = float("inf")


def _to_decimal(value: float | None) -> Decimal | None:
    """Mirrors ``app.jobs.handlers.backtest``'s own identical ``_to_decimal`` convention."""
    return Decimal(str(round(value, 4))) if value is not None else None


@register_handler(JobType.COLD_START_PROJECTION_VALIDATION)
class ColdStartProjectionValidationJobRunner(JobRunner):
    """Runs all three cold-start-formula sweeps against one shared, leakage-safe observation set
    built from every currently-usable historical season. See module docstring for scope.
    """

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        try:
            parameters = ctx.parameters or {}
            half_life_candidates = (
                [float(c) for c in parameters["half_life_candidates"]]
                if "half_life_candidates" in parameters
                else list(EARLY_SEASON_HALF_LIFE_SWEEP_CANDIDATES)
            )
            difficulty_candidates = (
                [float(c) for c in parameters["difficulty_coefficient_candidates"]]
                if "difficulty_coefficient_candidates" in parameters
                else list(DIFFICULTY_COEFFICIENT_SWEEP_CANDIDATES)
            )
            blend_candidates = (
                [float(c) for c in parameters["blend_ratio_candidates"]]
                if "blend_ratio_candidates" in parameters
                else list(BLEND_RATIO_SWEEP_CANDIDATES)
            )
            price_prior_candidates = (
                [float(c) for c in parameters["price_prior_weight_candidates"]]
                if "price_prior_weight_candidates" in parameters
                else list(PRICE_PRIOR_BLEND_WEIGHT_SWEEP_CANDIDATES)
            )
            dc_reliability_candidates = (
                [float(c) for c in parameters["dc_reliability_coefficient_candidates"]]
                if "dc_reliability_coefficient_candidates" in parameters
                else list(DC_RELIABILITY_COEFFICIENT_SWEEP_CANDIDATES)
            )

            observations, usable_seasons = build_player_season_observations(session)
            if len(usable_seasons) < 2:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="NOT_ENOUGH_USABLE_SEASONS",
                    error_message=(
                        f"cannot validate -- {len(usable_seasons)} season(s) with player_gameweek_stats "
                        "rows; at least 2 are required (one to predict from, one to predict)."
                    ),
                )
            if not observations:
                return JobResult(
                    status=JobStatus.FAILED,
                    output=None,
                    error_code="NO_EVALUABLE_OBSERVATIONS",
                    error_message=(
                        "no (player, season) pair had both strictly-prior early-season history and "
                        "real current-season GW1-6 data to evaluate against."
                    ),
                )

            started_at = datetime.now(UTC)
            half_life_results = sweep_half_life_candidates(observations, half_life_candidates)
            difficulty_results = sweep_difficulty_coefficient_candidates(observations, difficulty_candidates)
            blend_results = sweep_blend_ratio_candidates(observations, blend_candidates)

            # Structurally independent population (career_avg is None -- zero prior-season history
            # at all, disjoint from `observations` above, which all require non-empty prior_stats)
            # -- never allowed to fail this job even if zero true rookies are evaluable (a small,
            # real population that can legitimately be empty for some historical seasons).
            true_rookie_observations, _ = build_true_rookie_observations(session)
            price_prior_results = (
                sweep_price_prior_blend_weight_candidates(
                    session, true_rookie_observations, usable_seasons, price_prior_candidates
                )
                if true_rookie_observations
                else []
            )
            price_comparable_buckets, _ = compute_price_comparable_buckets(session)

            # Fifth, structurally different, DEF/MID-only population -- never allowed to fail this
            # job's main sweeps even though it currently, honestly, always evaluates zero real
            # observations (see build_dc_reliability_observations's own docstring for the real
            # reason: defensive_contribution has only one real season of data so far).
            dc_reliability_observations, _ = build_dc_reliability_observations(session)
            dc_reliability_results = sweep_dc_reliability_coefficient_candidates(
                dc_reliability_observations, dc_reliability_candidates
            )
            completed_at = datetime.now(UTC)

            # half_life_results is guaranteed >=1 defined-MAE entry whenever observations is
            # non-empty (already checked above) -- build_player_season_observations only ever
            # returns observations whose prior_stats produce a real prediction for ANY half-life.
            best_half_life = min(
                half_life_results, key=lambda r: r.mae if r.mae is not None else _UNDEFINED_MAE_SORT_VALUE
            )
            default_half_life = self._match_half_life(
                half_life_results, engineering.DEFAULT_EARLY_SEASON_HALF_LIFE_SEASONS
            )

            # Unlike half-life, the difficulty and blend-ratio dimensions can legitimately end up
            # with zero scoreable pairs (no early-gameweek fixture resolved / no ep_next proxy
            # anywhere) even when observations itself is non-empty -- skip that dimension's "best"
            # rather than crash comparing None values.
            scored_difficulty = [r for r in difficulty_results if r.mae is not None]
            best_difficulty = (
                min(
                    scored_difficulty, key=lambda r: r.mae if r.mae is not None else _UNDEFINED_MAE_SORT_VALUE
                )
                if scored_difficulty
                else None
            )
            default_difficulty = next(
                (
                    r
                    for r in difficulty_results
                    if r.coefficient == COLD_START_DIFFICULTY_ADJUSTMENT_COEFFICIENT
                ),
                None,
            )

            scored_blend = [r for r in blend_results if r.mae is not None]
            best_blend = (
                min(scored_blend, key=lambda r: r.mae if r.mae is not None else _UNDEFINED_MAE_SORT_VALUE)
                if scored_blend
                else None
            )
            default_blend = self._match_blend(blend_results, COLD_START_BLEND_CAREER_AVG_WEIGHT)

            # Same "can legitimately be empty" handling as the difficulty/blend dimensions above.
            scored_price_prior = [r for r in price_prior_results if r.mae is not None]
            best_price_prior = (
                min(
                    scored_price_prior,
                    key=lambda r: r.mae if r.mae is not None else _UNDEFINED_MAE_SORT_VALUE,
                )
                if scored_price_prior
                else None
            )
            default_price_prior = self._match_price_prior(
                price_prior_results, COLD_START_PRICE_PRIOR_BLEND_WEIGHT
            )

            # Same "can legitimately be empty" handling -- currently ALWAYS empty (n=0), see
            # _DC_RELIABILITY_CAVEAT above.
            scored_dc_reliability = [r for r in dc_reliability_results if r.mae is not None]
            best_dc_reliability = (
                min(
                    scored_dc_reliability,
                    key=lambda r: r.mae if r.mae is not None else _UNDEFINED_MAE_SORT_VALUE,
                )
                if scored_dc_reliability
                else None
            )
            default_dc_reliability = self._match_dc_reliability(
                dc_reliability_results, _DC_RELIABILITY_PRODUCTION_DEFAULT
            )

            row = ColdStartValidationRun(
                job_id=uuid.UUID(ctx.job_id),
                run_status=ColdStartValidationRunStatus.SUCCEEDED,
                early_season_gameweek_cutoff=engineering.DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF,
                component_evaluated="career_avg_early_season_points_prior_seasons_only",
                usable_seasons_json=usable_seasons,
                seasons_evaluated_json=usable_seasons[1:],
                n_observations=len(observations),
                half_life_sweep_json=[asdict(r) for r in half_life_results],
                default_half_life_seasons=_to_decimal(engineering.DEFAULT_EARLY_SEASON_HALF_LIFE_SEASONS),
                default_half_life_mae=_to_decimal(default_half_life.mae) if default_half_life else None,
                best_half_life_seasons=_to_decimal(best_half_life.half_life_seasons),
                best_half_life_label=best_half_life.label,
                best_half_life_mae=_to_decimal(best_half_life.mae),
                difficulty_coefficient_sweep_json=[asdict(r) for r in difficulty_results],
                default_difficulty_coefficient=_to_decimal(COLD_START_DIFFICULTY_ADJUSTMENT_COEFFICIENT),
                default_difficulty_coefficient_mae=_to_decimal(default_difficulty.mae)
                if default_difficulty
                else None,
                best_difficulty_coefficient=_to_decimal(best_difficulty.coefficient)
                if best_difficulty
                else None,
                best_difficulty_coefficient_mae=_to_decimal(best_difficulty.mae) if best_difficulty else None,
                blend_ratio_sweep_json=[asdict(r) for r in blend_results],
                default_blend_career_avg_weight=_to_decimal(COLD_START_BLEND_CAREER_AVG_WEIGHT),
                default_blend_mae=_to_decimal(default_blend.mae) if default_blend else None,
                best_blend_career_avg_weight=_to_decimal(best_blend.career_avg_weight)
                if best_blend
                else None,
                best_blend_mae=_to_decimal(best_blend.mae) if best_blend else None,
                blend_ratio_caveat=_BLEND_RATIO_CAVEAT,
                n_true_rookie_observations=len(true_rookie_observations),
                price_prior_sweep_json=[asdict(r) for r in price_prior_results] or None,
                default_price_prior_weight=_to_decimal(COLD_START_PRICE_PRIOR_BLEND_WEIGHT),
                default_price_prior_weight_mae=_to_decimal(default_price_prior.mae)
                if default_price_prior
                else None,
                best_price_prior_weight=_to_decimal(best_price_prior.price_prior_weight)
                if best_price_prior
                else None,
                best_price_prior_weight_mae=_to_decimal(best_price_prior.mae) if best_price_prior else None,
                price_prior_caveat=_PRICE_PRIOR_CAVEAT,
                price_comparable_buckets_json=[asdict(b) for b in price_comparable_buckets] or None,
                n_dc_reliability_observations=len(dc_reliability_observations),
                dc_reliability_sweep_json=[asdict(r) for r in dc_reliability_results] or None,
                default_dc_reliability_coefficient=_to_decimal(_DC_RELIABILITY_PRODUCTION_DEFAULT),
                default_dc_reliability_coefficient_mae=_to_decimal(default_dc_reliability.mae)
                if default_dc_reliability
                else None,
                best_dc_reliability_coefficient=_to_decimal(best_dc_reliability.coefficient)
                if best_dc_reliability
                else None,
                best_dc_reliability_coefficient_mae=_to_decimal(best_dc_reliability.mae)
                if best_dc_reliability
                else None,
                dc_reliability_caveat=_DC_RELIABILITY_CAVEAT,
                started_at=started_at,
                completed_at=completed_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)

            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={
                    "cold_start_validation_run_id": str(row.id),
                    "usable_seasons": usable_seasons,
                    "n_observations": len(observations),
                    "half_life_sweep": [asdict(r) for r in half_life_results],
                    "best_half_life_seasons": best_half_life.half_life_seasons,
                    "best_half_life_mae": best_half_life.mae,
                    "difficulty_coefficient_sweep": [asdict(r) for r in difficulty_results],
                    "best_difficulty_coefficient": best_difficulty.coefficient if best_difficulty else None,
                    "best_difficulty_coefficient_mae": best_difficulty.mae if best_difficulty else None,
                    "blend_ratio_sweep": [asdict(r) for r in blend_results],
                    "best_blend_career_avg_weight": best_blend.career_avg_weight if best_blend else None,
                    "best_blend_mae": best_blend.mae if best_blend else None,
                    "blend_ratio_caveat": _BLEND_RATIO_CAVEAT,
                    "n_true_rookie_observations": len(true_rookie_observations),
                    "price_prior_sweep": [asdict(r) for r in price_prior_results],
                    "best_price_prior_weight": best_price_prior.price_prior_weight
                    if best_price_prior
                    else None,
                    "best_price_prior_weight_mae": best_price_prior.mae if best_price_prior else None,
                    "price_prior_caveat": _PRICE_PRIOR_CAVEAT,
                    "n_price_comparable_buckets": len(price_comparable_buckets),
                    "n_dc_reliability_observations": len(dc_reliability_observations),
                    "dc_reliability_sweep": [asdict(r) for r in dc_reliability_results],
                    "best_dc_reliability_coefficient": best_dc_reliability.coefficient
                    if best_dc_reliability
                    else None,
                    "best_dc_reliability_coefficient_mae": best_dc_reliability.mae
                    if best_dc_reliability
                    else None,
                    "dc_reliability_caveat": _DC_RELIABILITY_CAVEAT,
                },
                error_code=None,
                error_message=None,
            )
        except Exception as exc:  # noqa: BLE001 - job row must always reach a terminal status
            session.rollback()
            return JobResult(
                status=JobStatus.FAILED,
                output=None,
                error_code="COLD_START_PROJECTION_VALIDATION_FAILED",
                error_message=str(exc),
            )
        finally:
            session.close()

    @staticmethod
    def _match_half_life(results: list[HalfLifeSweepResult], value: float) -> HalfLifeSweepResult | None:
        return next((r for r in results if r.half_life_seasons == value), None)

    @staticmethod
    def _match_blend(results: list[BlendRatioSweepResult], value: float) -> BlendRatioSweepResult | None:
        return next((r for r in results if r.career_avg_weight == value), None)

    @staticmethod
    def _match_price_prior(
        results: list[PricePriorSweepResult], value: float
    ) -> PricePriorSweepResult | None:
        return next((r for r in results if r.price_prior_weight == value), None)

    @staticmethod
    def _match_dc_reliability(
        results: list[DcReliabilityCoefficientSweepResult], value: float
    ) -> DcReliabilityCoefficientSweepResult | None:
        return next((r for r in results if r.coefficient == value), None)
