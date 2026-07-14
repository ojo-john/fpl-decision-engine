"""Shared fixtures for integration tests.

These tests require a real, reachable Postgres database (the same one
Alembic migrations target) — via `docker compose up -d postgres` locally, or
a Postgres service container in CI. Docker is not installed on the machine
this repo was originally scaffolded on, so locally these tests are expected
to skip cleanly (not error) via the `require_database` fixture below. They
run for real in CI, where a Postgres service container is always available,
and will run locally too once Docker is installed.
"""

import copy
import csv
import itertools
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.ingestion.historical_client import HistoricalFPLClient
from packages.database.session import SessionLocal, engine
from packages.domain.ingestion import EndpointHealth
from packages.fpl_client import FetchResult, FPLClient
from packages.fpl_client.fingerprint import schema_signature, sha256_hex


def _database_reachable() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False


@pytest.fixture(scope="session")
def require_database():
    """Skip the test cleanly if Postgres isn't reachable."""
    if not _database_reachable():
        pytest.skip("Postgres not reachable — skipping integration tests")


@pytest.fixture
def db_session(require_database):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# --- FPL API fixture-data helpers (no live network access) ------------------

FPL_API_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fpl_api"

# Seeded from wall-clock time (not a fixed constant) so re-running the suite
# against the same persistent, non-ephemeral local Postgres across separate
# process invocations doesn't collide with a season label left over from a
# previous run — each pytest session gets its own practically-unique block of
# synthetic season years.
_season_year_counter = itertools.count(2100 + (int(time.time()) % 5000))


def load_bootstrap_json() -> dict:
    return json.loads((FPL_API_FIXTURES_DIR / "bootstrap_static_sample.json").read_text())


def load_fixtures_json() -> list:
    return json.loads((FPL_API_FIXTURES_DIR / "fixtures_sample.json").read_text())


def unique_bootstrap_json(base: dict | None = None) -> dict:
    """Return a bootstrap-static payload whose season label is fresh for this test run.

    Every ingestion integration test works against the same persistent local
    Postgres, so without this, two tests (or two runs of the suite) would
    collide on the same "2025-26" season derived from the fixture's real
    deadline dates. Shifting every event's deadline year to a fresh synthetic
    value keeps each test's rows fully isolated without needing to truncate
    tables between tests.
    """
    bootstrap = copy.deepcopy(base if base is not None else load_bootstrap_json())
    year = next(_season_year_counter)
    for event in bootstrap["events"]:
        event["deadline_time"] = f"{year}{event['deadline_time'][4:]}"
    return bootstrap


_OPTIMIZATION_POSITION_ELEMENT_TYPE = {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}


