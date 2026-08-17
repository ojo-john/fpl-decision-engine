"""Integration tests for app.learning.note_effectiveness against a real DB.

Mirrors tests/integration/test_failure_attribution.py's own approach:
``RecommendationOutcome``/``RecommendationAudit``/``OptimizationRun``/``OptimizationRunPlayer``
rows (and the ``player_availability_risk_notes``/``player_role_notes`` rows
``evaluate_note_effectiveness_for_outcome`` reads) are built directly via the ORM rather than
driving a real recommendation-generation pipeline end to end -- that function only ever reads
already-persisted rows by FK/gameweek-window, so it cannot tell the difference, and hand-building
is the only practical way to deterministically exercise each boundary condition in isolation.
Real ``Player``/``Season``/``Gameweek``/``RulesConfig`` rows are still seeded via a real
``DATA_INGESTION`` run (``patched_fpl_client_optimization``) so every FK here is a genuine row in
a genuine Postgres. End-to-end wiring through a real ``GAMEWEEK_REVIEW`` run is covered separately
in ``tests/integration/test_gameweek_review_job.py``.

Priority order (this repo's own established test-writing convention): failure/boundary cases
first, happy path last.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.jobs.service import execute_job
from app.learning.note_effectiveness import evaluate_note_effectiveness_for_outcome
from app.optimization.projection import AVAILABILITY_RISK_NOTED_SUFFIX, ROLE_BOOSTED_SUFFIX
from packages.database.models.gameweek import Gameweek
from packages.database.models.note_effectiveness_evaluation import NoteEffectivenessEvaluation
from packages.database.models.optimization_run import OptimizationRun
from packages.database.models.optimization_run_player import OptimizationRunPlayer
from packages.database.models.player import Player
from packages.database.models.player_availability_risk_note import PlayerAvailabilityRiskNote
from packages.database.models.player_gameweek_stats import PlayerGameweekStats
from packages.database.models.player_role_note import PlayerRoleNote
from packages.database.models.recommendation_audit import RecommendationAudit
from packages.database.models.recommendation_outcome import RecommendationOutcome
from packages.database.models.rules_config import RulesConfig
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.learning import RecommendationConfidence
from packages.domain.optimization import (
    OptimizationRunStatus,
    RecommendationType,
    SolverStatus,
    TargetGameweekStatus,
)
from packages.domain.rules import RulesStatus

_REVIEWED_FPL_EVENT_ID = 20


def _now():
    return datetime.now(UTC)


def _seed_players(db_session) -> uuid.UUID:
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()


def _gameweek(db_session, season_id, fpl_event_id: int = _REVIEWED_FPL_EVENT_ID) -> Gameweek:
    return db_session.execute(
        select(Gameweek).where(Gameweek.season_id == season_id, Gameweek.fpl_event_id == fpl_event_id)
    ).scalar_one()


def _player_id(db_session, season_id, element_id: int) -> uuid.UUID:
    return db_session.execute(
        select(Player.id).where(Player.season_id == season_id, Player.fpl_element_id == element_id)
    ).scalar_one()


def _seed_rules_config_id(db_session, season_id) -> uuid.UUID:
    return db_session.execute(
        select(RulesConfig.id).where(RulesConfig.season_id == season_id, RulesConfig.is_current.is_(True))
    ).scalar_one()


def _create_audit(db_session, season_id, gameweek, **overrides) -> RecommendationAudit:
    defaults: dict = dict(
        season_id=season_id,
        target_gameweek_id=gameweek.id,
        target_fpl_event_id=gameweek.fpl_event_id,
        is_authoritative=True,
        confidence=RecommendationConfidence.HIGH,
        confidence_drivers_json=[],
        assumptions_json=[],
        known_risks_json=[],
        generated_at=_now(),
    )
    defaults.update(overrides)
    audit = RecommendationAudit(**defaults)
    db_session.add(audit)
    db_session.commit()
    db_session.refresh(audit)
    return audit


def _create_outcome(db_session, audit_id, **overrides) -> RecommendationOutcome:
    defaults: dict = dict(recommendation_audit_id=audit_id, finalized_at=_now())
    defaults.update(overrides)
    outcome = RecommendationOutcome(**defaults)
    db_session.add(outcome)
    db_session.commit()
    db_session.refresh(outcome)
    return outcome


def _create_optimization_run(db_session, season_id, rules_config_id, gameweek) -> OptimizationRun:
    run = OptimizationRun(
        season_id=season_id,
        rules_config_id=rules_config_id,
        recommendation_type=RecommendationType.JOINT_SQUAD_AND_XI,
        run_status=OptimizationRunStatus.SUCCEEDED,
        solver_status=SolverStatus.OPTIMAL,
        target_gameweek_id=gameweek.id,
        target_gameweek_status=TargetGameweekStatus.RESOLVED,
        squad_total_cost=800,
        squad_projected_points=Decimal("70.00"),
        xi_projected_points=Decimal("65.00"),
        optimization_permitted_snapshot=True,
        data_quality_score_snapshot=Decimal("100.00"),
        rules_status_snapshot=RulesStatus.OK,
        projection_formula_version=1,
        started_at=_now(),
        completed_at=_now(),
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _add_run_player(
    db_session, run_id, player_id, projected_points: float, source: str
) -> OptimizationRunPlayer:
    row = OptimizationRunPlayer(
        optimization_run_id=run_id,
        player_id=player_id,
        is_starting=True,
        is_captain=False,
        is_vice_captain=False,
        projected_points=Decimal(str(projected_points)),
        projection_source=source,
        now_cost_snapshot=80,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _seed_actual_points(db_session, player_id, season_id, gameweek_id, total_points: int, minutes: int = 90):
    db_session.add(
        PlayerGameweekStats(
            player_id=player_id,
            season_id=season_id,
            gameweek_id=gameweek_id,
            minutes=minutes,
            total_points=total_points,
            data_source="test",
        )
    )
    db_session.commit()


def _create_risk_note(
    db_session, player_id, season_id, severity: str, valid_from: int | None, valid_until: int | None
):
    note = PlayerAvailabilityRiskNote(
        player_id=player_id,
        season_id=season_id,
        reason="test risk note",
        severity=Decimal(severity),
        valid_from_fpl_event_id=valid_from,
        valid_until_fpl_event_id=valid_until,
        created_by="test",
    )
    db_session.add(note)
    db_session.commit()
    db_session.refresh(note)
    return note


def _create_role_note(
    db_session, player_id, season_id, severity: str, valid_from: int | None, valid_until: int | None
):
    note = PlayerRoleNote(
        player_id=player_id,
        season_id=season_id,
        role="penalties",
        reason="test role note",
        severity=Decimal(severity),
        valid_from_fpl_event_id=valid_from,
        valid_until_fpl_event_id=valid_until,
        created_by="test",
    )
    db_session.add(note)
    db_session.commit()
    db_session.refresh(note)
    return note


def _build_scenario(db_session):
    season_id = _seed_players(db_session)
    rules_config_id = _seed_rules_config_id(db_session, season_id)
    gameweek = _gameweek(db_session, season_id)
    run = _create_optimization_run(db_session, season_id, rules_config_id, gameweek)
    audit = _create_audit(db_session, season_id, gameweek, optimization_run_id=run.id)
    outcome = _create_outcome(db_session, audit.id)
    return season_id, gameweek, run, outcome


# --- Boundary / failure cases first ---------------------------------------------------------------


def test_no_active_notes_returns_empty_list_no_error(db_session, patched_fpl_client_optimization):
    season_id, gameweek, run, outcome = _build_scenario(db_session)
    player_id = _player_id(db_session, season_id, 30001)
    _add_run_player(db_session, run.id, player_id, 5.0, "test")
    _seed_actual_points(db_session, player_id, season_id, gameweek.id, 6)

    rows = evaluate_note_effectiveness_for_outcome(db_session, outcome.id)

    assert rows == []


def test_audit_with_no_optimization_run_returns_empty_list(db_session, patched_fpl_client_optimization):
    season_id = _seed_players(db_session)
    gameweek = _gameweek(db_session, season_id)
    audit = _create_audit(db_session, season_id, gameweek)  # no optimization_run_id
    outcome = _create_outcome(db_session, audit.id)

    rows = evaluate_note_effectiveness_for_outcome(db_session, outcome.id)

    assert rows == []


def test_note_no_longer_active_for_reviewed_gameweek_is_skipped_gracefully(
    db_session, patched_fpl_client_optimization
):
    """The safety-net case: projection_source claims a risk note applied (an immutable fact about
    generation time), but the note's own window doesn't cover the reviewed gameweek right now
    (expired, or its window was since narrowed) -- get_active_availability_risk_note correctly
    returns None, and that player is silently skipped rather than erroring or producing a
    misleading row. Mirrors the real-world case a note's window ends before the gameweek being
    reviewed, or the note has since been deleted entirely (both give the same "no longer
    resolvable" result)."""
    season_id, gameweek, run, outcome = _build_scenario(db_session)
    player_id = _player_id(db_session, season_id, 30001)
    source = f"test{AVAILABILITY_RISK_NOTED_SUFFIX}"
    _add_run_player(db_session, run.id, player_id, 3.25, source)
    _seed_actual_points(db_session, player_id, season_id, gameweek.id, 6)
    # Window ends well before the reviewed gameweek (20) -- not active now.
    _create_risk_note(db_session, player_id, season_id, "0.65", valid_from=1, valid_until=3)

    rows = evaluate_note_effectiveness_for_outcome(db_session, outcome.id)

    assert rows == []


def test_note_window_boundary_inclusive_valid_until_equals_reviewed_gameweek(
    db_session, patched_fpl_client_optimization
):
    season_id, gameweek, run, outcome = _build_scenario(db_session)
    player_id = _player_id(db_session, season_id, 30001)
    source = f"test{AVAILABILITY_RISK_NOTED_SUFFIX}"
    _add_run_player(db_session, run.id, player_id, 6.5, source)
    _seed_actual_points(db_session, player_id, season_id, gameweek.id, 6)
    # valid_until is EXACTLY the reviewed gameweek's fpl_event_id -- inclusive, must still apply.
    _create_risk_note(
        db_session, player_id, season_id, "0.65", valid_from=18, valid_until=_REVIEWED_FPL_EVENT_ID
    )

    rows = evaluate_note_effectiveness_for_outcome(db_session, outcome.id)

    assert len(rows) == 1
    assert rows[0].note_type == "availability_risk"


def test_note_window_boundary_inclusive_valid_from_equals_reviewed_gameweek(
    db_session, patched_fpl_client_optimization
):
    season_id, gameweek, run, outcome = _build_scenario(db_session)
    player_id = _player_id(db_session, season_id, 30001)
    source = f"test{AVAILABILITY_RISK_NOTED_SUFFIX}"
    _add_run_player(db_session, run.id, player_id, 6.5, source)
    _seed_actual_points(db_session, player_id, season_id, gameweek.id, 6)
    # valid_from is EXACTLY the reviewed gameweek's fpl_event_id -- inclusive, must still apply.
    _create_risk_note(
        db_session, player_id, season_id, "0.65", valid_from=_REVIEWED_FPL_EVENT_ID, valid_until=None
    )

    rows = evaluate_note_effectiveness_for_outcome(db_session, outcome.id)

    assert len(rows) == 1


def test_rerun_deletes_and_reinserts_rather_than_duplicating(db_session, patched_fpl_client_optimization):
    season_id, gameweek, run, outcome = _build_scenario(db_session)
    player_id = _player_id(db_session, season_id, 30001)
    source = f"test{AVAILABILITY_RISK_NOTED_SUFFIX}"
    _add_run_player(db_session, run.id, player_id, 6.5, source)
    _seed_actual_points(db_session, player_id, season_id, gameweek.id, 6)
    _create_risk_note(db_session, player_id, season_id, "0.65", valid_from=1, valid_until=None)

    first = evaluate_note_effectiveness_for_outcome(db_session, outcome.id)
    second = evaluate_note_effectiveness_for_outcome(db_session, outcome.id)

    assert len(first) == 1
    assert len(second) == 1
    persisted = (
        db_session.execute(
            select(NoteEffectivenessEvaluation).where(
                NoteEffectivenessEvaluation.recommendation_outcome_id == outcome.id
            )
        )
        .scalars()
        .all()
    )
    assert len(persisted) == 1


# --- Happy path / core behavior --------------------------------------------------------------------


def test_availability_risk_note_produces_correct_with_and_without_projection(
    db_session, patched_fpl_client_optimization
):
    season_id, gameweek, run, outcome = _build_scenario(db_session)
    player_id = _player_id(db_session, season_id, 30001)
    source = f"test{AVAILABILITY_RISK_NOTED_SUFFIX}"
    # 10.0 base * 0.65 severity = 6.5 with-note projection; actual = 10.0 (the note hurt accuracy).
    _add_run_player(db_session, run.id, player_id, 6.5, source)
    _seed_actual_points(db_session, player_id, season_id, gameweek.id, 10)
    _create_risk_note(db_session, player_id, season_id, "0.65", valid_from=1, valid_until=None)

    rows = evaluate_note_effectiveness_for_outcome(db_session, outcome.id)

    assert len(rows) == 1
    row = rows[0]
    assert row.note_type == "availability_risk"
    assert row.note_severity == Decimal("0.65")
    assert row.projected_points_with_note == Decimal("6.50")
    assert row.projected_points_without_note == Decimal("10.00")
    assert row.actual_points == Decimal("10.00")
    assert row.error_with_note == Decimal("3.50")
    assert row.error_without_note == Decimal("0.00")
    assert row.moved_closer_to_actual is False


def test_role_boost_note_produces_correct_with_and_without_projection(
    db_session, patched_fpl_client_optimization
):
    season_id, gameweek, run, outcome = _build_scenario(db_session)
    player_id = _player_id(db_session, season_id, 30001)
    source = f"test{ROLE_BOOSTED_SUFFIX}"
    # 10.0 base * 1.15 severity = 11.5 with-note projection; actual = 12.0 (the boost helped).
    _add_run_player(db_session, run.id, player_id, 11.5, source)
    _seed_actual_points(db_session, player_id, season_id, gameweek.id, 12)
    _create_role_note(db_session, player_id, season_id, "1.15", valid_from=1, valid_until=None)

    rows = evaluate_note_effectiveness_for_outcome(db_session, outcome.id)

    assert len(rows) == 1
    row = rows[0]
    assert row.note_type == "role_boost"
    assert row.moved_closer_to_actual is True


def test_stacked_risk_and_role_notes_on_same_player_produce_two_isolated_rows(
    db_session, patched_fpl_client_optimization
):
    """A player carrying BOTH an active risk note (e.g. a minor knock) and an active role-boost
    note (e.g. newly handed penalties) at once -- both multipliers compound in the persisted
    projection, but removing "just the risk note" and removing "just the role note" are two
    independently meaningful questions, so this must produce two rows, not one merged row."""
    season_id, gameweek, run, outcome = _build_scenario(db_session)
    player_id = _player_id(db_session, season_id, 30001)
    source = f"test{AVAILABILITY_RISK_NOTED_SUFFIX}{ROLE_BOOSTED_SUFFIX}"
    # base 10.0 -> risk 0.65 -> 6.5 -> role boost 1.20 -> 7.8 final with-note projection. Chosen to
    # land exactly on Numeric(6,2)'s own 2-decimal-place precision at every step -- an
    # OptimizationRunPlayer.projected_points value with a 3rd decimal digit (e.g. from a 1.15
    # multiplier) gets rounded on the way into/out of the DB, which would make this test's own
    # expected-value arithmetic diverge from what the code under test actually reads back.
    _add_run_player(db_session, run.id, player_id, 7.8, source)
    _seed_actual_points(db_session, player_id, season_id, gameweek.id, 6)
    _create_risk_note(db_session, player_id, season_id, "0.65", valid_from=1, valid_until=None)
    _create_role_note(db_session, player_id, season_id, "1.20", valid_from=1, valid_until=None)

    rows = evaluate_note_effectiveness_for_outcome(db_session, outcome.id)

    assert len(rows) == 2
    note_types = {row.note_type for row in rows}
    assert note_types == {"availability_risk", "role_boost"}
    risk_row = next(row for row in rows if row.note_type == "availability_risk")
    role_row = next(row for row in rows if row.note_type == "role_boost")
    # Isolating the risk note: divide out ONLY 0.65, leaving the role boost's own 1.20 baked in.
    assert risk_row.projected_points_without_note == Decimal("12.00")
    # Isolating the role note: divide out ONLY 1.20, leaving the risk note's own 0.65 baked in.
    assert role_row.projected_points_without_note == Decimal("6.50")
