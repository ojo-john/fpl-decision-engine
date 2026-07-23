# FPL Decision Engine

An autonomous decision-support system for a single Fantasy Premier League
(FPL) manager competing in a private head-to-head (H2H) league. It is
intended to eventually ingest player/fixture data, model expected points,
optimize squad/transfer/chip decisions, simulate H2H matchups, and explain
its recommendations — learning recursively from how past recommendations
actually performed.

**This repository is currently at Milestone 1 ("Rules and Data Ingestion").**
Milestone 0 (repo scaffolding, job runner framework, `system_health` job,
Docker Compose, CI workflows) is done. Milestone 1 adds: an FPL API client,
a rules-versioning system (bootstrap-static as the authoritative rules
source), data ingestion + normalization (teams/positions/players/gameweeks/
fixtures), a data quality agent, and the `rules_sync`/`data_ingestion` job
handlers. Every other subsystem (`app/modeling`, `app/optimization`,
`app/transfers`, `app/h2h`, etc., plus the frontend) is still an empty
placeholder package — those land in later milestones.

## Architecture overview

```
apps/api/        FastAPI app -- auth, CORS, and the full REST surface over every domain job
apps/web/        Next.js dashboard (Milestone 8): login, read-only pages, and a few interactive actions
app/*/           Domain subpackages — placeholders except app/jobs, app/rules, app/data_quality, app/ingestion
app/jobs/        Job runner framework: JobRunner ABC, registry, execute_job service
app/rules/       Rules extraction/diff/versioning (bootstrap-static -> RulesConfigSnapshot)
app/data_quality/ Pure DQ check functions + score/optimization-gate computation
app/ingestion/   Normalization (upserts) + full ingestion orchestration + report rendering
packages/domain/ Framework-agnostic enums shared across the app (JobType, RuleConfidence, EndpointHealth, ...)
packages/database/ SQLAlchemy Base, engine/session, health check, ORM models
packages/fpl_client/ Resilient FPL public API HTTP adapter (retries, health classification, fingerprinting)
packages/shared/ Settings (pydantic-settings) and JSON logging setup
migrations/      Alembic migrations (hand-written)
config/          YAML config placeholders for future milestones
scripts/run_job.py  CLI to execute a single job synchronously
tests/           unit (no DB) / integration (real Postgres) / contract & e2e (stubs)
tests/fixtures/fpl_api/ Trimmed, committed FPL API samples used by tests (never live network)
.github/workflows/  ci.yml, health.yml, ingest.yml, scheduled-dispatcher.yml
```

