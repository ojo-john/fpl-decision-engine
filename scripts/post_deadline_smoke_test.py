#!/usr/bin/env python
"""Real-data smoke test for features that have only ever run against synthetic fixtures.

Six features touch real, live FPL data in ways ``tests/unit``/``tests/integration`` cannot
exercise (those run entirely against committed fixtures/synthetic seasons — see this repo's own
``CLAUDE.md``): MANAGER_SYNC's squad fetch, the user's own chip-inventory derivation
(``app.chips.inventory``), LEAGUE_SYNC's standings/matches sync, rival-profile derivation
(``app.h2h.rival_state``, which only needs ``/history/`` and works pre-season), rival
picks-based squad differentials (``app.h2h.differential``, gated on a finished gameweek), and
price-signal threshold calibration (``app.players.price_signal_calibration``, gated on real
price movements). Each has a real, FPL-platform-imposed precondition that does not exist yet in
pre-season (see ``docs/operations.md``'s "Post-deadline real-data verification" section for the
full runbook and why each precondition exists) — this script's job is to tell PENDING (expected,
precondition not met yet) apart from FAIL (the precondition IS met and the feature still broke).

Usage:
    set -a; source .env; set +a   # real dev DB + real FPL API -- see CLAUDE.md, NOT .env.test
    uv run python scripts/post_deadline_smoke_test.py

Exit code is 0 unless at least one check FAILs; PENDING checks never affect the exit code. Every
check that runs a real job (MANAGER_SYNC/LEAGUE_SYNC) uses a same-day idempotency key, so
reruns within a day return the already-recorded result instead of re-hitting the live FPL API.
"""

import argparse
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

# See scripts/run_job.py's own comment: a plain script invocation only puts scripts/ on
# sys.path, not the repo root, so app/packages wouldn't otherwise resolve.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.chips.inventory import derive_user_chip_windows  # noqa: E402
from app.chips.windows import resolve_chip_windows  # noqa: E402
from app.h2h.differential import compute_squad_differential  # noqa: E402
from app.jobs.service import execute_job  # noqa: E402
from app.optimization.gameweek_resolution import resolve_most_recent_finished_gameweek_event  # noqa: E402
from app.players.price_signal_calibration import (  # noqa: E402
    MIN_CALIBRATION_SAMPLE_SIZE,
    PlayerPriceChangeInput,
    PlayerPriceObservationInput,
    build_calibration_samples,
    calibrate_thresholds,
)
from app.transfers.rules import load_current_rules_config_id  # noqa: E402
from packages.database.models.gameweek import Gameweek  # noqa: E402
from packages.database.models.league import League  # noqa: E402
from packages.database.models.league_entry import LeagueEntry  # noqa: E402
from packages.database.models.manager_squad_snapshot import ManagerSquadSnapshot  # noqa: E402
from packages.database.models.player_price_history import PlayerPriceHistory  # noqa: E402
from packages.database.models.player_price_signal_observation import (  # noqa: E402
    PlayerPriceSignalObservation,
)
from packages.database.models.rival_gameweek_snapshot import RivalGameweekSnapshot  # noqa: E402
from packages.database.models.rival_profile import RivalProfile  # noqa: E402
from packages.database.models.rules_config import RulesConfig  # noqa: E402
from packages.database.models.season import Season  # noqa: E402
from packages.database.session import SessionLocal  # noqa: E402
from packages.domain.h2h import PicksAvailability  # noqa: E402
from packages.domain.jobs import JobStatus, JobType, TriggerType  # noqa: E402
from packages.domain.transfers import ManagerSquadSnapshotSource  # noqa: E402

# The real, specific manager/league this app is built for -- same hardcoding precedent as
# .github/workflows/league-watch.yml and scout-watch.yml's current-squad.json (a single-user
# tool's own identity data, not deployment config).
_DEFAULT_FPL_MANAGER_ID = 918600
_DEFAULT_FPL_LEAGUE_ID = 814682


class CheckStatus(StrEnum):
    PASS = "PASS"
    PENDING = "PENDING"  # expected: real-world precondition hasn't happened yet
    FAIL = "FAIL"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str


def _resolve_current_season(session: Session) -> Season | None:
    return session.execute(select(Season).where(Season.is_current.is_(True))).scalar_one_or_none()


