"""Integration tests for the RULES_SYNC job end-to-end through execute_job.

Requires a running Postgres with migrations applied (see conftest.py).
FPLClient is monkeypatched (see `patched_fpl_client` in conftest.py) so no
real network access happens here.
"""

from sqlalchemy import select

from app.jobs.service import execute_job
from packages.database.models.rules_config import RulesConfig
from packages.domain.jobs import JobStatus, JobType, TriggerType


def test_rules_sync_rerun_with_identical_payload_bumps_confirmation_not_version(
    db_session, patched_fpl_client
):
    first = execute_job(db_session, job_type=JobType.RULES_SYNC, trigger_type=TriggerType.LOCAL)
    assert first.status == JobStatus.SUCCEEDED
    assert first.output_json["changed"] is True
    first_version = first.output_json["version_number"]
    first_config_id = first.output_json["rules_config_id"]

    second = execute_job(db_session, job_type=JobType.RULES_SYNC, trigger_type=TriggerType.LOCAL)
    assert second.status == JobStatus.SUCCEEDED
    assert second.output_json["changed"] is False
    assert second.output_json["version_number"] == first_version
    assert second.output_json["rules_config_id"] == first_config_id
    assert second.output_json["report_markdown"] is None

    rows = db_session.execute(select(RulesConfig).where(RulesConfig.id == first_config_id)).scalars().all()
    assert len(rows) == 1
    assert rows[0].version_number == first_version


def test_rules_sync_rerun_with_changed_rule_creates_new_version_with_diff(db_session, patched_fpl_client):
    first = execute_job(db_session, job_type=JobType.RULES_SYNC, trigger_type=TriggerType.LOCAL)
    first_version = first.output_json["version_number"]

    patched_fpl_client["bootstrap"]["game_config"]["rules"]["squad_team_limit"] = (
        patched_fpl_client["bootstrap"]["game_config"]["rules"]["squad_team_limit"] + 1
    )

    second = execute_job(db_session, job_type=JobType.RULES_SYNC, trigger_type=TriggerType.LOCAL)
    assert second.status == JobStatus.SUCCEEDED
    assert second.output_json["changed"] is True
    assert second.output_json["version_number"] == first_version + 1
    assert second.output_json["report_markdown"] is not None
    assert "squad_team_limit" in second.output_json["report_markdown"]

    current_rows = (
        db_session.execute(
            select(RulesConfig).where(
                RulesConfig.is_current.is_(True), RulesConfig.id == second.output_json["rules_config_id"]
            )
        )
        .scalars()
        .all()
    )
    assert len(current_rows) == 1

    old_row = db_session.execute(
        select(RulesConfig).where(
            RulesConfig.version_number == first_version, RulesConfig.season_id == current_rows[0].season_id
        )
    ).scalar_one()
    assert old_row.is_current is False
    assert old_row.superseded_at is not None
