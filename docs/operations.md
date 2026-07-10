# Operations

This document covers day-to-day operational tasks for the FPL Decision
Engine. As of Milestone 0, scope is limited to the job runner, database
migrations, and the CI/health/scheduled-dispatcher workflows — it will grow
significantly starting Milestone 1 as ingestion, modeling, and the frontend
come online.

## Rerunning a failed job

Every job is tracked as a row in the `jobs` table (see
`packages/database/models/job.py`). To rerun a job manually:

```bash
uv run python scripts/run_job.py \
  --job-type system_health \
  --requested-by <your-name> \
  --trigger-type local
```

Supported `--trigger-type` values: `local`, `api`, `web`, `github_manual`,
`github_schedule`, `retry`. Pass `--idempotency-key <key>` to make a run
idempotent (a second call with the same key returns the existing job instead
of creating a new one). Pass `--git-commit-sha <sha>` to record which commit
produced the run (used by CI workflows).

Currently only `system_health` has an implemented handler. Every other
`JobType` will raise `JobHandlerNotImplementedError` — that's expected until
the corresponding milestone implements it.

## Where logs live

There is no log aggregation system yet. All application and job output is
structured JSON written to stdout (see `packages/shared/logging.py`). In
GitHub Actions, this means job logs are visible directly in the workflow run
output; `health.yml` additionally writes a small Markdown summary via
`$GITHUB_STEP_SUMMARY` and uploads `job_summary.json` as a build artifact.

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

## Secret rotation

Not applicable yet. Milestone 0 has no persistent, deployed secrets — CI
workflows (`ci.yml`, `health.yml`, `scheduled-dispatcher.yml`) all use
ephemeral, CI-local Postgres containers with fixed, non-sensitive
placeholder credentials. This section will be filled in once a real
deployment target and real secrets exist.