def _today_idempotency_key(*parts: str) -> str:
    return ":".join(["post_deadline_smoke_test", *parts, date.today().isoformat()])


def check_manager_sync(session: Session, fpl_manager_id: int) -> CheckResult:
    job = execute_job(
        session,
        job_type=JobType.MANAGER_SYNC,
        parameters={"fpl_manager_id": fpl_manager_id},
        requested_by="post_deadline_smoke_test",
        trigger_type=TriggerType.LOCAL,
        idempotency_key=_today_idempotency_key("manager_sync", str(fpl_manager_id)),
    )
    if job.status == JobStatus.SUCCEEDED:
        return CheckResult(
            "manager_sync",
            CheckStatus.PASS,
            f"Real squad synced -- job {job.id}, output={job.output_json}.",
        )
    if job.status == JobStatus.FAILED and job.error_code == "NO_SQUAD_SET_YET":
        return CheckResult(
            "manager_sync",
            CheckStatus.PENDING,
            "FPL withholds every manager's picks until the gameweek's own deadline passes "
            "(see app/jobs/handlers/manager_sync.py's module docstring) -- expected pre-deadline.",
        )
    return CheckResult(
        "manager_sync",
        CheckStatus.FAIL,
        f"job {job.id} ended {job.status.value} unexpectedly: "
        f"error_code={job.error_code}, error_message={job.error_message}",
    )


def check_chip_inventory(session: Session, season: Season, fpl_manager_id: int) -> CheckResult:
    snapshot = session.execute(
        select(ManagerSquadSnapshot).where(
            ManagerSquadSnapshot.fpl_manager_id == fpl_manager_id,
            ManagerSquadSnapshot.source == ManagerSquadSnapshotSource.MANAGER_SYNC,
            ManagerSquadSnapshot.is_current.is_(True),
        )
    ).scalar_one_or_none()
    if snapshot is None:
        return CheckResult(
            "chip_inventory",
            CheckStatus.PENDING,
            "No current ManagerSquadSnapshot yet -- depends on manager_sync succeeding first.",
        )

    rules_config_id = load_current_rules_config_id(session, season.id)
    raw_chips = session.execute(
        select(RulesConfig.raw_chips_json).where(RulesConfig.id == rules_config_id)
    ).scalar_one()
    chip_windows = resolve_chip_windows(raw_chips)
    current_fpl_event_id = (
        None
        if snapshot.as_of_gameweek_id is None
        else session.execute(
            select(Gameweek.fpl_event_id).where(Gameweek.id == snapshot.as_of_gameweek_id)
        ).scalar_one_or_none()
    )
    user_windows = derive_user_chip_windows(
        chip_windows, snapshot.chips_used_json, snapshot.active_chip, current_fpl_event_id
    )
    statuses = ", ".join(f"{w.window.chip_type.value}={w.status.value}" for w in user_windows)
    return CheckResult(
        "chip_inventory",
        CheckStatus.PASS,
        f"Derived {len(user_windows)} real chip window(s) from snapshot {snapshot.id}: {statuses}.",
    )


