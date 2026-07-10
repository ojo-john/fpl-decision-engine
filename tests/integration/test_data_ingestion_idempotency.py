"""Integration test: DATA_INGESTION is idempotent at the normalized-row level,
but NOT idempotency-key-deduped at the ingestion_runs level.

Unlike SYSTEM_HEALTH (which uses `execute_job`'s idempotency_key to avoid a
second `jobs` row for "the same" run), every DATA_INGESTION run should always
produce a genuinely new `ingestion_runs` row — it's an audit trail of every
ingestion attempt, not a cache of "the current state". Normalized entity
tables (teams/positions/players/gameweeks/fixtures), on the other hand, are
upserted, so re-running with identical upstream data changes nothing there.

Requires a running Postgres with migrations applied (see conftest.py).
"""

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.fixture import Fixture
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.player import Player
from packages.database.models.season import Season
from packages.domain.ingestion import IngestionRunType
from packages.domain.jobs import JobStatus, JobType, TriggerType


def test_rerun_with_identical_data_keeps_row_counts_but_adds_new_ingestion_run(
    db_session, patched_fpl_client
):
    first = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert first.status == JobStatus.SUCCEEDED
    season_label = first.output_json["season_label"]
    season_id = db_session.execute(select(Season.id).where(Season.label == season_label)).scalar_one()

    players_after_first = (
        db_session.execute(select(Player).where(Player.season_id == season_id)).scalars().all()
    )
    fixtures_after_first = (
        db_session.execute(select(Fixture).where(Fixture.season_id == season_id)).scalars().all()
    )

    second = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert second.status == JobStatus.SUCCEEDED
    assert second.output_json["season_label"] == season_label

    players_after_second = (
        db_session.execute(select(Player).where(Player.season_id == season_id)).scalars().all()
    )
    fixtures_after_second = (
        db_session.execute(select(Fixture).where(Fixture.season_id == season_id)).scalars().all()
    )

    assert len(players_after_second) == len(players_after_first)
    assert len(fixtures_after_second) == len(fixtures_after_first)
    assert {p.id for p in players_after_second} == {p.id for p in players_after_first}
    assert {f.id for f in fixtures_after_second} == {f.id for f in fixtures_after_first}

    ingestion_runs = (
        db_session.execute(
            select(IngestionRun).where(IngestionRun.run_type == IngestionRunType.DATA_INGESTION)
        )
        .scalars()
        .all()
    )
    run_ids = {str(r.id) for r in ingestion_runs}
    assert first.output_json["ingestion_run_id"] in run_ids
    assert second.output_json["ingestion_run_id"] in run_ids
    assert first.output_json["ingestion_run_id"] != second.output_json["ingestion_run_id"]
