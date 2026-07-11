"""The optimization go/no-go gate: decides whether either optimizer job is allowed to run at all.

Both refusal cases are a HARD refuse — no ``optimization_runs`` row is
created at all when the gate blocks (never pollute the audit trail with
runs built on data the system already flagged as untrustworthy).
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.rules.status import get_current_rules_status
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.season import Season
from packages.domain.ingestion import IngestionRunStatus, IngestionRunType
from packages.domain.rules import RulesStatus

_TERMINAL_STATUSES = (
    IngestionRunStatus.SUCCEEDED,
    IngestionRunStatus.SUCCEEDED_WITH_WARNINGS,
    IngestionRunStatus.FAILED,
)


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason_code: str | None  # "RULES_BLOCKED" | "OPTIMIZATION_NOT_PERMITTED" | "NO_INGESTION_DATA"
    rules_status: RulesStatus
    optimization_permitted: bool
    data_quality_score: float | None


def _most_recent_terminal_ingestion_run(session: Session, season_id: uuid.UUID) -> IngestionRun | None:
    season_label = session.execute(select(Season.label).where(Season.id == season_id)).scalar_one_or_none()
    if season_label is None:
        return None

    return session.execute(
        select(IngestionRun)
        .where(
            IngestionRun.run_type == IngestionRunType.DATA_INGESTION,
            IngestionRun.status.in_(_TERMINAL_STATUSES),
            IngestionRun.summary_json["season_label"].astext == season_label,
        )
        .order_by(IngestionRun.completed_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def check_optimization_gate(session: Session, season_id: uuid.UUID) -> GateResult:
    """Decide whether optimization is currently permitted for a season.

    BLOCKED rules status refuses outright. Otherwise, requires a terminal
    DATA_INGESTION run for the season whose ``optimization_permitted`` flag
    is True. DEGRADED rules status never blocks by itself — it only flows
    through as ``rules_status`` for the caller to snapshot.
    """
    rules_status = get_current_rules_status(session, season_id)
    if rules_status == RulesStatus.BLOCKED:
        return GateResult(
            allowed=False,
            reason_code="RULES_BLOCKED",
            rules_status=rules_status,
            optimization_permitted=False,
            data_quality_score=None,
        )

    latest_run = _most_recent_terminal_ingestion_run(session, season_id)
    if latest_run is None:
        return GateResult(
            allowed=False,
            reason_code="NO_INGESTION_DATA",
            rules_status=rules_status,
            optimization_permitted=False,
            data_quality_score=None,
        )

    data_quality_score = (
        float(latest_run.data_quality_score) if latest_run.data_quality_score is not None else None
    )

    if not latest_run.optimization_permitted:
        return GateResult(
            allowed=False,
            reason_code="OPTIMIZATION_NOT_PERMITTED",
            rules_status=rules_status,
            optimization_permitted=False,
            data_quality_score=data_quality_score,
        )

    return GateResult(
        allowed=True,
        reason_code=None,
        rules_status=rules_status,
        optimization_permitted=True,
        data_quality_score=data_quality_score,
    )
