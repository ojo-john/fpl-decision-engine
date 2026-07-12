"""Orchestrates one HISTORICAL_DATA_INGESTION run for a single season: fetch, normalize, check, report.

Mirrors ``app.ingestion.service.run_ingestion``'s shape closely (fetch,
persist raw snapshots, normalize in FK order, quality-check, score, render
report, finalize the ``ingestion_runs`` row), but sources data from
``HistoricalFPLClient`` (4 CSV files per season) instead of the live FPL
API, and has no rules-sync step at all — there is no per-season rules
snapshot in this historical dataset.

Enforced ordering: ``season`` -> ``positions``, ``teams`` -> ``players`` ->
``gameweeks`` -> ``fixtures`` -> ``player_gameweek_stats``. Players need
resolved team/position foreign keys; fixtures need resolved team/gameweek
foreign keys; player_gameweek_stats needs resolved player/gameweek/fixture/
team foreign keys.
"""

import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_quality.scoring import compute_optimization_permitted, compute_score
from app.ingestion.historical_client import HistoricalFPLClient
from app.ingestion.historical_normalization import (
    NormalizationCounts,
    upsert_historical_fixtures,
    upsert_historical_gameweeks,
    upsert_historical_players,
    upsert_historical_positions,
    upsert_historical_season,
    upsert_historical_teams,
    upsert_player_gameweek_stats,
)
from app.ingestion.report import render_historical_ingestion_report_markdown
from app.ingestion.service import _combine_health, persist_raw_snapshot
from packages.database.models.data_quality_issue import DataQualityIssue as DataQualityIssueRow
from packages.database.models.fixture import Fixture
from packages.database.models.gameweek import Gameweek
from packages.database.models.ingestion_run import IngestionRun
from packages.database.models.player import Player
from packages.database.models.position import Position
from packages.database.models.raw_api_snapshot import RawApiSnapshot
from packages.database.models.team import Team
from packages.domain.ingestion import IngestionRunStatus
from packages.domain.rules import RulesStatus


class HistoricalFetchBlockedError(Exception):
    """Raised when one of the 4 required season files couldn't be fetched at all (no usable body)."""


