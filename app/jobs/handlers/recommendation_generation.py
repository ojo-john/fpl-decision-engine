"""RECOMMENDATION_GENERATION job: correlate the 5 recommendation-producing jobs' most recent
SUCCEEDED runs for one target gameweek into a single ``recommendation_audits`` row.

Milestone 7 Layer 1 (Recursive Learning Loop: Recommendation Correlation/
Audit). This is deliberately the smallest complete vertical slice, NOT an
orchestrator: it never runs or triggers SQUAD_OPTIMIZATION/LINEUP_OPTIMIZATION/
TRANSFER_OPTIMIZATION/H2H_SIMULATION/CHIP_SIMULATION itself — it only looks up
each one's own already-persisted most recent SUCCEEDED run for the resolved
target gameweek and records the correlation, plus generation-time context
(champion model, FPL API health) and a mechanically-derived confidence/
assumptions/known-risks summary. See ``packages.database.models.recommendation_audit``
for the full column rationale.

Season/manager resolution: an explicit ``parameters.manager_squad_snapshot_id``
scopes ``season_id`` to that snapshot's own season AND additionally scopes the
3 manager-specific component lookups (transfer/H2H/chip — each has its own
``manager_squad_snapshot_id`` FK) to that exact snapshot, mirroring how
``H2HSimulationJobRunner``/``ChipSimulationJobRunner`` resolve season from a
supplied snapshot. Without one, ``season_id`` resolves via ``Season.is_current``
(mirrors ``SquadOptimizationJobRunner``'s own ``_resolve_current_season_id``)
and every component lookup is unscoped by manager (there is at most one real
manager configured in this system today; a future multi-manager Layer would
need to decide how to pick "the" manager here — out of scope for Layer 1).
``optimization_runs`` (SQUAD_OPTIMIZATION/LINEUP_OPTIMIZATION) is never
manager-scoped at all — that table has no ``manager_squad_snapshot_id`` column
(squad/lineup optimization is season-wide, see that model's own docstring), so
its own lookup is always by ``target_gameweek_id`` alone.

Target gameweek resolution: an explicit ``parameters.target_fpl_event_id``
always wins (a caller re-generating a summary for a specific, possibly past,
gameweek). Otherwise resolves the next unfinished gameweek via
``resolve_target_gameweek`` and HARD REFUSES (``NO_ACTIVE_GAMEWEEK``, no row
created) if none exists — mirrors ``TRANSFER_OPTIMIZATION``/``CHIP_SIMULATION``'s
off-season precedent (not ``H2H_SIMULATION``'s softer fallback), since a
recommendation summary for a gameweek that doesn't exist has nothing left to
aggregate.

``optimization_run_id`` covers BOTH SQUAD_OPTIMIZATION and LINEUP_OPTIMIZATION
(both write to ``optimization_runs``, distinguished by ``recommendation_type``)
— this handler resolves whichever is more RECENT for the target gameweek (by
``completed_at``), never both; see that model's own docstring for the full
rationale.

RUNNING-row discipline: unlike the multi-step optimizer jobs (which create an
early RUNNING row so a mid-run crash still leaves a terminal FAILED row behind
for their own child rows to hang off of), this handler follows
``ManagerSyncJobRunner``'s "build fully in memory, insert once" precedent
instead — every step here is a handful of read-only SELECTs plus one final
INSERT, with no child-row hierarchy that needs an early anchor id. A crash at
any point before the final commit leaves NO ``recommendation_audits`` row at
all (never a dangling RUNNING one) — see ``RecommendationGenerationRunStatus``'s
own docstring for why its ``RUNNING``/``FAILED`` members are consequently never
actually persisted anywhere.

This job also never calls ``app.optimization.gate.check_optimization_gate`` —
it performs no optimization or projection of its own, only reads already-gated
child jobs' own persisted results, so the gate's go/no-go snapshot doesn't
apply to a read-only correlation step.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.jobs.registry import register_handler
from app.jobs.runner import JobContext, JobResult, JobRunner
from app.optimization.gameweek_resolution import resolve_target_gameweek
from packages.database.models.chip_optimization_run import ChipOptimizationRun
from packages.database.models.gameweek import Gameweek
from packages.database.models.h2h_simulation_run import H2HSimulationRun
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.database.models.model_version import ModelVersion
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.season import Season
from packages.database.models.transfer_optimization_run import TransferOptimizationRun
from packages.database.session import SessionLocal
from packages.domain.chips import ChipOptimizationRunStatus
from packages.domain.h2h import H2HSimulationRunStatus
from packages.domain.ingestion import EndpointHealth, IngestionRunStatus, IngestionRunType
from packages.domain.jobs import JobStatus, JobType
from packages.domain.learning import RecommendationConfidence
from packages.domain.optimization import OptimizationRunStatus, TargetGameweekStatus
from packages.domain.transfers import TransferOptimizationRunStatus

# --- Confidence heuristic (see module docstring's "RUNNING-row discipline" section for the ---
# --- overall design; these are the exact, named-constant thresholds compute_confidence uses) ---

_TOTAL_COMPONENTS = 4
# LOW whenever at most this many of the 4 components resolved -- a summary correlating one (or
# zero) real component runs is not meaningfully trustworthy regardless of API health.
_CONFIDENCE_LOW_MAX_RESOLVED_COMPONENTS = 1
# HIGH requires ALL 4 components resolved, HEALTHY FPL API, AND a data quality score at or above
# this threshold -- any one of those three missing/failing caps confidence at MEDIUM.
_CONFIDENCE_HIGH_MIN_DATA_QUALITY_SCORE = Decimal("80")


class _InputResolutionError(Exception):
    """Raised for a clear, user-facing input problem, always caught before any row is created."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def _parse_uuid_parameter(parameters: dict | None, key: str) -> uuid.UUID:
    raw = (parameters or {}).get(key)
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        raise _InputResolutionError(
            f"INVALID_{key.upper()}", f"parameters.{key} is not a valid UUID."
        ) from None