def check_league_sync(
    session: Session, season: Season, fpl_league_id: int, fpl_manager_id: int
) -> CheckResult:
    job = execute_job(
        session,
        job_type=JobType.LEAGUE_SYNC,
        parameters={"fpl_league_id": fpl_league_id, "exclude_fpl_manager_id": fpl_manager_id},
        requested_by="post_deadline_smoke_test",
        trigger_type=TriggerType.LOCAL,
        idempotency_key=_today_idempotency_key("league_sync", str(fpl_league_id)),
    )
    if job.status != JobStatus.SUCCEEDED:
        return CheckResult(
            "league_sync",
            CheckStatus.FAIL,
            f"job {job.id} ended {job.status.value}: "
            f"error_code={job.error_code}, error_message={job.error_message}. "
            "Standings/matches sync is NOT deadline-gated -- this should always succeed.",
        )
    league_row = session.execute(
        select(League.id).where(League.fpl_league_id == fpl_league_id)
    ).first()
    if league_row is None:
        return CheckResult(
            "league_sync",
            CheckStatus.FAIL,
            f"job {job.id} SUCCEEDED but no League row was persisted for league {fpl_league_id}.",
        )
    entry_count = len(
        session.execute(
            select(LeagueEntry.id)
            .join(League, LeagueEntry.league_id == League.id)
            .where(League.fpl_league_id == fpl_league_id)
        )
        .scalars()
        .all()
    )
    if entry_count > 0:
        return CheckResult(
            "league_sync",
            CheckStatus.PASS,
            f"Real standings synced -- job {job.id}, {entry_count} league entries, output={job.output_json}.",
        )

    # Real, live-confirmed FPL platform behavior (see league-watch.yml / ARCHITECTURE.md's
    # "League Watch" section): FPL only populates `standings.results` for an H2H league once
    # the season has started AND at least one gameweek has been played; joined managers sit
    # under `new_entries` in an empty-standings state indefinitely before that. Zero entries
    # is only "expected, not a bug" while that precondition genuinely hasn't happened yet.
    if resolve_most_recent_finished_gameweek_event(session, season.id) is None:
        return CheckResult(
            "league_sync",
            CheckStatus.PENDING,
            f"job {job.id} SUCCEEDED and the League row exists, but FPL's own "
            "leagues-h2h/{id}/standings/ genuinely returns an empty standings.results "
            "until the season has started and a gameweek has finished (joined managers "
            "show up under new_entries instead) -- expected pre-season, not a bug.",
        )
    return CheckResult(
        "league_sync",
        CheckStatus.FAIL,
        f"job {job.id} SUCCEEDED with a finished gameweek already on record, but zero "
        "LeagueEntry rows exist -- standings should be populated by now.",
    )


def check_rival_profile_computation(session: Session, season: Season, fpl_league_id: int) -> CheckResult:
    profile_count = len(
        session.execute(
            select(RivalProfile.id)
            .join(League, RivalProfile.league_id == League.id)
            .where(League.fpl_league_id == fpl_league_id, RivalProfile.season_id == season.id)
        )
        .scalars()
        .all()
    )
    if profile_count == 0:
        return CheckResult(
            "rival_profile_computation",
            CheckStatus.PENDING,
            "No RivalProfile rows yet -- league_sync only computes a profile per standings "
            "entry_row, and (per check_league_sync's own finding) standings.results is "
            "genuinely empty pre-season, so this is gated on the same real precondition as "
            "league_sync's entries, not on manager_sync's deadline directly.",
        )
    return CheckResult(
        "rival_profile_computation",
        CheckStatus.PASS,
        f"{profile_count} real RivalProfile row(s) computed from /history/ data.",
    )


def check_rival_differentials(session: Session, season: Season, fpl_manager_id: int) -> CheckResult:
    own_snapshot = session.execute(
        select(ManagerSquadSnapshot).where(
            ManagerSquadSnapshot.fpl_manager_id == fpl_manager_id,
            ManagerSquadSnapshot.source == ManagerSquadSnapshotSource.MANAGER_SYNC,
            ManagerSquadSnapshot.is_current.is_(True),
        )
    ).scalar_one_or_none()
    if own_snapshot is None:
        return CheckResult(
            "rival_differentials",
            CheckStatus.PENDING,
            "Own squad not synced yet (blocked pre-deadline, same as manager_sync).",
        )

    fpl_event_id = resolve_most_recent_finished_gameweek_event(session, season.id)
    if fpl_event_id is None:
        return CheckResult(
            "rival_differentials",
            CheckStatus.PENDING,
            "No finished gameweek yet -- FPL withholds every manager's picks (own and rivals') "
            "until a gameweek's own deadline passes (see app/jobs/handlers/league_sync.py).",
        )

    available_rival = session.execute(
        select(RivalGameweekSnapshot)
        .where(
            RivalGameweekSnapshot.fpl_event_id == fpl_event_id,
            RivalGameweekSnapshot.picks_availability == PicksAvailability.AVAILABLE,
        )
        .order_by(RivalGameweekSnapshot.created_at.desc())
    ).scalars().first()
    if available_rival is None:
        return CheckResult(
            "rival_differentials",
            CheckStatus.FAIL,
            f"Gameweek {fpl_event_id} has finished (real precondition met) but no rival "
            "RivalGameweekSnapshot with picks_availability=AVAILABLE exists -- run league_sync.",
        )

    own_ids = [uuid.UUID(pid) for pid in own_snapshot.squad_player_ids]
    rival_ids = [uuid.UUID(pid) for pid in (available_rival.squad_player_ids or [])]
    diff = compute_squad_differential(own_ids, rival_ids)
    return CheckResult(
        "rival_differentials",
        CheckStatus.PASS,
        f"Real differential vs manager {available_rival.fpl_manager_id} for gameweek "
        f"{fpl_event_id}: {len(diff.only_first)} only-mine, {len(diff.only_second)} only-theirs.",
    )


