"""Real-historical-data validation for the ELO_ADJUSTMENT_SCALE constant added to
``app.optimization.projection._cold_start_projection`` (gap #3, ClubElo-based fixture
adjustment).

Compares real MAE (against real actual points) of three candidate cold-start fixture adjustments,
for real players with real prior-season history, in a real completed season's own early
gameweeks (GW1-6, matching ``app.features.engineering.DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF``):

1. ``no_adjustment``  -- career_avg alone, the control.
2. ``fpl_difficulty``  -- the existing production formula: career_avg * (1 + (3-difficulty)*0.075).
3. ``elo_differential`` -- the new formula: career_avg * (1 + (expected_score-0.5)*ELO_ADJUSTMENT_SCALE),
   using REAL historical ClubElo ratings for the two teams as of the real fixture date.

Deliberately isolates JUST the adjustment-multiplier choice (not the full ep_next/career_avg
blend the coordinator's own cold_start_validation harness covers elsewhere) by using
career_avg_early_season_points_prior_seasons ALONE as the base signal -- this avoids the "no
historical ep_next snapshot exists" gap that harness already documented, without inheriting its
stale-proxy caveat.

Read-only against the real dev database (never writes/commits) and makes real, rate-limited
(1/sec, enforced by ClubEloClient itself) calls to ClubElo's real historical archive. Scoped to
ONE real completed season (2025-26, the most recent) rather than all 6 -- kept deliberately
narrow out of respect for ClubElo's "be reasonable" ask (this already issues on the order of
dozens of real historical-date requests) and to keep total real runtime reasonable; this is a
narrower evidence base than the other cold-start constants' own 6-season validation, and this
script says so honestly in its own report rather than overstating it.

Usage: uv run python scripts/validate_elo_fixture_adjustment.py
"""

import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

# See scripts/run_job.py's identical comment: a plain script invocation only puts scripts/ on
# sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.features.engineering import (  # noqa: E402
    DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF,
    PriorGameweekStat,
    career_avg_early_season_points_prior_seasons,
)
from app.ingestion.team_strength import (  # noqa: E402
    parse_ratings_csv,
    premier_league_ratings_by_club,
    resolve_clubelo_name,
)
from packages.clubelo_client.client import ClubEloClient  # noqa: E402

# Real dev database, read-only -- constructed directly (not via .env, unavailable in an isolated
# worktree) using this repo's own documented default local credentials (see .env.example).
_REAL_DB_URL = "postgresql+psycopg://fpl:changeme@localhost:5432/fpl_decision_engine"

_TARGET_SEASON_LABEL = "2025-26"
_FPL_DIFFICULTY_COEFFICIENT = 0.075
_ELO_SCALE_CANDIDATES = [0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.375, 0.5, 0.75, 1.0]


def _mae(errors: list[float]) -> float:
    return sum(abs(e) for e in errors) / len(errors)