def compute_confidence(
    resolved_component_count: int,
    fpl_api_health: EndpointHealth | None,
    data_quality_score: Decimal | None,
) -> tuple[RecommendationConfidence, list[str]]:
    """Mechanically derive a ``RecommendationConfidence`` from generation-time context.

    Pure function (no DB access) so it's directly unit-testable. Returns
    ``(confidence, drivers)`` where ``drivers`` is a short, structured list of
    the exact facts that decided this outcome -- persisted verbatim as
    ``confidence_drivers_json``.

    - LOW: at most ``_CONFIDENCE_LOW_MAX_RESOLVED_COMPONENTS`` of the 4
      components resolved, OR the FPL API was not HEALTHY at generation time.
    - HIGH: all 4 components resolved AND HEALTHY AND a data quality score at
      or above ``_CONFIDENCE_HIGH_MIN_DATA_QUALITY_SCORE``.
    - MEDIUM: everything else (a real, usable summary with some gap short of
      the HIGH bar -- e.g. 3/4 components, or a missing/borderline data
      quality score despite healthy API and full component coverage).
    """
    drivers = [
        f"resolved_components={resolved_component_count}/{_TOTAL_COMPONENTS}",
        f"fpl_api_health={fpl_api_health.value if fpl_api_health is not None else 'unknown'}",
        f"data_quality_score={data_quality_score if data_quality_score is not None else 'unknown'}",
    ]

    if (
        fpl_api_health != EndpointHealth.HEALTHY
        or resolved_component_count <= _CONFIDENCE_LOW_MAX_RESOLVED_COMPONENTS
    ):
        return RecommendationConfidence.LOW, drivers

    if (
        resolved_component_count == _TOTAL_COMPONENTS
        and data_quality_score is not None
        and data_quality_score >= _CONFIDENCE_HIGH_MIN_DATA_QUALITY_SCORE
    ):
        return RecommendationConfidence.HIGH, drivers

    return RecommendationConfidence.MEDIUM, drivers


# --- Assumptions / known risks: fixed, TEMPLATED strings -- never free-text generation ---------

_ASSUMPTIONS = [
    "This summary correlates each component's most recently SUCCEEDED run as of generation time; "
    "it does not itself re-run any optimizer, simulation, or chip evaluation.",
    "Squad/lineup optimization is season-wide, not manager-scoped; transfer optimization, H2H "
    "simulation, and chip timing are scoped to the manager squad snapshot supplied at generation "
    "time, if any.",
]

_COMPONENT_LABELS: dict[str, str] = {
    "optimization_run_id": "Squad/lineup optimization (SQUAD_OPTIMIZATION/LINEUP_OPTIMIZATION)",
    "transfer_optimization_run_id": "Transfer optimization (TRANSFER_OPTIMIZATION)",
    "h2h_simulation_run_id": "H2H simulation (H2H_SIMULATION)",
    "chip_optimization_run_id": "Chip timing analysis (CHIP_SIMULATION)",
}


