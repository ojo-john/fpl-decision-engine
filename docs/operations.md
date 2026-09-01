# Operations

This document covers day-to-day operational tasks for the FPL Decision
Engine. As of Milestone 1, scope covers the job runner, database migrations,
the CI/health/ingest workflows, and data ingestion + rules versioning. It
will grow further as modeling, optimization, and the frontend come online.

**Note (2026-09-01): this document is known-stale** (see the project's own
CLAUDE.md) — much of it still describes a Milestone-1 state where only
`system_health`/`rules_sync`/`data_ingestion` had handlers. `ARCHITECTURE.md`
is the maintained ground truth; verify anything here against the real code
before relying on it.

## Rerunning a failed job

Every job is tracked as a row in the `jobs` table (see
`packages/database/models/job.py`). To rerun a job manually:

```bash
uv run python scripts/run_job.py \
  --job-type system_health \
  --requested-by <your-name> \
  --trigger-type local

# or, to rerun ingestion:
uv run python scripts/run_job.py \
  --job-type data_ingestion \
  --requested-by <your-name> \
  --trigger-type local
```

Supported `--trigger-type` values: `local`, `api`, `web`, `github_manual`,
`github_schedule`, `retry`. Pass `--idempotency-key <key>` to make a run
idempotent (a second call with the same key returns the existing job instead
of creating a new one). Pass `--git-commit-sha <sha>` to record which commit
produced the run (used by CI workflows).

`system_health`, `rules_sync`, and `data_ingestion` have implemented
handlers. Every other `JobType` will raise `JobHandlerNotImplementedError` —
that's expected until the corresponding milestone implements it.

## Ingestion runs, rules status, and the optimization gate

Every `rules_sync`/`data_ingestion` run is tracked as a row in
`ingestion_runs` (separate from — but linked to via `job_id` — the `jobs`
row that triggered it). Key fields to check when something looks wrong:

- `ingestion_runs.status`: `running` / `succeeded` / `succeeded_with_warnings`
  / `failed`. Unlike `system_health`, `data_ingestion` is intentionally
  **not** idempotency-key-deduped at this level — every run gets a genuinely
  new `ingestion_runs` row, since it's meant to be a full audit trail of
  every ingestion attempt, not a cache of "current state". (The `jobs` row
  above it can still be idempotency-keyed as usual — see
  `tests/integration/test_data_ingestion_idempotency.py`.)
- `ingestion_runs.overall_health`: the worst `EndpointHealth`
  (`healthy`/`degraded`/`blocked`) across bootstrap-static/fixtures/
  event-status for that run.
- `ingestion_runs.data_quality_score` (0-100) and
  `ingestion_runs.optimization_permitted` (bool): see
  `app/data_quality/scoring.py`. Any `CRITICAL` data quality issue, a
  `BLOCKED` `rules_status`, or `BLOCKED` `overall_health` vetoes
  `optimization_permitted` outright regardless of score; otherwise it
  requires `score >= 70`. Downstream milestones (optimizer, recommendations)
  must check this flag before acting on ingested data.
- `ingestion_runs.report_markdown`: the full human-readable report (endpoint
  health table, rules changes, normalization summary, data quality issues by
  severity) — this is the fastest way to diagnose a bad run.
- `rules_config.rules_status` (`ok`/`degraded`/`blocked`): whether the
  *current* rules configuration itself is trustworthy — see "Rules
  versioning" in the README for what triggers each state. Query via
  `app.rules.status.get_current_rules_status(session, season_id)`.
- `data_quality_issues`: one row per check failure for a run, with
  `severity`/`entity_type`/`entity_fpl_id`/`check_name`/`message`. Row-level
  `ERROR`-severity issues (bad player/fixture data) also result in that row
  being persisted with `is_quarantined=True` — always filter active data
  with `WHERE NOT is_quarantined`.

## Where logs live

There is no log aggregation system yet. All application and job output is
structured JSON written to stdout (see `packages/shared/logging.py`). In
GitHub Actions, this means job logs are visible directly in the workflow run
output; `health.yml` and `ingest.yml` additionally write a Markdown summary
via `$GITHUB_STEP_SUMMARY` (for `ingest.yml`, this includes the full
ingestion/rules-change report) and upload `job_summary.json` as a build
artifact. Locally, `make ingest` also writes the same report Markdown to
`reports/ingestion/{ingestion_run_id}.md` (gitignored — generated, not
source; the durable copy is the `ingestion_runs.report_markdown` DB column).

## Running ingestion manually

```bash
make ingest                        # local data_ingestion run
gh workflow run ingest.yml -f job_type=data_ingestion
gh workflow run ingest.yml -f job_type=rules_sync
```

See the README's "Rules versioning and data ingestion" section for what each
job does, and "Ingestion runs, rules status, and the optimization gate" above
for how to interpret the result.

