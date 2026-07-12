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