Persistence is Postgres via **synchronous SQLAlchemy 2.0** (`psycopg[binary]`
driver) plus **Alembic** for migrations — no async ORM, no ORM abstraction
beyond SQLAlchemy itself. Dependency management is **[uv](https://docs.astral.sh/uv/)**;
there is no packaging/build backend configured — this project runs from the
repo root via `uv run`, it is not installed as a distributable package.

## Local setup

Requires Python 3.12 (this repo pins `3.12.13` via `.python-version` /
`uv python pin`) and `uv`.

```bash
git clone <this-repo>
cd fpl-decision-engine
make setup      # installs uv if missing, `uv sync`, copies .env.example -> .env
make migrate    # requires Postgres running — see Docker setup below
make run        # starts the API on http://localhost:8000
```

`make setup` copies `.env.example` to `.env` if `.env` doesn't already exist
— edit `.env` to change local Postgres credentials if needed.

## Docker setup

```bash
cp .env.example .env   # if not already done
docker compose up
```

This starts three containers: `postgres:16-alpine`, the API (with live
reload), and the web dashboard (production build) -- `web` depends on `api`
passing its healthcheck, which depends on `postgres` passing its own. Once
up: `curl http://localhost:8000/health`, `curl http://localhost:8000/health/db`,
and open `http://localhost:3000` (log in with `APP_SHARED_TOKEN` from your
`.env` -- the committed `.env.example` default is `dev-token-change-me`).

## Web dashboard (apps/web)

Next.js (App Router, TypeScript), single shared-secret login (see
`apps/api/auth.py`'s own docstring for why a full multi-user auth system
would be over-engineering here), talking to the API above. Local, non-Docker
setup:

```bash
cd apps/web
cp .env.example .env.local   # APP_SHARED_TOKEN must match the API's own value
npm install
npm run dev                   # http://localhost:3000, API must already be running
```

Two things worth knowing:

- `lib/api-types.ts` is generated from the API's own OpenAPI schema, not
  hand-written -- regenerate it after changing any `apps/api` request/response
  schema:
  ```bash
  # with the API running on :8000
  uv run python -c "import json; from apps.api.main import app; json.dump(app.openapi(), open('openapi.json', 'w'))"
  cd apps/web && npm run generate-api-types
  ```
- The browser never holds `APP_SHARED_TOKEN` directly. Read-only pages are
  Server Components that call the API directly (server-to-server); the few
  interactive actions (buttons/forms) go through `apps/web`'s own
  `/api/proxy/*` route, which attaches the real token server-side.

```bash
cd apps/web
npm run lint
npm run typecheck
npm run build
```

## Database setup

Locally without Docker, point `DATABASE_URL` in `.env` at any reachable
Postgres 16 instance, then:

```bash
uv run alembic upgrade head
```

## Rules versioning and data ingestion

`https://fantasy.premierleague.com/help/rules` is a client-rendered JS SPA
with no server-side rules content, so this repo does **not** scrape it.
Instead, `bootstrap-static`'s `game_config.rules`/`game_config.scoring`
objects (plus top-level `chips`, `element_types`, `events`) are treated as
the primary, authoritative, machine-readable rules source — they come
straight from FPL's own game engine, are structured, and are versioned by
this codebase every time they change (`rules_config`/`rules_config_fields`
tables, one immutable version per detected change, diffed against the
previous version — see `app/rules/`).

One confirmed gap: the "-4 points per transfer beyond free transfers" hit
cost, and the mechanical *effects* of each chip (wildcard/free hit/bench
boost/triple captain — their scheduling windows ARE in bootstrap's `chips[]`,
but not what they *do*) are not present anywhere in the structured API data.
These are hand-maintained constants (`app/rules/constants.py`) tagged
`PROVISIONAL` confidence — never treated as equally authoritative as
API-sourced (`CONFIRMED`) fields. `RulesStatus` (`ok`/`degraded`/`blocked`)
reflects whether the current rules config is trustworthy enough to build on:
`blocked` if a squad-legality-critical field is missing/inconsistent or the
bootstrap fetch failed with no prior confirmed config to fall back on;
`degraded` if the fetch was flaky or the config is >48h stale (never blocks
by itself, just a caution signal); `ok` otherwise.

Data ingestion (`app/ingestion/`) fetches `bootstrap-static`, `fixtures`, and
`event-status`, normalizes teams/positions/players/gameweeks/fixtures into
their own tables (upserted, keyed by `(season_id, fpl_*_id)`), and runs a
data quality agent (`app/data_quality/`) that scores the run 0-100 and
computes `optimization_permitted` — a hard gate: any `CRITICAL` issue, a
`BLOCKED` rules status, or `BLOCKED` overall endpoint health vetoes it
outright regardless of score; otherwise it requires `score >= 70`. Row-level
bad data (a bad player or fixture) is quarantined (`is_quarantined=True`,
never silently dropped) rather than blocking the whole run; row-level bad
data at the *team*/*gameweek* level (wrong team count, two "current"
gameweeks) is a run-level `CRITICAL` issue instead, since that indicates a
systemic ingestion failure, not one bad row.
`player_fixture_stats` is explicitly **deferred** — it needs the
`/api/event/{gw}/live/` endpoint, out of scope for this milestone.

Run it locally:

```bash
make ingest   # equivalent to: uv run python scripts/run_job.py --job-type data_ingestion --requested-by local --trigger-type local
```

This prints a JSON job summary to stdout and additionally writes the full
Markdown ingestion report to `reports/ingestion/{ingestion_run_id}.md` (this
directory is gitignored — generated, not source; the durable copy of the
report lives in the `ingestion_runs.report_markdown` DB column).

### Live API smoke check (manual, local-only — never run by CI or tests)

Every automated test (`tests/unit`, `tests/integration`) runs against
monkeypatched `FPLClient` methods backed by the trimmed, committed fixtures
in `tests/fixtures/fpl_api/` — no test ever makes a real network call. To
sanity-check the real `FPLClient` against the live FPL API by hand:

```bash
uv run python - <<'EOF'
from packages.fpl_client import FPLClient

client = FPLClient()
for label, result in [
    ("bootstrap-static", client.fetch_bootstrap_static()),
    ("fixtures", client.fetch_fixtures()),
    ("event-status", client.fetch_event_status()),
]:
    print(label, result.health_status, result.http_status, result.attempt_count, result.latency_ms, "ms")
client.close()
EOF
```

Or, for a full end-to-end local run against the live API (writes to your
local Postgres): `make ingest`.

## Environment variables

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `POSTGRES_USER` | Docker Compose only | — | Postgres container user |
| `POSTGRES_PASSWORD` | Docker Compose only | — | Postgres container password |
| `POSTGRES_DB` | Docker Compose only | — | Postgres container database name |
| `DATABASE_URL` | Yes | — | Full SQLAlchemy URL, e.g. `postgresql+psycopg://fpl:changeme@localhost:5432/fpl_decision_engine`. Use `@postgres:5432` instead of `@localhost:5432` when running via Docker Compose (handled automatically by `docker-compose.yml`'s `api` service). |
| `APPLICATION_SECRET_KEY` | No | `dev-secret-change-me` | Not yet used for anything security-sensitive. |
| `LOG_LEVEL` | No | `INFO` | Root logger level. |
| `ENVIRONMENT` | No | `development` | Free-form environment label. |
| `FPL_MANAGER_ID` | No | `None` | Public numeric FPL manager ID. Not a secret. Unused until a later milestone. |
| `FPL_H2H_LEAGUE_ID` | No | `None` | Public numeric FPL H2H league ID. Not a secret. Unused until a later milestone. |
| `APP_SHARED_TOKEN` | No | `dev-token-change-me` | Gates every non-`/health` API endpoint (see `apps/api/auth.py`) and is `apps/web`'s login password / Bearer token in one. **Must be overridden to a real random value for anything beyond local Docker Compose.** |
| `CORS_ALLOWED_ORIGINS` | No | `` (empty, fail-closed) | Comma-separated browser origins allowed to call the API directly. Not needed by `apps/web` itself (it always talks to the API server-to-server, never from the browser) -- only relevant if something else calls the API from a browser. |

`apps/web` has its own env file, `apps/web/.env.example` (`APP_SHARED_TOKEN`
— **must match** the API's own value above — and `API_BASE_URL`, the API's
reachable base URL: `http://localhost:8000` for local `npm run dev`,
`http://api:8000` inside Docker Compose).

## GitHub setup

**No GitHub secrets are required.** All four workflows (`ci.yml`,
`health.yml`, `ingest.yml`, `scheduled-dispatcher.yml`) spin up their own
ephemeral, CI-local Postgres service container with fixed, non-sensitive
placeholder credentials — there is no persistent hosted database yet for
them to need real credentials for. `ingest.yml` and every ingestion job
handler still never touch the real FPL API in CI in a way that could leak
anything sensitive — there is nothing sensitive in these URLs (they're all
public, unauthenticated endpoints).

When a later milestone adds real FPL identifiers, set `FPL_MANAGER_ID` and
`FPL_H2H_LEAGUE_ID` as GitHub Actions **variables** (not secrets) — they are
public numeric IDs, not sensitive.

To trigger the health check workflow manually:

```bash
gh workflow run health.yml
# or with an explicit requester:
gh workflow run health.yml -f requested_by=yourname
```

...or via the GitHub Actions UI ("Run workflow" button on the `Health Check`
workflow).

### Running ingestion via GitHub Actions

`ingest.yml` is `workflow_dispatch`-only (never scheduled directly) and
supports both job types via its `job_type` input:

```bash
gh workflow run ingest.yml -f job_type=data_ingestion
gh workflow run ingest.yml -f job_type=rules_sync
# both accept an optional explicit requester too:
gh workflow run ingest.yml -f job_type=data_ingestion -f requested_by=yourname
```

The run's full Markdown ingestion report is printed into the workflow's job
summary and uploaded as a `job-summary` build artifact (same pattern as
`health.yml`).

### How the scheduled dispatcher works

`scheduled-dispatcher.yml` now dispatches `data_ingestion` (not
`system_health`) every 4 hours (cron `0 */4 * * *` — still an explicit
placeholder cadence, not real deadline-aware scheduling, which lands in a
later milestone). Each run stands up an ephemeral Postgres container,
applies migrations, then calls `scripts/run_job.py` **twice** with the same
idempotency key (derived from the current UTC 4-hour bucket, e.g.
`scheduled-ingest-2026-07-09T-3` for the 12:00-15:59 UTC window), and asserts
the two calls returned the same `job_id` — proving the idempotency mechanism
actually prevents duplicate `jobs` rows, not just in theory. Note this is
`jobs`-table idempotency only: the underlying `ingestion_runs` row is never
deduplicated this way (see "Rules versioning and data ingestion" above) —
the second `scripts/run_job.py` call here never even reaches the handler,
since `execute_job` short-circuits on the matching idempotency key before
invoking it.

## Running tests

```bash
make test              # unit tests only — no DB needed
make test-integration  # integration tests — needs Postgres (Docker or CI service container)
```

Integration tests skip cleanly (with a clear message) if Postgres isn't
reachable, rather than failing the whole suite — useful locally before
Docker is installed.

## Troubleshooting

**pyenv + non-interactive shells:** if you're scripting against this repo
(e.g. from CI runners emulated locally, `bash -c '...'`, cron, or another
tool that invokes commands via a non-interactive, non-login shell), be aware
that **`.zshrc` (and pyenv's shim setup within it) is not sourced by
non-interactive shells**. This means `python3`/`pip` may silently resolve to
the system Python instead of the pyenv-managed 3.12.13, or `uv`/`gitleaks`
may not be found even though they're installed in `~/.local/bin`. Fix by
either invoking binaries with full paths (`~/.pyenv/versions/3.12.13/bin/python3`,
`~/.local/bin/uv`) or explicitly exporting `PATH` first:

```bash
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/versions/3.12.13/bin:$HOME/.local/bin:$PATH"
```

`uv` itself pins the Python version via `.python-version` (`3.12.13`), so
`uv run`/`uv sync` are deterministic regardless of shell type, once `uv`
itself is on `PATH`.

**PuLP's bundled CBC solver on Apple Silicon:** the squad/lineup optimizer
(`app/optimization/squad_solver.py`, `lineup_solver.py`) uses PuLP, which
ships a bundled CBC solver binary per platform. As of the PuLP version this
project pins, that bundled binary for macOS is **x86_64 only** — there is no
native `arm64` build (Linux does have one, which is why this is not an issue
in CI, which runs on `ubuntu-latest`). On an Apple Silicon Mac without
Rosetta 2, running `SQUAD_OPTIMIZATION`/`LINEUP_OPTIMIZATION` fails with:

```
[Errno 86] Bad CPU type in executable: '.../pulp/solverdir/cbc/osx/i64/cbc'
```

Fix (one-time, no admin password required beyond the license prompt):

```bash
softwareupdate --install-rosetta --agree-to-license
```

This is unrelated to any bug in this codebase — it's a one-time local
machine-setup step for Apple Silicon, exactly like installing Docker Desktop.