### Live FPL API smoke check (manual/local only)

Tests never hit the real FPL API — they run against monkeypatched
`FPLClient` methods backed by the committed fixtures in
`tests/fixtures/fpl_api/`. To sanity-check the real client against the live
API by hand (never run this from CI):

```bash
uv run python - <<'EOF'
from packages.fpl_client import FPLClient

client = FPLClient()
for label, fetch in [
    ("bootstrap-static", client.fetch_bootstrap_static),
    ("fixtures", client.fetch_fixtures),
    ("event-status", client.fetch_event_status),
]:
    result = fetch()
    print(label, result.health_status, result.http_status, result.attempt_count, f"{result.latency_ms}ms")
client.close()
EOF
```

### CI self-test workflows (superseded)

The old `scheduled-dispatcher.yml` CI self-test (ephemeral Postgres,
4-hour-bucketed idempotency key proof for `data_ingestion`) was removed
2026-09-01 once it became fully redundant with
`tests/integration/test_job_idempotency.py`/
`test_data_ingestion_idempotency.py`, which already run on every push via
`ci.yml`. `ingest.yml` still spins up its OWN empty, throwaway Postgres
service container inside the GitHub Actions runner — it has never touched,
and structurally cannot touch, the real local database
`docker-compose.yml`'s own `postgres` service serves. For real automation
against a real database, see the next section and `ARCHITECTURE.md`'s
"Cloud deployment" section (`scheduler-tick.yml`).

### Local scheduler — the real automation (`scripts/scheduler.py`)

Starts automatically as the `scheduler` service the moment you run `docker compose up` (reuses
the `api` service's own image, no extra build) — as long as your Docker stack is running, it:

- Reviews a gameweek automatically the moment it finishes and hasn't been reviewed yet
  (`GAMEWEEK_REVIEW`, checked every 15 minutes, fires at most once per gameweek).
- Syncs your league's real rival data every 6 hours (`LEAGUE_SYNC`).
- Refreshes live player/price/status data every 4 hours (`DATA_INGESTION`) — the real equivalent
  of what the old `scheduled-dispatcher.yml` CI self-test only ever simulated.

To opt out of background automation: `docker compose stop scheduler` after bringing the rest of
the stack up, or comment the `scheduler` service block out of `docker-compose.yml` entirely. The
manual "sync"/"review" buttons already in the web dashboard (see below) keep working exactly the
same either way — this is purely additive, on-demand triggering was never removed.

**Limitation, stated plainly**: this only runs while your own machine is on and Docker is up. It
is a deliberate, zero-hosting-decision substitute for real automation, not a replacement for
actually deploying this app somewhere with a real, always-on, publicly-reachable database. That
decision has since been made — see `ARCHITECTURE.md`'s "Cloud deployment" section: `scheduler-tick.yml`
now runs `run_tick()` directly against the real production database via `PROD_DATABASE_URL`,
scheduled every 15 minutes.

## Running / rolling back a migration

```bash
# Apply all pending migrations
uv run alembic upgrade head

# Roll back the most recent migration
uv run alembic downgrade -1

# Roll back everything
uv run alembic downgrade base
```

Migrations live in `migrations/versions/`. The Alembic environment
(`migrations/env.py`) derives the database URL at runtime from
`packages.shared.config.get_settings().database_url` — never hardcode a URL
in `alembic.ini` or a migration file.

## Web dashboard (apps/web)

See the README's "Web dashboard" section for local setup. Operationally:

- `docker compose logs web` / `docker compose logs api` -- both containers
  log structured JSON to stdout, same convention as every other job.
- If `apps/web` can't log in or every page 500s, first check
  `APP_SHARED_TOKEN` actually matches between the two `.env` files
  (`.env`/`apps/web/.env.local` locally, or the two containers' resolved
  environments under Docker Compose) -- a mismatch here is the most likely
  cause, not a code bug.
- `apps/web/lib/api-types.ts` is generated, not hand-written -- if a page
  shows a TypeScript error about a field that clearly exists on the API
  response, regenerate it (see the README) rather than hand-editing the
  generated file.

## Post-deadline real-data verification

Six features have only ever been exercised against `tests/`'s committed
fixtures/synthetic seasons (see this repo's `CLAUDE.md` — the test suite
never hits the real FPL API or a live season): `MANAGER_SYNC`'s squad fetch,
the user's own chip-inventory derivation (`app.chips.inventory`),
`LEAGUE_SYNC`'s standings/matches sync, rival-profile derivation
(`app.h2h.rival_state`), rival picks-based squad differentials
(`app.h2h.differential`), and price-signal threshold calibration
(`app.players.price_signal_calibration`). Each depends on a real,
FPL-platform-imposed precondition that a synthetic fixture can't reproduce:

| Feature | Real precondition | Confirmed by |
|---|---|---|
| `MANAGER_SYNC` squad fetch | The gameweek's own deadline has passed — FPL withholds every manager's picks (own included) until then, live-confirmed against `/entry/{id}/event/{event}/picks/` | `app/jobs/handlers/manager_sync.py` module docstring |
| Chip inventory (`app.chips.inventory`) | A current `ManagerSquadSnapshot` exists — i.e. `MANAGER_SYNC` above has succeeded once | derived directly |
| `LEAGUE_SYNC` standings/entries | The season has started AND at least one gameweek has finished — FPL's `leagues-h2h/{id}/standings/` returns zero `standings.results` before that (joined managers sit under `new_entries` instead) | `app/jobs/handlers/league_sync.py` module docstring; also documented in `ARCHITECTURE.md`'s "League Watch" section |
| Rival profile computation | Same as `LEAGUE_SYNC` standings above — Layer 2 only computes a profile per standings entry row | derived directly |
| Rival squad differentials (`app.h2h.differential`) | Both the user's own squad (`MANAGER_SYNC`) AND at least one rival's picks for a finished gameweek (`LEAGUE_SYNC`) | derived directly |
| Price-signal calibration (`app.players.price_signal_calibration`) | At least `MIN_CALIBRATION_SAMPLE_SIZE` (30) real (signal, price-change) pairs — needs real price movements across a live season, accumulated via repeated `DATA_INGESTION` runs | module docstring |

### Running the smoke test

```bash
# Real dev DB (never .env.test) and the real, live FPL API -- pydantic-settings
# loads .env automatically (packages/shared/config.py's env_file=".env"), no
# manual `source` needed as long as .env exists in the repo root you're running from.
uv run python scripts/post_deadline_smoke_test.py
```

Runs six checks (one per feature above) against the real dev database and
prints one of three verdicts per check:

- **PASS** — the real precondition is met and the feature produced real,
  sane output.
- **PENDING** — the real precondition genuinely hasn't happened yet (expected
  pre-season/pre-deadline state, not a failure). Every check names exactly
  which precondition is still outstanding.
