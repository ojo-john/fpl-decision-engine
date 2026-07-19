"""Integration tests for app.modeling.promotion / app.modeling.shadow_evaluation against a real DB.

Milestone 7 Layer 5 (Recursive Learning Loop: Challenger Training/Shadow-Mode
Evaluation/Promotion Gates/Rollback). Mirrors ``tests/integration
/test_learning_signals.py``'s own approach: real ``Player``/``Season``/
``Gameweek`` rows are seeded via a real ``DATA_INGESTION`` run
(``patched_fpl_client_optimization``), and every other row
(``ModelVersion``/``ModelChallengerEvaluation``/``PlayerFeatures``) is
hand-built directly via the ORM.

This project's integration tests run against a shared, persistent local
Postgres (see conftest.py's module docstring). ``ModelVersion.is_champion``/
``lifecycle_state`` are GLOBAL invariants (at most one ``is_champion=True``/
``lifecycle_state=CHAMPION`` row across the WHOLE table, enforced at the
application level, not by a DB constraint -- see that model's own
docstring), so exactly like ``tests/integration/test_champion_loader.py``,
every test here clears any pre-existing champion/shadow-challenger state
BEFORE running and a module-scoped autouse fixture clears it again AFTER
every test, regardless of pass/fail/order, so a stray row from this module
never leaks into another test module running later in the same session (or
into a second full run of the suite against the same volume).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.jobs.service import execute_job
from app.modeling.promotion import decide_and_record_promotion as _decide_and_record_promotion
from app.modeling.promotion import evaluate_promotion
from app.modeling.promotion import rollback_to_previous_champion as _rollback_to_previous_champion
from app.modeling.shadow_evaluation import compute_shadow_challenger_evaluation
from packages.database.models.gameweek import Gameweek
from packages.database.models.model_challenger_evaluation import ModelChallengerEvaluation
from packages.database.models.model_promotion import ModelPromotion
from packages.database.models.model_version import ModelVersion
from packages.database.models.player import Player
from packages.database.models.player_features import PlayerFeatures
from packages.database.models.season import Season
from packages.domain.jobs import JobStatus, JobType, TriggerType
from packages.domain.learning import ModelLifecycleState, PromotionVerdict
from packages.domain.modeling import ModelFamily


def _now():
    return datetime.now(UTC)


def _clear_global_model_lifecycle_state(db_session) -> None:
    """Blanket reset across the WHOLE ``model_versions`` table -- mirrors
    ``test_champion_loader.py``'s own ``_clear_existing_champion``, extended to also reset
    ``lifecycle_state`` (Layer 5's own new global invariant column).
    """
    db_session.execute(
        update(ModelVersion).values(is_champion=False, lifecycle_state=ModelLifecycleState.CANDIDATE)
    )
    db_session.commit()


@pytest.fixture(autouse=True)
def _clear_model_lifecycle_state_after_test(db_session):
    _clear_global_model_lifecycle_state(db_session)
    yield
    _clear_global_model_lifecycle_state(db_session)


def _seed_players(db_session) -> uuid.UUID:
    ingestion_job = execute_job(db_session, job_type=JobType.DATA_INGESTION, trigger_type=TriggerType.LOCAL)
    assert ingestion_job.status == JobStatus.SUCCEEDED
    return db_session.execute(
        select(Season.id).where(Season.label == ingestion_job.output_json["season_label"])
    ).scalar_one()


def _gameweek(db_session, season_id, fpl_event_id: int) -> Gameweek:
    return db_session.execute(
        select(Gameweek).where(Gameweek.season_id == season_id, Gameweek.fpl_event_id == fpl_event_id)
    ).scalar_one()


def _player_id(db_session, season_id, element_id: int) -> uuid.UUID:
    return db_session.execute(
        select(Player.id).where(Player.season_id == season_id, Player.fpl_element_id == element_id)
    ).scalar_one()


def _seed_model_version(
    db_session,
    *,
    lifecycle_state: ModelLifecycleState,
    is_champion: bool,
    feature_version: int = 1,
    family: ModelFamily = ModelFamily.BASELINE_SEASON_AVERAGE,
) -> ModelVersion:
    version = ModelVersion(
        model_name=family.value,
        model_family=family,
        feature_version=feature_version,
        target_variable="total_points",
        training_seasons_json=["2023-24"],
        artifact_uri=None,
        is_champion=is_champion,
        lifecycle_state=lifecycle_state,
        trained_at=_now(),
    )
    db_session.add(version)
    db_session.commit()
    db_session.refresh(version)
    return version


def _seed_evaluation(
    db_session,
    *,
    challenger_id: uuid.UUID,
    champion_id: uuid.UUID,
    season_id: uuid.UUID,
    gameweek_id: uuid.UUID,
    challenger_mae: str,
    champion_mae: str,
    n_observations: int = 10,
) -> ModelChallengerEvaluation:
    row = ModelChallengerEvaluation(
        challenger_model_version_id=challenger_id,
        champion_model_version_id=champion_id,
        season_id=season_id,
        gameweek_id=gameweek_id,
        challenger_mae=Decimal(challenger_mae),
        champion_mae=Decimal(champion_mae),
        n_observations=n_observations,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _seed_player_features(
    db_session,
    *,
    player_id: uuid.UUID,
    season_id: uuid.UUID,
    gameweek_id: uuid.UUID,
    feature_version: int,
    season_avg_points_to_date: str,
    target_total_points: int,
) -> None:
    db_session.add(
        PlayerFeatures(
            player_id=player_id,
            season_id=season_id,
            gameweek_id=gameweek_id,
            feature_version=feature_version,
            cutoff_timestamp=_now(),
            computed_at=_now(),
            target_total_points=target_total_points,
            season_avg_points_to_date=Decimal(season_avg_points_to_date),
            data_source="test",
        )
    )
    db_session.commit()


# --- evaluate_promotion / decide_and_record_promotion --------------------------------------------


def test_decide_and_record_promotion_promotes_when_challenger_clears_margin(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    champion = _seed_model_version(db_session, lifecycle_state=ModelLifecycleState.CHAMPION, is_champion=True)
    challenger = _seed_model_version(
        db_session, lifecycle_state=ModelLifecycleState.SHADOW_CHALLENGER, is_champion=False
    )
    for fpl_event_id in range(1, 7):
        gameweek = _gameweek(db_session, season_id, fpl_event_id)
        _seed_evaluation(
            db_session,
            challenger_id=challenger.id,
            champion_id=champion.id,
            season_id=season_id,
            gameweek_id=gameweek.id,
            challenger_mae="1.0000",
            champion_mae="2.0000",
        )

    gate_result = evaluate_promotion(db_session, challenger.id, champion.id)
    assert gate_result.verdict == PromotionVerdict.PROMOTED

    promotion = _decide_and_record_promotion(db_session, gate_result, challenger.id)

    assert promotion.verdict == PromotionVerdict.PROMOTED
    assert promotion.previous_champion_model_version_id == champion.id
    assert promotion.challenger_model_version_id == challenger.id

    db_session.refresh(challenger)
    db_session.refresh(champion)
    assert challenger.is_champion is True
    assert challenger.lifecycle_state == ModelLifecycleState.CHAMPION
    assert champion.is_champion is False
    assert champion.lifecycle_state == ModelLifecycleState.RETIRED_CHAMPION

    # Exactly one champion across the whole table still holds.
    champions = (
        db_session.execute(select(ModelVersion).where(ModelVersion.is_champion.is_(True))).scalars().all()
    )
    assert len(champions) == 1
    assert champions[0].id == challenger.id


def test_decide_and_record_promotion_rejects_when_challenger_does_not_clear_margin(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    champion = _seed_model_version(db_session, lifecycle_state=ModelLifecycleState.CHAMPION, is_champion=True)
    challenger = _seed_model_version(
        db_session, lifecycle_state=ModelLifecycleState.SHADOW_CHALLENGER, is_champion=False
    )
    for fpl_event_id in range(1, 7):
        gameweek = _gameweek(db_session, season_id, fpl_event_id)
        _seed_evaluation(
            db_session,
            challenger_id=challenger.id,
            champion_id=champion.id,
            season_id=season_id,
            gameweek_id=gameweek.id,
            challenger_mae="1.9500",
            champion_mae="2.0000",
        )

    gate_result = evaluate_promotion(db_session, challenger.id, champion.id)
    assert gate_result.verdict == PromotionVerdict.REJECTED

    promotion = _decide_and_record_promotion(db_session, gate_result, challenger.id)

    assert promotion.verdict == PromotionVerdict.REJECTED

    db_session.refresh(challenger)
    db_session.refresh(champion)
    assert challenger.is_champion is False
    assert challenger.lifecycle_state == ModelLifecycleState.REJECTED_CHALLENGER
    # The current champion is completely untouched.
    assert champion.is_champion is True
    assert champion.lifecycle_state == ModelLifecycleState.CHAMPION


def test_decide_and_record_promotion_retained_inconclusive_changes_nothing(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    champion = _seed_model_version(db_session, lifecycle_state=ModelLifecycleState.CHAMPION, is_champion=True)
    challenger = _seed_model_version(
        db_session, lifecycle_state=ModelLifecycleState.SHADOW_CHALLENGER, is_champion=False
    )
    # Only 2 evaluations recorded -- well under the default min_evaluation_window=6.
    for fpl_event_id in range(1, 3):
        gameweek = _gameweek(db_session, season_id, fpl_event_id)
        _seed_evaluation(
            db_session,
            challenger_id=challenger.id,
            champion_id=champion.id,
            season_id=season_id,
            gameweek_id=gameweek.id,
            challenger_mae="0.1000",
            champion_mae="5.0000",
        )

    gate_result = evaluate_promotion(db_session, challenger.id, champion.id)
    assert gate_result.verdict == PromotionVerdict.RETAINED_INCONCLUSIVE

    promotion = _decide_and_record_promotion(db_session, gate_result, challenger.id)

    assert promotion.verdict == PromotionVerdict.RETAINED_INCONCLUSIVE
    # A real audit row IS still persisted for this verdict (see decide_and_record_promotion's own
    # docstring) -- findable in model_promotions.
    found = db_session.get(ModelPromotion, promotion.id)
    assert found is not None

    db_session.refresh(challenger)
    db_session.refresh(champion)
    assert challenger.is_champion is False
    assert challenger.lifecycle_state == ModelLifecycleState.SHADOW_CHALLENGER
    assert champion.is_champion is True
    assert champion.lifecycle_state == ModelLifecycleState.CHAMPION


# --- rollback_to_previous_champion -----------------------------------------------------------------


def test_rollback_reinstates_the_retired_champion_and_retires_the_current_one(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    original_champion = _seed_model_version(
        db_session, lifecycle_state=ModelLifecycleState.CHAMPION, is_champion=True
    )
    challenger = _seed_model_version(
        db_session, lifecycle_state=ModelLifecycleState.SHADOW_CHALLENGER, is_champion=False
    )
    for fpl_event_id in range(1, 7):
        gameweek = _gameweek(db_session, season_id, fpl_event_id)
        _seed_evaluation(
            db_session,
            challenger_id=challenger.id,
            champion_id=original_champion.id,
            season_id=season_id,
            gameweek_id=gameweek.id,
            challenger_mae="1.0000",
            champion_mae="2.0000",
        )
    gate_result = evaluate_promotion(db_session, challenger.id, original_champion.id)
    assert gate_result.verdict == PromotionVerdict.PROMOTED
    _decide_and_record_promotion(db_session, gate_result, challenger.id)

    db_session.refresh(original_champion)
    db_session.refresh(challenger)
    assert original_champion.lifecycle_state == ModelLifecycleState.RETIRED_CHAMPION
    assert challenger.lifecycle_state == ModelLifecycleState.CHAMPION

    rollback = _rollback_to_previous_champion(db_session, original_champion.id)

    assert rollback.verdict == PromotionVerdict.ROLLED_BACK
    assert rollback.challenger_model_version_id == original_champion.id
    assert rollback.previous_champion_model_version_id == challenger.id

    db_session.refresh(original_champion)
    db_session.refresh(challenger)
    assert original_champion.is_champion is True
    assert original_champion.lifecycle_state == ModelLifecycleState.CHAMPION
    # The model being rolled back FROM is retired, never deleted.
    assert challenger.is_champion is False
    assert challenger.lifecycle_state == ModelLifecycleState.RETIRED_CHAMPION

    champions = (
        db_session.execute(select(ModelVersion).where(ModelVersion.is_champion.is_(True))).scalars().all()
    )
    assert len(champions) == 1
    assert champions[0].id == original_champion.id


def test_rollback_rejects_a_target_that_is_not_a_retired_champion(db_session):
    challenger = _seed_model_version(
        db_session, lifecycle_state=ModelLifecycleState.SHADOW_CHALLENGER, is_champion=False
    )
    with pytest.raises(ValueError, match="RETIRED_CHAMPION"):
        _rollback_to_previous_champion(db_session, challenger.id)


# --- compute_shadow_challenger_evaluation ----------------------------------------------------------


def test_compute_shadow_challenger_evaluation_computes_real_paired_mae(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    gameweek = _gameweek(db_session, season_id, 1)
    player_a = _player_id(db_session, season_id, 30000)
    player_b = _player_id(db_session, season_id, 30001)

    champion = _seed_model_version(
        db_session, lifecycle_state=ModelLifecycleState.CHAMPION, is_champion=True, feature_version=1
    )
    challenger = _seed_model_version(
        db_session,
        lifecycle_state=ModelLifecycleState.SHADOW_CHALLENGER,
        is_champion=False,
        feature_version=2,
    )

    # Champion (feature_version=1): predicts 9.0 for both players; both actually scored 10 -> abs
    # error 1.0 each -> champion_mae = 1.0.
    _seed_player_features(
        db_session,
        player_id=player_a,
        season_id=season_id,
        gameweek_id=gameweek.id,
        feature_version=1,
        season_avg_points_to_date="9.00",
        target_total_points=10,
    )
    _seed_player_features(
        db_session,
        player_id=player_b,
        season_id=season_id,
        gameweek_id=gameweek.id,
        feature_version=1,
        season_avg_points_to_date="9.00",
        target_total_points=10,
    )
    # Challenger (feature_version=2): predicts 5.0 for both players -> abs error 5.0 each ->
    # challenger_mae = 5.0.
    _seed_player_features(
        db_session,
        player_id=player_a,
        season_id=season_id,
        gameweek_id=gameweek.id,
        feature_version=2,
        season_avg_points_to_date="5.00",
        target_total_points=10,
    )
    _seed_player_features(
        db_session,
        player_id=player_b,
        season_id=season_id,
        gameweek_id=gameweek.id,
        feature_version=2,
        season_avg_points_to_date="5.00",
        target_total_points=10,
    )

    result = compute_shadow_challenger_evaluation(db_session, season_id, gameweek.id)

    assert result is not None
    assert result.challenger_model_version_id == challenger.id
    assert result.champion_model_version_id == champion.id
    assert result.n_observations == 2
    assert result.champion_mae == Decimal("1.0000")
    assert result.challenger_mae == Decimal("5.0000")

    # Exactly one row persisted.
    rows = (
        db_session.execute(
            select(ModelChallengerEvaluation).where(
                ModelChallengerEvaluation.challenger_model_version_id == challenger.id
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_compute_shadow_challenger_evaluation_rerun_upserts_in_place(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    gameweek = _gameweek(db_session, season_id, 1)
    player_a = _player_id(db_session, season_id, 30000)

    _seed_model_version(
        db_session, lifecycle_state=ModelLifecycleState.CHAMPION, is_champion=True, feature_version=1
    )
    challenger = _seed_model_version(
        db_session,
        lifecycle_state=ModelLifecycleState.SHADOW_CHALLENGER,
        is_champion=False,
        feature_version=1,
    )
    _seed_player_features(
        db_session,
        player_id=player_a,
        season_id=season_id,
        gameweek_id=gameweek.id,
        feature_version=1,
        season_avg_points_to_date="9.00",
        target_total_points=10,
    )

    first = compute_shadow_challenger_evaluation(db_session, season_id, gameweek.id)
    assert first is not None
    first_id = first.id

    # A late correction changes the real actual for this player -- rerun must converge the SAME
    # row to the corrected number, not create a second one (would violate the table's own unique
    # constraint on (challenger_model_version_id, gameweek_id)).
    row = db_session.execute(
        select(PlayerFeatures).where(
            PlayerFeatures.player_id == player_a,
            PlayerFeatures.gameweek_id == gameweek.id,
            PlayerFeatures.feature_version == 1,
        )
    ).scalar_one()
    row.target_total_points = 20
    db_session.commit()

    second = compute_shadow_challenger_evaluation(db_session, season_id, gameweek.id)
    assert second is not None
    assert second.id == first_id
    assert second.challenger_mae == Decimal("11.0000")  # |9 - 20|

    rows = (
        db_session.execute(
            select(ModelChallengerEvaluation).where(
                ModelChallengerEvaluation.challenger_model_version_id == challenger.id
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_compute_shadow_challenger_evaluation_returns_none_when_no_shadow_challenger(
    db_session, patched_fpl_client_optimization
):
    season_id = _seed_players(db_session)
    gameweek = _gameweek(db_session, season_id, 1)
    _seed_model_version(db_session, lifecycle_state=ModelLifecycleState.CHAMPION, is_champion=True)

    result = compute_shadow_challenger_evaluation(db_session, season_id, gameweek.id)

    assert result is None
