"""Integration tests for app.optimization.gate.check_optimization_gate, exercised through
the SQUAD_OPTIMIZATION and LINEUP_OPTIMIZATION job handlers.

Both refusal paths are a hard refuse: no ``optimization_runs`` row is ever
created for a gated invocation.
"""

import uuid

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.rules_config import RulesConfig
from packages.domain.ingestion import IngestionRunType
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.rules import RulesStatus


def _optimization_run_count(db_session) -> int:
    return len(db_session.execute(select(OptimizationRun.id)).scalars().all())


def test_gate_refuses_both_jobs_when_rules_blocked(db_session, patched_fpl_client_optimization):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED

    # A legitimate SUCCEEDED squad run exists before the rules config goes
    # bad, so the LINEUP_OPTIMIZATION half of this test has a valid
    # `source_optimization_run_id` to reference (parsing that input happens
    # before the gate check in the handler).
    squad_job = execute_job(db_session, job_type=JobType.SQUAD_OPTIMIZATION, trigger_type=TriggerType.LOCAL)
    assert squad_job.status == JobStatus.SUCCEEDED
    prior_squad_run_id = squad_job.output_json["optimization_run_id"]

    # Corrupt the bootstrap payload so a squad-legality-critical rules field
    # ("scoring.assists", one of compute_rules_status's _CORE_REQUIRED_PATHS)
    # goes missing, then re-ingest — this drives the real BLOCKED-detection
    # logic in app.rules.status.compute_rules_status (not a direct DB poke).
    del patched_fpl_client_optimization["bootstrap"]["game_config"]["scoring"]["assists"]

    reingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    # The re-ingestion job itself may succeed or succeed-with-warnings at the
    # ingestion-run level — what matters is the rules_config it produced.
    assert reingestion_job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED)

    current_rules_config = (
        db_session.execute(select(RulesConfig).where(RulesConfig.rules_status == RulesStatus.BLOCKED))
        .scalars()
        .first()
    )
    assert current_rules_config is not None, "expected the missing scoring.assists field to trigger BLOCKED"

    before = _optimization_run_count(db_session)

    squad_result = execute_job(
        db_session, job_type=JobType.SQUAD_OPTIMIZATION, trigger_type=TriggerType.LOCAL
    )
    assert squad_result.status == JobStatus.FAILED
    assert squad_result.error_code == "RULES_BLOCKED"
    assert _optimization_run_count(db_session) == before

    lineup_result = execute_job(
        db_session,
        job_type=JobType.LINEUP_OPTIMIZATION,
        parameters={"source_optimization_run_id": prior_squad_run_id},
        trigger_type=TriggerType.LOCAL,
    )
    assert lineup_result.status == JobStatus.FAILED
    assert lineup_result.error_code == "RULES_BLOCKED"
    assert _optimization_run_count(db_session) == before


def test_gate_refuses_both_jobs_when_optimization_not_permitted(db_session, patched_fpl_client_optimization):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    ingestion_run_id = uuid.UUID(ingestion_job.output_json["ingestion_run_id"])

    # Force the most recent terminal DATA_INGESTION run's optimization_permitted
    # flag to False directly — a realistic stand-in for "ingestion succeeded
    # but a data-quality issue (or low score) vetoed optimization downstream",
    # without needing to hand-craft a specific critical/error-severity issue.
    ingestion_run = db_session.get(IngestionRun, ingestion_run_id)
    assert ingestion_run.run_type == IngestionRunType.DATA_INGESTION
    ingestion_run.optimization_permitted = False
    db_session.commit()

    before = _optimization_run_count(db_session)

    squad_result = execute_job(
        db_session, job_type=JobType.SQUAD_OPTIMIZATION, trigger_type=TriggerType.LOCAL
    )
    assert squad_result.status == JobStatus.FAILED
    assert squad_result.error_code == "OPTIMIZATION_NOT_PERMITTED"
    assert _optimization_run_count(db_session) == before

    # player_ids mode never needs to resolve/exist in the DB for parsing to
    # succeed (see app.jobs.handlers.lineup_optimization._parse_lineup_input)
    # — the gate check runs, and refuses, before any candidate is ever built.
    fifteen_uuids = [str(uuid.uuid4()) for _ in range(15)]
    lineup_result = execute_job(
        db_session,
        job_type=JobType.LINEUP_OPTIMIZATION,
        parameters={"player_ids": fifteen_uuids},
        trigger_type=TriggerType.LOCAL,
    )
    assert lineup_result.status == JobStatus.FAILED
    assert lineup_result.error_code == "OPTIMIZATION_NOT_PERMITTED"
    assert _optimization_run_count(db_session) == before