def build_optimization_bootstrap_json() -> dict:
    """Bootstrap-static payload with a clean, programmatically generated ~60-player pool.

    Starts from ``unique_bootstrap_json()`` (preserves the real
    ``teams``/``element_types``/``game_config``/``chips``/``events`` from the
    fixture sample and gives the season a fresh, collision-free label) but
    replaces ``elements`` entirely with a synthetic pool of 60 players
    spanning all 20 real teams (3 players/team, so the team_limit=3
    constraint is never even close to binding) and all 4 positions, with
    generous depth per position (10 GKP/18 DEF/20 MID/12 FWD) and varied
    ``now_cost``/``ep_next``/``status`` values.

    Costs are deliberately kept low enough that even the *most expensive*
    legal 15-player combination (2 GKP + 5 DEF + 5 MID + 3 FWD, each at this
    pool's max cost for its position) stays under the real GBP100.0m budget
    (``squad_total_spend=1000``) — so both optimizer jobs always have a
    feasible, comfortably-solvable candidate pool here, regardless of which
    players end up picked.
    """
    bootstrap = unique_bootstrap_json()

    position_plan = ["GKP"] * 10 + ["DEF"] * 18 + ["MID"] * 20 + ["FWD"] * 12
    assert len(position_plan) == 60

    base_cost_by_position = {"GKP": 42, "DEF": 42, "MID": 48, "FWD": 50}

    elements = []
    for index, position in enumerate(position_plan):
        team_id = (index % 20) + 1
        element_id = 30000 + index
        code = 930000 + index

        now_cost = base_cost_by_position[position] + (index % 7) * 3

        # Most players are fully available; a handful exercise the
        # availability-scaling ('d', partial chance) and hard-exclusion
        # ('i') projection paths, without starving any position of enough
        # remaining eligible depth to still be comfortably solvable.
        if index % 17 == 0:
            status, chance = "i", 0
        elif index % 13 == 0:
            status, chance = "d", 50
        else:
            status, chance = "a", None

        ep_next = round(3.0 + (index % 11) * 0.55, 1)

        elements.append(
            {
                "id": element_id,
                "code": code,
                "first_name": f"First{index}",
                "second_name": f"Last{index}",
                "web_name": f"Player{index}",
                "team": team_id,
                "element_type": _OPTIMIZATION_POSITION_ELEMENT_TYPE[position],
                "now_cost": now_cost,
                "status": status,
                "total_points": 20 + index,
                "minutes": 900 + index * 10,
                "goals_scored": index % 5,
                "assists": index % 4,
                "clean_sheets": index % 6,
                "goals_conceded": index % 8,
                "own_goals": 0,
                "penalties_saved": 0,
                "penalties_missed": 0,
                "yellow_cards": index % 3,
                "red_cards": 0,
                "saves": (index % 10) if position == "GKP" else 0,
                "bonus": index % 6,
                "bps": 100 + index,
                "selected_by_percent": f"{(index % 30) + 1}.0",
                "chance_of_playing_next_round": chance,
                "chance_of_playing_this_round": chance,
                "points_per_game": f"{2.0 + (index % 9) * 0.5:.1f}",
                "form": f"{1.0 + (index % 6) * 0.4:.1f}",
                "ep_this": f"{max(ep_next - 0.3, 0.0):.1f}",
                "ep_next": f"{ep_next:.1f}",
                "starts": 10 + (index % 20),
                "news": "",
            }
        )

    bootstrap["elements"] = elements
    return bootstrap


def make_fetch_result(endpoint: str, body, retrieved_at: datetime | None = None) -> FetchResult:
    """Build a FetchResult as if it came from a real, healthy FPLClient fetch."""
    retrieved_at = retrieved_at or datetime.now(UTC)
    raw_bytes = json.dumps(body).encode()
    return FetchResult(
        endpoint=endpoint,
        request_url=f"https://fantasy.premierleague.com/api/{endpoint}/",
        request_params=None,
        retrieved_at=retrieved_at,
        http_status=200,
        attempt_count=1,
        latency_ms=10,
        health_status=EndpointHealth.HEALTHY,
        checksum_sha256=sha256_hex(raw_bytes),
        schema_signature=schema_signature(body),
        raw_body=body,
        error_message=None,
    )


@pytest.fixture
def patched_fpl_client(monkeypatch):
    """Monkeypatch FPLClient so job handlers never hit the real network in tests.

    Returns a mutable ``state`` dict (``bootstrap``/``fixtures``/``event_status``)
    that a test can edit *before* calling the job handler again, to simulate a
    rules change or a player transfer between two ingestion runs.
    """
    state = {
        "bootstrap": unique_bootstrap_json(),
        "fixtures": load_fixtures_json(),
        "event_status": {"leagues": "updated"},
    }

    def fetch_bootstrap_static(self):
        return make_fetch_result("bootstrap_static", state["bootstrap"])

    def fetch_fixtures(self, event=None):
        endpoint = "fixtures_by_event" if event is not None else "fixtures"
        return make_fetch_result(endpoint, state["fixtures"])

    def fetch_event_status(self):
        return make_fetch_result("event_status", state["event_status"])

    monkeypatch.setattr(FPLClient, "fetch_bootstrap_static", fetch_bootstrap_static)
    monkeypatch.setattr(FPLClient, "fetch_fixtures", fetch_fixtures)
    monkeypatch.setattr(FPLClient, "fetch_event_status", fetch_event_status)

    return state