def run_historical_ingestion(
    session: Session,
    ingestion_run_id: uuid.UUID,
    client: HistoricalFPLClient,
    season_label: str,
    started_at: datetime,
) -> dict[str, Any]:
    """Run one season's full historical ingestion pass.

    Finalizes the ``ingestion_runs`` row before returning. Raises
    ``HistoricalFetchBlockedError`` if any of the 4 season files couldn't be
    fetched at all — callers must finalize the run as FAILED themselves in
    that case (see ``app/jobs/handlers/historical_data_ingestion.py``).
    """
    # --- Step 1: fetch all 4 files, persist raw snapshots ---
    file_results = client.fetch_all_season_files(season_label)
    for fetch_result in file_results.values():
        persist_raw_snapshot(session, ingestion_run_id, fetch_result)

    missing = [stem for stem, result in file_results.items() if result.raw_body is None]
    if missing:
        details = "; ".join(f"{stem}: {file_results[stem].error_message}" for stem in missing)
        raise HistoricalFetchBlockedError(f"season {season_label!r}: failed to fetch {missing} — {details}")

    # --- Step 2: season -> positions, teams -> players ---
    season = upsert_historical_season(session, season_label)

    positions_counts = upsert_historical_positions(session, season.id)
    position_id_by_fpl_id: dict[int, uuid.UUID] = {
        fpl_id: position_id
        for fpl_id, position_id in session.execute(
            select(Position.fpl_element_type_id, Position.id).where(Position.season_id == season.id)
        ).all()
    }

    teams_result = file_results["teams"]
    assert isinstance(teams_result.raw_body, list)
    teams_counts = upsert_historical_teams(
        session, season.id, teams_result.raw_body, last_synced_at=teams_result.retrieved_at
    )
    team_id_by_fpl_id: dict[int, uuid.UUID] = {
        fpl_id: team_id
        for fpl_id, team_id in session.execute(
            select(Team.fpl_team_id, Team.id).where(Team.season_id == season.id)
        ).all()
    }

    players_result = file_results["players_raw"]
    assert isinstance(players_result.raw_body, list)
    players_counts, player_issues = upsert_historical_players(
        session,
        season.id,
        players_result.raw_body,
        team_id_by_fpl_id=team_id_by_fpl_id,
        position_id_by_fpl_id=position_id_by_fpl_id,
        last_synced_at=players_result.retrieved_at,
    )
    element_to_player_id_map: dict[int, uuid.UUID] = {
        fpl_id: player_id
        for fpl_id, player_id in session.execute(
            select(Player.fpl_element_id, Player.id).where(Player.season_id == season.id)
        ).all()
    }

    # --- Step 3: gameweeks (derived from merged_gw.csv), fixtures ---
    merged_gw_result = file_results["merged_gw"]
    assert isinstance(merged_gw_result.raw_body, list)
    merged_gw_rows = merged_gw_result.raw_body

    gameweeks_counts = upsert_historical_gameweeks(
        session, season.id, merged_gw_rows, last_synced_at=merged_gw_result.retrieved_at
    )
    gameweek_id_by_round: dict[int, uuid.UUID] = {
        fpl_id: gameweek_id
        for fpl_id, gameweek_id in session.execute(
            select(Gameweek.fpl_event_id, Gameweek.id).where(Gameweek.season_id == season.id)
        ).all()
    }

    fixtures_result = file_results["fixtures"]
    assert isinstance(fixtures_result.raw_body, list)
    fixtures_counts, fixture_issues = upsert_historical_fixtures(
        session,
        season.id,
        fixtures_result.raw_body,
        team_id_by_fpl_id=team_id_by_fpl_id,
        gameweek_id_by_fpl_id=gameweek_id_by_round,
        last_synced_at=fixtures_result.retrieved_at,
    )
    fixture_id_by_fpl_id: dict[int, uuid.UUID] = {
        fpl_id: fixture_id
        for fpl_id, fixture_id in session.execute(
            select(Fixture.fpl_fixture_id, Fixture.id).where(Fixture.season_id == season.id)
        ).all()
    }

    # --- Step 4: player_gameweek_stats ---
    pgw_counts, pgw_issues = upsert_player_gameweek_stats(
        session,
        season.id,
        merged_gw_rows,
        element_to_player_id_map=element_to_player_id_map,
        gameweek_id_by_round=gameweek_id_by_round,
        fixture_id_by_fpl_id=fixture_id_by_fpl_id,
        team_id_by_fpl_id=team_id_by_fpl_id,
    )

    # --- Step 5: data quality issues, scoring ---
    all_issues = list(player_issues) + list(fixture_issues) + list(pgw_issues)
    session.bulk_save_objects(
        [
            DataQualityIssueRow(
                ingestion_run_id=ingestion_run_id,
                entity_type=issue.entity_type,
                entity_fpl_id=issue.entity_fpl_id,
                severity=issue.severity,
                check_name=issue.check_name,
                message=issue.message,
            )
            for issue in all_issues
        ]
    )
    session.flush()

    score = compute_score(all_issues)
    overall_health = _combine_health([result.health_status for result in file_results.values()])
    # There is no rules-versioning concept for historical seasons — RulesStatus.OK
    # is a neutral placeholder so this reuses the exact same go/no-go formula
    # live ingestion uses, without a spurious "rules blocked" veto.
    optimization_permitted = compute_optimization_permitted(score, all_issues, RulesStatus.OK, overall_health)

    # --- Step 6: render report, finalize the ingestion_runs row ---
    normalization_summary: dict[str, NormalizationCounts] = {
        "positions": positions_counts,
        "teams": teams_counts,
        "players": players_counts,
        "gameweeks": gameweeks_counts,
        "fixtures": fixtures_counts,
        "player_gameweek_stats": pgw_counts,
    }
    run_snapshots = (
        session.execute(select(RawApiSnapshot).where(RawApiSnapshot.ingestion_run_id == ingestion_run_id))
        .scalars()
        .all()
    )
    endpoint_rows = [
        {
            "endpoint": snap.endpoint,
            "health_status": snap.health_status.value
            if hasattr(snap.health_status, "value")
            else snap.health_status,
            "http_status": snap.http_status,
            "attempt_count": snap.attempt_count,
            "latency_ms": snap.latency_ms,
            "schema_drift_detected": snap.schema_drift_detected,
        }
        for snap in run_snapshots
    ]

    completed_at = datetime.now(UTC)
    has_notable_issues = len(all_issues) > 0
    status = (
        IngestionRunStatus.SUCCEEDED_WITH_WARNINGS if has_notable_issues else IngestionRunStatus.SUCCEEDED
    )

    report_markdown = render_historical_ingestion_report_markdown(
        ingestion_run_id=str(ingestion_run_id),
        started_at=started_at,
        completed_at=completed_at,
        status=status.value,
        season_labels=[season_label],
        endpoint_rows=endpoint_rows,
        normalization_summary=normalization_summary,
        issues=all_issues,
        data_quality_score=score,
        optimization_permitted=optimization_permitted,
    )

    run_row = session.get(IngestionRun, ingestion_run_id)
    if run_row is None:
        raise RuntimeError(f"ingestion_runs row {ingestion_run_id} not found during finalization")
    run_row.status = status
    run_row.overall_health = overall_health
    run_row.data_quality_score = Decimal(str(score))
    run_row.optimization_permitted = optimization_permitted
    run_row.report_markdown = report_markdown
    run_row.completed_at = completed_at
    run_row.summary_json = {
        "season_label": season.label,
        "normalization": {key: asdict(value) for key, value in normalization_summary.items()},
        "issue_count": len(all_issues),
    }
    session.flush()

    return {
        "ingestion_run_id": str(ingestion_run_id),
        "season_label": season.label,
        "data_quality_score": score,
        "optimization_permitted": optimization_permitted,
        "report_markdown": report_markdown,
        "normalization": {key: asdict(value) for key, value in normalization_summary.items()},
        "issue_count": len(all_issues),
        "status": status.value,
    }
