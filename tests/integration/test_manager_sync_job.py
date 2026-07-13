"""Integration tests for the MANAGER_SYNC job against a real database.

Seeds current-season players via DATA_INGESTION (reusing the ~60-player
optimization pool fixture, see ``build_optimization_bootstrap_json`` in
``conftest.py``), then runs MANAGER_SYNC against the committed manager
fixture files (``tests/fixtures/fpl_api/manager/``), which deliberately
reference one squad element id (99999) that doesn't exist in that pool, to
exercise the unresolved-player path end to end.
"""

import uuid

from app.jobs.service import execute_job
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot
from packages.domain.jobs import JobStatus, JobType, TriggerType

FPL_MANAGER_ID = 1


def _seed_players(db_session):
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return ingestion_job


def _run_manager_sync(db_session):
    return execute_job(
        db_session,
        job_type=JobType.MANAGER_SYNC,
        parameters={"fpl_manager_id": FPL_MANAGER_ID},
        requested_by="integration-test",
        trigger_type=TriggerType.LOCAL,
    )


def test_manager_sync_happy_path_with_unresolved_player(
    db_session, patched_fpl_client_optimization, patched_fpl_client_manager
):
    _seed_players(db_session)

    job = _run_manager_sync(db_session)

    assert job.status == JobStatus.SUCCEEDED
    assert job.output_json["has_unresolved_players"] is True
    assert job.output_json["unresolved_fpl_element_ids"] == [99999]

    snapshot_id = uuid.UUID(job.output_json["manager_squad_snapshot_id"])
    snapshot = db_session.get(ManagerSquadSnapshot, snapshot_id)
    assert snapshot is not None
    assert snapshot.fpl_manager_id == FPL_MANAGER_ID
    assert snapshot.is_current is True
    assert snapshot.has_unresolved_players is True
    assert snapshot.unresolved_fpl_element_ids_json == [99999]
    assert len(snapshot.squad_player_ids) == 14
    # bank comes from the picks endpoint's entry_history, not history()'s per-gameweek rows.
    assert snapshot.bank == 25
    assert snapshot.free_transfers_available >= 0
    assert snapshot.squad_player_purchase_prices_json
    assert snapshot.squad_player_selling_prices_json
    assert len(snapshot.squad_player_purchase_prices_json) == 14
    assert len(snapshot.squad_player_selling_prices_json) == 14
    # Every selling price should be a plausible tenths-of-GBP-millions figure.
    for price in snapshot.squad_player_selling_prices_json.values():
        assert 0 < price < 200


def test_manager_sync_rerun_supersedes_prior_current_snapshot(
    db_session, patched_fpl_client_optimization, patched_fpl_client_manager
):
    _seed_players(db_session)

    first_job = _run_manager_sync(db_session)
    assert first_job.status == JobStatus.SUCCEEDED
    first_snapshot_id = uuid.UUID(first_job.output_json["manager_squad_snapshot_id"])

    second_job = _run_manager_sync(db_session)
    assert second_job.status == JobStatus.SUCCEEDED
    second_snapshot_id = uuid.UUID(second_job.output_json["manager_squad_snapshot_id"])

    assert first_snapshot_id != second_snapshot_id

    db_session.expire_all()
    first_snapshot = db_session.get(ManagerSquadSnapshot, first_snapshot_id)
    second_snapshot = db_session.get(ManagerSquadSnapshot, second_snapshot_id)

    assert first_snapshot.is_current is False
    assert first_snapshot.superseded_at is not None
    assert second_snapshot.is_current is True

    current_rows = (
        db_session.query(ManagerSquadSnapshot)
        .filter(
            ManagerSquadSnapshot.fpl_manager_id == FPL_MANAGER_ID,
            ManagerSquadSnapshot.is_current.is_(True),
        )
        .all()
    )
    assert len(current_rows) == 1
    assert current_rows[0].id == second_snapshot_id


def test_manager_sync_missing_manager_id_fails(db_session, patched_fpl_client_optimization):
    _seed_players(db_session)

    job = execute_job(
        db_session,
        job_type=JobType.MANAGER_SYNC,
        parameters={},
        trigger_type=TriggerType.LOCAL,
    )

    assert job.status == JobStatus.FAILED
    assert job.error_code == "MISSING_MANAGER_ID"