# --- Historical ingestion fixture-data helpers (no live network access) -----

HISTORICAL_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "historical"


def _load_historical_csv(path: Path) -> list[dict]:
    with path.open() as f:
        return list(csv.DictReader(f))


@pytest.fixture
def patched_historical_fpl_client(monkeypatch):
    """Monkeypatch HistoricalFPLClient so historical ingestion tests never hit the real network.

    Returns the committed ``tests/fixtures/historical/2023-24/`` CSV files
    (real, trimmed samples fetched from the actual live
    raw.githubusercontent.com source) regardless of which ``season_label``
    is requested — only one season's fixture data is committed.
    """
    season_dir = HISTORICAL_FIXTURES_DIR / "2023-24"
    file_bodies = {
        "teams": _load_historical_csv(season_dir / "teams.csv"),
        "players_raw": _load_historical_csv(season_dir / "players_raw.csv"),
        "fixtures": _load_historical_csv(season_dir / "fixtures.csv"),
        "merged_gw": _load_historical_csv(season_dir / "gws" / "merged_gw.csv"),
    }

    def fetch_all_season_files(self, season_label):
        return {
            file_stem: make_fetch_result(f"historical_{season_label}_{file_stem}", body)
            for file_stem, body in file_bodies.items()
        }

    monkeypatch.setattr(HistoricalFPLClient, "fetch_all_season_files", fetch_all_season_files)
    return file_bodies


@pytest.fixture
def patched_fpl_client_optimization(monkeypatch):
    """Like ``patched_fpl_client``, but seeded with the ~60-player optimization pool.

    Used by optimizer job integration tests: a DATA_INGESTION run against
    this fixture gives SQUAD_OPTIMIZATION/LINEUP_OPTIMIZATION a clean,
    comfortably-solvable candidate pool (see
    ``build_optimization_bootstrap_json``) instead of the 10-row, mostly
    intentionally-quarantined default sample.
    """
    state = {
        "bootstrap": build_optimization_bootstrap_json(),
        "fixtures": load_fixtures_json(),
        "event_status": {"leagues": "updated"},
    }

    def fetch_bootstrap_static(self):
        return make_fetch_result("bootstrap_static", state["bootstrap"])

    def fetch_fixtures(self, event=None):
        endpoint = "fixtures_by_event" if event is not None else "fixtures"
        return make_fetch_result(endpoint, state["fixtures"])

    def fetch_event_status(self):
        return make_fetch_result("event_status", state["event_status"])

    monkeypatch.setattr(FPLClient, "fetch_bootstrap_static", fetch_bootstrap_static)
    monkeypatch.setattr(FPLClient, "fetch_fixtures", fetch_fixtures)
    monkeypatch.setattr(FPLClient, "fetch_event_status", fetch_event_status)

    return state


# --- Manager-facing fixture data (Milestone 4 Layer 1, no live network access) -----

MANAGER_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fpl_api" / "manager"


def load_manager_entry_json() -> dict:
    return json.loads((MANAGER_FIXTURES_DIR / "entry.json").read_text())


def load_manager_history_json() -> dict:
    return json.loads((MANAGER_FIXTURES_DIR / "history.json").read_text())


def load_manager_picks_json() -> dict:
    return json.loads((MANAGER_FIXTURES_DIR / "picks.json").read_text())


def load_manager_transfers_json() -> list:
    return json.loads((MANAGER_FIXTURES_DIR / "transfers.json").read_text())