def compute_known_risks(
    resolved_components: dict[str, uuid.UUID | None],
    target_fpl_event_id: int,
    fpl_api_health: EndpointHealth | None,
    champion_model_version_id: uuid.UUID | None,
) -> list[str]:
    """Mechanically derive ``known_risks_json`` from which fields are null/notable.

    Pure function (no DB access) -- a fixed, enumerable set of short
    structured strings, one per genuinely missing/degraded signal, never
    free-text generation. Order is deterministic: the 4 components (in
    ``_COMPONENT_LABELS`` order), then API health, then champion model.
    """
    risks: list[str] = []
    for field, label in _COMPONENT_LABELS.items():
        if resolved_components[field] is None:
            risks.append(
                f"{label} has not produced a SUCCEEDED run for GW{target_fpl_event_id}; that "
                "recommendation is not reflected in this summary."
            )
    if fpl_api_health is not None and fpl_api_health != EndpointHealth.HEALTHY:
        risks.append(
            f"FPL API was {fpl_api_health.value.upper()} (not HEALTHY) at generation time; "
            "underlying source data may be incomplete or stale."
        )
    if champion_model_version_id is None:
        risks.append(
            "No champion model is currently designated; projections underlying these "
            "recommendations may rely on an untrained baseline."
        )
    return risks


# --- Component resolution queries -- each is "most recent SUCCEEDED run for this target ---------
# --- gameweek", scoped by manager_squad_snapshot_id only where that FK actually exists ----------


