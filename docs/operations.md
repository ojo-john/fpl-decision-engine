# Operations

This document covers day-to-day operational tasks for the FPL Decision
Engine. As of Milestone 1, scope covers the job runner, database migrations,
the CI/health/ingest/scheduled-dispatcher workflows, and data ingestion +
rules versioning. It will grow further as modeling, optimization, and the
frontend come online.

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
  `scheduled-dispatcher.yml`.)
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

### Scheduled dispatcher cadence

`scheduled-dispatcher.yml` dispatches `data_ingestion` every 4 hours (cron
`0 */4 * * *`, changed from Milestone 0's hourly `system_health` placeholder
cadence) using a 4-hour-bucketed idempotency key. This is still a
placeholder cadence, not real deadline-aware scheduling — see the README for
details.

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

## Secret rotation

`APP_SHARED_TOKEN` is the one real secret in this system once it's deployed
anywhere beyond local Docker Compose (see `apps/api/auth.py`'s own
docstring) -- rotating it means updating it in both the API's and
`apps/web`'s environments at the same time (they must always match) and
every existing browser session will need to log in again. Otherwise, not
applicable yet: CI workflows (`ci.yml`, `health.yml`, `ingest.yml`,
`scheduled-dispatcher.yml`) all use ephemeral, CI-local Postgres containers
with fixed, non-sensitive placeholder credentials, and the FPL API endpoints
hit by `packages.fpl_client` are all public and unauthenticated. This
section will be filled in further once a real deployment target exists.