@pytest.fixture
def patched_fpl_client_manager(monkeypatch):
    """Monkeypatch FPLClient's manager-facing methods, serving the committed manager fixture files.

    Mirrors ``patched_fpl_client_optimization``'s exact monkeypatch style.
    Returns a mutable ``state`` dict (``entry``/``history``/``picks``/``transfers``)
    a test can edit before calling MANAGER_SYNC again.
    """
    state = {
        "entry": load_manager_entry_json(),
        "history": load_manager_history_json(),
        "picks": load_manager_picks_json(),
        "transfers": load_manager_transfers_json(),
    }

    def fetch_manager_entry(self, manager_id):
        return make_fetch_result("manager_entry", state["entry"])

    def fetch_manager_history(self, manager_id):
        return make_fetch_result("manager_history", state["history"])

    def fetch_manager_picks(self, manager_id, event):
        return make_fetch_result("manager_picks", state["picks"])

    def fetch_manager_transfers(self, manager_id):
        return make_fetch_result("manager_transfers", state["transfers"])

    monkeypatch.setattr(FPLClient, "fetch_manager_entry", fetch_manager_entry)
    monkeypatch.setattr(FPLClient, "fetch_manager_history", fetch_manager_history)
    monkeypatch.setattr(FPLClient, "fetch_manager_picks", fetch_manager_picks)
    monkeypatch.setattr(FPLClient, "fetch_manager_transfers", fetch_manager_transfers)

    return state


# --- H2H league fixture data (Milestone 5 Layer 1, no live network access) -----

LEAGUE_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fpl_api" / "league"

# Synthetic 4-entry H2H league: entry A is "self" (excluded via
# exclude_fpl_manager_id in tests that model the calling user), B/C/D are
# rivals exercising the three PicksAvailability outcomes.
LEAGUE_SELF_FPL_MANAGER_ID = 2001
LEAGUE_RIVAL_B_FPL_MANAGER_ID = 2002  # picks resolve cleanly (HTTP 200)
LEAGUE_RIVAL_C_FPL_MANAGER_ID = 2003  # picks fetch returns a clean HTTP 404
LEAGUE_RIVAL_D_FPL_MANAGER_ID = 2004  # picks fetch raises (simulated client-level failure)


def load_league_standings_json() -> dict:
    return json.loads((LEAGUE_FIXTURES_DIR / "standings.json").read_text())


def load_league_matches_json() -> dict:
    return json.loads((LEAGUE_FIXTURES_DIR / "matches.json").read_text())


def load_league_picks_b_json() -> dict:
    return json.loads((LEAGUE_FIXTURES_DIR / "picks_b.json").read_text())


def _not_found_fetch_result(manager_id: int, event: int) -> FetchResult:
    return FetchResult(
        endpoint="manager_picks",
        request_url=f"https://fantasy.premierleague.com/api/entry/{manager_id}/event/{event}/picks/",
        request_params=None,
        retrieved_at=datetime.now(UTC),
        http_status=404,
        attempt_count=1,
        latency_ms=10,
        health_status=EndpointHealth.BLOCKED,
        checksum_sha256=None,
        schema_signature=None,
        raw_body=None,
        error_message="http_status_404",
    )


def _not_found_history_fetch_result(manager_id: int) -> FetchResult:
    return FetchResult(
        endpoint="manager_history",
        request_url=f"https://fantasy.premierleague.com/api/entry/{manager_id}/history/",
        request_params=None,
        retrieved_at=datetime.now(UTC),
        http_status=404,
        attempt_count=1,
        latency_ms=10,
        health_status=EndpointHealth.BLOCKED,
        checksum_sha256=None,
        schema_signature=None,
        raw_body=None,
        error_message="http_status_404",
    )