def main() -> None:
    engine = create_engine(_REAL_DB_URL)
    session = Session(engine)

    target_season_id = session.execute(
        text("SELECT id FROM seasons WHERE label = :label"), {"label": _TARGET_SEASON_LABEL}
    ).scalar_one()

    # Real prior-season history for every fpl_code appearing in an EARLIER season, early
    # gameweeks only (fpl_event_id <= cutoff) -- built once, up front, then grouped by fpl_code.
    prior_rows_by_code: dict[int, list[PriorGameweekStat]] = defaultdict(list)
    prior_result = session.execute(
        text(
            """
            SELECT p.fpl_code, s.id AS season_id, s.label AS season_label,
                   pgs.total_points, pgs.minutes, g.fpl_event_id, g.deadline_time
            FROM player_gameweek_stats pgs
            JOIN players p ON p.id = pgs.player_id
            JOIN seasons s ON s.id = pgs.season_id
            JOIN gameweeks g ON g.id = pgs.gameweek_id
            WHERE s.label < :target_label
              AND g.fpl_event_id <= :cutoff
              AND p.fpl_code IS NOT NULL
            """
        ),
        {"target_label": _TARGET_SEASON_LABEL, "cutoff": DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF},
    ).all()
    for row in prior_result:
        prior_rows_by_code[row.fpl_code].append(
            PriorGameweekStat(
                season_id=str(row.season_id),
                gameweek_key=f"{row.season_id}:{row.fpl_event_id}",
                gameweek_deadline=row.deadline_time,
                total_points=row.total_points,
                minutes=row.minutes,
                was_home=None,
                fixture_kickoff_time=None,
                fpl_event_id=row.fpl_event_id,
            )
        )

    # Real target-season GW1-6 fixtures with a real, finished actual score and a real fixture
    # difficulty + kickoff date -- the ground truth this script validates candidates against.
    target_rows = session.execute(
        text(
            """
            SELECT pgs.player_id, p.fpl_code, p.team_id, pgs.total_points AS actual_points,
                   g.fpl_event_id, f.home_team_id, f.away_team_id, f.home_difficulty,
                   f.away_difficulty, f.kickoff_time
            FROM player_gameweek_stats pgs
            JOIN players p ON p.id = pgs.player_id
            JOIN gameweeks g ON g.id = pgs.gameweek_id
            JOIN fixtures f ON f.season_id = pgs.season_id AND f.gameweek_id = pgs.gameweek_id
                AND (f.home_team_id = p.team_id OR f.away_team_id = p.team_id)
            WHERE pgs.season_id = :season_id AND g.fpl_event_id <= :cutoff
              AND f.kickoff_time IS NOT NULL
            """
        ),
        {"season_id": target_season_id, "cutoff": DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF},
    ).all()

    team_name_by_id: dict = dict(
        session.execute(
            text("SELECT id, name FROM teams WHERE season_id = :season_id"), {"season_id": target_season_id}
        ).all()
    )

    # Real historical ClubElo snapshots, fetched once per distinct real kickoff date and cached
    # -- ClubEloClient's own MIN_REQUEST_INTERVAL_S throttle keeps this respectful.
    distinct_dates = sorted({row.kickoff_time.date().isoformat() for row in target_rows})
    print(f"Fetching {len(distinct_dates)} real historical ClubElo snapshots (rate-limited)...")
    elo_by_date_and_club: dict[str, dict[str, Decimal]] = {}
    with ClubEloClient() as client:
        for date_str in distinct_dates:
            result = client.fetch_ratings_snapshot(date_str)
            if result.raw_csv is None:
                print(f"  {date_str}: FETCH FAILED ({result.error_message}) -- skipping this date")
                continue
            elo_by_date_and_club[date_str] = premier_league_ratings_by_club(parse_ratings_csv(result.raw_csv))
    print(f"Fetched {len(elo_by_date_and_club)}/{len(distinct_dates)} snapshots successfully.\n")

    errors_no_adjustment: list[float] = []
    errors_fpl_difficulty: list[float] = []
    errors_elo_by_scale: dict[float, list[float]] = {scale: [] for scale in _ELO_SCALE_CANDIDATES}
    elo_skipped_unresolvable = 0

    for row in target_rows:
        prior = prior_rows_by_code.get(row.fpl_code, [])
        career_avg = career_avg_early_season_points_prior_seasons(prior)
        if career_avg is None:
            continue  # no real prior-season history -- not a cold-start-relevant observation

        base = float(career_avg)
        actual = row.actual_points

        errors_no_adjustment.append(base - actual)

        is_home = row.home_team_id == row.team_id
        difficulty = row.home_difficulty if is_home else row.away_difficulty
        if difficulty is not None:
            fpl_adjusted = base * (1 + (3 - difficulty) * _FPL_DIFFICULTY_COEFFICIENT)
            errors_fpl_difficulty.append(fpl_adjusted - actual)

        date_str = row.kickoff_time.date().isoformat()
        ratings = elo_by_date_and_club.get(date_str)
        team_clubelo_name = resolve_clubelo_name(team_name_by_id.get(row.team_id, ""))
        team_elo = ratings.get(team_clubelo_name) if ratings else None
        opponent_id = row.away_team_id if is_home else row.home_team_id
        opponent_clubelo_name = resolve_clubelo_name(team_name_by_id.get(opponent_id, ""))
        opponent_elo = ratings.get(opponent_clubelo_name) if ratings else None
        if team_elo is None or opponent_elo is None:
            elo_skipped_unresolvable += 1
            continue
        elo_diff = float(team_elo - opponent_elo)
        expected_score = 1 / (1 + 10 ** (-elo_diff / 400))
        for scale in _ELO_SCALE_CANDIDATES:
            elo_adjusted = base * (1 + (expected_score - 0.5) * scale)
            errors_elo_by_scale[scale].append(elo_adjusted - actual)

    n_elo = len(next(iter(errors_elo_by_scale.values())))
    print(f"Real season validated: {_TARGET_SEASON_LABEL}, GW1-{DEFAULT_EARLY_SEASON_GAMEWEEK_CUTOFF}")
    print(f"n (no_adjustment / fpl_difficulty)  = {len(errors_no_adjustment)}")
    print(
        f"n (elo_differential, resolvable)    = {n_elo}  "
        f"({elo_skipped_unresolvable} skipped: unresolvable team/date)"
    )
    print()
    print(f"MAE no_adjustment    = {_mae(errors_no_adjustment):.4f}")
    print(f"MAE fpl_difficulty   = {_mae(errors_fpl_difficulty):.4f}")
    for scale in _ELO_SCALE_CANDIDATES:
        errors = errors_elo_by_scale[scale]
        mae = _mae(errors) if errors else float("nan")
        print(f"MAE elo_scale={scale:<5} = {mae:.4f}")

    session.close()


if __name__ == "__main__":
    main()