- **FAIL** — the precondition IS met and the feature still broke. This is
  the only outcome that should ever fail CI/block a release; the script's
  exit code is `0` unless at least one check is `FAIL`.

`MANAGER_SYNC`/`LEAGUE_SYNC` checks execute the real job (same
`app.jobs.service.execute_job` path as `scripts/run_job.py`) with a
same-day idempotency key, so rerunning the script multiple times in one day
returns the already-recorded result instead of re-hitting the live FPL API
on every invocation. Defaults to this app's own real manager (`918600`) and
H2H league (`814682`, "Pro Clubs '27") — override with `--fpl-manager-id`/
`--fpl-league-id` if verifying a different manager/league.

### What to actually check once GW1's deadline (2026-08-21 17:30 UTC) has passed

1. Run the smoke test. `manager_sync` and `chip_inventory` should flip to
   PASS immediately (the deadline is the only precondition for both).
   `league_sync`/`rival_profile_computation` need the season to have
   started *and* GW1 to finish being played (a few days later, not the
   instant the deadline passes) — they'll still legitimately read PENDING
   in between; don't treat that as a bug.
2. Once GW1 has actually finished, rerun it — `league_sync`,
   `rival_profile_computation`, and `rival_differentials` should all flip to
   PASS (assuming `manager_sync`/`chip_inventory` already passed in step 1).
3. `price_signal_calibration` will likely stay PENDING for longer — FPL
   moves individual player prices based on transfer momentum, not on a
   fixed schedule, so accumulating 30 real (signal, outcome) pairs takes
   multiple gameweeks of repeated `DATA_INGESTION` runs. Re-check
   periodically; there is nothing actionable to do before then.
4. Any `FAIL` at this point is a real regression, not an expected gap —
   investigate via the specific job/table the check names before assuming
   it's another platform quirk.

## Secret rotation

`APP_SHARED_TOKEN` is the one real secret in this system once it's deployed
anywhere beyond local Docker Compose (see `apps/api/auth.py`'s own
docstring) -- rotating it means updating it in both the API's and
`apps/web`'s environments at the same time (they must always match) and
every existing browser session will need to log in again. Otherwise, not
applicable yet: CI workflows (`ci.yml`, `health.yml`, `ingest.yml`) all use
ephemeral, CI-local Postgres containers with fixed, non-sensitive
placeholder credentials, and the FPL API endpoints hit by
`packages.fpl_client` are all public and unauthenticated. Workflows that
touch the real production database (`scheduler-tick.yml`,
`train-production.yml`, `prod-db-recovery.yml`, etc.) use a
`PROD_DATABASE_URL` repository secret instead — see `ARCHITECTURE.md`.