@pytest.fixture
def patched_fpl_client_league(monkeypatch):
    """Monkeypatch FPLClient's H2H-league-facing methods, serving the committed league fixture files.

    Mirrors ``patched_fpl_client_manager``'s exact monkeypatch style. Entry B
    (``LEAGUE_RIVAL_B_FPL_MANAGER_ID``)'s picks resolve cleanly; entry C's
    picks fetch returns a clean HTTP 404 (``UNAVAILABLE_PRIVATE_OR_NOT_RELEASED``);
    entry D's picks fetch raises, simulating an unexpected client-level
    failure (``FETCH_FAILED``) — see ``app.jobs.handlers.league_sync``'s own
    per-rival failure-isolation docstring.

    ``fetch_manager_history`` (Milestone 5 Layer 2) is mocked independently
    of the picks outcome above, exactly matching Layer 2's own "reuse
    whatever history exists regardless of this run's picks-fetch outcome"
    discipline: entry C's picks are unavailable but its *history* fetch still
    succeeds here (a deliberate, meaningful test case), entry D's history
    fetch raises (simulating an unexpected client-level failure distinct
    from its picks failure), and every other manager id (B, self) gets a
    real, richer 5-gameweek history (reusing the same committed
    ``tests/fixtures/fpl_api/manager/history.json`` Milestone 4 fixture).
    """
    state = {
        "standings": load_league_standings_json(),
        "matches": load_league_matches_json(),
        "picks_b": load_league_picks_b_json(),
        "history": load_manager_history_json(),
    }

    def fetch_h2h_league_standings(self, league_id, page=1):
        return make_fetch_result("h2h_league_standings", state["standings"])

    def fetch_h2h_matches(self, league_id, event=None, entry=None, page=1):
        return make_fetch_result("h2h_league_matches", state["matches"])

    def fetch_manager_picks(self, manager_id, event):
        if manager_id == LEAGUE_RIVAL_B_FPL_MANAGER_ID:
            return make_fetch_result("manager_picks", state["picks_b"])
        if manager_id == LEAGUE_RIVAL_C_FPL_MANAGER_ID:
            return _not_found_fetch_result(manager_id, event)
        if manager_id == LEAGUE_RIVAL_D_FPL_MANAGER_ID:
            raise RuntimeError("simulated rival picks fetch failure")
        raise AssertionError(f"unexpected manager_id {manager_id} in patched_fpl_client_league")

    def fetch_manager_history(self, manager_id):
        if manager_id == LEAGUE_RIVAL_D_FPL_MANAGER_ID:
            raise RuntimeError("simulated rival history fetch failure")
        if manager_id == LEAGUE_RIVAL_C_FPL_MANAGER_ID:
            return _not_found_history_fetch_result(manager_id)
        return make_fetch_result("manager_history", state["history"])

    monkeypatch.setattr(FPLClient, "fetch_h2h_league_standings", fetch_h2h_league_standings)
    monkeypatch.setattr(FPLClient, "fetch_h2h_matches", fetch_h2h_matches)
    monkeypatch.setattr(FPLClient, "fetch_manager_picks", fetch_manager_picks)
    monkeypatch.setattr(FPLClient, "fetch_manager_history", fetch_manager_history)

    return state


@pytest.fixture
def patched_fpl_client_optimization_with_active_gameweek(monkeypatch):
    """Like patched_fpl_client_optimization, but with one gameweek left unfinished.

    Milestone 3 Layer 4's champion-aware projection path
    (app.optimization.projection.project_player_with_champion) only engages
    once the optimizer resolves a concrete target gameweek (see
    app.optimization.gameweek_resolution) -- the plain
    patched_fpl_client_optimization fixture's bootstrap sample has every
    gameweek finished (a realistic close-season snapshot, which is exactly
    why its own tests assert NO_ACTIVE_GAMEWEEK). This variant marks the last
    event unfinished so champion-aware optimizer integration tests can
    exercise the live projection path end-to-end.
    """
    bootstrap = build_optimization_bootstrap_json()
    for event in bootstrap["events"]:
        event["is_current"] = False
    bootstrap["events"][-1]["finished"] = False
    bootstrap["events"][-1]["is_current"] = True

    state = {
        "bootstrap": bootstrap,
        "fixtures": load_fixtures_json(),
        "event_status": {"leagues": "updated"},
    }

    def fetch_bootstrap_static(self):
        return make_fetch_result("bootstrap_static", state["bootstrap"])

    def fetch_fixtures(self, event=None):
        endpoint = "fixtures_by_event" if event is not None else "fixtures"
        return make_fetch_result(endpoint, state["fixtures"])

    def fetch_event_status(self):
        return make_fetch_result("event_status", state["event_status"])

    monkeypatch.setattr(FPLClient, "fetch_bootstrap_static", fetch_bootstrap_static)
    monkeypatch.setattr(FPLClient, "fetch_fixtures", fetch_fixtures)
    monkeypatch.setattr(FPLClient, "fetch_event_status", fetch_event_status)

    return state