def _latest_succeeded_optimization_run_id(
    session: Session, target_gameweek_id: uuid.UUID
) -> uuid.UUID | None:
    """Most recent SUCCEEDED SQUAD_OPTIMIZATION/LINEUP_OPTIMIZATION run for this gameweek.

    Never manager-scoped -- ``optimization_runs`` has no ``manager_squad_snapshot_id`` column (see
    module docstring). Covers both ``recommendation_type``s; whichever is more recent wins.
    """
    return session.execute(
        select(OptimizationRun.id)
        .where(
            OptimizationRun.target_gameweek_id == target_gameweek_id,
            OptimizationRun.run_status == OptimizationRunStatus.SUCCEEDED,
        )
        .order_by(OptimizationRun.completed_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _latest_succeeded_transfer_optimization_run_id(
    session: Session, target_gameweek_id: uuid.UUID, manager_squad_snapshot_id: uuid.UUID | None
) -> uuid.UUID | None:
    stmt = select(TransferOptimizationRun.id).where(
        TransferOptimizationRun.horizon_start_gameweek_id == target_gameweek_id,
        TransferOptimizationRun.run_status == TransferOptimizationRunStatus.SUCCEEDED,
    )
    if manager_squad_snapshot_id is not None:
        stmt = stmt.where(TransferOptimizationRun.manager_squad_snapshot_id == manager_squad_snapshot_id)
    return session.execute(
        stmt.order_by(TransferOptimizationRun.completed_at.desc()).limit(1)
    ).scalar_one_or_none()


def _latest_succeeded_h2h_simulation_run_id(
    session: Session, target_fpl_event_id: int, manager_squad_snapshot_id: uuid.UUID | None
) -> uuid.UUID | None:
    stmt = select(H2HSimulationRun.id).where(
        H2HSimulationRun.target_fpl_event_id == target_fpl_event_id,
        H2HSimulationRun.run_status == H2HSimulationRunStatus.SUCCEEDED,
    )
    if manager_squad_snapshot_id is not None:
        stmt = stmt.where(H2HSimulationRun.manager_squad_snapshot_id == manager_squad_snapshot_id)
    return session.execute(stmt.order_by(H2HSimulationRun.completed_at.desc()).limit(1)).scalar_one_or_none()


def _latest_succeeded_chip_optimization_run_id(
    session: Session, target_fpl_event_id: int, manager_squad_snapshot_id: uuid.UUID | None
) -> uuid.UUID | None:
    stmt = select(ChipOptimizationRun.id).where(
        ChipOptimizationRun.target_fpl_event_id == target_fpl_event_id,
        ChipOptimizationRun.run_status == ChipOptimizationRunStatus.SUCCEEDED,
    )
    if manager_squad_snapshot_id is not None:
        stmt = stmt.where(ChipOptimizationRun.manager_squad_snapshot_id == manager_squad_snapshot_id)
    return session.execute(
        stmt.order_by(ChipOptimizationRun.completed_at.desc()).limit(1)
    ).scalar_one_or_none()


def _current_champion_model_version_id(session: Session) -> uuid.UUID | None:
    """A direct, simple ``is_champion IS TRUE`` read -- no need for the full ``champion_loader``
    live-prediction-capable-champion resolution machinery here, this is a read-only reference.
    """
    return session.execute(
        select(ModelVersion.id)
        .where(ModelVersion.is_champion.is_(True))
        .order_by(ModelVersion.trained_at.desc())
        .limit(1)
    ).scalar_one_or_none()


# Mirrors app.optimization.gate._most_recent_terminal_ingestion_run's exact season-scoping
# technique (duplicated, not imported -- that helper is private to its own module, same
# duplication precedent as e.g. app.jobs.handlers.chip_simulation's _split_starting_and_bench).
_INGESTION_TERMINAL_STATUSES = (
    IngestionRunStatus.SUCCEEDED,
    IngestionRunStatus.SUCCEEDED_WITH_WARNINGS,
    IngestionRunStatus.FAILED,
)


def _most_recent_terminal_ingestion_run(session: Session, season_id: uuid.UUID) -> IngestionRun | None:
    season_label = session.execute(select(Season.label).where(Season.id == season_id)).scalar_one_or_none()
    if season_label is None:
        return None
    return session.execute(
        select(IngestionRun)
        .where(
            IngestionRun.run_type == IngestionRunType.DATA_INGESTION,
            IngestionRun.status.in_(_INGESTION_TERMINAL_STATUSES),
            IngestionRun.summary_json["season_label"].astext == season_label,
        )
        .order_by(IngestionRun.completed_at.desc())
        .limit(1)
    ).scalar_one_or_none()


@dataclass(frozen=True)
class _TargetGameweek:
    gameweek_id: uuid.UUID
    fpl_event_id: int


def _resolve_target_gameweek_for_recommendation(
    session: Session, season_id: uuid.UUID, explicit_event_id: int | None
) -> _TargetGameweek:
    """An explicit ``parameters.target_fpl_event_id`` always wins. Otherwise HARD REFUSES
    (``NO_ACTIVE_GAMEWEEK``) off-season -- see module docstring for why this mirrors
    TRANSFER_OPTIMIZATION/CHIP_SIMULATION, not H2H_SIMULATION's softer fallback.
    """
    if explicit_event_id is not None:
        gameweek_id = session.execute(
            select(Gameweek.id).where(
                Gameweek.season_id == season_id, Gameweek.fpl_event_id == explicit_event_id
            )
        ).scalar_one_or_none()
        if gameweek_id is None:
            raise _InputResolutionError(
                "GAMEWEEK_NOT_FOUND", f"No gameweek found for fpl_event_id={explicit_event_id}."
            )
        return _TargetGameweek(gameweek_id=gameweek_id, fpl_event_id=explicit_event_id)

    gameweek_id, status = resolve_target_gameweek(session, season_id)
    if status == TargetGameweekStatus.NO_ACTIVE_GAMEWEEK:
        raise _InputResolutionError(
            "NO_ACTIVE_GAMEWEEK",
            "No unfinished gameweek exists to generate a recommendation summary for (off-season).",
        )
    assert gameweek_id is not None
    gameweek = session.get(Gameweek, gameweek_id)
    assert gameweek is not None
    return _TargetGameweek(gameweek_id=gameweek_id, fpl_event_id=gameweek.fpl_event_id)


@register_handler(JobType.RECOMMENDATION_GENERATION)
class RecommendationGenerationJobRunner(JobRunner):
    """Correlates the most recent SUCCEEDED run of each recommendation-producing job type, for one
    target gameweek, into a single ``recommendation_audits`` row. A pure aggregator -- see module
    docstring.
    """

    def run(self, ctx: JobContext) -> JobResult:
        session = SessionLocal()
        try:
            try:
                raw_snapshot_id = (ctx.parameters or {}).get("manager_squad_snapshot_id")
                manager_squad_snapshot_id: uuid.UUID | None = None
                season_id: uuid.UUID | None
                if raw_snapshot_id is not None:
                    manager_squad_snapshot_id = _parse_uuid_parameter(
                        ctx.parameters, "manager_squad_snapshot_id"
                    )
                    snapshot = session.get(ManagerSquadSnapshot, manager_squad_snapshot_id)
                    if snapshot is None:
                        raise _InputResolutionError(
                            "SNAPSHOT_NOT_FOUND",
                            f"manager_squad_snapshots row {manager_squad_snapshot_id} not found.",
                        )
                    season_id = snapshot.season_id
                else:
                    season_id = session.execute(
                        select(Season.id).where(Season.is_current.is_(True))
                    ).scalar_one_or_none()
                    if season_id is None:
                        raise _InputResolutionError("NO_CURRENT_SEASON", "No current season found.")

                raw_target_event = (ctx.parameters or {}).get("target_fpl_event_id")
                explicit_event_id = int(raw_target_event) if raw_target_event is not None else None
                target = _resolve_target_gameweek_for_recommendation(session, season_id, explicit_event_id)
            except _InputResolutionError as exc:
                return JobResult(
                    status=JobStatus.FAILED, output=None, error_code=exc.error_code, error_message=str(exc)
                )

            resolved_components: dict[str, uuid.UUID | None] = {
                "optimization_run_id": _latest_succeeded_optimization_run_id(session, target.gameweek_id),
                "transfer_optimization_run_id": _latest_succeeded_transfer_optimization_run_id(
                    session, target.gameweek_id, manager_squad_snapshot_id
                ),
                "h2h_simulation_run_id": _latest_succeeded_h2h_simulation_run_id(
                    session, target.fpl_event_id, manager_squad_snapshot_id
                ),
                "chip_optimization_run_id": _latest_succeeded_chip_optimization_run_id(
                    session, target.fpl_event_id, manager_squad_snapshot_id
                ),
            }
            resolved_component_count = sum(1 for v in resolved_components.values() if v is not None)

            champion_model_version_id = _current_champion_model_version_id(session)

            latest_ingestion_run = _most_recent_terminal_ingestion_run(session, season_id)
            fpl_api_health_at_generation = (
                latest_ingestion_run.overall_health if latest_ingestion_run is not None else None
            )
            data_quality_score = (
                latest_ingestion_run.data_quality_score if latest_ingestion_run is not None else None
            )

            confidence, confidence_drivers = compute_confidence(
                resolved_component_count, fpl_api_health_at_generation, data_quality_score
            )
            known_risks = compute_known_risks(
                resolved_components,
                target.fpl_event_id,
                fpl_api_health_at_generation,
                champion_model_version_id,
            )

            now = datetime.now(UTC)

            # Supersede any prior authoritative row for this exact (season, gameweek) -- mirrors
            # ManagerSquadSnapshot's own is_current supersession technique exactly (UPDATE, not
            # delete; see that model's own docstring).
            session.execute(
                update(RecommendationAudit)
                .where(
                    RecommendationAudit.season_id == season_id,
                    RecommendationAudit.target_gameweek_id == target.gameweek_id,
                    RecommendationAudit.is_authoritative.is_(True),
                )
                .values(is_authoritative=False)
            )

            audit = RecommendationAudit(
                job_id=uuid.UUID(ctx.job_id),
                season_id=season_id,
                target_gameweek_id=target.gameweek_id,
                target_fpl_event_id=target.fpl_event_id,
                is_authoritative=True,
                optimization_run_id=resolved_components["optimization_run_id"],
                transfer_optimization_run_id=resolved_components["transfer_optimization_run_id"],
                h2h_simulation_run_id=resolved_components["h2h_simulation_run_id"],
                chip_optimization_run_id=resolved_components["chip_optimization_run_id"],
                champion_model_version_id=champion_model_version_id,
                fpl_api_health_at_generation=fpl_api_health_at_generation,
                # application_version: left NULL, mirroring jobs.application_version's own
                # currently-unwired state elsewhere in this codebase (no real version source
                # exists yet to populate either column from).
                application_version=None,
                confidence=confidence,
                confidence_drivers_json=confidence_drivers,
                assumptions_json=list(_ASSUMPTIONS),
                known_risks_json=known_risks,
                user_actual_decision_json=None,
                generated_at=now,
            )
            session.add(audit)
            session.commit()
            session.refresh(audit)

            return JobResult(
                status=JobStatus.SUCCEEDED,
                output={
                    "recommendation_audit_id": str(audit.id),
                    "target_fpl_event_id": target.fpl_event_id,
                    "confidence": confidence.value,
                    "resolved_component_count": resolved_component_count,
                },
                error_code=None,
                error_message=None,
            )
        except Exception as exc:  # noqa: BLE001 - job row must always reach a terminal status
            session.rollback()
            return JobResult(
                status=JobStatus.FAILED,
                output=None,
                error_code="RECOMMENDATION_GENERATION_FAILED",
                error_message=str(exc),
            )
        finally:
            session.close()