def check_price_signal_calibration(session: Session, season: Season) -> CheckResult:
    observation_rows = session.execute(
        select(
            PlayerPriceSignalObservation.player_id,
            PlayerPriceSignalObservation.observed_at,
            PlayerPriceSignalObservation.transfers_in_event,
            PlayerPriceSignalObservation.transfers_out_event,
            PlayerPriceSignalObservation.selected_by_percent,
            PlayerPriceSignalObservation.total_players,
        ).where(PlayerPriceSignalObservation.season_id == season.id)
    ).all()
    change_rows = session.execute(
        select(PlayerPriceHistory.player_id, PlayerPriceHistory.observed_at).where(
            PlayerPriceHistory.season_id == season.id
        )
    ).all()

    observations = [
        PlayerPriceObservationInput(
            player_id=row.player_id,
            observed_at=row.observed_at,
            transfers_in_event=row.transfers_in_event,
            transfers_out_event=row.transfers_out_event,
            selected_by_percent=row.selected_by_percent,
            total_players=row.total_players,
        )
        for row in observation_rows
    ]
    price_changes = [
        PlayerPriceChangeInput(player_id=player_id, observed_at=observed_at)
        for player_id, observed_at in change_rows
    ]
    samples = build_calibration_samples(observations, price_changes)

    if len(samples) < MIN_CALIBRATION_SAMPLE_SIZE:
        return CheckResult(
            "price_signal_calibration",
            CheckStatus.PENDING,
            f"Only {len(samples)}/{MIN_CALIBRATION_SAMPLE_SIZE} real (signal, outcome) samples "
            "exist -- needs real price movements across a live season "
            "(app/players/price_signal_calibration.py's module docstring).",
        )

    result = calibrate_thresholds(
        samples,
        current_ratio_threshold=Decimal("0.02"),
        current_absolute_floor=1000,
    )
    if result is None:
        return CheckResult(
            "price_signal_calibration",
            CheckStatus.FAIL,
            f"{len(samples)} samples exist (>= threshold) but calibrate_thresholds returned None.",
        )
    return CheckResult(
        "price_signal_calibration",
        CheckStatus.PASS,
        f"Real calibration ran on {result.sample_size} samples: "
        f"ratio_threshold={result.ratio_threshold}, absolute_floor={result.absolute_floor}, "
        f"accuracy={result.accuracy}.",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fpl-manager-id", type=int, default=_DEFAULT_FPL_MANAGER_ID)
    parser.add_argument("--fpl-league-id", type=int, default=_DEFAULT_FPL_LEAGUE_ID)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session = SessionLocal()
    try:
        season = _resolve_current_season(session)
        if season is None:
            print("FAIL no_current_season: no Season row has is_current=True.")
            return 1

        results: list[CheckResult] = []
        results.append(check_manager_sync(session, args.fpl_manager_id))
        results.append(check_chip_inventory(session, season, args.fpl_manager_id))
        results.append(check_league_sync(session, season, args.fpl_league_id, args.fpl_manager_id))
        results.append(check_rival_profile_computation(session, season, args.fpl_league_id))
        results.append(check_rival_differentials(session, season, args.fpl_manager_id))
        results.append(check_price_signal_calibration(session, season))

        print(f"\nPost-deadline real-data smoke test -- {datetime.now(UTC).isoformat()}")
        print(f"Season: {season.id} (is_current=True)\n")
        for r in results:
            print(f"[{r.status.value:7s}] {r.name}: {r.detail}")

        fail_count = sum(1 for r in results if r.status == CheckStatus.FAIL)
        pending_count = sum(1 for r in results if r.status == CheckStatus.PENDING)
        pass_count = sum(1 for r in results if r.status == CheckStatus.PASS)
        print(f"\n{pass_count} PASS, {pending_count} PENDING, {fail_count} FAIL")
        return 1 if fail_count > 0 else 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
